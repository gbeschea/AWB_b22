from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select

import models
import schemas
from database import get_db
from services.couriers import get_courier_service

router = APIRouter(prefix="/actions", tags=["Actions"])

@router.post("/create-awb", response_model=schemas.ShipmentBase)
async def create_awb_for_order(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
):
    """Creează AWB pentru o comandă.
    Așteaptă payload cu:
        - order_id (int)
        - courier_account_key (str)
        - options (dict)  -> { service_id, parcels_count, total_weight, cod_amount, payer, include_shipping_in_cod, pickup_date, dimensions }
    """
    order_id = payload.get("order_id")
    account_key = payload.get("courier_account_key")
    options = (payload.get("options") or {}).copy()

    if not order_id or not account_key:
        raise HTTPException(status_code=400, detail="order_id și courier_account_key sunt obligatorii.")

    # Încărcăm comanda + relații necesare
    result = await db.execute(
        select(models.Order)
        .options(
            selectinload(models.Order.store),
            selectinload(models.Order.line_items),
            selectinload(models.Order.shipments),
        )
        .where(models.Order.id == order_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Comanda nu a fost găsită.")

    # Nu permite generarea unui nou AWB dacă comanda are deja
    if any(s.awb for s in order.shipments):
        raise HTTPException(status_code=400, detail="Comanda are deja un AWB generat.")

    # Determină tipul de plată -> COD sau nu
    mapped_payment = (order.mapped_payment or "").lower()
    is_cod = ("ramburs" in mapped_payment) or ("cod" in mapped_payment)

    # Completează automat valoarea COD dacă lipsește
    if options.get("cod_amount") is None:
        total = float(getattr(order, "total_price", 0) or 0)
        include_ship = options.get("include_shipping_in_cod", True)
        # Dacă nu avem cost de livrare separat, folosim totalul.
        cod_val = total if include_ship else total
        options["cod_amount"] = float(cod_val) if is_cod else 0.0

    # Normalizează data de ridicare (string)
    from datetime import date, datetime
    pd = options.get("pickup_date")
    if isinstance(pd, (date, datetime)):
        options["pickup_date"] = pd.strftime("%Y-%m-%d")
    elif pd is not None:
        options["pickup_date"] = str(pd)[:10]

    # Fallback pentru colete/greutate
    options.setdefault("parcels_count", 1)
    options.setdefault("total_weight", 1.0)

    # Găsește contul de curier
    acc_res = await db.execute(select(models.CourierAccount).where(models.CourierAccount.account_key == account_key))
    courier_account = acc_res.scalar_one_or_none()
    if not courier_account or not courier_account.is_active:
        raise HTTPException(status_code=400, detail="Contul de curier este invalid sau inactiv.")

    courier_key = (courier_account.courier_type or "").lower()
    courier = get_courier_service(courier_key)
    if not courier:
        raise HTTPException(status_code=400, detail=f"Nu există implementare pentru curierul: {courier_key}")

    try:
        result = await courier.create_awb(db, order, account_key, options=options)

        awb = result.get("awb")
        if not awb:
            raise RuntimeError("Răspuns invalid de la curier: lipsă AWB.")

        # Creăm Shipment în DB
        new_shipment = models.Shipment(
            order_id=order.id,
            awb=awb,
            courier=courier_key,
            account_key=account_key,
        )
        db.add(new_shipment)

        # Marchează comanda
        order.processing_status = "awb_generated"

        await db.commit()
        await db.refresh(new_shipment)

        return new_shipment

    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        import logging
        logging.exception("Eroare neașteptată la crearea AWB-ului")
        raise HTTPException(status_code=500, detail=f"O eroare neașteptată a apărut: {e}")