import base64
import hashlib
import hmac
import logging
from typing import Dict, Any # <-- MODIFICAREA ESTE AICI

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Header, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update

import models
from database import get_db
from services import webhook_service


router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


async def verify_shopify_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_shopify_topic: str = Header(...),
    x_shopify_hmac_sha256: str = Header(...),
    x_shopify_shop_domain: str = Header(...),
) -> Dict[str, Any]:
    """
    Dependință partajată pentru a verifica și a pre-procesa toate webhook-urile.
    """
    raw_body = await request.body()
    
    store_res = await db.execute(select(models.Store).where(models.Store.domain == x_shopify_shop_domain))
    store = store_res.scalar_one_or_none()

    if not store or not store.shared_secret:
        logging.error(f"Webhook primit pentru un magazin neconfigurat sau fără secret: {x_shopify_shop_domain}")
        raise HTTPException(status_code=404, detail="Store not configured or missing secret.")

    digest = hmac.new(store.shared_secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    computed_hmac = base64.b64encode(digest).decode()
    if not hmac.compare_digest(computed_hmac, x_shopify_hmac_sha256):
        logging.error("Verificarea HMAC a eșuat!")
        raise HTTPException(status_code=401, detail="HMAC verification failed.")
    
    payload = await request.json()
    return {"store": store, "payload": payload, "topic": x_shopify_topic, "db": db}

# --- Shopify GDPR / mandatory compliance webhooks ---
# Registered BEFORE the generic catch-all so they win route matching.
# HMAC is verified via the shared `verify_shopify_webhook` dependency (401 on bad HMAC).


@router.post("/customers/data_request", include_in_schema=False)
async def gdpr_customers_data_request(
    common: Dict[str, Any] = Depends(verify_shopify_webhook)
):
    """
    customers/data_request: a store owner requests a customer's data on their behalf.
    We only hold order/shipping data already visible to the merchant in Shopify, so we
    acknowledge. The merchant fulfils the actual data request from Shopify admin.
    """
    payload = common["payload"]
    cust = (payload.get("customer") or {}) if isinstance(payload, dict) else {}
    logging.info(
        "GDPR customers/data_request for shop=%s customer_id=%s (acknowledged).",
        common["store"].domain, cust.get("id"),
    )
    return Response(status_code=200, content="Data request acknowledged.")


@router.post("/customers/redact", include_in_schema=False)
async def gdpr_customers_redact(
    common: Dict[str, Any] = Depends(verify_shopify_webhook)
):
    """
    customers/redact: erase the stored PII for the customer's orders.
    Payload includes `orders_to_redact` (Shopify order ids). We null the shipping/PII
    columns on the matching orders for this store.
    """
    db: AsyncSession = common["db"]
    store = common["store"]
    payload = common["payload"]

    order_ids = [str(x) for x in (payload.get("orders_to_redact") or [])]
    if order_ids:
        await db.execute(
            update(models.Order)
            .where(
                models.Order.store_id == store.id,
                models.Order.shopify_order_id.in_(order_ids),
            )
            .values(
                customer=None,
                note=None,
                shipping_name=None,
                shipping_address1=None,
                shipping_address2=None,
                shipping_phone=None,
                shipping_city=None,
                shipping_zip=None,
                shipping_province=None,
                shipping_country=None,
            )
        )
        await db.commit()

    logging.info(
        "GDPR customers/redact for shop=%s redacted %d order(s).",
        store.domain, len(order_ids),
    )
    return Response(status_code=200, content="Customer data redacted.")


@router.post("/shop/redact", include_in_schema=False)
async def gdpr_shop_redact(
    common: Dict[str, Any] = Depends(verify_shopify_webhook)
):
    """
    shop/redact: 48h after a store uninstalls, erase ALL of that shop's data.
    Deletes in FK-safe order: order children -> orders -> store category links -> store.
    Idempotent: a re-delivery for an already-removed shop 404s at HMAC lookup, which
    Shopify tolerates.
    """
    db: AsyncSession = common["db"]
    store = common["store"]

    order_ids_subq = (
        select(models.Order.id)
        .where(models.Order.store_id == store.id)
        .scalar_subquery()
    )
    # Children of orders first (no ON DELETE CASCADE at the DB level).
    await db.execute(delete(models.LineItem).where(models.LineItem.order_id.in_(order_ids_subq)))
    await db.execute(delete(models.Shipment).where(models.Shipment.order_id.in_(order_ids_subq)))
    await db.execute(delete(models.FulfillmentOrder).where(models.FulfillmentOrder.order_id.in_(order_ids_subq)))
    await db.execute(delete(models.AddressValidation).where(models.AddressValidation.order_id.in_(order_ids_subq)))
    # Orders, then the many-to-many category links, then the store row itself.
    await db.execute(delete(models.Order).where(models.Order.store_id == store.id))
    await db.execute(
        models.store_category_map.delete().where(
            models.store_category_map.c.store_id == store.id
        )
    )
    await db.execute(delete(models.Store).where(models.Store.id == store.id))
    await db.commit()

    logging.info("GDPR shop/redact: erased all data for shop=%s (store_id=%s).",
                 store.domain, store.id)
    return Response(status_code=200, content="Shop data erased.")


# --- App lifecycle ---


@router.post("/app/uninstalled", include_in_schema=False)
async def app_uninstalled(
    common: Dict[str, Any] = Depends(verify_shopify_webhook)
):
    """
    app/uninstalled: the merchant removed the app. Deactivate the store (soft) and clear
    its token so no further API calls are attempted. Data is erased later by shop/redact.
    """
    db: AsyncSession = common["db"]
    store = common["store"]
    await db.execute(
        update(models.Store)
        .where(models.Store.id == store.id)
        .values(is_active=False, access_token=None)
    )
    await db.commit()
    logging.info("app/uninstalled: deactivated shop=%s (store_id=%s).", store.domain, store.id)
    return Response(status_code=200, content="Uninstall processed.")


# --- Endpoint-uri Separate pentru Fiecare Topic ---

@router.post("/{topic:path}", include_in_schema=False)
async def receive_generic_webhook(
    background_tasks: BackgroundTasks,
    common: Dict[str, Any] = Depends(verify_shopify_webhook)
):
    """
    Un singur endpoint dinamic care prinde toate căile și le trimite la procesor.
    """
    topic = common["topic"]
    
    # Verificăm dacă există un handler specific pentru acest topic în serviciul nostru
    if topic in webhook_service.WEBHOOK_HANDLERS:
        background_tasks.add_task(
            webhook_service.process_webhook_event,
            db=common["db"],
            topic=topic,
            store_id=common["store"].id,
            payload=common["payload"]
        )
        return Response(status_code=200, content="Webhook received and queued for processing.")
    
    logging.warning(f"Webhook primit pentru un topic neimplementat: {topic}")
    return Response(status_code=404, content="Topic handler not implemented.")