"""Customer-Service backlog / call-queue.

A prioritized queue of orders that need a human before (or instead of) shipping: bad address,
matched a CS rule (value / products / tags), looks like a duplicate, contains an out-of-stock
product, or was pushed in by hand. While an *unfulfilled* order sits here it is put ON HOLD in
Shopify (fulfilled ones keep their AWB and are never held). The agent works each item —
call/email the customer, add notes + tags — then resolves it: create the AWB directly (which
releases the hold and overrides a false-positive address validation), cancel, or just clear it.

Enqueue is shared (`enqueue_order`) so auto-scans, manual adds, out-of-stock pushes and the
duplicate detector all go through the same hold + flag-tag + auto-email path.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models
from database import get_db
from services import org_service, shopify_service
from services.shopify_auth import require_shop
from services.utils import no_cs
from routes.order_actions import render_email, _tags_to_list

router = APIRouter(prefix="/api/cs-queue", tags=["CS Queue"])
config_router = APIRouter(prefix="/api", tags=["CS Queue Config"])
logger = logging.getLogger(__name__)

_VALID_ADDR = ("valid", "validat")
_REASONS = {"wrong_address", "rule", "duplicate", "out_of_stock", "manual"}
_STATUSES = {"open", "in_progress", "solved"}


# ───────────────────────── settings ─────────────────────────

def _cs(store: models.Store) -> Dict[str, Any]:
    return dict(getattr(store, "cs_settings", None) or {})


def _digits9(phone: Optional[str]) -> str:
    d = "".join(ch for ch in (phone or "") if ch.isdigit())
    return d[-9:] if len(d) >= 9 else d


# ───────────────────────── enqueue (shared) ─────────────────────────

async def enqueue_order(
    db: AsyncSession, store: models.Store, order: models.Order, *,
    reason: str, detail: Optional[str] = None, created_by: str = "auto",
    hold: Optional[bool] = None,
) -> models.CSQueueItem:
    """Upsert a queue item for `order`. Holds the order (if unfulfilled and auto-hold is on or
    `hold=True`), applies the merchant's flag tag, and fires a matching auto-send email once.
    Re-enqueuing an existing item only updates its reason/detail (keeps notes + status).
    NO_CS: pe magazinele fără coadă lucrată (Bonhaus CZ/PL/BG, SK, HU, Orice Redus) NU rutăm la CS —
    HOLD-ul acolo = comandă moartă (nimeni nu lucrează coada). Se lasă cronul să auto-rezolve."""
    if no_cs(store):
        logger.info("NO_CS skip enqueue: %s (%s) reason=%s", order.name, getattr(store, "domain", ""), reason)
        return None
    cs = _cs(store)
    existing = (await db.execute(
        select(models.CSQueueItem).where(models.CSQueueItem.order_id == order.id)
    )).scalar_one_or_none()

    fulfilled = bool(order.shipments)
    want_hold = (cs.get("auto_hold", True) if hold is None else bool(hold)) and not fulfilled and not order.cancelled_at
    is_new = existing is None

    item = existing or models.CSQueueItem(
        store_id=order.store_id, order_id=order.id, notes=[], status="open",
    )
    item.reason = reason
    if detail:
        item.reason_detail = detail
    item.created_by = created_by if is_new else item.created_by
    if is_new:
        item.status = "open"
        item.notes = []
        db.add(item)

    # Hold the unfulfilled order in Shopify so it can't ship while queued.
    if want_hold and not item.was_held and order.shopify_order_id:
        try:
            held = await shopify_service.hold_fulfillment_orders(
                order.store or store, order.shopify_order_id, reason=reason, notes=detail or "CS queue")
            if held:
                item.was_held = True
                order.is_on_hold_shopify = True
        except Exception as e:
            logger.info("enqueue hold failed for %s: %s", order.name, e)

    # Flag tag (cross-system) + auto-email — only when the item is freshly created.
    if is_new and order.shopify_order_id:
        flag = (cs.get("flag_tag") or "").strip()
        if flag:
            try:
                await shopify_service.add_order_tags(order.store or store, order.shopify_order_id, [flag])
            except Exception as e:
                logger.info("enqueue flag-tag failed for %s: %s", order.name, e)
        await _auto_email(db, store, order, reason)
    return item


async def _auto_email(db: AsyncSession, store: models.Store, order: models.Order, reason: str) -> None:
    """Send the first active template whose auto_on == reason (e.g. 'wrong_address')."""
    allowed = await org_service.org_store_ids(db, store)
    tpl = (await db.execute(
        select(models.CSEmailTemplate).where(
            models.CSEmailTemplate.auto_on == reason,
            models.CSEmailTemplate.is_active.is_(True),
            (models.CSEmailTemplate.store_id.in_(allowed)) | (models.CSEmailTemplate.store_id.is_(None)),
        ).order_by(models.CSEmailTemplate.id.asc()).limit(1)
    )).scalar_one_or_none()
    if not tpl:
        return
    try:
        await shopify_service.send_order_email(
            order.store or store, order.shopify_order_id,
            subject=render_email(tpl.subject, order), body=render_email(tpl.body, order))
    except Exception as e:
        logger.info("auto-email failed for %s: %s", order.name, e)


def _append_note(item: Optional[models.CSQueueItem], text: Optional[str], by: str = "system") -> None:
    if item is None or not text:
        return
    log = list(item.notes or [])
    log.append({"at": datetime.now(timezone.utc).isoformat(), "text": text, "by": by})
    item.notes = log


# ───────────────────────── serialization ─────────────────────────

def _order_units(o: models.Order) -> int:
    return sum(int(li.quantity or 0) for li in (o.line_items or []))


def _addr_full(o: models.Order) -> str:
    return ", ".join(x for x in [o.shipping_address1, o.shipping_address2, o.shipping_city,
                                 o.shipping_zip, o.shipping_province, o.shipping_country] if x)


def _admin_url(o: models.Order) -> Optional[str]:
    dom = o.store.domain if o.store else None
    if not dom or not o.shopify_order_id:
        return None
    return f"https://{dom}/admin/orders/{str(o.shopify_order_id).split('/')[-1]}"


def _order_brief(o: models.Order) -> Dict[str, Any]:
    s = o.shipments[-1] if o.shipments else None
    return {
        "id": o.id, "name": o.name, "customer": o.customer,
        "shipping_name": o.shipping_name,
        "total_price": o.total_price,
        "financial_status": o.financial_status,
        "financial_paid": (o.financial_status or "").lower() == "paid",
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "address_status": o.address_status, "address_score": o.address_score,
        "city": o.shipping_city, "zip": o.shipping_zip, "phone": o.shipping_phone,
        "email": o.shipping_email, "province": o.shipping_province, "country": o.shipping_country,
        "address1": o.shipping_address1, "address2": o.shipping_address2,
        "address_full": _addr_full(o),
        "units": _order_units(o), "line_count": len(o.line_items or []),
        "fulfilled": bool(s), "awb": (s.awb if s else None), "courier": (s.courier if s else None),
        "shipment_id": (s.id if s else None),
        "on_hold": bool(o.is_on_hold_shopify), "cancelled": bool(o.cancelled_at),
        "note": o.note, "tags": _tags_to_list(o.tags),
        "store_domain": (o.store.domain if o.store else None),
        "store_name": (o.store.name if o.store else None),
        "admin_url": _admin_url(o),
    }


def _item_json(item: models.CSQueueItem, o: models.Order) -> Dict[str, Any]:
    return {
        "id": item.id, "order_id": item.order_id, "reason": item.reason,
        "reason_detail": item.reason_detail, "status": item.status,
        "priority": item.priority, "was_held": item.was_held, "created_by": item.created_by,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "solved_at": item.solved_at.isoformat() if item.solved_at else None,
        "notes": item.notes or [],
        "order": _order_brief(o),
    }


async def _load_item(db: AsyncSession, store: models.Store, item_id: int) -> models.CSQueueItem:
    allowed = await org_service.org_store_ids(db, store)
    item = (await db.execute(
        select(models.CSQueueItem)
        .options(selectinload(models.CSQueueItem.order).selectinload(models.Order.line_items),
                 selectinload(models.CSQueueItem.order).selectinload(models.Order.shipments),
                 selectinload(models.CSQueueItem.order).selectinload(models.Order.store))
        .where(models.CSQueueItem.id == item_id, models.CSQueueItem.store_id.in_(allowed))
    )).scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Queue item not found.")
    return item


# ───────────────────────── list + panel ─────────────────────────

@router.get("")
async def list_queue(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = None,
    reason: Optional[str] = None,
    scope: Optional[str] = None,
    sort: str = "age",   # age | value | qty | priority
    include_solved: bool = False,
):
    """The queue, prioritized. Filter by status/reason; sort by age (oldest first), value,
    units, or manual priority. Solved items are hidden unless include_solved."""
    store_ids = await org_service.resolve_scope(db, store, scope)

    base = select(models.CSQueueItem).where(models.CSQueueItem.store_id.in_(store_ids))
    if status and status in _STATUSES:
        base = base.where(models.CSQueueItem.status == status)
    elif not include_solved:
        base = base.where(models.CSQueueItem.status != "solved")
    if reason and reason in _REASONS:
        base = base.where(models.CSQueueItem.reason == reason)

    items = (await db.execute(
        base.options(selectinload(models.CSQueueItem.order).selectinload(models.Order.line_items),
                     selectinload(models.CSQueueItem.order).selectinload(models.Order.shipments),
                     selectinload(models.CSQueueItem.order).selectinload(models.Order.store))
        .limit(1000)
    )).scalars().all()

    rows = [(it, it.order) for it in items if it.order is not None]

    def _key(pair):
        it, o = pair
        if sort == "value":
            return -(o.total_price or 0.0)
        if sort == "qty":
            return -_order_units(o)
        if sort == "priority":
            return -((it.priority or 0), )[0]
        # age → oldest first
        return o.created_at.timestamp() if o.created_at else 0.0
    rows.sort(key=_key)

    # counts by reason/status for the header chips
    counts: Dict[str, int] = {}
    for it, _ in rows:
        counts[it.reason] = counts.get(it.reason, 0) + 1
    return {
        "items": [_item_json(it, o) for it, o in rows],
        "total": len(rows), "counts": counts,
    }


@router.get("/{item_id}")
async def panel(
    item_id: int,
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Full call-panel for one queue item: the order (phone/email/address/contents/notes/tags),
    a link to open it in Shopify (full timeline), the customer's OTHER orders, and the email
    templates the agent can send."""
    item = await _load_item(db, store, item_id)
    o = item.order
    allowed = await org_service.org_store_ids(db, store)

    # Customer's other orders — match by phone (last 9 digits) or email, across the org.
    # Phone/email are encrypted at rest, so we match on their blind indexes.
    import crypto
    d9 = _digits9(o.shipping_phone)
    email = (o.shipping_email or "").strip().lower()
    others: List[Dict[str, Any]] = []
    if d9 or email:
        conds = []
        if d9:
            conds.append(models.Order.shipping_phone_bidx == crypto.phone_blind_index(o.shipping_phone))
        if email:
            conds.append(models.Order.shipping_email_bidx == crypto.blind_index(email))
        oth = (await db.execute(
            select(models.Order)
            .options(selectinload(models.Order.shipments))
            .where(models.Order.store_id.in_(allowed), models.Order.id != o.id, or_(*conds))
            .order_by(models.Order.created_at.desc()).limit(25)
        )).scalars().all()
        for x in oth:
            s = x.shipments[-1] if x.shipments else None
            others.append({
                "id": x.id, "name": x.name, "total_price": x.total_price,
                "financial_status": x.financial_status,
                "created_at": x.created_at.isoformat() if x.created_at else None,
                "city": x.shipping_city, "awb": (s.awb if s else None),
                "cancelled": bool(x.cancelled_at),
            })

    # line items with per-line detail
    lines = [{"sku": li.sku, "title": li.title, "quantity": li.quantity} for li in (o.line_items or [])]
    shipments = [{"id": s.id, "awb": s.awb, "courier": s.courier, "last_status": s.last_status,
                  "paper_size": s.paper_size} for s in sorted(o.shipments or [], key=lambda s: s.id)]

    templates = (await db.execute(
        select(models.CSEmailTemplate).where(
            models.CSEmailTemplate.is_active.is_(True),
            (models.CSEmailTemplate.store_id.in_(allowed)) | (models.CSEmailTemplate.store_id.is_(None)),
        ).order_by(models.CSEmailTemplate.name.asc())
    )).scalars().all()

    return {
        **_item_json(item, o),
        "line_items": lines,
        "shipments": shipments,
        "other_orders": others,
        "other_orders_count": len(others),
        "email_templates": [{"id": t.id, "name": t.name, "subject": t.subject, "body": t.body,
                             "auto_on": t.auto_on} for t in templates],
    }


