from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select

import models
import schemas
from database import get_db
from services.couriers import get_courier_service
import logging

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/actions",
    tags=["Actions"]
)

@router.post("/create-awb", response_model=schemas.ShipmentBase)
async def create_awb_for_order(
    order_id: int = Body(..., embed=True),
    courier_account_key: str = Body(..., embed=True),
    db: AsyncSession = Depends(get_db)
):
    """
    Generează un AWB pentru o comandă specifică folosind un cont de curier.
    """
    stmt = (
        select(models.Order)
        .options(
            selectinload(models.Order.store), 
            selectinload(models.Order.line_items), 
            selectinload(models.Order.shipments)
        )
        .where(models.Order.id == order_id)
    )
    order = (await db.execute(stmt)).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Comanda nu a fost găsită.")

    if any(shipment.awb for shipment in order.shipments):
        raise HTTPException(status_code=400, detail="Comanda are deja un AWB generat.")

    try:
        courier_service = get_courier_service(courier_account_key)
        result = await courier_service.create_awb(db, order, courier_account_key)

        # === MODIFICAREA CHEIE ESTE AICI ===
        # Salvăm răspunsul brut în coloana corectă: 'courier_specific_data'
        new_shipment = models.Shipment(
            order_id=order.id,
            awb=result.get("awb"),
            courier=courier_account_key,
            account_key=courier_account_key,
            paper_size=order.store.paper_size if order.store else 'A6',
            courier_specific_data=result.get("raw_response")
        )
        # ===================================
        
        db.add(new_shipment)
        order.processing_status = "awb_generated"
        
        await db.commit()
        await db.refresh(new_shipment)
        
        return new_shipment

    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Eroare neașteptată la crearea AWB-ului")
        raise HTTPException(status_code=500, detail=f"O eroare neașteptată a apărut: {e}")