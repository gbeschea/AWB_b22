# services/couriers/sameday.py
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from models import Order
from .base import BaseCourier, TrackingResponse

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
    async def create_awb(self, db: AsyncSession, order: Order, account_key: str) -> Dict[str, Any]:
        """
        Neimplementat aici – păstrăm comportamentul tău.
        """
        raise NotImplementedError("Crearea AWB Sameday nu e implementată în această versiune.")

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
