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
    recipient_email,
    email_from_note_attributes,
    receiver_name,
)

logger = logging.getLogger(__name__)

# Background per-order shadow tasks (keep strong refs so they aren't GC'd mid-run).
_ORDER_SHADOW_TASKS: set = set()


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
        # Init collections as loaded-empty: a NEW order has a PK only after flush, and any later access
        # to an UNLOADED relationship (order.shipments in calculate_and_set_derived_status, order.line_items
        # in the tag rebuild) then lazy-loads in the async session → MissingGreenlet, which poisons the
        # session so the whole webhook 500s. Existing orders are selectinload'd above.
        order.line_items = []
        order.shipments = []
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
        # Recipient name courier-safe: dacă e telefon/gunoi (nicio literă) → placeholder `Client <Oraș>`
        # (paritate xconnector #562); numele reale în orice alfabet trec neatinse.
        order.shipping_name = receiver_name(
            _full_name(sa.get("first_name"), sa.get("last_name")) or sa.get("name"), sa.get("city"))
        # Recipient email: real one from the payload if present (câmp standard SAU note_attributes din
        # formularul COD, paritate #568), else a synthesized placeholder so couriers that require an
        # email don't reject the AWB.
        raw_email = (payload.get("email") or payload.get("contact_email") or cust.get("email")
                     or email_from_note_attributes(payload.get("note_attributes")))
        order.shipping_email = recipient_email(raw_email, store, order.name)
        order.shipping_address1 = sa.get("address1")
        order.shipping_address2 = sa.get("address2")
        order.shipping_phone = sa.get("phone") or payload.get("phone")
        order.shipping_city = sa.get("city")
        order.shipping_zip = sa.get("zip")
        order.shipping_province = sa.get("province")
        order.shipping_country = sa.get("country")

    # Line items — the webhook payload is authoritative; rebuild the set.
    line_items = payload.get("line_items") or []
    # Rebuild the whole line-item set BEFORE db.flush(): on a pending NEW order there is no PK yet, so
    # touching order.line_items returns the in-memory collection WITHOUT a query; AFTER the flush it
    # would lazy-load in the async session → MissingGreenlet (an orders/updated for an order OH hasn't
    # synced yet 500s). Existing orders are selectinload'd. The flush then persists order + children
    # together (the relationship cascades the FK — the PK is NOT needed before appending).
    if line_items:
        prev_tags = {(li.sku or "").strip().lower(): li.product_tags
                     for li in order.line_items if li.sku and li.product_tags}
        order.line_items.clear()
        for item in line_items:
            order.line_items.append(
                models.LineItem(
                    sku=item.get("sku"),
                    title=item.get("title"),
                    quantity=item.get("quantity"),
                    product_tags=prev_tags.get((item.get("sku") or "").strip().lower()),
                )
            )

    # PK + children flushed together.
    await db.flush()

    # Product-tags enrich moved POST-COMMIT (see below): it calls the Shopify API, and doing that
    # while holding this session was the biggest pool-hold under webhook storms (18-aug incident #2).
    needs_tag_enrich = bool(line_items) and any(li.sku and not li.product_tags for li in order.line_items)

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

    # Per-order parity shadow (duplicate + parcele + surpriză) — rulate LA COMANDĂ, într-o sesiune
    # PROPRIE, DUPĂ commit: webhook-ul rămâne rapid și o eroare aici nu poate rupe ingestul. Înlocuiește
    # sweep-ul de 15 min pentru aceste detectoare (validarea de adresă a rulat deja mai sus).
    try:
        import asyncio
        from services.cron_parity import order_shadow
        _t = asyncio.create_task(order_shadow.run_for_order(store.id, order.id))
        _ORDER_SHADOW_TASKS.add(_t)
        _t.add_done_callback(_ORDER_SHADOW_TASKS.discard)
    except Exception:
        logger.exception("could not schedule per-order shadow for %s", order.name)

    # Product-tags enrich — POST-COMMIT, sesiune proprie, gardat de semaforul shadow: apelul Shopify
    # (extern, lent) nu mai ține sesiunea webhook-ului deschisă. Best-effort ca înainte.
    if needs_tag_enrich:
        try:
            import asyncio
            _te = asyncio.create_task(_enrich_product_tags(store.id, order.id))
            _ORDER_SHADOW_TASKS.add(_te)
            _te.add_done_callback(_ORDER_SHADOW_TASKS.discard)
        except Exception:
            logger.info("could not schedule tag enrich for %s", order.name)

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


