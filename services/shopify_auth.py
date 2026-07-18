"""Shopify App Bridge session-token verification for the embedded SPA.

Every request from the embedded React/Polaris app carries an `Authorization: Bearer <jwt>`
session token (App Bridge `authenticatedFetch`). The token is an HS256 JWT signed with the
app's client secret. We verify it and resolve the shop, then load the Store.

Docs: https://shopify.dev/docs/apps/auth/session-tokens
"""
import logging
from urllib.parse import urlparse

import jwt  # PyJWT
from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from crud import stores as crud_stores
from database import get_db
from settings import settings

_logger = logging.getLogger(__name__)
_LEEWAY = 10  # seconds of clock skew tolerance


def _shop_from_dest(dest: str) -> str:
    """`dest` is like 'https://shop.myshopify.com' → 'shop.myshopify.com'."""
    host = urlparse(dest).hostname or ""
    return host.lower()


def verify_session_token(token: str) -> str:
    """Verify a Shopify session-token JWT and return the shop domain. Raises on any failure."""
    if not settings.SHOPIFY_API_SECRET or not settings.SHOPIFY_API_KEY:
        raise HTTPException(503, "Shopify auth not configured")
    try:
        payload = jwt.decode(
            token,
            settings.SHOPIFY_API_SECRET,
            algorithms=["HS256"],
            audience=settings.SHOPIFY_API_KEY,
            leeway=_LEEWAY,
            options={"require": ["exp", "nbf", "dest", "aud"]},
        )
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"Invalid session token: {e}")

    shop = _shop_from_dest(payload.get("dest", ""))
    if not shop.endswith(".myshopify.com"):
        raise HTTPException(401, "Invalid session token destination")
    # iss and dest must reference the same shop.
    if payload.get("iss") and _shop_from_dest(payload["iss"]) != shop:
        raise HTTPException(401, "Session token iss/dest mismatch")
    return shop


async def require_shop(
    authorization: str = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """FastAPI dependency: verify the session token and return the active Store.
    Use on every /api route: `store = Depends(require_shop)`."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    shop = verify_session_token(token)
    store = await crud_stores.get_store_by_domain(db, shop)
    if not store or not store.is_active:
        raise HTTPException(403, "Shop not installed")
    return store
