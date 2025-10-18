# services/couriers/econt.py
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from services.couriers.base import BaseCourier, TrackingResponse, LabelResponse
from crud.couriers import get_courier_account_by_key
from httpx import Response

logger = logging.getLogger("services.couriers.econt")


class EcontCourier(BaseCourier):
    """
    Tracking Econt via /services endpoints (ex: https://ee.econt.com/services/track?shipmentNumber=AWB)
    - Nu cade sync-ul dacă răspunsul nu e standard; întoarce status=None (N/A).
    - Nu cere autentificare pentru tracking public. Dacă ai alt base_url, pune-l pe cont.
    """

    name: str = "econt"
    display_name: str = "Econt"

    # -------- AWB creation/label (neimplementat la Econt aici) ----------
    async def create_awb(self, db, order, account_key: Optional[str] = None) -> LabelResponse:
        return LabelResponse(success=False, message="Create AWB pentru Econt nu este implementat.")

    async def get_label(self, db, awb: str, account_key: Optional[str] = None) -> LabelResponse:
        return LabelResponse(success=False, message="Descarcare label pentru Econt nu este implementată.")

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
