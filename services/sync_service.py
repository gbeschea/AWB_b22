# /services/sync_service.py

import asyncio
import logging
import json
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
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

from models import Order, Store


def _dt(v: Optional[str]) -> Optional[datetime]:
    if not v: return None
    try:
        return datetime.fromisoformat(v.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None

def map_payment_method(gateways: List[str], financial_status: str) -> str:
    raw_gateways = gateways or []
    lower_gateways_set = {g.lower().strip() for g in raw_gateways}
    
    if settings.PAYMENT_MAP:
        for standard_name, keywords in settings.PAYMENT_MAP.items():
            if not lower_gateways_set.isdisjoint(keywords):
                return standard_name

    if financial_status.lower() == 'paid': return 'card'
    return 'unknown'

async def _process_and_insert_orders_in_batches(db: AsyncSession, orders_data: List[Dict[str, Any]], store_id: int, pii_source: str):
    BATCH_SIZE = 200
    total_processed = 0
    for i in range(0, len(orders_data), BATCH_SIZE):
        batch_data = orders_data[i:i + BATCH_SIZE]
        logger.info(f"Procesare lot de {len(batch_data)} comenzi (de la comanda #{i+1})...")
        orders_to_insert = []
        for order_data in batch_data:
            shopify_id_str = order_data['id'].split('/')[-1]
            customer_info = order_data.get('customer')
            customer_name = f"{customer_info['firstName'] or ''} {customer_info['lastName'] or ''}".strip() if customer_info else 'N/A'
            shipping_address = order_data.get('shippingAddress') or {}
            payment_gateways = order_data.get('paymentGatewayNames', [])
            financial_status = order_data.get('displayFinancialStatus', '')
            order_payload = {
                'store_id': store_id,
                'shopify_order_id': shopify_id_str,
                'name': order_data.get('name', f"#{shopify_id_str}"),
                'created_at': _dt(order_data.get('createdAt')),
                'financial_status': financial_status,
                'total_price': float(order_data['totalPriceSet']['shopMoney']['amount']) if order_data.get('totalPriceSet') else None,
                'payment_gateway_names': ", ".join(payment_gateways),
                'mapped_payment': map_payment_method(payment_gateways, financial_status),
                'tags': ", ".join(order_data.get('tags', [])),
                'note': order_data.get('note'),
                'sync_status': 'synced',
                'last_sync_at': datetime.now(timezone.utc),
                'shopify_status': order_data.get('displayFulfillmentStatus', '').lower(),
            }
            if pii_source == 'shopify':
                order_payload.update({
                    'customer': customer_name,
                    'shipping_name': f"{shipping_address.get('firstName') or ''} {shipping_address.get('lastName') or ''}".strip(),
                    'shipping_address1': shipping_address.get('address1'),
                    'shipping_address2': shipping_address.get('address2'),
                    'shipping_phone': shipping_address.get('phone'),
                    'shipping_city': shipping_address.get('city'),
                    'shipping_zip': shipping_address.get('zip'),
                    'shipping_province': shipping_address.get('province'),
                    'shipping_country': shipping_address.get('country'),
                })
            orders_to_insert.append(order_payload)
        if not orders_to_insert:
            continue
        stmt = pg_insert(models.Order).values(orders_to_insert)
        update_dict = {c.name: c for c in stmt.excluded if c.name not in ['id', 'shopify_order_id', 'store_id']}
        stmt = stmt.on_conflict_do_update(index_elements=['shopify_order_id'], set_=update_dict).returning(models.Order.id, models.Order.shopify_order_id)
        result = await db.execute(stmt)
        order_id_map = {shopify_id: internal_id for internal_id, shopify_id in result.fetchall()}
        shipments_to_insert = []
        for order_data in batch_data:
            shopify_order_id = order_data['id'].split('/')[-1]
            internal_order_id = order_id_map.get(shopify_order_id)
            if not internal_order_id:
                continue
            for fulfillment in order_data.get('fulfillments', []):
                tracking_info = fulfillment.get('trackingInfo', [{}])[0]
                if not tracking_info.get('number'):
                    continue
                shipment_payload = {
                    'order_id': internal_order_id,
                    'shopify_fulfillment_id': fulfillment['id'].split('/')[-1],
                    'fulfillment_created_at': _dt(fulfillment.get('createdAt')),
                    'awb': tracking_info.get('number'),
                    'courier': tracking_info.get('company', 'Unknown').strip(),
                    'last_status': fulfillment.get('displayStatus'),
                    'account_key': (tracking_info.get('company') or 'default').lower().replace(' ', '')
                }
                shipments_to_insert.append(shipment_payload)
        if shipments_to_insert:
            shipment_stmt = pg_insert(models.Shipment).values(shipments_to_insert)
            shipment_update_dict = {c.name: c for c in shipment_stmt.excluded if c.name not in ['id', 'shopify_fulfillment_id']}
            shipment_stmt = shipment_stmt.on_conflict_do_update(index_elements=['shopify_fulfillment_id'], set_=shipment_update_dict)
            await db.execute(shipment_stmt)
        if pii_source == 'shopify':
            for order_id in order_id_map.values():
                order = await db.get(models.Order, order_id)
                if order and order.address_status != 'validat':
                    await address_service.validate_address_for_order(db, order)
        await db.commit()
        total_processed += len(orders_to_insert)
        logger.info(f"Lotul a fost salvat. Total procesate până acum: {total_processed}")
    return total_processed

async def run_orders_sync(db: AsyncSession, days: int, full_sync: bool = False):
    logger.info("Începe sincronizarea comenzilor...")
    await manager.broadcast(json.dumps({"event": "sync:start", "data": {"type": "orders"}}))
    total_synced_count = 0
    start_ts = datetime.now(timezone.utc)
    active_stores_q = await db.execute(select(Store).where(Store.is_active == True))
    active_stores = active_stores_q.scalars().all()
    for store in active_stores:
        logger.info(f"Procesare magazin: {store.name}")
        await manager.broadcast(json.dumps({"event": "sync:progress", "data": {"store": store.name, "status": "fetching"}}))
        created_at_min = datetime.now(timezone.utc) - timedelta(days=days)
        created_at_max = datetime.now(timezone.utc)
        orders_data = await shopify_service.fetch_orders(db, store.id, created_at_min, created_at_max)
        await manager.broadcast(json.dumps({"event": "sync:progress", "data": {"store": store.name, "status": "processing", "count": len(orders_data)}}))
        processed_count = await _process_and_insert_orders_in_batches(db, orders_data, store.id, store.pii_source)
        total_synced_count += processed_count
        store.last_sync_at = datetime.now(timezone.utc)
        db.add(store)
        await db.commit()
    await manager.broadcast(json.dumps({"event": "sync:finish", "data": {"type": "orders", "total": total_synced_count}}))
    logger.info(f"Sincronizare comenzi finalizată. Total procesate: {total_synced_count} în {(datetime.now(timezone.utc) - start_ts).total_seconds():.1f}s.")

async def sync_orders_for_stores(store_ids: List[int], created_at_min: datetime, created_at_max: datetime):
    logger.info(f"Începe sincronizarea comenzilor pentru magazinele: {store_ids}...")
    logger.info(f"Interval de date: {created_at_min.strftime('%Y-%m-%d')} -> {created_at_max.strftime('%Y-%m-%d')}")
    total_synced_count = 0
    start_ts = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        for store_id in store_ids:
            logger.info(f"Procesare magazin ID: {store_id}")
            try:
                store = await db.get(Store, store_id)
                if not store:
                    logger.warning(f"Magazinul cu ID-ul {store_id} nu a fost găsit. Se omite.")
                    continue
                orders_data = await shopify_service.fetch_orders(db, store.id, created_at_min, created_at_max)
                if orders_data:
                    logger.info(f"S-au preluat {len(orders_data)} comenzi de la Shopify pentru magazinul {store.name}.")
                    processed_count = await _process_and_insert_orders_in_batches(db, orders_data, store.id, store.pii_source)
                    total_synced_count += processed_count
                    logger.info(f"S-au procesat și salvat {processed_count} comenzi pentru magazinul {store.name}.")
                else:
                    logger.info(f"Nu s-au găsit comenzi noi în intervalul specificat pentru magazinul {store.name}.")
                store.last_sync_at = datetime.now(timezone.utc)
                db.add(store)
                await db.commit()
            except Exception as e:
                logger.error(f"Eroare la sincronizarea magazinului {store_id}: {e}", exc_info=True)
    end_ts = datetime.now(timezone.utc)
    logger.info(f"Sincronizare finalizată. Total comenzi procesate: {total_synced_count} în {(end_ts - start_ts).total_seconds():.2f} secunde.")

async def run_couriers_sync(db: AsyncSession, full_sync: bool = False):
    await courier_service.track_and_update_shipments(db, full_sync=full_sync)

async def run_full_sync(db: AsyncSession, days: int):
    await run_orders_sync(db, days, full_sync=True)
    await run_couriers_sync(db, full_sync=True)

async def run_address_validation_for_all_orders(db: AsyncSession):
    logger.info("Începe validarea adreselor pentru toate comenzile...")
    stmt = select(Order)
    result = await db.execute(stmt)
    all_orders = result.scalars().all()
    total = len(all_orders)
    logger.info(f"S-au găsit {total} comenzi pentru validare.")
    for i, order in enumerate(all_orders):
        try:
            await address_service.validate_address_for_order(db, order)
        except Exception as e:
            logger.error(f"Eroare la validarea comenzii {order.name}: {e}")
        if (i + 1) % 100 == 0:
            await db.commit()
            logger.info(f"Progres validare: {i + 1} / {total}")
    await db.commit()
    logger.info("Validarea adreselor pentru toate comenzile a fost finalizată.")