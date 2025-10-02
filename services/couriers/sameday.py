# /services/couriers/sameday.py

import asyncio
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timezone, timedelta
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from models import Order
from .base import BaseCourier, TrackingResponse
from settings import settings

class SamedayCourier(BaseCourier):
    def __init__(self, client: httpx.AsyncClient):
        super().__init__(client)
        self._tokens: Dict[str, Dict[str, Any]] = {}
        self._token_lock = asyncio.Lock()
        self._rate_limit_interval: float = 0.3

    async def _get_token(self, creds: dict) -> Optional[str]:
        username = creds.get('username')
        if not username:
            raise Exception("Numele de utilizator Sameday lipsește din credențiale.")

        async with self._token_lock:
            if username in self._tokens:
                token_info = self._tokens[username]
                if datetime.now(timezone.utc) < token_info['expires_at']:
                    return token_info['token']
            
            try:
                headers = {'X-Auth-Username': creds['username'], 'X-Auth-Password': creds.get('password')}
                response = await self.client.post('https://api.sameday.ro/api/authenticate', headers=headers)
                response.raise_for_status()
                data = response.json()
                token = data['token']
                self._tokens[username] = {
                    'token': token,
                    'expires_at': datetime.now(timezone.utc) + timedelta(minutes=25)
                }
                return token
            except Exception as e:
                logging.error(f"Autentificare Sameday eșuată pentru {username}: {e}")
                return None

    async def create_awb(self, db: AsyncSession, order: Order, account_key: str) -> Dict[str, Any]:
        raise NotImplementedError("Sameday AWB creation logic is not implemented in this version.")

    async def track_awb(self, db: AsyncSession, awb: str, account_key: Optional[str]) -> TrackingResponse:
        if not account_key:
            return TrackingResponse(status="error", date=datetime.now(timezone.utc), raw_data={"error": "Account key is missing for Sameday tracking."})

        creds = await self.get_credentials(db, account_key)
        token = await self._get_token(creds)
        if not token:
            return TrackingResponse(status="auth-error", date=datetime.now(timezone.utc))

        headers = {"X-Auth-Token": token}
        url = f"https://api.sameday.ro/api/awb/track/{awb}"
        try:
            res = await self.client.get(url, headers=headers)
            if res.status_code == 404:
                logging.warning(f"Sameday AWB {awb} nu a fost găsit (404).")
                return TrackingResponse(status="not found", date=datetime.now(timezone.utc))

            res.raise_for_status()
            data = res.json()
            summary = data.get("summary", {})
            history = data.get("history", [])
            status = summary.get("statusState")
            dt = datetime.fromisoformat(history[-1]["createdAt"]) if history else None
            return TrackingResponse(status=status, date=dt, raw_data=data)
        except httpx.HTTPStatusError as http_err:
            logging.error(f"Eroare HTTP la tracking Sameday AWB {awb}: {http_err}")
            return TrackingResponse(status="tracking-error", date=None)
        except Exception as e:
            logging.error(f"Eroare neașteptată la tracking Sameday AWB {awb}: {e}")
            return TrackingResponse(status="tracking-error", date=None)

    async def get_label(self, awb: str, creds: dict, paper_size: str) -> bytes:
        token = await self._get_token(creds)
        if not token:
            raise Exception("Autentificare Sameday eșuată pentru printare.")
        
        size = "A6" if (paper_size or 'a6').lower() == 'a6' else "A4"
        url = f"https://api.sameday.ro/api/awb/download/{awb}/{size}"
        
        try:
            headers = {'X-AUTH-TOKEN': token}
            res = await self.client.get(url, headers=headers, timeout=30)
            res.raise_for_status()
            if 'application/pdf' in res.headers.get('content-type', ''):
                return res.content
            raise Exception("Răspunsul de la Sameday nu este un PDF.")
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Eroare HTTP Sameday la printare: {e.response.text}")
        except Exception as e:
            raise RuntimeError(f"Eroare neașteptată Sameday la printare: {e}")