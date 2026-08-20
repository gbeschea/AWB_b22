"""Courier status → Shopify lifecycle sync + refusal automation.

For every shipment that has an AWB and isn't in a terminal state yet, we poll the courier
for its live status, record it, and drive Shopify:

  • parcel is moving (in_transit / shipped / at a pickup point / delivered) and the order
    isn't fulfilled yet → create a Shopify fulfillment with the AWB as tracking
    (emails the customer the "shipped" notice iff `store.fulfill_notify_customer`);
  • every later poll → push a delivery EVENT (IN_TRANSIT / OUT_FOR_DELIVERY / DELIVERED /
    FAILURE …) so the admin + customer see live delivery status;
  • courier reports REFUSED / RETURNED and `store.auto_cancel_on_refusal` is on → cancel the
    order (restock + notify per settings) — but ONLY for unpaid (COD) orders. PAID orders
    (card etc.) are never touched: the merchant handles refunds themselves.

The canonical status mapping mirrors the `orders_view` SQL CASE in main.py so the DB view
and this poller always agree.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time as _time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models
from database import AsyncSessionLocal, engine
from services import shopify_service
from services.couriers import get_courier_service

logger = logging.getLogger(__name__)

# Once a shipment reaches one of these, there's nothing left to poll.
_TERMINAL = {"delivered", "refused", "canceled"}
# Canonical states that mean "the courier physically has the parcel" → safe to fulfill in
# Shopify. We deliberately DON'T fulfill on "processed" (AWB registered but maybe not yet
# scanned — it could still be voided before pickup).
_FULFILL_TRIGGER = {"in_transit", "shipped", "pickup_office", "delivered"}

# Per-run cap so one cycle can't run unbounded; the loop picks up the rest next cycle.
# Debitul per tură. ATENȚIE: o tură ține O SINGURĂ sesiune deschisă cât durează toate apelurile, deci
# limita e și un plafon de timp-de-ținere a conexiunii. Am încercat 1000 ca să golesc mai repede coada
# moștenită (~70k) și am epuizat pool-ul în câteva minute (1000 × 0,2s ≈ 200s cu sesiunea blocată, peste
# traficul de webhook). 250 e valoarea care ține. Ca să crești debitul REAL, taie tura în bucăți cu sesiuni
# separate — nu urca limita. Reglabil din env pentru experimente controlate.
_DEFAULT_LIMIT = int(os.environ.get("AWB_STATUS_POLL_LIMIT", "250"))
# A tiny pause between courier calls so we don't hammer their APIs.
_PER_CALL_SLEEP = 0.2
# Postgres advisory-lock key — ensures only ONE poller runs across workers/containers.
_ADVISORY_LOCK_KEY = 4820_1001


def canonical_status(raw: Optional[str]) -> Optional[str]:
    """Free-text courier status → Order Hub canonical status. Mirrors orders_view's CASE.
    Order matters: delivered/refused/canceled are checked before the in-transit buckets."""
    s = (raw or "").strip().lower()
    if not s:
        return None
    def has(*subs: str) -> bool:
        return any(x in s for x in subs)

    if has("delivered", "livrat"):
        return "delivered"
    if has("refus", "return", "retur"):
        return "refused"
    if has("cancel", "anulat"):
        return "canceled"
    if has("locker", "parcelshop", "pick-up", "easybox", "ready for pickup"):
        return "pickup_office"
    if has("in curs", "tranzit", "transit", "out for delivery", "in livrare"):
        return "in_transit"
    if has("expediat", "warehouse", "picked up", "shipped", "colectat", "in depozit"):
        return "shipped"
    if has("proces", "registered", "awb", "generat"):
        return "processed"
    return None


def _tracking_url(courier: Optional[str], awb: str) -> Optional[str]:
    c = (courier or "").lower()
    if "sameday" in c:
        return f"https://sameday.ro/track-awb/{awb}"
    if "dpd" in c:
        return f"https://tracking.dpd.ro?shipmentNumber={awb}"
    if "fan" in c:
        return f"https://www.fancourier.ro/awb-tracking/?tracking_number={awb}"
    if "gls" in c:
        return f"https://gls-group.com/RO/en/parcel-tracking?match={awb}"
    if "cargus" in c or "urgent" in c:
        return f"https://www.cargus.ro/track-awb/?tracking_number={awb}"
    if "econt" in c:
        return f"https://www.econt.com/en/services/track-shipment/{awb}"
    if "packeta" in c or "zasilkovna" in c:
        return f"https://tracking.packeta.com/en/tracking?id={awb}"
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _select_shipments(db: AsyncSession, store_id: Optional[int], limit: int) -> List[models.Shipment]:
    """Shipments worth polling: have an AWB, not in a terminal courier state, and the order
    isn't cancelled. Eager-loads order + store so the sync has everything it needs."""
    stmt = (
        select(models.Shipment)
        .join(models.Order, models.Order.id == models.Shipment.order_id)
        .options(selectinload(models.Shipment.order).selectinload(models.Order.store))
        .where(
            models.Shipment.awb.isnot(None),
            models.Order.cancelled_at.is_(None),
            or_(
                models.Shipment.derived_status.is_(None),
                models.Shipment.derived_status.notin_(list(_TERMINAL)),
            ),
        )
        # Ordonăm după ÎNCERCARE, nu după status: un poll eșuat (curier nerezolvabil, API căzut) nu mai
        # poate ține shipmentul în capul cozii la infinit (starvation măsurat: 99,7% nepollate).
        # `id DESC` = coletele RECENTE primele. Cu `id ASC` se pollau întâi comenzile cele mai vechi
        # (livrate demult, fără valoare operațională) iar cele din tranzit așteptau zile. Nimic nu se
        # pierde: vechile intră tot în coadă, doar după cele care contează azi.
        .order_by(models.Shipment.last_poll_at.asc().nullsfirst(), models.Shipment.id.desc())
        .limit(limit)
    )
    if store_id is not None:
        stmt = stmt.where(models.Order.store_id == store_id)
    return (await db.execute(stmt)).scalars().all()


