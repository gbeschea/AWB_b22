# services/couriers/common.py

from pydantic import BaseModel
from typing import Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import models

# --- Clasa ta existentă (rămâne neschimbată) ---
class TrackingStatus(BaseModel):
    raw_status: str
    details: Optional[str] = None
    delivered: bool = False
    refused: bool = False
    canceled: bool = False
    raw_response: Optional[Dict[str, Any]] = None
    derived_status: Optional[str] = None

# --- Funcția lipsă (pe care o adăugăm acum) ---
async def get_courier_account_by_key(db: AsyncSession, account_key: str) -> Optional[models.CourierAccount]:
    """
    Preia detaliile unui cont de curier din baza de date după cheia unică (ex: 'dpd-ro').
    """
    stmt = select(models.CourierAccount).where(models.CourierAccount.account_key == account_key)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()