# main.py

import asyncio
import os
import json
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
    courier_actions as courier_actions_routes,
    order_actions as order_actions_routes,
    cs_queue as cs_queue_routes,
    org as org_routes,
    print_ops as print_ops_routes,
    address_tools as address_tools_routes,
    scan as scan_routes,
    spa as spa_routes,
    legal as legal_routes,
    hub_settings as hub_settings_routes,
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
# Sidekick app-data tools run in Shopify's sandboxed browser and fetch this backend cross-origin.
# Allow Shopify-owned sandbox origins — the request still carries an auto-attached session token that
# require_shop validates, so CORS only decides which browser origins may READ the (already-authorized)
# response. Kept as a regex since Shopify doesn't publish one fixed sandbox origin.
_SHOPIFY_SANDBOX_RE = r"https://([a-z0-9-]+\.)*(shopifycdn\.com|shopifycloud\.com|shopify\.com)"
if _cors_env:
    _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_origin_regex=_SHOPIFY_SANDBOX_RE,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    logging.getLogger(__name__).warning(
        "AWB_B2_CORS_ORIGINS not set — using dev-only localhost + Shopify-sandbox CORS. "
        "Set an explicit allowlist in production."
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?|" + _SHOPIFY_SANDBOX_RE,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Static
app.mount("/static", StaticFiles(directory="static"), name="static")

# Routers
app.include_router(auth_routes.router)
app.include_router(api_routes.router)
app.include_router(courier_actions_routes.router)
app.include_router(order_actions_routes.router)
app.include_router(cs_queue_routes.router)
app.include_router(cs_queue_routes.config_router)
app.include_router(org_routes.router)
app.include_router(hub_settings_routes.router)
app.include_router(webhooks.router, tags=["Webhooks"])
app.include_router(couriers_routes.data_router)
app.include_router(print_ops_routes.router)
app.include_router(address_tools_routes.router)
app.include_router(scan_routes.router)
app.include_router(background.router, tags=["Background Tasks"])
app.include_router(legal_routes.router, tags=["Legal"])

# ⛔ The LEGACY Jinja tool is deliberately NOT mounted.
#
# It predates the embedded Shopify app and was mounted alongside it with NO authentication of any
# kind — no Depends, no middleware, no cookie check (the `awb_session` cookie is set at install and
# read by nothing). Every one of its routes read and wrote the SAME multi-tenant tables as /api/*,
# scoped to no store, so anonymous requests could:
#   • read every merchant's decrypted Shopify access token + webhook HMAC secret (/settings/stores)
#   • read and overwrite courier credentials (/settings/couriers…)
#   • create REAL courier AWBs on any merchant's orders and any merchant's courier contract
#     (/actions/create-awb, /processing/create-awbs — the latter never even recorded a Shipment,
#     so those labels were invisible to the plan quota forever)
#   • mark orders as paid via the merchant's own Admin token (/financials/mark-as-paid)
#   • download label PDFs with customer PII and silently mark parcels printed (/labels/…)
#   • trigger a full sync of every installed store (/sync/…)
# Verified live before removal: every legacy HTML page already returned 500 (a Starlette/Jinja
# TemplateResponse API break), so the UI was already dead — only the JSON endpoints still worked,
# which is exactly the dangerous half. The React SPA calls ONLY /api/* (verified against
# frontend/src), so nothing that works today depends on these.
#
# The modules still exist on disk; re-mounting ANY of them requires adding real auth
# (Depends(require_shop)) AND org/store scoping to every query in them first.
#   removed: orders, processing, sync, labels, settings, validation,
#            couriers_routes.settings_router, printing, logs, store_categories,
#            financials, actions, profiles (html_router/api_router are the same object)

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
    # Not installed → start OAuth. If Shopify loaded this entry EMBEDDED (in the admin iframe),
    # a plain 302 to Shopify OAuth loads the OAuth page INSIDE the iframe → "refused to connect"
    # (it works only after a manual refresh, which reloads top-level). So when embedded, break OUT
    # of the iframe via App Bridge — window.open(url,'_top') is intercepted and does the top-level
    # navigation — and run OAuth at the top. Loaded TOP-LEVEL (the other install path) → plain 302
    # (emitting App Bridge OUTSIDE the admin would hijack navigation, hence the guard).
    embedded = request.query_params.get("embedded") == "1" or request.headers.get("sec-fetch-dest") == "iframe"
    if embedded and settings.SHOPIFY_API_KEY:
        install_url = f"{(settings.SHOPIFY_APP_URL or '').rstrip('/')}/auth/install?shop={quote(shop)}"
        html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<meta name='shopify-api-key' content='{settings.SHOPIFY_API_KEY}'>"
            "<script src='https://cdn.shopify.com/shopifycloud/app-bridge.js'></script></head>"
            "<body><script>window.open(" + json.dumps(install_url) + ", '_top');</script></body></html>"
        )
        return HTMLResponse(html, headers={
            "Content-Security-Policy": "frame-ancestors https://*.myshopify.com https://admin.shopify.com;"})
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

    # Background courier-status → Shopify sync loop (single-runner via a pg advisory lock,
    # so it's safe even if the app runs multiple workers). Toggle with AWB_STATUS_POLL_ENABLED=0.
    if (os.environ.get("AWB_STATUS_POLL_ENABLED", "1").strip() != "0"):
        from services import status_sync_service
        try:
            interval = int(os.environ.get("AWB_STATUS_POLL_INTERVAL_SEC", "900"))
        except ValueError:
            interval = 900
        app.state.status_poll_task = asyncio.create_task(status_sync_service.poll_loop(interval))
        logger.info("Status-sync background loop scheduled (interval=%ss).", interval)

    # cron-parity SHADOW (duplicate / COD capture / surpriză / colete) — LOG-ONLY, paritate cu
    # cronul xConnector; nu scrie nimic în Shopify. Activ doar cu CRON_PARITY_SHADOW=1.
    if os.environ.get("CRON_PARITY_SHADOW") == "1":
        from services.cron_parity import loop as cron_parity_loop
        app.state.cron_parity_task = asyncio.create_task(cron_parity_loop.run_forever())
        logger.info("cron-parity SHADOW loop scheduled.")

    # Punte xConnector → OH pt statusul PRINTED (etichete printate la depozit = `downloaded` pe
    # documentul SHIPPING_LABEL). Se auto-reduce la tăcere pt magazine fără cont xConnector.
    if os.environ.get("PRINTED_BRIDGE_ENABLED", "1").strip() != "0":
        from services import awbprint_bridge
        app.state.printed_bridge_task = asyncio.create_task(awbprint_bridge.run_forever())
        logger.info("printed-bridge (xConnector downloaded) scheduled.")


@app.on_event("shutdown")
async def on_shutdown():
    task = getattr(app.state, "status_poll_task", None)
    if task:
        task.cancel()
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

