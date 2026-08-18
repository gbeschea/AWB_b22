"""
parcels.py — paritate cu `order_parcel_count` din cron: numărul de COLETE al unui AWB.
Cronul: metafield order `xconnector.parcel-count` (total gata calculat) → altfel ceil(Σ cutii-per-
produs × qty) din metafield-urile produsului (`custom.nr_cutii`/`nr_produse`) cu fallback pe map-ul
CENTRAL SKU→nr_cutii (`sku_box_map.json`, construit zilnic din metafield-urile de pe orice magazin
deals) → altfel 1. Liniile FĂRĂ SKU (livrare express / surpriză virtuală) NU sunt colete fizice.

În OH: tabelul global `sku_box_map` (sincronizat din map-ul central de pe VPS cu
scripts/sku_box_map_sync.sh) + PackingRule-urile per-magazin existente. Shadow: loghează coletele
calculate pe comenzile neexpediate ≥2 colete (adică unde default-ul 1 al OH ar fi GREȘIT la AWB).
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

import models

logger = logging.getLogger("cron_parity.parcels")


def _ceil_pos(x: float) -> int:
    i = int(x)
    return i + 1 if x > i else i


async def _box_map(db) -> Dict[str, float]:
    rows = (await db.execute(text("select sku, boxes from sku_box_map"))).fetchall()
    return {r[0]: float(r[1]) for r in rows if r[1] is not None}


def parcel_count(order: Any, box_map: Dict[str, float]) -> int:
    """ceil(Σ cutii×qty) pe liniile cu SKU; cutii ≤0 (zgomot de metafield) = nedefinit; fallback 1."""
    total, found = 0.0, False
    for li in (order.line_items or []):
        sku = (li.sku or "").strip()
        if not sku:
            continue                     # linie virtuală — nu e colet fizic
        box: Optional[float] = box_map.get(sku)
        if box is not None and box <= 0:
            box = None
        if box is not None:
            total += box * int(li.quantity or 1)
            found = True
    return max(1, _ceil_pos(total)) if (found and total > 0) else 1


async def run_shadow(db, store: models.Store, hours: int = 48) -> Dict[str, int]:
    """Calculează coletele pe comenzile PENDING (fără AWB) și le MEMOREAZĂ în `order.parcel_count` — câmp
    OH-intern pe care auto_awb_service îl citește la creare AWB („preia + își memorează", cererea owner-ului).
    Scriere SIGURĂ: doar OH-intern (nu atinge Shopify, nu creează AWB), doar când calculăm ≥2 colete (unde
    default-ul 1 ar fi GREȘIT) ȘI câmpul e încă gol — un metafield `parcel-count` deja sincronizat NU se suprascrie."""
    box_map = await _box_map(db)
    if not box_map:
        return {"skipped": 1}            # map-ul nu e sincronizat încă — nimic de comparat
    floor = datetime.now(timezone.utc) - timedelta(hours=hours)
    has_awb = select(models.Shipment.id).where(
        models.Shipment.order_id == models.Order.id, models.Shipment.awb.isnot(None))
    rows = (await db.execute(
        select(models.Order)
        .options(selectinload(models.Order.line_items))
        .where(models.Order.store_id == store.id,
               models.Order.created_at >= floor,
               models.Order.cancelled_at.is_(None),
               models.Order.fulfilled_at.is_(None),
               ~has_awb.exists())          # doar comenzi care încă vor primi AWB
    )).scalars().all()
    stats = {"orders": 0, "multi_parcel": 0, "memoized": 0}
    for o in rows:
        stats["orders"] += 1
        n = parcel_count(o, box_map)
        if n >= 2:
            stats["multi_parcel"] += 1
            # memorează DOAR dacă nu avem deja o valoare (din metafield sau memoizare anterioară)
            if getattr(o, "parcel_count", None) in (None, 0):
                o.parcel_count = n
                stats["memoized"] += 1
                logger.info("PARCELS store=%s order=%s -> memorez %d colete (default-ul 1 ar fi greșit la AWB)",
                            store.id, o.name, n)
            else:
                logger.info("PARCELS store=%s order=%s -> %d colete (deja setat=%s, nu suprascriu)",
                            store.id, o.name, n, o.parcel_count)
    if stats["memoized"]:
        await db.commit()
    return stats
