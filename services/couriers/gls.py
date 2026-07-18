# services/couriers/gls.py
from __future__ import annotations

import base64
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from .base import BaseCourier, TrackingResponse, VoidResponse

log = logging.getLogger("couriers.gls")

# MyGLS JSON API base per country. RO by default.
_BASES = {
    "ro": "https://api.mygls.ro/ParcelService.svc/json",
    "hu": "https://api.mygls.hu/ParcelService.svc/json",
    "sk": "https://api.mygls.sk/ParcelService.svc/json",
    "cz": "https://api.mygls.cz/ParcelService.svc/json",
    "hr": "https://api.mygls.hr/ParcelService.svc/json",
    "si": "https://api.mygls.si/ParcelService.svc/json",
    "ro-test": "https://api.test.mygls.ro/ParcelService.svc/json",
}


class GLSCourier(BaseCourier):
    """GLS via the MyGLS JSON API (ParcelService.svc/json).

    PrintLabels validates + creates parcels AND returns the label PDF in one call.
    Auth on every request: Username + Password (SHA-512 of the plaintext, as a byte array)
    + ClientNumber. Credentials: {username, password, client_number, country? (default 'ro')}.
    Not verified live (no account yet) — built to the documented API.
    """

    name: str = "gls"
    display_name: str = "GLS"

    @staticmethod
    def _base(creds: Dict[str, Any]) -> str:
        country = str(creds.get("country") or "ro").lower()
        return _BASES.get(country, _BASES["ro"])

    @staticmethod
    def _password_bytes(pwd: str) -> List[int]:
        # MyGLS JSON expects the SHA-512 digest as an array of unsigned bytes (0-255).
        return list(hashlib.sha512((pwd or "").encode("utf-8")).digest())

    @staticmethod
    def _client_number(creds: Dict[str, Any]) -> Optional[int]:
        v = creds.get("client_number") or creds.get("clientNumber") or creds.get("client_id")
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _wcf_date(dt: datetime) -> str:
        return f"/Date({int(dt.timestamp() * 1000)})/"

    def _auth(self, creds: Dict[str, Any]) -> Dict[str, Any]:
        return {"Username": creds.get("username"), "Password": self._password_bytes(creds.get("password") or "")}

    def _addr(self, name, street, city, zipc, country, phone, email) -> Dict[str, Any]:
        return {
            "Name": (name or "")[:60], "Street": (street or "")[:60], "HouseNumber": "",
            "City": (city or "")[:60], "ZipCode": (zipc or "")[:20],
            "CountryIsoCode": (country or "RO").upper()[:2],
            "ContactName": (name or "")[:60], "ContactPhone": (phone or "")[:20],
            "ContactEmail": (email or "")[:60],
        }

    async def create_awb(self, db: AsyncSession, order, account_key: str,
                         *, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        opts = options or {}
        creds = await self.get_credentials(db, account_key)
        client_number = self._client_number(creds)
        if not creds.get("username") or not creds.get("password") or not client_number:
            raise RuntimeError("GLS: lipsesc username/password/client_number.")
        base = self._base(creds)
        sender = creds.get("sender_address") or {}

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
        remail = g("shipping_email")
        if not (rname and rphone and rcity and rstreet):
            raise RuntimeError("GLS: lipsesc date destinatar (nume/telefon/oraș/adresă).")

        count = max(int(opts.get("parcels_count") or 1), 1)
        cod = float(opts.get("cod_amount") or 0.0)

        parcel: Dict[str, Any] = {
            "ClientNumber": client_number,
            "ClientReference": (getattr(order, "name", None) or "")[:40],
            "Content": (opts.get("content_desc") or getattr(order, "name", None) or "Colet")[:60],
            "Count": count,
            "PickupDate": self._wcf_date(datetime.now()),
            "PickupAddress": self._addr(
                sender.get("contact_person") or sender.get("name") or "Sender",
                sender.get("street"), sender.get("city"), sender.get("postal_code"),
                "RO", sender.get("phone"), sender.get("email")),
            "DeliveryAddress": self._addr(rname, rstreet, rcity, rzip, rcountry, rphone, remail),
            "ServiceList": [],
        }
        if cod > 0:
            parcel["CODAmount"] = round(cod, 2)
            parcel["CODReference"] = (getattr(order, "name", None) or "")[:40]

        # PrintLabels registers the parcel AND returns the ParcelNumber + label PDF in one call.
        # (PrepareLabels returns only a ParcelId, no number.) The label size is chosen HERE via
        # TypeOfPrinter — GLS generates the label now, so we keep the returned PDF for printing.
        sz = str(opts.get("label_size") or "A6").upper()
        printer = "Thermo" if sz in ("A6", "A7", "THERMO") else "A4_2x2"
        body = {**self._auth(creds), "ParcelList": [parcel], "TypeOfPrinter": printer, "PrintPosition": 1}
        r = await self.client.post(f"{base}/PrintLabels", json=body, timeout=45.0)
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            raise RuntimeError(f"GLS PrintLabels HTTP {r.status_code}: {r.text[:400]}")
        errs = data.get("PrintLabelsErrorList") or []
        info = data.get("PrintLabelsInfoList") or []
        if errs and not info:
            raise RuntimeError(f"GLS: {errs}")
        first = info[0] if info else {}
        awb = first.get("ParcelNumber")
        pid = first.get("ParcelId")
        if not awb:
            raise RuntimeError(f"GLS: răspuns fără ParcelNumber: {str(data)[:300]}")
        labels = data.get("Labels")
        label_b64 = base64.b64encode(bytes(labels)).decode() if labels else None
        return {"awb": str(awb), "raw": {"ParcelId": pid, "ParcelNumber": awb, "label_b64": label_b64},
                "label_available": bool(label_b64), "parcel_id": pid}

    async def get_label(self, awb: str, creds: dict, paper_size: str = "A6") -> bytes:
        """Return the GLS label PDF. GLS generates it at create (PrintLabels), so we serve the
        stored PDF (creds['label_b64']); GetPrintedLabels can't re-fetch an already-printed one."""
        b64 = (creds or {}).get("label_b64")
        if b64:
            return base64.b64decode(b64)
        parcel_id = (creds or {}).get("parcel_id")
        if not parcel_id:
            raise NotImplementedError("GLS: eticheta e în răspunsul de la creare (label_b64).")
        base = self._base(creds)
        sz = (paper_size or "A6").upper()
        printer = "Thermo" if sz in ("A6", "A7", "THERMO") else "A4_2x2"
        body = {**self._auth(creds), "ParcelIdList": [int(parcel_id)], "TypeOfPrinter": printer, "PrintPosition": 1}
        r = await self.client.post(f"{base}/GetPrintedLabels", json=body, timeout=30.0)
        data = r.json() if r.content else {}
        labels = data.get("Labels")
        if not labels:
            raise RuntimeError(f"GLS label: {data.get('GetPrintedLabelsErrorList') or r.status_code}")
        return bytes(labels)

    async def void_awb(self, db: AsyncSession, awb: str, account_key: Optional[str] = None) -> VoidResponse:
        try:
            creds = await self.get_credentials(db, account_key)
        except ValueError:
            return VoidResponse(success=False, message="GLS: lipsesc credențialele pentru anulare.")
        parcel_id = (creds or {}).get("parcel_id")
        if not parcel_id:
            return VoidResponse(success=False, message="GLS: anularea necesită ParcelId.")
        base = self._base(creds)
        try:
            r = await self.client.post(f"{base}/DeleteLabels",
                                       json={**self._auth(creds), "ParcelIdList": [int(parcel_id)]}, timeout=30.0)
            data = r.json() if r.content else {}
            errs = data.get("DeleteLabelsErrorList") or []
            if r.status_code < 400 and not errs:
                return VoidResponse(success=True, raw=data)
            return VoidResponse(success=False, message=f"{errs or r.status_code}", raw=data)
        except Exception as e:
            return VoidResponse(success=False, message=f"GLS cancel eroare: {e}")

    async def track_awb(self, db: AsyncSession, awb: str, account_key: Optional[str] = None) -> TrackingResponse:
        try:
            creds = await self.get_credentials(db, account_key)
        except ValueError:
            return TrackingResponse(status="no-credentials", date=None)
        base = self._base(creds)
        try:
            r = await self.client.post(f"{base}/GetParcelStatuses",
                                       json={**self._auth(creds), "ParcelNumber": int(awb),
                                             "ReturnPOD": False, "LanguageIsoCode": "EN"}, timeout=20.0)
        except Exception:
            return TrackingResponse(status="tracking-error", date=None)
        if r.status_code != 200:
            return TrackingResponse(status=f"HTTP {r.status_code}", date=None)
        data = r.json() if r.content else {}
        statuses = data.get("ParcelStatusList") or []
        if not statuses:
            return TrackingResponse(status="AWB Generat", date=None, raw_data=data)
        last = statuses[-1]
        return TrackingResponse(status=last.get("StatusDescription") or last.get("StatusCode") or "Unknown",
                                date=None, raw_data=data)
