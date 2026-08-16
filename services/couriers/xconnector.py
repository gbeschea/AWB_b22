# services/couriers/xconnector.py
"""
xConnector ca CONNECTOR de curierat + facturare în Order Hub — puntea prin care OH preia FULLY
cronul: OH decide (validare/dedup/colete/surpriză/COD), xConnector execută (AWB + facturi), exact
ca în producția de azi. Paritate prin construcție: aceleași endpoint-uri ca xconnector.py (cron):

  GET  /api/orders/by-id?orderId=<shopify_numeric_id>      → comanda xConnector (orderId, documents)
  GET  /api/orders/by-tracking-number?trackingNumber=<awb>  → comanda după AWB (pt void/label)
  GET  /api/merchant/connectors                             → curieri + facturare (SMART_BILL)
  POST /api/actions/create-shipping-label  {orderId, connectorId, parcelCount, parcelType, notifyCustomer}
  POST /api/actions/cancel-shipping-label  {orderId, connectorId?}
  POST /api/actions/create-invoice | cancel-invoice | revert-invoice  {orderId, connectorId, refundId?, languageCode?}

Auth = cheia API xConnector a MAGAZINULUI (Bearer), ținută în CourierAccount.credentials (criptat):
  {"api_key": "...", "shop_domain": "...", "connector_id": opțional (default: singurul activ / DPD),
   "billing_connector_id": opțional (default: singurul SMART_BILL activ)}
Un cont per magazin: account_key = "xconnector-<slug-domeniu>". Gărzile din cron sunt păstrate:
nu creez peste un AWB existent (awb-regen e anulare+refacere explicită), erorile xConnector se
întorc lizibil (errorMessage/errorDescription).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

import models
from .base import BaseCourier, TrackingResponse, VoidResponse

logger = logging.getLogger(__name__)

XBASE = "https://xconnector.app"


class XConnectorCourier(BaseCourier):
    name = "xconnector"
    display_name = "xConnector (punte AWB + facturi)"

    # ── HTTP primitives ──
    def _headers(self, creds: Dict[str, Any]) -> Dict[str, str]:
        return {"Authorization": "Bearer " + (creds.get("api_key") or ""), "Content-Type": "application/json"}

    async def _get(self, creds: Dict[str, Any], path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        r = await self.http.get(XBASE + path, params=params, headers=self._headers(creds))
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text

    async def _post(self, creds: Dict[str, Any], path: str, body: Dict[str, Any]) -> Any:
        r = await self.http.post(XBASE + path, json=body, headers=self._headers(creds))
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text

    @staticmethod
    def _err(d: Any) -> str:
        if isinstance(d, dict):
            return d.get("errorDescription") or d.get("errorMessage") or d.get("errorCode") or str(d)[:200]
        return str(d)[:200]

    # ── comenzi & connectori ──
    async def xc_order_by_shopify_id(self, creds: Dict[str, Any], shopify_order_id: str) -> Dict[str, Any]:
        s, d = await self._get(creds, "/api/orders/by-id", {"orderId": str(shopify_order_id)})
        return d if s == 200 and isinstance(d, dict) else {}

    async def xc_order_by_tracking(self, creds: Dict[str, Any], awb: str) -> Dict[str, Any]:
        s, d = await self._get(creds, "/api/orders/by-tracking-number", {"trackingNumber": awb})
        return d if s == 200 and isinstance(d, dict) else {}

    async def connectors(self, creds: Dict[str, Any]) -> List[Dict[str, Any]]:
        s, d = await self._get(creds, "/api/merchant/connectors")
        return d if s == 200 and isinstance(d, list) else []

    @staticmethod
    def _doc(o: Dict[str, Any], doc_type: str) -> Optional[Dict[str, Any]]:
        for d in (o.get("documents") or []):
            if isinstance(d, dict) and d.get("documentType") == doc_type:
                return d
        return None

    async def _pick_shipping_connector(self, creds: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Regula cronului: connectorul explicit din config, altfel singurul activ de curierat,
        altfel preferă DPD (default-ul producției — livrează și cross-border CZ/PL/BG/HU/SK)."""
        cid = creds.get("connector_id")
        cons = [c for c in await self.connectors(creds)
                if c.get("active") and (c.get("type") or "").upper() not in ("SMART_BILL",)]
        if cid:
            m = [c for c in cons if c.get("id") == cid]
            return m[0] if m else {"id": cid, "name": "config"}
        if len(cons) == 1:
            return cons[0]
        dpd = [c for c in cons if "dpd" in (c.get("name") or "").lower()]
        return dpd[0] if dpd else (cons[0] if cons else None)

    async def _pick_billing_connector(self, creds: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        cid = creds.get("billing_connector_id")
        bills = [c for c in await self.connectors(creds)
                 if c.get("active") and (c.get("type") or "").upper() == "SMART_BILL"]
        if cid:
            m = [c for c in bills if c.get("id") == cid]
            return m[0] if m else {"id": cid, "name": "config"}
        return bills[0] if len(bills) == 1 else None

    # ── contractul BaseCourier ──
    async def create_awb(self, db: AsyncSession, order: models.Order, account_key: str, *,
                         options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        opts = options or {}
        creds = await self.get_credentials(db, account_key)
        if not creds or not creds.get("api_key"):
            raise RuntimeError("xConnector: contul '%s' nu are api_key." % account_key)
        if not order.shopify_order_id:
            return {"success": False, "message": "comanda nu are shopify_order_id"}
        o = await self.xc_order_by_shopify_id(creds, order.shopify_order_id)
        if not o.get("orderId"):
            return {"success": False, "message": "comanda nu există (încă) în xConnector"}
        if self._doc(o, "SHIPPING_LABEL"):
            return {"success": False, "message": "are DEJA AWB în xConnector — folosește void + create (regen)"}
        con = await self._pick_shipping_connector(creds)
        if not con:
            return {"success": False, "message": "niciun connector de curierat activ pe cheia xConnector"}
        body = {"orderId": o["orderId"], "connectorId": con["id"],
                "parcelCount": int(opts.get("parcels") or opts.get("parcelCount") or 1),
                "parcelType": opts.get("parcel_type") or "BOX",
                "notifyCustomer": bool(opts.get("notify", False))}
        s, d = await self._post(creds, "/api/actions/create-shipping-label", body)
        ok = s == 200 and isinstance(d, dict) and d.get("accepted")
        labels = (d.get("shippingLabels") or []) if isinstance(d, dict) else []
        good = [L for L in labels if L.get("success")]
        if not (ok and good):
            msg = self._err(d) or (good and good[0].get("errorMessage")) or "respins"
            return {"success": False, "message": msg, "raw": d}
        L = good[0]
        return {"success": True, "awb": L.get("trackingNumber"), "tracking_number": L.get("trackingNumber"),
                "carrier": L.get("carrierName"), "label_url": L.get("shippingLabelUrl"),
                "price": L.get("price"), "connector_id": con["id"], "raw": d}

    async def void_awb(self, db: AsyncSession, awb: str, account_key: Optional[str] = None) -> VoidResponse:
        creds = await self.get_credentials(db, account_key)
        if not creds or not creds.get("api_key"):
            return VoidResponse(success=False, message="xConnector: cont fără api_key")
        o = await self.xc_order_by_tracking(creds, awb)
        if not o.get("orderId"):
            return VoidResponse(success=False, message="AWB negăsit în xConnector")
        body: Dict[str, Any] = {"orderId": o["orderId"]}
        doc = self._doc(o, "SHIPPING_LABEL")
        if doc and doc.get("connectorId"):
            body["connectorId"] = doc["connectorId"]
        s, d = await self._post(creds, "/api/actions/cancel-shipping-label", body)
        ok = s == 200 and isinstance(d, dict) and d.get("accepted")
        return VoidResponse(success=bool(ok), message=None if ok else self._err(d), raw=d)

    async def get_label(self, awb: str, creds: dict, paper_size: str = "A6") -> bytes:
        o = await self.xc_order_by_tracking(creds, awb)
        doc = self._doc(o, "SHIPPING_LABEL") or {}
        url = doc.get("shippingLabelUrl") or doc.get("fileUrl")
        if not url:
            raise RuntimeError("xConnector: eticheta AWB %s nu are URL de PDF" % awb)
        r = await self.http.get(url, headers=self._headers(creds))
        r.raise_for_status()
        return r.content

    async def track_awb(self, db: AsyncSession, awb: str, account_key: Optional[str] = None) -> TrackingResponse:
        """xConnector nu e sursă de tracking live — statusul vine de la curierul real (adapterul
        DPD/etc. al OH). Întoarcem ce știe xConnector despre comandă (best-effort)."""
        creds = await self.get_credentials(db, account_key)
        if not creds or not creds.get("api_key"):
            return TrackingResponse(success=False, status_raw="cont fără api_key")
        o = await self.xc_order_by_tracking(creds, awb)
        if not o:
            return TrackingResponse(success=False, status_raw="AWB negăsit în xConnector")
        return TrackingResponse(success=True, code=awb, status_raw=o.get("status") or o.get("fulfillmentStatus"),
                                raw_data=o)

    # ── FACTURI (dincolo de BaseCourier — puntea de facturare SMART_BILL prin xConnector) ──
    async def _invoice_action(self, db: AsyncSession, order: models.Order, account_key: str, endpoint: str,
                              refund_id: Optional[int] = None, lang: Optional[str] = None) -> Dict[str, Any]:
        creds = await self.get_credentials(db, account_key)
        if not creds or not creds.get("api_key"):
            return {"success": False, "message": "xConnector: cont fără api_key"}
        o = await self.xc_order_by_shopify_id(creds, order.shopify_order_id or "")
        if not o.get("orderId"):
            return {"success": False, "message": "comanda nu există în xConnector"}
        con = await self._pick_billing_connector(creds)
        if not con:
            return {"success": False, "message": "connector de facturare (SMART_BILL) ambiguu/absent"}
        body: Dict[str, Any] = {"orderId": o["orderId"], "connectorId": con["id"]}
        if refund_id is not None:
            body["refundId"] = int(refund_id)
        if lang:
            body["languageCode"] = lang
        s, d = await self._post(creds, endpoint, body)
        ok = s == 200 and isinstance(d, dict) and d.get("accepted")
        invs = (d.get("invoices") or []) if isinstance(d, dict) else []
        good = [i for i in invs if i.get("success")]
        if ok and (good or not invs):
            first = good[0] if good else {}
            return {"success": True, "serie": first.get("invoiceSerie"), "numar": first.get("invoiceNumber"),
                    "storno": bool(first.get("storno")), "raw": d}
        return {"success": False, "message": self._err(d), "raw": d}

    async def create_invoice(self, db: AsyncSession, order: models.Order, account_key: str,
                             lang: Optional[str] = None) -> Dict[str, Any]:
        creds = await self.get_credentials(db, account_key)
        o = await self.xc_order_by_shopify_id(creds or {}, order.shopify_order_id or "") if creds else {}
        if o and self._doc(o, "INVOICE"):
            return {"success": False, "message": "are DEJA factură — folosește cancel + create (regen)"}
        return await self._invoice_action(db, order, account_key, "/api/actions/create-invoice", lang=lang)

    async def cancel_invoice(self, db: AsyncSession, order: models.Order, account_key: str) -> Dict[str, Any]:
        return await self._invoice_action(db, order, account_key, "/api/actions/cancel-invoice")

    async def storno_invoice(self, db: AsyncSession, order: models.Order, account_key: str,
                             refund_id: Optional[int] = None) -> Dict[str, Any]:
        return await self._invoice_action(db, order, account_key, "/api/actions/revert-invoice", refund_id=refund_id)
