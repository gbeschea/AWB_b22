"""Atomic per-order operations for the embedded app — reusable from the Orders page AND the
CS backlog: put the order's fulfillment on HOLD / release it, CANCEL the order, edit its NOTE,
add/remove TAGS, and EMAIL the customer through Shopify (the order-page invoice channel).

Every route is scoped by `require_shop`; a shop only acts on its own (or org-linked) orders.
The heavy lifting lives in services.shopify_service; here we just orchestrate + mirror the
result onto our local Order row so the UI reflects it immediately.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models
from database import get_db
from services import org_service, shopify_service
from services.shopify_auth import require_shop

router = APIRouter(prefix="/api", tags=["Order Actions"])
logger = logging.getLogger(__name__)


async def load_order(db: AsyncSession, store: models.Store, order_id: int) -> models.Order:
    """Org-scoped order load with the relations the actions/panel need."""
    allowed = await org_service.org_store_ids(db, store)
    o = (await db.execute(
        select(models.Order)
        .options(selectinload(models.Order.line_items), selectinload(models.Order.shipments),
                 selectinload(models.Order.store))
        .where(models.Order.id == order_id, models.Order.store_id.in_(allowed))
    )).scalar_one_or_none()
    if not o:
        raise HTTPException(404, f"Order {order_id} not found.")
    if not o.shopify_order_id:
        raise HTTPException(400, "This order has no shopify_order_id (not synced from Shopify).")
    return o


async def _xconnector_account(db: AsyncSession, store: models.Store) -> Optional[str]:
    """The store's active xConnector account_key (raw lookup), or None if it has none."""
    if store is None:
        return None
    row = (await db.execute(
        select(models.CourierAccount.account_key).where(
            models.CourierAccount.store_id == store.id,
            models.CourierAccount.courier_type == "xconnector",
            models.CourierAccount.is_active.is_(True),
        ).limit(1)
    )).first()
    return row[0] if row else None


async def _invoice_via_xconnector(db: AsyncSession, store: models.Store) -> Optional[str]:
    """Which connector issues this store's invoices — the store's CHOICE (invoice_settings.invoice_via):
       'xconnector' → xConnector's SmartBill bridge (returns the xconnector account_key to use);
       'smartbill'  → the server-level smartbill_service (returns None).
       Unset → default to xConnector when the store has one (owner rule: prefer xConnector if present).
    The AWB connector (store.auto_awb_account_key) and the invoice connector are chosen INDEPENDENTLY."""
    choice = ((getattr(store, "invoice_settings", None) or {}).get("invoice_via") or "").strip().lower()
    if choice == "smartbill":
        return None
    return await _xconnector_account(db, store)


def _tags_to_list(tags: Optional[str]) -> List[str]:
    return [t.strip() for t in (tags or "").split(",") if t.strip()]


def render_email(text: str, order: models.Order) -> str:
    """Fill the merchant's template placeholders from the order."""
    st = order.store
    addr = ", ".join(x for x in [order.shipping_address1, order.shipping_address2,
                                 order.shipping_city, order.shipping_zip,
                                 order.shipping_province, order.shipping_country] if x)
    awb = order.shipments[-1].awb if order.shipments else ""
    repl = {
        "{customer}": order.shipping_name or order.customer or "",
        "{order}": order.name or "",
        "{address}": addr,
        "{store}": (st.name if st else "") or "",
        "{phone}": order.shipping_phone or "",
        "{tracking}": awb or "",
    }
    out = text or ""
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


@router.get("/smartbill/status")
async def smartbill_status(store: models.Store = Depends(require_shop)):
    """Is invoicing available? In test mode the sandbox provider stands in for SmartBill, so the
    invoice actions light up without any credentials."""
    from services import smartbill_service
    if getattr(store, "test_mode", False):
        return {"configured": True, "sandbox": True}
    return {"configured": smartbill_service.is_configured()}


