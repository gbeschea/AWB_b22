from __future__ import annotations

from typing import Optional, Dict, Any, Tuple
from datetime import datetime, date, timedelta
import logging
import httpx
import json
import re

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

import models
from .base import BaseCourier, TrackingResponse

log = logging.getLogger("dpd")
log.setLevel(logging.INFO)


def _mask_headers(h: Optional[Dict[str, str]]) -> Dict[str, str]:
    return {k: ("***" if k.lower().startswith("authorization") else v) for k, v in (h or {}).items()}

DPD_BASE_URL = "https://api.dpd.ro/v1"
DPD_PICKUP_CUTOFF_HOUR = 16

# Mapări pentru modul de ambalare (profil -> DPD)
DPD_PACK_MAP = {
    "CUTIE DE CARTON": "BOX",
    "CUTIE": "BOX",
    "BOX": "BOX",
    "PALET": "PALLET",
    "PALLET": "PALLET",
    "PLIC": "ENVELOPE",
    "ENVELOPE": "ENVELOPE",
    "SAC": "BAG",
    "BAG": "BAG",
    "FOLIE": "WRAP",
    "WRAP": "WRAP",
}
def _map_package(v: Optional[str]) -> str:
    key = (v or "").strip().upper()
    return DPD_PACK_MAP.get(key, "BOX")


def _safe_str(x) -> str:
    return (x or "").strip()


def _next_business_day(start: Optional[datetime] = None) -> str:
    now = start or datetime.now()
    d = now.date()
    if now.hour >= DPD_PICKUP_CUTOFF_HOUR:
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _drop_nones(x):
    if isinstance(x, dict):
        return {k: _drop_nones(v) for k, v in x.items() if v is not None}
    if isinstance(x, list):
        return [_drop_nones(v) for v in x]
    return x


def _split_street_and_no(s: str) -> Tuple[Optional[str], Optional[str]]:
    s = (s or "").strip()
    if not s:
        return None, None
    m = re.search(r"^(.*?)(?:\s+nr\.?\s*)?(\d+[A-Za-z]?)$", s, flags=re.IGNORECASE)
    if m:
        street = m.group(1).strip(", ").strip()
        no = m.group(2)
    else:
        street = s
        no = None
    return street, no


def _get_items_list(order):
    for name in ("items", "line_items", "lines", "products"):
        v = getattr(order, name, None)
        if isinstance(v, (list, tuple)) and v:
            return list(v)
    return []


def _build_content_line(order) -> Tuple[str, int]:
    """Return 'ORDER / QTY x SKU' fără a declanșa lazy-load.
       Citește items doar dacă sunt deja pre-încărcate în memorie."""
    order_no = (
        _safe_str(getattr(order, "order_number", None))
        or _safe_str(getattr(order, "name", None))
        or _safe_str(getattr(order, "number", None))
        or _safe_str(getattr(order, "reference", None))
        or _safe_str(getattr(order, "external_id", None))
        or "ORDER"
    )

    items = _get_items_list(order)
    qty = 1
    sku_or_title = "COLET"
    if items:
        it = items[0]
        try:
            qty = int(getattr(it, "quantity", None) or getattr(it, "qty", None) or getattr(it, "count", None) or 1)
        except Exception:
            qty = 1
        sku = _safe_str(getattr(it, "sku", None)) or _safe_str(getattr(it, "code", None)) or _safe_str(getattr(it, "product_code", None))
        title = _safe_str(getattr(it, "title", None)) or _safe_str(getattr(it, "name", None)) or _safe_str(getattr(it, "product_name", None))
        sku_or_title = sku or title or "COLET"

    desc = f"{order_no} / {qty} x {sku_or_title}"
    desc = re.sub(r"\s+", " ", str(desc)).strip()
    if len(desc) > 100:
        desc = desc[:100]
    return desc, qty


