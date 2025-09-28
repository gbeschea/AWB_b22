from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Union

import httpx
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

# Project-local imports (adjust these paths if your project structure differs)
try:
    from database import get_db
except Exception:  # pragma: no cover
    # fallback name if your project uses a different module name
    from .database import get_db  # type: ignore

try:
    import models
except Exception:  # pragma: no cover
    from . import models  # type: ignore

# Courier service
try:
    from services.couriers.dpd import DPDCourier  # type: ignore
except Exception:  # pragma: no cover
    # If your DPDCourier sits at a different path, adjust here;
    # this helps the file remain portable in various layouts.
    from .services.couriers.dpd import DPDCourier  # type: ignore


router = APIRouter(prefix="/actions", tags=["actions"])


def _as_int(x: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if x is None or x == "":
            return default
        return int(x)
    except Exception:
        return default


def _as_float(x: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


def _as_bool(x: Any, default: bool = False) -> bool:
    if isinstance(x, bool):
        return x
    if x is None:
        return default
    if isinstance(x, (int, float)):
        return bool(x)
    s = str(x).strip().lower()
    if s in {"1", "true", "t", "yes", "y", "da"}:
        return True
    if s in {"0", "false", "f", "no", "n", "nu"}:
        return False
    return default


async def _load_profile(db: AsyncSession, profile_id: Optional[int]) -> Optional[Any]:
    if not profile_id:
        return None
    res = await db.execute(select(models.ShipmentProfile).where(models.ShipmentProfile.id == profile_id))
    return res.scalar_one_or_none()


async def _load_order(db: AsyncSession, order_id: int) -> Any:
    res = await db.execute(select(models.Order).where(models.Order.id == order_id))
    order = res.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail=f"Comanda {order_id} nu a fost găsită.")
    return order


def _order_cod_amount(order: Any) -> float:
    # Robust COD computation: if order.financial_status == 'paid' -> 0 else total_price
    try:
        fs = (getattr(order, "financial_status", "") or "").lower()
        if fs == "paid":
            return 0.0
        # Prefer `total_price` then `total` then 0
        for attr in ("total_price", "total", "grand_total"):
            val = getattr(order, attr, None)
            if val is not None:
                try:
                    return float(val)
                except Exception:
                    pass
        return 0.0
    except Exception:
        return 0.0


def _merge_from_profile(
    raw: Dict[str, Any],
    profile: Optional[Any],
) -> Dict[str, Any]:
    """Mergează câmpurile din profil peste payload-ul primit din UI (fără să suprascriem ce vine explicit din UI)."""
    out = dict(raw)  # shallow copy

    if profile:
        # account key / courier
        if not out.get("courier_account_key"):
            out["courier_account_key"] = getattr(profile, "account_key", None) or getattr(profile, "courier_account_key", None)

        # service id
        if out.get("service_id") in (None, "", 0, "0", "null"):
            svc = getattr(profile, "default_service_id", None) or getattr(profile, "service_id", None)
            if svc not in (None, "", 0, "0", "null"):
                out["service_id"] = int(svc)

        # defaults
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

        # content template (optional)
        if not out.get("content_desc"):
            tmpl = getattr(profile, "content_template", None)
            if tmpl:
                out["content_desc"] = tmpl

        # include shipping in COD (optional)
        if "include_shipping_in_cod" not in out and hasattr(profile, "include_shipping_in_cod"):
            try:
                out["include_shipping_in_cod"] = bool(getattr(profile, "include_shipping_in_cod"))
            except Exception:
                pass

    # Normalize types
    out["service_id"] = _as_int(out.get("service_id"))
    out["parcels_count"] = _as_int(out.get("parcels_count"), 1) or 1
    out["total_weight"] = _as_float(out.get("total_weight"), 1.0) or 1.0
    out["payer"] = (out.get("payer") or "SENDER").upper()

    return out


def _options_from_payload(p: Dict[str, Any], order: Any) -> Dict[str, Any]:
    # Compute COD if missing
    cod = p.get("cod_amount")
    if cod is None or str(cod) == "":
        cod = _order_cod_amount(order)

    opts: Dict[str, Any] = {
        "service_id": _as_int(p.get("service_id")),
        "parcels_count": _as_int(p.get("parcels_count"), 1) or 1,
        "total_weight_kg": _as_float(p.get("total_weight"), 1.0) or 1.0,
        "payer": (p.get("payer") or "SENDER").upper(),
        "cod_amount": _as_float(cod, 0.0) or 0.0,
        "include_shipping_in_cod": _as_bool(p.get("include_shipping_in_cod"), False),
    }
    # Optional content
    if p.get("content_desc"):
        opts["content_desc"] = p["content_desc"]
    return opts


@router.post("/create-awb")
async def create_awb_action(
    request: Request,
    payload: Dict[str, Any],
    db: AsyncSession = Depends(get_db),
):
    """
    Creează AWB pentru o comandă sau pentru mai multe comenzi (bulk).
    Acceptă atât câmpuri venite direct din UI, cât și un `shipment_profile_id`
    care completează valorile lipsă (service_id, courier_account_key, etc.).
    """
    # Accept either {order_id: ...} or {order_ids: [...]}
    order_ids: List[int] = []
    if "order_id" in payload and payload["order_id"] not in (None, "", 0, "0"):
        order_ids = [_as_int(payload["order_id"]) or 0]
    elif "order_ids" in payload and isinstance(payload["order_ids"], list):
        order_ids = [int(x) for x in payload["order_ids"] if str(x).strip()]
    order_ids = [oid for oid in order_ids if oid]

    if not order_ids:
        raise HTTPException(status_code=400, detail="Lipsește `order_id` sau `order_ids`.")

    # Profile (optional)
    shipment_profile_id = _as_int(payload.get("shipment_profile_id"))
    profile = await _load_profile(db, shipment_profile_id)

    # Merge payload with profile defaults
    merged = _merge_from_profile(payload, profile)

    # Now we *require* a courier account key and a service id.
    courier_account_key: Optional[str] = merged.get("courier_account_key")
    if not courier_account_key:
        raise HTTPException(
            status_code=400,
            detail="Selectează un cont de curier sau alege un profil care are cont configurat."
        )

    if merged.get("service_id") in (None, 0):
        raise HTTPException(
            status_code=400,
            detail="DPD: Service ID lipsește. Selectează un profil cu serviciu sau completează manual."
        )

    async with httpx.AsyncClient(timeout=30.0) as client:
        dpd = DPDCourier(client)

        created: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []

        for oid in order_ids:
            try:
                order = await _load_order(db, oid)

                options = _options_from_payload(merged, order)
                # DPDCourier.create_awb(db, order, account_key, options)
                res = await dpd.create_awb(
                    db=db,
                    order=order,
                    account_key=courier_account_key,
                    options=options,
                )

                # Persist shipment row if your project has such a model/table
                try:
                    awb = (res or {}).get("awb") or (res or {}).get("awb_no")
                    if awb:
                        # Optional: save Shipment in DB if you have such a model
                        # (Keep it best-effort; don't fail the request if persisting fails)
                        if hasattr(models, "Shipment"):
                            shipment = models.Shipment(
                                order_id=oid,
                                courier_key=courier_account_key,
                                tracking_number=awb,
                                label_url=(res or {}).get("label_pdf"),
                                courier="DPD",
                            )
                            db.add(shipment)
                            await db.flush()
                except Exception:
                    # Ignore persistence errors here; AWB was created at courier side.
                    pass

                created.append({
                    "order_id": oid,
                    "awb": (res or {}).get("awb") or (res or {}).get("awb_no"),
                    "label_pdf": (res or {}).get("label_pdf"),
                    "raw": res,
                })

            except HTTPException as he:
                errors.append({"order_id": oid, "error": he.detail})
            except Exception as ex:
                errors.append({"order_id": oid, "error": f"O eroare neașteptată a apărut: {ex}"})

        if not created and errors:
            # All failed
            raise HTTPException(status_code=400, detail=errors[0]["error"])

        # Try to commit any DB writes (shipments) best-effort
        try:
            await db.commit()
        except Exception:
            await db.rollback()

        return {
            "success": True,
            "created": created,
            "errors": errors,
            "message": f"AWB creat pentru {len(created)} comenzi. Probleme la {len(errors)} comenzi." if errors else f"AWB creat pentru {len(created)} comenzi.",
        }
