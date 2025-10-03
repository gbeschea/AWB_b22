import asyncio
import logging
from datetime import datetime
from typing import Dict, Any, List, Union

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

import models

_logger = logging.getLogger(__name__)

# Cache client httpx per store
_shopify_clients: Dict[int, httpx.AsyncClient] = {}


def _api_version(store: models.Store) -> str:
    # dacă nu ai coloană api_version în tabel, funcția va cădea pe default
    return getattr(store, "api_version", None) or "2025-07"


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



async def get_store_from_db(db: AsyncSession, store_id: int) -> models.Store:
    res = await db.execute(select(models.Store).where(models.Store.id == store_id))
    store = res.scalar_one_or_none()
    if not store:
        raise ValueError(f"Magazinul cu ID-ul {store_id} nu a fost găsit.")
    return store


def _orders_query(include_pii: bool) -> str:
    """
    Construiește query-ul GraphQL. Când include_pii=False, nu cerem câmpuri care
    au nevoie de access la obiectul Customer (evităm ACCESS_DENIED).
    """
    base = """
    query($first: Int!, $cursor: String, $query: String) {
      orders(first: $first, after: $cursor, query: $query) {
        pageInfo { hasNextPage endCursor }
        edges {
          node {
            id
            name
            createdAt
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
        base += """
            customer { firstName lastName }
            shippingAddress {
              firstName lastName address1 address2 city province zip country phone
            }
        """
    base += """
          }
        }
      }
    }
    """
    return base


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
    client = get_shopify_client(store)

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


# --------------------
# Tranzacții / plăți
# --------------------

async def get_transactions(db: AsyncSession, store_id: int, order_id: str) -> List[Dict[str, Any]]:
    store = await get_store_from_db(db, store_id)
    client = get_shopify_client(store)
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
    client = get_shopify_client(store)
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
        raise RuntimeError("Capturarea plății a eșuat în Shopify.")


async def mark_order_as_paid(db: AsyncSession, store_id: int, shopify_order_id: Union[str, int]) -> None:
    store = await get_store_from_db(db, store_id)
    client = get_shopify_client(store)
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
