"""Unit tests for the webhook order-ingest parsers (REST-shaped payloads).

The full upsert needs a DB; these lock in the DB-free mapping helpers that turn a
Shopify webhook payload into Order columns — the bit most likely to regress
(tags-as-string vs list, gateway list, money string → float, courier→account_key).
"""
from services import webhook_service as w


def test_to_float_coerces_and_tolerates_junk():
    assert w._to_float("123.45") == 123.45
    assert w._to_float(10) == 10.0
    assert w._to_float(None) is None
    assert w._to_float("") is None
    assert w._to_float("not-a-number") is None


def test_tags_from_webhook_string_and_graphql_list():
    # REST webhook delivers a comma-separated STRING …
    assert w._tags_to_str("vip, urgent") == "vip, urgent"
    # … the GraphQL path delivers a LIST.
    assert w._tags_to_str(["vip", "urgent"]) == "vip, urgent"
    assert w._tags_to_str("") is None
    assert w._tags_to_str([]) is None
    assert w._tags_to_str(None) is None


def test_full_name_builds_or_returns_none():
    assert w._full_name("Ana", "Pop") == "Ana Pop"
    assert w._full_name("Ana", None) == "Ana"
    assert w._full_name(None, None) is None
    assert w._full_name("", "  ") is None


def test_normalize_account_key():
    assert w._normalize_account_key("DPD Romania") == "dpdromania"
    assert w._normalize_account_key("DPD") == "dpd"
    assert w._normalize_account_key("Sameday Courier") == "sameday"
    assert w._normalize_account_key("Fan Courier") == "fancourier"
    assert w._normalize_account_key(None) == "default"
    assert w._normalize_account_key("") == "default"


def test_both_order_topics_share_the_idempotent_upsert():
    assert w.WEBHOOK_HANDLERS["orders/create"] is w.upsert_order_from_webhook
    assert w.WEBHOOK_HANDLERS["orders/updated"] is w.upsert_order_from_webhook
