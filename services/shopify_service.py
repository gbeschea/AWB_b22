import asyncio
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Union

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

import models
from settings import settings

_logger = logging.getLogger(__name__)

# Cache client httpx per store
_shopify_clients: Dict[int, httpx.AsyncClient] = {}


def _api_version(store: models.Store) -> str:
    # A store may pin its own version; otherwise the app's declared one, so this can never
    # drift behind shopify.app.toml the way a hardcoded literal did.
    return getattr(store, "api_version", None) or settings.SHOPIFY_API_VERSION


def _token_fingerprint(tok: str) -> str:
    if not tok:
        return "<empty>"
    t = tok.strip()
    if len(t) <= 8:
        return t
    return f"{t[:4]}…{t[-4:]}"


def get_shopify_client(store: models.Store) -> httpx.AsyncClient:
    """
    Reutilizează un AsyncClient per store pentru a păstra conexiunile.
    Face strip() la token și loghează un fingerprint ca să vezi imediat dacă
    DB conține tokenul corect sau un șir mascat '••••'.
    """
    if store.id not in _shopify_clients:
        token = (store.access_token or "").strip()

        # atenționări utile în log
        if not token:
            _logger.warning("Store %s (%s) are token gol în DB!",
                            store.name, store.domain)
        if "•" in token:
            _logger.error("Store %s (%s) are token MASCAT în DB (conține '•'). Actualizează-l!",
                          store.name, store.domain)
        if not token.startswith("shpat_"):
            _logger.warning("Tokenul pentru %s pare ne-standard (fingerprint %s).",
                            store.domain, _token_fingerprint(token))

        headers = {
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        }
        _logger.info("Shopify client pentru %s, token %s",
                     store.domain, _token_fingerprint(token))

        _shopify_clients[store.id] = httpx.AsyncClient(
            base_url=f"https://{store.domain}/admin/api/{_api_version(store)}/",
            headers=headers,
            timeout=30.0,
        )
    return _shopify_clients[store.id]


async def authed_client(store: models.Store) -> httpx.AsyncClient:
    """`get_shopify_client`, but guarantees the token is still valid first.

    Access tokens now live about an hour, so anything running outside a merchant request —
    the AWB cron, webhook handlers, backfills — would start 403ing partway through a shift if it
    just reused whatever was in the DB. Renewal uses the refresh token and needs no merchant.

    Everything that talks to Shopify should go through here rather than `get_shopify_client`.
    """
    from database import AsyncSessionLocal
    from services import token_exchange
    try:
        async with AsyncSessionLocal() as db:
            fresh = await db.get(models.Store, store.id)
            if fresh is not None:
                await token_exchange.ensure_background_token(db, fresh)
                # Carry the renewed token back to the caller's instance so the client below,
                # and any later use of this object, sees it.
                if fresh.access_token and fresh.access_token != store.access_token:
                    store.access_token = fresh.access_token
                    store.token_expires_at = fresh.token_expires_at
                    old = _shopify_clients.pop(store.id, None)
                    if old is not None:
                        await old.aclose()
    except Exception:
        # Never block a call on the refresh path — if the existing token still works, it works.
        _logger.exception("token renewal check failed for %s", getattr(store, "domain", "?"))
    return get_shopify_client(store)



async def get_store_from_db(db: AsyncSession, store_id: int) -> models.Store:
    res = await db.execute(select(models.Store).where(models.Store.id == store_id))
    store = res.scalar_one_or_none()
    if not store:
        raise ValueError(f"Store {store_id} not found.")
    return store


def _order_node_fields(include_pii: bool) -> str:
    """The order node fields we ingest — shared by the list query and the single-order
    re-fetch (orders/edited). We deliberately DON'T request the linked `customer` object:
    it needs the read_customers scope (+ PCD Email) and makes the whole query ACCESS_DENIED.
    For shipping/AWB the recipient IS the shipping address, so the name is derived from
    shippingAddress (covered by read_orders + the PCD Name/Phone/Address grant)."""
    fields = """
        id
        name
        createdAt
        cancelledAt
        displayFinancialStatus
        displayFulfillmentStatus
        tags
        note
        totalPriceSet { shopMoney { amount currencyCode } }
        paymentGatewayNames
        fulfillments {
          id
          createdAt
          displayStatus
          trackingInfo { company number url }
        }
    """
    if include_pii:
        fields += """
        shippingAddress {
          firstName lastName address1 address2 city province zip country phone
        }
        """
    return fields


def _orders_query(include_pii: bool) -> str:
    """Paginated list query used by the backfill."""
    return """
    query($first: Int!, $cursor: String, $query: String) {
      orders(first: $first, after: $cursor, query: $query) {
        pageInfo { hasNextPage endCursor }
        edges { node { %s } }
      }
    }
    """ % _order_node_fields(include_pii)


async def fetch_orders(
    db: AsyncSession,
    store_id: int,
    created_at_min: datetime,
    created_at_max: datetime,
) -> List[Dict[str, Any]]:
    """
    Preia comenzile din Shopify. Dacă store.pii_source == "metafield", NU cerem câmpuri PII
    (customer/shippingAddress) ca să evităm ACCESS_DENIED pe planuri fără acces la Customer.
    """
    store = await get_store_from_db(db, store_id)
    client = await authed_client(store)

    include_pii = (getattr(store, "pii_source", "") or "").lower() == "shopify"
    query = _orders_query(include_pii)

    start_str = created_at_min.isoformat()
    end_str = created_at_max.isoformat()

    all_orders: List[Dict[str, Any]] = []
    cursor = None
    has_next = True

    while has_next:
        # gentle pacing (ratelimit)
        await asyncio.sleep(0.35)
        variables = {
            "first": 50,
            "cursor": cursor,
            "query": f"created_at:>{start_str} created_at:<{end_str}",
        }
        try:
            r = await client.post("graphql.json", json={"query": query, "variables": variables})
            r.raise_for_status()
            payload = r.json()

            # Dacă apar erori (inclusiv throttled), tratează-le
            if "errors" in payload and payload["errors"]:
                if any((e.get("extensions", {}) or {}).get("code") == "THROTTLED" for e in payload["errors"]):
                    _logger.warning("Shopify throttling; sleep 5s și reîncerc...")
                    await asyncio.sleep(5)
                    continue
                _logger.error("Eroare GraphQL la preluarea comenzilor pentru %s: %s",
                              store.domain, payload["errors"])
                # la erori de acces, nu insistăm; ieșim din buclă
                break

            data = (payload.get("data") or {}).get("orders") or {}
            edges = data.get("edges") or []
            for edge in edges:
                all_orders.append(edge["node"])

            page_info = data.get("pageInfo") or {}
            has_next = bool(page_info.get("hasNextPage"))
            cursor = page_info.get("endCursor")
        except httpx.HTTPStatusError as e:
            _logger.error("HTTP %s la preluarea comenzilor %s: %s",
                          e.response.status_code, store.domain, e.response.text)
            break
        except Exception as ex:
            _logger.exception("Eroare la preluarea comenzilor pentru %s: %s", store.domain, ex)
            break

    return all_orders


