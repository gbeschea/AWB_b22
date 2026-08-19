"""JSON API for the embedded React/Polaris SPA. Every route is authenticated by the
Shopify session token via `require_shop` (returns the active Store). This is the contract
the frontend calls with App Bridge `authenticatedFetch`.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from sqlalchemy import and_, asc, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models
from crud import couriers as crud_couriers
from database import get_db
from services import app_ledger, org_service, shipment_rules as rules_svc, shopify_billing
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
    # Detect scopes we now require but the shop hasn't granted yet (e.g. write_inventory,
    # write_order_edits added after install) so the UI can prompt a re-consent. Best-effort.
    missing_scopes = await _missing_scopes(store)
    return {
        "shop": store.domain,
        "name": store.name,
        "is_active": store.is_active,
        "api_version": store.api_version,
        "plan": store.plan or "free",
        "missing_scopes": missing_scopes,
        "reauth_url": f"/auth/install?shop={store.domain}" if missing_scopes else None,
    }


def _required_scopes() -> set:
    return {s.strip() for s in (settings.SHOPIFY_SCOPES or "").split(",") if s.strip()}


async def _missing_scopes(store: models.Store) -> list:
    """Required − granted, treating write_X as covering read_X. Returns [] on any error so the
    app never breaks over this check."""
    try:
        from services import shopify_service
        granted = await shopify_service.get_granted_scopes(store)
        granted_exp = set(granted) | {"read_" + g[len("write_"):] for g in granted if g.startswith("write_")}
        return sorted(_required_scopes() - granted_exp)
    except Exception:
        return []


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

    # The trial is consumed HERE — when Shopify confirms a live subscription — not when the
    # confirmationUrl was minted. Creating a subscription only yields a PENDING one plus a URL the
    # merchant still has to approve; marking the ledger there burned the trial of anyone who opened
    # the approval page and backed out (or double-clicked), permanently and reinstall-proof.
    if sub and plan_key != shopify_billing.FREE_PLAN:
        plan_def = shopify_billing.PLANS.get(plan_key) or {}
        if plan_def.get("trial_days", 0) > 0:
            try:
                if not await app_ledger.has_used_trial(db, store.domain):
                    await app_ledger.mark_trial_used(db, store.domain)
            except Exception:  # the ledger must never break the billing page
                _logger.exception("could not record trial usage for %s", store.domain)
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
        "test": await shopify_billing.use_test_charge(store),
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

    # Come back INSIDE the admin. Returning to the bare app URL lands the merchant on an
    # un-embedded page with no session token — right after they've approved a payment, which is
    # the worst possible moment for the app to look broken.
    shop_handle = (store.domain or "").replace(".myshopify.com", "")
    return_url = (f"https://admin.shopify.com/store/{shop_handle}"
                  f"/apps/{settings.SHOPIFY_APP_HANDLE}/app/billing")
    try:
        url = await shopify_billing.create_subscription(store, plan, return_url, trial_days=trial_override)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Billing error: {e}")

    # NOTE: the trial is NOT marked used here. `create_subscription` only returns a URL for a PENDING
    # subscription — the merchant still has to approve it, and many never do. The ledger is written in
    # `_reconcile_plan`, once Shopify reports a live subscription, so backing out of the approval page
    # (or double-clicking Subscribe) can no longer burn a trial the merchant never received.
    return {"confirmationUrl": url}


@router.post("/billing/cancel")
async def billing_cancel(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    # Only clear the cached plan if Shopify actually cancelled. Reporting "Free" on a failed cancel
    # tells the merchant to stop expecting charges while Shopify keeps billing them.
    try:
        await shopify_billing.cancel_subscription(store)
    except Exception as e:
        _logger.warning("subscription cancel failed for %s: %s", store.domain, e)
        raise HTTPException(502, f"Could not cancel the subscription with Shopify: {e}")
    store.plan = shopify_billing.FREE_PLAN
    store.subscription_gid = None
    store.subscription_status = None
    await db.commit()
    return {"current_plan": shopify_billing.FREE_PLAN}


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

    # Setup-guide signals — each one is DERIVED from real configuration, never a stored "I clicked
    # done" flag, so the guide can't claim a step is finished when it isn't.
    has_profile = bool((await db.execute(
        select(models.ShipmentProfile.id).where(models.ShipmentProfile.store_id == store.id).limit(1)
    )).scalar())
    has_box = bool((await db.execute(
        select(models.PackingBox.id).where(models.PackingBox.store_id == store.id).limit(1)
    )).scalar())
    has_awb = bool((await db.execute(
        select(models.Shipment.id).join(models.Order, models.Order.id == models.Shipment.order_id)
        .where(models.Order.store_id == store.id, models.Shipment.awb.isnot(None)).limit(1)
    )).scalar())
    from services import smartbill_service

    return {
        "orders_total": orders_total,
        "address_issues": address_issues,
        "print_queue": print_queue,
        "has_courier_account": len(accounts) > 0,
        "test_mode": bool(getattr(store, "test_mode", False)),
        "test_mode_used": bool(getattr(store, "test_mode_used", False)),
        "has_sender": bool(store.sender_name),
        "has_packing": bool(store.default_pieces_per_parcel or store.default_box_id or has_box),
        "has_profile": has_profile,
        "has_invoicing": smartbill_service.is_configured(),
        "has_awb": has_awb,
        "auto_awb_enabled": bool(store.auto_awb_enabled),
        "plan": store.plan or "free",
        "last_sync_at": store.last_sync_at.isoformat() if store.last_sync_at else None,
        "syncing": store.id in _syncing,
    }


@router.get("/overview/stores")
async def overview_stores(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Cross-store processing overview: per-store + total counts for every store in this store's
    org (just itself if unlinked). Numbers use the SAME predicates as the Orders lenses / Print
    queue / CS backlog, so each cell deep-links to a page that shows exactly that set."""
    from services import org_service
    stores = await org_service.linked_stores(db, store)
    ids = [s.id for s in stores]

    O, S, CS = models.Order, models.Shipment, models.CSQueueItem
    has_awb = select(S.id).where(S.order_id == O.id, S.awb.isnot(None))

    def moving(*pats):
        return select(S.id).where(S.order_id == O.id, or_(*[S.last_status.ilike(p) for p in pats]))

    cs_open = select(CS.id).where(CS.order_id == O.id, CS.status != "solved")
    printable = select(S.id).where(S.order_id == O.id, S.awb.isnot(None), S.printed_at.is_(None))

    async def grouped(cond) -> dict:
        rows = (await db.execute(
            select(O.store_id, func.count()).where(O.store_id.in_(ids), cond).group_by(O.store_id)
        )).all()
        return {sid: n for sid, n in rows}

    to_ship = await grouped(and_(O.cancelled_at.is_(None), ~has_awb.exists(), ~cs_open.exists()))
    to_print = await grouped(printable.exists())
    addr_issues = await grouped(O.address_status.in_(_ADDR_PROBLEM_STATUSES))
    in_transit = await grouped(and_(
        moving("%curs%", "%tranzit%", "%transit%", "%out for delivery%").exists(),
        ~moving("%livrat%", "%delivered%").exists()))
    refused = await grouped(or_(O.cancelled_at.isnot(None),
                                moving("%refuz%", "%retur%", "%return%").exists()))
    cs_backlog = await grouped(cs_open.exists())

    keys = ["to_ship", "address_issues", "to_print", "in_transit", "refused", "cs_backlog"]
    maps = {"to_ship": to_ship, "address_issues": addr_issues, "to_print": to_print,
            "in_transit": in_transit, "refused": refused, "cs_backlog": cs_backlog}
    rows_out = [{
        "id": s.id, "domain": s.domain, "name": s.name or s.domain,
        "group": s.store_group, "is_me": s.id == store.id,
        **{k: maps[k].get(s.id, 0) for k in keys},
    } for s in stores]
    totals = {k: sum(r[k] for r in rows_out) for k in keys}

    org = None
    if store.organization_id:
        o = await db.get(models.Organization, store.organization_id)
        if o:
            org = {"id": o.id, "name": o.name}
    groups = sorted({s.store_group for s in stores if s.store_group})
    return {"org": org, "multi": len(stores) > 1, "stores": rows_out, "totals": totals, "groups": groups}


