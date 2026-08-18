"""
order_shadow.py — rulează detectoarele de paritate PER COMANDĂ, LA INGEST (webhook), conform
PROGRAMĂRII alese de merchant (services.automation_config): pentru fiecare detector cu mod `on_order`
îl rulează la comandă (cu delay opțional). Detectoare: DUPLICATE + NR. COLETE + SURPRIZĂ + BLOCKLIST
+ RISC comandă. (Validarea de adresă rulează deja în webhook_service.)

DUPLICATE și RISC se evaluează la nivel de ORGANIZAȚIE (client care comandă în mai multe magazine ale
grupului). Log-only + efecte OH-interne SIGURE (memoizează order.parcel_count, CSQueueItem) — NU atinge
Shopify/AWB. Chemat fire-and-forget din webhook DUPĂ commit, în SESIUNE PROPRIE. Fail-safe: fiecare
detector, pe eroare, face rollback (ca să nu otrăvească sesiunea) și continuă.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

import models
from database import AsyncSessionLocal
from services import automation_config, org_service
from services.settings import resolver
from services.utils import no_cs

from . import blocklist, duplicates, parcels, surprise

logger = logging.getLogger("cron_parity")

# GARDĂ DE POOL: un task per webhook, iar seara webhook-urile (orders/updated) curg în valuri — fără limită,
# zeci de task-uri concurente epuizează QueuePool-ul (5+10) și 500-ează TOT app-ul (incident 18-aug seara).
# Max N task-uri ating DB-ul simultan; restul așteaptă la semafor (nu țin conexiuni). + dedupe per comandă.
import os as _os
_SEM = asyncio.Semaphore(int(_os.environ.get("ORDER_SHADOW_CONCURRENCY", "3")))
_INFLIGHT: set = set()


# ── detectoare per-comandă (întorc True dacă au scris ceva OH-intern) ──────────────────────────

async def _surprise(db, store, order) -> bool:
    if store.domain not in surprise._SURPRISE_SHOPS:
        return False
    n = surprise.analyze(order)
    if n > 0:
        logger.info("ORDER-surprise store=%s order=%s -> +%d parfum-surpriza", store.id, order.name, n)
    return False


async def _parcels(db, store, order) -> bool:
    box_map = await parcels._box_map(db)
    if not box_map:
        return False
    n = parcels.parcel_count(order, box_map)
    if n >= 2 and getattr(order, "parcel_count", None) in (None, 0):
        order.parcel_count = n
        logger.info("ORDER-parcels store=%s order=%s -> memorez %d colete", store.id, order.name, n)
        return True
    return False


async def _duplicates(db, store, order) -> bool:
    """ORG-level: același client (telefon/email bidx) + aceleași SKU-uri, în orice magazin al grupului."""
    cfg = await resolver.resolve_capability(db, store, "duplicates")
    if not cfg.get("enabled"):
        return False
    match = (cfg.get("duplicate_match") or "phone").lower()
    hours = int(cfg.get("duplicate_window_hours") or 24)
    ident, skus = duplicates._identity(order, match), duplicates._skuset(order)
    if not ident or not skus:
        return False
    org_ids = await org_service.org_store_ids(db, store)
    conds = []
    if order.shipping_phone_bidx:
        conds.append(models.Order.shipping_phone_bidx == order.shipping_phone_bidx)
    if order.shipping_email_bidx:
        conds.append(models.Order.shipping_email_bidx == order.shipping_email_bidx)
    if not conds:
        return False
    floor = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = (await db.execute(
        select(models.Order)
        .options(selectinload(models.Order.line_items), selectinload(models.Order.shipments))
        .where(models.Order.store_id.in_(org_ids), models.Order.created_at >= floor,
               models.Order.cancelled_at.is_(None), or_(*conds))
    )).scalars().all()
    grp = [o for o in rows if duplicates._identity(o, match) == ident and duplicates._skuset(o) == skus]
    if len(grp) < 2:
        return False
    added = False
    for o, decision, why in duplicates.resolve_group(grp):
        logger.info("ORDER-dup store=%s order=%s -> %s (%s)", o.store_id, o.name, decision, why)
        if decision == "held":
            # dup-sumă-diferită = poate fi comandă reală → normal HOLD la CS. Pe magazinele fără CS
            # (international) nu ținem hold-uri: încercăm să trimitem (would-ship).
            if automation_config.effective_action(store, "hold") == "ship":
                logger.info("ORDER-dup store=%s order=%s -> would-SHIP (dup-suma-diferita, fara hold)",
                            o.store_id, o.name)
            elif not no_cs(store):
                exists = (await db.execute(
                    select(models.CSQueueItem.id).where(models.CSQueueItem.order_id == o.id))).first()
                if not exists:
                    db.add(models.CSQueueItem(store_id=o.store_id, order_id=o.id, reason="duplicate",
                                              status="open", reason_detail=why, created_by="auto"))
                    added = True
    return added


async def _blocklist(db, store, order) -> bool:
    cfg = await resolver.resolve_capability(db, store, "blocklist")
    if not cfg.get("enabled"):
        return False
    ph, em = order.shipping_phone_bidx, order.shipping_email_bidx
    manual = await blocklist._manual_blocked(db, store)
    why = None
    if (ph and ("phone", ph) in manual) or (em and ("email", em) in manual):
        why = "blocklist manual"
    elif ph:
        threshold = int(cfg.get("serial_refuser_threshold") or blocklist._SERIAL_REFUSER_DEFAULT)
        serial = await blocklist._serial_refusers(db, store, {ph}, threshold, bool(cfg.get("include_failed")))
        if ph in serial:
            why = "serial-refuser (>=%d refuzuri)" % threshold
    if not why:
        return False
    if automation_config.close_instead_of_hold(store):
        # international / fără CS: client blocat = „nu putem trimite" → would-CANCEL (nu hold la o coadă nelucrată)
        logger.info("ORDER-block store=%s order=%s -> would-CANCEL (block, fara CS: %s)", store.id, order.name, why)
        return False
    logger.info("ORDER-block store=%s order=%s -> would-block (%s)", store.id, order.name, why)
    exists = (await db.execute(
        select(models.CSQueueItem.id).where(models.CSQueueItem.order_id == order.id))).first()
    if not exists:
        db.add(models.CSQueueItem(store_id=store.id, order_id=order.id, reason="rule",
                                  status="open", reason_detail="Blocklist: " + why, created_by="auto"))
        return True
    return False


async def _special(db, store, order) -> bool:
    """REGULI SPECIALE (merchant): keyword în tags/note → acțiune PESTE politica implicită.
    Ex. „influencer" → HOLD (chiar și pe internațional unde altfel s-ar trimite). Numele e criptat →
    match doar pe tags+note. hold → CSQueueItem; cancel/ship → doar log (shadow)."""
    rules = automation_config.special_rules(store)
    if not rules:
        return False
    blob = " ".join([(order.tags or ""), (order.note or "")]).lower()
    added = False
    for r in rules:
        if r["contains"] and r["contains"] in blob:
            action = r["action"]
            logger.info("ORDER-special store=%s order=%s -> would-%s (regula: '%s')",
                        store.id, order.name, action.upper(), r["contains"])
            if action == "hold":
                exists = (await db.execute(
                    select(models.CSQueueItem.id).where(models.CSQueueItem.order_id == order.id))).first()
                if not exists:
                    db.add(models.CSQueueItem(store_id=store.id, order_id=order.id, reason="rule",
                                              status="open", reason_detail="Regula speciala: " + r["contains"],
                                              created_by="auto"))
                    added = True
    return added


_HANDLERS = {
    "special":    _special,
    "duplicates": _duplicates,
    "parcels":    _parcels,
    "surprise":   _surprise,
    "blocklist":  _blocklist,
}


async def run_for_order(store_id: int, order_id: int) -> None:
    """Fire-and-forget din webhook DUPĂ commit. Rulează detectoarele cu mod `on_order` (cu delay), izolat.
    Gardat de semafor (pool) + dedupe (o comandă nu rulează de 2 ori simultan la rafale de orders/updated)."""
    if order_id in _INFLIGHT:
        return
    _INFLIGHT.add(order_id)
    try:
        # 1) programarea: care detectoare on_order + delay (sesiune scurtă, sub semafor)
        async with _SEM:
            async with AsyncSessionLocal() as db:
                store = await db.get(models.Store, store_id)
                if not store or not store.is_active:
                    return
                on_order = [k for k in automation_config.ON_ORDER_DETECTORS
                            if automation_config.mode_of(store, k) == "on_order"]
                if not on_order:
                    return
                delay = max((automation_config.minutes_of(store, k) for k in on_order), default=0)
        if delay > 0:
            await asyncio.sleep(min(delay, 120) * 60)     # sleep FĂRĂ semafor/sesiune ținute
        # 2) rulează, în sesiune proprie, sub semafor
        async with _SEM, AsyncSessionLocal() as db:
            store = await db.get(models.Store, store_id)
            order = (await db.execute(
                select(models.Order)
                .options(selectinload(models.Order.line_items), selectinload(models.Order.shipments))
                .where(models.Order.id == order_id)
            )).scalar_one_or_none()
            if not store or not order or order.cancelled_at:
                return
            for key in on_order:
                fn = _HANDLERS.get(key)
                if not fn:
                    continue
                try:
                    if await fn(db, store, order):
                        await db.commit()
                except Exception as e:
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                    logger.warning("ORDER-%s err store=%s order=%s: %s", key, store_id, order_id, e)
    except Exception:
        logger.exception("order-shadow crashed store=%s order=%s", store_id, order_id)
    finally:
        _INFLIGHT.discard(order_id)
