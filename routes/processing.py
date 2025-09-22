# routes/processing.py

from fastapi import APIRouter, Depends, Request, Query, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, not_
from sqlalchemy.orm import selectinload

import models
from database import get_db
from dependencies import get_templates, get_pagination_numbers

router = APIRouter(
    prefix="/processing",
    tags=["Processing"]
)

@router.get("", response_class=HTMLResponse, name="get_processing_page")
async def get_processing_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    templates = Depends(get_templates),
    page: int = Query(1, ge=1)
):
    """
    Afișează comenzile cu adresa validată care sunt gata pentru generarea AWB-ului.
    """
    page_size = 50
    
    # Condiția: adresa validă ȘI nicio expediere cu AWB deja generat.
    condition = (
        models.Order.address_status == 'valid',
        not_(models.Order.shipments.any(models.Shipment.awb.is_not(None)))
    )

    # Numărăm totalul de comenzi care îndeplinesc condiția
    count_stmt = select(func.count(models.Order.id)).where(*condition)
    total_orders = (await db.execute(count_stmt)).scalar_one() or 0
    
    # Preluăm comenzile pentru pagina curentă
    stmt = (
        select(models.Order)
        .where(*condition)
        .options(
            selectinload(models.Order.store),
            selectinload(models.Order.line_items)
        )
        .order_by(models.Order.created_at.asc()) # Cele mai vechi primele
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    
    result = await db.execute(stmt)
    orders_to_process = result.scalars().unique().all()
    
    total_pages = (total_orders + page_size - 1) // page_size if total_orders > 0 else 1
    page_numbers = get_pagination_numbers(page, total_pages)

    context = {
        "request": request,
        "orders": orders_to_process,
        "page": page,
        "total_pages": total_pages,
        "page_numbers": page_numbers,
        "total_orders": total_orders,
        "page_title": "Procesare Comenzi"
    }
    return templates.TemplateResponse("processing.html", context)