async def fetch_single_order(db: AsyncSession, store_id: int, order_id) -> Optional[Dict[str, Any]]:
    """Fetch ONE order by id, in the same node shape as fetch_orders. Used by the
    orders/edited webhook, whose payload is only an edit diff — so we re-fetch the
    authoritative current state and upsert it."""
    store = await get_store_from_db(db, store_id)
    client = await authed_client(store)
    include_pii = (getattr(store, "pii_source", "") or "").lower() == "shopify"
    query = "query($id: ID!) { order(id: $id) { %s } }" % _order_node_fields(include_pii)
    gid = f"gid://shopify/Order/{str(order_id).split('/')[-1]}"
    try:
        r = await client.post("graphql.json", json={"query": query, "variables": {"id": gid}})
        r.raise_for_status()
        payload = r.json()
        if payload.get("errors"):
            _logger.error("GraphQL error fetching order %s for %s: %s",
                          order_id, store.domain, payload["errors"])
            return None
        return (payload.get("data") or {}).get("order")
    except Exception:
        _logger.exception("Failed to fetch single order %s for %s", order_id, store.domain)
        return None


# --------------------
# Webhook subscriptions (idempotent, self-healing)
# --------------------

# (topic, path). GDPR privacy webhooks are declared in shopify.app.toml, not here.
_OPERATIONAL_WEBHOOKS = [
    ("APP_UNINSTALLED", "/webhooks/app/uninstalled"),
    ("ORDERS_CREATE", "/webhooks/orders/create"),
    ("ORDERS_UPDATED", "/webhooks/orders/updated"),
    ("ORDERS_EDITED", "/webhooks/orders/edited"),
    # orders/cancelled: `orders/updated` acoperă anularea în teorie, dar semnalul CANONIC e ăsta —
    # iar o comandă anulată pe care OH o crede activă înseamnă (cu auto-AWB pornit) AWB pe o comandă
    # anulată, sau chiar expedierea ei dacă fulfillment order-ul a rămas OPEN. Măsurat 19-aug: 3 din 4
    # comenzi Nubra „active fără AWB" erau de fapt anulate în Shopify.
    ("ORDERS_CANCELLED", "/webhooks/orders/cancelled"),
]

_LIST_WEBHOOKS_Q = """
{ webhookSubscriptions(first: 100) {
    edges { node { topic endpoint { __typename ... on WebhookHttpEndpoint { callbackUrl } } } }
} }
"""

_CREATE_WEBHOOK_M = """
mutation($topic: WebhookSubscriptionTopic!, $sub: WebhookSubscriptionInput!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $sub) {
    webhookSubscription { id }
    userErrors { field message }
  }
}
"""


async def ensure_operational_webhooks(store: models.Store) -> Dict[str, Any]:
    """Make sure every operational webhook is registered for `store`, creating only the
    missing ones. Idempotent and self-healing: safe to call at install AND on app load,
    so a registration that failed at install (e.g. before PCD was granted) is repaired on
    the next open — without a reinstall. Logs userErrors instead of swallowing them."""
    base = (settings.SHOPIFY_APP_URL or "").rstrip("/")
    client = await authed_client(store)

    # 1) What's already there?
    existing = set()
    try:
        r = await client.post("graphql.json", json={"query": _LIST_WEBHOOKS_Q})
        r.raise_for_status()
        for e in ((((r.json().get("data") or {}).get("webhookSubscriptions") or {}).get("edges")) or []):
            node = e.get("node") or {}
            cb = (node.get("endpoint") or {}).get("callbackUrl")
            if node.get("topic") and cb:
                existing.add((node["topic"], cb))
    except Exception:
        _logger.exception("Could not list webhooks for %s; will attempt to (re)create all.", store.domain)

    # 2) Create the missing ones.
    created, errors = [], []
    for topic, path in _OPERATIONAL_WEBHOOKS:
        url = f"{base}{path}"
        if (topic, url) in existing:
            continue
        try:
            resp = await client.post("graphql.json", json={
                "query": _CREATE_WEBHOOK_M,
                "variables": {"topic": topic, "sub": {"callbackUrl": url, "format": "JSON"}},
            })
            body = resp.json()
            ue = ((((body.get("data") or {}).get("webhookSubscriptionCreate")) or {}).get("userErrors")) or []
            top_errs = body.get("errors") or []
            if ue or top_errs:
                errors.append({"topic": topic, "userErrors": ue, "errors": top_errs})
            else:
                created.append(topic)
        except Exception as ex:
            errors.append({"topic": topic, "exception": str(ex)})

    if created:
        _logger.info("Webhooks created for %s: %s", store.domain, created)
    if errors:
        _logger.warning("Webhook registration issues for %s: %s", store.domain, errors)
    return {"created": created, "errors": errors,
            "already_present": [t for (t, _) in existing]}


# --------------------
# Tranzacții / plăți
# --------------------

async def get_transactions(db: AsyncSession, store_id: int, order_id: str) -> List[Dict[str, Any]]:
    store = await get_store_from_db(db, store_id)
    client = await authed_client(store)
    query = """
    query($orderId: ID!) {
      order(id: $orderId) {
        transactions {
          id
          status
          kind
          amountSet { shopMoney { amount currencyCode } }
          gateway
        }
      }
    }
    """
    variables = {"orderId": f"gid://shopify/Order/{order_id}"}
    try:
        r = await client.post("graphql.json", json={"query": query, "variables": variables})
        r.raise_for_status()
        data = r.json()
        if "errors" in data and data["errors"]:
            _logger.error("Eroare GraphQL la tranzacții: %s", data["errors"])
            return []
        return (((data.get("data") or {}).get("order") or {}).get("transactions") or [])
    except Exception as ex:
        _logger.exception("Eroare la preluarea tranzacțiilor pentru comanda %s: %s", order_id, ex)
        return []