def _latest_shipment(order: models.Order):
    """The most recent shipment (by id) for AWB/courier/status display."""
    return max(order.shipments, key=lambda s: s.id) if order.shipments else None


def _order_json(o: models.Order, in_cs: bool = False) -> dict:
    from services.status_sync_service import _tracking_url
    s = _latest_shipment(o)
    return {
        "id": o.id,
        "in_cs": in_cs,
        "is_demo": bool(getattr(o, "is_demo", False)),
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
        "shipment_id": s.id if s else None,
        "awb": s.awb if s else None,
        "courier": s.courier if s else None,
        "last_status": s.last_status if s else None,
        "printed": bool(s and s.printed_at) if s else False,
        "invoice_number": (f"{o.invoice_series or ''}{o.invoice_number}" if o.invoice_number else None),
        "invoice_url": o.invoice_url,
        "financial_paid": (o.financial_status or "").lower() == "paid",
        "on_hold": bool(o.is_on_hold_shopify),
        "line_count": len(o.line_items) if o.line_items is not None else 0,
        "items": [{"sku": li.sku, "title": li.title, "quantity": li.quantity}
                  for li in (o.line_items or [])],
        "product_tags": sorted({t for li in (o.line_items or [])
                                for t in (li.product_tags or "").split("|") if t.strip()}),
        "address": {
            "name": o.shipping_name, "address1": o.shipping_address1, "address2": o.shipping_address2,
            "city": o.shipping_city, "zip": o.shipping_zip, "province": o.shipping_province,
            "country": o.shipping_country, "phone": o.shipping_phone,
        },
        "store_domain": (o.store.domain if o.store else None),
        "store_name": (o.store.name if o.store else None),
        "tracking_url": (_tracking_url(s.courier, s.awb) if (s and s.awb) else None),
    }


