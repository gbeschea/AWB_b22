"""
blocklist.py — paritate cu blocklist-ul + serial-refuser din cron (blocklist_gid_sweep / serial-refuser,
memoria serial-refuser-blocklist). Un client BLOCAT → comanda lui NU primește auto-AWB (cronul o anulează
cu restock, fără refund pe COD). Două surse:
 (1) MANUAL — intrări în tabelul `blocklist` (telefon/email pe BLIND-INDEX, puse de noi din UI/CS);
 (2) SERIAL-REFUSER — regulă: clienți cu ≥ prag comenzi cu livrare REFUZATĂ în istoricul OH.

Shadow (log-only, model ADDR_SHADOW): log "would-block" + CSQueueItem reason="rule" (intern, sigur — fără
nicio scriere în Shopify). PROTECȚIE LIVRARE: comenzile deja expediate/fulfilled nu se ating.
Matching pe blind-index (shipping_phone_bidx/email_bidx) → fără decriptare PII (paritate cu Level-2 PCD).
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Set

from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

import models
from services.settings import resolver

logger = logging.getLogger("cron_parity.blocklist")

_REFUSED = ("refus", "retur", "return", "refuz")   # substring pe status shipment = livrare refuzată/returnată
_FAILED = ("fail", "esuat", "eșuat", "nelivr", "undeliver")   # + preset „agresiv": livrare eșuată, nu doar refuz explicit
_SERIAL_REFUSER_DEFAULT = 2                          # ≥ atâtea comenzi refuzate = serial-refuser
_LOOKBACK_H = 72                                     # comenzi recente fără AWB de verificat


async def _manual_blocked(db, store: models.Store) -> Set:
    """{(match_type, value_bidx)} active pt magazin (sau globale, store_id NULL)."""
    rows = (await db.execute(
        select(models.BlocklistEntry.match_type, models.BlocklistEntry.value_bidx).where(
            models.BlocklistEntry.active.is_(True),
            (models.BlocklistEntry.store_id == store.id) | (models.BlocklistEntry.store_id.is_(None)),
        )
    )).all()
    return {(r[0], r[1]) for r in rows}


async def _serial_refusers(db, store: models.Store, phone_bidxs: Set[str], threshold: int,
                           include_failed: bool = False) -> Set[str]:
    """Din bidx-urile date, care au ≥ threshold comenzi cu shipment REFUZAT (opțional + eșuat) în istoric."""
    if not phone_bidxs:
        return set()
    keys = _REFUSED + (_FAILED if include_failed else ())
    refused = None
    for k in keys:
        c = models.Shipment.last_status.ilike("%" + k + "%")
        refused = c if refused is None else (refused | c)
    q = (
        select(models.Order.shipping_phone_bidx)
        .join(models.Shipment, models.Shipment.order_id == models.Order.id)
        .where(models.Order.store_id == store.id,
               models.Order.shipping_phone_bidx.in_(list(phone_bidxs)),
               refused)
        .group_by(models.Order.shipping_phone_bidx)
        .having(func.count(func.distinct(models.Order.id)) >= threshold)
    )
    return {r[0] for r in (await db.execute(q)).all()}


async def run_shadow(db, store: models.Store) -> Dict[str, int]:
    """Detectează clienți blocați pe comenzile recente FĂRĂ AWB, LOG-ONLY. HOLD → CSQueueItem reason='rule'.
    Config (enabled/prag/include_failed) = presetul capabilității, moștenit org→magazin (resolver)."""
    cfg = await resolver.resolve_capability(db, store, "blocklist")
    if not cfg.get("enabled"):
        return {"skipped": "off"}
    threshold = int(cfg.get("serial_refuser_threshold") or _SERIAL_REFUSER_DEFAULT)
    include_failed = bool(cfg.get("include_failed"))
    floor = datetime.now(timezone.utc) - timedelta(hours=_LOOKBACK_H)
    has_awb = select(models.Shipment.id).where(
        models.Shipment.order_id == models.Order.id, models.Shipment.awb.isnot(None))
    orders = (await db.execute(
        select(models.Order)
        .options(selectinload(models.Order.shipments))
        .where(models.Order.store_id == store.id,
               models.Order.created_at >= floor,
               models.Order.cancelled_at.is_(None),
               models.Order.fulfilled_at.is_(None),      # protecție livrare: nu atingem ce-a plecat
               ~has_awb.exists())
    )).scalars().all()
    if not orders:
        return {}
    manual = await _manual_blocked(db, store)
    phone_bidxs = {o.shipping_phone_bidx for o in orders if o.shipping_phone_bidx}
    serial = await _serial_refusers(db, store, phone_bidxs, threshold, include_failed)
    stats = {"manual": 0, "serial_refuser": 0}
    for o in orders:
        ph, em = o.shipping_phone_bidx, o.shipping_email_bidx
        why = None
        if (ph and ("phone", ph) in manual) or (em and ("email", em) in manual):
            why, key = "blocklist manual", "manual"
        elif ph and ph in serial:
            why, key = "serial-refuser (>=%d refuzuri)" % threshold, "serial_refuser"
        if not why:
            continue
        stats[key] += 1
        logger.info("BLOCK store=%s order=%s -> would-block (%s)", store.id, o.name, why)
        exists = (await db.execute(
            select(models.CSQueueItem.id).where(models.CSQueueItem.order_id == o.id))).first()
        if not exists:
            db.add(models.CSQueueItem(store_id=store.id, order_id=o.id, reason="rule",
                                      status="open", reason_detail="Blocklist: " + why, created_by="auto"))
    await db.commit()
    return stats
