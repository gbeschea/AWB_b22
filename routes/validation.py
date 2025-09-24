from fastapi import APIRouter, Depends, Request, Query, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, not_, desc, or_
from sqlalchemy.orm import selectinload
import math

from database import get_db
from models import Order
from templating import templates
from schemas import ValidationResult as ValidationResultSchema
from services import address_service

router = APIRouter(
    prefix="/validation",
    tags=["Validation"]
)

@router.get("/", response_class=HTMLResponse, name="get_validation_page")
async def get_validation_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=1000),
):
    validation_condition = Order.address_status.in_(['pending', 'invalid', 'partial_match', 'failed'])

    count_stmt = select(func.count(Order.id)).where(validation_condition)
    total_orders = (await db.execute(count_stmt)).scalar_one() or 0
    total_pages = math.ceil(total_orders / per_page) if total_orders > 0 else 1
    offset = (page - 1) * per_page

    # === MODIFICAREA CHEIE ESTE AICI ===
    stmt = (
        select(Order)
        .options(
            selectinload(Order.shipments),
            selectinload(Order.store),
            selectinload(Order.line_items)
        )
        .where(validation_condition)
        .order_by(desc(Order.created_at))
        .offset(offset)
        .limit(per_page)
    )
    # ===================================
    
    result = await db.execute(stmt)
    orders = result.scalars().all()

    context = {
        "request": request,
        "orders": orders,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "total_orders": total_orders,
        "route_name": "get_validation_page",
        "sort_by": "created_at",
        "sort_order": "desc",
        "filters": None
    }
    return templates.TemplateResponse("validation.html", context)

@router.post("/validate_address/{order_id}", response_model=ValidationResultSchema)
async def validate_address_route(
    order_id: int,
    db: AsyncSession = Depends(get_db),
):
    order = await db.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Comanda nu a fost găsită")

    validation_result = await address_service.validate_address_for_order(db, order)
    await db.commit()
    
    return {
        "is_valid": validation_result.is_valid,
        "score": validation_result.score,
        "errors": validation_result.errors,
        "suggestions": validation_result.suggestions,
        "address_status": order.address_status
    }