"""
order_shadow.py — rulează detectoarele de paritate PER COMANDĂ, LA INGEST (webhook), în locul
sweep-ului de 15 min: DUPLICATE + NR. COLETE + SURPRIZĂ. (Validarea de adresă rulează deja în
services.webhook_service la fiecare ingest.)

Log-only + efecte OH-interne SIGURE (memoizează `order.parcel_count` pt AWB, CSQueueItem la duplicate
„held") — NU atinge Shopify, NU creează AWB. Chemat din services.webhook_service DUPĂ ce comanda e
comisă, într-o SESIUNE PROPRIE (izolat de webhook → o eroare aici nu poate rupe ingestul). Fiecare
detector e fail-safe: pe eroare face rollback (ca să nu otrăvească sesiunea) și trece mai departe.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

import models
from database import AsyncSessionLocal
from services.settings import resolver
from services.utils import no_cs

from . import duplicates, parcels, surprise

logger = logging.getLogger("cron_parity")


async def _surprise(db, store, order) -> None:
    # surpriza-parfum: doar brandurile de parfum (garda din surprise)
    if store.domain not in surprise._SURPRISE_SHOPS:
        return
    n = surprise.analyze(order)
    if n > 0:
        logger.info("ORDER-surprise store=%s order=%s -> +%d parfum-surpriza", store.id, order.name, n)


async def _parcels(db, store, order) -> bool:
    """Memoizează order.parcel_count când coletele ≥2 (default-ul 1 ar fi greșit la AWB). Întoarce True dacă a scris."""
    box_map = await parcels._box_map(db)
    if not box_map:
        return False
    n = parcels.parcel_count(order, box_map)
    if n >= 2 and getattr(order, "parcel_count", None) in (None, 0):
        order.parcel_count = n
        logger.info("ORDER-parcels store=%s order=%s -> memorez %d colete", store.id, order.name, n)
        return True
    return False


async def _dup(db, store, order) -> bool:
    """Duplicate pt ACEASTĂ comandă: prefiltru SQL îngust pe telefon/email (rafinat cu _identity în Python),
    nu scanăm toată fereastra. CSQueueItem pe „held". Întoarce True dacă a adăugat ceva."""
    cfg = await resolver.resolve_capability(db, store, "duplicates")
    if not cfg.get("enabled"):
        return False
    match = (cfg.get("duplicate_match") or "phone").lower()
    hours = int(cfg.get("duplicate_window_hours") or 24)
    ident, skus = duplicates._identity(order, match), duplicates._skuset(order)
    if not ident or not skus:
        return False
    conds = []
    if order.shipping_phone:
        conds.append(models.Order.shipping_phone == order.shipping_phone)
    if order.shipping_email:
        conds.append(models.Order.shipping_email == order.shipping_email)
    if not conds:
        return False
    floor = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = (await db.execute(
        select(models.Order)
        .options(selectinload(models.Order.line_items), selectinload(models.Order.shipments))
        .where(models.Order.store_id == store.id, models.Order.created_at >= floor,
               models.Order.cancelled_at.is_(None), or_(*conds))
    )).scalars().all()
    grp = [o for o in rows if duplicates._identity(o, match) == ident and duplicates._skuset(o) == skus]
    if len(grp) < 2:
        return False
    added = False
    for o, decision, why in duplicates.resolve_group(grp):
        logger.info("ORDER-dup store=%s order=%s -> %s (%s)", store.id, o.name, decision, why)
        if decision == "held" and not no_cs(store):
            exists = (await db.execute(
                select(models.CSQueueItem.id).where(models.CSQueueItem.order_id == o.id))).first()
            if not exists:
                db.add(models.CSQueueItem(store_id=store.id, order_id=o.id, reason="duplicate",
                                          status="open", reason_detail=why, created_by="auto"))
                added = True
    return added


async def run_for_order(store_id: int, order_id: int) -> None:
    """Punctul de intrare chemat (fire-and-forget) din webhook DUPĂ commit. Sesiune proprie, izolată."""
    try:
        async with AsyncSessionLocal() as db:
            store = await db.get(models.Store, store_id)
            if not store:
                return
            order = (await db.execute(
                select(models.Order)
                .options(selectinload(models.Order.line_items), selectinload(models.Order.shipments))
                .where(models.Order.id == order_id)
            )).scalar_one_or_none()
            if not order:
                return
            # fiecare detector izolat: pe eroare rollback (ca să nu otrăvească sesiunea) + continuă
            for name, fn, writes in (("surprise", _surprise, False),
                                     ("parcels", _parcels, True),
                                     ("dup", _dup, True)):
                try:
                    changed = await fn(db, store, order)
                    if writes and changed:
                        await db.commit()
                except Exception as e:
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                    logger.warning("ORDER-%s err store=%s order=%s: %s", name, store_id, order_id, e)
    except Exception:
        logger.exception("order-shadow crashed store=%s order=%s", store_id, order_id)