async def _sync_shipment(db: AsyncSession, shipment: models.Shipment) -> str:
    """Poll one shipment and reconcile Shopify. Returns a short action tag for the summary."""
    # Ștampilăm ÎNCERCAREA înainte de orice ieșire (skip/eroare/succes) — ăsta e mecanismul care împiedică
    # coada să flămânzească.
    shipment.last_poll_at = _now()
    order = shipment.order
    store = order.store if order else None
    if not order or not store or not store.is_active or not order.shopify_order_id:
        return "skip:no-order"
    if not getattr(store, "status_sync_enabled", True):
        return "skip:sync-off"

    svc = get_courier_service(shipment.courier or shipment.account_key or "")
    if not svc:
        return "skip:no-courier"

    try:
        tr = await svc.track_awb(db, shipment.awb, shipment.account_key)
    except Exception as e:
        logger.info("track_awb failed for %s (%s): %s", shipment.awb, shipment.courier, e)
        return "err:track"

    raw = getattr(tr, "status", None) or getattr(tr, "status_raw", None)
    canon = canonical_status(raw)
    prev_derived = shipment.derived_status      # pt detecția TRANZIȚIEI de status (cod_capture event-driven)
    # Always record the latest courier status.
    shipment.last_status = (raw or "")[:255] if raw else shipment.last_status
    shipment.last_status_at = getattr(tr, "date", None) or _now()
    if canon:
        shipment.derived_status = canon
    if not canon:
        return "noop:unmapped"

    action = f"status:{canon}"

    # 1) Fulfill in Shopify once the parcel is actually moving — DOAR pentru curierii direcți.
    # xConnector/Frisbo fulfill-uiesc singure comanda; dacă am împinge și noi, ar ieși fulfillment DUBLU
    # (două tracking-uri pe aceeași comandă, clientul primește două emailuri de expediere).
    if (canon in _FULFILL_TRIGGER and not shipment.shopify_fulfillment_id
            and not getattr(svc, "owns_shopify_fulfillment", False)):
        try:
            gid = await shopify_service.create_fulfillment_with_tracking(
                store, order.shopify_order_id,
                tracking_number=shipment.awb,
                tracking_company=(shipment.courier or "").upper() or None,
                tracking_url=_tracking_url(shipment.courier, shipment.awb),
                notify_customer=bool(getattr(store, "fulfill_notify_customer", False)),
            )
            if gid:
                shipment.shopify_fulfillment_id = str(gid).split("/")[-1]
                shipment.fulfillment_created_at = _now()
                order.shopify_status = "fulfilled"
                order.fulfilled_at = order.fulfilled_at or _now()
                action = f"fulfilled+{canon}"
        except Exception as e:
            logger.info("fulfillmentCreate failed for order %s: %s", order.name, e)

    # 2) Push the delivery event so Shopify shows live progress.
    if shipment.shopify_fulfillment_id:
        try:
            gid = f"gid://shopify/Fulfillment/{shipment.shopify_fulfillment_id}"
            await shopify_service.add_fulfillment_event(store, gid, canon)
        except Exception as e:
            logger.info("fulfillmentEvent failed for order %s: %s", order.name, e)

    if canon == "delivered":
        order.fulfilled_at = order.fulfilled_at or _now()
        # cod_capture EVENT-DRIVEN: la TRANZIȚIA în livrat, dacă magazinul a ales modul on_delivered pentru
        # cod_capture, loghează decizia COD pt ACEASTĂ comandă (shadow). Doar pe tranziție (prev != delivered),
        # ca să nu spameze la fiecare poll. Folosește doar câmpuri de order (fără a atinge shipments → greenlet).
        if prev_derived != "delivered":
            try:
                from services import automation_config
                from services.cron_parity import cod_capture as _cc
                if (automation_config.mode_of(store, "cod_capture") == "on_delivered"
                        and _cc.is_cod(order)
                        and (order.financial_status or "").lower() in ("pending", "authorized", "")):
                    logger.info("COD-event store=%s order=%s LIVRAT+pending -> would mark PAID (total=%s)",
                                store.id, order.name, order.total_price)
            except Exception as e:
                logger.info("cod-event failed for %s: %s", order.name, e)

    # 3) Refusal automation — COD only, never paid.
    if canon == "refused":
        action = await _handle_refusal(shipment, order, store)

    return action


