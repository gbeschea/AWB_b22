# main.py

import os
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from database import get_db
from crud import stores as crud_stores

from routes import (
    store_categories, printing, logs, orders, sync, labels, actions,
    settings as settings_router, validation, webhooks, processing,
    background, profiles, financials,
    couriers as couriers_routes,
    auth as auth_routes,
    api as api_routes,
    spa as spa_routes,
)
from websocket_manager import manager
from settings import settings
from database import engine
import logging

# Optional: close shared HTTP client used by courier services
try:
    from services.couriers import _http_client as couriers_http_client
except Exception:
    couriers_http_client = None

app = FastAPI(
    title="Order Hub",
    description="Aplicatie pentru managementul comenzilor și generarea de AWB-uri.",
    version="1.0.0"
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# CORS — explicit allowlist. `allow_origins=["*"]` + credentials is invalid per the
# CORS spec and insecure, so it was removed. Production MUST set AWB_B2_CORS_ORIGINS
# (comma-separated exact origins). With it unset we fall back to a dev-only regex that
# matches any localhost/127.0.0.1 port.
_cors_env = (os.environ.get("AWB_B2_CORS_ORIGINS") or "").strip()
if _cors_env:
    _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    logging.getLogger(__name__).warning(
        "AWB_B2_CORS_ORIGINS not set — using dev-only localhost CORS. "
        "Set an explicit allowlist in production."
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Static
app.mount("/static", StaticFiles(directory="static"), name="static")

# Routers
app.include_router(auth_routes.router)
app.include_router(api_routes.router)
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
app.include_router(financials.router, tags=["Financials"])
app.include_router(actions.router)
app.include_router(profiles.html_router)
app.include_router(profiles.api_router)

# Embedded React/Polaris SPA (served under /app). Mounted last so its /app/{path:path}
# catch-all doesn't shadow the API/legacy routes above.
spa_routes.mount_spa(app)


@app.get("/", include_in_schema=False)
async def root_entry(request: Request, db=Depends(get_db)):
    """Embedded-app entry (Shopify loads the App URL here). Installed shop → the SPA;
    fresh shop → break out of the iframe and start OAuth."""
    shop = (request.query_params.get("shop") or "").strip().lower()
    host = request.query_params.get("host", "")
    if not shop:
        return RedirectResponse("/app")
    store = await crud_stores.get_store_by_domain(db, shop)
    if store and store.is_active and store.access_token:
        suffix = f"?shop={quote(shop)}" + (f"&host={quote(host)}" if host else "")
        return RedirectResponse(f"/app{suffix}")
    # Not installed → start OAuth. Shopify loads this entry TOP-LEVEL during install, so a
    # plain 302 works. (Do NOT emit an App Bridge <script> here — outside the admin it
    # hijacks navigation to the admin app URL and pre-empts the OAuth redirect.)
    return RedirectResponse(f"/auth/install?shop={quote(shop)}")


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

