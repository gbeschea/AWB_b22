import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

import models
from settings import settings
from services import shopify_service, address_service, courier_service
from websocket_manager import manager
from database import AsyncSessionLocal

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

Order = models.Order
Store = models.Store
Shipment = models.Shipment


def _dt(v: Optional[str]) -> Optional[datetime]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def map_payment_method(gateways: List[str], financial_status: str) -> str:
    """
    Normalizează metoda de plată folosind settings.PAYMENT_MAP când există.
    """
    raw = gateways or []
    lowered = {g.lower().strip() for g in raw}

    # hartă configurabilă
    if settings.PAYMENT_MAP:
        for standard, keywords in settings.PAYMENT_MAP.items():
            if not lowered.isdisjoint(keywords):
                return standard

    # fallback simplu
    if (financial_status or "").lower() == "paid":
        return "card"
    return "unknown"


def _normalize_account_key(company: Optional[str]) -> str:
    """
    Deducem account_key din numele curierului (pentru a lega credențialele).
    """
    if not company:
        return "default"
    s = company.strip().lower()
    # normalizări uzuale
    if "dpd" in s:
        return "dpdromania" if "romania" in s else "dpd"
    if "sameday" in s:
        return "sameday"
    return s.replace(" ", "")


async def _process_and_insert_orders_in_batches(
    db: AsyncSession,
    orders_data: List[Dict[str, Any]],
    store_id: int,
    pii_source: str,
) -> int:
    """
    Inserează/actualizează comenzile + shipment-urile asociate în loturi pentru performanță.
    """
    BATCH = 200
    total = 0
    include_pii = (pii_source or "").lower() == "shopify"

    for i in range(0, len(orders_data), BATCH):
        batch = orders_data[i : i + BATCH]
        logger.info("Procesare lot de %s comenzi (de la #%s)...", len(batch), i + 1)

        to_upsert_orders = []
        for o in batch:
            shopify_id = o["id"].split("/")[-1]
            shipping_address = o.get("shippingAddress") or {}
            customer = o.get("customer")
            customer_name = (
                f"{(customer or {}).get('firstName') or ''} {(customer or {}).get('lastName') or ''}".strip()
                if customer
                else None
            )
            gateways = o.get("paymentGatewayNames") or []
            financial_status = o.get("displayFinancialStatus") or ""

            payload = {
                "store_id": store_id,
                "shopify_order_id": shopify_id,
                "name": o.get("name", f"#{shopify_id}"),
                "created_at": _dt(o.get("createdAt")),
                "financial_status": financial_status,
                "total_price": float(((o.get("totalPriceSet") or {}).get("shopMoney") or {}).get("amount"))
                if o.get("totalPriceSet")
                else None,
                "payment_gateway_names": ", ".join(gateways),
                "mapped_payment": map_payment_method(gateways, financial_status),
                "tags": ", ".join(o.get("tags") or []),
                "note": o.get("note"),
                "sync_status": "synced",
                "last_sync_at": datetime.now(timezone.utc),
                "shopify_status": (o.get("displayFulfillmentStatus") or "").lower(),
            }

            if include_pii:
                payload.update(
                    {
                        "customer": customer_name,
                        "shipping_name": f"{shipping_address.get('firstName') or ''} {shipping_address.get('lastName') or ''}".strip()
                        or None,
                        "shipping_address1": shipping_address.get("address1"),
                        "shipping_address2": shipping_address.get("address2"),
                        "shipping_phone": shipping_address.get("phone"),
                        "shipping_city": shipping_address.get("city"),
                        "shipping_zip": shipping_address.get("zip"),
                        "shipping_province": shipping_address.get("province"),
                        "shipping_country": shipping_address.get("country"),
                    }
                )

            to_upsert_orders.append(payload)

        if not to_upsert_orders:
            continue

        # upsert pe orders (unique: shopify_order_id)
        stmt = pg_insert(Order).values(to_upsert_orders)
        update_cols = {c.name: c for c in stmt.excluded if c.name not in ("id", "shopify_order_id", "store_id")}
        stmt = stmt.on_conflict_do_update(index_elements=["shopify_order_id"], set_=update_cols).returning(
            Order.id, Order.shopify_order_id
        )
        result = await db.execute(stmt)
        order_id_map = {shopify_id: oid for oid, shopify_id in result.fetchall()}

        # construim shipment-urile
        to_upsert_shipments = []
        for o in batch:
            internal_id = order_id_map.get(o["id"].split("/")[-1])
            if not internal_id:
                continue

            for f in o.get("fulfillments") or []:
                info = (f.get("trackingInfo") or [{}])[0]
                number = (info or {}).get("number")
                if not number:
                    continue

                to_upsert_shipments.append(
                    {
                        "order_id": internal_id,
                        "shopify_fulfillment_id": f["id"].split("/")[-1],
                        "fulfillment_created_at": _dt(f.get("createdAt")),
                        "awb": number,
                        "courier": (info or {}).get("company") or "Unknown",
                        "last_status": f.get("displayStatus"),
                        "account_key": _normalize_account_key((info or {}).get("company")),
                    }
                )

        if to_upsert_shipments:
            s_stmt = pg_insert(Shipment).values(to_upsert_shipments)
            allowed = {"order_id", "fulfillment_created_at", "awb", "courier", "account_key", "last_status"}
            s_update = {k: getattr(s_stmt.excluded, k) for k in allowed}
            s_stmt = s_stmt.on_conflict_do_update(index_elements=["shopify_fulfillment_id"], set_=s_update)
            await db.execute(s_stmt)

        # validarea adreselor – doar dacă am PII din Shopify
        if include_pii:
            for oid in order_id_map.values():
                order = await db.get(Order, oid)
                if order and (order.address_status or "").lower() != "validat":
                    try:
                        await address_service.validate_address_for_order(db, order)
                    except Exception:
                        logger.exception("Validare adresă eșuată pentru %s", order.name)

        await db.commit()
        total += len(to_upsert_orders)
        logger.info("Lotul a fost salvat. Total procesate până acum: %s", total)

    return total


