"""Characterization tests for the OAuth helper functions in routes/auth.py."""
import hashlib
import hmac

from routes.auth import _valid_shop, _verify_hmac
from settings import settings


def test_valid_shop_accepts_myshopify():
    assert _valid_shop("cool-store.myshopify.com") is True


def test_valid_shop_rejects_non_myshopify_and_injection():
    assert _valid_shop("evil.com") is False
    assert _valid_shop("shop.myshopify.com/admin") is False
    assert _valid_shop("shop.myshopify.com.evil.com") is False
    assert _valid_shop("") is False


def _sign(params: dict) -> str:
    msg = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(settings.SHOPIFY_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()


def test_verify_hmac_accepts_valid_signature():
    params = {"shop": "cool-store.myshopify.com", "code": "abc", "state": "xyz"}
    params["hmac"] = _sign(params)
    assert _verify_hmac(params) is True


def test_verify_hmac_rejects_tampered_params():
    params = {"shop": "cool-store.myshopify.com", "code": "abc", "state": "xyz"}
    params["hmac"] = _sign(params)
    params["shop"] = "attacker.myshopify.com"
    assert _verify_hmac(params) is False
