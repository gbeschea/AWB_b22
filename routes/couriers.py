# routes/couriers.py

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
import json
from typing import List

from database import get_db
from dependencies import get_templates
from crud import couriers as crud_couriers
import models
import schemas
from services.couriers import get_courier_service

# Router pentru pagina de setări
settings_router = APIRouter(prefix='/settings/couriers', tags=['Settings - Couriers'])
# Router pentru endpoint-urile de date (ex: servicii DPD)
data_router = APIRouter(prefix='/couriers', tags=['Couriers Data'])


DEFAULT_TRACKING_URLS = {
    "dpd": "https://tracking.dpd.ro/?shipmentNumber={awb}",
    "sameday": "https://sameday.ro/#awb={awb}",
    "econt": "https://www.econt.com/en/services/track-shipment/{awb}"
}

async def _parse_credentials_from_form(form_data: dict) -> dict:
    credentials = {}
    for key, value in form_data.items():
        if key.startswith('cred_') and value:
            cred_key = key[5:]
            credentials[cred_key] = True if value == 'on' else value
    if form_data.get('courier_type') == 'econt' and 'test_mode' not in credentials:
        credentials['test_mode'] = False
    return credentials

@settings_router.get('', response_class=HTMLResponse, name="get_couriers_page")
async def get_couriers_page(request: Request, db: AsyncSession = Depends(get_db), templates=Depends(get_templates)):
    accounts = await crud_couriers.get_courier_accounts(db)
    mappings = await crud_couriers.get_courier_mappings(db)
    context = {
        "request": request, "accounts": accounts, "mappings": mappings,
        "page_title": "Setări Curieri", "default_tracking_urls": DEFAULT_TRACKING_URLS
    }
    return templates.TemplateResponse("settings_couriers.html", context)

@settings_router.post('/accounts', response_class=RedirectResponse, name="create_courier_account")
async def create_account(request: Request, db: AsyncSession = Depends(get_db)):
    form_data = await request.form()
    form_dict = dict(form_data)
    credentials = await _parse_credentials_from_form(form_dict)
    await crud_couriers.create_courier_account(
        db=db, name=form_dict.get('name'), account_key=form_dict.get('account_key'),
        courier_type=form_dict.get('courier_type'), tracking_url=form_dict.get('tracking_url'),
        credentials=credentials, is_active=form_dict.get('is_active') == 'on'
    )
    return RedirectResponse(url=settings_router.url_path_for("get_couriers_page"), status_code=303)

@settings_router.post('/accounts/{account_id}/update', response_class=RedirectResponse, name="update_courier_account")
async def update_account(account_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    form_data = await request.form()
    form_dict = dict(form_data)
    account = await db.get(models.CourierAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Contul nu a fost găsit.")
    existing_credentials = account.credentials or {}
    new_credentials = await _parse_credentials_from_form(form_dict)
    for key in list(new_credentials.keys()):
        if ('password' in key or 'secret' in key) and not new_credentials[key]:
            del new_credentials[key]
    updated_credentials = {**existing_credentials, **new_credentials}
    await crud_couriers.update_courier_account(
        db=db, account_id=account_id, name=form_dict.get('name'),
        account_key=form_dict.get('account_key'), courier_type=form_dict.get('courier_type'),
        tracking_url=form_dict.get('tracking_url'), credentials=updated_credentials,
        is_active=form_dict.get('is_active') == 'on'
    )
    return RedirectResponse(url=settings_router.url_path_for("get_couriers_page"), status_code=303)

@settings_router.post('/mappings', response_class=RedirectResponse, name="create_courier_mapping")
async def create_mapping(db: AsyncSession = Depends(get_db), shopify_name: str = Form(), account_key: str = Form()):
    await crud_couriers.create_courier_mapping(db, shopify_name, account_key)
    return RedirectResponse(url=settings_router.url_path_for("get_couriers_page"), status_code=303)

@data_router.get("/dpd/services", response_model=List[schemas.DpdServiceOption])
async def get_dpd_services_for_order(order_id: int, db: AsyncSession = Depends(get_db)):
    order = await db.get(models.Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Comanda nu a fost găsită.")
    try:
        dpd_service = get_courier_service("dpd-ro")
        if hasattr(dpd_service, "get_available_services"):
            services = await dpd_service.get_available_services(db, order, "dpd-ro")
            return services
        return []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))