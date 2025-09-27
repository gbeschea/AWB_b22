# /routes/couriers.py

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional

import models
import schemas
import crud.couriers as crud
from database import get_db
from templating import templates
from services.couriers.dpd import DPDCourier as DpdService
import settings

from sqlalchemy import select, text
from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError



# Router pentru pagina de setări (formulare HTML)
settings_router = APIRouter(prefix='/settings/couriers', tags=['Settings - Couriers'])
# Router pentru endpoint-urile de date (API pentru JavaScript)
data_router = APIRouter(prefix='/api/couriers', tags=['Couriers Data API'])


@settings_router.get("", name="get_couriers_page")
async def get_couriers_settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    accounts = await crud.get_courier_accounts(db)
    mappings = await crud.get_courier_mappings(db)

    # ORM -> modele
    result = await db.execute(select(models.ShipmentProfile).order_by(models.ShipmentProfile.id))
    orm_profiles = result.scalars().all()  # are elemente (ai confirmat)

    # Convertim în listă de dict-uri 100% Jinja-friendly
    shipment_profiles_rows = [
        {"id": p.id, "name": p.name, "account_key": p.account_key}
        for p in orm_profiles
    ]

    # (opțional) debug direct din SQL – lăsat provizoriu
    count_profiles = (await db.execute(text("SELECT COUNT(*) FROM shipment_profiles"))).scalar()

    return templates.TemplateResponse("settings_couriers.html", {
        "request": request,
        "accounts": accounts,
        "mappings": mappings,
        "shipment_profiles": shipment_profiles_rows,  # <-- acum e listă de dict-uri
        "debug_profiles_count": count_profiles,
        "debug_len": len(shipment_profiles_rows),
    })




