"""JSON API for the embedded React/Polaris SPA. Every route is authenticated by the
Shopify session token via `require_shop` (returns the active Store). This is the contract
the frontend calls with App Bridge `authenticatedFetch`.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models
from crud import couriers as crud_couriers
from database import get_db
from services import app_ledger, shopify_billing
from services.shopify_auth import require_shop
from settings import settings

router = APIRouter(prefix="/api", tags=["API"])
_logger = logging.getLogger(__name__)

# Store ids with an in-flight manual sync (prevents piling up on repeated clicks).
_SYNC_BACKFILL_DAYS = 30
_syncing: set = set()
_sync_tasks: set = set()

# Shops whose webhooks we've already reconciled this process lifetime (resets on restart,
# so a deploy re-verifies each shop on its next load — cheap self-heal).
_wh_ensured: set = set()


async def _ensure_webhooks_bg(store_id: int):
    """Reconcile operational webhooks for a shop out-of-band (own DB session)."""
    try:
        from database import AsyncSessionLocal
        from services import shopify_service
        async with AsyncSessionLocal() as db:
            store = await db.get(models.Store, store_id)
            if store and store.is_active and store.access_token:
                await shopify_service.ensure_operational_webhooks(store)
    except Exception:
        _logger.exception("Webhook reconcile failed for store_id=%s", store_id)


# --- Fleet-convention endpoints (mirror @studio/core api.health / api.vitals) ---
# Unauthenticated: health checks + web-vitals beacons don't carry a session token.

@router.get("/health")
async def health():
    """Liveness probe (also used by the Docker HEALTHCHECK)."""
    return {"status": "ok"}


@router.post("/vitals")
async def vitals(request: Request):
    """Core Web Vitals beacon from the embedded SPA (Built-for-Shopify perf signal)."""
    try:
        body = await request.json()
        _logger.info("web-vitals %s", {k: body.get(k) for k in ("name", "value", "id", "rating")})
    except Exception:
        pass
    return Response(status_code=204)


@router.get("/me")
async def me(store: models.Store = Depends(require_shop)):
    """Shop context for the embedded app shell (who am I, what's my plan).
    Also self-heals webhook registration once per shop per process — repairs any that
    failed at install (e.g. before PCD was granted) without needing a reinstall."""
    if store.domain not in _wh_ensured:
        _wh_ensured.add(store.domain)
        t = asyncio.create_task(_ensure_webhooks_bg(store.id))
        _sync_tasks.add(t)
        t.add_done_callback(_sync_tasks.discard)
    return {
        "shop": store.domain,
        "name": store.name,
        "is_active": store.is_active,
        "api_version": store.api_version,
        "plan": store.plan or "free",
    }


# ---- Order sync (manual "Sync now" from the embedded UI) ----

async def _run_backfill(store_id: int):
    """Pull recent orders for one shop, then clear the in-flight flag. Own DB session
    (sync_orders_for_stores opens AsyncSessionLocal)."""
    try:
        from services import sync_service
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=_SYNC_BACKFILL_DAYS)
        await sync_service.sync_orders_for_stores([store_id], start, end)
    except Exception:
        _logger.exception("Manual sync failed for store_id=%s", store_id)
    finally:
        _syncing.discard(store_id)


@router.post("/sync")
async def sync_now(store: models.Store = Depends(require_shop)):
    """Trigger a backfill of recent orders for THIS shop, in the background.
    Idempotent-ish: if a sync is already running for the shop, we don't start a second."""
    if store.id in _syncing:
        return {"status": "in_progress"}
    _syncing.add(store.id)
    task = asyncio.create_task(_run_backfill(store.id))
    _sync_tasks.add(task)
    task.add_done_callback(_sync_tasks.discard)
    return {"status": "started", "since_days": _SYNC_BACKFILL_DAYS}


# ---- Billing (Shopify Billing GraphQL; reconcile-on-load) ----

async def _reconcile_plan(store: models.Store, db: AsyncSession) -> str:
    """Sync Store.plan from Shopify's active subscription (source of truth)."""
    try:
        sub = await shopify_billing.get_active_subscription(store)
    except Exception as e:  # network/API hiccup → keep the cached plan
        _logger.warning("billing reconcile failed for %s: %s", store.domain, e)
        return store.plan or "free"
    plan_key = shopify_billing.plan_for_subscription_name(sub.get("name") if sub else None)
    changed = (
        store.plan != plan_key
        or store.subscription_gid != (sub.get("id") if sub else None)
        or store.subscription_status != (sub.get("status") if sub else None)
    )
    if changed:
        store.plan = plan_key
        store.subscription_gid = sub.get("id") if sub else None
        store.subscription_status = sub.get("status") if sub else None
        await db.commit()
    return plan_key


@router.get("/billing")
async def billing_status(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    plan_key = await _reconcile_plan(store, db)
    return {
        "current_plan": plan_key,
        "subscription_status": store.subscription_status,
        "test": shopify_billing.BILLING_TEST,
        "plans": list(shopify_billing.PLANS.values()),
    }


@router.post("/billing/subscribe")
async def billing_subscribe(
    plan: str,
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Start a paid plan → returns the Shopify confirmationUrl the merchant must approve.
    The trial is reinstall-proof: a shop that already used it gets 0 trial days."""
    plan_def = shopify_billing.PLANS.get(plan)
    grants_trial = bool(plan_def and plan_def.get("trial_days", 0) > 0)
    used = await app_ledger.has_used_trial(db, store.domain) if grants_trial else False
    trial_override = 0 if used else None

    return_url = f"{settings.SHOPIFY_APP_URL}/app/billing"
    try:
        url = await shopify_billing.create_subscription(store, plan, return_url, trial_days=trial_override)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Billing error: {e}")

    # Record that this shop has now consumed its trial (survives reinstall).
    if grants_trial and not used:
        await app_ledger.mark_trial_used(db, store.domain)
    return {"confirmationUrl": url}


@router.post("/billing/cancel")
async def billing_cancel(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    await shopify_billing.cancel_subscription(store)
    store.plan = "free"
    store.subscription_gid = None
    store.subscription_status = None
    await db.commit()
    return {"current_plan": "free"}


@router.get("/overview")
async def overview(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Home dashboard signals: drives the setup guide + the at-a-glance ROI card."""
    async def _count(stmt) -> int:
        return (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0

    orders_total = await _count(select(models.Order).where(models.Order.store_id == store.id))
    address_issues = await _count(
        select(models.Order).where(
            models.Order.store_id == store.id,
            models.Order.address_status.in_(_ADDR_PROBLEM_STATUSES),
        )
    )
    printable = select(models.Shipment.id).where(
        models.Shipment.order_id == models.Order.id,
        models.Shipment.awb.isnot(None),
        models.Shipment.printed_at.is_(None),
    )
    print_queue = await _count(
        select(models.Order).where(models.Order.store_id == store.id, printable.exists())
    )
    accounts = await crud_couriers.get_courier_accounts_for_store(db, store.id)

    return {
        "orders_total": orders_total,
        "address_issues": address_issues,
        "print_queue": print_queue,
        "has_courier_account": len(accounts) > 0,
        "plan": store.plan or "free",
        "last_sync_at": store.last_sync_at.isoformat() if store.last_sync_at else None,
        "syncing": store.id in _syncing,
    }


def _latest_shipment(order: models.Order):
    """The most recent shipment (by id) for AWB/courier/status display."""
    return max(order.shipments, key=lambda s: s.id) if order.shipments else None


def _order_json(o: models.Order) -> dict:
    s = _latest_shipment(o)
    return {
        "id": o.id,
        "name": o.name,
        "customer": o.customer,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "total_price": o.total_price,
        "financial_status": o.financial_status,
        "processing_status": o.processing_status,
        "derived_status": o.derived_status,
        "address_status": o.address_status,
        "address_score": o.address_score,
        "city": o.shipping_city,
        "phone": o.shipping_phone,
        "assigned_courier": o.assigned_courier,
        "awb": s.awb if s else None,
        "courier": s.courier if s else None,
        "last_status": s.last_status if s else None,
        "printed": bool(s and s.printed_at) if s else False,
    }


@router.get("/orders")
async def list_orders(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
    page: int = 1,
    per_page: int = 50,
    q: Optional[str] = None,
    status: Optional[str] = None,
):
    """Paginated orders for THIS shop only (tenancy enforced via require_shop)."""
    per_page = min(max(per_page, 1), 200)
    page = max(page, 1)

    base = select(models.Order).where(models.Order.store_id == store.id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        base = base.where(or_(
            models.Order.name.ilike(like),
            models.Order.customer.ilike(like),
            models.Order.shipping_phone.ilike(like),
            models.Order.shipping_city.ilike(like),
        ))
    if status and status.strip():
        base = base.where(models.Order.processing_status == status.strip())

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    rows = (await db.execute(
        base.options(selectinload(models.Order.shipments))
        .order_by(desc(models.Order.created_at))
        .offset((page - 1) * per_page)
        .limit(per_page)
    )).scalars().all()

    return {
        "orders": [_order_json(o) for o in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/print-queue")
async def print_queue(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
    page: int = 1,
    per_page: int = 50,
):
    """Orders with a generated AWB that hasn't been printed yet (the depot print queue)."""
    per_page = min(max(per_page, 1), 200)
    page = max(page, 1)

    printable = select(models.Shipment.id).where(
        models.Shipment.order_id == models.Order.id,
        models.Shipment.awb.isnot(None),
        models.Shipment.printed_at.is_(None),
    )
    base = select(models.Order).where(
        models.Order.store_id == store.id,
        printable.exists(),
    )
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    rows = (await db.execute(
        base.options(selectinload(models.Order.shipments))
        .order_by(desc(models.Order.created_at))
        .offset((page - 1) * per_page)
        .limit(per_page)
    )).scalars().all()

    def _item(o: models.Order) -> dict:
        shipments = [
            {"id": s.id, "awb": s.awb, "courier": s.courier, "paper_size": s.paper_size}
            for s in o.shipments
            if s.awb and not s.printed_at
        ]
        return {
            "id": o.id,
            "name": o.name,
            "customer": o.customer,
            "city": o.shipping_city,
            "shipments": shipments,
        }

    return {"items": [_item(o) for o in rows], "total": total, "page": page, "per_page": per_page}


_ADDR_PROBLEM_STATUSES = ("invalid", "not_found", "partial_match", "nevalidat")


@router.get("/address-issues")
async def address_issues(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
    page: int = 1,
    per_page: int = 50,
):
    """Orders whose shipping address needs attention, with the latest validation detail.
    This is the address-yield surface: fix these before the parcel ships."""
    per_page = min(max(per_page, 1), 200)
    page = max(page, 1)

    base = select(models.Order).where(
        models.Order.store_id == store.id,
        models.Order.address_status.in_(_ADDR_PROBLEM_STATUSES),
    )
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    rows = (await db.execute(
        base.options(selectinload(models.Order.address_validations))
        .order_by(desc(models.Order.created_at))
        .offset((page - 1) * per_page)
        .limit(per_page)
    )).scalars().all()

    def _issue(o: models.Order) -> dict:
        latest = max(o.address_validations, key=lambda v: v.id) if o.address_validations else None
        return {
            "id": o.id,
            "name": o.name,
            "customer": o.customer,
            "address_status": o.address_status,
            "address_score": o.address_score,
            "shipping": {
                "name": o.shipping_name,
                "address1": o.shipping_address1,
                "address2": o.shipping_address2,
                "city": o.shipping_city,
                "zip": o.shipping_zip,
                "province": o.shipping_province,
                "country": o.shipping_country,
                "phone": o.shipping_phone,
            },
            "errors": o.address_validation_errors or (latest.errors if latest else None),
            "suggestions": latest.suggestions if latest else None,
        }

    return {"issues": [_issue(o) for o in rows], "total": total, "page": page, "per_page": per_page}


@router.get("/couriers")
async def couriers_config(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Courier accounts, name→account mappings, and shipment profiles for the settings screen.
    NEVER exposes credentials — only whether they're set. (Global today; multi-tenancy will
    scope these by store — TODO Phase 3.)"""
    accounts = await crud_couriers.get_courier_accounts_for_store(db, store.id)
    mappings = await crud_couriers.get_courier_mappings_for_store(db, store.id)
    profiles = await crud_couriers.get_shipment_profiles_for_store(db, store.id)
    return {
        "accounts": [
            {
                "id": a.id,
                "name": a.name,
                "account_key": a.account_key,
                "courier_type": a.courier_type,
                "tracking_url": a.tracking_url,
                "is_active": a.is_active,
                "has_credentials": bool(a.credentials),  # never send the secret itself
            }
            for a in accounts
        ],
        "mappings": [
            {"id": m.id, "shopify_name": m.shopify_name, "account_key": m.account_key}
            for m in mappings
        ],
        "profiles": [
            {
                "id": p.id,
                "name": p.name,
                "account_key": p.account_key,
                "default_parcels": p.default_parcels,
                "default_weight_kg": p.default_weight_kg,
                "default_service_id": p.default_service_id,
            }
            for p in profiles
        ],
    }
