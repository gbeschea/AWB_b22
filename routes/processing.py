# routes/processing.py

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select, desc
from typing import List
from pydantic import BaseModel

from database import get_db
import models
import schemas
from templating import templates
from services.courier_service import get_courier_service_by_name
import crud.couriers as couriers_crud

router = APIRouter(
    prefix="/processing",
    tags=["Processing"]
)

@router.get("/", response_class=HTMLResponse, name="get_processing_page")
async def get_processing_page(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    processing_condition = (
        models.Order.address_status == 'valid',
        models.Order.processing_status != 'processed'
    )

    stmt = (
        select(models.Order)
        .options(
            selectinload(models.Order.store),
            selectinload(models.Order.line_items)
        )
        .where(*processing_condition)
        .order_by(desc(models.Order.created_at))
    )
    
    result = await db.execute(stmt)
    orders_to_process = result.scalars().all()

    # Apelăm funcțiile din modulul CRUD, unde este locația lor corectă
    shipment_profiles = await couriers_crud.get_all_shipment_profiles(db)
    courier_accounts = await couriers_crud.get_courier_accounts(db)

    context = {
        "request": request,
        "orders": orders_to_process,
        "shipment_profiles": shipment_profiles,
        "courier_accounts": courier_accounts
    }
    return templates.TemplateResponse("processing.html", context)


class AwbCreationPayload(BaseModel):
    order_ids: List[int]
    courier_account_key: str
    options: schemas.AwbCreateOptions

@router.post("/create-awbs", summary="Creează AWB-uri pentru una sau mai multe comenzi")
async def create_awbs_endpoint(
    payload: AwbCreationPayload,
    db: AsyncSession = Depends(get_db)
):
    success_count = 0
    error_count = 0
    errors = []

    courier_type = payload.courier_account_key.split('-')[0]

    for order_id in payload.order_ids:
        order_name_for_error = f"ID {order_id}"
        try:
            order = await db.get(models.Order, order_id, options=[selectinload(models.Order.line_items)])
            if not order:
                raise ValueError(f"Comanda cu ID {order_id} nu a fost găsită.")
            
            order_name_for_error = order.name

            if order.processing_status == 'processed':
                raise ValueError(f"Comanda {order.name} are deja un AWB generat.")

            courier_service = get_courier_service_by_name(courier_type)
            
            awb_result = await courier_service.create_awb(
                db=db,
                order=order,
                account_key=payload.courier_account_key,
                options=payload.options
            )

            if awb_result and getattr(awb_result, 'success', False):
                success_count += 1
                order.processing_status = 'processed'
                db.add(order)
            else:
                error_message = getattr(awb_result, 'error_message', 'Eroare necunoscută de la curier.')
                raise ValueError(error_message)

        except Exception as e:
            error_count += 1
            errors.append(f"Comanda {order_name_for_error}: {str(e)}")
            
    await db.commit()

    return {
        "message": "Procesare finalizată.",
        "success_count": success_count,
        "error_count": error_count,
        "errors": errors
    }