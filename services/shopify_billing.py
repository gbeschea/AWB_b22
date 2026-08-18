"""Shopify Billing (managed pricing via the Billing GraphQL API).

Source of truth for what a shop pays is Shopify's `activeSubscriptions`; we cache the
resulting plan on `Store.plan` and reconcile on load / after the approval return.
"""
import logging
import os
from typing import Optional

from services.shopify_service import authed_client

_logger = logging.getLogger(__name__)

# Charges are REAL by default — a config flag that has to be remembered at launch is a flag that
# gets forgotten, and the failure is silent: subscriptions look active and nobody is ever charged.
#
# Development and partner-test stores CANNOT be charged for real, so they always get test charges,
# decided from the shop's own plan rather than from configuration. That keeps the App Store
# reviewer's dev store working without putting the live setting at the mercy of an env var.
# SHOPIFY_BILLING_TEST=true still forces test charges everywhere, for local work.
_FORCE_TEST = (os.environ.get("SHOPIFY_BILLING_TEST", "").strip().lower() == "true")
CURRENCY = "USD"

# shop domain -> is a development/partner store (plans don't change under us; cache for the process)
_dev_store_cache: dict = {}


async def _is_development_store(store) -> bool:
    """True for development / partner / staff-affiliate stores, which Shopify can't bill for real."""
    domain = getattr(store, "domain", None) or ""
    if domain in _dev_store_cache:
        return _dev_store_cache[domain]
    verdict = False
    try:
        client = await authed_client(store)
        r = await client.post("graphql.json", json={
            "query": "{ shop { plan { partnerDevelopment shopifyPlus displayName } } }"
        })
        r.raise_for_status()
        plan = (((r.json() or {}).get("data") or {}).get("shop") or {}).get("plan") or {}
        name = (plan.get("displayName") or "").strip().lower()
        verdict = bool(plan.get("partnerDevelopment")) or name in {
            "developer preview", "development", "partner test", "affiliate", "staff business"
        }
    except Exception as e:
        # Unknown → bill for real. Guessing "test" here would silently zero out revenue.
        _logger.warning("could not read shop plan for %s (%s); assuming a billable store", domain, e)
    _dev_store_cache[domain] = verdict
    return verdict


async def use_test_charge(store) -> bool:
    """Whether this shop's subscription must be created as a Shopify TEST charge."""
    return True if _FORCE_TEST else await _is_development_store(store)

# The ONLY difference between the plans is the monthly shipping-label cap, and that cap is
# ENFORCED (services.shopify_billing.assert_label_quota, called at every AWB-creation choke point).
# Every other capability — all couriers, lockers, address validation, bulk, picking, scanning,
# shipment profiles — is available on Free. Do not list a Pro-only feature here that the code does
# not actually gate: an unenforced paid tier fails the honesty gate and App Store review.
PLANS = {
    "free": {
        "key": "free",
        "name": "Free",
        "price": 0.0,
        "trial_days": 0,
        "order_limit": 150,
        "features": [
            "Up to 150 shipping labels / month",
            "All couriers, lockers & address validation",
            "Bulk labels, picking, scanning & invoicing",
        ],
    },
    "pro": {
        "key": "pro",
        "name": "Pro",
        "price": 19.99,
        "trial_days": 14,
        "order_limit": None,
        "features": [
            "Unlimited shipping labels",
            "Everything in Free",
            "Priority email support",
        ],
    },
}

FREE_PLAN = "free"


# Shopify AppSubscription statuses that mean "this shop is actually paying right now". A trialing
# subscription is ACTIVE, so the trial is covered. PENDING (never approved), DECLINED, EXPIRED,
# FROZEN (payment failed) and CANCELLED are NOT entitlements.
_ENTITLING_STATUSES = {"ACTIVE", "ACCEPTED"}


def entitled_plan_key(store) -> str:
    """The plan this shop is actually ENTITLED to right now.

    `Store.plan` is only a cache of the last reconcile, and the only reconcile runs when the merchant
    opens the billing page. Reading it alone meant a cancelled/expired/frozen subscription — or an
    uninstall, which cancels the subscription at Shopify — kept its paid cap indefinitely. So a paid
    plan additionally requires a live subscription status; anything else falls back to Free.

    COMP: our OWN shops are entitled to Pro without any Shopify subscription (it's our app on our shops).
    """
    if getattr(store, "comp", False):
        return "pro"
    plan = getattr(store, "plan", None) or FREE_PLAN
    if plan == FREE_PLAN or plan not in PLANS:
        return FREE_PLAN
    status = (getattr(store, "subscription_status", None) or "").strip().upper()
    return plan if status in _ENTITLING_STATUSES else FREE_PLAN


