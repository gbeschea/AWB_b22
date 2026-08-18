# routes/auth.py — Shopify OAuth install/callback for Order Hub (standalone, login-via-Shopify).
#
# Flow:
#   GET /auth/install?shop=<x>.myshopify.com  -> redirect to Shopify consent (signed+cookie state)
#   GET /auth/callback?code&shop&state&hmac    -> verify HMAC + state, exchange code->token,
#                                                 upsert Store (token encrypted at rest via the
#                                                 EncryptedString column), register webhooks,
#                                                 set session cookie, redirect into the app.
#
# Requires (settings.py / .env): SHOPIFY_API_KEY, SHOPIFY_API_SECRET, SHOPIFY_APP_URL,
# SHOPIFY_SCOPES, SHOPIFY_API_VERSION, SESSION_SECRET.

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from crud import stores as crud_stores
from database import get_db
from services import shopify_service, shopify_billing
from settings import settings

router = APIRouter(prefix="/auth", tags=["Auth"])
_logger = logging.getLogger(__name__)

_SHOP_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$")
_STATE_MAX_AGE = 600            # 10 min for the OAuth handshake
_SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days
_INSTALL_BACKFILL_DAYS = 30     # how far back to pull orders on first install

# Keep strong refs to detached backfill tasks so they aren't GC'd mid-run.
_bg_tasks: set = set()


def _require_config():
    missing = [
        k for k in ("SHOPIFY_API_KEY", "SHOPIFY_API_SECRET", "SHOPIFY_APP_URL", "SESSION_SECRET")
        if not getattr(settings, k, None)
    ]
    if missing:
        raise HTTPException(503, f"Shopify OAuth not configured: missing {', '.join(missing)}")


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.SESSION_SECRET, salt="shopify-oauth")


def _valid_shop(shop: str) -> bool:
    return bool(shop) and bool(_SHOP_RE.match(shop))


def _verify_hmac(params: dict) -> bool:
    """Verify the ?hmac= Shopify appends to install/callback GET params."""
    received = params.get("hmac", "")
    msg = "&".join(f"{k}={v}" for k, v in sorted((k, v) for k, v in params.items() if k != "hmac"))
    digest = hmac.new(settings.SHOPIFY_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, received)


@router.get("/install")
async def install(request: Request, shop: str = ""):
    _require_config()
    shop = shop.strip().lower()
    if not _valid_shop(shop):
        raise HTTPException(400, "Invalid shop domain")
    q = dict(request.query_params)
    if "hmac" in q and not _verify_hmac(q):
        raise HTTPException(401, "HMAC verification failed")

    state = _signer().dumps({"shop": shop, "n": secrets.token_urlsafe(16)})
    authorize = f"https://{shop}/admin/oauth/authorize?" + urlencode({
        "client_id": settings.SHOPIFY_API_KEY,
        "scope": settings.SHOPIFY_SCOPES,
        "redirect_uri": f"{settings.SHOPIFY_APP_URL}/auth/callback",
        "state": state,
    })
    resp = RedirectResponse(authorize, status_code=302)
    resp.set_cookie("oauth_state", state, max_age=_STATE_MAX_AGE,
                    httponly=True, secure=True, samesite="lax")
    return resp


