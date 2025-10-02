# /services/shopify_service.py

import httpx
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

import models

_shopify_clients: Dict[int, httpx.AsyncClient] = {}

def get_shopify_client(store: models.Store) -> httpx.AsyncClient:
    if store.id not in _shopify_clients:
        headers = {
            "X-Shopify-Access-Token": store.access_token,
            "Content-Type": "application/json",
        }
        _shopify_clients[store.id] = httpx.AsyncClient(
            base_url=f"https://{store.domain}/admin/api/{store.api_version or '2025-07'}/",
            headers=headers,
            timeout=30.0
        )
    return _shopify_clients[store.id]

async def get_store_from_db(db: AsyncSession, store_id: int) -> models.Store:
    result = await db.execute(select(models.Store).where(models.Store.id == store_id))
    store = result.scalar_one_or_none()
    if not store:
        raise ValueError(f"Magazinul cu ID-ul {store_id} nu a fost găsit.")
    return store

async def fetch_orders(
    db: AsyncSession, 
    store_id: int, 
    created_at_min: datetime,
    created_at_max: datetime
) -> List[Dict[str, Any]]:
    """
    Preia comenzile de la Shopify, incluzând datele esențiale despre livrări (fulfillments).
    """
    store = await get_store_from_db(db, store_id)
    client = get_shopify_client(store)
    
    all_orders = []
    has_next_page = True
    cursor = None
    
    # =================================================================
    # MODIFICARE: Am actualizat interogarea pentru a include fulfillments și trackingInfo
    # =================================================================
    query = """
    query($first: Int!, $cursor: String, $query: String) {
      orders(first: $first, after: $cursor, query: $query) {
        pageInfo {
          hasNextPage
          endCursor
        }
        edges {
          node {
            id
            name
            createdAt
            displayFinancialStatus
            displayFulfillmentStatus
            tags
            note
            totalPriceSet { shopMoney { amount } }
            customer { firstName lastName }
            paymentGatewayNames
            shippingAddress { 
                firstName, lastName, address1, address2, city, 
                province, zip, country, phone 
            }
            fulfillments {
                id
                createdAt
                displayStatus
                trackingInfo {
                    company
                    number
                    url
                }
            }
          }
        }
      }
    }
    """
    
    start_date_str = created_at_min.isoformat()
    end_date_str = created_at_max.isoformat()
    
    while has_next_page:
        await asyncio.sleep(0.5) 

        variables = {
            "first": 50,
            "cursor": cursor,
            "query": f"created_at:>{start_date_str} created_at:<{end_date_str}"
        }
        
        try:
            response = await client.post("graphql.json", json={"query": query, "variables": variables})
            response.raise_for_status()
            data = response.json()
            
            if "errors" in data:
                if any(e.get('extensions', {}).get('code') == 'THROTTLED' for e in data['errors']):
                    logging.warning("Shopify API Throttling detectat. Se așteaptă 5 secunde...")
                    await asyncio.sleep(5)
                    continue 
                
                logging.error(f"Eroare GraphQL la preluarea comenzilor pentru {store.domain}: {data['errors']}")
                break

            orders_data = data.get("data", {}).get("orders", {})
            page_info = orders_data.get("pageInfo", {})
            
            for edge in orders_data.get("edges", []):
                all_orders.append(edge["node"])
            
            has_next_page = page_info.get("hasNextPage", False)
            cursor = page_info.get("endCursor")

        except httpx.HTTPStatusError as e:
            logging.error(f"Eroare HTTP la preluarea comenzilor pentru {store.domain}: {e.response.text}")
            break
        except Exception as e:
            logging.error(f"Eroare neașteptată la preluarea comenzilor pentru {store.domain}: {e}")
            break
            
    return all_orders

# ... restul fișierului (get_transactions, capture_payment) rămâne neschimbat ...
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
          amountSet { shopMoney { amount } }
          gateway
        }
      }
    }
    """
    variables = {"orderId": f"gid://shopify/Order/{order_id}"}
    try:
        response = await client.post("graphql.json", json={"query": query, "variables": variables})
        response.raise_for_status()
        data = response.json()
        if "errors" in data:
            logging.error(f"Eroare GraphQL la preluarea tranzacțiilor: {data['errors']}")
            return []
        return data.get("data", {}).get("order", {}).get("transactions", [])
    except Exception as e:
        logging.error(f"Eroare la preluarea tranzacțiilor pentru comanda {order_id}: {e}")
        return []

async def capture_payment(db: AsyncSession, store_id: int, shopify_order_id: str):
    store = await get_store_from_db(db, store_id)
    client = get_shopify_client(store)
    order_gid = f"gid://shopify/Order/{shopify_order_id}"
    transactions = await get_transactions(db, store_id, shopify_order_id)
    auth_transaction = None
    for t in transactions:
        if t['kind'].upper() == 'AUTHORIZATION' and t['status'].upper() == 'SUCCESS':
            auth_transaction = t
            break
    if not auth_transaction:
        raise Exception("Nu a fost găsită o tranzacție de autorizare validă pentru capturare.")
    mutation = """
    mutation captureTransaction($transactionId: ID!, $amount: MoneyInput!) {
      transactionCreate(transaction: {
        orderId: "%s",
        kind: CAPTURE,
        gateway: "manual",
        amount: $amount.amount,
        parentId: $transactionId,
        currency: $amount.currencyCode
      }) {
        transaction {
          id
          status
        }
        userErrors {
          field
          message
        }
      }
    }
    """ % order_gid
    variables = {
        "transactionId": auth_transaction['id'],
        "amount": {
            "amount": auth_transaction['amountSet']['shopMoney']['amount'],
            "currencyCode": auth_transaction['amountSet']['shopMoney'].get('currencyCode', 'RON')
        }
    }
    try:
        response = await client.post("graphql.json", json={"query": mutation, "variables": variables})
        response.raise_for_status()
        data = response.json()
        errors = data.get("data", {}).get("transactionCreate", {}).get("userErrors", [])
        if errors:
            error_messages = ", ".join([e['message'] for e in errors])
            raise Exception(f"Eroare la capturarea plății: {error_messages}")
        transaction_data = data.get("data", {}).get("transactionCreate", {}).get("transaction", {})
        if not transaction_data or transaction_data.get('status').upper() != 'SUCCESS':
            raise Exception("Capturarea plății a eșuat în Shopify.")
        logging.info(f"Plata pentru comanda {shopify_order_id} a fost capturată cu succes.")
    except httpx.HTTPStatusError as e:
        logging.error(f"Eroare HTTP la capturarea plății: {e.response.text}")
        raise Exception(f"Eroare server Shopify: {e.response.status_code}")
    except Exception as e:
        logging.error(f"Eroare neașteptată la capturarea plății: {e}")
        raise e