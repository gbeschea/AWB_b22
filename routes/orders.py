from datetime import datetime, timezone
from typing import List, Dict, Any
from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import math

import models
from database import get_db
from services import filter_service
from settings import settings
from templating import get_templates

router = APIRouter(tags=["Orders"])

def _enhance_orders_for_view(orders: List[models.Order]) -> List[models.Order]:
    """Adaugă câmpuri derivate utile pentru UI (ultima expediere, etc.)."""
    for order in orders:
        latest_shipment = None
        if getattr(order, "shipments", None):
            sorted_shipments = sorted(
                order.shipments,
                key=lambda s: (getattr(s, 'last_status_at', None) or getattr(s, 'fulfillment_created_at', None) or datetime.min.replace(tzinfo=timezone.utc)),
                reverse=True
            )
            latest_shipment = sorted_shipments[0] if sorted_shipments else None
        setattr(order, "latest_shipment", latest_shipment)
    return orders

def _build_courier_tracking_map() -> Dict[str, str]:
    """Construiește o hartă a URL-urilor de tracking pentru afișare."""
    display_to_template: Dict[str, str] = {}
    base_map = settings.COURIER_TRACKING_MAP or {}
    if not base_map: return display_to_template

    for display_name, account_key in (settings.COURIER_MAP or {}).items():
        ak = (account_key or "").lower()
        base = None
        if "dpd" in ak: base = "DPD"
        elif "sameday" in ak: base = "Sameday"
        elif "econt" in ak: base = "Econt"
        if base and base in base_map:
            display_to_template[display_name] = base_map[base]
    return display_to_template

@router.get("/view", response_class=HTMLResponse, name="view_orders")
async def view_orders_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    templates = Depends(get_templates),
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=1000)
):
    orders, total_orders, filter_counts = await filter_service.get_filtered_orders(db, request.query_params)
    total_pages = math.ceil(total_orders / per_page) if total_orders > 0 else 1

    stores_res = await db.execute(select(models.Store).order_by(models.Store.name))
    all_stores = stores_res.scalars().all()

    categories_res = await db.execute(select(models.StoreCategory).order_by(models.StoreCategory.name))
    categories = categories_res.scalars().all()

    def _opts(key: str):
        d = filter_counts.get(key, {}) or {}
        return [k for k in d.keys() if k != "all"]

    courier_status_group_options = []
    if settings.COURIER_STATUS_MAP:
        for group_key, payload in settings.COURIER_STATUS_MAP.items():
            display_name = payload[0] if isinstance(payload, list) and payload else group_key
            courier_status_group_options.append((group_key, display_name))

    context: Dict[str, Any] = {
        "request": request,
        "orders": _enhance_orders_for_view(orders),
        "total_orders": total_orders,
        "stores": all_stores,
        "categories": categories,
        "couriers": _opts("courier"),
        "derived_status_options": _opts("derived_status"),
        "financial_status_options": _opts("financial_status"),
        "fulfillment_status_options": _opts("fulfillment_status"),
        "address_status_options": _opts("address_status"),
        "courier_status_group_options": courier_status_group_options,
        "courier_tracking_map": _build_courier_tracking_map(),
        "filter_counts": filter_counts,
        "page_title": "Comenzi",
        # === AM REINTRODUS VARIABILELE PENTRU PAGINARE ===
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "route_name": "view_orders"
    }
    return templates.TemplateResponse("index.html", context)