"""Real-time order ingest from Shopify webhooks.

The generic webhook route (`routes/webhooks.py`) verifies HMAC, then hands the
parsed payload here. `orders/create` and `orders/updated` both flow through one
idempotent upsert so the merchant's Orders/Printing/Validation screens stay live
without waiting for a periodic sync.

Note: Shopify webhook payloads are REST-shaped JSON (snake_case, `tags` as a comma
string, `shipping_address`, `line_items`, `fulfillments[].tracking_number`). Parsing
that payload is NOT a REST *API call* — we never call the deprecated REST Admin API
(App Store req 2.2.4); the periodic backfill uses GraphQL (`services/shopify_service`).
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models
from database import AsyncSessionLocal
from services import address_service
from services.utils import (
    parse_timestamp,
    get_payment_mapping,
    calculate_and_set_derived_status,
)

logger = logging.getLogger(__name__)


# --- small payload helpers -------------------------------------------------

def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _tags_to_str(tags: Any) -> Optional[str]:
    """Webhook `tags` is a comma-separated STRING; the GraphQL path gives a list."""
    if isinstance(tags, (list, tuple)):
        return ", ".join(str(t) for t in tags) or None
    return (tags or None)


def _full_name(first: Any, last: Any) -> Optional[str]:
    name = f"{first or ''} {last or ''}".strip()
    return name or None


def _normalize_account_key(company: Optional[str]) -> str:
    """Deduce a courier account_key from the tracking company name (label linkage)."""
    if not company:
        return "default"
    s = company.strip().lower()
    if "dpd" in s:
        return "dpdromania" if "romania" in s else "dpd"
    if "sameday" in s:
        return "sameday"
    return s.replace(" ", "")


async def _account_key_from_db(db: AsyncSession, company: Optional[str]) -> Optional[str]:
    """Prefer an explicit CourierMapping (merchant-configured) over the heuristic."""
    if not company:
        return None
    res = await db.execute(
        select(models.CourierMapping.account_key).where(
            models.CourierMapping.shopify_name == company.strip().lower()
        )
    )
    row = res.first()
    return row[0] if row else None


# --- the one order upsert (create + update share this) ---------------------

async def upsert_order_from_webhook(
    db: AsyncSession, store: models.Store, payload: Dict[str, Any]
) -> Optional[models.Order]:
    """Create-or-update one order from a REST-shaped webhook payload, scoped to `store`.

    Idempotent: `orders/create` inserts the row, `orders/updated` refreshes it, and a
    re-delivery of either is harmless. Also refreshes line items, links any fulfillment
    tracking as a Shipment, validates the shipping address, and recomputes derived status.
    """
    shopify_id = payload.get("id")
    if shopify_id in (None, "", "None"):
        logger.warning("Webhook order payload with no id for shop=%s; ignored.", store.domain)
        return None
    shopify_id = str(shopify_id)

    res = await db.execute(
        select(models.Order)
        .options(
            selectinload(models.Order.line_items),
            selectinload(models.Order.shipments),
        )
        .where(models.Order.shopify_order_id == shopify_id)
    )
    order = res.scalar_one_or_none()
    is_new = order is None
    if is_new:
        order = models.Order(store_id=store.id, shopify_order_id=shopify_id)
        db.add(order)

    include_pii = (getattr(store, "pii_source", "") or "").lower() == "shopify"
    gateways: List[str] = payload.get("payment_gateway_names") or []
    fin = payload.get("financial_status") or ""

    order.name = payload.get("name") or f"#{shopify_id}"
    created = parse_timestamp(payload.get("created_at"))
    if created:
        order.created_at = created
    order.cancelled_at = parse_timestamp(payload.get("cancelled_at"))
    order.financial_status = fin
    order.total_price = _to_float(payload.get("total_price"))
    order.payment_gateway_names = ", ".join(gateways) if gateways else None
    order.mapped_payment = get_payment_mapping(gateways) or (
        "card" if fin.lower() == "paid" else "unknown"
    )
    order.tags = _tags_to_str(payload.get("tags"))
    order.note = payload.get("note")
    order.shopify_status = (payload.get("fulfillment_status") or "").lower() or None
    order.sync_status = "synced"
    order.last_sync_at = datetime.now(timezone.utc)

    if include_pii:
        sa = payload.get("shipping_address") or {}
        cust = payload.get("customer") or {}
        order.customer = _full_name(cust.get("first_name"), cust.get("last_name")) or _full_name(
            sa.get("first_name"), sa.get("last_name")
        )
        order.shipping_name = _full_name(sa.get("first_name"), sa.get("last_name")) or sa.get("name")
        order.shipping_address1 = sa.get("address1")
        order.shipping_address2 = sa.get("address2")
        order.shipping_phone = sa.get("phone") or payload.get("phone")
        order.shipping_city = sa.get("city")
        order.shipping_zip = sa.get("zip")
        order.shipping_province = sa.get("province")
        order.shipping_country = sa.get("country")

    # Need the PK before appending children on a new row.
    await db.flush()

    # Line items — the webhook payload is authoritative; rebuild the set.
    line_items = payload.get("line_items") or []
    if line_items:
        order.line_items.clear()
        for item in line_items:
            order.line_items.append(
                models.LineItem(
                    sku=item.get("sku"),
                    title=item.get("title"),
                    quantity=item.get("quantity"),
                )
            )

    # Fulfillment tracking → Shipment (so it enters the print queue / AWB tracking).
    for f in payload.get("fulfillments") or []:
        fid = f.get("id")
        number = f.get("tracking_number") or (f.get("tracking_numbers") or [None])[0]
        if fid in (None, "") or not number:
            continue
        fid = str(fid)
        company = f.get("tracking_company")
        ak = await _account_key_from_db(db, company) or _normalize_account_key(company)
        existing = next((s for s in order.shipments if s.shopify_fulfillment_id == fid), None)
        if existing:
            existing.awb = number
            existing.courier = company or existing.courier
            if not existing.account_key:
                existing.account_key = ak
        else:
            order.shipments.append(
                models.Shipment(
                    shopify_fulfillment_id=fid,
                    fulfillment_created_at=parse_timestamp(f.get("created_at")),
                    awb=number,
                    courier=company or "Unknown",
                    account_key=ak,
                )
            )

    # Address validation — the address-yield surface. Only when we hold PII to check.
    if include_pii and (order.address_status or "").lower() != "validat":
        try:
            await address_service.validate_address_for_order(db, order)
        except Exception:
            logger.exception("Address validation failed (webhook) for %s", order.name)

    try:
        calculate_and_set_derived_status(order)
    except Exception:
        logger.exception("Derived-status calc failed (webhook) for %s", order.name)

    await db.commit()
    logger.info(
        "Webhook %s order %s for shop=%s.",
        "created" if is_new else "updated", order.name, store.domain,
    )
    return order


async def handle_order_edited(
    db: AsyncSession, store: models.Store, payload: Dict[str, Any]
) -> Optional[models.Order]:
    """orders/edited fires when a merchant edits an order's LINE ITEMS in the admin.
    Its payload is only an edit DIFF (additions/removals), not a full order — so we
    re-fetch the authoritative current state via GraphQL and upsert it (line items,
    totals, fulfillments all refreshed)."""
    oe = payload.get("order_edit") or {}
    order_id = oe.get("order_id") or payload.get("id")
    if order_id in (None, "", "None"):
        logger.warning("orders/edited with no order_id for shop=%s; ignored.", store.domain)
        return None

    # Lazy imports avoid pulling the sync stack in at module load.
    from services import shopify_service, sync_service

    node = await shopify_service.fetch_single_order(db, store.id, order_id)
    if not node:
        logger.warning("orders/edited: could not re-fetch order %s for %s.", order_id, store.domain)
        return None

    # Reuse the GraphQL-shape upsert (rebuilds line items + shipments + validates address).
    await sync_service._process_and_insert_orders_in_batches(db, [node], store.id, store.pii_source)
    logger.info("orders/edited re-synced order %s for shop=%s.", order_id, store.domain)
    return None


# --- dispatch (referenced by routes/webhooks.py) ---------------------------

WEBHOOK_HANDLERS = {
    "orders/create": upsert_order_from_webhook,
    "orders/updated": upsert_order_from_webhook,
    "orders/edited": handle_order_edited,
}


async def process_webhook_event(
    db: AsyncSession, topic: str, store_id: int, payload: Dict[str, Any]
) -> None:
    """Background dispatcher. Runs AFTER the 200 response, so the request-scoped `db` is
    already being torn down — we open a fresh session here and reload the store."""
    handler = WEBHOOK_HANDLERS.get(topic)
    if not handler:
        logger.warning("No handler for webhook topic=%s (shop store_id=%s).", topic, store_id)
        return
    async with AsyncSessionLocal() as session:
        store = await session.get(models.Store, store_id)
        if not store or not store.is_active:
            logger.warning("Webhook for missing/inactive store_id=%s; ignored.", store_id)
            return
        try:
            await handler(session, store, payload)
        except Exception:
            logger.exception("Webhook handler crashed: topic=%s store_id=%s", topic, store_id)
