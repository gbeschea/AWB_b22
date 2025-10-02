# /services/courier_service.py

import asyncio
import logging
from collections import defaultdict
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
from datetime import datetime, timedelta, timezone

import models
from services.couriers import get_courier_service

logger = logging.getLogger(__name__)

def get_courier_service_by_name(courier_name: str):
    service = get_courier_service(courier_name)
    if not service:
        raise ValueError(f"Niciun serviciu de curierat găsit pentru numele: {courier_name}")
    return service

async def track_and_update_shipments(db: AsyncSession, full_sync: bool = False, days_ago: int = 14):
    logger.info("--- COURIER SYNC A PORNIT ---")
    final_statuses = ['delivered', 'refused', 'returned', 'canceled', 'livrat', 'refuzat', 'returnat', 'anulat', 'unknown', 'not found', 'error', 'tracking-error']
    since_date = datetime.now(timezone.utc) - timedelta(days=days_ago)

    stmt = select(models.Shipment).where(
        models.Shipment.fulfillment_created_at >= since_date,
        models.Shipment.awb.isnot(None),
        models.Shipment.last_status.is_(None) | ~func.lower(models.Shipment.last_status).in_(final_statuses)
    )
    
    result = await db.execute(stmt)
    shipments_to_track = result.scalars().all()

    if not shipments_to_track:
        logger.info("COURIER SYNC: Nu există livrări de urmărit.")
        return

    logger.info(f"COURIER SYNC: S-au găsit {len(shipments_to_track)} livrări de urmărit.")

    grouped_shipments = defaultdict(list)
    for s in shipments_to_track:
        if s.courier and s.account_key and s.awb:
            grouped_shipments[(s.courier, s.account_key)].append(s)

    updated_count = 0
    
    for (courier_name, account_key), shipments_group in grouped_shipments.items():
        try:
            courier_service_instance = get_courier_service(courier_name)
            if not courier_service_instance:
                logger.warning(f"Nu s-a găsit serviciu pentru curierul '{courier_name}' (cont: {account_key})")
                continue

            logger.info(f"Procesare {len(shipments_group)} AWB-uri pentru {courier_name} (cont: {account_key})...")
            
            for shipment in shipments_group:
                response = await courier_service_instance.track_awb(db, shipment.awb, shipment.account_key)
                
                if response and response.status and response.status != shipment.last_status:
                    logger.info(f"Status nou pentru AWB {shipment.awb} ({courier_name}): '{shipment.last_status}' -> '{response.status}'")
                    shipment.last_status = response.status
                    shipment.last_status_at = response.date
                    updated_count += 1
                await asyncio.sleep(0.3) 

        except Exception as e:
            logger.error(f"Eroare la procesarea grupului pentru {courier_name} / {account_key}: {e}", exc_info=True)

    if updated_count > 0:
        logger.info(f"COURIER SYNC: Se salvează {updated_count} statusuri noi în baza de date...")
        await db.commit()
    else:
        logger.info("COURIER SYNC: Nu a fost găsit niciun status nou de actualizat.")

    logger.info("--- COURIER SYNC FINALIZAT ---")