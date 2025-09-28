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


# ------------------------- helpers ------------------------- #

def _to_date_str(v) -> Optional[str]:
    if not v:
        return None
    if isinstance(v, str):
        return v[:10]
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    return str(v)


def _safe_str(x) -> str:
    return (x or "").strip()


def _next_business_day(start: Optional[datetime] = None) -> str:
    """
    Returnează următoarea zi lucrătoare (RO), cu regula simplă:
    - dacă ora curentă >= cutoff -> ziua următoare
    - sari peste weekend (nu ținem cont de sărbători legale)
    """
    now = start or datetime.now()
    d = now.date()
    if now.hour >= DPD_PICKUP_CUTOFF_HOUR:
        d += timedelta(days=1)
    while d.weekday() >= 5:  # 5,6 = weekend
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _drop_nones(x):
    if isinstance(x, dict):
        return {k: _drop_nones(v) for k, v in x.items() if v is not None}
    if isinstance(x, list):
        return [_drop_nones(v) for v in x]
    return x


# ------------------------- courier ------------------------- #

class DPDCourier(BaseCourier):
    def __init__(self, client: httpx.AsyncClient):
        super().__init__(client)

    # -------- accounts -------- #
    async def _get_account(self, db: AsyncSession, account_key: str) -> Optional[models.CourierAccount]:
        res = await db.execute(
            select(models.CourierAccount).where(models.CourierAccount.account_key == account_key)
        )
        return res.scalar_one_or_none()

    # -------- public API used by actions.create_awb_for_order -------- #
    async def create_awb(  # semnătura așteptată de fluxul nou
        self,
        db: AsyncSession,
        order: models.Order,
        account_key: str,
        *,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Adapter stabil: primește (db, order, account_key, options),
        normalizează opțiunile și apelează implementarea unitară.
        """
        opts = options or {}
        # normalize
        service_id = opts.get("service_id")
        parcels_count = int(opts.get("parcels_count") or 1)
        total_weight = float(opts.get("total_weight") or 1.0)

        cod_raw = opts.get("cod_amount")
        cod_amount = 0.0 if cod_raw in (None, "", "0", 0) else float(cod_raw)

        payer = (opts.get("payer") or "SENDER").upper()
        if payer not in ("SENDER", "RECIPIENT"):
            payer = "SENDER"

        content_desc = (
            opts.get("content_desc")
            or opts.get("contents_desc")
            or opts.get("content")
            or "Goods"
        )

        # Heuristică pt. persoană fizică: dacă nu avem companie -> True
        private_person = opts.get("recipient_private_person")
        if private_person is None:
            ship_company = getattr(order, "shipping_company", None)
            private_person = False if (ship_company and str(ship_company).strip()) else True

        include_shipping_in_cod = bool(opts.get("include_shipping_in_cod") or False)
        pickup_date = opts.get("pickup_date") or _next_business_day()

        account = await self._get_account(db, account_key)
        if not account or not account.credentials:
            raise RuntimeError("DPD: contul de curier lipsește sau nu are credențiale.")

        return await self._create_awb_impl(
            order=order,
            account=account,
            service_id=service_id,
            parcels_count=parcels_count,
            total_weight=total_weight,
            cod_amount=cod_amount,
            payer=payer,
            content_desc=content_desc,
            private_person=private_person,
            include_shipping_in_cod=include_shipping_in_cod,
            pickup_date=pickup_date,
        )

    # -------- implementare unitary (stabilă) -------- #
    async def _create_awb_impl(
        self,
        *,
        order: models.Order,
        account: models.CourierAccount,
        service_id: Optional[int],
        parcels_count: int,
        total_weight: float,
        cod_amount: float,
        payer: str,
        content_desc: str,
        private_person: bool,
        include_shipping_in_cod: bool,
        pickup_date: str,
    ) -> Dict[str, Any]:
        if not service_id:
            raise RuntimeError("DPD: Service ID lipsește. Selectează un profil cu serviciu sau completează manual.")

        creds = account.credentials or {}

        # ----- receiver (din comandă) ----- #
        ship_name = _safe_str(getattr(order, "shipping_name", None))
        ship_company = _safe_str(getattr(order, "shipping_company", None))
        ship_phone = _safe_str(getattr(order, "shipping_phone", None))
        ship_email = _safe_str(getattr(order, "shipping_email", None))
        ship_addr1 = _safe_str(getattr(order, "shipping_address1", None))
        ship_city = _safe_str(getattr(order, "shipping_city", None))
        ship_zip = _safe_str(getattr(order, "shipping_zip", None))
        ship_country = _safe_str(getattr(order, "shipping_country", None) or "RO")

        receiver_name = ship_name or ship_company
        if not receiver_name:
            # DPD cere obligatoriu nume/companie
            raise RuntimeError("DPD: Lipsesc numele/compania destinatarului.")

        # ----- sender (din cont) ----- #
        sender_address = (creds.get("sender_address") or {}) if isinstance(creds, dict) else {}
        sender_name = (
            _safe_str(sender_address.get("contact_person"))
            or _safe_str(sender_address.get("name"))
            or _safe_str(account.name)
            or "Sender"
        )
        sender_phone = _safe_str(sender_address.get("phone") or creds.get("phone"))
        sender_email = _safe_str(sender_address.get("email") or creds.get("email"))

        # greutate pe colet
        count_int = max(int(parcels_count or 1), 1)
        try:
            total_w = float(total_weight or 1.0)
        except Exception:
            total_w = 1.0
        per_parcel_w = max(total_w / count_int, 0.1)

        # cod
        cod_obj = {"amount": round(float(cod_amount or 0), 2), "currency": "RON"} if (cod_amount or 0) > 0 else None

        # conținut conform API DPD (NU string!)
        content_obj = {
            "parcelsContent": [
                {
                    "name": str(content_desc)[:100],
                    "count": count_int
                }
            ]
        }

        payload: Dict[str, Any] = {
            "clientId": creds.get("dpd_client_id") or creds.get("client_id") or creds.get("clientId"),
            "serviceNumber": int(service_id),
            "pickupDate": pickup_date,
            "payer": payer,
            "sender": {
                "name": sender_name,
                "street": _safe_str(sender_address.get("street")),
                "city": _safe_str(sender_address.get("city")),
                "postCode": _safe_str(sender_address.get("postcode") or sender_address.get("zip")),
                "country": _safe_str(sender_address.get("country") or "RO"),
                "phone": sender_phone,
                "email": sender_email,
            },
            "receiver": {
                "name": receiver_name,
                "companyName": ship_company or None,
                "privatePerson": bool(private_person),
                "street": ship_addr1,
                "city": ship_city,
                "postCode": ship_zip,
                "country": ship_country,
                "phone": ship_phone,
                "email": ship_email,
            },
            "parcels": [{"sequence": i + 1, "weight": round(per_parcel_w, 3)} for i in range(count_int)],
            "content": content_obj,
        }
        if cod_obj:
            payload["cod"] = cod_obj

        payload = _drop_nones(payload)

        # ---- call API ---- #
        url = f"{DPD_BASE_URL}/shipments"
        try:
            resp = await self.client.post(url, json=payload, timeout=45.0)
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

    # -------- tracking & label -------- #
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