# ---------------------------
# Rulări/ orchestration
# ---------------------------

async def sync_orders_for_stores(store_ids: List[int], created_at_min: datetime, created_at_max: datetime) -> int:
    """
    Sincronizează comenzile pt. store-urile indicate (fără curieri).
    """
    logger.info("Începe sincronizarea comenzilor pentru magazinele: %s...", store_ids)
    logger.info("Interval de date: %s -> %s", created_at_min.strftime("%Y-%m-%d"), created_at_max.strftime("%Y-%m-%d"))

    total_synced = 0
    async with AsyncSessionLocal() as db:
        for sid in store_ids:
            logger.info("Procesare magazin ID: %s", sid)
            try:
                store = await db.get(Store, sid)
                if not store:
                    logger.warning("Magazinul %s nu există; se omite.", sid)
                    continue

                orders = await shopify_service.fetch_orders(db, sid, created_at_min, created_at_max)
                if orders:
                    logger.info("S-au preluat %s comenzi din Shopify pentru %s.", len(orders), store.name)
                    cnt = await _process_and_insert_orders_in_batches(db, orders, sid, store.pii_source)
                    total_synced += cnt
                    logger.info("S-au procesat și salvat %s comenzi pentru %s.", cnt, store.name)
                else:
                    logger.info("Nu s-au găsit comenzi noi pentru %s.", store.name)

                store.last_sync_at = datetime.now(timezone.utc)
                db.add(store)
                await db.commit()
            except Exception:
                logger.exception("Eroare la sincronizarea magazinului %s", sid)

    logger.info("Sincronizare finalizată. Total comenzi procesate: %s", total_synced)
    return total_synced


async def full_sync_for_stores(
    store_ids: Optional[List[int]],
    created_at_min: datetime,
    created_at_max: datetime,
    with_couriers: bool = True,
) -> None:
    """
    Versiunea "end-to-end" folosită de pagina de Financials.
    Dacă store_ids este None/vid → sincronizează toate magazinele active.
    """
    async with AsyncSessionLocal() as db:
        if not store_ids:
            rows = await db.execute(select(Store.id).where(Store.is_active == True))
            store_ids = [r[0] for r in rows.all()]

    await sync_orders_for_stores(store_ids, created_at_min, created_at_max)

    if with_couriers:
        async with AsyncSessionLocal() as db:
            await courier_service.track_and_update_shipments(db, full_sync=False)


async def run_orders_sync(db: AsyncSession, days: int, full_sync: bool = False) -> None:
    logger.info("Începe sincronizarea comenzilor...")
    await manager.broadcast(json.dumps({"event": "sync:start", "data": {"type": "orders"}}))

    total = 0
    active_q = await db.execute(select(Store).where(Store.is_active == True))
    stores = active_q.scalars().all()
    created_at_min = datetime.now(timezone.utc) - timedelta(days=days)
    created_at_max = datetime.now(timezone.utc)

    for store in stores:
        logger.info("Procesare magazin: %s", store.name)
        await manager.broadcast(json.dumps({"event": "sync:progress", "data": {"store": store.name, "status": "fetching"}}))

        orders = await shopify_service.fetch_orders(db, store.id, created_at_min, created_at_max)
        await manager.broadcast(json.dumps({"event": "sync:progress", "data": {"store": store.name, "status": "processing", "count": len(orders)}}))

        cnt = await _process_and_insert_orders_in_batches(db, orders, store.id, store.pii_source)
        total += cnt

        store.last_sync_at = datetime.now(timezone.utc)
        db.add(store)
        await db.commit()

    await manager.broadcast(json.dumps({"event": "sync:finish", "data": {"type": "orders", "total": total}}))
    logger.info("Sincronizare comenzi finalizată. Total: %s.", total)


async def run_couriers_sync(db: AsyncSession, full_sync: bool = False) -> None:
    await courier_service.track_and_update_shipments(db, full_sync=full_sync)
