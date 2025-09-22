# /services/couriers/dpd.py

from typing import Optional, Dict, Any
from datetime import datetime
import logging
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import models
from .base import BaseCourier, TrackingResponse

class DPDCourier(BaseCourier):
    def __init__(self, client: httpx.AsyncClient):
        super().__init__(client)

    async def create_awb(self, db: AsyncSession, order: models.Order, account_key: str) -> Dict[str, Any]:
        raise NotImplementedError("Funcția create_awb nu este implementată.")

    # --- MODIFICARE MAJORĂ AICI: Refactorizarea completă a funcției ---
    async def track_awb(self, db: AsyncSession, awb: str, account_key: Optional[str]) -> TrackingResponse:
        """Interoghează API-ul DPD folosind credențiale din baza de date."""
        if not account_key:
            return TrackingResponse(status='Cont Lipsă', date=None)

        # Pas 1: Preluăm credențialele din baza de date
        stmt = select(models.CourierAccount).where(models.CourierAccount.account_key == account_key)
        result = await db.execute(stmt)
        account = result.scalar_one_or_none()

        if not (account and account.credentials):
            logging.error(f"DPD: Nu s-au găsit credențiale în DB pentru contul: {account_key}")
            return TrackingResponse(status='Cont Necunoscut', date=None)
        
        creds = account.credentials
        url = 'https://api.dpd.ro/v1/track/'
        body = {
            'userName': creds.get('username'), 
            'password': creds.get('password'), 
            'language': 'EN', 
            'parcels': [{'id': awb}]
        }
        
        try:
            r = await self.client.post(url, json=body, timeout=15.0)
            if r.status_code != 200:
                logging.warning(f"DPD HTTP Error {r.status_code} pentru AWB {awb}")
                return TrackingResponse(status=f'HTTP {r.status_code}', date=None)
            
            data = (r.json() or {}).get('parcels', [{}])[0]
            operations = data.get('operations', [])
            if not operations:
                return TrackingResponse(status='AWB Generat', date=None, raw_data=data)

            last_op = operations[-1]
            last_desc = (last_op.get('description') or 'N/A').strip()
            date_str = last_op.get('date')
            last_dt = datetime.fromisoformat(date_str.replace('Z', '+00:00')) if date_str else None
            
            return TrackingResponse(status=last_desc, date=last_dt, raw_data=data)
        except Exception as e:
            logging.error(f"Excepție la tracking DPD pentru AWB {awb}: {e}")
            return TrackingResponse(status='Eroare Tracking', date=None)

    async def get_label(self, awb: str, creds: dict, paper_size: str) -> bytes:
        """Funcția de printare, care folosește credențialele primite din DB."""
        body = {
            'userName': creds.get('username'), 
            'password': creds.get('password'), 
            'paperSize': paper_size or 'A6', 
            'parcels': [{'parcel': {'id': awb}}]
        }
        try:
            res = await self.client.post('https://api.dpd.ro/v1/print', json=body, timeout=45)
            res.raise_for_status()
            if 'application/pdf' in res.headers.get('content-type', ''):
                return res.content
            error_msg = res.json().get('error', {}).get('message', 'Răspuns necunoscut')
            raise Exception(f"Eroare DPD: {error_msg}")
        except httpx.HTTPStatusError as e:
            raise Exception(f"Eroare API DPD: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            raise Exception(f"Eroare la descărcarea etichetei DPD: {e}")