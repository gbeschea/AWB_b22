# routes/couriers.py
from __future__ import annotations

import json
import logging
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import models
import crud.couriers as crud
from crud.couriers import upsert_courier_account
from database import get_db
from templating import templates
from services.couriers.dpd import DPDCourier as DpdService
import settings

SUPPORTED_COURIER_TYPES = ("dpd", "sameday", "packeta", "econt")

HUMAN_COURIER_LABELS = {
    "dpd": "DPD",
    "sameday": "Sameday",
    "packeta": "Packeta",
    "econt": "Econt",
}



# Router pentru pagină setări (HTML) și API (JS)
settings_router = APIRouter(prefix="/settings/couriers", tags=["Settings - Couriers"])
data_router = APIRouter(prefix="/api/couriers", tags=["Couriers Data API"])

logger = logging.getLogger(__name__)


def _parse_credentials_from_form(form: Dict[str, Any]) -> Dict[str, Any]:
    """
    Acceptă fie 'credentials' ca JSON, fie câmpuri simple:
      password, api_password, token, api_key, base_url, accept_language, username, client_id.
    Le pune în structura compatibilă cu servicii: { "api": {...}, "base_url": "...", ... }.
    """
    creds: Dict[str, Any] = {}

    # dacă avem blob JSON
    raw = form.get("credentials")
    if raw:
        try:
            creds = json.loads(raw)
            if not isinstance(creds, dict):
                creds = {}
        except Exception:
            creds = {}

    # normalize simple fields
    def put_api(k: str, v: Optional[str]):
        if v:
            creds.setdefault("api", {})[k] = v

    def put_root(k: str, v: Optional[str]):
        if v:
            creds[k] = v

    put_api("username", (form.get("username") or "").strip())
    put_api("password", (form.get("password") or "").strip())
    put_api("client_id", (form.get("client_id") or "").strip())
    put_api("api_key", (form.get("api_key") or "").strip())
    put_api("api_password", (form.get("api_password") or "").strip())
    put_api("token", (form.get("token") or "").strip())
    put_api("secret", (form.get("secret") or "").strip())
    put_api("api_token", (form.get("api_token") or "").strip())

    put_root("base_url", (form.get("base_url") or "").strip())
    put_root("accept_language", (form.get("accept_language") or "").strip())

    return creds


@settings_router.get("", name="get_couriers_page")
async def get_couriers_settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    accounts = await crud.get_courier_accounts(db)
    mappings = await crud.get_courier_mappings(db)

    result = await db.execute(select(models.ShipmentProfile).order_by(models.ShipmentProfile.id))
    orm_profiles = result.scalars().all()

    shipment_profiles_rows = [
        {"id": p.id, "name": p.name, "account_key": p.account_key}
        for p in orm_profiles
    ]
    count_profiles = (await db.execute(text("SELECT COUNT(*) FROM shipment_profiles"))).scalar()

    return templates.TemplateResponse(
        "settings_couriers.html",
        {
            "request": request,
            "accounts": accounts,
            "mappings": mappings,
            "shipment_profiles": shipment_profiles_rows,
            "debug_profiles_count": count_profiles,
            "debug_len": len(shipment_profiles_rows),
        },
    )


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


