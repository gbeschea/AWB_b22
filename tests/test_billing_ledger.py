"""Tests for billing plan mapping + the reinstall-proof trial-ledger hashing."""
from services import app_ledger
from services import shopify_billing as b


def test_plan_mapping():
    assert b.plan_for_subscription_name("Order Hub Pro") == "pro"
    assert b.plan_for_subscription_name(None) == "free"
    assert b.plan_for_subscription_name("Something else") == "free"


def test_plans_have_free_and_pro():
    assert "free" in b.PLANS and "pro" in b.PLANS
    assert b.PLANS["free"]["price"] == 0.0
    assert b.PLANS["pro"]["price"] > 0


def test_domain_hash_is_deterministic_and_salted():
    h1 = app_ledger.domain_hash("shop.myshopify.com")
    h2 = app_ledger.domain_hash("shop.myshopify.com")
    assert h1 == h2                              # deterministic
    assert h1 != app_ledger.domain_hash("other.myshopify.com")  # per-domain
    assert "shop.myshopify.com" not in h1        # salted hash, not the raw domain
    assert len(h1) == 64                         # sha-256 hex


def test_domain_hash_case_insensitive():
    assert app_ledger.domain_hash("Shop.MyShopify.com") == app_ledger.domain_hash("shop.myshopify.com")