async def capture_payment(db: AsyncSession, store_id: int, shopify_order_id: str) -> None:
    """
    Încearcă să captureze o autorizare existentă; dacă nu există, marchează ca plătit.
    """
    store = await get_store_from_db(db, store_id)
    client = await authed_client(store)
    order_gid = f"gid://shopify/Order/{shopify_order_id}"

    txns = await get_transactions(db, store_id, shopify_order_id)
    auth = next((t for t in txns if (t.get("kind", "").upper() == "AUTHORIZATION") and (t.get("status", "").upper() == "SUCCESS")), None)

    if not auth:
        # fallback: direct mark as paid (de ex. ramburs)
        await mark_order_as_paid(db, store_id, shopify_order_id)
        return

    mutation = """
    mutation captureTransaction($transactionId: ID!, $amount: MoneyInput!) {
      transactionCreate(transaction: {
        orderId: "%s",
        kind: CAPTURE,
        amount: $amount.amount,
        currency: $amount.currencyCode,
        parentId: $transactionId
      }) {
        transaction { id status }
        userErrors { field message }
      }
    }
    """ % order_gid

    variables = {
        "transactionId": auth["id"],
        "amount": {
            "amount": auth["amountSet"]["shopMoney"]["amount"],
            "currencyCode": auth["amountSet"]["shopMoney"].get("currencyCode", "RON"),
        },
    }
    r = await client.post("graphql.json", json={"query": mutation, "variables": variables})
    r.raise_for_status()
    data = r.json()
    errs = (((data.get("data") or {}).get("transactionCreate") or {}).get("userErrors") or [])
    if errs:
        raise RuntimeError(", ".join(e.get("message", "Unknown error") for e in errs))
    txn = ((data.get("data") or {}).get("transactionCreate") or {}).get("transaction") or {}
    if (txn.get("status", "") or "").upper() != "SUCCESS":
        raise RuntimeError("Shopify couldn't capture the payment.")


async def mark_order_as_paid(db: AsyncSession, store_id: int, shopify_order_id: Union[str, int]) -> None:
    store = await get_store_from_db(db, store_id)
    client = await authed_client(store)
    order_gid = f"gid://shopify/Order/{shopify_order_id}"
    mutation = """
    mutation MarkPaid($input: OrderMarkAsPaidInput!) {
      orderMarkAsPaid(input: $input) {
        order { id financialStatus }
        userErrors { field message }
      }
    }
    """
    variables = {"input": {"id": order_gid}}
    r = await client.post("graphql.json", json={"query": mutation, "variables": variables})
    r.raise_for_status()
    data = r.json()
    errs = (((data.get("data") or {}).get("orderMarkAsPaid") or {}).get("userErrors") or [])
    if errs:
        # <- aici era paranteza în plus în varianta ta
        raise RuntimeError("; ".join(e.get("message", "Unknown error") for e in errs))


# --------------------
# Fulfillment + delivery-status sync (courier tracking → Shopify)
# --------------------

async def _gql(store: models.Store, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run a GraphQL op against the shop's Admin API. Raises on transport/GraphQL errors."""
    client = await authed_client(store)
    r = await client.post("graphql.json", json={"query": query, "variables": variables or {}})
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload.get("data") or {}


_OPEN_FO_Q = """
query($id: ID!) {
  order(id: $id) {
    displayFulfillmentStatus
    fulfillmentOrders(first: 20) {
      edges { node { id status } }
    }
  }
}
"""

_FULFILLMENT_CREATE_M = """
mutation fulfillmentCreate($fulfillment: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $fulfillment) {
    fulfillment { id status trackingInfo { number url company } }
    userErrors { field message }
  }
}
"""


async def create_fulfillment_with_tracking(
    store: models.Store,
    shopify_order_id: Union[str, int],
    *,
    tracking_number: str,
    tracking_company: Optional[str] = None,
    tracking_url: Optional[str] = None,
    notify_customer: bool = False,
) -> Optional[str]:
    """Fulfill the order's open fulfillment orders and attach the AWB as tracking.

    Returns the created Fulfillment GID, or None if there was nothing open to fulfill
    (already fulfilled / cancelled). Idempotent-ish: if no OPEN fulfillment orders remain
    we return None instead of erroring, so the poller can call this freely."""
    order_gid = f"gid://shopify/Order/{str(shopify_order_id).split('/')[-1]}"
    data = await _gql(store, _OPEN_FO_Q, {"id": order_gid})
    order = data.get("order") or {}
    edges = ((order.get("fulfillmentOrders") or {}).get("edges")) or []
    open_fo_ids = [
        e["node"]["id"] for e in edges
        if (e.get("node") or {}).get("status") in ("OPEN", "IN_PROGRESS", "SCHEDULED")
    ]
    if not open_fo_ids:
        return None

    tracking_info: Dict[str, Any] = {"number": tracking_number}
    if tracking_company:
        tracking_info["company"] = tracking_company
    if tracking_url:
        tracking_info["url"] = tracking_url

    variables = {
        "fulfillment": {
            "lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": fo} for fo in open_fo_ids],
            "trackingInfo": tracking_info,
            "notifyCustomer": bool(notify_customer),
        }
    }
    data = await _gql(store, _FULFILLMENT_CREATE_M, variables)
    res = data.get("fulfillmentCreate") or {}
    errs = res.get("userErrors") or []
    if errs:
        raise RuntimeError("; ".join(e.get("message", "Unknown error") for e in errs))
    ful = res.get("fulfillment") or {}
    return ful.get("id")


_FULFILLMENT_EVENT_M = """
mutation fulfillmentEventCreate($fulfillmentEvent: FulfillmentEventInput!) {
  fulfillmentEventCreate(fulfillmentEvent: $fulfillmentEvent) {
    fulfillmentEvent { id status }
    userErrors { field message }
  }
}
"""

# Canonical status → Shopify FulfillmentEventStatus enum. Only the states Shopify accepts.
_EVENT_STATUS_MAP = {
    "shipped": "IN_TRANSIT",
    "in_transit": "IN_TRANSIT",
    "out_for_delivery": "OUT_FOR_DELIVERY",
    "delivered": "DELIVERED",
    "refused": "FAILURE",
    "returned": "FAILURE",
    "pickup_office": "READY_FOR_PICKUP",
}


async def add_fulfillment_event(
    store: models.Store, fulfillment_gid: str, canonical_status: str
) -> Optional[str]:
    """Push a delivery event (IN_TRANSIT / OUT_FOR_DELIVERY / DELIVERED / FAILURE / …) onto
    a fulfillment so Shopify shows the live delivery status. No-op for statuses that don't
    map to a Shopify event. Returns the event status pushed, or None."""
    event_status = _EVENT_STATUS_MAP.get(canonical_status)
    if not event_status or not fulfillment_gid:
        return None
    variables = {"fulfillmentEvent": {"fulfillmentId": fulfillment_gid, "status": event_status}}
    data = await _gql(store, _FULFILLMENT_EVENT_M, variables)
    res = data.get("fulfillmentEventCreate") or {}
    errs = res.get("userErrors") or []
    if errs:
        # Shopify rejects out-of-order/duplicate events (e.g. DELIVERED after DELIVERED) —
        # that's benign, so we swallow it rather than fail the whole poll.
        _logger.info("fulfillmentEventCreate skipped for %s (%s): %s",
                     fulfillment_gid, event_status, errs)
        return None
    return event_status


