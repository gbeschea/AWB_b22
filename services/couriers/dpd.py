# services/couriers/dpd.py
from __future__ import annotations

from typing import Optional, Dict, Any, List
from datetime import datetime, date, timedelta
import logging
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import models
from .base import BaseCourier, TrackingResponse

DPD_BASE_URL = "https://api.dpd.ro/v1"
DPD_PICKUP_CUTOFF_HOUR = 16  # după 16:00, ridicarea trece pe ziua următoare

def _to_date_str(v) -> Optional[str]:
    if not v:
        return None
    if isinstance(v, str):
        return v[:10]
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    return str(v)

def _next_business_day(start: Optional[datetime] = None) -> str:
    now = start or datetime.now()
    d = now.date()
    if now.hour >= DPD_PICKUP_CUTOFF_HOUR:
        d += timedelta(days=1)
    while d.weekday() >= 5:  # 5,6 weekend
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")

def _safe_str(x) -> str:
    return (x or "").strip()

class DPDCourier(BaseCourier):
    def __init__(self, client: httpx.AsyncClient):
        super().__init__(client)

    async def _get_account(self, db: AsyncSession, account_key: str) -> Optional[models.CourierAccount]:
        res = await db.execute(select(models.CourierAccount).where(models.CourierAccount.account_key == account_key))
        return res.scalar_one_or_none()

    async def create_awb(
        self,
        db: AsyncSession,
        order: models.Order,
        account_key: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        options = options or {}
        account = await self._get_account(db, account_key)
        if not account or not account.credentials:
            raise RuntimeError("Cont DPD invalid sau fără credențiale.")

        creds = account.credentials
        service_id = int(options.get("service_id") or 2505)  # STANDARD
        payer = (options.get("payer") or "SENDER").upper()
        parcels_count = int(options.get("parcels_count") or 1)
        total_weight = float(options.get("total_weight") or 1.0)
        per_parcel_weight = max(total_weight / max(parcels_count, 1), 0.1)
        cod_amount = float(options.get("cod_amount") or 0.0)
        include_shipping = bool(options.get("include_shipping_in_cod", True))
        pickup_date = _to_date_str(options.get("pickup_date")) or _next_business_day()

        # Recipient
        recipient_name = _safe_str(order.shipping_name or order.customer)
        recipient_phone = _safe_str(order.shipping_phone)
        recipient_email = _safe_str(getattr(order, "email", None))
        city = _safe_str(order.shipping_city)
        zip_code = _safe_str(order.shipping_zip)
        country = _safe_str(order.shipping_country or "Romania")
        address1 = _safe_str(order.shipping_address1)
        address2 = _safe_str(order.shipping_address2)
        address_note = " ".join([p for p in [address1, address2] if p]).strip()

        # Conținut
        try:
            # Desc vremuri: "1x SKU | 2x SKU2"
            items = getattr(order, "line_items", None) or []
            content = " | ".join([f"{li.quantity} x {li.sku or li.title or ''}".strip() for li in items]) or order.name or "Shopify Order"
        except Exception:
            content = order.name or "Shopify Order"

        payload: Dict[str, Any] = {
            "userName": creds.get("username"),
            "password": creds.get("password"),
            "serviceId": service_id,
            "takingDate": pickup_date,
            "payer": "SENDER" if payer.startswith("S") else "RECEIVER",
            "referenceNumber": order.name,
            "content": content[:200],
            "parcels": [{"weight": round(per_parcel_weight, 3)} for _ in range(parcels_count)],
            "recipient": {
                "name": recipient_name or "Client",
                "phoneNumber": recipient_phone or None,
                "email": recipient_email or None,
                "address": {
                    "countryId": 642 if country.lower().startswith("rom") else None,
                    "siteName": city,
                    "postCode": zip_code,
                    # Pentru a evita eroarea cu StreetNo/BlockNo, punem totul în note.
                    "addressNote": address_note or city,
                    # Lăsăm street/streetNo goale intenționat; DPD acceptă totul în note.
                    "street": None,
                    "streetNo": None,
                },
            },
            "options": {
                "includeShippingInCod": include_shipping,
            },
        }

        if cod_amount and cod_amount > 0:
            payload["cod"] = {"amount": round(float(cod_amount), 2), "currency": "RON"}

        # 1) Creează shipment
        url = f"{DPD_BASE_URL}/shipment"
        logging.getLogger("httpx").info("POST %s", url)
        res = await self.client.post(url, json=payload, timeout=45.0)
        try:
            data = res.json()
        except Exception:
            data = {}

        if res.status_code != 200:
            raise RuntimeError(f"Eroare DPD ({res.status_code}): {res.text}")

        # Dacă API-ul întoarce eroare business
        err = (data.get("error") or {}).get("message")
        if err and "Ora de preluare este in afara orelor de program" in err:
            # retry cu următoarea zi lucrătoare
            payload["takingDate"] = _next_business_day(datetime.strptime(pickup_date, "%Y-%m-%d") + timedelta(days=1))
            res = await self.client.post(url, json=payload, timeout=45.0)
            try:
                data = res.json()
            except Exception:
                data = {}
            err = (data.get("error") or {}).get("message")

        if err:
            raise RuntimeError(f"DPD Error: {err}")

        # Extrage AWB
        awb = None
        if isinstance(data, dict):
            # răspunsurile obișnuite au 'parcels': [{'id': '<AWB>'}, ...]
            parcels = data.get("parcels") or data.get("result") or []
            if isinstance(parcels, list) and parcels:
                first = parcels[0]
                awb = first.get("id") or first.get("parcel", {}).get("id")
            # uneori vine direct {"id": "..."} etc.
            awb = awb or data.get("id") or data.get("awb")

        if not awb:
            raise RuntimeError("DPD nu a returnat un AWB.")

        return {"awb": str(awb), "raw_response": data}

    async def track_awb(self, db: AsyncSession, awb: str, account_key: Optional[str]) -> TrackingResponse:
        account = await self._get_account(db, account_key) if account_key else None
        if not account or not account.credentials:
            return TrackingResponse(status="no-credentials", date=None)

        creds = account.credentials
        url = f"{DPD_BASE_URL}/track/"
        body = {
            "userName": creds.get("username"),
            "password": creds.get("password"),
            "language": "EN",
            "parcels": [{"id": awb}],
        }
        try:
            r = await self.client.post(url, json=body, timeout=20.0)
            if r.status_code != 200:
                logging.warning("DPD HTTP %s la tracking pentru %s", r.status_code, awb)
                return TrackingResponse(status=f"HTTP {r.status_code}", date=None)
            data = r.json()
            events = (data.get("parcels") or [{}])[0].get("events") or []
            last_ev = events[0] if events else {}
            status = last_ev.get("name") or last_ev.get("status") or "Unknown"
            date_str = last_ev.get("date") or last_ev.get("datetime")
            dt = None
            if date_str:
                try:
                    dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                except Exception:
                    dt = None
            return TrackingResponse(status=status, date=dt, raw_data=data)
        except Exception as e:
            logging.error("Excepție tracking DPD %s: %s", awb, e)
            return TrackingResponse(status="tracking-error", date=None)

    async def get_label(self, awb: str, creds: dict, paper_size: str) -> bytes:
        size = "A6" if (paper_size or "A6").upper() == "A6" else "A4"
        url = f"{DPD_BASE_URL}/print"
        body = {
            "userName": creds.get("username"),
            "password": creds.get("password"),
            "paperSize": size,
            "parcels": [{"parcel": {"id": awb}}],
        }
        try:
            res = await self.client.post(url, json=body, timeout=45.0)
            res.raise_for_status()
            if "application/pdf" in (res.headers.get("content-type") or ""):
                return res.content
            msg = res.json().get("error", {}).get("message", "Răspuns necunoscut")
            raise RuntimeError(f"Eroare DPD: {msg}")
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Eroare API DPD: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            raise RuntimeError(f"Eroare la descărcarea etichetei DPD: {e}")