# services/couriers/packeta.py
from __future__ import annotations
from typing import Optional, Dict, Any
from datetime import datetime
import logging
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import models
from .base import BaseCourier, TrackingResponse
from xml.etree.ElementTree import Element, SubElement, tostring

log = logging.getLogger("services.couriers.packeta")

# Endpoint oficial pentru tracking Packeta (REST/XML)
DEFAULT_BASE_URL = "https://www.zasilkovna.cz/api/rest"

class PacketaCourier(BaseCourier):
    def __init__(self, client: httpx.AsyncClient):
        super().__init__(client)

    async def _get_account(self, db: AsyncSession, account_key: str) -> Optional[models.CourierAccount]:
        # întâi după account_key, apoi fallback pe primul cont de tip packeta
        res = await db.execute(
            select(models.CourierAccount).where(models.CourierAccount.account_key == account_key)
        )
        acct = res.scalar_one_or_none()
        if acct:
            return acct
        res = await db.execute(
            select(models.CourierAccount).where(models.CourierAccount.courier_type == "packeta").limit(1)
        )
        return res.scalar_one_or_none()

    async def track_awb(self, db: AsyncSession, awb: str, account_key: Optional[str]) -> TrackingResponse:
        if not account_key:
            return TrackingResponse(success=False, status="Fără account_key", date=None, code=awb)

        acct = await self._get_account(db, account_key)
        if not acct or not acct.credentials:
            return TrackingResponse(success=False, status="Cont inexistent", date=None, code=awb)

        creds: Dict[str, Any] = acct.credentials or {}
        api = creds.get("api", {}) or {}
        # Packeta folosește apiPassword; lăsăm fallback pe 'password' dacă există din versiuni vechi
        api_password = api.get("api_password") or api.get("password")
        if not api_password:
            return TrackingResponse(success=False, status="Lipsește api_password", date=None, code=awb)

        # Body XML <packetTracking>
        root = Element("packetTracking")
        SubElement(root, "apiPassword").text = api_password
        SubElement(root, "barcode").text = awb
        xml_body = tostring(root, encoding="utf-8")

        base_url = (creds.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        headers = {
            "Content-Type": "application/xml",
            "Accept": "application/xml",
            "Accept-Language": creds.get("accept_language") or "ro_RO",
        }

        try:
            r = await self.client.post(base_url, content=xml_body, headers=headers, timeout=30.0)
            if r.status_code != 200:
                return TrackingResponse(success=False, status=f"HTTP {r.status_code}", date=None, code=awb)

            txt = r.text or ""

            def xtag(tag: str) -> Optional[str]:
                a, b = f"<{tag}>", f"</{tag}>"
                i, j = txt.find(a), txt.find(b)
                return txt[i + len(a): j].strip() if i != -1 and j != -1 and j > i else None

            status = (
                xtag("codeText")
                or xtag("statusText")
                or xtag("description")
                or xtag("status")
                or xtag("statusCode")
                or "Unknown"
            )

            ts = xtag("eventTime") or xtag("date")
            dt: Optional[datetime] = None
            if ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    dt = None

            return TrackingResponse(success=True, status=status, date=dt, code=awb, extra={"xml": txt})


        except Exception as e:
            log.error(f"Packeta tracking error {awb}: {e}", exc_info=True)
            return TrackingResponse(success=False, status="Eroare tracking Packeta", date=None, code=awb)

    async def create_awb(self, db: AsyncSession, order: models.Order, account_key: str) -> Dict[str, Any]:
        raise NotImplementedError("Packeta.create_awb neimplementat.")

    async def get_label(self, awb: str, creds: dict, paper_size: str) -> bytes:
        raise NotImplementedError("Packeta.get_label neimplementat.")
