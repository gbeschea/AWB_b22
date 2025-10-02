from __future__ import annotations

from typing import Any, Dict, Optional, List

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
import models

# Core router for this module
router = APIRouter(tags=["settings"])
# Backward-compat: some apps import `html_router` from routes.profiles
html_router = router  # <-- satisfies app.include_router(profiles.html_router)
# (If you also need an API-only router, you can alias api_router = router)
api_router = router

templates = Jinja2Templates(directory="templates")


# ---------- helpers ----------

async def _all_accounts(db: AsyncSession) -> List[models.CourierAccount]:
    res = await db.execute(select(models.CourierAccount).order_by(models.CourierAccount.name))
    return list(res.scalars().all())

async def _all_profiles(db: AsyncSession) -> List[models.ShipmentProfile]:
    res = await db.execute(select(models.ShipmentProfile).order_by(models.ShipmentProfile.id.desc()))
    return list(res.scalars().all())

async def _get_profile(db: AsyncSession, profile_id: int) -> models.ShipmentProfile:
    res = await db.execute(select(models.ShipmentProfile).where(models.ShipmentProfile.id == profile_id))
    prof = res.scalar_one_or_none()
    if not prof:
        raise HTTPException(status_code=404, detail="Profilul nu există.")
    return prof


# ---------- pages ----------

@router.get("/settings/couriers", name="get_couriers_page")
async def get_couriers_page(request: Request, db: AsyncSession = Depends(get_db)):
    accounts = await _all_accounts(db)
    profiles = await _all_profiles(db)
    # include mappings if you have them; otherwise pass an empty list
    return templates.TemplateResponse("settings_couriers.html", {
        "request": request,
        "accounts": accounts,
        "shipment_profiles": profiles,
        "mappings": [],
    })


@router.get("/settings/couriers/profiles/{profile_id}/edit", name="edit_shipment_profile_page")
async def edit_shipment_profile_page(request: Request, profile_id: int, db: AsyncSession = Depends(get_db)):
    profile = await _get_profile(db, profile_id)
    accounts = await _all_accounts(db)
    return templates.TemplateResponse("settings_profiles_edit.html", {
        "request": request,
        "profile": profile,
        "accounts": accounts,
    })


# ---------- actions ----------

@router.post("/settings/couriers/profiles", name="create_shipment_profile")
async def create_shipment_profile(
    request: Request,
    name: str = Form(...),
    account_key: str = Form(...),
    default_parcels: Optional[int] = Form(None),
    default_weight_kg: Optional[float] = Form(None),
    default_length_cm: Optional[int] = Form(None),
    default_width_cm: Optional[int] = Form(None),
    default_height_cm: Optional[int] = Form(None),
    default_service_id: Optional[int] = Form(None),
    content_template: Optional[str] = Form(None),
    default_packing: Optional[str] = Form(None),   # <-- new
    db: AsyncSession = Depends(get_db),
):
    prof = models.ShipmentProfile(
        name=name.strip(),
        account_key=account_key.strip(),
        default_parcels=default_parcels or 1,
        default_weight_kg=default_weight_kg or 1.0,
        default_length_cm=default_length_cm,
        default_width_cm=default_width_cm,
        default_height_cm=default_height_cm,
        default_service_id=default_service_id,
        content_template=(content_template or "").strip() or None,
        default_packing=(default_packing or "").strip() or None,  # <-- new
    )
    db.add(prof)
    await db.commit()
    return RedirectResponse(url=request.url_for("get_couriers_page") + "#profiles", status_code=303)


@router.post("/settings/couriers/profiles/{profile_id}", name="update_shipment_profile")
async def update_shipment_profile(
    request: Request,
    profile_id: int,
    name: str = Form(...),
    account_key: str = Form(...),
    default_parcels: Optional[int] = Form(None),
    default_weight_kg: Optional[float] = Form(None),
    default_length_cm: Optional[int] = Form(None),
    default_width_cm: Optional[int] = Form(None),
    default_height_cm: Optional[int] = Form(None),
    default_service_id: Optional[int] = Form(None),
    content_template: Optional[str] = Form(None),
    default_packing: Optional[str] = Form(None),   # <-- new
    db: AsyncSession = Depends(get_db),
):
    prof = await _get_profile(db, profile_id)
    prof.name = name.strip()
    prof.account_key = account_key.strip()
    prof.default_parcels = default_parcels or 1
    prof.default_weight_kg = default_weight_kg or 1.0
    prof.default_length_cm = default_length_cm
    prof.default_width_cm = default_width_cm
    prof.default_height_cm = default_height_cm
    prof.default_service_id = default_service_id
    prof.content_template = (content_template or "").strip() or None
    prof.default_packing = (default_packing or "").strip() or None   # <-- new

    await db.commit()
    return RedirectResponse(url=request.url_for("get_couriers_page") + "#profiles", status_code=303)


@router.post("/settings/couriers/profiles/{profile_id}/delete", name="delete_shipment_profile")
async def delete_shipment_profile(request: Request, profile_id: int, db: AsyncSession = Depends(get_db)):
    prof = await _get_profile(db, profile_id)
    await db.delete(prof)
    await db.commit()
    return RedirectResponse(url=request.url_for("get_couriers_page") + "#profiles", status_code=303)