_ORDER_FULFILLMENTS_Q = """
query($id: ID!) {
  order(id: $id) { fulfillments(first: 10) { id status } }
}
"""


_GRANTED_SCOPES_Q = "query { currentAppInstallation { accessScopes { handle } } }"


async def get_granted_scopes(store: models.Store) -> set:
    """The access scopes the shop has actually granted this app (handles). Used to detect when
    a newly-added scope still needs a re-consent."""
    data = await _gql(store, _GRANTED_SCOPES_Q)
    scopes = (((data.get("currentAppInstallation") or {}).get("accessScopes")) or [])
    return {s.get("handle") for s in scopes if s.get("handle")}


_VARIANT_INV_Q = """
query variantInventory($q: String!) {
  productVariants(first: 200, query: $q) { edges { node { sku inventoryQuantity } } }
}
"""

_ORDER_TIMELINE_Q = """
query orderTimeline($id: ID!) {
  order(id: $id) {
    events(first: 50, sortKey: CREATED_AT, reverse: true) {
      edges { node { id message createdAt criticalAlert appTitle attributeToApp attributeToUser } }
    }
  }
}
"""


_ORDER_INVOICE_Q = """
query invoiceData($id: ID!) {
  order(id: $id) {
    name currencyCode
    totalShippingPriceSet { shopMoney { amount } }
    shippingAddress { firstName lastName name address1 address2 city province zip country }
    lineItems(first: 100) {
      edges { node { title sku quantity
        variant { barcode }
        originalUnitPriceSet { shopMoney { amount currencyCode } }
        discountedUnitPriceSet { shopMoney { amount } } } }
    }
  }
}
"""


async def get_order_for_invoice(store: models.Store, shopify_order_id) -> Optional[Dict[str, Any]]:
    """Itemized order data for invoicing (line prices, customer, address). Needs read_orders.
    email/phone are omitted (protected customer data — not approved for this app). Returns
    {name, currency, client{...}, lines:[{title, sku, quantity, unit_price}]}."""
    order_gid = f"gid://shopify/Order/{str(shopify_order_id).split('/')[-1]}"
    data = await _gql(store, _ORDER_INVOICE_Q, {"id": order_gid})
    o = data.get("order")
    if not o:
        return None
    a = o.get("shippingAddress") or {}
    name = a.get("name") or " ".join(x for x in [a.get("firstName"), a.get("lastName")] if x) or "Client"
    lines = []
    for e in (((o.get("lineItems") or {}).get("edges")) or []):
        n = e.get("node") or {}
        disc = ((n.get("discountedUnitPriceSet") or {}).get("shopMoney") or {}).get("amount")
        orig = ((n.get("originalUnitPriceSet") or {}).get("shopMoney") or {}).get("amount")
        try:
            price = float(disc if disc is not None else orig)
        except (TypeError, ValueError):
            price = 0.0
        lines.append({"title": n.get("title") or n.get("sku") or "Produs",
                      "sku": n.get("sku"), "barcode": (n.get("variant") or {}).get("barcode"),
                      "quantity": n.get("quantity") or 1, "unit_price": price})
    ship_total = ((o.get("totalShippingPriceSet") or {}).get("shopMoney") or {}).get("amount")
    try:
        ship_total = float(ship_total) if ship_total is not None else 0.0
    except (TypeError, ValueError):
        ship_total = 0.0
    return {
        "name": o.get("name"), "email": "", "phone": "", "shipping_total": ship_total,
        "currency": o.get("currencyCode") or "RON",
        "client": {"name": name, "address1": a.get("address1") or "", "address2": a.get("address2") or "",
                   "city": a.get("city") or "", "province": a.get("province") or "",
                   "zip": a.get("zip") or "", "country": a.get("country") or "Romania",
                   "email": "", "phone": ""},
        "lines": lines,
    }


async def get_order_timeline(store: models.Store, shopify_order_id) -> List[Dict[str, Any]]:
    """The order's Shopify timeline (activity feed) — newest first. Only needs read_orders.
    Returns [{id, message, created_at, critical, app, by_app, by_user}]. Empty on any error."""
    order_gid = f"gid://shopify/Order/{str(shopify_order_id).split('/')[-1]}"
    try:
        data = await _gql(store, _ORDER_TIMELINE_Q, {"id": order_gid})
    except Exception as e:
        _logger.info("order timeline query failed: %s", e)
        return []
    edges = (((data.get("order") or {}).get("events")) or {}).get("edges") or []
    out: List[Dict[str, Any]] = []
    for e in edges:
        n = e.get("node") or {}
        out.append({
            "id": n.get("id"),
            "message": n.get("message"),
            "created_at": n.get("createdAt"),
            "critical": bool(n.get("criticalAlert")),
            "app": n.get("appTitle"),
            "by_app": bool(n.get("attributeToApp")),
            "by_user": bool(n.get("attributeToUser")),
        })
    return out


async def get_variant_inventory(store: models.Store, skus: List[str]) -> Dict[str, Optional[int]]:
    """Available stock per SKU (total across locations) from Shopify. {sku_lower: qty}. Only
    needs read_products. SKUs not found are simply absent from the result."""
    out: Dict[str, Optional[int]] = {}
    clean = [s.strip() for s in (skus or []) if s and s.strip()]
    CHUNK = 40
    for i in range(0, len(clean), CHUNK):
        chunk = clean[i:i + CHUNK]
        q = " OR ".join(f'sku:"{s.replace(chr(34), "")}"' for s in chunk)
        try:
            data = await _gql(store, _VARIANT_INV_Q, {"q": q})
        except Exception as e:
            _logger.info("variant inventory query failed: %s", e)
            continue
        for e in (((data.get("productVariants") or {}).get("edges")) or []):
            n = e.get("node") or {}
            sku = (n.get("sku") or "").strip().lower()
            if sku:
                out[sku] = n.get("inventoryQuantity")
    return out


_VARIANT_BC_Q = """
query($q: String!) {
  productVariants(first: 100, query: $q) { edges { node { sku barcode } } }
}
"""


