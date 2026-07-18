"""Serve the embedded React/Polaris SPA under /app.

The Vite build lands in frontend/dist. index.html must contain the placeholder
`%SHOPIFY_API_KEY%` (in the App Bridge <script data-api-key> and a meta tag); we
substitute the real key at serve time so the key isn't baked into the static bundle.

During migration the SPA lives at /app and the legacy Jinja UI keeps working elsewhere;
once every screen is ported, /app becomes the sole UI and the Jinja routes are removed.
"""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from settings import settings

router = APIRouter(tags=["App"])

_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
_INDEX = _DIST / "index.html"

_PLACEHOLDER = """<!doctype html><html><head><meta charset="utf-8">
<title>Order Hub</title></head><body style="font-family:system-ui;padding:2rem">
<h1>Order Hub</h1><p>The embedded app is not built yet. Run <code>npm --prefix frontend run build</code>.</p>
</body></html>"""


def _render_index() -> str:
    if _INDEX.exists():
        html = _INDEX.read_text(encoding="utf-8")
        return html.replace("%SHOPIFY_API_KEY%", settings.SHOPIFY_API_KEY or "")
    return _PLACEHOLDER


# Embedded apps must be frameable by the Shopify admin. Never send X-Frame-Options here;
# scope framing to Shopify with a CSP frame-ancestors directive (required for the iframe).
_FRAME_CSP = "frame-ancestors https://*.myshopify.com https://admin.shopify.com;"


def mount_spa(app):
    """Mount static assets + the SPA entry. Call from main.py after other routers."""
    assets = _DIST / "assets"
    if assets.exists():
        app.mount("/app/assets", StaticFiles(directory=str(assets)), name="spa-assets")

    @app.get("/app", response_class=HTMLResponse)
    @app.get("/app/{full_path:path}", response_class=HTMLResponse)
    async def serve_spa(full_path: str = ""):
        # Client-side routing: any /app/* path returns the SPA shell.
        return HTMLResponse(_render_index(), headers={"Content-Security-Policy": _FRAME_CSP})
