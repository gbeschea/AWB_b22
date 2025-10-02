# routes/financials.py

import logging
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
# MODIFICARE: Folosim selectinload, o metodă mai robustă pentru încărcarea relațiilor
from sqlalchemy.orm import selectinload 
from typing import List
from datetime import datetime, timezone

from database import get_db, AsyncSessionLocal 
from models import Order, Shipment
from services import sync_service, courier_service, shopify_service

templates = Jinja2Templates(directory="templates")
router = APIRouter()
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


@router.get("/financials", response_class=HTMLResponse)
async def get_financials_page(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Afișează pagina Financiar cu comenzile relevante.
    """
    # =================================================================
    # MODIFICARE FINALĂ PENTRU QUERY:
    # 1. Folosim selectinload() pentru a evita duplicatele cauzate de join.
    # 2. Folosim func.lower() pentru a garanta o filtrare corectă, insensibilă la majuscule.
    # =================================================================
    query = (
        select(Order)
        .options(selectinload(Order.shipments))
        .where(
            func.lower(Order.shopify_status) == 'fulfilled',
            func.lower(Order.financial_status) == 'pending'
        )
        .order_by(Order.created_at.desc())
        .limit(250)
    )
    result = await db.execute(query)
    # Cu selectinload, .unique() nu mai este necesar, dar îl lăsăm ca o siguranță suplimentară.
    orders = result.scalars().unique().all()
    
    couriers_query = select(Shipment.courier).distinct()
    couriers_result = await db.execute(couriers_query)
    couriers = [c[0] for c in couriers_result if c[0]]

    return templates.TemplateResponse(
        "financials.html", 
        {"request": request, "orders": orders, "couriers": couriers}
    )

# Restul fișierului rămâne neschimbat
@router.post("/financials/sync-range")
async def sync_date_range(
    store_ids: List[int] = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...)
):
    logger.info(f"Primită cerere de sincronizare pentru magazinele {store_ids} între {start_date} și {end_date}")
    try:
        start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00")).replace(hour=23, minute=59, second=59)
    except ValueError:
        raise HTTPException(status_code=400, detail="Formatul datei este invalid. Folosiți YYYY-MM-DD.")
    try:
        await sync_service.sync_orders_for_stores(
            store_ids=store_ids,
            created_at_min=start_dt,
            created_at_max=end_dt
        )
        logger.info("Sincronizarea comenzilor finalizată. Se pornește sincronizarea statusurilor de la curieri...")
        async with AsyncSessionLocal() as db:
            if hasattr(courier_service, 'track_and_update_shipments'):
                 await courier_service.track_and_update_shipments(db, full_sync=False)
            else:
                logger.warning("Funcția 'track_and_update_shipments' nu a fost găsită în courier_service.")
        return JSONResponse({"status": "success", "message": "Sincronizare finalizată."})
    except Exception as e:
        logger.error(f"Eroare în endpoint-ul sync_date_range: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="A apărut o eroare internă în timpul sincronizării.")


@router.post("/financials/mark-as-paid")
async def mark_orders_as_paid(
    order_ids: List[int] = Form(...), 
    db: AsyncSession = Depends(get_db)
):
    success_orders = []
    failed_orders = []
    logger.info(f"Primită cerere de marcare ca plătite pentru {len(order_ids)} comenzi.")
    for order_id in order_ids:
        order = await db.get(Order, order_id)
        if order and order.store:
            is_cod = 'cash_on_delivery' in (order.mapped_payment or '')
            is_pending = (order.financial_status or '').lower() == 'pending'
            if is_cod and is_pending:
                try:
                    await shopify_service.capture_payment(db, order.store_id, order.shopify_order_id)
                    order.financial_status = 'paid'
                    db.add(order)
                    success_orders.append(order.name)
                    logger.info(f"Comanda {order.name} a fost marcată ca plătită cu succes.")
                except Exception as e:
                    logger.error(f"Eroare la marcarea comenzii {order.name} ca plătită: {e}", exc_info=True)
                    failed_orders.append({"name": order.name, "error": str(e)})
            else:
                 msg = f"Comanda {order.name} a fost omisă. Motiv: Nu este ramburs ('{order.mapped_payment}') sau statusul nu este 'pending' ('{order.financial_status}')."
                 logger.warning(msg)
                 failed_orders.append({"name": order.name, "error": msg})
    if success_orders:
        await db.commit()
    return JSONResponse({
        "status": "success",
        "message": f"Operațiune finalizată. Comenzi marcate ca plătite: {len(success_orders)}.",
        "success_orders": success_orders,
        "failed_orders": failed_orders
    })