async def _enrich_product_tags(store_id: int, order_id: int) -> None:
    """Umple LineItem.product_tags din Shopify — POST-COMMIT, sesiune proprie, sub semaforul shadow
    (apel extern lent; nu ținem nici pool-ul, nici webhook-ul). Eșecul lasă NULL pt backfill."""
    from services.cron_parity.order_shadow import _SEM
    from sqlalchemy.orm import selectinload as _sel
    try:
        async with _SEM, AsyncSessionLocal() as db:
            store = await db.get(models.Store, store_id)
            order = (await db.execute(
                select(models.Order).options(_sel(models.Order.line_items))
                .where(models.Order.id == order_id)
            )).scalar_one_or_none()
            if not store or not order:
                return
            need = [li.sku for li in order.line_items if li.sku and not li.product_tags]
            if not need:
                return
            # DB-FIRST („nu ai deja datele?" — ba da): același SKU apare pe sute de comenzi vechi ale
            # magazinului, cu tag-urile deja salvate. Le refolosim din istoricul propriu; Shopify e apelat
            # DOAR pentru SKU-uri pe care nu le-am văzut niciodată (produs nou) → aproape zero apeluri.
            known_rows = (await db.execute(
                select(models.LineItem.sku, models.LineItem.product_tags)
                .join(models.Order, models.LineItem.order_id == models.Order.id)
                .where(models.Order.store_id == store.id,
                       models.LineItem.sku.in_(need),
                       models.LineItem.product_tags.isnot(None))
                .order_by(models.LineItem.id.desc())
                .limit(500)
            )).all()
            tag_map: dict = {}
            for sku, tags in known_rows:                      # primul văzut = cel mai recent (desc)
                tag_map.setdefault((sku or "").strip().lower(), tags)
            missing = [s for s in need if (s or "").strip().lower() not in tag_map]
            if missing:
                from services import shopify_service
                tag_map.update(await shopify_service.get_variant_product_tags(store, missing))
            changed = False
            for li in order.line_items:
                if li.sku and not li.product_tags:
                    tags = tag_map.get(li.sku.strip().lower())
                    if tags:
                        li.product_tags = tags
                        changed = True
            if changed:
                await db.commit()
    except Exception as e:
        logger.info("product-tags enrich (post-commit) failed for order_id=%s: %s", order_id, e)


# --- dispatch (referenced by routes/webhooks.py) ---------------------------

WEBHOOK_HANDLERS = {
    "orders/create": upsert_order_from_webhook,
    "orders/updated": upsert_order_from_webhook,
    # Anularea trece prin ACELAȘI upsert — el citește deja `cancelled_at` din payload. Topic separat
    # pentru că e semnalul canonic (nu ne bazăm pe orders/updated să vină și el).
    "orders/cancelled": upsert_order_from_webhook,
    "orders/edited": handle_order_edited,
}


async def process_webhook_event(
    db: AsyncSession, topic: str, store_id: int, payload: Dict[str, Any]
) -> None:
    """Dispatch one webhook to its handler. Called SYNCHRONOUSLY from the route (before the
    response) so a failure can propagate and Shopify retries — it used to run after a 200 was
    already sent, which lost the event permanently. It still opens its OWN session rather than
    reusing the request-scoped one, so the transaction boundary belongs to the handler.
    Raises on handler failure; the route turns that into a 500."""
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
            # RE-RAISE. Swallowing here is what made a failed orders/create vanish: the route had
            # already returned 200, so Shopify never retried and the order simply never existed.
            # The caller now returns 500 on this, and Shopify redelivers. Handlers are idempotent.
            logger.exception("Webhook handler crashed: topic=%s store_id=%s", topic, store_id)
            raise
