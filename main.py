# main.py

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from routes import (
    store_categories, printing, logs, orders, sync, labels, actions,
    settings as settings_router, validation, webhooks, processing,
    background, profiles,
    couriers as couriers_routes,
    financials # <-- MODIFICARE: Am adăugat noul router
)
from websocket_manager import manager
from settings import settings
from database import engine
import logging

from routes.couriers_profiles_full import settings_router as couriers_router


# Optional: close shared HTTP client used by courier services
try:
    from services.couriers import _http_client as couriers_http_client
except Exception:
    couriers_http_client = None

app = FastAPI(
    title="AWB Hub",
    description="Aplicatie pentru managementul comenzilor și generarea de AWB-uri.",
    version="1.0.0"
)

app.include_router(couriers_router)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static
app.mount("/static", StaticFiles(directory="static"), name="static")

# Routers
app.include_router(orders.router, tags=["Orders"])
app.include_router(processing.router, tags=["Processing"])
app.include_router(sync.router, tags=["Sync"])
app.include_router(labels.router, tags=["Labels"])
app.include_router(settings_router.router, tags=["Settings"])
app.include_router(validation.router, tags=["Validation"])
app.include_router(webhooks.router, tags=["Webhooks"])
app.include_router(couriers_routes.settings_router)
app.include_router(couriers_routes.data_router)
app.include_router(printing.router, tags=["Printing"])
app.include_router(logs.router, tags=["Logs"])
app.include_router(store_categories.router, tags=["Store Categories"])
app.include_router(background.router, tags=["Background Tasks"])
app.include_router(financials.router, tags=["Financials"]) # <-- MODIFICARE: Am inclus router-ul aici
app.include_router(actions.router)
app.include_router(profiles.html_router) 
app.include_router(profiles.api_router)


@app.on_event("startup")
async def on_startup():
    # Asigură-te că vizualizarea orders_view există
    view_sql = """
    CREATE OR REPLACE VIEW orders_view AS
    WITH latest_shipment AS (
      SELECT s.order_id, s.last_status, s.last_status_at, s.id,
             ROW_NUMBER() OVER (PARTITION BY s.order_id ORDER BY s.last_status_at NULLS LAST, s.id DESC) AS rn
      FROM shipments s
    )
    SELECT
      o.id,
      CASE
        WHEN ls.last_status ILIKE 'delivered%%' OR ls.last_status ILIKE '%%livrat%%' THEN 'delivered'
        WHEN ls.last_status ILIKE '%%refus%%' OR ls.last_status ILIKE '%%return%%' THEN 'refused'
        WHEN ls.last_status ILIKE '%%cancel%%' OR ls.last_status ILIKE '%%anulat%%' THEN 'canceled'
        WHEN ls.last_status ILIKE '%%locker%%' OR ls.last_status ILIKE '%%parcelshop%%' OR ls.last_status ILIKE '%%pick-up%%' THEN 'pickup_office'
        WHEN ls.last_status ILIKE '%%in curs%%' OR ls.last_status ILIKE '%%tranzit%%' OR ls.last_status ILIKE 'out for delivery%%' OR ls.last_status ILIKE 'in transit%%' THEN 'in_transit'
        WHEN ls.last_status ILIKE '%%expediat%%' OR ls.last_status ILIKE '%%warehouse%%' OR ls.last_status ILIKE '%%pick-up%%' THEN 'shipped'
        WHEN ls.last_status ILIKE '%%proces%%' OR ls.last_status ILIKE '%%registered%%' OR ls.last_status ILIKE '%%awb%%' THEN 'processed'
        ELSE NULL
      END AS mapped_courier_status
    FROM orders o
    LEFT JOIN latest_shipment ls ON ls.order_id = o.id AND ls.rn = 1;
    """
    async with engine.begin() as conn:
        await conn.execute(text(view_sql))

@app.on_event("shutdown")
async def on_shutdown():
    if couriers_http_client:
        try:
            await couriers_http_client.aclose()
        except Exception:
            pass

@app.websocket("/ws/status")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)