async def get_variant_barcodes(store: models.Store, skus: List[str]) -> Dict[str, str]:
    """{sku_lower: barcode} for the given SKUs (only ones that HAVE a barcode). Needs read_products.
    Used by the scanner to verify items by their Shopify barcode, not only the SKU."""
    out: Dict[str, str] = {}
    clean = [s.strip() for s in (skus or []) if s and s.strip()]
    for i in range(0, len(clean), 40):
        chunk = clean[i:i + 40]
        q = " OR ".join(f'sku:"{s.replace(chr(34), "")}"' for s in chunk)
        try:
            data = await _gql(store, _VARIANT_BC_Q, {"q": q})
        except Exception as e:
            _logger.info("variant barcode query failed: %s", e)
            continue
        for e in (((data.get("productVariants") or {}).get("edges")) or []):
            n = e.get("node") or {}
            sku = (n.get("sku") or "").strip().lower()
            bc = (n.get("barcode") or "").strip()
            if sku and bc:
                out[sku] = bc
    return out


_VARIANT_TAGS_Q = """
query($q: String!) {
  productVariants(first: 100, query: $q) { edges { node { sku product { tags } } } }
}
"""


def wrap_product_tags(tags: List[str]) -> str:
    """Normalize a Shopify product-tag list to the stored form: lowercased, pipe-wrapped
    (`|fragile|gift wrap|`) for exact-token ILIKE filtering. No tags → `"|"` (a fetched-but-empty
    sentinel that yields nothing when unnested, so it's distinct from NULL = not fetched)."""
    norm = [t.strip().lower() for t in (tags or []) if t and t.strip()]
    return ("|" + "|".join(norm) + "|") if norm else "|"


async def get_variant_product_tags(store: models.Store, skus: List[str]) -> Dict[str, str]:
    """{sku_lower: '|tag1|tag2|'} — each SKU's product tags in the stored pipe-wrapped form.
    Needs read_products. A product with no tags maps to `"|"`; SKUs not found are absent."""
    out: Dict[str, str] = {}
    clean = [s.strip() for s in (skus or []) if s and s.strip()]
    for i in range(0, len(clean), 40):
        chunk = clean[i:i + 40]
        q = " OR ".join(f'sku:"{s.replace(chr(34), "")}"' for s in chunk)
        try:
            data = await _gql(store, _VARIANT_TAGS_Q, {"q": q})
        except Exception as e:
            _logger.info("variant tags query failed: %s", e)
            continue
        for e in (((data.get("productVariants") or {}).get("edges")) or []):
            n = e.get("node") or {}
            sku = (n.get("sku") or "").strip().lower()
            if not sku:
                continue
            out[sku] = wrap_product_tags(((n.get("product") or {}).get("tags")) or [])
    return out


async def get_latest_fulfillment_gid(store: models.Store, shopify_order_id) -> Optional[str]:
    """The order's newest non-cancelled Fulfillment GID (to attach a delivery event to an order
    that's already fulfilled). None if it has no fulfillment yet."""
    order_gid = f"gid://shopify/Order/{str(shopify_order_id).split('/')[-1]}"
    data = await _gql(store, _ORDER_FULFILLMENTS_Q, {"id": order_gid})
    fs = ((data.get("order") or {}).get("fulfillments")) or []
    for f in fs:
        if (f.get("status") or "").upper() != "CANCELLED":
            return f.get("id")
    return fs[0].get("id") if fs else None


_ORDER_CANCEL_M = """
mutation orderCancel($orderId: ID!, $reason: OrderCancelReason!, $refund: Boolean!,
                     $restock: Boolean!, $notifyCustomer: Boolean, $staffNote: String) {
  orderCancel(orderId: $orderId, reason: $reason, refund: $refund, restock: $restock,
              notifyCustomer: $notifyCustomer, staffNote: $staffNote) {
    job { id }
    orderCancelUserErrors { field message code }
  }
}
"""


async def cancel_order(
    store: models.Store,
    shopify_order_id: Union[str, int],
    *,
    reason: str = "DECLINED",
    refund: bool = False,
    restock: bool = True,
    notify_customer: bool = False,
    staff_note: Optional[str] = None,
) -> Dict[str, Any]:
    """Cancel an order in Shopify (used by the refusal automation). `refund=False` because
    a refused COD parcel was never paid — nothing to refund. `restock` returns the units to
    inventory. Returns {"job_id": ...}. Raises on userErrors."""
    order_gid = f"gid://shopify/Order/{str(shopify_order_id).split('/')[-1]}"
    variables = {
        "orderId": order_gid,
        "reason": reason,
        "refund": bool(refund),
        "restock": bool(restock),
        "notifyCustomer": bool(notify_customer),
        "staffNote": staff_note,
    }
    data = await _gql(store, _ORDER_CANCEL_M, variables)
    res = data.get("orderCancel") or {}
    errs = res.get("orderCancelUserErrors") or []
    if errs:
        raise RuntimeError("; ".join(e.get("message", "Unknown error") for e in errs))
    return {"job_id": (res.get("job") or {}).get("id")}


# --------------------
# Multi-location split (one AWB per fulfillment location)
# --------------------

# Each Shopify fulfillment order = one assigned location with its own line items. Splitting a
# cross-location order into one AWB per location is exactly "one AWB per fulfillment order".
# Scopes: covered by our write_*_fulfillment_orders (write implies read). We DON'T request
# assignedLocation.location{id} — that needs read_locations we don't hold; the name is enough.
_FO_GROUPS_Q = """
query($id: ID!) {
  order(id: $id) {
    name
    fulfillmentOrders(first: 20) {
      edges { node {
        id
        status
        assignedLocation { name }
        lineItems(first: 100) {
          edges { node { id remainingQuantity lineItem { sku name } } }
        }
      } }
    }
  }
}
"""

_OPEN_FO_STATUSES = {"OPEN", "IN_PROGRESS", "SCHEDULED"}


_PACKING_Q = """
query($id: ID!, $ns: String!, $key: String!) {
  order(id: $id) {
    lineItems(first: 100) {
      edges {
        node {
          sku
          quantity
          variant {
            metafield(namespace: $ns, key: $key) { value }
            product { metafield(namespace: $ns, key: $key) { value } }
          }
        }
      }
    }
  }
}
"""