async def _handle_refusal(shipment: models.Shipment, order: models.Order, store: models.Store) -> str:
    """Auto-cancel a refused/returned order when the merchant opted in — but ONLY if it's a
    COD order (never paid). Paid orders are left for the merchant."""
    if not getattr(store, "auto_cancel_on_refusal", False):
        return "refused:no-auto"
    if order.cancelled_at is not None:
        return "refused:already-cancelled"
    is_paid = (order.financial_status or "").lower() == "paid"
    if is_paid:
        # Card/prepaid — the app must not cancel/refund; the merchant handles it.
        logger.info("Order %s refused but PAID — leaving for merchant.", order.name)
        return "refused:paid-skip"
    try:
        await shopify_service.cancel_order(
            store, order.shopify_order_id,
            reason="DECLINED",
            refund=False,          # COD was never paid → nothing to refund
            restock=bool(getattr(store, "refusal_restock", True)),
            notify_customer=bool(getattr(store, "refusal_notify_customer", False)),
            staff_note="Order Hub: parcel refused/returned by the courier — cancelled automatically.",
        )
        order.cancelled_at = _now()
        order.shopify_status = "cancelled"
        return "refused:cancelled"
    except Exception as e:
        logger.info("orderCancel failed for %s: %s", order.name, e)
        return "refused:cancel-err"


async def poll(store_id: Optional[int] = None, limit: int = _DEFAULT_LIMIT) -> Dict[str, Any]:
    """Run one polling pass (all active stores, or one store). Opens its own DB session and
    commits per shipment so a single failure never drops the batch."""
    summary: Dict[str, int] = {}
    processed = 0
    async with AsyncSessionLocal() as db:
        shipments = await _select_shipments(db, store_id, limit)
        for ship in shipments:
            try:
                action = await _sync_shipment(db, ship)
                await db.commit()
            except Exception as e:
                await db.rollback()
                action = "err:exc"
                logger.exception("status sync error for shipment %s: %s", ship.id, e)
            summary[action] = summary.get(action, 0) + 1
            processed += 1
            await asyncio.sleep(_PER_CALL_SLEEP)
    return {"processed": processed, "actions": summary}


