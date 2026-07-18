# services/couriers/econt.py
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from services.couriers.base import BaseCourier, TrackingResponse, LabelResponse, VoidResponse
from crud.couriers import get_courier_account_by_key
from httpx import Response

logger = logging.getLogger("services.couriers.econt")

_COUNTRY_CODE3 = {"RO": "ROU", "BG": "BGR", "GR": "GRC"}


class EcontCourier(BaseCourier):
    """
    Econt (RO/BG) — create/label/void via the JSON services API + tracking.
    Create: POST {base}/services/Shipments/LabelService.createLabel.json (basic auth).
    Credentials: {api:{username,password}, sender_address:{...}, base_url?}.
    """

    name: str = "econt"
    display_name: str = "Econt"
    DEFAULT_BASE = "https://ee.econt.com"

    @staticmethod
    def _api(creds: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        api = creds.get("api") or {}
        return api.get("username") or api.get("user"), api.get("password") or api.get("pass")

    async def create_awb(self, db, order, account_key: Optional[str] = None,
                         *, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        opts = options or {}
        creds = await self.get_credentials(db, account_key)
        user, pwd = self._api(creds)
        if not user or not pwd:
            raise RuntimeError("Econt: lipsesc api username/password.")
        sender = creds.get("sender_address") or {}
        base = (creds.get("base_url") or self.DEFAULT_BASE).rstrip("/")
        url = f"{base}/services/Shipments/LabelService.createLabel.json"

        def g(*names: str) -> str:
            for n in names:
                v = getattr(order, n, None)
                if v:
                    return str(v).strip()
            return ""

        rname = g("shipping_name", "customer")
        rphone = g("shipping_phone")
        rcity = g("shipping_city")
        rstreet = g("shipping_address1")
        addr2 = g("shipping_address2")
        if addr2:
            rstreet = f"{rstreet}, {addr2}".strip(", ")
        rzip = g("shipping_zip")
        rcountry = (g("shipping_country") or "RO").upper()[:2]
        code3 = _COUNTRY_CODE3.get(rcountry, "ROU")
        if not (rname and rphone and rcity and rstreet):
            raise RuntimeError("Econt: lipsesc date destinatar (nume/telefon/oraș/adresă).")

        parcels = max(int(opts.get("parcels_count") or 1), 1)
        weight = float(opts.get("total_weight") or 1.0)
        cod = float(opts.get("cod_amount") or 0.0)
        currency = opts.get("currency") or ("RON" if code3 == "ROU" else "BGN")

        label: Dict[str, Any] = {
            "senderClient": {
                "name": sender.get("contact_person") or sender.get("name") or "Sender",
                "phones": [sender.get("phone") or ""],
                "email": sender.get("email") or "",
            },
            "senderAddress": {
                "city": {
                    "name": sender.get("city"),
                    "postCode": sender.get("postal_code") or sender.get("zip"),
                    "country": {"code3": "ROU"},
                },
                "street": sender.get("street"),
                "num": sender.get("street_no") or sender.get("num") or "1",
            },
            "receiverClient": {"name": rname, "phones": [rphone]},
            "receiverAddress": {
                "city": {"name": rcity, "postCode": rzip, "country": {"code3": code3}},
                "street": rstreet,
                "num": opts.get("street_no") or "1",
            },
            "packCount": parcels,
            "shipmentType": "PACK",
            "weight": round(weight, 3),
            "shipmentDescription": (getattr(order, "name", None) or "Colet")[:255],
            "orderNumber": getattr(order, "name", None) or "",
        }
        if cod > 0:
            label["services"] = {"cdAmount": round(cod, 2), "cdType": "get", "cdCurrency": currency}

        body = {"label": label, "mode": "create"}
        r = await self.http.post(url, json=body, auth=(user, pwd), timeout=45.0)
        data = r.json() if r.content else {}
        if r.status_code >= 400 or (isinstance(data, dict) and data.get("type") and "error" in str(data.get("type")).lower()):
            raise RuntimeError(f"Econt create HTTP {r.status_code}: {r.text[:700]}")
        lbl = (data.get("label") or {}) if isinstance(data, dict) else {}
        awb = lbl.get("shipmentNumber")
        if not awb:
            raise RuntimeError(f"Econt: răspuns fără shipmentNumber: {str(data)[:400]}")
        return {"awb": str(awb), "raw": lbl, "label_available": bool(lbl.get("pdfURL")),
                "pdf_url": lbl.get("pdfURL")}

    async def get_label(self, awb: str, creds: dict, paper_size: str = "A6") -> bytes:
        """Fetch the Econt label PDF. Econt returns the URL at creation (stored in the
        shipment); if a pdf_url is passed via creds, fetch that. Otherwise not retrievable
        by AWB alone."""
        pdf_url = (creds or {}).get("pdf_url")
        if not pdf_url:
            raise NotImplementedError("Econt: eticheta se obține din pdfURL-ul de la creare.")
        user, pwd = self._api(creds)
        r = await self.http.get(pdf_url, auth=(user, pwd) if user else None, timeout=30.0)
        if r.status_code != 200 or r.content[:4] != b"%PDF":
            raise RuntimeError(f"Econt label HTTP {r.status_code}")
        return r.content

    async def void_awb(self, db, awb: str, account_key: Optional[str] = None) -> VoidResponse:
        try:
            creds = await self.get_credentials(db, account_key)
        except ValueError:
            return VoidResponse(success=False, message="Econt: lipsesc credențialele pentru anulare.")
        user, pwd = self._api(creds)
        base = (creds.get("base_url") or self.DEFAULT_BASE).rstrip("/")
        url = f"{base}/services/Shipments/LabelService.deleteLabels.json"
        try:
            r = await self.http.post(url, json={"shipmentNumbers": [str(awb)]}, auth=(user, pwd), timeout=30.0)
            data = r.json() if r.content else {}
            if r.status_code < 400:
                return VoidResponse(success=True, raw=data)
            return VoidResponse(success=False, message=f"HTTP {r.status_code}: {r.text[:200]}", raw=data)
        except Exception as e:
            return VoidResponse(success=False, message=f"Eroare rețea Econt la anulare: {e}")

    # ------------------------------- Tracking ----------------------------
    async def track_awb(self, db, awb: str, account_key: Optional[str] = None) -> TrackingResponse:
        try:
            # 1) baza URL din cont (dacă e gol -> ee.econt.com)
            account = await get_courier_account_by_key(db, account_key or "")
            base_url = (account.base_url or "https://ee.econt.com").rstrip("/")

            # 2) endpoints posibile sub /services (unele instanțe au structuri ușor diferite)
            urls = [
                f"{base_url}/services/track?shipmentNumber={awb}",
                f"{base_url}/services/shipments/track?shipmentNumber={awb}",
                f"{base_url}/services/shipments/{awb}/tracking",
            ]

            data: Optional[Dict[str, Any]] = None
            last_ok: Optional[str] = None

            for url in urls:
                try:
                    resp: Response = await self.http.get(url, headers={"Accept": "application/json"}, timeout=20.0)
                except Exception as e:
                    logger.debug("Econt request fail %s: %s", url, e)
                    continue

                if resp.status_code // 100 != 2:
                    continue
                # uneori serverul răspunde text/html cu JSON; încercăm .json() dar protejat
                try:
                    data = resp.json()
                    last_ok = url
                    break
                except Exception:
                    logger.debug("Econt non-JSON la %s", url)

            if not data:
                return TrackingResponse(success=True, status=None, status_raw=None, code=awb, extra={"reason": "no_match"})

            status_text, raw = self._extract_latest_status(data)
            mapped = self._map_status(status_text) if status_text else None

            return TrackingResponse(
                success=True,
                status=mapped,
                status_raw=status_text,
                code=awb,
                extra={"source_url": last_ok, "raw_event": raw},
            )

        except Exception as e:
            logger.exception("Econt track_awb error %s: %s", awb, e)
            return TrackingResponse(success=False, status=None, status_raw=None, code=awb, extra={"error": str(e)})

    # ------------------------------ helpers ------------------------------
    def _extract_latest_status(self, data: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """
        Extrage ultimul eveniment din forme frecvente:
        - {"events":[{"time": "...", "status"/"description"/"message": "..."}]}
        - {"history":[{"date": "...", "status": "..."}]}
        - {"shipment":{"state"/"status": "..."}}
        - răspuns învelit în {"result": {...}} / {"data": {...}}
        """
        # 1) events / trackingEvents
        events = self._as_list(data.get("events") or data.get("trackingEvents"))
        if events:
            ev = self._latest(events)
            text = ev.get("status") or ev.get("description") or ev.get("message")
            return (self._clean(text), ev)

        # 2) history
        history = self._as_list(data.get("history"))
        if history:
            ev = self._latest(history)
            text = ev.get("status") or ev.get("description") or ev.get("message")
            return (self._clean(text), ev)

        # 3) shipment/consignment
        shipment = data.get("shipment") or data.get("consignment")
        if isinstance(shipment, dict):
            state = shipment.get("state") or shipment.get("status")
            return (self._clean(state), shipment)

        # 4) wrappers
        for k in ("result", "data"):
            v = data.get(k)
            if isinstance(v, dict):
                return self._extract_latest_status(v)

        return (None, None)

    @staticmethod
    def _as_list(v: Any) -> List[Dict[str, Any]]:
        return [x for x in v or [] if isinstance(x, dict)] if isinstance(v, list) else []

    @staticmethod
    def _clean(v: Optional[str]) -> Optional[str]:
        return " ".join(v.split()) if isinstance(v, str) else None

    @staticmethod
    def _latest(items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Ia elementul cu cea mai 'recentă' cheie de timp cunoscută; fallback: ultimul."""
        keys = ("time", "date", "datetime", "eventTime", "event_time", "createdAt", "created_at")
        def score(it: Dict[str, Any]):
            for i, k in enumerate(keys):
                if isinstance(it.get(k), str):
                    return (i, it[k])
            return (99, "")
        try:
            return sorted(items, key=score)[-1]
        except Exception:
            return items[-1] if items else {}

    @staticmethod
    def _map_status(text: Optional[str]) -> Optional[str]:
        """Mapează în categoriile interne UI: delivered, returned, in_transit, pickup_ready, canceled, problem, created."""
        if not text:
            return None
        t = text.lower()
        if any(w in t for w in ("delivered", "livrat", "finalized")):
            return "delivered"
        if any(w in t for w in ("returned", "return", "back to sender")):
            return "returned"
        if any(w in t for w in ("pickup", "locker", "office", "waiting for pickup", "ready for pickup")):
            return "pickup_ready"
        if any(w in t for w in ("canceled", "cancelled", "anulat", "voided")):
            return "canceled"
        if any(w in t for w in ("address", "refused", "failed", "problem", "exception", "hold", "insufficient")):
            return "problem"
        if any(w in t for w in ("in transit", "tranzit", "out for delivery", "courier", "sorting", "processed")):
            return "in_transit"
        if any(w in t for w in ("created", "generated", "shipment data")):
            return "created"
        return None
