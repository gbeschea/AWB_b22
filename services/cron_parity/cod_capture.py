"""
cod_capture.py — paritate cu `xconnector.py cmd_capture`: comenzile COD (ramburs) rămân „pending"
în Shopify pe veci fiindcă plata se întâmplă la ușă, la curier — Shopify nu află singur.
Cronul (și de-acum OH): LIVRAT → orderMarkAsPaid · REFUZAT/întors → tag 'refuzata' · în curs → lasă.

Diferență de arhitectură (în avantajul OH): cronul citea statusul din AWBprint + verifica live DPD;
OH își ține SINGUR statusurile de curier (status_sync_service, canonicalizate delivered/refused) →
sursa e internă, fără lag de sincronizare. În shadow: log-only. La apply: orderMarkAsPaid există
deja în services/shopify_service (mutația e scrisă); tags idem.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.orm import selectinload

import models

logger = logging.getLogger("cron_parity.cod_capture")

_COD_MARKERS = ("cash on delivery", "cod", "ramburs", "numerar la livrare")


def is_cod(order: Any) -> bool:
    blob = " ".join([(order.payment_gateway_names or ""), (getattr(order, "mapped_payment", "") or "")]).lower()
    return any(m in blob for m in _COD_MARKERS)


def classify(order: Any) -> str:
    """paid | refuzata | leave — după statusul canonic al shipment-urilor PROPRII."""
    statuses = {(getattr(sh, "status", None) or getattr(sh, "derived_status", "") or "").lower()
                for sh in (order.shipments or [])}
    if "delivered" in statuses:
        return "paid"
    if "refused" in statuses:
        return "refuzata"
    return "leave"


async def run_shadow(db, store: models.Store, days: int = 30) -> Dict[str, int]:
    floor = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(
        select(models.Order)
        .options(selectinload(models.Order.shipments))
        .where(models.Order.store_id == store.id,
               models.Order.created_at >= floor,
               models.Order.cancelled_at.is_(None),
               models.Order.financial_status.in_(("pending", "PENDING", "authorized")))
    )).scalars().all()
    stats = {"pending_cod": 0, "would_mark_paid": 0, "would_tag_refuzata": 0, "leave": 0}
    for o in rows:
        if not is_cod(o):
            continue
        stats["pending_cod"] += 1
        act = classify(o)
        if act == "paid":
            stats["would_mark_paid"] += 1
            logger.info("CAPTURE store=%s order=%s LIVRAT+pending -> would mark PAID (total=%s)",
                        store.id, o.name, o.total_price)
        elif act == "refuzata":
            stats["would_tag_refuzata"] += 1
            logger.info("CAPTURE store=%s order=%s REFUZAT+pending -> would tag 'refuzata'", store.id, o.name)
        else:
            stats["leave"] += 1
    return stats
