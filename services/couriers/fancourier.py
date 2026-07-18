# services/couriers/fancourier.py
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from .base import BaseCourier, TrackingResponse, VoidResponse

log = logging.getLogger("couriers.fancourier")


class FanCourier(BaseCourier):
    """FAN Courier selfAWB API v2 (api.fancourier.ro, JSON).

    Auth: POST /login?username=&password= -> {data:{token}} (Bearer, 24h).
    Create: POST /intern-awb {clientId, shipments:[{info, recipient}]} -> {response:[{awbNumber}]}.
    Label: GET /awb/label?clientId=&awbs[]=&pdf=1&format=A6 -> PDF.
    Delete: DELETE /awb?clientId=&awb=.
    Track: GET /reports/awb/tracking?clientId=&awb[]=.
    Credentials: {clientId, username, password} (+ optional default_service, default_payment).
    """

    name: str = "fancourier"
    display_name: str = "FAN Courier"
    BASE_URL = "https://api.fancourier.ro"

    def __init__(self, client: httpx.AsyncClient):
        super().__init__(client)
        self._token_cache: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _client_id(creds: Dict[str, Any]) -> Optional[Any]:
        return creds.get("clientId") or creds.get("client_id") or creds.get("clientID")

    async def _get_token(self, creds: Dict[str, Any]) -> Optional[str]:
        user = creds.get("username")
        pwd = creds.get("password")
        if not user or not pwd:
            log.error("FAN Courier: lipsesc username/password.")
            return None
        cached = self._token_cache.get(user)
        if cached and cached["expires_at"] > datetime.now(timezone.utc) + timedelta(minutes=2):
            return cached["token"]
        try:
            r = await self.client.post(f"{self.BASE_URL}/login",
                                       params={"username": user, "password": pwd}, timeout=20.0)
        except Exception as e:
            log.error("FAN Courier login exception: %s", e)
            return None
        if r.status_code != 200:
            log.error("FAN Courier login HTTP %s: %s", r.status_code, r.text[:200])
            return None
        token = ((r.json() or {}).get("data") or {}).get("token")
        if token:
            self._token_cache[user] = {"token": token,
                                       "expires_at": datetime.now(timezone.utc) + timedelta(hours=20)}
        return token

    async def create_awb(self, db: AsyncSession, order, account_key: str,
                         *, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        opts = options or {}
        creds = await self.get_credentials(db, account_key)
        token = await self._get_token(creds)
        if not token:
            raise RuntimeError("FAN Courier: autentificare eșuată.")
        client_id = self._client_id(creds)
        if not client_id:
            raise RuntimeError("FAN Courier: lipsește clientId în credențiale.")

        def g(*names: str) -> str:
            for n in names:
                v = getattr(order, n, None)
                if v:
                    return str(v).strip()
            return ""

        name = g("shipping_name", "customer")
        phone = g("shipping_phone")
        county = g("shipping_province")
        locality = g("shipping_city")
        street = g("shipping_address1")
        addr2 = g("shipping_address2")
        if addr2:
            street = f"{street}, {addr2}".strip(", ")
        zipc = g("shipping_zip")
        email = g("shipping_email")
        if not (name and phone and county and locality and street):
            raise RuntimeError("FAN Courier: lipsesc date destinatar (nume/telefon/județ/localitate/adresă).")

        parcels = max(int(opts.get("parcels_count") or 1), 1)
        weight = float(opts.get("total_weight") or 1.0)
        cod = float(opts.get("cod_amount") or 0.0)
        service = opts.get("service") or creds.get("default_service") or "Standard"
        payment = (opts.get("payment") or creds.get("default_payment") or "sender")
        content = (opts.get("content_desc") or (getattr(order, "name", None) or "Colet"))[:255]

        info: Dict[str, Any] = {
            "service": service,
            "packages": {"parcel": parcels, "envelope": 0},
            "weight": round(weight, 2),
            "cod": round(cod, 2),
            "declaredValue": 0,
            "payment": payment,
            "content": content,
            "dimensions": {"length": 10, "height": 10, "width": 10},
        }
        recipient: Dict[str, Any] = {
            "name": name[:50],
            "contactPerson": name[:50],
            "phone": phone[:16],
            "address": {"county": county[:50], "locality": locality[:50], "street": street[:255]},
        }
        if email:
            recipient["email"] = email[:100]
        if zipc:
            recipient["address"]["zipCode"] = zipc[:6]

        body = {"clientId": int(client_id), "shipments": [{"info": info, "recipient": recipient}]}
        r = await self.client.post(
            f"{self.BASE_URL}/intern-awb", json=body,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, timeout=45.0,
        )
        data = r.json() if r.content else {}
        resp = data.get("response") or data.get("data") or []
        if r.status_code >= 400 or not resp:
            raise RuntimeError(f"FAN Courier create AWB HTTP {r.status_code}: {r.text[:800]}")
        first = resp[0] if isinstance(resp, list) else resp
        if isinstance(first, dict) and first.get("errors"):
            raise RuntimeError(f"FAN Courier: {first['errors']}")
        awb = (first or {}).get("awbNumber") or (first or {}).get("awb")
        if not awb:
            raise RuntimeError(f"FAN Courier: răspuns fără awbNumber: {data}")
        return {"awb": str(awb), "raw": first, "label_available": True}

    @staticmethod
    def _next_business_day() -> str:
        from datetime import datetime, timedelta
        d = datetime.now()
        if d.hour >= 16:  # after the usual cut-off -> next day
            d += timedelta(days=1)
        while d.weekday() >= 5:  # skip Sat/Sun
            d += timedelta(days=1)
        return d.strftime("%Y-%m-%d")

    async def request_pickup(self, db: AsyncSession, awbs, account_key: Optional[str] = None,
                             *, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Place a FAN courier order (pickup). FAN requires this separately — an AWB alone is
        not collected. One order per branch covers all its AWBs (docs), so `awbs` may be a
        list; we send the count/weight and, for a single AWB, print it on the order."""
        opts = options or {}
        creds = await self.get_credentials(db, account_key)
        token = await self._get_token(creds)
        if not token:
            return {"supported": True, "requested": False, "message": "FAN Courier: autentificare eșuată."}
        client_id = self._client_id(creds)
        awb_list = [awbs] if isinstance(awbs, str) else [a for a in (awbs or []) if a]
        parcels = int(opts.get("parcels_count") or max(len(awb_list), 1))
        weight = float(opts.get("total_weight") or max(len(awb_list), 1))
        info: Dict[str, Any] = {
            "packages": {"parcel": parcels, "envelope": 0},
            "weight": round(weight, 2),
            "dimensions": {"width": 10, "length": 10, "height": 10},
            "orderType": opts.get("order_type") or "Standard",
            "pickupDate": opts.get("pickup_date") or self._next_business_day(),
            "pickupHours": {"first": opts.get("pickup_from") or "09:00",
                            "second": opts.get("pickup_to") or "17:00"},
        }
        if len(awb_list) == 1:
            info["awbNumber"] = str(awb_list[0])
        body = {"info": info, "clientId": int(client_id)}
        try:
            r = await self.client.post(f"{self.BASE_URL}/order", json=body,
                                       headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                                       timeout=30.0)
            data = r.json() if r.content else {}
        except Exception as e:
            return {"supported": True, "requested": False, "message": f"FAN Courier pickup eroare: {e}"}
        if r.status_code >= 400 or data.get("status") != "success":
            return {"supported": True, "requested": False,
                    "message": f"HTTP {r.status_code}: {r.text[:300]}"}
        return {"supported": True, "requested": True, "order": data.get("data")}

    async def get_label(self, awb: str, creds: dict, paper_size: str) -> bytes:
        token = await self._get_token(creds)
        if not token:
            raise RuntimeError("FAN Courier: autentificare eșuată (etichetă).")
        size = (paper_size or "A6").upper()
        if size not in ("A4", "A5", "A6"):
            size = "A6"
        r = await self.client.get(
            f"{self.BASE_URL}/awb/label",
            params={"clientId": self._client_id(creds), "awbs[]": str(awb),
                    "pdf": "1", "format": size, "language": "ro"},
            headers={"Authorization": f"Bearer {token}"}, timeout=30.0,
        )
        if r.status_code != 200:
            raise RuntimeError(f"FAN Courier label HTTP {r.status_code}: {r.text[:200]}")
        if "application/pdf" not in (r.headers.get("content-type") or "") and r.content[:4] != b"%PDF":
            raise RuntimeError("FAN Courier: răspunsul nu este PDF.")
        return r.content

    async def void_awb(self, db: AsyncSession, awb: str, account_key: Optional[str] = None) -> VoidResponse:
        try:
            creds = await self.get_credentials(db, account_key)
        except ValueError:
            return VoidResponse(success=False, message="FAN Courier: lipsesc credențialele pentru anulare.")
        token = await self._get_token(creds)
        if not token:
            return VoidResponse(success=False, message="FAN Courier: autentificare eșuată la anulare.")
        try:
            r = await self.client.delete(
                f"{self.BASE_URL}/awb",
                params={"clientId": self._client_id(creds), "awb": str(awb)},
                headers={"Authorization": f"Bearer {token}"}, timeout=30.0,
            )
            data = r.json() if r.content else {}
            if r.status_code < 400 and (data.get("status") == "success" or "success" in str(data).lower()):
                return VoidResponse(success=True, raw=data)
            return VoidResponse(success=False, message=f"HTTP {r.status_code}: {r.text[:200]}", raw=data)
        except Exception as e:
            return VoidResponse(success=False, message=f"Eroare rețea FAN Courier la anulare: {e}")

    async def track_awb(self, db: AsyncSession, awb: str, account_key: Optional[str] = None) -> TrackingResponse:
        try:
            creds = await self.get_credentials(db, account_key)
        except ValueError:
            return TrackingResponse(status="no-credentials", date=None)
        token = await self._get_token(creds)
        if not token:
            return TrackingResponse(status="auth-error", date=None)
        try:
            r = await self.client.get(
                f"{self.BASE_URL}/reports/awb/tracking",
                params={"clientId": self._client_id(creds), "awb[]": str(awb)},
                headers={"Authorization": f"Bearer {token}"}, timeout=20.0,
            )
        except Exception:
            return TrackingResponse(status="tracking-error", date=None)
        if r.status_code != 200:
            return TrackingResponse(status=f"HTTP {r.status_code}", date=None)
        data = r.json() if r.content else {}
        rows = data.get("data") or []
        events = (rows[0].get("events") if rows and isinstance(rows[0], dict) else None) or []
        if not events:
            return TrackingResponse(status="AWB Generat", date=None, raw_data=data)
        last = events[-1]
        return TrackingResponse(status=last.get("name") or last.get("status") or "Unknown",
                                date=None, raw_data=data)