class DPDCourier(BaseCourier):
    def __init__(self, client: httpx.AsyncClient):
        super().__init__(client)

    async def _get_account(self, db: AsyncSession, account_key: str) -> Optional[models.CourierAccount]:
        res = await db.execute(
            select(models.CourierAccount).where(models.CourierAccount.account_key == account_key)
        )
        return res.scalar_one_or_none()

    async def create_awb(
        self,
        db: AsyncSession,
        order: models.Order,
        account_key: str,
        *,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        opts = options or {}

        acct = await self._get_account(db, account_key)
        if not acct or not getattr(acct, "credentials", None):
            raise RuntimeError("DPD: contul de curier lipsește sau nu are credențiale.")

        service_id = (
            opts.get("service_id") or opts.get("serviceId") or opts.get("serviceNumber")
            or (acct.credentials or {}).get("service_id")
            or (acct.credentials or {}).get("serviceId")
            or (acct.credentials or {}).get("serviceNumber")
            or getattr(acct, "service_id", None)
            or getattr(acct, "default_service_id", None)
            or 2505
        )

        parcels_count = int(opts.get("parcels_count") or 1)
        total_weight = float(opts.get("total_weight") or 1.0)

        cod_raw = opts.get("cod_amount")
        cod_amount = 0.0 if cod_raw in (None, "", "0", 0) else float(cod_raw)

        payer = (opts.get("payer") or "SENDER").upper()
        if payer not in ("SENDER", "RECIPIENT", "THIRD_PARTY"):
            payer = "SENDER"

        private_person = opts.get("recipient_private_person")
        if private_person is None:
            ship_company = getattr(order, "shipping_company", None)
            private_person = False if (ship_company and str(ship_company).strip()) else True

        pickup_date = opts.get("pickup_date") or _next_business_day()

        third_party_client_id = (
            opts.get("third_party_client_id")
            or (acct.credentials or {}).get("third_party_client_id")
            or (acct.credentials or {}).get("thirdPartyClientId")
            or (acct.credentials or {}).get("payer_client_id")
        )

        package = opts.get("package")  # ex. BOX / PALLET / ENVELOPE / BAG / WRAP sau denumire RO

        return await self._create_awb_impl(
            order=order,
            account=acct,
            service_id=int(service_id),
            parcels_count=parcels_count,
            total_weight=total_weight,
            cod_amount=cod_amount,
            payer=payer,
            private_person=private_person,
            pickup_date=pickup_date,
            third_party_client_id=third_party_client_id,
            package=package,
        )

    async def _create_awb_impl(
        self,
        *,
        order: models.Order,
        account: models.CourierAccount,
        service_id: int,
        parcels_count: int,
        total_weight: float,
        cod_amount: float,
        payer: str,
        private_person: bool,
        pickup_date: str,
        third_party_client_id: Optional[str],
        package: Optional[str],
    ) -> Dict[str, Any]:

        if not service_id:
            raise RuntimeError("DPD: Service ID lipsește. Selectează un profil cu serviciu sau completează manual.")

        creds = account.credentials or {}

        # recipient
        ship_name = _safe_str(getattr(order, "shipping_name", None))
        ship_company = _safe_str(getattr(order, "shipping_company", None))
        ship_phone = _safe_str(getattr(order, "shipping_phone", None))
        ship_email = _safe_str(getattr(order, "shipping_email", None))
        ship_addr1 = _safe_str(getattr(order, "shipping_address1", None)) or _safe_str(getattr(order, "shipping_street", None))
        ship_city = _safe_str(getattr(order, "shipping_city", None))
        ship_zip = _safe_str(getattr(order, "shipping_zip", None)) or _safe_str(getattr(order, "shipping_postcode", None))
        ship_country = _safe_str(getattr(order, "shipping_country", None)) or "RO"
        ship_country = ship_country.strip().upper()
        if len(ship_country) != 2:
            ship_country = "RO"

        receiver_name = ship_name or ship_company
        if not receiver_name:
            raise RuntimeError("DPD: Lipsesc numele/compania destinatarului.")

        # sender (optional)
        sender_address = (creds.get("sender_address") or {}) if isinstance(creds, dict) else {}
        sender_name = (
            _safe_str(sender_address.get("contact_person"))
            or _safe_str(sender_address.get("name"))
            or _safe_str(getattr(account, "name", None))
            or "Sender"
        )
        sender_phone = _safe_str(sender_address.get("phone") or creds.get("phone"))
        sender_email = _safe_str(sender_address.get("email") or creds.get("email"))
        sender_obj = {
            "name": sender_name,
            "street": _safe_str(sender_address.get("street")),
            "city": _safe_str(sender_address.get("city")),
            "postCode": _safe_str(sender_address.get("postcode") or sender_address.get("zip")),
            "country": _safe_str(sender_address.get("country") or "RO"),
            "phone": sender_phone,
            "email": sender_email,
        }
        if not (sender_obj["street"] and sender_obj["city"]):
            sender_obj = None

        # weights/content
        count_int = max(int(parcels_count or 1), 1)
        try:
            total_w = float(total_weight or 1.0)
        except Exception:
            total_w = 1.0
        per_parcel_w = max(total_w / count_int, 0.1)

        cod_obj = {"amount": round(float(cod_amount or 0), 2), "currency": "RON"} if (cod_amount or 0) > 0 else None

        desc, qty = _build_content_line(order)

        content_block = {
            "parcelsCount": count_int,
            "totalWeight": round(total_w, 3),
            "contents": desc,
            "package": _map_package(package),
        }

        # payload
        payload: Dict[str, Any] = {
            "clientId": creds.get("dpd_client_id") or creds.get("client_id") or creds.get("clientId"),
            "service": {
                "serviceId": int(service_id),
                "pickupDate": pickup_date,
                "autoAdjustPickupDate": True
            },
            "payment": {"courierServicePayer": payer},
            "recipient": {
                "clientName": receiver_name,
                "companyName": ship_company or None,
                "privatePerson": bool(private_person),
                "email": ship_email or None,
                "phone1": {"number": ship_phone} if ship_phone else None,
                "address": _drop_nones({
                    "countryId": 642 if ship_country == "RO" else None,
                    "siteName": (ship_city or "").upper() or None,
                    "postCode": ship_zip or None,
                    "streetType": "str.",
                    "streetName": (_split_street_and_no(ship_addr1)[0] or "").upper() if ship_addr1 else None,
                    "streetNo": _split_street_and_no(ship_addr1)[1] if ship_addr1 else None,
                })
            },
            "content": content_block,
            "parcels": [{"sequence": i + 1, "weight": round(per_parcel_w, 3)} for i in range(count_int)],
            "userName": creds.get("username"),
            "password": creds.get("password"),
            "language": "RO",
        }

        if payer == "THIRD_PARTY":
            if not third_party_client_id:
                raise RuntimeError("DPD: Pentru 'Contract/tert' trebuie să setezi third_party_client_id (clientId-ul plătitorului).")
            payload["payment"]["payerClientId"] = str(third_party_client_id)

        if sender_obj:
            payload["sender"] = sender_obj
        if cod_obj:
            payload["cod"] = cod_obj

        payload = _drop_nones(payload)

        url = f"{DPD_BASE_URL}/shipment"
        headers = {"Accept": "application/json"}

        log.info("DPD REQUEST -> POST %s | Headers: %s | Body: %s",
                 url, _mask_headers(headers), json.dumps(payload, ensure_ascii=False)[:2000])

        try:
            resp = await self.client.post(
                url,
                json=payload,
                headers=headers,
                timeout=45.0,
                follow_redirects=False,
            )

            ct = (resp.headers.get("content-type") or "").lower()
            body_preview = resp.text[:800]
            log.info("DPD RESPONSE <- %s | CT=%s | Body[:800]=%r", resp.status_code, ct, body_preview)

            if "text/html" in ct or "<html" in resp.text.lower():
                raise RuntimeError(
                    f"Ai nimerit portalul web DPD (nu API). Verifică URL-ul {url}, metoda și autentificarea. "
                    f"CT={ct}. Body[:200]={resp.text[:200]!r}"
                )

            if resp.status_code >= 400:
                try:
                    err = resp.json()
                    msg = (
                        (err.get("error") or {}).get("message")
                        or err.get("message")
                        or resp.text
                    )
                except Exception:
                    msg = resp.text
                raise RuntimeError(f"DPD Error ({resp.status_code}): {msg}")

            data = resp.json()
            awb = data.get("awb") or data.get("shipmentNumber") or data.get("id")
            if not awb:
                raise RuntimeError(f"DPD: răspuns neașteptat: {data}")

            return {
                "awb": str(awb),
                "raw": data,
                "label_available": True,
            }

        except httpx.HTTPError as e:
            raise RuntimeError(f"Eroare rețea DPD: {str(e)}")
        except Exception as e:
            raise RuntimeError(f"O eroare neașteptată a apărut: {e}")

    # tracking & label (nemodificate funcțional)
    async def track_awb(self, db: AsyncSession, awb: str, account_key: Optional[str]) -> TrackingResponse:
        if not account_key:
            return TrackingResponse(status="error", date=datetime.now(), raw_data={"error": "Account key is missing for DPD tracking."})
        
        creds = await self.get_credentials(db, account_key)
        
        url = f"{DPD_BASE_URL}/tracking/{awb}"
        # Presupunem că DPD folosește user/parolă pentru a obține un token, sau un token direct
        # Acest cod presupune un token direct in credentials
        headers = {"Authorization": f"Bearer {creds.get('token')}"}
        try:
            res = await self.client.get(url, headers=headers)
            if res.status_code == 404:
                return TrackingResponse(status="not found", date=datetime.now(), raw_data=None)
            res.raise_for_status()
            data = res.json()
            status = data.get("status", {}).get("status")
            dt_str = data.get("status", {}).get("timestamp")
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00")) if dt_str else None
            return TrackingResponse(status=status, date=dt, raw_data=data)
        except Exception as e:
            log.error("Excepție tracking DPD %s: %s", awb, e)
            return TrackingResponse(status="tracking-error", date=None, raw_data=None)


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
            res = await self.client.post(url, json=body, headers={"Accept": "application/pdf, application/json"}, timeout=45.0, follow_redirects=False)
            res.raise_for_status()
            if "application/pdf" in (res.headers.get("content-type") or ""):
                return res.content
            try:
                msg = res.json().get("error", {}).get("message", "Răspuns necunoscut")
            except Exception:
                msg = res.text[:300]
            raise RuntimeError(f"Eroare DPD: {msg}")
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Eroare API DPD: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            raise RuntimeError(f"Eroare la descărcarea etichetei DPD: {e}")
