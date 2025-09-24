# routes/orders.py
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

import crud.stores as store_crud
from database import get_db
from services import filter_service
from dependencies import get_templates
from settings import settings
import json
import os
import math
from templating import templates
import base64

router = APIRouter()

def _enhance_orders_for_view(orders: List):
    """Adaugă câmpuri dinamice la obiectele Order pentru afișare."""
    for order in orders:
        latest_shipment = None
        if getattr(order, "shipments", None):
            sorted_shipments = sorted(
                order.shipments,
                key=lambda s: s.fulfillment_created_at or datetime.min.replace(tzinfo=timezone.utc)
            )
            latest_shipment = sorted_shipments[-1]
        setattr(order, "latest_shipment", latest_shipment)
        line_items = getattr(order, "line_items", []) or []
        setattr(order, "line_items_str", ", ".join([f"{item.quantity}x {item.title}" for item in line_items]))
        if latest_shipment and latest_shipment.last_status:
            setattr(order, "mapped_courier_status", latest_shipment.last_status)
        else:
            setattr(order, "mapped_courier_status", "N/A")
    return orders

@router.get("/view", response_class=HTMLResponse, name="view_orders")
async def view_orders_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    page: int = 1,
    per_page: int = 100,
    sort_by: str = "created_at",
    sort_order: str = "desc",
    filters: str = None
):
    query_params = request.query_params

    orders, total_orders, filter_counts = await filter_service.get_filtered_orders(db, query_params)
    all_stores = await store_crud.get_stores(db)
    categories = await store_crud.get_all_store_categories(db)

    # Opțiuni pentru dropdown-uri din pagina de filtre
    def _opts(key: str):
        d = filter_counts.get(key, {}) or {}
        return [k for k in d.keys() if k != "all"]

    derived_status_options = _opts("derived_status")
    financial_status_options = _opts("financial_status")
    fulfillment_status_options = _opts("fulfillment_status")
    address_status_options = _opts("address_status") or ["valid", "invalid", "partial_match", "not_found", "nevalidat"]

    # Curieri disponibili pentru filtru
    couriers = _opts("courier")

    # Opțiuni grupuri status curier (din setări, dacă există harta)
    courier_status_group_options = []
    if settings.COURIER_STATUS_MAP:
        for group_key, payload in settings.COURIER_STATUS_MAP.items():
            # payload: [display_name, [...status list...]]
            display_name = payload[0] if isinstance(payload, list) and payload else group_key
            courier_status_group_options.append((group_key, display_name))

    orders = _enhance_orders_for_view(orders)
           # === ÎNCEPUT MODIFICARE (înlocuiește complet codul anterior) ===
    with open("config/courier_map.json", "r") as f:
        courier_map = json.load(f)

    courier_tracking_map = {}
    for courier_name, courier_slug in courier_map.items():
        # Extragem numele de bază al curierului (ex: 'dpd' din 'dpd-ro')
        config_file_name = courier_slug.split('-')[0]
        config_path = f"config/{config_file_name}.json"

        # Verificăm dacă fișierul de configurare există
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                courier_config = json.load(f)
                tracking_url = courier_config.get("tracking_url")
                if tracking_url:
                    courier_tracking_map[courier_name] = tracking_url
    # === SFÂRȘIT MODIFICARE ===
        # === ÎNCEPUT MODIFICARE (Adaugă acest bloc) ===
    decoded_filters = {}
    if filters:
        try:
            # Decodează din Base64 și apoi din JSON
            decoded_filters = json.loads(base64.urlsafe_b64decode(filters))
        except (json.JSONDecodeError, TypeError, ValueError):
            # În caz de eroare, resetează la un dicționar gol
            decoded_filters = {}
    # === SFÂRȘIT MODIFICARE ===

    total_pages = math.ceil(total_orders / per_page)



    context = {
        "request": request,
        "orders": orders,
        "total_orders": total_orders,
        "all_stores": all_stores,
        "stores": all_stores,  # pentru dropdown-ul de filtrare
        "categories": categories,
        "couriers": couriers,
        "derived_status_options": derived_status_options,
        "financial_status_options": financial_status_options,
        "fulfillment_status_options": fulfillment_status_options,
        "address_status_options": address_status_options,
        "courier_status_group_options": courier_status_group_options,
        "filter_counts": filter_counts,
        "courier_tracking_map": courier_tracking_map,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "sort_by": sort_by,
        "sort_order": sort_order,
        "filters": decoded_filters,
        "route_name": "view_orders"

    }
    return templates.TemplateResponse("index.html", context)