@router.get("/callback")
async def callback(request: Request, db: AsyncSession = Depends(get_db)):
    _require_config()
    q = dict(request.query_params)
    shop = (q.get("shop") or "").strip().lower()
    code = q.get("code") or ""
    state = q.get("state") or ""

    if not _valid_shop(shop) or not code:
        raise HTTPException(400, "Missing shop/code")
    if not _verify_hmac(q):
        raise HTTPException(401, "HMAC verification failed")
    if not state:
        raise HTTPException(401, "State mismatch")
    # The signed state itself is the trust anchor (HMAC via URLSafeTimedSerializer,
    # time-limited, shop bound + verified below). The oauth_state cookie does NOT
    # survive a real embedded install — the flow can start inside the admin iframe
    # where Set-Cookie is third-party (blocked), and the admin frequently fires the
    # install twice so a second /auth/install overwrites the first cookie. Requiring
    # cookie equality therefore 401'd fresh installs with "State mismatch" (App
    # Store review, 2026-07-23). Signature + max_age + shop-match + Shopify's own
    # callback HMAC replace the cookie comparison.
    try:
        data = _signer().loads(state, max_age=_STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        raise HTTPException(401, "Invalid or expired state")
    if data.get("shop") != shop:
        raise HTTPException(401, "State shop mismatch")

    # Exchange the temporary code for an EXPIRING offline access token. `expiring=1` is required:
    # without it Shopify issues a non-expiring token, which public apps may no longer use and which
    # the Admin API rejects with a 403 that reads like a broken token rather than a missing flag.
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"https://{shop}/admin/oauth/access_token",
            data={
                "client_id": settings.SHOPIFY_API_KEY,
                "client_secret": settings.SHOPIFY_API_SECRET,
                "code": code,
                "expiring": "1",
            },
            headers={"Accept": "application/json"},
        )
    r.raise_for_status()
    body = r.json() or {}
    access_token = body.get("access_token")
    if not access_token:
        raise HTTPException(502, "No access_token returned by Shopify")
    _now = datetime.now(timezone.utc)
    _expires_at = _now + timedelta(seconds=int(body["expires_in"])) if body.get("expires_in") else None
    _refresh_token = body.get("refresh_token")
    _refresh_expires_at = (
        _now + timedelta(seconds=int(body["refresh_token_expires_in"]))
        if body.get("refresh_token_expires_in") else None
    )

    # For OAuth apps the webhook HMAC secret is the single app secret.
    store = await crud_stores.create_or_update_store(
        db,
        domain=shop,
        name=shop.replace(".myshopify.com", ""),
        access_token=access_token,
        shared_secret=settings.SHOPIFY_API_SECRET,
        is_active=True,
        token_expires_at=_expires_at,
        refresh_token=_refresh_token,
        refresh_token_expires_at=_refresh_expires_at,
    )

    # COMP: our OWN shops get Pro (unlimited labels) without a Shopify charge — it's our app on our
    # stores. While OH is private every install is ours, so auto-comp here (OH_COMP_ALL_INSTALLS) means
    # we never have to hand-flip it — the gap where a store installed after the manual pass stayed on
    # Free. External installs (post-launch, not in the allowlist) fall through and stay billable.
    if not getattr(store, "comp", False) and shopify_billing.should_comp_on_install(shop):
        store.comp = True
        await db.commit()

    # Register operational webhooks (app/uninstalled + orders create/updated/edited).
    # Idempotent + self-healing: if this fails now (e.g. PCD not yet granted), the on-load
    # reconcile in /api/me repairs it — no reinstall needed. GDPR webhooks live in the TOML.
    try:
        await shopify_service.ensure_operational_webhooks(store)
    except Exception:
        _logger.exception("Webhook registration failed at install for %s", shop)

    # Backfill recent orders so the app isn't empty on first open. Detached: the merchant
    # is redirected into the embedded app immediately while this runs in the background.
    task = asyncio.create_task(_initial_backfill(store.id))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)

    session_token = _signer().dumps({"shop": shop, "store_id": store.id, "t": int(time.time())})
    # Re-enter the Shopify admin so the app loads EMBEDDED (not standalone).
    handle = shop.replace(".myshopify.com", "")
    admin_url = f"https://admin.shopify.com/store/{handle}/apps/{settings.SHOPIFY_API_KEY}"
    resp = RedirectResponse(admin_url, status_code=302)
    resp.set_cookie("awb_session", session_token, max_age=_SESSION_MAX_AGE,
                    httponly=True, secure=True, samesite="lax")
    resp.delete_cookie("oauth_state")
    return resp


async def _initial_backfill(store_id: int):
    """Pull the last _INSTALL_BACKFILL_DAYS of orders for a freshly-installed shop.
    Uses its own DB session (sync_orders_for_stores opens AsyncSessionLocal). Lazy import
    keeps auth.py free of the sync stack at module load."""
    try:
        from services import sync_service
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=_INSTALL_BACKFILL_DAYS)
        _logger.info("Initial backfill starting for store_id=%s (%s days).",
                     store_id, _INSTALL_BACKFILL_DAYS)
        n = await sync_service.sync_orders_for_stores([store_id], start, end)
        _logger.info("Initial backfill done for store_id=%s: %s orders.", store_id, n)
    except Exception:
        _logger.exception("Initial backfill failed for store_id=%s", store_id)


