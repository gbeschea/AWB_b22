"""Închide tichetele AUTOMATE care nu mai au obiect.

Un tichet din coada CS înseamnă „opriți comanda asta, are nevoie de un om". După ce comanda a PLECAT
(sau a fost anulată), nu mai e nimic de oprit — tichetul devine zgomot care ascunde munca reală.

Se aduna tăcut, pentru că orice webhook de update de la Shopify re-evaluează detectoarele: o comandă
veche primea un update oarecare (marcată expediată, tag schimbat) și căpăta tichet deși coletul
plecase de săptămâni. Măsurat la prima rulare: 38 de tichete deschise pe comenzi din care 37 aveau
deja AWB, unele din iulie.

Garda din `order_shadow` oprește apariția lor. Asta le închide pe cele existente, și rămâne pornită
ca plasă: orice cale nouă care mai scapă unul e curățată în cel mult o tură.

Atingem DOAR tichetele deschise de automat (`created_by='auto'`). Dacă un OM a deschis tichetul, are
un motiv pe care noi nu-l vedem — tot un om îl închide.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from sqlalchemy import select

import models
from database import AsyncSessionLocal

logger = logging.getLogger(__name__)

_CAP = 500


async def close_moot_items() -> Dict[str, Any]:
    out: Dict[str, Any] = {"closed": 0, "by_reason": {}}
    async with AsyncSessionLocal() as db:
        has_awb = select(models.Shipment.id).where(
            models.Shipment.order_id == models.Order.id, models.Shipment.awb.isnot(None))
        rows = (await db.execute(
            select(models.CSQueueItem.id, models.CSQueueItem.reason)
            .join(models.Order, models.Order.id == models.CSQueueItem.order_id)
            .where(
                models.CSQueueItem.status != "solved",
                models.CSQueueItem.created_by == "auto",
                models.Order.fulfilled_at.isnot(None)
                | models.Order.cancelled_at.isnot(None)
                | has_awb.exists(),
            ).limit(_CAP)
        )).all()
        if not rows:
            return out
        for item_id, reason in rows:
            it = await db.get(models.CSQueueItem, item_id)
            if it is None or it.status == "solved":
                continue
            it.status = "solved"
            notes = list(it.notes or [])
            notes.append({"by": "auto",
                          "text": "Comanda a plecat (sau a fost anulată) — tichetul nu mai are obiect."})
            it.notes = notes
            out["closed"] += 1
            out["by_reason"][reason] = out["by_reason"].get(reason, 0) + 1
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("cs-janitor: commit eșuat")
            return {"closed": 0, "by_reason": {}}
    logger.info("cs-janitor: %d tichete închise (comenzi deja plecate) %s", out["closed"], out["by_reason"])
    return out