@router.get("/smartbill/series")
async def smartbill_series(store: models.Store = Depends(require_shop)):
    """List the SmartBill account's invoice series (for the settings dropdown). Verifies auth too."""
    from services import smartbill_service
    if not smartbill_service.is_configured():
        return {"configured": False, "series": []}
    try:
        rows = await smartbill_service.list_series("f")
        return {"configured": True, "series": [{"name": r.get("name"), "next": r.get("nextNumber")} for r in rows]}
    except Exception as e:
        return {"configured": True, "series": [], "error": str(e)[:200]}


@router.get("/invoice-settings")
async def get_invoice_settings(store: models.Store = Depends(require_shop)):
    """This store's SmartBill invoice settings (merged over defaults)."""
    from services import smartbill_service
    saved = getattr(store, "invoice_settings", None) or {}
    return {**smartbill_service.DEFAULT_SETTINGS, **saved}


@router.put("/invoice-settings")
async def put_invoice_settings(
    payload: Dict[str, Any] = Body(default={}),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Save this store's SmartBill invoice settings (whitelisted keys)."""
    from services import smartbill_service
    # invoice_via = alegerea conectorului de FACTURĂ ("xconnector" | "smartbill"), independent de AWB.
    allowed = set(smartbill_service.DEFAULT_SETTINGS.keys()) | {"invoice_via"}
    clean = {k: v for k, v in (payload or {}).items() if k in allowed}
    store.invoice_settings = {**(getattr(store, "invoice_settings", None) or {}), **clean}
    await db.commit()
    return {"success": True, "invoice_settings": store.invoice_settings}


@router.post("/orders/{order_id}/invoice")
async def create_invoice(
    order_id: int,
    payload: Dict[str, Any] = Body(default={}),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Create a SmartBill fiscal invoice for the order (itemized from Shopify line prices). Body:
    {draft?: bool, send_email?: bool, series?: str}. Persists series/number/url on the order."""
    from services import smartbill_service
    o = await load_order(db, store, order_id)
    if o.invoice_number:
        raise HTTPException(400, f"This order already has invoice {o.invoice_series or ''}{o.invoice_number}.")
    # Test mode issues a sandbox invoice so the whole invoice flow is testable with no SmartBill
    # account. Clearly marked (series "DEMO") and never sent anywhere.
    if getattr(store, "test_mode", False):
        from services import test_mode as tm
        res = tm.fake_invoice(o)
        o.invoice_series, o.invoice_number = res["series"], res["number"]
        o.invoice_url, o.invoiced_at = res["url"], datetime.now(timezone.utc)
        await db.commit()
        return {"success": True, **res}
    # xConnector stores invoice through their OWN SmartBill connector inside xConnector (per-brand CIF),
    # not the server-level smartbill_service. Route there when the store runs on xConnector.
    xc_key = await _invoice_via_xconnector(db, o.store or store)
    if xc_key:
        from services.couriers import get_courier_service
        svc = get_courier_service("xconnector")
        res = await svc.create_invoice(db, o, xc_key, lang=(payload.get("lang") or None))
        if not res.get("success"):
            raise HTTPException(400, res.get("message") or "xConnector: crearea facturii a eșuat")
        o.invoice_series = res.get("serie")
        o.invoice_number = str(res.get("numar") or "") or None
        o.invoice_url = res.get("url")
        o.invoiced_at = datetime.now(timezone.utc)
        await db.commit()
        return {"success": True, "via": "xconnector", **res}
    if not smartbill_service.is_configured():
        raise HTTPException(400, "SmartBill isn't configured on the server (SMARTBILL_EMAIL/TOKEN/CIF).")
    try:
        order_data = await shopify_service.get_order_for_invoice(o.store or store, o.shopify_order_id)
    except Exception as e:
        raise HTTPException(400, f"Couldn't read the order from Shopify: {e}")
    if not order_data:
        raise HTTPException(404, "That order wasn't found in Shopify.")
    inv_settings = getattr(o.store or store, "invoice_settings", None) or {}
    try:
        res = await smartbill_service.create_invoice(
            order_data, is_draft=bool(payload.get("draft")),
            series=(payload.get("series") or None),
            send_email=bool(payload.get("send_email") or inv_settings.get("auto_send_email")),
            settings=inv_settings)
    except smartbill_service.SmartBillNotConfigured as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, str(e))
    o.invoice_series = res.get("series")
    o.invoice_number = str(res.get("number") or "") or None
    o.invoice_url = res.get("url")
    o.invoiced_at = datetime.now(timezone.utc)
    await db.commit()
    return {"success": True, **res}


@router.post("/orders/{order_id}/invoice/cancel")
async def cancel_order_invoice(
    order_id: int,
    payload: Dict[str, Any] = Body(default={}),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Kill the order's fiscal invoice. `mode`:
      cancel  — mark it cancelled, the number stays used (default, the usual case);
      storno  — issue a reversing credit note (use once the customer already has the original);
      delete  — remove it entirely; SmartBill only permits this for the LAST invoice in the series.
    Clears the invoice off the order so a corrected one can be issued (except for `storno`, where
    both documents legitimately exist and the original stays recorded)."""
    from services import smartbill_service
    o = await load_order(db, store, order_id)
    if not o.invoice_number:
        raise HTTPException(400, "This order has no invoice.")
    mode = str(payload.get("mode") or "cancel").lower()
    if getattr(store, "test_mode", False):
        series, number = (o.invoice_series or ""), str(o.invoice_number)
        if mode in ("cancel", "delete"):
            o.invoice_series = o.invoice_number = o.invoice_url = None
            o.invoiced_at = None
            await db.commit()
        return {"success": True, "mode": mode, "series": series, "number": number, "sandbox": True}
    # xConnector stores: cancel/storno through xConnector's SmartBill connector (same bridge as create).
    xc_key = await _invoice_via_xconnector(db, o.store or store)
    if xc_key:
        from services.couriers import get_courier_service
        svc = get_courier_service("xconnector")
        series, number = (o.invoice_series or ""), str(o.invoice_number)
        res = await (svc.storno_invoice(db, o, xc_key) if mode == "storno"
                     else svc.cancel_invoice(db, o, xc_key))
        if not res.get("success"):
            raise HTTPException(400, res.get("message") or "xConnector: anularea facturii a eșuat")
        if mode in ("cancel", "delete"):
            o.invoice_series = o.invoice_number = o.invoice_url = None
            o.invoiced_at = None
            await db.commit()
        return {"success": True, "via": "xconnector", "mode": mode, "series": series, "number": number, **(res or {})}
    if not smartbill_service.is_configured():
        raise HTTPException(400, "SmartBill isn't configured on the server (SMARTBILL_EMAIL/TOKEN/CIF).")
    series, number = (o.invoice_series or ""), str(o.invoice_number)
    try:
        if mode == "storno":
            res = await smartbill_service.reverse_invoice(series, number)
        elif mode == "delete":
            res = await smartbill_service.delete_invoice(series, number)
        elif mode == "cancel":
            res = await smartbill_service.cancel_invoice(series, number)
        else:
            raise HTTPException(400, f"Unknown mode '{mode}' (use cancel, storno or delete).")
    except smartbill_service.SmartBillNotConfigured as e:
        raise HTTPException(400, str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))

    # `storno` leaves the original invoice standing (reversed by a credit note), so the order keeps
    # its number. `cancel`/`delete` free the order to be re-invoiced.
    if mode in ("cancel", "delete"):
        o.invoice_series = None
        o.invoice_number = None
        o.invoice_url = None
        o.invoiced_at = None
        await db.commit()
    return {"success": True, "mode": mode, "series": series, "number": number, **(res or {})}


@router.get("/orders/{order_id}/invoice/download")
async def download_invoice(
    order_id: int,
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Download the order's fiscal-invoice PDF. xConnector stores pull the INVOICE document straight
    from xConnector; otherwise redirect to the stored SmartBill URL."""
    o = await load_order(db, store, order_id)
    xc_key = await _invoice_via_xconnector(db, o.store or store)
    if xc_key:
        from services.couriers import get_courier_service
        svc = get_courier_service("xconnector")
        try:
            pdf = await svc.get_invoice(db, o, xc_key)
        except Exception as e:
            raise HTTPException(400, f"xConnector: {e}")
        return Response(content=pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="invoice-{o.name}.pdf"'})
    if o.invoice_url:
        return RedirectResponse(o.invoice_url)
    raise HTTPException(404, "This order has no invoice to download.")


@router.get("/orders/{order_id}/timeline")
async def order_timeline(
    order_id: int,
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """The order's Shopify timeline (activity feed) — address edits, holds, emails, fulfillments,
    everything the merchant sees on the order page. Newest first."""
    o = await load_order(db, store, order_id)
    events = await shopify_service.get_order_timeline(o.store or store, o.shopify_order_id)
    return {"events": events}


@router.post("/orders/{order_id}/hold")
async def hold_order(
    order_id: int,
    payload: Dict[str, Any] = Body(default={}),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Put the order's still-fulfillable fulfillment orders ON HOLD in Shopify."""
    o = await load_order(db, store, order_id)
    if o.shipments:
        raise HTTPException(400, "This order already has an AWB — it can no longer be put on hold.")
    held = await shopify_service.hold_fulfillment_orders(
        o.store or store, o.shopify_order_id,
        reason=(payload.get("reason") or "manual"), notes=payload.get("notes"),
    )
    o.is_on_hold_shopify = True
    await db.commit()
    return {"success": True, "held": held, "on_hold": True}


@router.post("/orders/{order_id}/release")
async def release_order(
    order_id: int,
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Release every hold on the order so it can ship again."""
    o = await load_order(db, store, order_id)
    released = await shopify_service.release_fulfillment_order_holds(o.store or store, o.shopify_order_id)
    o.is_on_hold_shopify = False
    await db.commit()
    return {"success": True, "released": released, "on_hold": False}


@router.post("/orders/{order_id}/cancel")
async def cancel_order(
    order_id: int,
    payload: Dict[str, Any] = Body(default={}),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Cancel the order in Shopify. Defaults: no refund (a COD parcel was never paid), restock
    on, don't notify. The caller can override any of these."""
    o = await load_order(db, store, order_id)
    try:
        await shopify_service.cancel_order(
            o.store or store, o.shopify_order_id,
            reason=(payload.get("reason") or "OTHER"),
            refund=bool(payload.get("refund", False)),
            restock=bool(payload.get("restock", True)),
            notify_customer=bool(payload.get("notify_customer", False)),
            staff_note=payload.get("staff_note"),
        )
    except Exception as e:
        raise HTTPException(400, f"Cancellation failed: {e}")
    o.cancelled_at = datetime.now(timezone.utc)
    o.processing_status = "Anulată"
    await db.commit()
    return {"success": True, "cancelled": True}


@router.post("/orders/{order_id}/note")
async def set_note(
    order_id: int,
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Set the Shopify order note (durable, merchant-visible everywhere)."""
    note = payload.get("note")
    if note is None:
        raise HTTPException(400, "note is required.")
    o = await load_order(db, store, order_id)
    try:
        await shopify_service.update_order_note(o.store or store, o.shopify_order_id, str(note))
    except Exception as e:
        raise HTTPException(400, f"Couldn't save the note: {e}")
    o.note = str(note)
    await db.commit()
    return {"success": True, "note": o.note}


@router.post("/orders/{order_id}/tags")
async def edit_tags(
    order_id: int,
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Add and/or remove Shopify order tags. Body: {add:[...], remove:[...]}."""
    add = [t for t in (payload.get("add") or []) if str(t).strip()]
    remove = [t for t in (payload.get("remove") or []) if str(t).strip()]
    if not add and not remove:
        raise HTTPException(400, "add or remove is required.")
    o = await load_order(db, store, order_id)
    owner = o.store or store
    try:
        if add:
            await shopify_service.add_order_tags(owner, o.shopify_order_id, add)
        if remove:
            await shopify_service.remove_order_tags(owner, o.shopify_order_id, remove)
    except Exception as e:
        raise HTTPException(400, f"Couldn't update the tags: {e}")
    cur = _tags_to_list(o.tags)
    low_remove = {t.lower() for t in remove}
    cur = [t for t in cur if t.lower() not in low_remove]
    for t in add:
        if t.strip().lower() not in {c.lower() for c in cur}:
            cur.append(t.strip())
    o.tags = ", ".join(cur)
    await db.commit()
    return {"success": True, "tags": o.tags}


async def _resolve_email(db: AsyncSession, store: models.Store, o: models.Order,
                         payload: Dict[str, Any]) -> tuple[str, str]:
    """(subject, body) from either an explicit subject/body or a saved template id."""
    tid = payload.get("template_id")
    if tid:
        allowed = await org_service.org_store_ids(db, store)
        tpl = (await db.execute(
            select(models.CSEmailTemplate).where(
                models.CSEmailTemplate.id == int(tid),
                (models.CSEmailTemplate.store_id.in_(allowed)) | (models.CSEmailTemplate.store_id.is_(None)),
            )
        )).scalar_one_or_none()
        if not tpl:
            raise HTTPException(404, "Email template not found.")
        return render_email(tpl.subject, o), render_email(tpl.body, o)
    subject = payload.get("subject")
    body = payload.get("body")
    if not subject or not body:
        raise HTTPException(400, "subject + body, or template_id, is required.")
    return render_email(str(subject), o), render_email(str(body), o)


@router.post("/orders/{order_id}/address")
async def edit_address(
    order_id: int,
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Visually edit the shipping address, write it to Shopify, mirror it locally, and RE-VALIDATE.
    Works for every order (Releaseit/COD-form addresses are editable — only their products aren't).
    Body: any of {name, address1, address2, city, zip, province, country, phone}."""
    o = await load_order(db, store, order_id)
    fields = {k: payload.get(k) for k in
              ("name", "address1", "address2", "city", "zip", "province", "country", "phone")
              if k in payload}
    if not fields:
        raise HTTPException(400, "No address fields to update.")
    try:
        await shopify_service.update_order_shipping_address(o.store or store, o.shopify_order_id, fields)
    except Exception as e:
        raise HTTPException(400, f"Couldn't save the address to Shopify: {e}")
    _apply_address_locally(o, fields)
    from services import address_service
    try:
        vr = await address_service.validate_address_for_order(db, o)
    except Exception as e:
        await db.commit()
        logger.info("re-validate after address edit failed for %s: %s", o.name, e)
        return {"success": True, "revalidated": False, "address_status": o.address_status}
    await db.commit()
    return {
        "success": True, "revalidated": True,
        "is_valid": bool(getattr(vr, "is_valid", False)),
        "address_status": o.address_status, "address_score": o.address_score,
        "errors": o.address_validation_errors,
    }


def _apply_address_locally(o: models.Order, fields: Dict[str, Any]) -> None:
    m = {"name": "shipping_name", "address1": "shipping_address1", "address2": "shipping_address2",
         "city": "shipping_city", "zip": "shipping_zip", "province": "shipping_province",
         "country": "shipping_country", "phone": "shipping_phone"}
    for k, col in m.items():
        if k in fields and fields[k] is not None:
            setattr(o, col, fields[k])


async def _ship_now_core(db: AsyncSession, store: models.Store, o: models.Order,
                         payload: Dict[str, Any]) -> Dict[str, Any]:
    """Mark the address correct (OVERRIDE the validator — CS confirmed it), release any hold,
    create the AWB. On ANY failure push the order to the CS backlog instead of dropping it.
    Commits internally (per-order isolation for the bulk path). Never raises for a shippable
    order — returns a result dict. Raises only for already-shipped/cancelled orders."""
    if o.shipments:
        raise HTTPException(400, "This order already has an AWB.")
    if o.cancelled_at:
        raise HTTPException(400, "This order is cancelled.")

    if payload.get("mark_correct", True):
        o.address_status = "valid"
        o.address_score = 100

    if o.is_on_hold_shopify:
        try:
            await shopify_service.release_fulfillment_order_holds(o.store or store, o.shopify_order_id)
        except Exception as e:
            logger.info("ship-now release-hold failed for %s: %s", o.name, e)
        o.is_on_hold_shopify = False

    from routes.courier_actions import _profile_base, _create_one, _request_pickup, _resolve_target
    from services import shipment_rules
    from sqlalchemy import select as _select

    async def _resolve():
        if payload.get("profile_id") or payload.get("account_key"):
            return await _resolve_target(db, store, payload)
        rules = (await db.execute(
            _select(models.ShipmentRule).where(
                models.ShipmentRule.enabled.is_(True),
                (models.ShipmentRule.store_id == o.store_id) | (models.ShipmentRule.store_id.is_(None)),
            ).order_by(models.ShipmentRule.priority.asc(), models.ShipmentRule.id.asc())
        )).scalars().all()
        owner = o.store or store
        pid = (getattr(o, "assigned_profile_id", None)
               or shipment_rules.pick_profile_id(o, rules)
               or getattr(owner, "auto_awb_profile_id", None))
        if pid:
            return await _profile_base(db, store, pid)
        if getattr(owner, "auto_awb_account_key", None):
            return owner.auto_awb_account_key, {}
        raise HTTPException(400, "No courier to route to — pick a profile or a courier account.")

    oid = o.id
    try:
        account_key, options = await _resolve()
        r = await _create_one(db, store, o, account_key, options)
        await db.commit()
    except Exception as e:
        await db.rollback()
        detail = e.detail if isinstance(e, HTTPException) else str(e)
        try:
            o2 = await load_order(db, store, oid)
            from routes.cs_queue import enqueue_order
            await enqueue_order(db, store, o2, reason="manual",
                                detail=f"AWB failed: {detail}", created_by="auto")
            await db.commit()
        except Exception as ee:
            await db.rollback()
            logger.info("ship-now → CS enqueue failed for order %s: %s", oid, ee)
        return {"order_id": oid, "success": False, "sent_to_cs": True, "error": detail}

    pickup = None
    if payload.get("request_pickup", True):
        pickup = await _request_pickup(db, store, account_key, r["awb"], options)
    return {"success": True, **r, "pickup": pickup}


@router.post("/orders/{order_id}/ship-now")
async def ship_now(
    order_id: int,
    payload: Dict[str, Any] = Body(default={}),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Decision from the address view: mark the address correct (OVERRIDE the validator) and
    create the AWB now; falls to the CS backlog if it can't ship. Body: {profile_id?|account_key?,
    mark_correct?=true}."""
    o = await load_order(db, store, order_id)
    return await _ship_now_core(db, store, o, payload)


@router.post("/orders/ship-now-bulk")
async def ship_now_bulk(
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Bulk 'these look fine — ship them all'. Select orders in the address view (validator can be
    wrong) and create AWBs in one go: per order it marks the address correct, makes the AWB, and
    anything that can't ship drops into the CS backlog. Body: {order_ids:[...], profile_id?|account_key?}.
    Per-order isolation so one failure never blocks the rest."""
    ids = payload.get("order_ids") or []
    if not ids:
        raise HTTPException(400, "order_ids is required.")
    shipped: List[Dict[str, Any]] = []
    to_cs: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for oid in ids:
        try:
            o = await load_order(db, store, int(oid))
            r = await _ship_now_core(db, store, o, payload)
            if r.get("success"):
                shipped.append(r)
            else:
                to_cs.append(r)
        except HTTPException as he:
            skipped.append({"order_id": oid, "error": he.detail})
        except Exception as e:
            await db.rollback()
            skipped.append({"order_id": oid, "error": str(e)})
    return {"success": bool(shipped), "shipped": shipped, "sent_to_cs": to_cs, "skipped": skipped,
            "counts": {"shipped": len(shipped), "sent_to_cs": len(to_cs), "skipped": len(skipped)}}


@router.get("/orders/{order_id}/editability")
async def order_editability(
    order_id: int,
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Can this order's PRODUCTS be edited (our COD-flex / native) or does it need a SWAP
    (Releaseit / COD-form)? Returns the hint + current line items (with variant ids)."""
    o = await load_order(db, store, order_id)
    from services import order_edit
    try:
        return await order_edit.get_editability(o.store or store, o)
    except Exception as e:
        raise HTTPException(400, f"Couldn't check whether this order is editable: {e}")


@router.post("/orders/{order_id}/edit/begin")
async def order_edit_begin(
    order_id: int,
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Start an edit session — returns the calculated line items + shipping lines to edit."""
    o = await load_order(db, store, order_id)
    from services import order_edit
    try:
        return {"success": True, **await order_edit.edit_begin(o.store or store, o.shopify_order_id)}
    except Exception as e:
        # Shopify's bare "The order cannot be edited." says nothing useful, and the commonest cause
        # by far is that a DIFFERENT app created the order. Look that up so the merchant learns why.
        detail = str(e)
        if "cannot be edited" in detail.lower():
            try:
                hint = await order_edit.get_editability(o.store or store, o)
                if hint.get("reason"):
                    detail = hint["reason"]
            except Exception:
                pass
        raise HTTPException(400, detail)


@router.post("/orders/{order_id}/edit/commit")
async def order_edit_commit(
    order_id: int,
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Apply the staged changes + commit. Body: {calc_order_id, set_qty, add_variants,
    line_discounts, remove_shipping_ids, notify, staff_note}."""
    calc_order_id = payload.get("calc_order_id")
    if not calc_order_id:
        raise HTTPException(400, "calc_order_id is required.")
    o = await load_order(db, store, order_id)
    from services import order_edit
    try:
        r = await order_edit.edit_commit(o.store or store, calc_order_id, payload)
    except Exception as e:
        raise HTTPException(400, f"The edit failed: {e}")
    return {"success": True, **r}


@router.post("/orders/{order_id}/swap")
async def order_swap(
    order_id: int,
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Place a replacement order tagged `swap` (for non-editable Releaseit/COD-form orders).
    Body: {line_items:[{variant_id, quantity}], note?, cancel_original?, notify?, tags?}."""
    line_items = payload.get("line_items") or []
    if not line_items:
        raise HTTPException(400, "line_items is required for a swap.")
    o = await load_order(db, store, order_id)
    from services import order_edit
    try:
        r = await order_edit.place_swap(
            o.store or store, o, line_items,
            extra_tags=payload.get("tags"), note=payload.get("note"),
            cancel_original=bool(payload.get("cancel_original", False)),
            notify=bool(payload.get("notify", False)))
    except Exception as e:
        raise HTTPException(400, f"The swap failed: {e}")
    if r.get("cancelled_original"):
        o.cancelled_at = datetime.now(timezone.utc)
        o.processing_status = "Anulată"
        await db.commit()
    return {"success": True, **r}


@router.post("/orders/{order_id}/manual-awb")
async def manual_awb(
    order_id: int,
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Attach an AWB created OUTSIDE the app (directly on the courier's site) to this order.
    Pick a real courier account so tracking + label-download work through the same adapters as
    our own AWBs. Optionally fulfills the order in Shopify with the AWB as tracking.
    Body: {awb, account_key, tracking_url?, fulfill?=true, notify?=false}."""
    awb = (payload.get("awb") or "").strip()
    account_key = (payload.get("account_key") or "").strip()
    if not awb:
        raise HTTPException(400, "awb is required.")
    if not account_key:
        raise HTTPException(400, "account_key (the courier account) is required for tracking and labels.")
    o = await load_order(db, store, order_id)
    if any(s.awb == awb for s in (o.shipments or [])):
        raise HTTPException(400, "This AWB is already attached to the order.")
    from routes.courier_actions import _get_account
    acct = await _get_account(db, store, account_key)  # validates ownership + credentials
    courier = (acct.courier_type or account_key).upper()
    owner = o.store or store
    from services.status_sync_service import _tracking_url
    tracking_url = (payload.get("tracking_url") or "").strip() or _tracking_url(courier, awb)

    ship = models.Shipment(
        order_id=o.id, courier=courier, account_key=account_key, awb=awb,
        courier_specific_data={"external": True, "tracking_url": tracking_url},
        paper_size=(owner.paper_size or "A6"),
    )
    db.add(ship)
    if not o.assigned_courier:
        o.assigned_courier = acct.courier_type or account_key
    o.processing_status = "Procesată"

    fulfilled = False
    if payload.get("fulfill", True) and o.shopify_order_id:
        try:
            gid = await shopify_service.create_fulfillment_with_tracking(
                owner, o.shopify_order_id, tracking_number=awb, tracking_company=courier,
                tracking_url=tracking_url, notify_customer=bool(payload.get("notify", False)))
            if gid:
                ship.shopify_fulfillment_id = str(gid).split("/")[-1]
                fulfilled = True
        except Exception as e:
            logger.info("manual-awb fulfill failed for %s: %s", o.name, e)

    await db.commit()
    await db.refresh(ship)
    return {"success": True, "shipment_id": ship.id, "awb": awb, "courier": courier,
            "fulfilled": fulfilled, "tracking_url": tracking_url,
            "note": "Tracking + label work if this courier supports lookup by AWB (DPD/Sameday/FAN); "
                    "GLS/Packeta need create-time data so their label may be unavailable."}


@router.post("/orders/{order_id}/mark-paid")
async def mark_paid(
    order_id: int,
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Mark the order as PAID in Shopify (e.g. COD collected). Mirrors financial_status locally."""
    o = await load_order(db, store, order_id)
    try:
        await shopify_service.mark_order_as_paid(db, o.store_id, o.shopify_order_id)
    except Exception as e:
        raise HTTPException(400, f"Couldn't mark the order as paid: {e}")
    o.financial_status = "paid"
    o.mapped_payment = o.mapped_payment or "paid"
    await db.commit()
    return {"success": True, "financial_status": "paid"}


@router.post("/orders/{order_id}/mark-delivered")
async def mark_delivered(
    order_id: int,
    payload: Dict[str, Any] = Body(default={}),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Mark the order DELIVERED in Shopify — pushes a DELIVERED delivery event onto its
    fulfillment (Shopify now shows the delivered status). Fulfills first (from the latest AWB)
    if the order isn't fulfilled yet. Body: {notify?=false}."""
    o = await load_order(db, store, order_id)
    owner = o.store or store
    latest = o.shipments[-1] if o.shipments else None

    gid = None
    try:
        gid = await shopify_service.get_latest_fulfillment_gid(owner, o.shopify_order_id)
    except Exception as e:
        logger.info("mark-delivered fulfillment lookup failed for %s: %s", o.name, e)
    if not gid and latest and latest.awb:
        from services.status_sync_service import _tracking_url
        try:
            gid = await shopify_service.create_fulfillment_with_tracking(
                owner, o.shopify_order_id, tracking_number=latest.awb,
                tracking_company=(latest.courier or ""),
                tracking_url=_tracking_url(latest.courier, latest.awb),
                notify_customer=bool(payload.get("notify", False)))
            if gid and not latest.shopify_fulfillment_id:
                latest.shopify_fulfillment_id = str(gid).split("/")[-1]
        except Exception as e:
            raise HTTPException(400, f"Couldn't create the fulfillment: {e}")
    if not gid:
        raise HTTPException(400, "This order has no fulfillment and no AWB — it can't be marked delivered.")

    ev = await shopify_service.add_fulfillment_event(owner, gid, "delivered")
    if latest:
        latest.last_status = "Livrat"
        latest.derived_status = "delivered"
    o.derived_status = "delivered"
    await db.commit()
    return {"success": True, "delivered": True, "event": ev}


@router.post("/orders/{order_id}/email")
async def email_customer(
    order_id: int,
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Email the customer via Shopify's order email (invoice channel). Body:
    {template_id} OR {subject, body}, optional {to} to override the recipient."""
    o = await load_order(db, store, order_id)
    subject, body = await _resolve_email(db, store, o, payload)
    to = (payload.get("to") or "").strip() or None
    try:
        await shopify_service.send_order_email(o.store or store, o.shopify_order_id,
                                               subject=subject, body=body, to=to)
    except Exception as e:
        raise HTTPException(400, f"Sending the email failed: {e}")
    return {"success": True, "sent": True, "subject": subject}
