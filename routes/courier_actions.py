"""Tenancy-scoped courier OPERATIONS for the embedded app — the "mutations":
create AWB (single + bulk-select), void/cancel, print label. Courier-agnostic via the
adapter registry (works for any courier whose adapter implements the operation).

Every route is authenticated + scoped by `require_shop`, so a shop can only act on its
own orders and courier accounts. Standard adapter contract:
  create_awb(db, order, account_key, *, options) -> {"awb", "raw", ...}
  get_label(awb, credentials, paper_size) -> bytes (PDF)
  void_awb(db, awb, account_key) -> VoidResponse
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models
from database import get_db
from services.couriers import get_courier_service
from services.shopify_auth import require_shop

router = APIRouter(prefix="/api", tags=["Courier Actions"])
logger = logging.getLogger(__name__)


async def _get_account(db: AsyncSession, store: models.Store, account_key: str) -> models.CourierAccount:
    acct = (await db.execute(
        select(models.CourierAccount).where(
            models.CourierAccount.account_key == account_key,
            (models.CourierAccount.store_id == store.id) | (models.CourierAccount.store_id.is_(None)),
        )
    )).scalar_one_or_none()
    if not acct:
        raise HTTPException(404, f"Cont curier '{account_key}' negăsit.")
    if not acct.credentials:
        raise HTTPException(400, f"Contul '{account_key}' nu are credențiale configurate.")
    return acct


async def _load_order(db: AsyncSession, store: models.Store, order_id: int) -> models.Order:
    o = (await db.execute(
        select(models.Order)
        .options(selectinload(models.Order.line_items), selectinload(models.Order.shipments))
        .where(models.Order.id == order_id, models.Order.store_id == store.id)
    )).scalar_one_or_none()
    if not o:
        raise HTTPException(404, f"Comanda {order_id} negăsită.")
    return o


def _cod_for(order: models.Order) -> float:
    """COD = order total unless already paid online."""
    if (order.financial_status or "").lower() == "paid":
        return 0.0
    try:
        return float(order.total_price or 0.0)
    except (TypeError, ValueError):
        return 0.0


async def _create_one(
    db: AsyncSession, store: models.Store, order: models.Order,
    account_key: str, options: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Create an AWB for one order via the right adapter, persist a Shipment. Caller commits."""
    acct = await _get_account(db, store, account_key)
    svc = get_courier_service(acct.courier_type or account_key)
    if not svc:
        raise HTTPException(400, f"Curier nesuportat: {acct.courier_type or account_key}")

    opts = dict(options or {})
    opts.setdefault("cod_amount", _cod_for(order))
    opts.setdefault("parcels_count", 1)
    opts.setdefault("total_weight", 1.0)

    try:
        res = await svc.create_awb(db=db, order=order, account_key=account_key, options=opts)
    except NotImplementedError:
        raise HTTPException(400, f"Crearea AWB nu e implementată pentru {acct.courier_type}.")
    except Exception as e:
        raise HTTPException(400, f"{account_key}: {e}")

    awb = res.get("awb") if isinstance(res, dict) else None
    if not awb:
        raise HTTPException(400, f"{account_key}: răspuns fără AWB ({res}).")

    shipment = models.Shipment(
        order_id=order.id,
        courier=(acct.courier_type or account_key).upper(),
        account_key=account_key,
        awb=str(awb),
        courier_specific_data=(res.get("raw") if isinstance(res, dict) else None),
        paper_size=(store.paper_size or "A6"),
    )
    db.add(shipment)
    if not order.assigned_courier:
        order.assigned_courier = acct.courier_type or account_key
    order.processing_status = "Procesată"
    return {"order_id": order.id, "order_name": order.name, "awb": str(awb),
            "courier": (acct.courier_type or account_key)}