# --------------------
# Background loop (single-runner via a Postgres advisory lock)
# --------------------

async def _try_advisory_lock() -> bool:
    async with engine.connect() as conn:
        got = (await conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY}
        )).scalar()
        # Connection closes on context exit → the session-level lock is released with it,
        # so we acquire per-cycle and never hold it across the sleep.
        return bool(got)


_GHOST_EVERY_SEC = float(os.environ.get("GHOST_RECONCILE_INTERVAL_SEC", str(6 * 3600)))
_ghost_next_at = 0.0


_INV_EVERY_SEC = float(os.environ.get("INVENTORY_GUARD_INTERVAL_SEC", str(3 * 3600)))
_inv_next_at = 0.0


async def _maybe_inventory_guard() -> None:
    global _inv_next_at
    now = _time.monotonic()
    if now < _inv_next_at:
        return
    _inv_next_at = now + _INV_EVERY_SEC
    from services import inventory_guard
    res = await inventory_guard.run_once()
    if res.get("new_alerts") or res.get("cleared"):
        logger.info("inventory-guard: %s", res)


async def _maybe_reconcile_ghosts() -> None:
    """Rulează reconcilierea fantomelor cel mult o dată la `_GHOST_EVERY_SEC`. Prima trecere se face
    la scurt timp după pornire (contorul începe de la 0) ca un restart să nu amâne recuperarea cu 6h."""
    global _ghost_next_at
    now = _time.monotonic()
    if now < _ghost_next_at:
        return
    _ghost_next_at = now + _GHOST_EVERY_SEC     # setat ÎNAINTE de lucru: un eșec nu declanșează o buclă strânsă
    from services import ghost_reconcile
    res = await ghost_reconcile.run_once()
    if res.get("cancelled") or res.get("fulfilled"):
        logger.info("ghost-reconcile: %s", res)


async def poll_loop(interval_sec: int) -> None:
    """Forever: every `interval_sec`, if we win the advisory lock, run one polling pass.
    Started as an asyncio task at app startup. Survives per-cycle errors."""
    logger.info("status-sync loop started (interval=%ss)", interval_sec)
    # Small initial delay so startup (view creation, webhook reconcile) settles first.
    await asyncio.sleep(20)
    while True:
        try:
            async with engine.connect() as conn:
                got = (await conn.execute(
                    text("SELECT pg_try_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY}
                )).scalar()
                if got:
                    try:
                        res = await poll()
                        if res["processed"]:
                            logger.info("status-sync pass: %s", res)
                        # Auto-AWB și-a luat bucla proprie (services.auto_awb_service.run_forever,
                        # implicit 300s): expedierea nu mai depinde de ritmul pollingului de status și
                        # nici nu mai poate fi înfometată de o măturare lungă.
                        # Reconcilierea „fantomelor" rămâne aici — e curățenie, are buget de timp propriu.
                        try:
                            await _maybe_reconcile_ghosts()
                        except Exception:
                            logger.exception("ghost-reconcile pass failed")
                        # Tichetele automate de pe comenzi deja plecate n-au obiect — curățate aici,
                        # pe toate magazinele, nu doar pe cele cu auto-AWB.
                        try:
                            from services import cs_queue_janitor
                            await cs_queue_janitor.close_moot_items()
                        except Exception:
                            logger.exception("cs-janitor pass failed")
                        # Garda de stoc — rar (3h implicit), alertele sunt pentru reaprovizionare,
                        # nu pentru reacție în minute.
                        try:
                            await _maybe_inventory_guard()
                        except Exception:
                            logger.exception("inventory-guard pass failed")
                    finally:
                        await conn.execute(
                            text("SELECT pg_advisory_unlock(:k)"), {"k": _ADVISORY_LOCK_KEY}
                        )
                else:
                    logger.debug("status-sync: another worker holds the lock; skipping.")
        except Exception:
            logger.exception("status-sync loop cycle failed")
        await asyncio.sleep(interval_sec)
