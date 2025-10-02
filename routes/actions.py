
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database import get_db
import models
from services.couriers.dpd import DPDCourier

router = APIRouter(prefix="/actions", tags=["actions"])


# -------------------------- small helpers --------------------------

def _as_int(x: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if x in (None, "", "null"):
            return default
        return int(x)
    except Exception:
        return default


def _as_float(x: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if x in (None, "", "null"):
            return default
        return float(x)
    except Exception:
        return default


def _as_bool(x: Any, default: bool = False) -> bool:
    if isinstance(x, bool):
        return x
    if x is None:
        return default
    s = str(x).strip().lower()
    if s in {"1", "true", "t", "yes", "y", "da"}:
        return True
    if s in {"0", "false", "f", "no", "n", "nu"}:
        return False
    return default


def _order_query_with_items():
    opts = []
    if hasattr(models.Order, "items"):
        opts.append(selectinload(getattr(models.Order, "items")))
    if hasattr(models.Order, "line_items"):
        opts.append(selectinload(getattr(models.Order, "line_items")))
    return opts


async def _load_order(db: AsyncSession, order_id: int) -> Any:
    stmt = select(models.Order).options(*_order_query_with_items()).where(models.Order.id == order_id)
    res = await db.execute(stmt)
    order = res.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail=f"Comanda {order_id} nu a fost găsită.")
    return order


async def _load_profile(db: AsyncSession, profile_id: Optional[int]) -> Optional[Any]:
    if not profile_id:
        return None
    res = await db.execute(select(models.ShipmentProfile).where(models.ShipmentProfile.id == profile_id))
    return res.scalar_one_or_none()


def _order_cod_amount(order: Any) -> float:
    try:
        fs = (getattr(order, "financial_status", "") or "").lower()
        if fs == "paid":
            return 0.0
        for attr in ("total_price", "total", "grand_total", "total_due", "amount_due"):
            val = getattr(order, attr, None)
            if val is not None:
                try:
                    return float(val)
                except Exception:
                    pass
        return 0.0
    except Exception:
        return 0.0


def _merge_from_profile(raw: Dict[str, Any], profile: Optional[Any]) -> Dict[str, Any]:
    out = dict(raw)

    if profile:
        if not out.get("courier_account_key"):
            out["courier_account_key"] = getattr(profile, "account_key", None) or getattr(profile, "courier_account_key", None)

        if out.get("service_id") in (None, "", "0", 0, "null"):
            svc = getattr(profile, "default_service_id", None) or getattr(profile, "service_id", None)
            if svc not in (None, "", "0", 0, "null"):
                out["service_id"] = int(svc)

        if _as_int(out.get("parcels_count")) is None:
            out["parcels_count"] = getattr(profile, "default_parcels", None) or 1
        if _as_float(out.get("total_weight")) is None:
            out["total_weight"] = (
                getattr(profile, "default_weight_kg", None)
                or getattr(profile, "default_weight", None)
                or 1.0
            )
        if not out.get("payer"):
            out["payer"] = getattr(profile, "default_payer", None) or "SENDER"

        # mod ambalare (BOX / PALLET / ENVELOPE / BAG / WRAP) – din profil
        if not out.get("package"):
            out["package"] = getattr(profile, "default_packing", None)

        # template conținut
        if not out.get("content_desc"):
            tmpl = getattr(profile, "content_template", None)
            if tmpl:
                out["content_desc"] = tmpl

        if "include_shipping_in_cod" not in out and hasattr(profile, "include_shipping_in_cod"):
            try:
                out["include_shipping_in_cod"] = bool(getattr(profile, "include_shipping_in_cod"))
            except Exception:
                pass

        if not out.get("third_party_client_id"):
            tpid = getattr(profile, "third_party_client_id", None) or getattr(profile, "thirdPartyClientId", None)
            if tpid:
                out["third_party_client_id"] = str(tpid)

    out["service_id"] = _as_int(out.get("service_id"))
    out["parcels_count"] = _as_int(out.get("parcels_count"), 1) or 1
    out["total_weight"] = _as_float(out.get("total_weight"), 1.0) or 1.0
    out["payer"] = (out.get("payer") or "SENDER").upper()

    if "quantity" in out:
        out["quantity"] = _as_int(out.get("quantity"), None)
    if "sku" in out and out.get("sku"):
        out["sku"] = str(out["sku"]).strip()
    if "package" in out and out.get("package"):
        out["package"] = str(out["package"]).strip()

    return out


def _options_from_payload(p: Dict[str, Any], order: Any) -> Dict[str, Any]:
    cod = p.get("cod_amount")
    if cod is None or str(cod) == "":
        cod = _order_cod_amount(order)

    opts: Dict[str, Any] = {
        "service_id": _as_int(p.get("service_id")),
        "parcels_count": _as_int(p.get("parcels_count"), 1) or 1,
        "total_weight": _as_float(p.get("total_weight"), 1.0) or 1.0,
        "payer": (p.get("payer") or "SENDER").upper(),
        "cod_amount": _as_float(cod, 0.0) or 0.0,
        "include_shipping_in_cod": _as_bool(p.get("include_shipping_in_cod"), False),
    }
    if p.get("content_desc"):
        opts["content_desc"] = p["content_desc"]
    if p.get("third_party_client_id"):
        opts["third_party_client_id"] = str(p["third_party_client_id"]).strip()
    if p.get("quantity") is not None:
        opts["quantity"] = _as_int(p.get("quantity"))
    if p.get("sku"):
        opts["sku"] = str(p["sku"]).strip()
    if p.get("package"):
        opts["package"] = str(p["package"]).strip()
    return opts


def _parse_order_ids(payload: Dict[str, Any]) -> List[int]:
    order_ids: List[int] = []
    if "order_id" in payload and payload["order_id"] not in (None, "", 0, "0"):
        order_ids = [_as_int(payload["order_id"]) or 0]
    elif "order_ids" in payload:
        ids_val = payload["order_ids"]
        if isinstance(ids_val, list):
            order_ids = [int(x) for x in ids_val if str(x).strip()]
        elif isinstance(ids_val, str):
            try:
                arr = json.loads(ids_val)
                if isinstance(arr, list):
                    order_ids = [int(x) for x in arr if str(x).strip()]
            except Exception:
                order_ids = [int(x) for x in ids_val.split(",") if x.strip().isdigit()]
    return [oid for oid in order_ids if oid]


# -------------------------- endpoint --------------------------

@router.post("/create-awb")
async def create_awb_action(request: Request, payload: Dict[str, Any], db: AsyncSession = Depends(get_db)):
    order_ids = _parse_order_ids(payload)
    if not order_ids:
        raise HTTPException(status_code=400, detail="Lipsește `order_id` sau `order_ids`.")

    shipment_profile_id = _as_int(payload.get("shipment_profile_id"))
    profile = await _load_profile(db, shipment_profile_id)
    merged = _merge_from_profile(payload, profile)

    courier_account_key: Optional[str] = (merged.get("courier_account_key") or "").strip() or None
    if not courier_account_key:
        raise HTTPException(status_code=400, detail="Selectează un cont de curier sau alege un profil care are cont configurat.")
    if merged.get("service_id") in (None, 0):
        raise HTTPException(status_code=400, detail="DPD: Service ID lipsește. Selectează un profil cu serviciu sau completează manual.")

    async with httpx.AsyncClient(timeout=45.0, follow_redirects=False) as client:
        dpd = DPDCourier(client)
        created, errors = [], []

        # Evităm problema INT vs VARCHAR folosind încărcarea fiecărui order_id individual
        for oid in order_ids:
            try:
                oid_int = int(oid)
                order = await _load_order(db, oid_int)
                options = _options_from_payload(merged, order)

                res = await dpd.create_awb(db=db, order=order, account_key=courier_account_key, options=options)

                # --- normalizează AWB din răspunsul DPD ---
                awb = None
                if isinstance(res, dict):
                    awb = res.get("id") or res.get("awb")
                    if not awb:
                        try:
                            awb = (res.get("parcels") or [{}])[0].get("id")
                        except Exception:
                            awb = None

                if not awb:
                    raise HTTPException(status_code=400, detail=f"DPD: nu am primit AWB în răspuns: {res}")

                # --- salvează în tabela Shipments (coloana este `awb`) ---
                shipment = models.Shipment(
                    order_id=oid_int,
                    courier="DPD",
                    account_key=courier_account_key,
                    awb=str(awb),
                    courier_specific_data=res,
                )
                db.add(shipment)

                # marchează curierul pe comandă, dacă modelul permite
                try:
                    order.assigned_courier = "DPD"
                except Exception:
                    pass

                created.append({"order_id": oid_int, "awb": str(awb)})
            except HTTPException as he:
                errors.append({"order_id": int(oid), "error": he.detail})
            except Exception as ex:
                errors.append({"order_id": int(oid), "error": f"O eroare neașteptată a apărut: {ex}"})

        try:
            await db.commit()
        except Exception:
            await db.rollback()

        if not created and errors:
            raise HTTPException(status_code=400, detail=errors[0]["error"])

        return {"success": True, "created": created, "errors": errors}