async def _request_pickup(db: AsyncSession, store: models.Store, account_key: str,
                          awbs, options: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Ask the courier to collect the AWB(s). No-op for couriers that auto-schedule."""
    try:
        acct = await _get_account(db, store, account_key)
        svc = get_courier_service(acct.courier_type or account_key)
        if not svc:
            return {"supported": False}
        return await svc.request_pickup(db, awbs, account_key, options=options or {})
    except NotImplementedError:
        return {"supported": False}
    except Exception as e:
        return {"supported": True, "requested": False, "message": str(e)}


@router.post("/awb/create")
async def create_awb(
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Create one AWB (+ request courier pickup where the courier needs it).
    Body: {order_id, account_key, options?, request_pickup? (default true)}."""
    order_id = payload.get("order_id")
    account_key = (payload.get("account_key") or "").strip()
    if not order_id or not account_key:
        raise HTTPException(400, "order_id și account_key sunt necesare.")
    order = await _load_order(db, store, int(order_id))
    try:
        r = await _create_one(db, store, order, account_key, payload.get("options"))
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    pickup = None
    if payload.get("request_pickup", True):
        pickup = await _request_pickup(db, store, account_key, r["awb"], payload.get("options"))
    return {"success": True, **r, "pickup": pickup}


@router.post("/awb/bulk")
async def bulk_create(
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Create AWBs for many selected orders. Body: {order_ids:[...], account_key, options?}.
    Commits per order so one failure doesn't drop the rest; returns per-order results."""
    order_ids = payload.get("order_ids") or []
    account_key = (payload.get("account_key") or "").strip()
    if not order_ids or not account_key:
        raise HTTPException(400, "order_ids și account_key sunt necesare.")
    options = payload.get("options")

    created: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for oid in order_ids:
        try:
            order = await _load_order(db, store, int(oid))
            r = await _create_one(db, store, order, account_key, options)
            await db.commit()
            created.append(r)
        except HTTPException as he:
            await db.rollback()
            errors.append({"order_id": oid, "error": he.detail})
        except Exception as e:
            await db.rollback()
            errors.append({"order_id": oid, "error": str(e)})

    # One pickup request covers all AWBs on the account (FAN/DPD docs), not one per AWB.
    pickup = None
    if created and payload.get("request_pickup", True):
        pickup = await _request_pickup(db, store, account_key, [c["awb"] for c in created], options)
    return {"success": bool(created), "created": created, "errors": errors,
            "total": len(order_ids), "pickup": pickup}


async def _load_shipment(db: AsyncSession, store: models.Store, shipment_id: int) -> models.Shipment:
    ship = (await db.execute(
        select(models.Shipment)
        .join(models.Order, models.Order.id == models.Shipment.order_id)
        .where(models.Shipment.id == shipment_id, models.Order.store_id == store.id)
    )).scalar_one_or_none()
    if not ship:
        raise HTTPException(404, "Expediere negăsită.")
    return ship


@router.post("/awb/void")
async def void_awb(
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Cancel an AWB. Body: {shipment_id}. Removes the shipment so the order is unprocessed again."""
    shipment_id = payload.get("shipment_id")
    if not shipment_id:
        raise HTTPException(400, "shipment_id este necesar.")
    ship = await _load_shipment(db, store, int(shipment_id))
    awb = ship.awb
    svc = get_courier_service(ship.courier or ship.account_key or "")
    if not svc:
        raise HTTPException(400, "Curier nesuportat pentru anulare.")
    try:
        v = await svc.void_awb(db, ship.awb, ship.account_key)
    except NotImplementedError:
        raise HTTPException(400, f"Anularea nu e suportată pentru {ship.courier}.")
    if not v.success:
        raise HTTPException(400, f"Anulare eșuată: {v.message}")
    await db.delete(ship)
    await db.commit()
    return {"success": True, "voided_awb": awb}


@router.get("/awb/label")
async def get_label(
    shipment_id: int,
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Return the courier label PDF for a shipment (inline)."""
    ship = await _load_shipment(db, store, shipment_id)
    acct = await _get_account(db, store, ship.account_key)
    svc = get_courier_service(ship.courier or ship.account_key or "")
    if not svc:
        raise HTTPException(400, "Curier nesuportat pentru etichetă.")
    try:
        pdf = await svc.get_label(ship.awb, acct.credentials, ship.paper_size or "A6")
    except NotImplementedError:
        raise HTTPException(400, f"Eticheta nu e suportată pentru {ship.courier}.")
    except Exception as e:
        raise HTTPException(400, f"Eroare etichetă: {e}")
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{ship.awb}.pdf"'},
    )