# ───────────────────────── mutations ─────────────────────────

@router.post("/add")
async def add(
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Manually push an order into the queue. Body: {order_id, reason?, detail?, hold?}."""
    order_id = payload.get("order_id")
    if not order_id:
        raise HTTPException(400, "order_id is required.")
    reason = payload.get("reason") or "manual"
    if reason not in _REASONS:
        reason = "manual"
    allowed = await org_service.org_store_ids(db, store)
    o = (await db.execute(
        select(models.Order)
        .options(selectinload(models.Order.line_items), selectinload(models.Order.shipments),
                 selectinload(models.Order.store))
        .where(models.Order.id == int(order_id), models.Order.store_id.in_(allowed))
    )).scalar_one_or_none()
    if not o:
        raise HTTPException(404, "Order not found.")
    item = await enqueue_order(db, store, o, reason=reason, detail=payload.get("detail"),
                               created_by="manual", hold=payload.get("hold"))
    await db.commit()
    await db.refresh(item)
    return {"success": True, **_item_json(item, o)}


@router.post("/bulk-add")
async def bulk_add(
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Push many orders in at once (e.g. every unshipped order with an out-of-stock product).
    Body: {order_ids:[...], reason?, detail?, hold?}."""
    ids = payload.get("order_ids") or []
    if not ids:
        raise HTTPException(400, "order_ids is required.")
    reason = payload.get("reason") if payload.get("reason") in _REASONS else "manual"
    allowed = await org_service.org_store_ids(db, store)
    added = 0
    for oid in ids:
        o = (await db.execute(
            select(models.Order)
            .options(selectinload(models.Order.line_items), selectinload(models.Order.shipments),
                     selectinload(models.Order.store))
            .where(models.Order.id == int(oid), models.Order.store_id.in_(allowed))
        )).scalar_one_or_none()
        if not o:
            continue
        await enqueue_order(db, store, o, reason=reason, detail=payload.get("detail"),
                            created_by="manual", hold=payload.get("hold"))
        added += 1
    await db.commit()
    return {"success": True, "added": added}


@router.post("/{item_id}/status")
async def set_status(
    item_id: int,
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Move an item through Open → Work-in-progress → Solved."""
    new = (payload.get("status") or "").strip().lower()
    if new not in _STATUSES:
        raise HTTPException(400, "status invalid (open|in_progress|solved).")
    item = await _load_item(db, store, item_id)
    item.status = new
    item.solved_at = datetime.now(timezone.utc) if new == "solved" else None
    await db.commit()
    return {"success": True, "status": new}


@router.post("/{item_id}/note")
async def add_note(
    item_id: int,
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Append a timestamped CS note to the item's running log (internal, not sent to Shopify)."""
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text is required.")
    item = await _load_item(db, store, item_id)
    _append_note(item, text, by=(payload.get("by") or "cs"))
    if item.status == "open":
        item.status = "in_progress"
    await db.commit()
    return {"success": True, "notes": item.notes or []}


@router.post("/{item_id}/priority")
async def set_priority(
    item_id: int,
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    item = await _load_item(db, store, item_id)
    item.priority = int(payload.get("priority") or 0)
    await db.commit()
    return {"success": True, "priority": item.priority}


@router.post("/{item_id}/resolve-awb")
async def resolve_awb(
    item_id: int,
    payload: Dict[str, Any] = Body(default={}),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """CS decides to ship it now: release any hold, create the AWB, mark the item solved.
    Creating the AWB deliberately OVERRIDES a failed address validation (the validator can
    be wrong; CS confirmed the address on the phone). Uses a passed profile/account, else the
    order's assigned profile, else a matching rule, else the store default."""
    from routes.courier_actions import _profile_base, _create_one, _request_pickup, _resolve_target
    from services import shipment_rules

    item = await _load_item(db, store, item_id)
    o = item.order
    if o.shipments:
        raise HTTPException(400, "This order already has an AWB.")
    if o.cancelled_at:
        raise HTTPException(400, "This order is cancelled.")

    # Release the hold first so the fulfillment order is shippable.
    if item.was_held or o.is_on_hold_shopify:
        try:
            await shopify_service.release_fulfillment_order_holds(o.store or store, o.shopify_order_id)
        except Exception as e:
            logger.info("resolve release-hold failed for %s: %s", o.name, e)
        o.is_on_hold_shopify = False

    # Resolve courier/options.
    if payload.get("profile_id") or payload.get("account_key"):
        account_key, options = await _resolve_target(db, store, payload)
    else:
        rules = (await db.execute(
            select(models.ShipmentRule).where(
                models.ShipmentRule.enabled.is_(True),
                (models.ShipmentRule.store_id == o.store_id) | (models.ShipmentRule.store_id.is_(None)),
            ).order_by(models.ShipmentRule.priority.asc(), models.ShipmentRule.id.asc())
        )).scalars().all()
        pid = (getattr(o, "assigned_profile_id", None)
               or shipment_rules.pick_profile_id(o, rules)
               or getattr(o.store or store, "auto_awb_profile_id", None))
        if pid:
            account_key, options = await _profile_base(db, store, pid)
        elif getattr(o.store or store, "auto_awb_account_key", None):
            account_key, options = (o.store or store).auto_awb_account_key, {}
        else:
            raise HTTPException(400, "No courier to route to — pick a profile or a courier account.")

    try:
        r = await _create_one(db, store, o, account_key, options)
        item.status = "solved"
        item.solved_at = datetime.now(timezone.utc)
        _append_note(item, f"AWB {r['awb']} created by CS ({account_key}).", by="cs")
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(400, f"Couldn't create the AWB: {e}")

    pickup = None
    if payload.get("request_pickup", True):
        pickup = await _request_pickup(db, store, account_key, r["awb"], options)
    return {"success": True, **r, "pickup": pickup, "status": "solved"}


@router.post("/{item_id}/remove")
async def remove(
    item_id: int,
    payload: Dict[str, Any] = Body(default={}),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Remove an item from the queue. By default releases any hold WE applied (so the order can
    ship normally again). Pass {keep_hold:true} to leave it held."""
    item = await _load_item(db, store, item_id)
    o = item.order
    if item.was_held and not payload.get("keep_hold") and o.shopify_order_id:
        try:
            await shopify_service.release_fulfillment_order_holds(o.store or store, o.shopify_order_id)
            o.is_on_hold_shopify = False
        except Exception as e:
            logger.info("remove release-hold failed for %s: %s", o.name, e)
    await db.delete(item)
    await db.commit()
    return {"success": True, "removed": True}


# ───────────────────────── scan (auto-enqueue) ─────────────────────────

@router.post("/scan")
async def scan(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
    scope: Optional[str] = None,
):
    """Sweep current UNSHIPPED, non-cancelled orders and enqueue the ones that need CS:
    invalid address (if enabled) + orders matching the CS rules (value_min / products_any /
    tags_any). Duplicate + out-of-stock have their own paths. Idempotent — re-running only
    tops up. Returns per-reason counts."""
    cs = _cs(store)
    store_ids = await org_service.org_store_ids(db, store) if scope == "all" else [store.id]

    has_awb = select(models.Shipment.id).where(
        models.Shipment.order_id == models.Order.id, models.Shipment.awb.isnot(None))
    orders = (await db.execute(
        select(models.Order)
        .options(selectinload(models.Order.line_items), selectinload(models.Order.shipments),
                 selectinload(models.Order.store))
        .where(models.Order.store_id.in_(store_ids), models.Order.cancelled_at.is_(None),
               ~has_awb.exists())
        .order_by(models.Order.created_at.asc()).limit(2000)
    )).scalars().all()

    # Existing queue to avoid double-enqueue churn.
    queued_ids = set((await db.execute(
        select(models.CSQueueItem.order_id).where(models.CSQueueItem.store_id.in_(store_ids))
    )).scalars().all())

    value_min = cs.get("value_min")
    products_any = {str(s).strip().lower() for s in (cs.get("products_any") or []) if str(s).strip()}
    tags_any = {str(t).strip().lower() for t in (cs.get("tags_any") or []) if str(t).strip()}
    do_addr = bool(cs.get("auto_enqueue_wrong_address", True))

    # Duplicate detection (port of MergeGuard's idea): among the unshipped orders, group by a
    # customer key (phone / email / address) inside a time window; 2+ in a group = duplicates.
    dup_ids: Dict[int, str] = {}
    if cs.get("duplicate_enabled"):
        from datetime import timedelta
        window_h = int(cs.get("duplicate_window_hours") or 72)
        match = (cs.get("duplicate_match") or "phone").lower()

        def _key(o: models.Order) -> Optional[str]:
            if match == "email":
                return (o.shipping_email or "").strip().lower() or None
            if match == "address":
                k = (_addr_full(o)).strip().lower()
                return k or None
            return _digits9(o.shipping_phone) or None

        groups: Dict[str, List[models.Order]] = {}
        for o in orders:
            k = _key(o)
            if k:
                groups.setdefault(k, []).append(o)
        for k, grp in groups.items():
            if len(grp) < 2:
                continue
            grp.sort(key=lambda x: x.created_at or datetime.min.replace(tzinfo=timezone.utc))
            first = grp[0]
            span = None
            if first.created_at and grp[-1].created_at:
                span = grp[-1].created_at - first.created_at
            if span is not None and span > timedelta(hours=window_h):
                continue  # too far apart to be a genuine duplicate
            for o in grp:
                dup_ids[o.id] = f"possible duplicate of {first.name} ({len(grp)} orders, key {match})"

    counts = {"wrong_address": 0, "rule": 0, "duplicate": 0}
    for o in orders:
        if o.id in queued_ids:
            continue
        reason = None
        detail = None
        if do_addr and (o.address_status or "").lower() not in _VALID_ADDR:
            reason, detail = "wrong_address", (o.address_status or "nevalidat")
        if reason is None and o.id in dup_ids:
            reason, detail = "duplicate", dup_ids[o.id]
        if reason is None:
            skus = {(li.sku or "").strip().lower() for li in (o.line_items or [])}
            otags = {t.lower() for t in _tags_to_list(o.tags)}
            hit = []
            if value_min is not None and (o.total_price or 0) >= float(value_min):
                hit.append(f"valoare ≥ {value_min}")
            if products_any and (skus & products_any):
                hit.append("produs: " + ", ".join(sorted(skus & products_any)))
            if tags_any and (otags & tags_any):
                hit.append("tag: " + ", ".join(sorted(otags & tags_any)))
            if hit:
                reason, detail = "rule", "; ".join(hit)
        if reason is None:
            continue
        await enqueue_order(db, store, o, reason=reason, detail=detail, created_by="auto")
        queued_ids.add(o.id)
        counts[reason if reason in counts else "rule"] += 1
    await db.commit()
    return {"success": True, "scanned": len(orders), "enqueued": counts}


@router.post("/scan-oos")
async def scan_oos(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
    scope: Optional[str] = None,
):
    """Flag orders that would drive stock NEGATIVE: aggregate unshipped demand per SKU, compare
    to Shopify's available stock, and enqueue every unshipped order containing a SKU that's out
    of stock (≤0) or oversold (demand > available). Reason = out_of_stock. Uses read_products."""
    store_ids = await org_service.org_store_ids(db, store) if scope == "all" else [store.id]
    has_awb = select(models.Shipment.id).where(
        models.Shipment.order_id == models.Order.id, models.Shipment.awb.isnot(None))
    orders = (await db.execute(
        select(models.Order)
        .options(selectinload(models.Order.line_items), selectinload(models.Order.shipments),
                 selectinload(models.Order.store))
        .where(models.Order.store_id.in_(store_ids), models.Order.cancelled_at.is_(None),
               ~has_awb.exists())
        .order_by(models.Order.created_at.asc()).limit(2000)
    )).scalars().all()

    demand: Dict[str, int] = {}
    order_skus: Dict[int, set] = {}
    for o in orders:
        for li in (o.line_items or []):
            sku = (li.sku or "").strip().lower()
            if not sku:
                continue
            demand[sku] = demand.get(sku, 0) + int(li.quantity or 0)
            order_skus.setdefault(o.id, set()).add(sku)
    if not demand:
        return {"success": True, "enqueued": 0, "short_skus": 0, "checked": 0,
                "message": "No unshipped orders with a SKU."}

    inv = await shopify_service.get_variant_inventory(store, list(demand.keys()))
    short: Dict[str, tuple] = {}
    for sku, dem in demand.items():
        avail = inv.get(sku)
        if avail is None:
            continue  # unknown SKU — don't guess
        if avail <= 0 or avail < dem:
            short[sku] = (avail, dem)

    queued = set((await db.execute(
        select(models.CSQueueItem.order_id).where(models.CSQueueItem.store_id.in_(store_ids))
    )).scalars().all())
    n = 0
    for o in orders:
        if o.id in queued:
            continue
        hit = [s for s in order_skus.get(o.id, ()) if s in short]
        if not hit:
            continue
        detail = "Stoc insuficient: " + ", ".join(
            f"{s} (stoc {short[s][0]} / cerere {short[s][1]})" for s in hit[:3])
        await enqueue_order(db, store, o, reason="out_of_stock", detail=detail, created_by="auto")
        queued.add(o.id)
        n += 1
    await db.commit()
    return {"success": True, "enqueued": n, "short_skus": len(short), "checked": len(demand)}


@router.post("/product-search")
async def product_search(
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Find UNSHIPPED, non-cancelled orders containing a product (by SKU or title fragment) so CS
    can bulk-add them (e.g. it just went out of stock in the warehouse). Body: {q, scope?}.
    Returns candidates with a `queued` flag; the UI selects some and calls /bulk-add."""
    q = (payload.get("q") or "").strip()
    if not q:
        raise HTTPException(400, "q (SKU or title) is required.")
    store_ids = await org_service.org_store_ids(db, store) if payload.get("scope") == "all" else [store.id]

    like = f"%{q.lower()}%"
    has_awb = select(models.Shipment.id).where(
        models.Shipment.order_id == models.Order.id, models.Shipment.awb.isnot(None))
    match_li = select(models.LineItem.order_id).where(
        models.LineItem.order_id == models.Order.id,
        or_(func.lower(models.LineItem.sku).like(like), func.lower(models.LineItem.title).like(like)))
    orders = (await db.execute(
        select(models.Order)
        .options(selectinload(models.Order.line_items), selectinload(models.Order.store))
        .where(models.Order.store_id.in_(store_ids), models.Order.cancelled_at.is_(None),
               ~has_awb.exists(), match_li.exists())
        .order_by(models.Order.created_at.asc()).limit(500)
    )).scalars().all()

    queued_ids = set((await db.execute(
        select(models.CSQueueItem.order_id).where(models.CSQueueItem.order_id.in_([o.id for o in orders] or [0]))
    )).scalars().all())

    out = []
    for o in orders:
        matched = [{"sku": li.sku, "title": li.title, "quantity": li.quantity}
                   for li in (o.line_items or [])
                   if q.lower() in (li.sku or "").lower() or q.lower() in (li.title or "").lower()]
        out.append({
            "id": o.id, "name": o.name, "customer": o.customer, "city": o.shipping_city,
            "total_price": o.total_price, "created_at": o.created_at.isoformat() if o.created_at else None,
            "address_status": o.address_status, "units": _order_units(o),
            "matched": matched, "queued": o.id in queued_ids,
            "store_name": (o.store.name if o.store else None),
        })
    return {"candidates": out, "total": len(out)}


# ───────────────────────── config: CS settings + email templates ─────────────────────────

_CS_KEYS = {"auto_hold", "auto_enqueue_wrong_address", "value_min", "products_any", "tags_any",
            "categories_any", "duplicate_enabled", "duplicate_window_hours", "duplicate_match",
            "flag_tag", "hold_reason"}


@config_router.get("/cs-settings")
async def get_cs_settings(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    cs = _cs(store)
    cs.setdefault("auto_hold", True)
    cs.setdefault("auto_enqueue_wrong_address", True)
    cs.setdefault("duplicate_enabled", False)
    cs.setdefault("duplicate_window_hours", 72)
    cs.setdefault("duplicate_match", "phone")
    return {"settings": cs}


@config_router.put("/cs-settings")
async def put_cs_settings(
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    cur = _cs(store)
    for k, v in (payload or {}).items():
        if k in _CS_KEYS:
            cur[k] = v
    # normalize list fields
    for lk in ("products_any", "tags_any", "categories_any"):
        if lk in cur and not isinstance(cur[lk], list):
            cur[lk] = [s.strip() for s in str(cur[lk]).split(",") if s.strip()]
    store.cs_settings = cur
    db.add(store)
    await db.commit()
    return {"success": True, "settings": cur}


def _tpl_json(t: models.CSEmailTemplate) -> Dict[str, Any]:
    return {"id": t.id, "name": t.name, "subject": t.subject, "body": t.body,
            "description": t.description, "auto_on": t.auto_on, "is_active": t.is_active}


# The situations a CS agent actually faces, with a ready email for each. Seeded on request so a new
# merchant isn't staring at an empty template list wondering what to write. All editable afterwards.
STARTER_TEMPLATES = [
    {"name": "Confirm the shipping address",
     "description": "The address failed validation — ask the customer to confirm it before dispatch.",
     "auto_on": "wrong_address",
     "subject": "We need to confirm your address for order {order}",
     "body": ("Hi {customer},\n\nWe're getting order {order} ready to ship, but we couldn't confirm "
              "this delivery address:\n\n{address}\n\nCould you reply with the correct street, number "
              "and postcode? As soon as we have it, the parcel goes out.\n\nThank you,\n{store}")},
    {"name": "Confirm a cash-on-delivery order",
     "description": "High-value or first-time COD order — confirm the customer will accept it.",
     "auto_on": None,
     "subject": "Please confirm order {order}",
     "body": ("Hi {customer},\n\nJust confirming order {order} before we send it out for cash on "
              "delivery to:\n\n{address}\n\nReply YES and we'll dispatch it today.\n\nThank you,\n{store}")},
    {"name": "Delivery is running late",
     "description": "The parcel is in transit but past the normal delivery window.",
     "auto_on": None,
     "subject": "Your order {order} is running late",
     "body": ("Hi {customer},\n\nYour parcel (AWB {tracking}) is taking longer than usual to reach "
              "you. We're chasing the courier and will keep you posted.\n\nSorry for the "
              "wait,\n{store}")},
    {"name": "Delivery was refused / returned",
     "description": "The courier reported the parcel refused or returned — offer to resend.",
     "auto_on": None,
     "subject": "About your order {order}",
     "body": ("Hi {customer},\n\nThe courier returned order {order} to us. If you'd still like it, "
              "reply and we'll send it out again — just confirm the delivery address and a good time "
              "to receive it.\n\nThank you,\n{store}")},
    {"name": "Item is out of stock",
     "description": "Something on the order can't be fulfilled — offer a swap or a refund.",
     "auto_on": "out_of_stock",
     "subject": "An item from order {order} is out of stock",
     "body": ("Hi {customer},\n\nOne of the items in order {order} has just sold out. We can send "
              "the rest right away, swap it for something similar, or refund that item — whichever "
              "you prefer.\n\nJust reply and let us know.\n\nThank you,\n{store}")},
    {"name": "Duplicate order",
     "description": "The same customer ordered twice — check before shipping both.",
     "auto_on": "duplicate",
     "subject": "Did you mean to order twice?",
     "body": ("Hi {customer},\n\nWe received two similar orders from you, including {order}. Before "
              "we ship both, could you confirm whether that was intentional? If not, we'll cancel "
              "the duplicate.\n\nThank you,\n{store}")},
    {"name": "Your parcel is on its way",
     "description": "Reassurance after dispatch, with the tracking number.",
     "auto_on": None,
     "subject": "Order {order} is on its way",
     "body": ("Hi {customer},\n\nYour order {order} has left our warehouse. You can follow it with "
              "AWB {tracking}.\n\nDelivering to:\n{address}\n\nThank you,\n{store}")},
]


@config_router.post("/cs-email-templates/seed-defaults")
async def seed_default_templates(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Create the starter template set, skipping any name this store already has."""
    existing = {n for (n,) in (await db.execute(
        select(models.CSEmailTemplate.name).where(models.CSEmailTemplate.store_id == store.id)
    )).all()}
    created = 0
    for t in STARTER_TEMPLATES:
        if t["name"] in existing:
            continue
        db.add(models.CSEmailTemplate(
            store_id=store.id, name=t["name"], subject=t["subject"], body=t["body"],
            description=t["description"], auto_on=t["auto_on"], is_active=True))
        created += 1
    await db.commit()
    return {"success": True, "created": created, "skipped": len(STARTER_TEMPLATES) - created}


@config_router.get("/cs-email-templates")
async def list_templates(
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    allowed = await org_service.org_store_ids(db, store)
    rows = (await db.execute(
        select(models.CSEmailTemplate).where(
            (models.CSEmailTemplate.store_id.in_(allowed)) | (models.CSEmailTemplate.store_id.is_(None))
        ).order_by(models.CSEmailTemplate.name.asc())
    )).scalars().all()
    return {"templates": [_tpl_json(t) for t in rows]}


@config_router.post("/cs-email-templates")
async def create_template(
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    name = (payload.get("name") or "").strip()
    subject = (payload.get("subject") or "").strip()
    body = (payload.get("body") or "").strip()
    if not (name and subject and body):
        raise HTTPException(400, "name, subject and body are required.")
    auto_on = (payload.get("auto_on") or "").strip() or None
    if auto_on and auto_on not in _REASONS:
        raise HTTPException(400, "auto_on invalid.")
    t = models.CSEmailTemplate(store_id=store.id, name=name, subject=subject, body=body,
                               description=(payload.get("description") or "").strip() or None,
                               auto_on=auto_on, is_active=bool(payload.get("is_active", True)))
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return {"success": True, **_tpl_json(t)}


@config_router.put("/cs-email-templates/{tpl_id}")
async def update_template(
    tpl_id: int,
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    allowed = await org_service.org_store_ids(db, store)
    t = (await db.execute(
        select(models.CSEmailTemplate).where(
            models.CSEmailTemplate.id == tpl_id, models.CSEmailTemplate.store_id.in_(allowed))
    )).scalar_one_or_none()
    if not t:
        raise HTTPException(404, "Template not found.")
    for k in ("name", "subject", "body"):
        if k in payload and str(payload[k]).strip():
            setattr(t, k, str(payload[k]).strip())
    if "description" in payload:   # blank clears it, unlike the required fields above
        t.description = (payload.get("description") or "").strip() or None
    if "auto_on" in payload:
        ao = (payload.get("auto_on") or "").strip() or None
        if ao and ao not in _REASONS:
            raise HTTPException(400, "auto_on invalid.")
        t.auto_on = ao
    if "is_active" in payload:
        t.is_active = bool(payload["is_active"])
    await db.commit()
    return {"success": True, **_tpl_json(t)}


@config_router.delete("/cs-email-templates/{tpl_id}")
async def delete_template(
    tpl_id: int,
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    allowed = await org_service.org_store_ids(db, store)
    t = (await db.execute(
        select(models.CSEmailTemplate).where(
            models.CSEmailTemplate.id == tpl_id, models.CSEmailTemplate.store_id.in_(allowed))
    )).scalar_one_or_none()
    if not t:
        raise HTTPException(404, "Template not found.")
    await db.delete(t)
    await db.commit()
    return {"success": True, "deleted": True}