def label_limit_for(store) -> Optional[int]:
    """The monthly shipping-label cap for this shop's plan (None = unlimited)."""
    plan = PLANS.get(entitled_plan_key(store), PLANS[FREE_PLAN])
    return plan.get("order_limit")


async def monthly_label_count(db, store) -> int:
    """Labels THIS APP created for this shop so far this calendar month.

    Only counts what we actually produced at a courier, so the merchant is never billed for
    someone else's work:
      • `courier_specific_data` non-NULL  → we called the courier and stored its response.
        Labels mirrored back from Shopify fulfillments (services/utils.py, webhook_service.py) leave
        it NULL — those were made in another tool and used to burn this app's quota.
      • the `{"external": true}` marker    → a tracking number the merchant pasted in by hand
        (routes/order_actions.py); we didn't create that label either.
      • `is_demo` orders                   → sandbox labels never count.

    Scoped to the CURRENT store only. It used to count every store in the org while the cap came
    from the current store's own plan, so a Free shop linked to a busy Pro shop inherited the Pro
    shop's volume against a 150 cap and was blocked at zero labels of its own.
    """
    from datetime import datetime, timezone
    from sqlalchemy import select, func as safunc, or_
    import models

    start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # `.as_string()` (not `.astext`, which is JSONB-only) — the column is a generic JSON type.
    external_flag = models.Shipment.courier_specific_data["external"].as_string()
    q = (
        select(safunc.count())
        .select_from(models.Shipment)
        .join(models.Order, models.Shipment.order_id == models.Order.id)
        .where(
            models.Order.store_id == store.id,
            models.Order.is_demo.isnot(True),        # sandbox labels never count
            models.Shipment.awb.isnot(None),
            models.Shipment.created_at.isnot(None),
            models.Shipment.created_at >= start,
            models.Shipment.courier_specific_data.isnot(None),   # we called the courier
            or_(external_flag.is_(None), external_flag != "true"),  # not a hand-entered tracking no.
        )
    )
    return int((await db.execute(q)).scalar() or 0)


async def assert_label_quota(db, store) -> None:
    """Raise 402 if this shop is on a capped plan and has already hit its monthly label cap.
    Called at every AWB-creation choke point (interactive + background), so the paid tier is real
    and can't be bypassed. Free = 150/month; Pro = unlimited (limit None → no-op).

    Test mode is a sandbox for setup + App Store review: while it's on, labels go to the demo
    courier and are never counted or capped, so a demo can exercise AWB creation without ever
    touching the real quota."""
    if getattr(store, "test_mode", False):
        return
    limit = label_limit_for(store)
    if limit is None:
        return
    used = await monthly_label_count(db, store)
    if used >= limit:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=402,
            detail=(
                f"Free plan limit reached: {used}/{limit} shipping labels this month. "
                f"Upgrade to Pro (Plans) for unlimited labels."
            ),
        )


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
    client = await authed_client(store)
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
        "test": await use_test_charge(store),
    }
    client = await authed_client(store)

    async def _attempt(as_test: bool):
        variables["test"] = as_test
        r = await client.post("graphql.json", json={"query": mutation, "variables": variables})
        r.raise_for_status()
        payload = ((r.json() or {}).get("data") or {}).get("appSubscriptionCreate") or {}
        return payload, [e.get("message", "billing error") for e in (payload.get("userErrors") or [])]

    payload, errors = await _attempt(variables["test"])

    # Shopify is the authority on whether a shop can be charged for real. Development and partner
    # stores can't, and no scope we hold reports that reliably (the shop.plan probe 403s), so ask
    # by trying: a real charge first, downgraded to a test charge only when Shopify says so. This
    # keeps live merchants billable without a config flag anyone has to remember.
    if errors and not variables["test"] and any(
        k in " ".join(errors).lower() for k in ("development store", "test charge", "partner", "cannot be charged")
    ):
        _logger.info("shop %s cannot take a real charge (%s); retrying as a test charge",
                     getattr(store, "domain", "?"), "; ".join(errors))
        _dev_store_cache[getattr(store, "domain", "")] = True
        payload, errors = await _attempt(True)

    if errors:
        raise RuntimeError("; ".join(errors))
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
    client = await authed_client(store)
    r = await client.post("graphql.json", json={"query": mutation, "variables": {"id": sub["id"]}})
    r.raise_for_status()
    # Shopify reports a refused cancellation in `userErrors`, not via the HTTP status. Ignoring them
    # let the app tell the merchant "you're on Free, cancelled" while Shopify kept charging them —
    # the merchant stops expecting the charge and only finds out on their next invoice. Raise instead,
    # so the caller surfaces the failure and leaves the cached plan alone.
    payload = ((r.json() or {}).get("data") or {}).get("appSubscriptionCancel") or {}
    errors = [e.get("message", "cancel failed") for e in (payload.get("userErrors") or [])]
    if errors:
        raise RuntimeError("; ".join(errors))
