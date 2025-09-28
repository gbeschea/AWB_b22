from fastapi import APIRouter, Depends, HTTPException
from typing import List, Optional
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

import models
from database import get_db

# Routers
html_router = APIRouter(tags=["Shipment Profiles HTML"])

# === AICI ESTE CORECȚIA ESENȚIALĂ ===
# Setăm prefixul complet și corect, pentru a se potrivi cu apelul din JavaScript: /api/profiles/{id}
api_router = APIRouter(prefix="/api/profiles", tags=["Shipment Profiles API"])

# --- Pydantic models (API validation) ---

class ShipmentProfile(BaseModel):
    id: int
    name: str
    account_key: str
    default_parcels: int
    default_weight_kg: float
    default_payer: Optional[str] = 'SENDER'
    default_service_id: Optional[int] = None
    content_template: Optional[str] = None


    class Config:
        from_attributes = True

# --- Funcții Helper ---

async def get_shipment_profile(db: AsyncSession, profile_id: int):
    profile = await db.get(models.ShipmentProfile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profilul nu a fost găsit.")
    return profile

# --- Rute API ---

# Calea URL pentru acest endpoint va fi: GET /api/profiles/{profile_id}
@api_router.get("/{profile_id}", response_model=ShipmentProfile)
async def get_shipment_profile_api(profile_id: int, db: AsyncSession = Depends(get_db)):
    """Returnează detaliile unui singur profil în format JSON."""
    return await get_shipment_profile(db, profile_id)