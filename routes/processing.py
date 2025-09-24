from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select, desc, func, exists
import math

from database import get_db
from models import Order, Shipment
from templating import templates

# Corecția 1: Am adăugat prefixul la router, pentru consistență
router = APIRouter(
    prefix="/processing",
    tags=["Processing"]
)

# Corecția 2: Am schimbat calea în "/" pentru a se potrivi cu prefixul de mai sus
# Corecția 3: Am adăugat parametrii lipsă în semnătura funcției
@router.get("/", response_class=HTMLResponse, name="get_processing_page")
async def get_processing_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    page: int = 1,
    per_page: int = 100,
    sort_by: str = None,      # Parametru adăugat
    sort_order: str = None,   # Parametru adăugat
    filters: str = None       # Parametru adăugat
):
    processing_condition = (
        Order.address_status == 'valid',
        ~exists().where(Shipment.order_id == Order.id)
    )

    count_stmt = select(func.count(Order.id)).where(*processing_condition)
    total_orders = (await db.execute(count_stmt)).scalar_one()
    total_pages = math.ceil(total_orders / per_page) if total_orders > 0 else 1
    offset = (page - 1) * per_page

    stmt = (
        select(Order)
        .options(
            selectinload(Order.shipments),
            selectinload(Order.store),
            selectinload(Order.line_items)
        )
        .where(*processing_condition)
        .order_by(desc(Order.created_at))
        .offset(offset)
        .limit(per_page)
    )
    
    result = await db.execute(stmt)
    orders_with_shipments = result.scalars().all()

    context = {
        "request": request,
        "orders": orders_with_shipments,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "total_orders": total_orders,
        "route_name": "get_processing_page",
        "sort_by": sort_by,
        "sort_order": sort_order,
        "filters": filters
    }
    return templates.TemplateResponse("processing.html", context)