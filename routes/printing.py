# routes/printing.py
import logging
import math
from pathlib import Path
from datetime import datetime, timezone
from typing import List

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTasks

import models
from database import get_db
from services import print_service
from routes.background import update_shopify_in_background  # <- use the routes version
from dependencies import get_templates
from settings import settings

router = APIRouter(tags=['Print View'])

@router.get("/print-view", response_class=HTMLResponse, name="get_print_view_page")
async def get_print_view_page(request: Request, db: AsyncSession = Depends(get_db), templates: Jinja2Templates = Depends(get_templates)):
    latest_shipment_subq = (select(models.Shipment.order_id, func.max(models.Shipment.id).label("max_id")).group_by(models.Shipment.order_id).alias("latest_shipment_subq"))
    supported_couriers_filter = or_(models.Shipment.courier.ilike('%dpd%'), models.Shipment.courier.ilike('%sameday%'))
    unprinted_counts_query = (
        select(models.StoreCategory.id, func.count(models.Order.id.distinct()))
        .join(models.store_category_map).join(models.Store).join(models.Order)
        .join(models.Shipment, models.Order.id == models.Shipment.order_id)
        .join(latest_shipment_subq, models.Shipment.id == latest_shipment_subq.c.max_id)
        .where(models.Shipment.printed_at.is_(None), models.Shipment.awb.isnot(None), supported_couriers_filter)
        .group_by(models.StoreCategory.id)
    )
    counts_res = await db.execute(unprinted_counts_query)
    counts_dict = dict(counts_res.all())
    categories_res = await db.execute(select(models.StoreCategory).order_by(models.StoreCategory.name))
    categories = categories_res.scalars().all()

    total_unprinted = 0
    batch_size = getattr(settings, "print_batch_size", 250)
    for cat in categories:
        count = counts_dict.get(cat.id, 0)
        cat.unprinted_count = count
        cat.total_batches = math.ceil(count / batch_size) if count > 0 else 0
        total_unprinted += count

    return templates.TemplateResponse("print_view.html", {"request": request, "categories": categories, "total_unprinted": total_unprinted})

@router.post("/print/selected-batches", name="process_and_print_selected_batches")
async def process_and_print_selected_batches(request: Request, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db), category_id: int = Form(...), batch_numbers: str = Form(...)):
    try:
        batch_nums_list = [int(b) for b in batch_numbers.split(',') if b.isdigit()]
        if not batch_nums_list: raise HTTPException(status_code=400, detail="Niciun lot valid selectat.")
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Format invalid pentru loturi.")

    category = await db.get(models.StoreCategory, category_id)
    if not category:
        raise HTTPException(status_code=404, detail="Categoria nu a fost găsită.")

    # TODO: implementarea selectării comenzilor din loturile cerute + generare PDF
    return {"ok": True, "message": "Funcția de printare loturi urmează să fie implementată."}