async def get_order_packing_units(store: models.Store, shopify_order_id, namespace: str, key: str) -> List[Dict[str, Any]]:
    """Read each line item's packing-density metafield (parcels-per-piece) from Shopify.
    Prefers the VARIANT metafield, falls back to the PRODUCT metafield. Returns
    [{sku, quantity, value|None}] — value None when the metafield is absent/unparseable."""
    order_gid = f"gid://shopify/Order/{str(shopify_order_id).split('/')[-1]}"
    data = await _gql(store, _PACKING_Q, {"id": order_gid, "ns": namespace, "key": key})
    out: List[Dict[str, Any]] = []
    for e in ((((data.get("order") or {}).get("lineItems") or {}).get("edges")) or []):
        n = e.get("node") or {}
        variant = n.get("variant") or {}
        raw = None
        vm = variant.get("metafield") or {}
        if vm.get("value") is not None:
            raw = vm.get("value")
        else:
            pm = (variant.get("product") or {}).get("metafield") or {}
            if pm.get("value") is not None:
                raw = pm.get("value")
        val = None
        if raw is not None:
            try:
                val = float(str(raw).strip().replace(",", "."))
                if val <= 0:
                    val = None
            except (TypeError, ValueError):
                val = None
        out.append({"sku": n.get("sku"), "quantity": int(n.get("quantity") or 0), "value": val})
    return out


def parcels_from_units(items: List[Dict[str, Any]], per_product: bool = False) -> Optional[int]:
    """Total parcels from packing density. value = parcels per piece (0.1 ⇒ 10 pieces/parcel).
    - default (shared): parcels = ceil(Σ quantity·value) — products share parcels by volume.
    - per_product: parcels = Σ ceil(quantity·value) — each product line rounds up on its own
      (use when different products can't share a box).
    Returns None if ANY line item lacks a density (so the caller safely falls back to the
    profile's default parcel count instead of under-parcelling). Epsilon guards 10×0.1 → 1."""
    import math
    pairs = []
    for it in items:
        v = it.get("value")
        if v is None:
            return None
        pairs.append((int(it.get("quantity") or 0), float(v)))
    if not pairs:
        return None
    if per_product:
        return max(1, sum(math.ceil(q * v - 1e-9) for q, v in pairs if q > 0))
    return max(1, math.ceil(sum(q * v for q, v in pairs) - 1e-9))


_PRODUCTS_Q = """
query($n: Int!, $q: String, $cursor: String) {
  products(first: $n, after: $cursor, query: $q) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      featuredMedia { preview { image { url } } }
      variants(first: 10) {
        nodes {
          id
          sku
          barcode
          inventoryItem { id measurement { weight { value unit } } }
        }
      }
    }
  }
}
"""

_WEIGHT_M = """
mutation($id: ID!, $input: InventoryItemInput!) {
  inventoryItemUpdate(id: $id, input: $input) {
    inventoryItem { id }
    userErrors { field message }
  }
}
"""

_WEIGHT_TO_KG = {"KILOGRAMS": 1.0, "GRAMS": 0.001, "POUNDS": 0.45359237, "OUNCES": 0.0283495231}


def _weight_kg(value, unit) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value) * _WEIGHT_TO_KG.get((unit or "KILOGRAMS").upper(), 1.0), 3)
    except (TypeError, ValueError):
        return None


