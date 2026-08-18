"""
surprise.py — paritate cu reconcilerul PROACTIV `xconnector surprise` (PR 10cdd69, memoria
surprise-perfume-flow): pe Esteban, Script-ul de checkout omoară 2+1 la comenzile cu `cutie-cadou`,
iar Flow-ul de surpriză pierde CURSA cu AWB-ul de seară → comanda rămâne partially_fulfilled și
cadoul nu intră în colet. Compensarea: adaugă DEVREME floor(parfumuri/3) × `parfum-surpriza` ($0).

REGULI CONFIRMATE DIN DATE (2.900+ comenzi, zero supra-cadouri):
 • DOAR comenzi cu `cutie-cadou` în linii (fără cutie = treaba Flow-ului, parf ≡ 2 mod 3);
 • qty surpriză = floor(parfumuri/3), parfumuri ≥ 3;
 • NU drafturi (regula owner), NU tag `farasurpriza`, NU dacă are deja surpriză, NU dacă a plecat.
În shadow: log-only (candidați + qty). Apply la cutover = order_edit-ul existent al OH.
Detecția parfumului: SKU numeric pe Esteban (memoria: EST/NUB numeric; cutia/surpriza au SKU text).
"""
from __future__ import annotations
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.orm import selectinload

import models

logger = logging.getLogger("cron_parity.surprise")

_GIFTBOX = "cutie-cadou"
_SURPRISE = "surpriz"          # prinde 'surpriza-*' (SKU) și „Parfum surpriză" (titlu, NUB SKU numeric)
_OPTOUT_TAG = "farasurpriza"

# Surpriza-parfum se aplică DOAR brandurilor de parfum (owner): Esteban, George Talent, Lab Noir, Nubra.
# Restul magazinelor n-au parfum → sar complet (altfel = zgomot în shadow + greșit la go-live).
_SURPRISE_SHOPS = {
    "6f9e22-9d.myshopify.com",   # esteban.ro
    "ix5bxc-hr.myshopify.com",   # georgetalent.ro
    "31k0py-bi.myshopify.com",   # labnoir.ro
    "bmuwvv-jy.myshopify.com",   # nubra.ro
}


def analyze(order: Any) -> int:
    """Câte surprize AR TREBUI să aibă comanda (0 = nu se aplică / are deja / opt-out)."""
    if order.cancelled_at or getattr(order, "fulfilled_at", None):
        return 0
    if _OPTOUT_TAG in (order.tags or "").lower():
        return 0
    for sh in (order.shipments or []):
        if getattr(sh, "awb", None):
            return 0               # AWB-ul există deja (câmpul e `awb`, NU `tracking_number`) — cursa e
                                   # pierdută, nu mai atingem (regula owner; altfel = supra-cadou)
    has_box = False
    perfumes = 0
    for li in (order.line_items or []):
        sku = (li.sku or "").strip().lower()
        title = (getattr(li, "title", "") or getattr(li, "name", "") or "").lower()
        if not sku:
            continue
        if _GIFTBOX in sku:
            has_box = True
            continue
        if _SURPRISE in sku or _SURPRISE in title:
            return 0               # are deja surpriză (idempotență — cine adaugă primul, celălalt sare)
        if re.fullmatch(r"\d+", sku):
            perfumes += int(li.quantity or 1)
    if not has_box or perfumes < 3:
        return 0
    return perfumes // 3


async def run_shadow(db, store: models.Store, hours: int = 24) -> Dict[str, int]:
    if store.domain not in _SURPRISE_SHOPS:
        return {"skipped": "not-a-surprise-shop"}
    floor = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = (await db.execute(
        select(models.Order)
        .options(selectinload(models.Order.line_items), selectinload(models.Order.shipments))
        .where(models.Order.store_id == store.id,
               models.Order.created_at >= floor,
               models.Order.cancelled_at.is_(None),
               models.Order.fulfilled_at.is_(None))
    )).scalars().all()
    stats = {"candidates": 0, "surprises": 0}
    for o in rows:
        n = analyze(o)
        if n > 0:
            stats["candidates"] += 1
            stats["surprises"] += n
            logger.info("SURPRISE store=%s order=%s -> would add %d x parfum-surpriza (cutie+%s parf)",
                        store.id, o.name, n, sum(int(li.quantity or 1) for li in o.line_items
                                                 if re.fullmatch(r"\d+", (li.sku or "").strip())))
    return stats