@router.get("/orders")
async def list_orders(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
    page: int = 1,
    per_page: int = 50,
    q: Optional[str] = None,
    status: Optional[str] = None,
    scope: Optional[str] = None,
    lens: Optional[str] = None,
    with_lens_counts: bool = False,
    # --- faceted filters (xConnector-style) ---
    payment: Optional[str] = None,          # card | cod
    delivery: Optional[str] = None,         # home | locker
    printed: Optional[str] = None,          # yes | no
    invoiced: Optional[str] = None,         # yes | no — has a fiscal invoice
    order_status: Optional[str] = None,     # unfulfilled | awb | on_hold | cancelled | cs
    delivery_status: Optional[str] = None,  # in_transit | delivered | refused | none
    address_status: Optional[str] = None,   # valid | invalid | nevalidat | partial_match
    tag: Optional[str] = None,              # substring in order tags
    product: Optional[str] = None,          # substring in a line item sku/title
    product_tag: Optional[str] = None,      # exact Shopify product tag on any line item
    province: Optional[str] = None,         # county (judet)
    city: Optional[str] = None,
    courier: Optional[str] = None,
    qty_min: Optional[int] = None,
    qty_max: Optional[int] = None,
    total_min: Optional[float] = None,
    total_max: Optional[float] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort: Optional[str] = None,             # date_desc(default)|date_asc|total_desc|total_asc|qty_desc|qty_asc|name_asc|name_desc
):
    """Paginated orders. `scope` = store set (org-scoped); `lens` = lifecycle filter tab
    (all | unfulfilled | fulfilled | in_transit | delivered | refused). Plus faceted filters +
    sort — see params."""
    per_page = min(max(per_page, 1), 200)
    page = max(page, 1)

    from services import org_service
    store_ids = await org_service.resolve_scope(db, store, scope)

    base = select(models.Order).where(models.Order.store_id.in_(store_ids))
    if q and q.strip():
        import crypto
        qs = q.strip()
        like = f"%{qs}%"
        # name (order #) and city stay plaintext; phone is encrypted so it's matched via its blind
        # index when the query looks like a phone number. Customer name is no longer free-text
        # searchable (encrypted) — CS looks up by phone / email / order # instead.
        conds = [models.Order.name.ilike(like), models.Order.shipping_city.ilike(like)]
        pbidx = crypto.phone_blind_index(qs)
        if pbidx:
            conds.append(models.Order.shipping_phone_bidx == pbidx)
        base = base.where(or_(*conds))
    if status and status.strip():
        base = base.where(models.Order.processing_status == status.strip())

    # Lifecycle lens (the filter tabs). AWB-existence + courier last_status decide the group.
    has_awb = select(models.Shipment.id).where(
        models.Shipment.order_id == models.Order.id, models.Shipment.awb.isnot(None))

    def _moving(*pats):
        return select(models.Shipment.id).where(
            models.Shipment.order_id == models.Order.id,
            or_(*[models.Shipment.last_status.ilike(p) for p in pats]))

    # Orders routed to CS (open backlog item) — used both to tag rows "Sent to CS" and to keep
    # them out of the "Unfulfilled" working queue.
    cs_open = select(models.CSQueueItem.id).where(
        models.CSQueueItem.order_id == models.Order.id, models.CSQueueItem.status != "solved")

    # Total ordered quantity per order (for the qty filter + sort).
    qty_sub = (select(func.coalesce(func.sum(models.LineItem.quantity), 0))
               .where(models.LineItem.order_id == models.Order.id).scalar_subquery())

    # --- Faceted filters ---
    # Locker detection moved to the precomputed Order.is_locker flag (address is encrypted).
    _COD = ["%cash on delivery%", "%cash_on_delivery%", "%cod%", "%ramburs%", "%numerar%"]

    if payment == "cod":
        base = base.where(or_(*[func.coalesce(models.Order.payment_gateway_names, "").ilike(p) for p in _COD],
                              func.lower(func.coalesce(models.Order.mapped_payment, "")).in_(["cod", "ramburs"])))
    elif payment == "card":
        base = base.where(
            ~or_(*[func.coalesce(models.Order.payment_gateway_names, "").ilike(p) for p in _COD]),
            or_(func.lower(func.coalesce(models.Order.financial_status, "")) == "paid",
                func.coalesce(models.Order.payment_gateway_names, "").ilike("%card%"),
                func.coalesce(models.Order.payment_gateway_names, "").ilike("%stripe%"),
                func.coalesce(models.Order.payment_gateway_names, "").ilike("%netopia%"),
                func.coalesce(models.Order.payment_gateway_names, "").ilike("%paypal%")))

    if delivery == "locker":
        # Address is encrypted; locker detection is precomputed into is_locker at ingest.
        base = base.where(models.Order.is_locker.is_(True))
    elif delivery == "home":
        base = base.where(or_(models.Order.is_locker.is_(False), models.Order.is_locker.is_(None)))

    if printed == "yes":
        base = base.where(select(models.Shipment.id).where(
            models.Shipment.order_id == models.Order.id, models.Shipment.awb.isnot(None),
            models.Shipment.printed_at.isnot(None)).exists())
    elif printed == "no":
        base = base.where(select(models.Shipment.id).where(
            models.Shipment.order_id == models.Order.id, models.Shipment.awb.isnot(None),
            models.Shipment.printed_at.is_(None)).exists())

    if invoiced == "yes":
        base = base.where(models.Order.invoice_number.isnot(None))
    elif invoiced == "no":
        base = base.where(models.Order.invoice_number.is_(None))

    if order_status == "unfulfilled":
        base = base.where(~has_awb.exists(), models.Order.cancelled_at.is_(None))
    elif order_status == "awb":
        base = base.where(has_awb.exists())
    elif order_status == "on_hold":
        base = base.where(models.Order.is_on_hold_shopify.is_(True))
    elif order_status == "cancelled":
        base = base.where(models.Order.cancelled_at.isnot(None))
    elif order_status == "cs":
        base = base.where(cs_open.exists())

    if delivery_status == "in_transit":
        base = base.where(_moving("%curs%", "%tranzit%", "%transit%", "%out for delivery%").exists(),
                          ~_moving("%livrat%", "%delivered%").exists())
    elif delivery_status == "delivered":
        base = base.where(_moving("%livrat%", "%delivered%").exists())
    elif delivery_status == "refused":
        base = base.where(or_(models.Order.cancelled_at.isnot(None),
                              _moving("%refuz%", "%retur%", "%return%").exists()))
    elif delivery_status == "none":
        base = base.where(~has_awb.exists())

    if address_status and address_status.strip():
        base = base.where(models.Order.address_status == address_status.strip())
    if tag and tag.strip():
        base = base.where(models.Order.tags.ilike(f"%{tag.strip()}%"))
    if product and product.strip():
        base = base.where(select(models.LineItem.id).where(
            models.LineItem.order_id == models.Order.id,
            or_(models.LineItem.sku.ilike(f"%{product.strip()}%"),
                models.LineItem.title.ilike(f"%{product.strip()}%"))).exists())
    if product_tag and product_tag.strip():
        pt = product_tag.strip().lower().replace("|", "")
        base = base.where(select(models.LineItem.id).where(
            models.LineItem.order_id == models.Order.id,
            models.LineItem.product_tags.ilike(f"%|{pt}|%")).exists())
    if province and province.strip():
        base = base.where(models.Order.shipping_province.ilike(f"%{province.strip()}%"))
    if city and city.strip():
        base = base.where(models.Order.shipping_city.ilike(f"%{city.strip()}%"))
    if courier and courier.strip():
        base = base.where(select(models.Shipment.id).where(
            models.Shipment.order_id == models.Order.id,
            models.Shipment.courier.ilike(f"%{courier.strip()}%")).exists())
    if qty_min is not None:
        base = base.where(qty_sub >= qty_min)
    if qty_max is not None:
        base = base.where(qty_sub <= qty_max)
    if total_min is not None:
        base = base.where(models.Order.total_price >= total_min)
    if total_max is not None:
        base = base.where(models.Order.total_price <= total_max)

    def _parse_dt(s):
        try:
            from datetime import datetime as _dt
            return _dt.fromisoformat(s.replace("Z", "+00:00")) if s else None
        except Exception:
            return None
    _df, _dt2 = _parse_dt(date_from), _parse_dt(date_to)
    if _df is not None:
        base = base.where(models.Order.created_at >= _df)
    if _dt2 is not None:
        if len((date_to or "")) <= 10:  # date-only → make the end day inclusive
            from datetime import timedelta
            base = base.where(models.Order.created_at < _dt2 + timedelta(days=1))
        else:
            base = base.where(models.Order.created_at <= _dt2)

    # Predicatele lentilelor definite O SINGURA DATA: le folosesc si pentru filtrare, si pentru numaratoarea
    # per tab. Doua liste separate ar diverge tacut, iar utilizatorul ar vedea un numar care nu se potriveste
    # cu randurile de sub el.
    _LENS_PREDICATES = {
        "unfulfilled": lambda q: q.where(models.Order.cancelled_at.is_(None), ~has_awb.exists(),
                                         ~cs_open.exists()),
        "fulfilled":   lambda q: q.where(has_awb.exists()),
        "in_transit":  lambda q: q.where(
            _moving("%curs%", "%tranzit%", "%transit%", "%out for delivery%").exists(),
            ~_moving("%livrat%", "%delivered%").exists()),
        "delivered":   lambda q: q.where(_moving("%livrat%", "%delivered%").exists()),
        "refused":     lambda q: q.where(or_(models.Order.cancelled_at.isnot(None),
                                             _moving("%refuz%", "%retur%", "%return%").exists())),
    }
    base_prelens = base          # snapshot: aceleasi filtre (cautare, date, magazin), FARA lentila
    lens_v = (lens or "").strip().lower()
    if lens_v in _LENS_PREDICATES:
        base = _LENS_PREDICATES[lens_v](base)

    _SORTS = {
        "date_desc": desc(models.Order.created_at), "date_asc": asc(models.Order.created_at),
        "total_desc": desc(models.Order.total_price), "total_asc": asc(models.Order.total_price),
        "qty_desc": desc(qty_sub), "qty_asc": asc(qty_sub),
        "name_asc": asc(models.Order.name), "name_desc": desc(models.Order.name),
    }
    order_by = _SORTS.get((sort or "").strip().lower(), desc(models.Order.created_at))

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    rows = (await db.execute(
        base.options(selectinload(models.Order.shipments), selectinload(models.Order.store),
                     selectinload(models.Order.line_items))
        .order_by(order_by)
        .offset((page - 1) * per_page)
        .limit(per_page)
    )).scalars().all()

    row_ids = [o.id for o in rows]
    in_cs_ids: set = set()
    if row_ids:
        in_cs_ids = set((await db.execute(
            select(models.CSQueueItem.order_id).where(
                models.CSQueueItem.order_id.in_(row_ids), models.CSQueueItem.status != "solved")
        )).scalars().all())

    # Numarul pe FIECARE tab, cu filtrele curente aplicate ("cate comenzi am in view-ul asta").
    # Optional: 6 COUNT-uri in plus, cerute de UI o data per schimbare de filtru, nu la fiecare pagina.
    lens_counts = None
    if with_lens_counts:
        lens_counts = {"all": (await db.execute(
            select(func.count()).select_from(base_prelens.subquery()))).scalar() or 0}
        for _k, _f in _LENS_PREDICATES.items():
            lens_counts[_k] = (await db.execute(
                select(func.count()).select_from(_f(base_prelens).subquery()))).scalar() or 0

    return {
        "orders": [_order_json(o, o.id in in_cs_ids) for o in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "lens_counts": lens_counts,
    }


@router.get("/product-tags")
async def product_tags_list(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
    scope: Optional[str] = None,
):
    """Distinct Shopify product tags seen across orders in scope — powers the Orders product-tag
    filter dropdown. Unnests the pipe-delimited `line_items.product_tags`."""
    from services import org_service
    store_ids = await org_service.resolve_scope(db, store, scope)
    tag_col = func.unnest(func.string_to_array(func.btrim(models.LineItem.product_tags, "|"), "|"))
    inner = (
        select(tag_col.label("t"))
        .select_from(models.LineItem)
        .join(models.Order, models.Order.id == models.LineItem.order_id)
        .where(models.Order.store_id.in_(store_ids), models.LineItem.product_tags.isnot(None))
    ).subquery()
    rows = (await db.execute(
        select(inner.c.t).where(inner.c.t != "").distinct().order_by(inner.c.t)
    )).scalars().all()
    return {"tags": list(rows)}


@router.post("/orders/backfill-product-tags")
async def backfill_product_tags(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
    scope: Optional[str] = None,
    limit: int = 3000,
):
    """Populate Shopify product tags for line items that don't have them yet (existing orders synced
    before this feature). Groups SKUs by their order's store and queries each store's own catalog."""
    from services import org_service, shopify_service
    store_ids = await org_service.resolve_scope(db, store, scope)
    limit = min(max(limit, 1), 10000)
    rows = (await db.execute(
        select(models.LineItem, models.Order.store_id)
        .join(models.Order, models.Order.id == models.LineItem.order_id)
        .where(models.Order.store_id.in_(store_ids),
               models.LineItem.product_tags.is_(None),
               models.LineItem.sku.isnot(None))
        .limit(limit)
    )).all()
    by_store: dict = {}
    for li, sid in rows:
        by_store.setdefault(sid, {}).setdefault((li.sku or "").strip().lower(), []).append(li)
    if not by_store:
        return {"updated": 0, "scanned": 0}
    stores = {s.id: s for s in (await db.execute(
        select(models.Store).where(models.Store.id.in_(list(by_store.keys())))
    )).scalars().all()}
    updated = 0
    for sid, sku_map in by_store.items():
        st = stores.get(sid)
        if not st:
            continue
        try:
            tag_map = await shopify_service.get_variant_product_tags(st, list(sku_map.keys()))
        except Exception:
            continue
        for sku_l, lis in sku_map.items():
            val = tag_map.get(sku_l)
            if val is not None:
                for li in lis:
                    li.product_tags = val
                    updated += 1
    await db.commit()
    return {"updated": updated, "scanned": len(rows)}


@router.get("/picking-list")
async def picking_list(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
    scope: Optional[str] = None,
    lens: str = "awb",              # awb (ready to pack) | unfulfilled (to make AWBs) | all
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    """Warehouse pick/pack list. Returns an AGGREGATE (each SKU × total quantity across the batch,
    for picking) plus the PER-ORDER breakdown (the packing slip for each parcel). Org-scoped +
    filterable by lens and date."""
    if scope == "all":
        store_ids = await org_service.org_store_ids(db, store)
    elif scope and scope.startswith("group:"):
        store_ids = await org_service.org_store_ids(db, store, group=scope.split(":", 1)[1] or None)
    else:
        store_ids = [store.id]

    base = select(models.Order).where(
        models.Order.store_id.in_(store_ids), models.Order.cancelled_at.is_(None))
    has_awb = select(models.Shipment.id).where(
        models.Shipment.order_id == models.Order.id, models.Shipment.awb.isnot(None))
    if lens == "awb":
        base = base.where(has_awb.exists())
    elif lens == "unfulfilled":
        base = base.where(~has_awb.exists())

    def _parse(s: Optional[str]):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None
    df, dt = _parse(date_from), _parse(date_to)
    if df:
        base = base.where(models.Order.created_at >= df)
    if dt:
        base = base.where(models.Order.created_at <= dt)

    rows = (await db.execute(
        base.options(selectinload(models.Order.line_items), selectinload(models.Order.shipments),
                     selectinload(models.Order.store))
        .order_by(desc(models.Order.created_at)).limit(1000)
    )).scalars().all()

    agg: dict = {}
    orders_out = []
    total_units = 0
    for o in rows:
        s = o.shipments[-1] if o.shipments else None
        items = []
        for li in (o.line_items or []):
            q = int(li.quantity or 0)
            total_units += q
            key = (li.sku or "").strip() or (li.title or "—")
            a = agg.setdefault(key, {"sku": li.sku, "title": li.title, "total_qty": 0, "_orders": set()})
            a["total_qty"] += q
            a["_orders"].add(o.id)
            items.append({"sku": li.sku, "title": li.title, "quantity": li.quantity})
        orders_out.append({
            "id": o.id, "name": o.name, "city": o.shipping_city, "customer": o.customer,
            "awb": (s.awb if s else None), "courier": (s.courier if s else None),
            "printed": bool(s and s.printed_at), "store_name": (o.store.name if o.store else None),
            "items": items,
        })
    aggregate = [{"sku": v["sku"], "title": v["title"], "total_qty": v["total_qty"],
                  "order_count": len(v["_orders"])} for v in agg.values()]
    # Attach each SKU's warehouse pick location (from packing rules) so the picker walks in order.
    skus = [a["sku"] for a in aggregate if a["sku"]]
    loc_by_sku: dict = {}
    if skus:
        prs = (await db.execute(select(models.PackingRule).where(
            models.PackingRule.sku.in_(skus),
            (models.PackingRule.store_id.in_(store_ids)) | (models.PackingRule.store_id.is_(None)),
        ))).scalars().all()
        loc_by_sku = {(r.sku or "").strip().lower(): r for r in prs}
    for a in aggregate:
        r = loc_by_sku.get((a["sku"] or "").strip().lower())
        a["location"] = r.location if r else None
        a["shelf"] = r.shelf if r else None
        a["shelf_position"] = r.shelf_position if r else None
    # Sort by location → shelf → position so the pick walk is ordered; unplaced SKUs (no location) last.
    def _lk(a):
        return (0 if a["location"] else 1, (a["location"] or "").lower(),
                (a["shelf"] or "").lower(), (a["shelf_position"] or "").lower(), -a["total_qty"])
    aggregate.sort(key=_lk)
    return {"aggregate": aggregate, "orders": orders_out,
            "total_orders": len(orders_out), "total_units": total_units, "total_skus": len(aggregate)}


def _picking_list_json(pl: models.PickingList) -> dict:
    return {
        "id": pl.id, "name": pl.name, "lens": pl.lens, "scope": pl.scope,
        "order_ids": list(pl.order_ids or []),
        "total_orders": pl.total_orders or 0, "total_units": pl.total_units or 0,
        "total_skus": pl.total_skus or 0, "status": pl.status or "open",
        "created_at": pl.created_at.isoformat() if pl.created_at else None,
        "completed_at": pl.completed_at.isoformat() if pl.completed_at else None,
    }


@router.get("/picking/lists")
async def picking_lists(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = None,      # open | picked | cancelled — omit for all
    limit: int = 50,
):
    """The merchant's saved picking runs, newest first."""
    ids = await org_service.org_store_ids(db, store)
    stmt = select(models.PickingList).where(models.PickingList.store_id.in_(ids))
    if status:
        stmt = stmt.where(models.PickingList.status == status)
    rows = (await db.execute(
        stmt.order_by(desc(models.PickingList.created_at)).limit(min(max(limit, 1), 200))
    )).scalars().all()
    return {"lists": [_picking_list_json(p) for p in rows]}


@router.post("/picking/lists")
async def create_picking_list(
    payload: Dict[str, Any] = Body(default={}),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Freeze the current pick batch into a named list. Body: {name?, lens?, scope?}.
    The order ids are snapshotted so the picker's list can't shift as new orders sync in."""
    lens = str(payload.get("lens") or "awb")
    scope = payload.get("scope") or None
    built = await picking_list(store=store, db=db, scope=scope, lens=lens)
    order_ids = [o["id"] for o in built["orders"]]
    if not order_ids:
        raise HTTPException(400, "Nothing to pick in that batch right now.")
    name = (payload.get("name") or "").strip() or (
        f"{datetime.now().strftime('%d %b %H:%M')} · {len(order_ids)} orders")
    pl = models.PickingList(
        store_id=store.id, name=name[:255], lens=lens, scope=(scope or "store"),
        order_ids=order_ids, total_orders=built["total_orders"],
        total_units=built["total_units"], total_skus=built["total_skus"], status="open",
    )
    db.add(pl)
    await db.commit()
    await db.refresh(pl)
    return {"success": True, "list": _picking_list_json(pl)}


@router.get("/picking/lists/{list_id}")
async def get_picking_list_detail(
    list_id: int,
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """One saved list, rebuilt from its snapshot: the SKU aggregate to pick + the per-order slips."""
    ids = await org_service.org_store_ids(db, store)
    pl = (await db.execute(select(models.PickingList).where(
        models.PickingList.id == list_id, models.PickingList.store_id.in_(ids)))).scalar_one_or_none()
    if not pl:
        raise HTTPException(404, "Picking list not found.")
    rows = (await db.execute(
        select(models.Order)
        .options(selectinload(models.Order.line_items), selectinload(models.Order.shipments),
                 selectinload(models.Order.store))
        .where(models.Order.id.in_(list(pl.order_ids or [])))
    )).scalars().all()
    agg: dict = {}
    orders_out = []
    total_units = 0
    for o in rows:
        s = o.shipments[-1] if o.shipments else None
        items = []
        for li in (o.line_items or []):
            q = int(li.quantity or 0)
            total_units += q
            key = (li.sku or "").strip() or (li.title or "—")
            a = agg.setdefault(key, {"sku": li.sku, "title": li.title, "total_qty": 0, "_orders": set()})
            a["total_qty"] += q
            a["_orders"].add(o.id)
            items.append({"sku": li.sku, "title": li.title, "quantity": li.quantity})
        orders_out.append({
            "id": o.id, "name": o.name, "city": o.shipping_city, "customer": o.customer,
            "awb": (s.awb if s else None), "courier": (s.courier if s else None),
            "printed": bool(s and s.printed_at), "store_name": (o.store.name if o.store else None),
            "items": items,
        })
    aggregate = [{"sku": v["sku"], "title": v["title"], "total_qty": v["total_qty"],
                  "order_count": len(v["_orders"]), "location": None, "shelf": None,
                  "shelf_position": None} for v in agg.values()]
    return {"list": _picking_list_json(pl), "aggregate": aggregate, "orders": orders_out,
            "total_orders": len(orders_out), "total_units": total_units, "total_skus": len(aggregate)}


@router.post("/picking/lists/{list_id}/status")
async def set_picking_list_status(
    list_id: int,
    payload: Dict[str, Any] = Body(default={}),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Mark a list picked (done) or cancelled."""
    new = str(payload.get("status") or "picked")
    if new not in ("open", "picked", "cancelled"):
        raise HTTPException(400, "status must be open, picked or cancelled.")
    ids = await org_service.org_store_ids(db, store)
    pl = (await db.execute(select(models.PickingList).where(
        models.PickingList.id == list_id, models.PickingList.store_id.in_(ids)))).scalar_one_or_none()
    if not pl:
        raise HTTPException(404, "Picking list not found.")
    pl.status = new
    pl.completed_at = datetime.now(timezone.utc) if new in ("picked", "cancelled") else None
    await db.commit()
    return {"success": True, "list": _picking_list_json(pl)}


@router.get("/orders/{order_id}")
async def order_detail(
    order_id: int,
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Full detail for one order — address + validation, line items, and every shipment with its
    live tracking status. Org-aware (a linked store's order is visible in the portfolio)."""
    from services import org_service
    from services.status_sync_service import _tracking_url
    allowed = await org_service.org_store_ids(db, store)
    o = (await db.execute(
        select(models.Order)
        .options(
            selectinload(models.Order.line_items),
            selectinload(models.Order.shipments),
            selectinload(models.Order.address_validations),
            selectinload(models.Order.store),
        )
        .where(models.Order.id == order_id, models.Order.store_id.in_(allowed))
    )).scalar_one_or_none()
    if not o:
        raise HTTPException(404, "Order not found")

    latest_val = max(o.address_validations, key=lambda v: v.id) if o.address_validations else None

    def _ship(s: models.Shipment) -> dict:
        csd = s.courier_specific_data if isinstance(s.courier_specific_data, dict) else {}
        return {
            "id": s.id,
            "awb": s.awb,
            "courier": s.courier,
            "account_key": s.account_key,
            "last_status": s.last_status,
            "last_status_at": s.last_status_at.isoformat() if s.last_status_at else None,
            "derived_status": s.derived_status,
            "printed": bool(s.printed_at),
            "location": csd.get("location") if csd else None,
            "tracking_url": _tracking_url(s.courier, s.awb) if s.awb else None,
        }

    return {
        "id": o.id, "name": o.name, "customer": o.customer,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "total_price": o.total_price, "financial_status": o.financial_status,
        "financial_paid": (o.financial_status or "").lower() == "paid",
        "processing_status": o.processing_status, "derived_status": o.derived_status,
        "shopify_status": o.shopify_status,
        "fulfilled_at": o.fulfilled_at.isoformat() if o.fulfilled_at else None,
        "cancelled_at": o.cancelled_at.isoformat() if o.cancelled_at else None,
        "tags": o.tags, "note": o.note,
        "invoice_number": (f"{o.invoice_series or ''}{o.invoice_number}" if o.invoice_number else None),
        "invoice_url": o.invoice_url,
        "store_name": o.store.name if o.store else None,
        "store_domain": o.store.domain if o.store else None,
        "address": {
            "status": o.address_status, "score": o.address_score,
            "name": o.shipping_name, "email": o.shipping_email, "phone": o.shipping_phone,
            "address1": o.shipping_address1, "address2": o.shipping_address2,
            "city": o.shipping_city, "zip": o.shipping_zip,
            "province": o.shipping_province, "country": o.shipping_country,
            "errors": o.address_validation_errors or (latest_val.errors if latest_val else None),
            "suggestions": latest_val.suggestions if latest_val else None,
        },
        "line_items": [{"sku": li.sku, "title": li.title, "quantity": li.quantity} for li in o.line_items],
        "shipments": [_ship(s) for s in sorted(o.shipments, key=lambda s: s.id)],
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


# ---- Automation settings (status sync + refusal auto-cancel) ----

_AUTOMATION_FLAGS = (
    "status_sync_enabled",
    "fulfill_notify_customer",
    "auto_cancel_on_refusal",
    "refusal_notify_customer",
    "refusal_restock",
)


def _automation_json(store: models.Store) -> dict:
    d = {k: bool(getattr(store, k)) for k in _AUTOMATION_FLAGS}
    d["sender_name"] = getattr(store, "sender_name", None)
    d["content_template"] = getattr(store, "content_template", None)
    d["packing_metafield"] = getattr(store, "packing_metafield", None)
    d["packing_per_product_tag"] = getattr(store, "packing_per_product_tag", None)
    d["default_pieces_per_parcel"] = getattr(store, "default_pieces_per_parcel", None)
    d["packing_rounding"] = getattr(store, "packing_rounding", None) or "shared"
    d["default_box_id"] = getattr(store, "default_box_id", None)
    d["auto_awb_enabled"] = bool(getattr(store, "auto_awb_enabled", False))
    d["auto_awb_account_key"] = getattr(store, "auto_awb_account_key", None)
    d["auto_awb_profile_id"] = getattr(store, "auto_awb_profile_id", None)
    d["awb_window_start"] = getattr(store, "awb_window_start", None)
    d["awb_window_end"] = getattr(store, "awb_window_end", None)
    d["auto_awb_delay_minutes"] = getattr(store, "auto_awb_delay_minutes", None)
    return d


def _as_int_or_none(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _as_float_or_none(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


@router.get("/settings/automation")
async def get_automation_settings(store: models.Store = Depends(require_shop)):
    """Lifecycle-automation toggles + sender name for the Settings screen."""
    return _automation_json(store)


@router.put("/settings/automation")
async def update_automation_settings(
    payload: dict,
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Update the automation toggles and the per-store sender name."""
    changed = False
    for k in _AUTOMATION_FLAGS:
        if k in payload:
            setattr(store, k, bool(payload[k]))
            changed = True
    if "sender_name" in payload:
        v = (payload.get("sender_name") or "").strip()
        store.sender_name = v or None
        changed = True
    if "content_template" in payload:
        v = (payload.get("content_template") or "").strip()
        store.content_template = v or None
        changed = True
    if "packing_metafield" in payload:
        v = (payload.get("packing_metafield") or "").strip()
        store.packing_metafield = v or None
        changed = True
    if "packing_per_product_tag" in payload:
        v = (payload.get("packing_per_product_tag") or "").strip()
        store.packing_per_product_tag = v or None
        changed = True
    if "default_pieces_per_parcel" in payload:
        store.default_pieces_per_parcel = _as_float_or_none(payload.get("default_pieces_per_parcel"))
        changed = True
    if "packing_rounding" in payload:
        v = (payload.get("packing_rounding") or "shared").strip().lower()
        store.packing_rounding = "per_product" if v == "per_product" else "shared"
        changed = True
    if "default_box_id" in payload:
        store.default_box_id = _as_int_or_none(payload.get("default_box_id"))
        changed = True
    if "auto_awb_enabled" in payload:
        store.auto_awb_enabled = bool(payload["auto_awb_enabled"])
        changed = True
    if "auto_awb_account_key" in payload:
        v = (payload.get("auto_awb_account_key") or "").strip()
        store.auto_awb_account_key = v or None
        changed = True
    if "auto_awb_profile_id" in payload:
        pid = _as_int_or_none(payload.get("auto_awb_profile_id"))
        if pid is not None:
            # Validate the profile belongs to this org before pinning it to auto-AWB.
            await _owned_profile(db, store, pid)
        store.auto_awb_profile_id = pid
        changed = True
    for k in ("awb_window_start", "awb_window_end", "auto_awb_delay_minutes"):
        if k in payload:
            setattr(store, k, _as_int_or_none(payload[k]))
            changed = True
    if changed:
        await db.commit()
    return _automation_json(store)


@router.get("/lockers")
async def lockers(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
    courier: Optional[str] = None,
    q: Optional[str] = None,
    city: Optional[str] = None,
    county: Optional[str] = None,
    limit: int = 60,
):
    """Live locker / pickup-point network from the shop's own courier accounts, filtered for
    the picker. Cached per courier (12h); one courier failing never breaks the list."""
    from services import lockers as locker_service
    items = await locker_service.search_lockers(
        db, store, courier=courier, q=q or "", city=city or "", county=county or "",
        limit=min(max(limit, 1), 20000),  # the map picker requests the whole network at once
    )
    return {"lockers": items, "total": len(items)}


@router.post("/awb/sync-statuses")
async def sync_statuses(store: models.Store = Depends(require_shop)):
    """Manually run a courier-status → Shopify sync pass for THIS shop's shipments now
    (the background loop also does this on a schedule)."""
    from services import status_sync_service
    res = await status_sync_service.poll(store_id=store.id, limit=200)
    return {"status": "ok", **res}


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
        "profiles": [_profile_json(p) for p in profiles],
    }


def _profile_json(p: models.ShipmentProfile) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "account_key": p.account_key,
        "default_parcels": p.default_parcels,
        "default_weight_kg": p.default_weight_kg,
        "default_length_cm": p.default_length_cm,
        "default_width_cm": p.default_width_cm,
        "default_height_cm": p.default_height_cm,
        "default_service_id": p.default_service_id,
        "default_payer": p.default_payer,
        "default_packing": p.default_packing,
        "default_label_size": p.default_label_size,
        "content_template": p.content_template,
    }


def _profile_fields(payload: dict) -> dict:
    """Coerce a JSON profile payload into ShipmentProfile columns (only the writable ones)."""
    def _i(v):
        try:
            return int(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    def _f(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None
    return {
        "name": (payload.get("name") or "").strip(),
        "account_key": (payload.get("account_key") or "").strip(),
        "default_parcels": _i(payload.get("default_parcels")) or 1,
        "default_weight_kg": _f(payload.get("default_weight_kg")) or 1.0,
        "default_length_cm": _i(payload.get("default_length_cm")),
        "default_width_cm": _i(payload.get("default_width_cm")),
        "default_height_cm": _i(payload.get("default_height_cm")),
        "default_service_id": _i(payload.get("default_service_id")),
        "default_payer": (payload.get("default_payer") or "").strip() or None,
        "default_packing": (payload.get("default_packing") or "").strip() or None,
        "default_label_size": (payload.get("default_label_size") or "").strip() or None,
        "content_template": (payload.get("content_template") or "").strip() or None,
    }


async def _owned_profile(db: AsyncSession, store: models.Store, profile_id: int) -> models.ShipmentProfile:
    """A profile is editable if it belongs to a store in the requester's org (or is a shared
    NULL-store profile). Prevents cross-tenant edits."""
    allowed = await org_service.org_store_ids(db, store)
    prof = (await db.execute(
        select(models.ShipmentProfile).where(
            models.ShipmentProfile.id == profile_id,
            (models.ShipmentProfile.store_id.in_(allowed)) | (models.ShipmentProfile.store_id.is_(None)),
        )
    )).scalar_one_or_none()
    if not prof:
        raise HTTPException(404, "Shipment profile not found.")
    return prof


@router.post("/couriers/profiles")
async def create_profile(
    payload: dict,
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Create a saved shipment profile owned by this store. Body = profile fields."""
    from sqlalchemy.exc import IntegrityError
    fields = _profile_fields(payload)
    if not fields["name"] or not fields["account_key"]:
        raise HTTPException(400, "name and account_key are required.")
    prof = models.ShipmentProfile(store_id=store.id, **fields)
    db.add(prof)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(400, f"A profile named \u201c{fields['name']}\u201d already exists.")
    await db.refresh(prof)
    return _profile_json(prof)


@router.put("/couriers/profiles/{profile_id}")
async def update_profile(
    profile_id: int,
    payload: dict,
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy.exc import IntegrityError
    prof = await _owned_profile(db, store, profile_id)
    fields = _profile_fields(payload)
    if not fields["name"] or not fields["account_key"]:
        raise HTTPException(400, "name and account_key are required.")
    for k, v in fields.items():
        setattr(prof, k, v)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(400, f"A profile named \u201c{fields['name']}\u201d already exists.")
    await db.refresh(prof)
    return _profile_json(prof)


@router.delete("/couriers/profiles/{profile_id}")
async def delete_profile(
    profile_id: int,
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    prof = await _owned_profile(db, store, profile_id)
    await db.delete(prof)
    await db.commit()
    return {"success": True, "deleted": profile_id}


# ---- Automation routing rules (conditions → profile) ----

def _rule_json(r: models.ShipmentRule) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "priority": r.priority,
        "enabled": bool(r.enabled),
        "conditions": r.conditions or {},
        "profile_id": r.profile_id,
    }


async def _owned_rule(db: AsyncSession, store: models.Store, rule_id: int) -> models.ShipmentRule:
    allowed = await org_service.org_store_ids(db, store)
    r = (await db.execute(
        select(models.ShipmentRule).where(
            models.ShipmentRule.id == rule_id,
            (models.ShipmentRule.store_id.in_(allowed)) | (models.ShipmentRule.store_id.is_(None)),
        )
    )).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Rule not found.")
    return r


async def _rule_fields(db: AsyncSession, store: models.Store, payload: dict) -> dict:
    name = (payload.get("name") or "").strip()
    profile_id = _as_int_or_none(payload.get("profile_id"))
    if not name or profile_id is None:
        raise HTTPException(400, "name and profile_id are required.")
    await _owned_profile(db, store, profile_id)  # the rule's action profile must belong to this org
    return {
        "name": name,
        "priority": _as_int_or_none(payload.get("priority")) if payload.get("priority") not in (None, "") else 100,
        "enabled": bool(payload.get("enabled", True)),
        "conditions": rules_svc.clean_conditions(payload.get("conditions")),
        "profile_id": profile_id,
    }


@router.get("/shipment-rules")
async def list_rules(store: models.Store = Depends(require_shop), db: AsyncSession = Depends(get_db)):
    allowed = await org_service.org_store_ids(db, store)
    rows = (await db.execute(
        select(models.ShipmentRule).where(
            (models.ShipmentRule.store_id.in_(allowed)) | (models.ShipmentRule.store_id.is_(None)),
        ).order_by(models.ShipmentRule.priority.asc(), models.ShipmentRule.id.asc())
    )).scalars().all()
    return {"rules": [_rule_json(r) for r in rows]}


@router.post("/shipment-rules")
async def create_rule(payload: dict, store: models.Store = Depends(require_shop), db: AsyncSession = Depends(get_db)):
    fields = await _rule_fields(db, store, payload)
    rule = models.ShipmentRule(store_id=store.id, **fields)
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return _rule_json(rule)


@router.put("/shipment-rules/{rule_id}")
async def update_rule(rule_id: int, payload: dict, store: models.Store = Depends(require_shop), db: AsyncSession = Depends(get_db)):
    rule = await _owned_rule(db, store, rule_id)
    fields = await _rule_fields(db, store, payload)
    for k, v in fields.items():
        setattr(rule, k, v)
    await db.commit()
    await db.refresh(rule)
    return _rule_json(rule)


@router.delete("/shipment-rules/{rule_id}")
async def delete_rule(rule_id: int, store: models.Store = Depends(require_shop), db: AsyncSession = Depends(get_db)):
    rule = await _owned_rule(db, store, rule_id)
    await db.delete(rule)
    await db.commit()
    return {"success": True, "deleted": rule_id}


# ---- Parcel packing: box types ----

def _box_json(b: models.PackingBox) -> dict:
    return {"id": b.id, "name": b.name, "box_type": b.box_type,
            "length_cm": b.length_cm, "width_cm": b.width_cm, "height_cm": b.height_cm}


def _box_fields(payload: dict) -> dict:
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required.")
    bt = (payload.get("box_type") or "BOX").strip().upper()
    return {
        "name": name,
        "box_type": "ENVELOPE" if bt == "ENVELOPE" else "BOX",
        "length_cm": _as_int_or_none(payload.get("length_cm")),
        "width_cm": _as_int_or_none(payload.get("width_cm")),
        "height_cm": _as_int_or_none(payload.get("height_cm")),
    }


async def _owned_box(db: AsyncSession, store: models.Store, box_id: int) -> models.PackingBox:
    allowed = await org_service.org_store_ids(db, store)
    b = (await db.execute(select(models.PackingBox).where(
        models.PackingBox.id == box_id,
        (models.PackingBox.store_id.in_(allowed)) | (models.PackingBox.store_id.is_(None)),
    ))).scalar_one_or_none()
    if not b:
        raise HTTPException(404, "Box not found.")
    return b


@router.get("/packing-boxes")
async def list_boxes(store: models.Store = Depends(require_shop), db: AsyncSession = Depends(get_db)):
    allowed = await org_service.org_store_ids(db, store)
    rows = (await db.execute(select(models.PackingBox).where(
        (models.PackingBox.store_id.in_(allowed)) | (models.PackingBox.store_id.is_(None)),
    ).order_by(models.PackingBox.name))).scalars().all()
    return {"boxes": [_box_json(b) for b in rows]}


@router.post("/packing-boxes")
async def create_box(payload: dict, store: models.Store = Depends(require_shop), db: AsyncSession = Depends(get_db)):
    b = models.PackingBox(store_id=store.id, **_box_fields(payload))
    db.add(b)
    await db.commit()
    await db.refresh(b)
    return _box_json(b)


@router.put("/packing-boxes/{box_id}")
async def update_box(box_id: int, payload: dict, store: models.Store = Depends(require_shop), db: AsyncSession = Depends(get_db)):
    b = await _owned_box(db, store, box_id)
    for k, v in _box_fields(payload).items():
        setattr(b, k, v)
    await db.commit()
    await db.refresh(b)
    return _box_json(b)


@router.delete("/packing-boxes/{box_id}")
async def delete_box(box_id: int, store: models.Store = Depends(require_shop), db: AsyncSession = Depends(get_db)):
    b = await _owned_box(db, store, box_id)
    await db.delete(b)
    await db.commit()
    return {"success": True, "deleted": box_id}


# ---- Parcel packing: per-product rules (upsert by SKU, supports BULK) ----

def _packrule_json(r: models.PackingRule) -> dict:
    return {"id": r.id, "sku": r.sku, "title": r.title, "image_url": r.image_url,
            "pieces_per_parcel": r.pieces_per_parcel, "weight_kg": r.weight_kg, "box_id": r.box_id,
            "location": r.location, "shelf": r.shelf, "shelf_position": r.shelf_position}


@router.get("/packing-rules")
async def list_packrules(store: models.Store = Depends(require_shop), db: AsyncSession = Depends(get_db)):
    allowed = await org_service.org_store_ids(db, store)
    rows = (await db.execute(select(models.PackingRule).where(
        (models.PackingRule.store_id.in_(allowed)) | (models.PackingRule.store_id.is_(None)),
    ).order_by(models.PackingRule.sku))).scalars().all()
    return {"rules": [_packrule_json(r) for r in rows]}


@router.post("/packing-rules")
async def upsert_packrules(payload: dict, store: models.Store = Depends(require_shop), db: AsyncSession = Depends(get_db)):
    """Upsert one or many packing rules BY SKU (this is the bulk-apply endpoint too).
    Body: a single rule object, or {rules: [ {sku, title?, image_url?, pieces_per_parcel?,
    weight_kg?, box_id?}, ... ]}. Only the provided keys are written."""
    raw = payload.get("rules") if isinstance(payload.get("rules"), list) else [payload]
    allowed = await org_service.org_store_ids(db, store)
    out: list = []
    for item in raw:
        sku = (item.get("sku") or "").strip()
        if not sku:
            continue
        r = (await db.execute(select(models.PackingRule).where(
            models.PackingRule.sku == sku,
            (models.PackingRule.store_id.in_(allowed)) | (models.PackingRule.store_id.is_(None)),
        ))).scalar_one_or_none()
        if not r:
            r = models.PackingRule(store_id=store.id, sku=sku)
            db.add(r)
        if "title" in item:
            r.title = (item.get("title") or None)
        if "image_url" in item:
            r.image_url = (item.get("image_url") or None)
        if "pieces_per_parcel" in item:
            r.pieces_per_parcel = _as_float_or_none(item.get("pieces_per_parcel"))
        if "weight_kg" in item:
            r.weight_kg = _as_float_or_none(item.get("weight_kg"))
        if "box_id" in item:
            r.box_id = _as_int_or_none(item.get("box_id"))
        for f in ("location", "shelf", "shelf_position"):
            if f in item:
                setattr(r, f, (str(item.get(f)).strip() or None) if item.get(f) is not None else None)
        out.append(r)
    await db.commit()
    for r in out:
        await db.refresh(r)
    return {"rules": [_packrule_json(r) for r in out]}


@router.delete("/packing-rules/{rule_id}")
async def delete_packrule(rule_id: int, store: models.Store = Depends(require_shop), db: AsyncSession = Depends(get_db)):
    allowed = await org_service.org_store_ids(db, store)
    r = (await db.execute(select(models.PackingRule).where(
        models.PackingRule.id == rule_id,
        (models.PackingRule.store_id.in_(allowed)) | (models.PackingRule.store_id.is_(None)),
    ))).scalar_one_or_none()
    if not r:
        raise HTTPException(404, "Packing rule not found.")
    await db.delete(r)
    await db.commit()
    return {"success": True, "deleted": rule_id}


# ---- Product browser (Shopify catalog) for the packing screen ----

@router.get("/products")
async def list_products_ep(q: Optional[str] = None, cursor: Optional[str] = None,
                           store: models.Store = Depends(require_shop), db: AsyncSession = Depends(get_db)):
    """Shop products (photo · title · SKU · weight) for the packing browser, each variant merged
    with its saved packing rule (pieces/box). Requires read_inventory for the weight field."""
    from services import shopify_service
    res = await shopify_service.list_products(store, q=q, cursor=cursor, limit=50)
    allowed = await org_service.org_store_ids(db, store)
    rules = {(r.sku or "").strip().lower(): r for r in (await db.execute(select(models.PackingRule).where(
        (models.PackingRule.store_id.in_(allowed)) | (models.PackingRule.store_id.is_(None)),
    ))).scalars().all() if (r.sku or "").strip()}
    for p in res["products"]:
        for v in p["variants"]:
            r = rules.get((v.get("sku") or "").strip().lower())
            v["pieces_per_parcel"] = r.pieces_per_parcel if r else None
            v["box_id"] = r.box_id if r else None
            v["location"] = r.location if r else None
            v["shelf"] = r.shelf if r else None
            v["shelf_position"] = r.shelf_position if r else None
    return res


@router.post("/products/packing")
async def save_product_packing(payload: dict, store: models.Store = Depends(require_shop), db: AsyncSession = Depends(get_db)):
    """Save packing from the browser (single or BULK): upsert the per-SKU rule and, when a weight
    + inventory-item id is given, write the weight back to Shopify (needs write_inventory)."""
    from services import shopify_service
    items = payload.get("items") or []
    allowed = await org_service.org_store_ids(db, store)
    for it in items:
        sku = (it.get("sku") or "").strip()
        if not sku:
            continue
        r = (await db.execute(select(models.PackingRule).where(
            models.PackingRule.sku == sku,
            (models.PackingRule.store_id.in_(allowed)) | (models.PackingRule.store_id.is_(None)),
        ))).scalar_one_or_none()
        if not r:
            r = models.PackingRule(store_id=store.id, sku=sku)
            db.add(r)
        if "title" in it:
            r.title = it.get("title") or None
        if "image_url" in it:
            r.image_url = it.get("image_url") or None
        if "pieces_per_parcel" in it:
            r.pieces_per_parcel = _as_float_or_none(it.get("pieces_per_parcel"))
        if "box_id" in it:
            r.box_id = _as_int_or_none(it.get("box_id"))
        if "weight_kg" in it:
            r.weight_kg = _as_float_or_none(it.get("weight_kg"))
        for f in ("location", "shelf", "shelf_position"):
            if f in it:
                setattr(r, f, (str(it.get(f)).strip() or None) if it.get(f) is not None else None)
    await db.commit()
    written, errors = 0, []
    for it in items:
        w = _as_float_or_none(it.get("weight_kg"))
        iid = it.get("inventory_item_id")
        if w is not None and iid:
            try:
                await shopify_service.set_variant_weight(store, iid, w)
                written += 1
            except Exception as e:
                errors.append({"sku": it.get("sku"), "error": str(e)})
    return {"saved": len(items), "weights_written": written, "errors": errors}


# ---- Product barcodes: generate + write to Shopify + printable visual labels ----
@router.post("/products/barcodes/set")
async def set_product_barcodes(payload: dict, store: models.Store = Depends(require_shop)):
    """Set or GENERATE barcodes on Shopify variants (needs write_products). Body:
    {items:[{product_id, variant_id, sku?, barcode?}]}. A blank/absent barcode is generated as a
    valid EAN-13 (GS1 restricted prefix 20) from the variant id. Returns {results:[...], errors:[...]}."""
    from services import shopify_service, barcode_service
    items = payload.get("items") or []
    by_product: dict = {}
    for it in items:
        pid, vid = it.get("product_id"), it.get("variant_id")
        if not pid or not vid:
            continue
        bc = (str(it.get("barcode") or "").strip()) or barcode_service.generate_ean13(vid)
        by_product.setdefault(pid, []).append({"id": vid, "barcode": bc})
    results, errors = [], []
    for pid, vs in by_product.items():
        try:
            written = await shopify_service.set_variant_barcodes(store, pid, vs)
            for w in written:
                results.append({"variant_id": w.get("id"), "sku": w.get("sku"), "barcode": w.get("barcode")})
        except Exception as e:
            for v in vs:
                errors.append({"variant_id": v["id"], "error": str(e)})
    return {"results": results, "errors": errors}


@router.post("/products/barcode-labels")
async def product_barcode_labels(payload: dict, store: models.Store = Depends(require_shop)):
    """A printable PDF of VISUAL barcode labels to stick on products. Body:
    {labels:[{barcode, sku, title, copies?}], options:{page, cols, rows, bar_height_mm, bar_width}}."""
    from fastapi.responses import Response
    from services import barcode_service
    o = payload.get("options") or {}
    pdf = barcode_service.build_labels_pdf(
        payload.get("labels") or [],
        page=o.get("page", "A4"), cols=o.get("cols", 3), rows=o.get("rows", 8),
        bar_height_mm=o.get("bar_height_mm", 12.0), bar_width=o.get("bar_width", 0.36))
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "inline; filename=barcode-labels.pdf"})


# ── Test mode (sandbox) ───────────────────────────────────────────────────────────────────────
@router.get("/test-mode")
async def get_test_mode(store: models.Store = Depends(require_shop)):
    """Is this store in sandbox mode? Drives the banner every page shows."""
    return {"test_mode": bool(getattr(store, "test_mode", False)),
            "test_mode_used": bool(getattr(store, "test_mode_used", False))}


@router.post("/test-mode")
async def set_test_mode(
    payload: Dict[str, Any] = Body(default={}),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Turn the sandbox on (seeds fake couriers, stores, orders, packing rules, picking lists) or
    off (deletes every seeded row and nothing else). Body: {enabled: bool}."""
    from services import test_mode as tm
    want = bool(payload.get("enabled"))
    if want:
        return await tm.enable(db, store)
    return await tm.disable(db, store)


@router.post("/test-mode/reseed")
async def reseed_test_mode(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Wipe the demo data and lay it down again — a clean slate mid-test."""
    from services import test_mode as tm
    await tm.disable(db, store)
    return await tm.enable(db, store)


# ================== Address Lab (validatorul consolidat + reguli + politici) ==================

@router.post("/address/check")
async def address_check(
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
):
    """«Verifică o adresă»: rulează validatorul consolidat (services.nomenclator.runner — rutare pe țară:
    RO→nomenclator bogat + guard omonimie; CZ/PL/BG/HU/SK→nomenclatoare intl; altele→geocoder) pe o adresă
    tastată liber. Read-only — nu atinge nicio comandă."""
    from services.nomenclator import runner
    fields = {k: (payload.get(k) or "") for k in ("country", "province", "city", "zip", "address1", "address2")}
    result = await runner.validate_address(fields)
    return {"input": fields, **result}


@router.get("/validation/rules")
async def validation_rules(store: models.Store = Depends(require_shop)):
    """Registrul regulilor de corectitudine (mereu ON, cod — read-only) + metadata politicilor."""
    from services.nomenclator.policy import POLICY_DEFAULTS, POLICY_META, RULES_REGISTRY
    return {"rules": RULES_REGISTRY, "policy_meta": POLICY_META, "policy_defaults": POLICY_DEFAULTS}


@router.get("/validation/policies")
async def get_validation_policies(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Politicile alegibile: defaults (cod) + override-ul GLOBAL (rândul cu store_id NULL) + efectivul."""
    from services.nomenclator.policy import POLICY_DEFAULTS, merge_policy
    row = (await db.execute(
        select(models.ValidationPolicy).where(models.ValidationPolicy.store_id.is_(None),
                                              models.ValidationPolicy.organization_id.is_(None))
    )).scalar_one_or_none()
    overrides = dict(row.policies or {}) if row else {}
    return {"defaults": POLICY_DEFAULTS, "overrides": overrides, "effective": merge_policy(overrides)}


@router.put("/validation/policies")
async def put_validation_policies(
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Scrie override-urile GLOBALE de politici (doar cheile cunoscute din POLICY_DEFAULTS; o valoare
    identică cu default-ul se scoate din override ca să urmeze default-ul de cod pe viitor)."""
    from services.nomenclator.policy import POLICY_DEFAULTS, merge_policy
    incoming = payload.get("overrides") if isinstance(payload.get("overrides"), dict) else payload
    unknown = [k for k in incoming if k not in POLICY_DEFAULTS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Politici necunoscute: {', '.join(sorted(unknown))}")
    overrides = {k: v for k, v in incoming.items() if v is not None and v != POLICY_DEFAULTS[k]}
    row = (await db.execute(
        select(models.ValidationPolicy).where(models.ValidationPolicy.store_id.is_(None),
                                              models.ValidationPolicy.organization_id.is_(None))
    )).scalar_one_or_none()
    if row is None:
        row = models.ValidationPolicy(store_id=None, policies=overrides)
        db.add(row)
    else:
        row.policies = overrides
    await db.commit()
    return {"overrides": overrides, "effective": merge_policy(overrides)}