@settings_router.post("/accounts/create", name="create_courier_account")
async def handle_create_courier_account_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Creează sau actualizează (upsert) un cont. Elimină 409.
    """
    form = {k: (v if isinstance(v, str) else v[0]) for k, v in (await request.form()).multi_items()}
    name = (form.get("name") or "").strip()
    account_key = (form.get("account_key") or "").strip()
    courier_type = (form.get("courier_type") or "").strip().lower()
    tracking_url = (form.get("tracking_url") or "").strip() or None
    is_active = (form.get("is_active") or "true").lower() not in {"0", "false", "off"}

    if not name or not account_key or not courier_type:
        raise HTTPException(status_code=400, detail="name, account_key și courier_type sunt obligatorii.")

    credentials = _parse_credentials_from_form(form)

    try:
        await upsert_courier_account(
            db,
            account_key=account_key,
            name=name,
            courier_type=courier_type,
            tracking_url=tracking_url,
            credentials=credentials,
            is_active=is_active,
        )
    except IntegrityError as e:
        await db.rollback()
        return JSONResponse({"error": "duplicate or invalid data", "detail": str(e)}, status_code=400)

    return RedirectResponse(url=settings_router.url_path_for("get_couriers_page"), status_code=303)


@settings_router.post("/mappings/create", name="create_courier_mapping")
async def create_mapping(
    db: AsyncSession = Depends(get_db),
    shopify_name: str = Form(...),
    account_key: str = Form(...),
):
    await crud.create_courier_mapping(db, shopify_name, account_key)
    return RedirectResponse(url=settings_router.url_path_for("get_couriers_page"), status_code=303)


# --------- API pentru JS ---------

@data_router.post("/dpd/services")
async def get_dpd_services_for_order(
    order_id: int = Form(...),
    account_key: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    order = await db.get(models.Order, order_id)
    account = await crud.get_courier_account_by_key(db, account_key)

    if not order or not account:
        raise HTTPException(status_code=404, detail="Comanda sau contul nu au fost găsite.")

    creds = account.credentials or {}
    api_creds = creds.get("api") or creds  # fallback dacă nu e încapsulat
    if not isinstance(api_creds, dict) or not api_creds:
        raise HTTPException(status_code=400, detail="Contul nu are credențiale API configurate.")

    try:
        dpd_service = DpdService(api_creds)
        # TODO: înlocuiește cu apelul real la API
        mock_services = [
            {"id": 2505, "name": "DPD Standard"},
            {"id": 2506, "name": "DPD Express"},
            {"id": 2508, "name": "DPD Livrare Sambata"},
        ]
        return mock_services
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Eroare la comunicarea cu API-ul DPD: {str(e)}")


@settings_router.get("/accounts/{account_id}/edit", name="edit_courier_account_page")
async def get_edit_courier_account_page(
    account_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    account = await db.get(models.CourierAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Contul de curier nu a fost găsit.")
    return templates.TemplateResponse("settings_couriers_edit.html", {"request": request, "account": account})


@settings_router.post("/accounts/{account_id}/edit", name="handle_edit_courier_account_form")
async def handle_edit_courier_account_form(
    account_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(models.CourierAccount).where(models.CourierAccount.id == account_id))
    account = res.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Contul de curier nu a fost găsit.")

    form = await request.form()
    courier_type = (form.get("courier_type") or "").strip().lower()
    name = (form.get("name") or "").strip()
    account_key = (form.get("account_key") or "").strip()

    updated_credentials = dict(account.credentials or {})
    sender_address = {
        "contact_person": (form.get("contact_name") or "").strip(),
        "phone": (form.get("phone") or "").strip(),
        "email": (form.get("email") or "").strip(),
        "street": (form.get("address_line1") or "").strip(),
        "city": (form.get("city") or "").strip(),
        "county": (form.get("county") or "").strip(),
        "postal_code": (form.get("postcode") or "").strip(),
    }
    updated_credentials["sender_address"] = sender_address

    api = dict(updated_credentials.get("api") or {})

    if courier_type == "packeta":
        for k in ("username", "client_id", "password", "token", "secret"):
            api.pop(k, None)
        pk = (form.get("packeta_api_key") or "").strip()
        pw = (form.get("packeta_api_password") or "").strip()
        bu = (form.get("packeta_base_url") or "").strip()
        if pk:
            api["api_key"] = pk
        if pw:
            api["api_password"] = pw
        if bu:
            updated_credentials["base_url"] = bu

    elif courier_type == "dpd":
        for k in ("api_key", "api_password"):
            api.pop(k, None)
        user = (form.get("dpd_username") or "").strip()
        pw = (form.get("dpd_password") or "").strip()
        cid = (form.get("dpd_client_id") or "").strip()
        bu = (form.get("dpd_base_url") or "").strip()
        if user:
            api["username"] = user
        if pw:
            api["password"] = pw
        if cid:
            api["client_id"] = cid
        if bu:
            updated_credentials["base_url"] = bu

    elif courier_type == "sameday":
        user = (form.get("sameday_username") or "").strip()
        pw = (form.get("sameday_password") or "").strip()
        cid = (form.get("sameday_client_id") or "").strip()
        bu = (form.get("sameday_base_url") or "").strip()
        if user:
            api["username"] = user
        if pw:
            api["password"] = pw
        if cid:
            api["client_id"] = cid
        if bu:
            updated_credentials["base_url"] = bu

    updated_credentials["api"] = api

    account.name = name or account.name
    account.account_key = account_key or account.account_key
    account.courier_type = courier_type or account.courier_type
    account.credentials = updated_credentials
    await db.commit()

    return RedirectResponse(url=settings_router.url_path_for("get_couriers_page"), status_code=303)


@settings_router.get("/profiles/{profile_id}/edit", name="edit_shipment_profile_page")
async def edit_shipment_profile_page(profile_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    profile = await db.get(models.ShipmentProfile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profilul nu a fost găsit.")
    accounts = await crud.get_courier_accounts(db)
    return templates.TemplateResponse(
        "settings_profiles_edit.html", {"request": request, "profile": profile, "accounts": accounts}
    )


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
    profile.default_width_cm = _to_int(form.get("default_width_cm"))
    profile.default_height_cm = _to_int(form.get("default_height_cm"))
    profile.default_service_id = _to_int(form.get("default_service_id"))
    profile.content_template = form.get("content_template") or profile.content_template

    await db.commit()
    return RedirectResponse(url=settings_router.url_path_for("get_couriers_page") + "#profiles", status_code=303)


@settings_router.post("/profiles/{profile_id}/delete", name="delete_shipment_profile")
async def delete_shipment_profile(profile_id: int, db: AsyncSession = Depends(get_db)):
    profile = await db.get(models.ShipmentProfile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profilul nu a fost găsit.")
    try:
        await db.delete(profile)
        await db.commit()
        return RedirectResponse(url=settings_router.url_path_for("get_couriers_page") + "#profiles", status_code=303)
    except IntegrityError:
        return RedirectResponse(
            url=settings_router.url_path_for("get_couriers_page") + "#profiles?error=profil_in_folosinta",
            status_code=303,
        )