@settings_router.post("/accounts/create", name="create_courier_account")
async def handle_create_courier_account_form(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Gestionează crearea unui cont nou din formularul complet.
    Citește formularul direct din request pentru a gestiona dinamic toate câmpurile.
    """
    form_data = await request.form()
    
    credentials_dict = {
        "sender_address": {
            "contact_person": form_data.get("contact_name"),
            "phone": form_data.get("phone"),
            "email": form_data.get("email"),
            "street": form_data.get("address_line1"),
            "city": form_data.get("city"),
            "county": form_data.get("county"),
            "postal_code": form_data.get("postcode")
        },
        "api": {}
    }

    courier_type = form_data.get("courier_type")

    # --- Logica completă pentru fiecare curier ---
    if courier_type == 'dpd':
        credentials_dict["api"] = {
            "username": form_data.get("dpd_username"),
            "password": form_data.get("dpd_password"),
            "client_id": form_data.get("dpd_client_id")
        }
    elif courier_type == 'sameday':
        credentials_dict["api"] = {
            "username": form_data.get("sameday_username"),
            "password": form_data.get("sameday_password")
        }
    elif courier_type == 'econt':
        credentials_dict["api"] = {
            "username": form_data.get("econt_username"),
            "password": form_data.get("econt_password")
        }
    
    account_key = form_data.get("account_key")
    existing_account = await crud.get_courier_account_by_key(db, account_key)
    if existing_account:
        raise HTTPException(status_code=409, detail=f"Cheia de cont '{account_key}' există deja.")

    await crud.create_courier_account(
        db=db,
        name=form_data.get("name"),
        account_key=account_key,
        courier_type=courier_type,
        credentials_dict=credentials_dict
    )
    
    return RedirectResponse(url=settings_router.url_path_for("get_couriers_settings_page"), status_code=303)


@settings_router.post('/mappings/create', name="create_courier_mapping")
async def create_mapping(db: AsyncSession = Depends(get_db), shopify_name: str = Form(), account_key: str = Form()):
    await crud.create_courier_mapping(db, shopify_name, account_key)
    return RedirectResponse(url=settings_router.url_path_for("get_couriers_settings_page"), status_code=303)


# --- RUTA PENTRU DATE API (PENTRU JAVASCRIPT) ---

@data_router.post("/dpd/services")
async def get_dpd_services_for_order(
    order_id: int = Form(...),
    account_key: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Endpoint API pe care îl va apela JavaScript-ul pentru a obține serviciile DPD.
    """
    order = await db.get(models.Order, order_id)
    account = await crud.get_courier_account_by_key(db, account_key)
    
    if not order or not account:
        raise HTTPException(status_code=404, detail="Comanda sau contul nu au fost găsite.")
    if not account.credentials or not account.credentials.get("api"):
        raise HTTPException(status_code=400, detail="Contul de curier nu are credențiale API configurate.")

    try:
        dpd_service = DpdService(account.credentials['api'])
        # Aici ar trebui să ai o metodă care returnează serviciile. 
        # Momentan, returnăm o listă demonstrativă.
        # services = await dpd_service.get_destination_services(...)
        
        # --- Înlocuiește cu apelul real către API-ul DPD ---
        # Listă demonstrativă
        mock_services = [
            {"id": 2505, "name": "DPD Standard"},
            {"id": 2506, "name": "DPD Express"},
            {"id": 2508, "name": "DPD Livrare Sambata"}
        ]
        return mock_services
        # --------------------------------------------------
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Eroare la comunicarea cu API-ul DPD: {str(e)}")
    
@settings_router.get("/accounts/{account_id}/edit", name="edit_courier_account_page")
async def get_edit_courier_account_page(
    account_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Afișează formularul de editare pre-completat pentru un cont de curier.
    """
    account = await db.get(models.CourierAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Contul de curier nu a fost găsit.")
    
    context = {"request": request, "account": account}
    return templates.TemplateResponse("settings_couriers_edit.html", context)


@settings_router.post("/accounts/{account_id}/edit", name="handle_edit_courier_account_form")
async def handle_edit_courier_account_form(
    account_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Procesează datele din formular și cheamă funcția existentă din CRUD pentru a salva.
    """
    form_data = await request.form()
    
    # Preia contul existent pentru a putea păstra valorile vechi (ex: parola)
    account_to_update = await db.get(models.CourierAccount, account_id)
    if not account_to_update:
        raise HTTPException(status_code=404, detail="Contul de curier nu a fost găsit.")

    # Reconstruim dicționarul de credențiale, păstrând ce e vechi și adăugând ce e nou
    updated_credentials = account_to_update.credentials or {}
    updated_credentials['sender_address'] = {
        "contact_person": form_data.get("contact_name"),
        "phone": form_data.get("phone"), "email": form_data.get("email"),
        "street": form_data.get("address_line1"), "city": form_data.get("city"),
        "county": form_data.get("county"), "postal_code": form_data.get("postcode")
    }
    
    # Actualizează parola doar dacă a fost introdusă una nouă
    new_password = form_data.get("dpd_password")
    if new_password:
        if 'api' not in updated_credentials: updated_credentials['api'] = {}
        updated_credentials['api']['password'] = new_password

    # Apelăm funcția ta existentă din CRUD
    await crud.update_courier_account(
        db=db,
        account_id=account_id,
        name=form_data.get("name"),
        account_key=form_data.get("account_key"),
        courier_type=form_data.get("courier_type"),
        tracking_url=account_to_update.tracking_url,  # Păstrăm tracking_url-ul vechi, deoarece nu e în formular
        credentials_dict=updated_credentials,
        is_active=account_to_update.is_active # Păstrăm statusul de activare
    )
    
    return RedirectResponse(url=settings_router.url_path_for("get_couriers_page"), status_code=303)


@settings_router.get("/profiles/{profile_id}/edit", name="edit_shipment_profile_page")
async def edit_shipment_profile_page(profile_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    profile = await db.get(models.ShipmentProfile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profilul nu a fost găsit.")
    # pentru dropdown-ul de conturi:
    accounts = await crud.get_courier_accounts(db)
    return templates.TemplateResponse("settings_profiles_edit.html", {
        "request": request, "profile": profile, "accounts": accounts
    })

@settings_router.post("/profiles/{profile_id}/edit", name="update_shipment_profile")
async def update_shipment_profile(profile_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    profile = await db.get(models.ShipmentProfile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profilul nu a fost găsit.")
    form = await request.form()

    def _to_int(v): 
        try:
            return int(v) if v not in (None, "", "None") else None
        except: 
            return None
    def _to_float(v):
        try:
            return float(v) if v not in (None, "", "None") else None
        except:
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
        # profilul e folosit (FK în orders.assigned_profile_id) -> redirecționăm cu mesaj
        return RedirectResponse(
            url=settings_router.url_path_for("get_couriers_page") + "#profiles?error=profil_in_folosinta",
            status_code=303
        )
