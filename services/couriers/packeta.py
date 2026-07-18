# services/couriers/packeta.py
from __future__ import annotations
from typing import Optional, Dict, Any
from datetime import datetime
import logging
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import base64
import models
from .base import BaseCourier, TrackingResponse, VoidResponse
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

    @staticmethod
    def _xtag(txt: str, tag: str) -> Optional[str]:
        a, b = f"<{tag}>", f"</{tag}>"
        i, j = txt.find(a), txt.find(b)
        return txt[i + len(a): j].strip() if i != -1 and j != -1 and j > i else None

    async def create_awb(self, db: AsyncSession, order: models.Order, account_key: str,
                         *, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Create a Packeta (Zásilkovna) packet via the XML API (<createPacket>).
        Requires a destination `addressId` — a pickup-point id (locker/point) or the
        home-delivery carrier id — passed via options.address_id or creds.default_address_id."""
        opts = options or {}
        acct = await self._get_account(db, account_key)
        if not acct or not acct.credentials:
            raise RuntimeError("Packeta: cont/credențiale lipsă.")
        creds: Dict[str, Any] = acct.credentials or {}
        api = creds.get("api") or {}
        api_password = api.get("api_password") or api.get("password")
        if not api_password:
            raise RuntimeError("Packeta: lipsește api_password.")
        base_url = (creds.get("base_url") or DEFAULT_BASE_URL).rstrip("/")

        address_id = opts.get("address_id") or creds.get("default_address_id")
        if not address_id:
            raise RuntimeError(
                "Packeta: lipsește addressId (punct de ridicare sau curier livrare la domiciliu). "
                "Setează-l în opțiuni sau ca default_address_id pe cont.")

        full = (getattr(order, "shipping_name", None) or getattr(order, "customer", None) or "").strip()
        parts = full.split()
        first = parts[0] if parts else (full or "Client")
        last = " ".join(parts[1:]) if len(parts) > 1 else first
        cod = float(opts.get("cod_amount") or 0.0)
        weight = float(opts.get("total_weight") or 1.0)
        value = float(opts.get("declared_value") or getattr(order, "total_price", None) or weight)

        root = Element("createPacket")
        SubElement(root, "apiPassword").text = api_password
        pa = SubElement(root, "packetAttributes")

        def add(tag: str, val: Any):
            if val not in (None, ""):
                SubElement(pa, tag).text = str(val)

        add("number", getattr(order, "name", None))
        add("name", first)
        add("surname", last)
        add("email", getattr(order, "shipping_email", None) or "")
        add("phone", getattr(order, "shipping_phone", None))
        add("addressId", address_id)
        add("cod", round(cod, 2) if cod > 0 else 0)
        add("value", round(value, 2) or round(weight, 2))
        add("weight", weight)
        add("currency", opts.get("currency") or "RON")
        eshop = opts.get("eshop") or creds.get("eshop")
        if not eshop:
            raise RuntimeError("Packeta: lipsește 'eshop' (id-ul expeditorului înregistrat în contul Packeta).")
        add("eshop", eshop)
        # Home-delivery address (ignored for pickup-point addressId)
        add("street", getattr(order, "shipping_address1", None))
        add("city", getattr(order, "shipping_city", None))
        add("zip", getattr(order, "shipping_zip", None))

        xml_body = tostring(root, encoding="utf-8")
        r = await self.client.post(base_url, content=xml_body,
                                   headers={"Content-Type": "application/xml", "Accept": "application/xml"}, timeout=45.0)
        txt = r.text or ""
        if self._xtag(txt, "status") != "ok":
            fault = self._xtag(txt, "fault") or self._xtag(txt, "string") or txt[:400]
            raise RuntimeError(f"Packeta create: {fault}")
        barcode = self._xtag(txt, "barcode")
        pid = self._xtag(txt, "id")
        if not barcode:
            raise RuntimeError(f"Packeta: răspuns fără barcode: {txt[:300]}")
        return {"awb": barcode, "raw": {"id": pid, "barcode": barcode}, "label_available": bool(pid),
                "packet_id": pid}

    async def get_label(self, awb: str, creds: dict, paper_size: str) -> bytes:
        """Fetch the Packeta label PDF (<packetLabelPdf>). Needs the internal packet id — pass
        it as creds['packet_id'] (stored at creation)."""
        api = (creds or {}).get("api") or {}
        api_password = api.get("api_password") or api.get("password")
        packet_id = (creds or {}).get("packet_id")
        if not packet_id:
            raise NotImplementedError("Packeta: eticheta necesită packet_id (din răspunsul de la creare).")
        base_url = ((creds or {}).get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        root = Element("packetLabelPdf")
        SubElement(root, "apiPassword").text = api_password
        SubElement(root, "packetId").text = str(packet_id)
        SubElement(root, "format").text = "A6 on A4" if (paper_size or "A6").upper() == "A6" else "A4"
        SubElement(root, "offset").text = "0"
        r = await self.client.post(base_url, content=tostring(root, encoding="utf-8"),
                                   headers={"Content-Type": "application/xml"}, timeout=30.0)
        txt = r.text or ""
        b64 = self._xtag(txt, "result")
        if self._xtag(txt, "status") != "ok" or not b64:
            raise RuntimeError(f"Packeta label: {self._xtag(txt, 'fault') or txt[:200]}")
        return base64.b64decode(b64)

    async def void_awb(self, db: AsyncSession, awb: str, account_key: Optional[str] = None) -> VoidResponse:
        """Cancel a Packeta packet (<packetCancel> by packet id). Barcode alone can't cancel —
        needs the packet id; if not resolvable, report unsupported."""
        acct = await self._get_account(db, account_key or "")
        creds = (acct.credentials if acct else {}) or {}
        api = creds.get("api") or {}
        api_password = api.get("api_password") or api.get("password")
        packet_id = creds.get("packet_id")
        if not (api_password and packet_id):
            return VoidResponse(success=False, message="Packeta: anularea necesită packet_id.")
        base_url = (creds.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        root = Element("packetCancel")
        SubElement(root, "apiPassword").text = api_password
        SubElement(root, "packetId").text = str(packet_id)
        try:
            r = await self.client.post(base_url, content=tostring(root, encoding="utf-8"),
                                       headers={"Content-Type": "application/xml"}, timeout=30.0)
            ok = self._xtag(r.text or "", "status") == "ok"
            return VoidResponse(success=ok, message=None if ok else (r.text or "")[:200])
        except Exception as e:
            return VoidResponse(success=False, message=f"Packeta cancel eroare: {e}")
