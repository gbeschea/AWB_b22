"""Reconciliere „comenzi fantomă": ce crede OH deschis, dar Shopify a închis demult.

DE CE există: OH află de anulări din webhook (`orders/cancelled`). Un webhook pierdut — dezabonare după
o rafală de 500, o pauză de deploy, o comandă anulată înainte ca magazinul să fie instalat — nu se
recuperează niciodată singur, iar comanda rămâne „deschisă" în OH PE VECIE. Nu e cosmetic: auto-AWB
vede exact aceste comenzi ca eligibile și încearcă la nesfârșit să expedieze comenzi moarte. Prima
zi de canary pe Gento a fost pierdută exact aici — toate cele 18 comenzi „eligibile" erau ANULATE în
Shopify, iar xConnector le respingea cu mesajul lui generic (deci nici măcar cauza nu era vizibilă).
Măsurat pe tot OH la prima rulare: 680 anulări + 4 expedieri necunoscute, față de 16 comenzi chiar
deschise — adică 98% din „vechi și neexpediate" erau minciună.

Întreabă Shopify DOAR despre comenzile suspecte (deschise + neexpediate + fără AWB + mai vechi de
`_MIN_AGE_DAYS`) — un lot mărginit per trecere, ca să nu concureze niciodată cu munca reală.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy import select, text

import models
from database import AsyncSessionLocal

logger = logging.getLogger(__name__)

_MIN_AGE_DAYS = 4          # sub asta e trafic normal: comanda chiar așteaptă AWB
_PER_STORE_CAP = 150       # lot mărginit per magazin per trecere
_Q = ('{ order(id: "gid://shopify/Order/%s") { cancelledAt displayFulfillmentStatus '
      'fulfillments(first:5){ id createdAt } } }')


def _dt(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)   # știm CĂ s-a întâmplat; ora exactă contează mai puțin


async def run_once() -> Dict[str, Any]:
    from services import shopify_service
    out = {"checked": 0, "cancelled": 0, "fulfilled": 0, "open": 0, "errors": 0}
    async with AsyncSessionLocal() as db:
        stores = (await db.execute(select(models.Store).where(
            models.Store.is_active.is_(True),
            ~models.Store.domain.like("%orderhub-test.invalid"),
            ~models.Store.domain.like("test-%"),
        ))).scalars().all()
        for st in stores:
            rows = (await db.execute(text("""
                select o.id, o.shopify_order_id from orders o
                left join shipments sh on sh.order_id = o.id
                where o.store_id = :s and o.cancelled_at is null and o.fulfilled_at is null
                  and sh.id is null and o.shopify_order_id is not null
                  and o.created_at < now() - make_interval(days => :d)
                order by o.created_at asc limit :lim"""),
                {"s": st.id, "d": _MIN_AGE_DAYS, "lim": _PER_STORE_CAP})).all()
            if not rows:
                continue
            try:
                cl = await shopify_service.authed_client(st)
            except Exception as e:
                logger.info("ghost-reconcile: fără client pentru %s (%s)", st.domain, e)
                out["errors"] += len(rows)
                continue
            for oid, sid in rows:
                out["checked"] += 1
                try:
                    r = await cl.post("graphql.json", json={"query": _Q % sid})
                    od = ((r.json() or {}).get("data") or {}).get("order") or {}
                except Exception:
                    out["errors"] += 1
                    continue
                if not od:            # ștearsă din Shopify / fără acces — n-o atingem
                    out["errors"] += 1
                    continue
                o = await db.get(models.Order, oid)
                if o is None:
                    continue
                if od.get("cancelledAt"):
                    o.cancelled_at = _dt(od["cancelledAt"])
                    out["cancelled"] += 1
                elif (od.get("displayFulfillmentStatus") or "").upper() in ("FULFILLED", "PARTIALLY_FULFILLED"):
                    fl = od.get("fulfillments") or []
                    o.fulfilled_at = _dt(fl[0].get("createdAt")) if fl else datetime.now(timezone.utc)
                    out["fulfilled"] += 1
                else:
                    out["open"] += 1
            try:
                await db.commit()
            except Exception:
                await db.rollback()
                logger.exception("ghost-reconcile: commit eșuat pe %s", st.domain)
    return out
