"""Punte xConnector → OH pentru statusul PRINTED (cererea owner: „printed/not-printed să-l ia și din
xConnector"). Adevărul despre printare la DEPOZIT = flag-ul `downloaded` de pe documentul SHIPPING_LABEL
din xConnector (print-batch descarcă eticheta → xConnector o marchează downloaded → iese din coada de
print). NU e AWBprint `orders.is_printed` — ăla e aproape nefolosit (1.070/723k, verificat 18-aug).

Bucla (30 min): pentru fiecare magazin cu cont xConnector, paginează comenzile din fereastra recentă
(GET /api/orders?fromOrderDate&toOrderDate, size=200 — răspunsul include `documents`), ia etichetele cu
downloaded=true și marchează `shipments.printed_at` în OH (momentul primei observări — xConnector nu
expune downloaded_at). Best-effort + politicos cu API-ul (sleep între pagini, se oprește pe 429 persistent).
Fără cont xConnector (instalări externe) → tace complet."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

import models
from database import AsyncSessionLocal

logger = logging.getLogger("awbprint_bridge")

XBASE = "https://xconnector.app"
_INTERVAL_S = int(os.environ.get("PRINTED_BRIDGE_INTERVAL_S", "1800"))
_WINDOW_DAYS = int(os.environ.get("PRINTED_BRIDGE_WINDOW_DAYS", "10"))
_MAX_PAGES = 30                      # plafon per magazin per pas (30×200 = 6000 comenzi — peste orice fereastră de 10 zile)


def _label_doc(o: dict) -> dict | None:
    for d in (o.get("documents") or []):
        if isinstance(d, dict) and d.get("documentType") == "SHIPPING_LABEL":
            return d
    return None


def _doc_tracking(doc: dict) -> str | None:
    t = doc.get("trackingNumber") or doc.get("awbNumber")
    if t:
        return str(t)
    # fallback: param `t=` din URL-ul etichetei (paritate cu doc_tracking din cron)
    url = doc.get("url") or doc.get("shippingLabelUrl") or ""
    if "t=" in url:
        from urllib.parse import parse_qs, urlparse
        vals = parse_qs(urlparse(url).query).get("t")
        if vals:
            return vals[0]
    return None


async def _downloaded_awbs_for_shop(client: httpx.AsyncClient, api_key: str) -> set[str]:
    """AWB-urile cu eticheta DESCĂRCATĂ (= printată la depozit) din fereastra recentă a unui magazin."""
    dto = datetime.now(timezone.utc).date().isoformat()
    dfrom = (datetime.now(timezone.utc).date() - timedelta(days=_WINDOW_DAYS)).isoformat()
    headers = {"Authorization": "Bearer " + api_key}
    out: set[str] = set()
    for page in range(_MAX_PAGES):
        r = await client.get(XBASE + "/api/orders", headers=headers, params={
            "fromOrderDate": dfrom, "toOrderDate": dto, "page": str(page), "size": "200"})
        if r.status_code == 429:
            await asyncio.sleep(20)
            r = await client.get(XBASE + "/api/orders", headers=headers, params={
                "fromOrderDate": dfrom, "toOrderDate": dto, "page": str(page), "size": "200"})
        if r.status_code != 200:
            break                     # best-effort: pasul următor reia; nu insistăm pe un API supărat
        d = r.json()
        arr = d if isinstance(d, list) else (d.get("content") or d.get("orders") or [])
        if not arr:
            break
        for o in arr:
            doc = _label_doc(o)
            if doc and doc.get("downloaded") is True:
                trk = _doc_tracking(doc)
                if trk:
                    out.add(trk)
        if len(arr) < 200:
            break
        await asyncio.sleep(0.4)      # politețe — nu mâncăm rate-limit-ul cronului
    return out


async def sync_printed_once() -> int:
    """Un pas: marchează în OH etichetele printate la depozit (downloaded=true în xConnector)."""
    floor = datetime.now(timezone.utc) - timedelta(days=_WINDOW_DAYS)
    async with AsyncSessionLocal() as db:
        accounts = (await db.execute(
            select(models.CourierAccount.store_id, models.CourierAccount.credentials)
            .where(models.CourierAccount.courier_type == "xconnector",
                   models.CourierAccount.is_active.is_(True),
                   models.CourierAccount.store_id.isnot(None))
        )).all()
        rows = (await db.execute(
            select(models.Shipment.id, models.Shipment.awb, models.Order.store_id)
            .join(models.Order, models.Shipment.order_id == models.Order.id)
            .where(models.Shipment.awb.isnot(None),
                   models.Shipment.printed_at.is_(None),
                   models.Shipment.created_at >= floor)
        )).all()
    if not rows or not accounts:
        return 0
    keys = {sid: (creds or {}).get("api_key") for sid, creds in accounts if (creds or {}).get("api_key")}
    by_store: dict[int, list] = {}
    for sid, awb, store_id in rows:
        if store_id in keys:
            by_store.setdefault(store_id, []).append((sid, awb))
    total = 0
    now = datetime.now(timezone.utc)
    async with httpx.AsyncClient(timeout=30) as client:
        for store_id, pending in by_store.items():
            try:
                downloaded = await _downloaded_awbs_for_shop(client, keys[store_id])
            except Exception as e:
                logger.info("printed-bridge: shop store_id=%s a picat (%s); reiau la pasul următor", store_id, e)
                continue
            hit = [sid for sid, awb in pending if awb in downloaded]
            if not hit:
                continue
            async with AsyncSessionLocal() as db:
                for sid in hit:
                    sh = await db.get(models.Shipment, sid)
                    if sh and sh.printed_at is None:
                        sh.printed_at = now
                        total += 1
                await db.commit()
    if total:
        logger.info("printed-bridge: %d etichete marcate PRINTATE (downloaded în xConnector)", total)
    return total


async def run_forever() -> None:
    logger.info("printed-bridge (xConnector downloaded) pornit — interval=%ss, fereastră=%s zile",
                _INTERVAL_S, _WINDOW_DAYS)
    while True:
        try:
            await sync_printed_once()
        except Exception as e:                       # punte best-effort — nu rupem NICIODATĂ app-ul
            logger.warning("printed-bridge pass failed: %s", e)
        await asyncio.sleep(_INTERVAL_S)