async def list_products(store: models.Store, q: Optional[str] = None,
                        cursor: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    """Page the shop's products for the packing browser: title, image, variants (sku + weight in
    KG + inventory-item id for write-back). `q` is a Shopify product search string."""
    data = await _gql(store, _PRODUCTS_Q, {"n": min(max(limit, 1), 100), "q": q or None, "cursor": cursor})
    products = data.get("products") or {}
    out: List[Dict[str, Any]] = []
    for p in (products.get("nodes") or []):
        img = ((((p.get("featuredMedia") or {}).get("preview") or {}).get("image")) or {}).get("url")
        variants = []
        for v in (((p.get("variants") or {}).get("nodes")) or []):
            inv = v.get("inventoryItem") or {}
            w = (inv.get("measurement") or {}).get("weight") or {}
            variants.append({
                "id": v.get("id"), "sku": v.get("sku"), "barcode": v.get("barcode"),
                "inventory_item_id": inv.get("id"),
                "weight_kg": _weight_kg(w.get("value"), w.get("unit")),
            })
        out.append({"id": p.get("id"), "title": p.get("title"), "image": img, "variants": variants})
    page = products.get("pageInfo") or {}
    return {"products": out, "next_cursor": page.get("endCursor") if page.get("hasNextPage") else None}


_VARIANT_BARCODE_M = """
mutation setBarcodes($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id sku barcode }
    userErrors { field message }
  }
}
"""


async def set_variant_barcodes(store: models.Store, product_id: str,
                               variants: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Write barcodes to variants of ONE product (needs write_products). variants=[{id, barcode}].
    Returns [{id, sku, barcode}]. Raises RuntimeError on userErrors."""
    data = await _gql(store, _VARIANT_BARCODE_M, {
        "productId": product_id,
        "variants": [{"id": v["id"], "barcode": v["barcode"]} for v in variants]})
    res = data.get("productVariantsBulkUpdate") or {}
    errs = res.get("userErrors") or []
    if errs:
        raise RuntimeError(f"barcode update: {errs}")
    return res.get("productVariants") or []


async def set_variant_weight(store: models.Store, inventory_item_id: str, kg: float) -> None:
    """Write a variant's weight back to Shopify (stored on the inventory item, in KILOGRAMS)."""
    data = await _gql(store, _WEIGHT_M, {
        "id": inventory_item_id,
        "input": {"measurement": {"weight": {"value": float(kg), "unit": "KILOGRAMS"}}},
    })
    errs = ((data.get("inventoryItemUpdate") or {}).get("userErrors")) or []
    if errs:
        raise RuntimeError(f"weight update: {errs}")


_INV_BY_SKU_Q = """
query invBySku($q: String!) {
  productVariants(first: 1, query: $q) {
    edges { node { sku title
      inventoryItem { id tracked
        inventoryLevels(first: 10) {
          edges { node { location { id name }
            quantities(names: ["available"]) { name quantity } } } } } } }
  }
}
"""

_INV_ADJUST_M = """
mutation invAdjust($input: InventoryAdjustQuantitiesInput!) {
  inventoryAdjustQuantities(input: $input) {
    inventoryAdjustmentGroup { createdAt reason }
    userErrors { field message }
  }
}
"""


async def adjust_inventory_by_sku(store: models.Store, sku: str, delta: int,
                                  *, reason: str = "correction") -> Dict[str, Any]:
    """Adjust 'available' stock for a SKU by `delta` (+/-) at the location that holds it (the first
    tracked location). Needs write_inventory. Returns {sku, title, location, delta, new_available}.
    Raises RuntimeError on not-found / untracked / no-location / userErrors."""
    sku = (sku or "").strip()
    if not sku:
        raise RuntimeError("Empty SKU.")
    if not int(delta):
        raise RuntimeError("The adjustment quantity is 0.")
    data = await _gql(store, _INV_BY_SKU_Q, {"q": f'sku:"{sku.replace(chr(34), "")}"'})
    edges = ((data.get("productVariants") or {}).get("edges")) or []
    if not edges:
        raise RuntimeError(f"SKU \u201c{sku}\u201d not found in Shopify.")
    node = edges[0].get("node") or {}
    inv = node.get("inventoryItem") or {}
    inv_id = inv.get("id")
    if not inv_id:
        raise RuntimeError(f"SKU \u201c{sku}\u201d has no inventory item.")
    if not inv.get("tracked"):
        raise RuntimeError(f"SKU \u201c{sku}\u201d: inventory isn't tracked in Shopify (enable tracking).")
    levels = (((inv.get("inventoryLevels") or {}).get("edges")) or [])
    if not levels:
        raise RuntimeError(f"SKU \u201c{sku}\u201d has no inventory location.")
    lvl = levels[0].get("node") or {}
    loc = lvl.get("location") or {}
    loc_id = loc.get("id")
    cur = next((q.get("quantity") for q in (lvl.get("quantities") or []) if q.get("name") == "available"), None)
    res = await _gql(store, _INV_ADJUST_M, {"input": {
        "reason": reason, "name": "available",
        "changes": [{"inventoryItemId": inv_id, "locationId": loc_id, "delta": int(delta)}]}})
    errs = ((res.get("inventoryAdjustQuantities") or {}).get("userErrors")) or []
    if errs:
        raise RuntimeError(f"inventar: {errs}")
    new_avail = (int(cur) + int(delta)) if cur is not None else None
    return {"sku": node.get("sku") or sku, "title": node.get("title"), "location": loc.get("name"),
            "delta": int(delta), "new_available": new_avail}


async def get_fulfillment_order_groups(store: models.Store, shopify_order_id) -> List[Dict[str, Any]]:
    """Return the order's fulfillment orders grouped by location, each with its still-to-ship
    line items: [{fo_id, status, location, open, items:[{sku,name,qty}]}]. Used by the
    multi-location split to make one AWB per location."""
    order_gid = f"gid://shopify/Order/{str(shopify_order_id).split('/')[-1]}"
    data = await _gql(store, _FO_GROUPS_Q, {"id": order_gid})
    order = data.get("order") or {}
    groups: List[Dict[str, Any]] = []
    for e in (((order.get("fulfillmentOrders") or {}).get("edges")) or []):
        node = e.get("node") or {}
        items = []
        for le in (((node.get("lineItems") or {}).get("edges")) or []):
            ln = le.get("node") or {}
            li = ln.get("lineItem") or {}
            qty = int(ln.get("remainingQuantity") or 0)
            if qty <= 0:
                continue
            items.append({"sku": li.get("sku"), "name": li.get("name"), "qty": qty})
        status = node.get("status")
        groups.append({
            "fo_id": node.get("id"),
            "status": status,
            "location": ((node.get("assignedLocation") or {}).get("name")) or "",
            "open": status in _OPEN_FO_STATUSES and bool(items),
            "items": items,
        })
    return groups


async def create_fulfillment_for_fo(
    store: models.Store, fulfillment_order_id: str, *,
    tracking_number: str, tracking_company: Optional[str] = None,
    tracking_url: Optional[str] = None, notify_customer: bool = False,
) -> Optional[str]:
    """Fulfill ONE specific fulfillment order (a single location) with its AWB as tracking.
    Returns the created Fulfillment GID (or None if that FO is no longer fulfillable)."""
    tracking_info: Dict[str, Any] = {"number": tracking_number}
    if tracking_company:
        tracking_info["company"] = tracking_company
    if tracking_url:
        tracking_info["url"] = tracking_url
    variables = {"fulfillment": {
        "lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": fulfillment_order_id}],
        "trackingInfo": tracking_info,
        "notifyCustomer": bool(notify_customer),
    }}
    data = await _gql(store, _FULFILLMENT_CREATE_M, variables)
    res = data.get("fulfillmentCreate") or {}
    errs = res.get("userErrors") or []
    if errs:
        raise RuntimeError("; ".join(e.get("message", "Unknown error") for e in errs))
    return (res.get("fulfillment") or {}).get("id")


# --------------------
# Fulfillment holds (CS backlog) + order note / tags
# --------------------

# A fulfillment order can be HELD only while it is still fulfillable; a shipped one can't.
_HOLDABLE_FO_STATUSES = {"OPEN", "IN_PROGRESS", "SCHEDULED"}

_FO_HOLD_M = """
mutation fulfillmentOrderHold($fulfillmentHold: FulfillmentOrderHoldInput!, $id: ID!) {
  fulfillmentOrderHold(fulfillmentHold: $fulfillmentHold, id: $id) {
    fulfillmentOrder { id status }
    userErrors { field message }
  }
}
"""

_FO_RELEASE_M = """
mutation fulfillmentOrderReleaseHold($id: ID!) {
  fulfillmentOrderReleaseHold(id: $id) {
    fulfillmentOrder { id status }
    userErrors { field message }
  }
}
"""

# reason -> FulfillmentHoldReason enum; anything else falls back to OTHER (always valid).
_HOLD_REASON_ENUM = {
    "wrong_address": "INCORRECT_ADDRESS",
    "incorrect_address": "INCORRECT_ADDRESS",
    "out_of_stock": "INVENTORY_OUT_OF_STOCK",
    "fraud": "HIGH_RISK_OF_FRAUD",
    "awaiting_payment": "AWAITING_PAYMENT",
}


async def _order_fulfillment_orders(store: models.Store, shopify_order_id) -> List[Dict[str, Any]]:
    """[{id, status}] for the order's fulfillment orders."""
    order_gid = f"gid://shopify/Order/{str(shopify_order_id).split('/')[-1]}"
    data = await _gql(store, _OPEN_FO_Q, {"id": order_gid})
    order = data.get("order") or {}
    out = []
    for e in (((order.get("fulfillmentOrders") or {}).get("edges")) or []):
        node = e.get("node") or {}
        if node.get("id"):
            out.append({"id": node["id"], "status": node.get("status")})
    return out


