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

import hashlib
import hmac
import re
import secrets
import time
from urllib.parse import urlencode, quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from crud import stores as crud_stores
from database import get_db
from settings import settings

router = APIRouter(prefix="/auth", tags=["Auth"])

_SHOP_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$")
_STATE_MAX_AGE = 600            # 10 min for the OAuth handshake
_SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days


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
    if not state or state != request.cookies.get("oauth_state"):
        raise HTTPException(401, "State mismatch")
    try:
        data = _signer().loads(state, max_age=_STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        raise HTTPException(401, "Invalid or expired state")
    if data.get("shop") != shop:
        raise HTTPException(401, "State shop mismatch")

    # Exchange the temporary code for a permanent offline access token.
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"https://{shop}/admin/oauth/access_token",
            json={
                "client_id": settings.SHOPIFY_API_KEY,
                "client_secret": settings.SHOPIFY_API_SECRET,
                "code": code,
            },
        )
    r.raise_for_status()
    access_token = (r.json() or {}).get("access_token")
    if not access_token:
        raise HTTPException(502, "No access_token returned by Shopify")

    # For OAuth apps the webhook HMAC secret is the single app secret.
    store = await crud_stores.create_or_update_store(
        db,
        domain=shop,
        name=shop.replace(".myshopify.com", ""),
        access_token=access_token,
        shared_secret=settings.SHOPIFY_API_SECRET,
        is_active=True,
    )

    await _register_webhooks(shop, access_token)

    session_token = _signer().dumps({"shop": shop, "store_id": store.id, "t": int(time.time())})
    resp = RedirectResponse(f"{settings.SHOPIFY_APP_URL}/?shop={quote(shop)}", status_code=302)
    resp.set_cookie("awb_session", session_token, max_age=_SESSION_MAX_AGE,
                    httponly=True, secure=True, samesite="lax")
    resp.delete_cookie("oauth_state")
    return resp


async def _register_webhooks(shop: str, token: str):
    """Register operational webhooks (app/uninstalled + orders/updated) via Admin GraphQL.
    GDPR privacy webhooks are declared in shopify.app.toml, not here."""
    api = settings.SHOPIFY_API_VERSION
    base = settings.SHOPIFY_APP_URL
    subs = [
        ("APP_UNINSTALLED", f"{base}/webhooks/app/uninstalled"),
        ("ORDERS_UPDATED", f"{base}/webhooks/orders/updated"),
    ]
    mutation = """
    mutation webhookSubscriptionCreate($topic: WebhookSubscriptionTopic!, $sub: WebhookSubscriptionInput!) {
      webhookSubscriptionCreate(topic: $topic, webhookSubscription: $sub) {
        userErrors { field message }
      }
    }"""
    async with httpx.AsyncClient(
        base_url=f"https://{shop}/admin/api/{api}/",
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        timeout=20,
    ) as client:
        for topic, addr in subs:
            try:
                await client.post("graphql.json", json={
                    "query": mutation,
                    "variables": {"topic": topic, "sub": {"callbackUrl": addr, "format": "JSON"}},
                })
            except httpx.HTTPError:
                # Non-fatal: the app still installs; webhooks can be re-registered later.
                pass
