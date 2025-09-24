from typing import Optional, Dict, Any
from datetime import datetime
import logging
import re
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

import models
from .base import BaseCourier, TrackingResponse

logger = logging.getLogger(__name__)
DPD_BASE_URL = "https://api.dpd.ro/v1"

# --- Funcții ajutătoare (NU sunt modificate) ---
def _norm_phone(raw: Optional[str]) -> Optional[str]:
    if not raw: return None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("40") and len(digits) >= 11: digits = digits[2:]
    if digits.startswith("0040") and len(digits) >= 12: digits = digits[4:]
    return digits or None

def _safe_str(x: Optional[str]) -> str: return (x or "").strip()
def _country(code: Optional[str]) -> str: return _safe_str(code).upper() or "RO"
def _float_or(v, dv):
    try: return float(v)
    except Exception: return dv

class DPDCourier(BaseCourier):
    def __init__(self, client: httpx.AsyncClient):
        super().__init__(client)

    async def create_awb(self, db: AsyncSession, order: models.Order, account_key: str) -> Dict[str, Any]:
        # Funcția ta existentă pentru creare AWB rămâne aici
        pass

    async def track_awb(self, db: AsyncSession, awb: str, account_key: Optional[str]) -> TrackingResponse:
        if not account_key:
            return TrackingResponse(status='Cont Lipsă', date=None)
        
        stmt = select(models.CourierAccount).where(models.CourierAccount.account_key == account_key)
        account = (await db.execute(stmt)).scalar_one_or_none()
        
        if not (account and account.credentials):
            return TrackingResponse(status='Cont Necunoscut', date=None)
        
        creds = account.credentials
        body = {'userName': creds.get('username'), 'password': creds.get('password'), 'language': 'RO', 'parcels': [{'parcel': {'id': awb}}]}
        
        # === MODIFICAREA CHEIE ESTE AICI: Am folosit endpoint-ul corect din documentație ===
        url = f"{DPD_BASE_URL}/tracking-parcels"
        # =================================================================================
        
        try:
            res = await self.client.post(url, json=body, timeout=20)
            res.raise_for_status()
            data = res.json()
            
            if not data or not data.get('parcels'):
                return TrackingResponse(status='AWB Necunoscut', date=None)

            last_op = (data['parcels'][0].get('operations') or [{}])[-1]
            last_desc = (last_op.get('description') or 'N/A').strip()
            date_str = last_op.get('date')
            last_dt = datetime.fromisoformat(date_str.replace('Z', '+00:00')) if date_str else None
            
            return TrackingResponse(status=last_desc, date=last_dt, raw_data=data)
        except Exception as e:
            logging.error(f"Excepție la tracking DPD pentru AWB {awb}: {e}")
            return TrackingResponse(status='Eroare Tracking', date=None)

    async def get_label(self, awb: str, creds: dict, paper_size: str) -> bytes:
        # Funcția ta existentă pentru printare etichetă rămâne aici
        pass