async def hold_fulfillment_orders(
    store: models.Store, shopify_order_id, *,
    reason: str = "manual", notes: Optional[str] = None, notify_merchant: bool = False,
) -> int:
    """Put every still-fulfillable fulfillment order of the order ON HOLD (so it can't be
    shipped until released). Idempotent: already-held/shipped FOs are skipped. Returns the
    number of FOs newly held. Raises only on a hard GraphQL/transport error."""
    fos = await _order_fulfillment_orders(store, shopify_order_id)
    enum = _HOLD_REASON_ENUM.get((reason or "").strip().lower(), "OTHER")
    hold: Dict[str, Any] = {"reason": enum, "notifyMerchant": bool(notify_merchant)}
    if notes:
        hold["reasonNotes"] = str(notes)[:500]
    held = 0
    for fo in fos:
        if fo["status"] not in _HOLDABLE_FO_STATUSES:
            continue
        data = await _gql(store, _FO_HOLD_M, {"fulfillmentHold": hold, "id": fo["id"]})
        res = data.get("fulfillmentOrderHold") or {}
        errs = res.get("userErrors") or []
        if errs:
            # A single FO refusing (e.g. already held in a race) shouldn't abort the rest.
            logger.info("hold FO %s failed: %s", fo["id"], errs)
            continue
        held += 1
    return held


async def release_fulfillment_order_holds(store: models.Store, shopify_order_id) -> int:
    """Release the hold on every ON_HOLD fulfillment order of the order. Returns how many
    were released. Safe to call when nothing is held (returns 0)."""
    fos = await _order_fulfillment_orders(store, shopify_order_id)
    released = 0
    for fo in fos:
        if fo["status"] != "ON_HOLD":
            continue
        data = await _gql(store, _FO_RELEASE_M, {"id": fo["id"]})
        res = data.get("fulfillmentOrderReleaseHold") or {}
        errs = res.get("userErrors") or []
        if errs:
            logger.info("release FO %s failed: %s", fo["id"], errs)
            continue
        released += 1
    return released


_ORDER_UPDATE_M = """
mutation orderUpdate($input: OrderInput!) {
  orderUpdate(input: $input) {
    order { id note }
    userErrors { field message }
  }
}
"""

_TAGS_ADD_M = """
mutation tagsAdd($id: ID!, $tags: [String!]!) {
  tagsAdd(id: $id, tags: $tags) { userErrors { field message } }
}
"""

_TAGS_REMOVE_M = """
mutation tagsRemove($id: ID!, $tags: [String!]!) {
  tagsRemove(id: $id, tags: $tags) { userErrors { field message } }
}
"""


async def update_order_note(store: models.Store, shopify_order_id, note: str) -> None:
    """Set the order's note in Shopify (durable, visible to the merchant everywhere)."""
    order_gid = f"gid://shopify/Order/{str(shopify_order_id).split('/')[-1]}"
    data = await _gql(store, _ORDER_UPDATE_M, {"input": {"id": order_gid, "note": note}})
    errs = (data.get("orderUpdate") or {}).get("userErrors") or []
    if errs:
        raise RuntimeError("; ".join(e.get("message", "Unknown error") for e in errs))


async def add_order_tags(store: models.Store, shopify_order_id, tags: List[str]) -> None:
    """Add Shopify order tags (cross-system flags the merchant can filter on)."""
    tags = [t.strip() for t in (tags or []) if t and t.strip()]
    if not tags:
        return
    order_gid = f"gid://shopify/Order/{str(shopify_order_id).split('/')[-1]}"
    data = await _gql(store, _TAGS_ADD_M, {"id": order_gid, "tags": tags})
    errs = (data.get("tagsAdd") or {}).get("userErrors") or []
    if errs:
        raise RuntimeError("; ".join(e.get("message", "Unknown error") for e in errs))


async def remove_order_tags(store: models.Store, shopify_order_id, tags: List[str]) -> None:
    tags = [t.strip() for t in (tags or []) if t and t.strip()]
    if not tags:
        return
    order_gid = f"gid://shopify/Order/{str(shopify_order_id).split('/')[-1]}"
    data = await _gql(store, _TAGS_REMOVE_M, {"id": order_gid, "tags": tags})
    errs = (data.get("tagsRemove") or {}).get("userErrors") or []
    if errs:
        raise RuntimeError("; ".join(e.get("message", "Unknown error") for e in errs))


_ORDER_INVOICE_SEND_M = """
mutation orderInvoiceSend($orderId: ID!, $email: EmailInput) {
  orderInvoiceSend(id: $orderId, email: $email) {
    order { id }
    userErrors { field message }
  }
}
"""


_ORDER_ADDR_UPDATE_M = """
mutation orderUpdate($input: OrderInput!) {
  orderUpdate(input: $input) {
    order { id shippingAddress { address1 address2 city zip province country phone name } }
    userErrors { field message }
  }
}
"""


async def update_order_shipping_address(store: models.Store, shopify_order_id, addr: Dict[str, Any]) -> Dict[str, Any]:
    """Write the order's shipping address in Shopify (works for ALL orders, including
    Releaseit/COD-form — only the *products* of those are locked, not the address). `addr`
    keys: name, address1, address2, city, zip, province, country, phone (any subset).
    Returns the saved shippingAddress. Raises on userErrors."""
    order_gid = f"gid://shopify/Order/{str(shopify_order_id).split('/')[-1]}"
    ma: Dict[str, Any] = {}
    for k in ("address1", "address2", "city", "zip", "province", "country", "phone"):
        if addr.get(k) is not None:
            ma[k] = str(addr[k])
    name = addr.get("name")
    if name is not None:
        parts = str(name).strip().split(" ", 1)
        ma["firstName"] = parts[0]
        ma["lastName"] = parts[1] if len(parts) > 1 else ""
    data = await _gql(store, _ORDER_ADDR_UPDATE_M, {"input": {"id": order_gid, "shippingAddress": ma}})
    res = data.get("orderUpdate") or {}
    errs = res.get("userErrors") or []
    if errs:
        raise RuntimeError("; ".join(e.get("message", "Unknown error") for e in errs))
    return (res.get("order") or {}).get("shippingAddress") or {}


async def send_order_email(
    store: models.Store, shopify_order_id, *,
    subject: str, body: str, to: Optional[str] = None,
) -> None:
    """Email the customer THROUGH Shopify — the same channel the admin's order page uses
    ("Send invoice"): from the store's address, logged on the order timeline, customer
    replies to the store. `subject`/`body` are the merchant's rendered template."""
    order_gid = f"gid://shopify/Order/{str(shopify_order_id).split('/')[-1]}"
    email: Dict[str, Any] = {"subject": (subject or "")[:255], "customMessage": body or ""}
    if to:
        email["to"] = to
    data = await _gql(store, _ORDER_INVOICE_SEND_M, {"orderId": order_gid, "email": email})
    errs = (data.get("orderInvoiceSend") or {}).get("userErrors") or []
    if errs:
        raise RuntimeError("; ".join(e.get("message", "Unknown error") for e in errs))
