"""Automated AWB creation (opt-in, safe by construction).

For a store that turned it on AND picked a courier, this makes AWBs for eligible orders on a
schedule. Eligible = valid address + not cancelled + no AWB yet + at least `auto_awb_delay_minutes`
old (a cancellation / address-fix / COD-confirmation buffer). It only runs inside the merchant's
window (or all day / continuously if no window is set) and NEVER guesses a courier — it does
nothing until `auto_awb_account_key` is set. Bounded per store per pass.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

import models
from database import AsyncSessionLocal

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("Europe/Bucharest")
_PER_STORE_CAP = 25            # bound dispatch per store per pass
_VALID_ADDR = ("valid", "validat")


def _in_span_min(now_min: int, s, e) -> bool:
    """now_min inside [s,e) minutes-of-day, wrapping past midnight when e<s. s/e None or equal → False."""
    if s is None or e is None or s == e:
        return False
    return (s <= now_min < e) if s < e else (now_min >= s or now_min < e)


def window_ok(store: models.Store) -> bool:
    """True if auto-AWB may run now (Bucharest local): inside the ALLOWED window (whole hours; NULL = all day)
    AND outside the BLACKOUT interval (minutes-of-day; NULL = none)."""
    now = datetime.now(_TZ)
    s, e = store.awb_window_start, store.awb_window_end
    if s is not None and e is not None and s != e:
        allowed = (s <= now.hour < e) if s < e else (now.hour >= s or now.hour < e)  # e<s wraps past midnight
        if not allowed:
            return False
    bs, be = getattr(store, "awb_blackout_start", None), getattr(store, "awb_blackout_end", None)
    if _in_span_min(now.hour * 60 + now.minute, bs, be):
        return False
    return True


async def run_store(db, store: models.Store) -> Dict[str, Any]:
    if not getattr(store, "auto_awb_enabled", False):
        return {"skipped": "off"}
    if not window_ok(store):
        return {"skipped": "outside-window"}

    # Lazy imports avoid a module-load cycle (courier_actions imports services.*).
    from routes.courier_actions import _profile_base, _create_one, _request_pickup
    from services import shipment_rules

    # Conditional routing: enabled rules (this store or shared), evaluated in priority order.
    rules = (await db.execute(
        select(models.ShipmentRule).where(
            models.ShipmentRule.enabled.is_(True),
            (models.ShipmentRule.store_id == store.id) | (models.ShipmentRule.store_id.is_(None)),
        ).order_by(models.ShipmentRule.priority.asc(), models.ShipmentRule.id.asc())
    )).scalars().all()

    default_profile_id = getattr(store, "auto_awb_profile_id", None)
    default_account = getattr(store, "auto_awb_account_key", None)
    if not (rules or default_profile_id or default_account):
        return {"skipped": "off"}  # nothing to route with — never guess a courier

    delay = int(getattr(store, "auto_awb_delay_minutes", 0) or 0)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=delay)

    has_awb = select(models.Shipment.id).where(
        models.Shipment.order_id == models.Order.id, models.Shipment.awb.isnot(None))
    cs_open = select(models.CSQueueItem.id).where(
        models.CSQueueItem.order_id == models.Order.id, models.CSQueueItem.status != "solved")
    stmt = (
        select(models.Order)
        .options(selectinload(models.Order.line_items), selectinload(models.Order.store),
                 selectinload(models.Order.shipments))
        .where(
            models.Order.store_id == store.id,
            models.Order.cancelled_at.is_(None),
            models.Order.address_status.in_(_VALID_ADDR),
            models.Order.created_at <= cutoff,
            ~has_awb.exists(),
            # Ghost-AWB / already-shipped guard: never auto-AWB an order Shopify already reports as
            # fulfilled (shipped by another system / a manual label) — that would double-ship.
            models.Order.fulfilled_at.is_(None),
            # HOLD-URILE SUNT OBLIGATORII, nu decorative: fără ele, tot ce opresc detectoarele (dublură,
            # client blocat, adresă proastă, regulă specială „influencer") ar primi AWB automat oricum —
            # exact ce încearcă să prevină. Două surse, ambele necesare: coada CS a OH și hold-ul pus în
            # Shopify (pe care îl poate pune și un om, sau cronul). (Blocantul #1 din auditul cronului.)
            ~cs_open.exists(),
            or_(models.Order.is_on_hold_shopify.is_(None),
                models.Order.is_on_hold_shopify.is_(False)),
        )
        .order_by(models.Order.created_at.asc())
        .limit(_PER_STORE_CAP)
    )
    orders = (await db.execute(stmt)).scalars().all()
    if not orders:
        return {"eligible": 0}

    # Comenzile pe care le-am ABANDONAT (prea multe eșecuri) sau le-am predat deja la CS nu se mai
    # reîncearcă: altfel ocupă permanent cota de 25/magazin (ordonată crescător pe dată) și blochează
    # comenzile noi — starvation tăcut, aceeași clasă ca la polling-ul de status.
    try:
        from services import awb_giveup
        _gave_up = await awb_giveup.gave_up_ids(db, [o.id for o in orders])
        if _gave_up:
            orders = [o for o in orders if o.id not in _gave_up]
            logger.info("auto-awb %s: sar %d comenzi abandonate/la CS", store.domain, len(_gave_up))
    except Exception as e:
        logger.info("auto-awb: gave_up_ids indisponibil (%s) — continui fără filtru", e)
    if not orders:
        return {"eligible": 0, "skipped_gave_up": True}

    # Resolve a profile id → (account_key, options) once per pass.
    resolved: Dict[int, Any] = {}
    async def _resolve(pid: int):
        if pid not in resolved:
            resolved[pid] = await _profile_base(db, store, pid)
        return resolved[pid]

    created, errors, no_route, waiting_multi = [], [], 0, 0
    by_account: Dict[str, list] = {}
    for o in orders:
        # Precedence: a manually-assigned profile > a matching rule > the store default profile.
        pid = getattr(o, "assigned_profile_id", None) or shipment_rules.pick_profile_id(o, rules) or default_profile_id
        try:
            if pid:
                account_key, base_opts = await _resolve(pid)
                opts = dict(base_opts)
            elif default_account:
                account_key, opts = default_account, {}
            else:
                no_route += 1
                continue  # no rule matched and no default courier — leave it for a human
            # Per-order parcel count (remembered in OH, synced from the Shopify metafield) overrides the
            # profile's default_parcels — so a 3-box order ships as 3 parcels even on a 1-parcel profile.
            # Marcat EXPLICIT: altfel packing.apply_to_options îl SUPRASCRIE cu regula de packing a
            # magazinului, iar numărul per-comandă (metafield-ul depozitului / harta SKU→cutii) se pierde
            # tăcut. Ordinea din cron e: metafield per-comandă > cutii-per-SKU > 1.
            if getattr(o, "parcel_count", None):
                opts["parcels_count"] = int(o.parcel_count)
                opts["_explicit"] = list(set(list(opts.get("_explicit") or []) + ["parcels_count"]))
            # SAFETY: never auto-ship an order that spans multiple fulfillment LOCATIONS from a
            # single AWB (that would ship everything from one location). Flag it to CS and wait
            # for a human to split it / add a location rule. Fail-soft: a check error still ships.
            if await _spans_multiple_locations(o.store or store, o):
                from routes.cs_queue import enqueue_order
                await enqueue_order(db, store, o, reason="manual",
                                    detail="Multiple locations — needs a split or a location rule",
                                    created_by="auto")
                await db.commit()
                waiting_multi += 1
                continue
            r = await _create_one(db, store, o, account_key, opts)
            await db.commit()
            created.append(r["awb"])
            by_account.setdefault(account_key, []).append(r["awb"])
            # Contorul de eșecuri se ZEROIZEAZĂ la succes — altfel mecanismul e o capcană cu sens unic:
            # eșecurile de acum două săptămâni s-ar aduna peste cele de azi și comanda ar fi abandonată
            # deși de fapt merge. Cablat în ACEEAȘI schimbare cu on_failure (avertismentul review-ului).
            try:
                from services import awb_giveup
                await awb_giveup.reset(db, o)
            except Exception:
                pass
        except Exception as e:
            await db.rollback()
            errors.append(str(e))
            # Decizia completă după un eșec: clasifică (tranzitoriu/permanent/config), incrementează
            # contorul și, la prag, predă comanda la CS în loc s-o reîncerce la infinit.
            try:
                from services import awb_giveup
                await awb_giveup.on_failure(db, store, o, e)
                await db.commit()
            except Exception as ge:
                await db.rollback()
                logger.info("auto-awb: giveup a picat pt %s: %s", getattr(o, "name", "?"), ge)

    # One pickup request per courier account (an order routed to DPD and another to FAN each get theirs).
    for acct_key, awbs in by_account.items():
        try:
            await _request_pickup(db, store, acct_key, awbs, {})
        except Exception:
            pass
    logger.info("auto-awb %s: created=%d errors=%d no-route=%d waiting-multi=%d",
                store.domain, len(created), len(errors), no_route, waiting_multi)
    return {"created": len(created), "errors": len(errors), "no_route": no_route,
            "waiting_multi_location": waiting_multi}


async def _spans_multiple_locations(store, order) -> bool:
    """True if the order still has items to ship from 2+ distinct fulfillment locations. Uses
    the fulfillment-order groups (assignedLocation name only — no read_locations needed).
    Fail-soft: returns False on any error so a check failure never blocks auto-AWB."""
    if not getattr(order, "shopify_order_id", None):
        return False
    try:
        from services import shopify_service
        groups = await shopify_service.get_fulfillment_order_groups(store, order.shopify_order_id)
        open_locs = {(g.get("location") or "") for g in groups if g.get("open")}
        return len(open_locs) > 1
    except Exception as e:
        logger.info("multi-location check failed for %s: %s", getattr(order, "name", "?"), e)
        return False


async def run_all() -> Dict[str, Any]:
    """One pass over every store that has auto-AWB enabled. Own DB session."""
    total = {"stores": 0, "created": 0, "errors": 0}
    async with AsyncSessionLocal() as db:
        stores = (await db.execute(
            select(models.Store).where(models.Store.is_active.is_(True),
                                       models.Store.auto_awb_enabled.is_(True))
        )).scalars().all()
        for s in stores:
            res = await run_store(db, s)
            total["stores"] += 1
            total["created"] += res.get("created", 0)
            total["errors"] += res.get("errors", 0)
    return total
