
from fastapi import APIRouter, Depends, HTTPException, Form
from fastapi.responses import RedirectResponse, Response
from typing import List, Optional
from pydantic import BaseModel

import models
from database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

# Routers
html_router = APIRouter(tags=["Shipment Profiles HTML"])
api_router = APIRouter(prefix="/api", tags=["Shipment Profiles API"])

# --- Pydantic models (API validation) ---

class ShipmentProfileBase(BaseModel):
    name: str
    account_key: str
    parcels: int = 1
    weight_kg: float = 1.0
    service: Optional[str] = None
    shipment_content: Optional[str] = None
    observations: Optional[str] = None

class ShipmentProfileCreate(ShipmentProfileBase):
    pass

class ShipmentProfileUpdate(ShipmentProfileBase):
    pass

class ShipmentProfile(ShipmentProfileBase):
    id: int
    class Config:
        from_attributes = True

# ===================================================================
# --- HTML FORM ROUTES (from settings page) ---
# ===================================================================

@html_router.post("/settings/profiles/create", name="create_shipment_profile")
async def handle_create_shipment_profile_form(
    db: AsyncSession = Depends(get_db),
    name: str = Form(...),
    account_key: str = Form(...),
    # Numele din formular pot rămâne simple
    default_parcels: int = Form(1),
    default_weight_kg: float = Form(1.0),
    default_length_cm: Optional[int] = Form(None),
    default_width_cm: Optional[int] = Form(None),
    default_height_cm: Optional[int] = Form(None),
    default_service_id: Optional[int] = Form(None),
    content_template: Optional[str] = Form(None)
):
    """
    Gestionează crearea unui nou profil de expediție din formularul de setări.
    """
    # Verificăm dacă numele există deja (codul acesta era corect)
    result = await db.execute(select(models.ShipmentProfile).filter_by(name=name))
    existing_profile = result.scalar_one_or_none()

    if existing_profile:
        raise HTTPException(status_code=409, detail=f"Un profil cu numele '{name}' există deja.")

    # --- AICI ESTE CORECȚIA PRINCIPALĂ ---
    # Folosim numele CORECTE ale coloanelor din models.py
    new_profile = models.ShipmentProfile(
        name=name,
        account_key=account_key,
        default_parcels=default_parcels,
        default_weight_kg=default_weight_kg,
        default_length_cm=default_length_cm,
        default_width_cm=default_width_cm,
        default_height_cm=default_height_cm,
        default_service_id=default_service_id,
        content_template=content_template
    )
    # ------------------------------------

    db.add(new_profile)
    await db.commit()

    return RedirectResponse(url="/settings/couriers#profiles", status_code=303)


# ===================================================================
# --- JSON API ROUTES (used by JS) ---
# ===================================================================

@api_router.get("/profiles", response_model=List[ShipmentProfile])
async def get_all_shipment_profiles_api(db: AsyncSession = Depends(get_db)):
    """Return all profiles ordered by name."""
    result = await db.execute(
        select(models.ShipmentProfile).order_by(models.ShipmentProfile.name)
    )
    return result.scalars().all()

@api_router.post("/profiles", response_model=ShipmentProfile, status_code=201)
async def create_shipment_profile_api(profile: ShipmentProfileCreate, db: AsyncSession = Depends(get_db)):
    """Create a new profile via API."""
    # Duplicate name check
    result = await db.execute(
        select(models.ShipmentProfile).filter_by(name=profile.name)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Un profil cu numele '{profile.name}' există deja.")

    db_profile = models.ShipmentProfile(**profile.model_dump())
    db.add(db_profile)
    await db.commit()
    await db.refresh(db_profile)
    return db_profile

@api_router.put("/profiles/{profile_id}", response_model=ShipmentProfile)
async def update_shipment_profile_api(profile_id: int, profile: ShipmentProfileUpdate, db: AsyncSession = Depends(get_db)):
    """Update an existing profile via API."""
    db_profile = await db.get(models.ShipmentProfile, profile_id)
    if not db_profile:
        raise HTTPException(status_code=404, detail="Profilul nu a fost găsit.")

    for key, value in profile.model_dump(exclude_unset=True).items():
        setattr(db_profile, key, value)

    await db.commit()
    await db.refresh(db_profile)
    return db_profile

@api_router.delete("/profiles/{profile_id}", status_code=204)
async def delete_shipment_profile_api(profile_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a profile via API."""
    db_profile = await db.get(models.ShipmentProfile, profile_id)
    if not db_profile:
        raise HTTPException(status_code=404, detail="Profilul nu a fost găsit.")

    await db.delete(db_profile)
    await db.commit()
    return Response(status_code=204)
