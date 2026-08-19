"""SALVAREA adreselor respinse — reluarea pe care o făcea cronul.

Validatorul respinge o adresă și, în OH, aici se oprea totul: comanda nu primea AWB (corect) și
pleca la CS (corect), dar NIMENI nu mai încerca s-o repare. Cronul o relua: rula periodic peste
comenzile fără AWB cu adresă WRONG/UNKNOWN și corecta agentic ce se putea, lăsând la om doar ce
chiar nu se putea. Diferența nu e cosmetică — o adresă cu județul scris greșit e reparabilă
automat, dar în OH producea un tichet CS.

Două încercări, în ordinea costului:

1. REVALIDARE. Ieftină, fără apeluri externe în cazul obișnuit, și prinde tot ce s-a schimbat de la
   prima trecere: nomenclator actualizat, politică modificată, și mai ales HERE ca a doua opinie
   (activ implicit, prag 0.9) pe verdictele „nu pot decide" — exact rolul pe care-l avea la cron.

2. CORECȚIE AGENTICĂ prin xConnector (`repair_address`), CONSERVATOR: un singur candidat canonic,
   zip/oraș/județ ≥0.95, stradă ≥0.90, ZIP reconfirmat, numărul casei păstrat. Corecția se oglindește
   în Shopify, deci se întoarce în OH prin webhook și comanda se revalidează singură.

Ce nu se repară rămâne pentru CS — dar abia după ce am încercat, nu în locul încercării.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.orm import selectinload

import models

logger = logging.getLogger(__name__)

_MIN_AGE_MIN = 12          # sub asta, validarea inițială poate fi încă în desfășurare
_MAX_AGE_DAYS = 21         # aceeași fereastră ca la cron
_PER_STORE_CAP = 15        # lot mărginit: repararea face apeluri externe
_VALID = ("valid", "validat")


async def rescue_store(db, store, store_id: int) -> Dict[str, Any]:
    """Încearcă să salveze adresele invalide ale unui magazin.

    Întoarce {tried, fixed, repaired, touched} — `touched` = ID-urile atinse în tura asta. Apelantul
    TREBUIE să le excludă de la predarea către CS: corecția agentică se scrie în Shopify și se întoarce
    în OH prin webhook, cu 2-3 secunde întârziere. Rutarea către CS, care rulează sincron imediat după,
    ar vedea încă `invalid` și ar deschide tichet + hold pentru o adresă TOCMAI reparată — comanda
    rămâne blocată în spatele propriei reparații. S-a întâmplat pe GEN17615: reparată la 22:03:20,
    revalidată la 22:03:22, cu tichet deschis în aceeași tură.
    """
    from services import address_service

    now = datetime.now(timezone.utc)
    rows = (await db.execute(
        select(models.Order.id).where(
            models.Order.store_id == store_id,
            models.Order.cancelled_at.is_(None),
            models.Order.fulfilled_at.is_(None),
            models.Order.address_status.isnot(None),
            ~models.Order.address_status.in_(_VALID),
            models.Order.created_at <= now - timedelta(minutes=_MIN_AGE_MIN),
            models.Order.created_at >= now - timedelta(days=_MAX_AGE_DAYS),
            ~select(models.Shipment.id).where(
                models.Shipment.order_id == models.Order.id,
                models.Shipment.awb.isnot(None)).exists(),
        ).order_by(models.Order.created_at.desc()).limit(_PER_STORE_CAP)
    )).scalars().all()
    out: Dict[str, Any] = {"tried": 0, "fixed": 0, "repaired": 0, "unstuck": 0, "touched": set()}

    # ÎNTÂI deblocăm ce s-a reparat deja: o comandă cu adresa devenită VALIDĂ dar cu tichetul de
    # adresă încă deschis rămâne blocată LA INFINIT — auto-AWB sare orice comandă cu tichet, deci
    # corecția nu folosește la nimic. Nu intră în bucla de mai jos, fiindcă aceea caută adrese
    # INVALIDE; e nevoie de o trecere separată.
    try:
        out["unstuck"] = await _unstick_repaired(db, store, store_id, out["touched"])
    except Exception:
        await db.rollback()
        logger.info("salvare-adresa: deblocarea a picat pe %s", getattr(store, "domain", "?"))

    if not rows:
        return out

    for oid in rows:
        o = (await db.execute(
            select(models.Order).options(selectinload(models.Order.line_items))
            .where(models.Order.id == oid)
        )).scalar_one_or_none()
        if o is None:
            continue
        out["tried"] += 1
        name = o.name

        # 1. revalidare (include HERE ca a doua opinie pe „nu pot decide")
        try:
            res = await address_service.validate_address_for_order(db, o)
            await db.commit()
            if (getattr(res, "status", None) or o.address_status or "").lower() in _VALID:
                out["fixed"] += 1
                out["touched"].add(oid)
                logger.info("salvare-adresa: %s a devenit VALIDĂ la revalidare", name)
                await _clear_stale_address_ticket(db, store, o)
                continue
        except Exception:
            await db.rollback()
            logger.info("salvare-adresa: revalidarea a picat pt %s", name)

        # 2. corecție agentică prin xConnector (se oglindește în Shopify → revine prin webhook)
        try:
            fixed = await _repair_via_xconnector(db, store, o)
            await db.commit()
            if fixed:
                out["repaired"] += 1
                out["touched"].add(oid)
                logger.info("salvare-adresa: %s corectată agentic", name)
        except Exception:
            await db.rollback()
            logger.info("salvare-adresa: corecția agentică a picat pt %s", name)
    if out["fixed"] or out["repaired"]:
        logger.info("salvare-adresa %s: %d încercate → %d la revalidare, %d corectate agentic",
                    getattr(store, "domain", "?"), out["tried"], out["fixed"], out["repaired"])
    return out


async def _repair_via_xconnector(db, store, order) -> bool:
    """Corectează adresa prin xConnector dacă acesta o vede WRONG/UNKNOWN. True dacă s-a schimbat ceva."""
    if not getattr(order, "shopify_order_id", None):
        return False
    from services.couriers import get_courier_service
    from services.couriers import address_repair

    acc = (await db.execute(
        select(models.CourierAccount).where(
            models.CourierAccount.store_id == store.id,
            models.CourierAccount.account_key.like("xconnector%"))
    )).scalars().first()
    if not acc or not (acc.credentials or {}).get("api_key"):
        return False
    svc = get_courier_service("xconnector")
    xo = await svc.xc_order_by_shopify_id(acc.credentials, order.shopify_order_id)
    if not xo.get("orderId"):
        return False
    # Doar dacă xConnector însuși e nemulțumit. Dacă el zice VALID, nu ne atingem de o adresă bună
    # doar pentru că validatorul nostru e mai sever — ar fi o modificare fără câștig.
    if str(xo.get("addressStatus") or "").upper() not in ("WRONG", "UNKNOWN"):
        return False
    rep = await address_repair.repair_address(svc, acc.credentials, xo, order,
                                              apply=True, store=store)
    return bool(rep.get("changed"))


async def _clear_stale_address_ticket(db, store, order) -> bool:
    """Închide tichetul de adresă și ridică hold-ul când adresa a devenit VALIDĂ.

    Fără asta, o adresă reparată rămâne blocată la infinit în spatele propriului tichet: auto-AWB sare
    orice comandă cu tichet deschis, deci corecția nu folosește la nimic. Atingem DOAR tichetele
    deschise de automat (`created_by='auto'`, motiv `wrong_address`) — dacă un OM a deschis tichetul,
    tot un om îl închide.
    """
    if (order.address_status or "").lower() not in _VALID:
        return False
    item = (await db.execute(
        select(models.CSQueueItem).where(
            models.CSQueueItem.order_id == order.id,
            models.CSQueueItem.status != "solved",
            models.CSQueueItem.reason == "wrong_address",
            models.CSQueueItem.created_by == "auto")
    )).scalar_one_or_none()
    if item is None:
        return False
    item.status = "solved"
    notes = list(item.notes or [])
    notes.append({"by": "auto", "text": "Adresa a fost corectată automat și e validă — tichet închis."})
    item.notes = notes
    order.is_on_hold_shopify = False
    try:
        from services import shopify_service
        await shopify_service.release_fulfillment_order_holds(store, order.shopify_order_id)
    except Exception as e:
        logger.info("salvare-adresa: n-am putut ridica hold-ul pt %s: %s", order.name, e)
    logger.info("salvare-adresa: %s reparată → tichet CS închis, hold ridicat", order.name)
    return True


async def _unstick_repaired(db, store, store_id: int, touched: set) -> int:
    """Comenzi cu adresa VALIDĂ dar cu tichet de adresă încă deschis → închide tichetul, ridică hold-ul."""
    ids = (await db.execute(
        select(models.Order.id).where(
            models.Order.store_id == store_id,
            models.Order.cancelled_at.is_(None),
            models.Order.fulfilled_at.is_(None),
            models.Order.address_status.in_(_VALID),
            select(models.CSQueueItem.id).where(
                models.CSQueueItem.order_id == models.Order.id,
                models.CSQueueItem.status != "solved",
                models.CSQueueItem.reason == "wrong_address",
                models.CSQueueItem.created_by == "auto").exists(),
        ).limit(_PER_STORE_CAP)
    )).scalars().all()
    n = 0
    for oid in ids:
        o = await db.get(models.Order, oid)
        if o is None:
            continue
        try:
            if await _clear_stale_address_ticket(db, store, o):
                await db.commit()
                touched.add(oid)
                n += 1
        except Exception:
            await db.rollback()
    return n
