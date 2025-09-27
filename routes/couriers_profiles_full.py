
# routes/couriers_profiles_full.py (with edit & delete)
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from typing import Optional, List, Dict, Any

import models
from database import get_db
from templating import templates
import crud.couriers as crud

settings_router = APIRouter(prefix="/settings/couriers", tags=["Settings - Couriers"])

def _rows_to_dicts(rows) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        m = getattr(r, "_mapping", None)
        if m is not None:
            out.append(dict(m))
        else:
            # ORM instance
            out.append({
                "id": r.id,
                "name": getattr(r, "name", None),
                "account_key": getattr(r, "account_key", None)
            })
    return out

@settings_router.get("", name="get_couriers_page")
async def get_couriers_settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    accounts = await crud.get_courier_accounts(db)
    mappings = await crud.get_courier_mappings(db)

    # 1) ORM
    orm_result = await db.execute(select(models.ShipmentProfile).order_by(models.ShipmentProfile.id))
    orm_profiles = orm_result.scalars().all()

    # 2) COUNT direct din SQL
    count_profiles = (await db.execute(text("SELECT COUNT(*) FROM shipment_profiles"))).scalar()

    # 3) Fallback: dacă ORM listează 0 dar COUNT>0, citim direct prin SQL în dict
    if not orm_profiles and count_profiles:
        sql_rows = await db.execute(text("SELECT id, name, account_key FROM shipment_profiles ORDER BY id"))
        shipment_profiles = _rows_to_dicts(sql_rows)
    else:
        shipment_profiles = _rows_to_dicts(orm_profiles)

    context = {
        "request": request,
        "accounts": accounts,
        "mappings": mappings,
        "shipment_profiles": shipment_profiles,
        # Debug vizibil în pagină
        "debug_profiles_count": count_profiles,
        "debug_len": len(shipment_profiles),
        "debug_first": shipment_profiles[0] if shipment_profiles else None,
    }
    return templates.TemplateResponse("settings_couriers.html", context)

# -------------------- CREATE --------------------
@settings_router.post("/profiles/create", name="create_shipment_profile")
async def create_shipment_profile(
    db: AsyncSession = Depends(get_db),
    name: str = Form(...),
    account_key: str = Form(...),
    default_parcels: int = Form(1),
    default_weight_kg: float = Form(1.0),
    default_length_cm: Optional[int] = Form(None),
    default_width_cm: Optional[int] = Form(None),
    default_height_cm: Optional[int] = Form(None),
    default_service_id: Optional[int] = Form(None),
    content_template: Optional[str] = Form('${orderName} / ${quantity} x ${sku}'),
):
    exists = await db.execute(select(models.ShipmentProfile).filter_by(name=name))
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Un profil cu numele '{name}' există deja.")

    profile = models.ShipmentProfile(
        name=name,
        account_key=account_key,
        default_parcels=default_parcels,
        default_weight_kg=default_weight_kg,
        default_length_cm=default_length_cm,
        default_width_cm=default_width_cm,
        default_height_cm=default_height_cm,
        default_service_id=default_service_id,
        content_template=content_template,
    )
    db.add(profile)
    await db.commit()
    return RedirectResponse(url="/settings/couriers#profiles", status_code=303)

# -------------------- EDIT PAGE --------------------
@settings_router.get("/profiles/{profile_id}/edit", name="edit_shipment_profile_page")
async def edit_shipment_profile_page(profile_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    profile = await db.get(models.ShipmentProfile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profilul nu a fost găsit.")
    accounts = await crud.get_courier_accounts(db)
    return templates.TemplateResponse("settings_profiles_edit.html", {
        "request": request,
        "profile": profile,
        "accounts": accounts,
    })

# -------------------- UPDATE --------------------
@settings_router.post("/profiles/{profile_id}/edit", name="update_shipment_profile")
async def update_shipment_profile(profile_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    profile = await db.get(models.ShipmentProfile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profilul nu a fost găsit.")
    form = await request.form()

    def _to_int(v):
        try:
            return int(v) if v not in (None, "", "None") else None
        except Exception:
            return None

    def _to_float(v):
        try:
            return float(v) if v not in (None, "", "None") else None
        except Exception:
            return None

    profile.name = form.get("name") or profile.name
    profile.account_key = form.get("account_key") or profile.account_key
    profile.default_parcels = _to_int(form.get("default_parcels")) or profile.default_parcels
    profile.default_weight_kg = _to_float(form.get("default_weight_kg")) or profile.default_weight_kg
    profile.default_length_cm = _to_int(form.get("default_length_cm"))
    profile.default_width_cm  = _to_int(form.get("default_width_cm"))
    profile.default_height_cm = _to_int(form.get("default_height_cm"))
    profile.default_service_id = _to_int(form.get("default_service_id"))
    profile.content_template = form.get("content_template") or profile.content_template

    await db.commit()
    return RedirectResponse(url="/settings/couriers#profiles", status_code=303)

# -------------------- DELETE --------------------
@settings_router.post("/profiles/{profile_id}/delete", name="delete_shipment_profile")
async def delete_shipment_profile(profile_id: int, db: AsyncSession = Depends(get_db)):
    profile = await db.get(models.ShipmentProfile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profilul nu a fost găsit.")
    try:
        await db.delete(profile)
        await db.commit()
    except IntegrityError:
        # FK constraint (ex: orders.assigned_profile_id) — nu ștergem
        return RedirectResponse("/settings/couriers#profiles?error=profil_in_folosinta", status_code=303)
    return RedirectResponse("/settings/couriers#profiles", status_code=303)
