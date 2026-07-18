"""Tests for Shopify session-token (App Bridge) verification."""
import time

import jwt
import pytest
from fastapi import HTTPException

from services.shopify_auth import verify_session_token
from settings import settings

SHOP = "cool-store.myshopify.com"


def _token(**overrides):
    now = int(time.time())
    claims = {
        "iss": f"https://{SHOP}/admin",
        "dest": f"https://{SHOP}",
        "aud": settings.SHOPIFY_API_KEY,
        "sub": "1",
        "exp": now + 60,
        "nbf": now - 10,
        "iat": now,
        "jti": "abc",
        "sid": "sid",
    }
    claims.update(overrides)
    return jwt.encode(claims, settings.SHOPIFY_API_SECRET, algorithm="HS256")


def test_valid_token_returns_shop():
    assert verify_session_token(_token()) == SHOP


def test_wrong_audience_rejected():
    with pytest.raises(HTTPException):
        verify_session_token(_token(aud="someone-else"))


def test_expired_token_rejected():
    now = int(time.time())
    with pytest.raises(HTTPException):
        verify_session_token(_token(exp=now - 100, nbf=now - 200))


def test_wrong_signature_rejected():
    bad = jwt.encode({"dest": f"https://{SHOP}", "aud": settings.SHOPIFY_API_KEY,
                      "exp": int(time.time()) + 60, "nbf": int(time.time()) - 10},
                     "wrong-secret", algorithm="HS256")
    with pytest.raises(HTTPException):
        verify_session_token(bad)


def test_non_myshopify_dest_rejected():
    with pytest.raises(HTTPException):
        verify_session_token(_token(iss="https://evil.com/admin", dest="https://evil.com"))
