# services/couriers/sameday.py
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from models import Order
from .base import BaseCourier, TrackingResponse, VoidResponse

log = logging.getLogger("couriers.sameday")
log.setLevel(logging.INFO)


class SamedayCourier(BaseCourier):
    """
    Integrare Sameday bazată pe credențiale din DB (courier_accounts.credentials).

    Fluxuri suportate:
      - Auth:  POST /api/authenticate                      -> token în JSON; acceptă autentificare prin headere
      - Track: GET  /api/client/awb/{awb}/status           -> header X-AUTH-TOKEN
      - Label: GET  /api/awb/download/{awb}/{size}         -> header X-AUTH-TOKEN
    """

    PROD_BASE_URL = "https://api.sameday.ro"
    SANDBOX_BASE_URL = "https://sameday-api.demo.zitec.com"
    AUTH_PATH = "/api/authenticate"
    TRACK_PATH_TMPL = "/api/client/awb/{awb}/status"
    LABEL_PATH_TMPL = "/api/awb/download/{awb}/{size}"
    CREATE_PATH = "/api/awb"
    CANCEL_PATH_TMPL = "/api/awb/{awb}"

    _rate_limit_interval: float = 0.20  # mic delay între call-uri, ca să evităm rate limits

    def __init__(self, client: httpx.AsyncClient):
        super().__init__(client)
        # cache token per (base_url, username)
        self._token_cache: Dict[str, Dict[str, Any]] = {}

    # ----------------------- helpers -----------------------
    @staticmethod
    def _choose_base(creds: Dict[str, Any]) -> str:
        base = (creds.get("base_url") or creds.get("BASE_URL") or "").strip()
        if base:
            return base.rstrip("/")
        env = (creds.get("env") or creds.get("environment") or "").lower()
        if env in {"sandbox", "demo", "test"}:
            return SamedayCourier.SANDBOX_BASE_URL
        return SamedayCourier.PROD_BASE_URL

    @staticmethod
    def _username(creds: Dict[str, Any]) -> Optional[str]:
        # suportă câteva alias-uri uzuale
        return creds.get("username") or creds.get("userName") or creds.get("user") or creds.get("email")

    @staticmethod
    def _password(creds: Dict[str, Any]) -> Optional[str]:
        return creds.get("password") or creds.get("pass")

    def _token_valid(self, entry: Dict[str, Any]) -> bool:
        return bool(entry.get("token") and entry.get("expires_at") and datetime.now(timezone.utc) + timedelta(seconds=60) < entry["expires_at"])

    async def _get_token(self, base_url: str, creds: Dict[str, Any]) -> Optional[str]:
        """
        Autentificare Sameday.
        - Prioritizează varianta pe care ai folosit-o în trecut: headere X-Auth-Username / X-Auth-Password.
        - Fallback: JSON body {"username","password"} în caz că e nevoie.
        - Cache local ~55min.
        """
        user = self._username(creds); pwd = self._password(creds)
        if not user or not pwd:
            log.error("Sameday: lipsesc username/password în credentials.")
            return None

        cache_key = f"{base_url}::{user}"
        cached = self._token_cache.get(cache_key)
        if cached and self._token_valid(cached):
            return cached["token"]

        url = f"{base_url}{self.AUTH_PATH}"
        try:
            # 1) Varianta istorică (headere) – cea mai compatibilă cu implementările existente
            headers = {'X-Auth-Username': user, 'X-Auth-Password': pwd}
            res = await self.client.post(url, headers=headers, timeout=20.0)
            if res.status_code == 200:
                token = (res.json() or {}).get("token")
                if token:
                    self._token_cache[cache_key] = {"token": token, "expires_at": datetime.now(timezone.utc) + timedelta(minutes=55)}
                    return token
                else:
                    log.warning("Sameday auth (headers): token lipsă în răspuns, încerc JSON body...")

            # 2) Fallback: JSON body
            res2 = await self.client.post(url, json={"username": user, "password": pwd}, timeout=20.0)
            if res2.status_code == 200:
                token = (res2.json() or {}).get("token")
                if token:
                    self._token_cache[cache_key] = {"token": token, "expires_at": datetime.now(timezone.utc) + timedelta(minutes=55)}
                    return token

            log.error("Sameday auth failed: H1=%s, H2=%s, body2=%s", res.status_code, res2.status_code, res2.text[:300])
            return None

        except Exception as e:
            log.exception("Sameday auth exception: %s", e)
            return None

    # ----------------------- interfață publică -----------------------
    async def create_awb(
        self, db: AsyncSession, order: Order, account_key: str,
        *, options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a Sameday eAWB (POST /api/awb, form-encoded, X-AUTH-TOKEN).

        Sender/pickup + service defaults come from the account credentials (pickup_point_id,
        default_service_id, default_package_type, awb_payment). Recipient city/county go as
        strings — Sameday resolves them."""
        opts = options or {}
        creds = await self.get_credentials(db, account_key)
        base_url = self._choose_base(creds)
        token = await self._get_token(base_url, creds)
        if not token:
            raise RuntimeError("Sameday: autentificare eșuată.")

        service = int(opts.get("service_id") or creds.get("default_service_id") or 0)
        if not service:
            raise RuntimeError("Sameday: lipsește service_id (default_service_id).")
        pickup_point = opts.get("pickup_point_id") or creds.get("pickup_point_id")
        if not pickup_point:
            raise RuntimeError("Sameday: lipsește pickup_point_id.")
        contact_person = opts.get("contact_person") or creds.get("contact_person_id")
        package_type = int(opts.get("package_type", creds.get("default_package_type", 0)) or 0)
        awb_payment = int(opts.get("awb_payment", creds.get("awb_payment", 1)) or 1)
        parcels_count = max(int(opts.get("parcels_count") or 1), 1)
        total_weight = float(opts.get("total_weight") or 1.0)
        cod = float(opts.get("cod_amount") or 0.0)

        def g(*names) -> str:
            for n in names:
                v = getattr(order, n, None)
                if v:
                    return str(v).strip()
            return ""

        name = g("shipping_name", "customer")
        phone = g("shipping_phone")
        city = g("shipping_city")
        county = g("shipping_province")
        address = g("shipping_address1")
        addr2 = g("shipping_address2")
        if addr2:
            address = f"{address}, {addr2}".strip(", ")
        postal = g("shipping_zip")
        email = g("shipping_email")
        company = g("shipping_company")
        if not (name and phone and city and address):
            raise RuntimeError("Sameday: lipsesc date destinatar (nume/telefon/oraș/adresă).")
        person_type = 1 if company else 0  # 0=persoană fizică, 1=juridică

        form: Dict[str, str] = {
            "pickupPoint": str(pickup_point),
            "packageType": str(package_type),
            "packageNumber": str(parcels_count),
            "packageWeight": str(round(total_weight, 2)),
            "service": str(service),
            "awbPayment": str(awb_payment),
            "cashOnDelivery": str(round(cod, 2)),
            "insuredValue": "0",
            "thirdPartyPickup": "0",
            "clientInternalReference": (g("name") or "")[:50],
            "awbRecipient[name]": name[:100],
            "awbRecipient[phoneNumber]": phone,
            "awbRecipient[personType]": str(person_type),
            "awbRecipient[companyName]": company,
            "awbRecipient[cityString]": city,
            "awbRecipient[county]": county,
            "awbRecipient[address]": address,
            "awbRecipient[postalCode]": postal,
            "awbRecipient[email]": email,
        }
        if contact_person:
            form["contactPerson"] = str(contact_person)

        per_w = max(total_weight / parcels_count, 0.1)
        for i in range(parcels_count):
            form[f"parcels[{i}][weight]"] = str(round(per_w, 2))
            form[f"parcels[{i}][width]"] = "10"
            form[f"parcels[{i}][height]"] = "10"
            form[f"parcels[{i}][length]"] = "10"

        url = f"{base_url}{self.CREATE_PATH}"
        res = await self.client.post(
            url, data=form,
            headers={"X-AUTH-TOKEN": token, "Accept": "application/json"}, timeout=45.0,
        )
        if res.status_code >= 400:
            raise RuntimeError(f"Sameday create AWB HTTP {res.status_code}: {res.text[:500]}")
        data = res.json() if res.content else {}
        awb = data.get("awbNumber") or data.get("awb_number")
        if not awb:
            raise RuntimeError(f"Sameday: răspuns fără awbNumber: {data}")
        return {"awb": str(awb), "raw": data, "label_available": True}

    async def void_awb(self, db: AsyncSession, awb: str, account_key: Optional[str] = None) -> VoidResponse:
        """Cancel a Sameday AWB (DELETE /api/awb/{awb})."""
        try:
            creds = await self.get_credentials(db, account_key)
        except ValueError:
            return VoidResponse(success=False, message="Sameday: lipsesc credențialele pentru anulare.")
        base_url = self._choose_base(creds)
        token = await self._get_token(base_url, creds)
        if not token:
            return VoidResponse(success=False, message="Sameday: autentificare eșuată la anulare.")
        url = f"{base_url}{self.CANCEL_PATH_TMPL.format(awb=awb)}"
        try:
            res = await self.client.delete(url, headers={"X-AUTH-TOKEN": token}, timeout=30.0)
            if res.status_code in (200, 204):
                return VoidResponse(success=True, raw=(res.json() if res.content else {}))
            return VoidResponse(success=False, message=f"HTTP {res.status_code}: {res.text[:200]}")
        except Exception as e:
            return VoidResponse(success=False, message=f"Eroare rețea Sameday la anulare: {e}")

    async def track_awb(self, db: AsyncSession, awb: str, account_key: Optional[str]) -> TrackingResponse:
        """
        Tracking AWB folosind credențialele din DB (account_key) și token cache.
        """
        try:
            creds = await self.get_credentials(db, account_key)  # ridică ValueError dacă lipsesc
            base_url = self._choose_base(creds)
            token = await self._get_token(base_url, creds)
            if not token:
                return TrackingResponse(status="auth-error", date=None)

            await asyncio.sleep(self._rate_limit_interval)

            url = f"{base_url}{self.TRACK_PATH_TMPL.format(awb=awb)}"
            res = await self.client.get(url, headers={'X-AUTH-TOKEN': token}, timeout=20.0)

            if res.status_code == 404:
                return TrackingResponse(status="not found", date=None)
            if res.status_code != 200:
                return TrackingResponse(status=f"HTTP {res.status_code}", date=None)

            data: Dict[str, Any] = res.json() if res.content else {}
            history: List[Dict[str, Any]] = data.get("expeditionHistory", []) or []

            if not history:
                return TrackingResponse(status="AWB Generat", date=None, raw_data=data)

            def _parse_dt(s: str) -> datetime:
                try:
                    return datetime.fromisoformat(s.replace("Z", "+00:00"))
                except Exception:
                    try:
                        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    except Exception:
                        return datetime.now(timezone.utc)

            latest = max(history, key=lambda e: _parse_dt(e.get("statusDate") or e.get("date") or ""))
            status = latest.get("statusLabel") or latest.get("status") or "Unknown"
            when = latest.get("statusDate") or latest.get("date")
            dt = _parse_dt(when) if when else None

            return TrackingResponse(status=status, date=dt, raw_data=data)

        except Exception as e:
            log.exception("Sameday tracking exception for %s: %s", awb, e)
            return TrackingResponse(status="Eroare Tracking", date=None)

    async def get_label(self, awb: str, creds: dict, paper_size: str) -> bytes:
        """
        Download etichetă PDF (A4/A6).
        """
        base_url = self._choose_base(creds)
        token = await self._get_token(base_url, creds)
        if not token:
            raise RuntimeError("Autentificare Sameday eșuată.")
        size = "A6" if (paper_size or "A6").upper() == "A6" else "A4"
        url = f"{base_url}{self.LABEL_PATH_TMPL.format(awb=awb, size=size)}"
        res = await self.client.get(url, headers={'X-AUTH-TOKEN': token}, timeout=30.0)
        if res.status_code != 200:
            raise RuntimeError(f"Eroare API Sameday la label: HTTP {res.status_code} - {res.text[:300]}")
        if "application/pdf" not in (res.headers.get("content-type") or ""):
            raise RuntimeError("Răspunsul Sameday nu este PDF.")
        return res.content
