# services/couriers/frisbo.py
"""
Frisbo ca CONNECTOR în Order Hub — pentru magazinele fulfillate 3PL de Frisbo (azi: duppo.md /
Moldova; API-ul e același pentru toate org-urile Frisbo). Semantica diferă de xConnector/curieri:
FRISBO generează AWB-ul singur în pipeline-ul lui de fulfillment — OH nu «creează» eticheta, ci:

  GET  /orders/search?reference=<nr comandă>           → comanda (uid, statusuri)
  GET  /orders/order/{uid}/shipments                    → AWB: documents[].external_id = tracking,
                                                          documents[].labels[].download_url = PDF (S3)
  POST /orders/order/{uid}/regenerate_shipment          → REFACE eticheta cu {order_uid, parcel_count}
                                                          (echivalentul awb-regen cu N colete)
  POST /orders/order/{uid}/mark_waiting_for_courier|pickup → împinge comanda în fluxul depozitului

NU există: anulare AWB, facturare, căutare după tracking (doar după referință), picking list
(picking-ul e al depozitului Frisbo — vizibil doar ca status ready_for_picking/in_picking).
⚠️ Label PDF = S3 presigned — NU trimite header-ul Authorization către S3 (403).

Credentials per organizație (un JWT acoperă toate magazinele org-ului): {"token": <JWT>, "org_name": ...}.
Rate limit Frisbo: 20 req/s — apelurile OH sunt punctuale, fără sync în masă (AWBprint face sync-ul).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

import models
from .base import BaseCourier, TrackingResponse, VoidResponse

logger = logging.getLogger(__name__)

FRISBO_BASE = "https://ingest.apis.store-view.frisbo.dev"


class FrisboCourier(BaseCourier):
    name = "frisbo"
    display_name = "Frisbo (3PL — AWB generat de depozitul Frisbo)"
    # Frisbo e 3PL: generează AWB-ul în pipeline-ul lui și fulfill-uiește comanda în Shopify singur.
    owns_shopify_fulfillment = True

    def _headers(self, creds: Dict[str, Any]) -> Dict[str, str]:
        return {"Authorization": "Bearer " + (creds.get("token") or ""), "Content-Type": "application/json"}

    async def _get(self, creds: Dict[str, Any], path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        r = await self.http.get(FRISBO_BASE + path, params=params, headers=self._headers(creds))
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text

    async def _post(self, creds: Dict[str, Any], path: str, body: Dict[str, Any]) -> Any:
        r = await self.http.post(FRISBO_BASE + path, json=body, headers=self._headers(creds))
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text

    # ── comenzi & shipments ──
    async def order_by_reference(self, creds: Dict[str, Any], reference: str) -> Dict[str, Any]:
        s, d = await self._get(creds, "/orders/search", {"reference": reference, "limit": 3})
        orders = (((d or {}).get("data") or {}).get("orders") or []) if s == 200 and isinstance(d, dict) else []
        return orders[0] if orders else {}

    async def shipments(self, creds: Dict[str, Any], order_uid: str) -> List[Dict[str, Any]]:
        s, d = await self._get(creds, "/orders/order/%s/shipments" % order_uid)
        if s == 200 and isinstance(d, dict):
            return ((d.get("data") or {}).get("shipments")) or []
        return []

    @staticmethod
    def awb_info(shipments: List[Dict[str, Any]]) -> Dict[str, Any]:
        """{tracking, label_url, courier} din shipments (primul document ne-retur)."""
        for sh in shipments:
            for doc in (sh.get("documents") or []):
                if doc.get("is_return"):
                    continue
                labels = doc.get("labels") or []
                return {"tracking": doc.get("external_id"),
                        "label_url": (labels[0].get("download_url") if labels else None),
                        "courier": sh.get("courier_id")}
        return {}

    # ── contractul BaseCourier ──
    async def create_awb(self, db: AsyncSession, order: models.Order, account_key: str, *,
                         options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Frisbo își generează AWB-ul singur (3PL). «create» = citește AWB-ul dacă există (idempotent);
        cu options.parcels și AWB EXISTENT → regenerate_shipment cu parcel_count (awb-regen)."""
        opts = options or {}
        creds = await self.get_credentials(db, account_key)
        if not creds or not creds.get("token"):
            raise RuntimeError("Frisbo: contul '%s' nu are token." % account_key)
        o = await self.order_by_reference(creds, order.name or "")
        if not o.get("uid"):
            return {"success": False, "message": "comanda nu există în Frisbo (sync-ul org-ului lipsă?)"}
        ships = await self.shipments(creds, o["uid"])
        info = self.awb_info(ships)
        if info.get("tracking"):
            if opts.get("parcels") and int(opts["parcels"]) > 1:
                s, d = await self._post(creds, "/orders/order/%s/regenerate_shipment" % o["uid"],
                                        {"order_uid": o["uid"], "parcel_count": int(opts["parcels"])})
                if s == 200:
                    ships = await self.shipments(creds, o["uid"])
                    info = self.awb_info(ships)
                    return {"success": True, "awb": info.get("tracking"), "tracking_number": info.get("tracking"),
                            "carrier": info.get("courier"), "label_url": info.get("label_url"),
                            "regenerated": True, "parcels": int(opts["parcels"])}
                return {"success": False, "message": "regenerate_shipment a eșuat (%s)" % s, "raw": d}
            return {"success": True, "awb": info["tracking"], "tracking_number": info["tracking"],
                    "carrier": info.get("courier"), "label_url": info.get("label_url"), "existing": True}
        agg = o.get("aggregated_status")
        st = agg.get("key") if isinstance(agg, dict) else agg
        return {"success": False,
                "message": "Frisbo n-a generat încă AWB-ul (status: %s) — îl generează depozitul lor în flux" % st}

    async def void_awb(self, db: AsyncSession, awb: str, account_key: Optional[str] = None) -> VoidResponse:
        return VoidResponse(success=False,
                            message="Frisbo nu expune anulare de AWB — folosește regenerate (înlocuire) sau contactează Frisbo.")

    async def get_label(self, awb: str, creds: dict, paper_size: str = "A6") -> bytes:
        """`awb` = REFERINȚA comenzii (Frisbo nu caută după tracking). PDF-ul e S3 presigned —
        descărcat FĂRĂ header-ul de auth Frisbo (S3 ar da 403)."""
        o = await self.order_by_reference(creds, awb)
        if not o.get("uid"):
            raise RuntimeError("Frisbo: comanda '%s' negăsită" % awb)
        info = self.awb_info(await self.shipments(creds, o["uid"]))
        if not info.get("label_url"):
            raise RuntimeError("Frisbo: comanda '%s' nu are (încă) etichetă PDF" % awb)
        r = await self.http.get(info["label_url"])
        r.raise_for_status()
        return r.content

    async def track_awb(self, db: AsyncSession, awb: str, account_key: Optional[str] = None) -> TrackingResponse:
        """Tracking pe REFERINȚA comenzii (nu pe AWB — API-ul Frisbo nu caută după tracking)."""
        creds = await self.get_credentials(db, account_key)
        if not creds or not creds.get("token"):
            return TrackingResponse(success=False, status_raw="cont fără token")
        o = await self.order_by_reference(creds, awb)
        if not o:
            return TrackingResponse(success=False, status_raw="comandă negăsită în Frisbo")
        agg = o.get("aggregated_status")
        st = agg.get("key") if isinstance(agg, dict) else agg
        return TrackingResponse(success=True, code=awb, status_raw=st, raw_data=o)

    async def mark_waiting_for_courier(self, db: AsyncSession, order: models.Order, account_key: str) -> Dict[str, Any]:
        creds = await self.get_credentials(db, account_key)
        o = await self.order_by_reference(creds or {}, order.name or "") if creds else {}
        if not o.get("uid"):
            return {"success": False, "message": "comanda nu există în Frisbo"}
        s, d = await self._post(creds, "/orders/order/%s/mark_waiting_for_courier" % o["uid"], {})
        return {"success": s == 200, "raw": d}
