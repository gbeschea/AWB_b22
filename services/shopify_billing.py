"""Shopify Billing (managed pricing via the Billing GraphQL API).

Source of truth for what a shop pays is Shopify's `activeSubscriptions`; we cache the
resulting plan on `Store.plan` and reconcile on load / after the approval return.
"""
import logging
import os
from typing import Optional

from services.shopify_service import get_shopify_client

_logger = logging.getLogger(__name__)

# Charges are TEST charges unless SHOPIFY_BILLING_TEST=false (so dev/review never bills).
BILLING_TEST = (os.environ.get("SHOPIFY_BILLING_TEST", "true").lower() != "false")
CURRENCY = "USD"

PLANS = {
    "free": {
        "key": "free",
        "name": "Free",
        "price": 0.0,
        "trial_days": 0,
        "order_limit": 150,
        "features": [
            "Up to 150 orders / month",
            "Address validation",
            "1 courier account",
        ],
    },
    "pro": {
        "key": "pro",
        "name": "Pro",
        "price": 19.99,
        "trial_days": 14,
        "order_limit": None,
        "features": [
            "Unlimited orders",
            "All couriers & shipment profiles",
            "Priority support",
        ],
    },
}

FREE_PLAN = "free"


def plan_for_subscription_name(name: Optional[str]) -> str:
    """Map a Shopify subscription name back to our plan key."""
    if not name:
        return FREE_PLAN
    low = name.lower()
    for key, plan in PLANS.items():
        if key != FREE_PLAN and plan["name"].lower() in low:
            return key
    return FREE_PLAN


async def get_active_subscription(store) -> Optional[dict]:
    """The shop's current active app subscription (or None on Free)."""
    client = get_shopify_client(store)
    query = "{ currentAppInstallation { activeSubscriptions { id name status test } } }"
    r = await client.post("graphql.json", json={"query": query})
    r.raise_for_status()
    data = r.json() or {}
    subs = (((data.get("data") or {}).get("currentAppInstallation") or {}).get("activeSubscriptions")) or []
    return subs[0] if subs else None


async def create_subscription(store, plan_key: str, return_url: str, trial_days: Optional[int] = None) -> str:
    """Start a paid subscription. Returns the confirmationUrl the merchant must approve.
    trial_days overrides the plan default (used by the reinstall-proof trial guard)."""
    plan = PLANS.get(plan_key)
    if not plan or plan["price"] <= 0:
        raise ValueError(f"Not a paid plan: {plan_key}")
    effective_trial = plan["trial_days"] if trial_days is None else trial_days

    mutation = """
    mutation CreateSub($name: String!, $returnUrl: URL!, $amount: Decimal!, $currency: CurrencyCode!, $trialDays: Int!, $test: Boolean!) {
      appSubscriptionCreate(
        name: $name,
        returnUrl: $returnUrl,
        trialDays: $trialDays,
        test: $test,
        lineItems: [{
          plan: { appRecurringPricingDetails: { price: { amount: $amount, currencyCode: $currency }, interval: EVERY_30_DAYS } }
        }]
      ) {
        userErrors { field message }
        confirmationUrl
        appSubscription { id status }
      }
    }"""
    variables = {
        "name": f"Order Hub {plan['name']}",
        "returnUrl": return_url,
        "amount": plan["price"],
        "currency": CURRENCY,
        "trialDays": int(effective_trial),
        "test": BILLING_TEST,
    }
    client = get_shopify_client(store)
    r = await client.post("graphql.json", json={"query": mutation, "variables": variables})
    r.raise_for_status()
    payload = ((r.json() or {}).get("data") or {}).get("appSubscriptionCreate") or {}
    errors = payload.get("userErrors") or []
    if errors:
        raise RuntimeError("; ".join(e.get("message", "billing error") for e in errors))
    url = payload.get("confirmationUrl")
    if not url:
        raise RuntimeError("No confirmationUrl returned by Shopify")
    return url


async def cancel_subscription(store) -> None:
    """Cancel the active subscription (downgrade to Free)."""
    sub = await get_active_subscription(store)
    if not sub:
        return
    mutation = """
    mutation Cancel($id: ID!) {
      appSubscriptionCancel(id: $id) { userErrors { field message } appSubscription { id status } }
    }"""
    client = get_shopify_client(store)
    r = await client.post("graphql.json", json={"query": mutation, "variables": {"id": sub["id"]}})
    r.raise_for_status()
