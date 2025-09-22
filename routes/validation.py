# routes/validation.py

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy import and_, or_, func

import models
import schemas
from services import address_service
from database import get_db

router = APIRouter(prefix="/validation", tags=["Validation"])
templates = Jinja2Templates(directory="templates")


def _needs_review_clause():
    """
    DOAR comenzile invalidate/partial/not_found.
    Evităm funcții pe JSONB (coloana e JSON). Folosim json_array_length(JSON).
    """
    O = models.Order
    return or_(
        O.address_status.in_(("invalid", "partial_match", "not_found"))
    )


@router.get("/", name="get_validation_page")
async def get_validation_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(500, ge=1, le=1000),
):
    """
    Pagina cu comenzile care necesită validare manuală.
    """
    O = models.Order

    where_clause = _needs_review_clause()

    count_stmt = select(func.count(O.id)).where(where_clause)
    total_orders = (await db.execute(count_stmt)).scalar_one() or 0

    stmt = (
        select(O)
        .where(where_clause)
        .options(
            selectinload(O.store),
            selectinload(O.line_items),
            selectinload(O.shipments),
        )
        .order_by(
            O.address_score.asc().nulls_last(),  # cele mai problematice sus
            O.created_at.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    result = await db.execute(stmt)
    orders_to_validate = result.scalars().unique().all()

    total_pages = (total_orders + page_size - 1) // page_size if total_orders else 1
    context = {
        "request": request,
        "orders": orders_to_validate,
        "page": page,
        "total_pages": total_pages,
        "page_numbers": list(range(1, total_pages + 1)),
        "total_orders": total_orders,
        "page_title": "Validation Hub",
    }
    return templates.TemplateResponse("validation.html", context)


@router.post("/validate_address/{order_id}", response_model=schemas.ValidationResult)
async def validate_address_route(
    order_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Rulează validarea pentru un singur order din UI.
    """
    O = models.Order
    order = (await db.execute(select(O).where(O.id == order_id))).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Comanda nu a fost găsită")

    await address_service.validate_address_for_order(db, order)
    # NU facem acces la relații aici; doar câmpurile simple
    await db.commit()

    is_valid = (order.address_status == "valid")
    errors = order.address_validation_errors if order.address_validation_errors else []
    return schemas.ValidationResult(is_valid=is_valid, errors=errors, suggestions=[])
