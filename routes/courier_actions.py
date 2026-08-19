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
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models
from database import get_db
from services import org_service, packing, shopify_service, shopify_billing
from services.couriers import get_courier_service
from services.shopify_auth import require_shop

router = APIRouter(prefix="/api", tags=["Courier Actions"])
logger = logging.getLogger(__name__)


async def _get_account(db: AsyncSession, store: models.Store, account_key: str) -> models.CourierAccount:
    # Org-aware: an account owned by ANY store in the requester's organization (or a shared
    # NULL-store account) is usable — so portfolio actions find the owning store's courier.
    allowed = await org_service.org_store_ids(db, store)
    acct = (await db.execute(
        select(models.CourierAccount).where(
            models.CourierAccount.account_key == account_key,
            (models.CourierAccount.store_id.in_(allowed)) | (models.CourierAccount.store_id.is_(None)),
        )
    )).scalar_one_or_none()
    if not acct:
        # Legacy / webhook shipments store a GENERIC key ("dpd", "sameday") or the courier NAME
        # instead of a real account_key (dpd-jg / dpd-px / dpd-ro). Fall back to any credentialed
        # account of that courier type — so "print" resolves instead of "cont curier negăsit".
        ct = (account_key or "").strip().lower()
        acct = (await db.execute(
            select(models.CourierAccount)
            .where(
                (models.CourierAccount.courier_type == ct)
                | (models.CourierAccount.account_key.ilike(f"{ct}%")),
                (models.CourierAccount.store_id.in_(allowed)) | (models.CourierAccount.store_id.is_(None)),
                models.CourierAccount.credentials.isnot(None),
            )
            .order_by(models.CourierAccount.store_id.isnot(None).desc(), models.CourierAccount.account_key)
            .limit(1)
        )).scalar_one_or_none()
    if not acct:
        raise HTTPException(404, f"Courier account '{account_key}' not found.")
    if not acct.credentials:
        raise HTTPException(400, f"Courier account '{account_key}' has no credentials configured.")
    # ── Test mode is a hard wall in BOTH directions ──────────────────────────────────────────
    # While sandboxing, a real courier account must not be reachable: the whole point is that a
    # merchant (or a reviewer) can press every button without a single real parcel being booked.
    # And a sandbox account must never be usable once test mode is off, so demo AWBs can't leak
    # into live operations.
    in_test = bool(getattr(store, "test_mode", False))
    if in_test and not acct.is_demo:
        raise HTTPException(400,
                            f"Test mode is on, so real courier accounts are disabled — "
                            f"'{acct.name or account_key}' wasn't contacted. Use one of the sandbox "
                            f"couriers, or turn test mode off in Settings.")
    if not in_test and acct.is_demo:
        raise HTTPException(400,
                            f"'{acct.name or account_key}' is a sandbox courier and only works in "
                            f"test mode.")
    return acct


async def _profile_base(db: AsyncSession, store: models.Store, profile_id) -> tuple[str, Dict[str, Any]]:
    """Resolve a saved ShipmentProfile (org-scoped) into (account_key, base_options) so the
    operator picks a profile ONCE instead of re-entering courier + parcels + weight + dims +
    content on every order. Returns option keys the adapters already read."""
    allowed = await org_service.org_store_ids(db, store)
    prof = (await db.execute(
        select(models.ShipmentProfile).where(
            models.ShipmentProfile.id == int(profile_id),
            (models.ShipmentProfile.store_id.in_(allowed)) | (models.ShipmentProfile.store_id.is_(None)),
        )
    )).scalar_one_or_none()
    if not prof:
        raise HTTPException(404, f"Shipment profile {profile_id} not found.")
    opts: Dict[str, Any] = dict(prof.dpd_payload_template or {})
    if prof.default_parcels:    opts["parcels_count"] = prof.default_parcels
    if prof.default_weight_kg:  opts["total_weight"] = prof.default_weight_kg
    if prof.default_width_cm:   opts["width"] = prof.default_width_cm
    if prof.default_height_cm:  opts["height"] = prof.default_height_cm
    if prof.default_length_cm:  opts["length"] = prof.default_length_cm
    if prof.default_service_id: opts["service_id"] = prof.default_service_id
    if prof.default_payer:      opts["payer"] = prof.default_payer
    if prof.default_packing:    opts["package"] = prof.default_packing
    if prof.content_template:   opts["content_template"] = prof.content_template
    if getattr(prof, "default_label_size", None):
        opts["label_size"] = prof.default_label_size  # auto-set label size from the profile
    return prof.account_key, opts


async def _resolve_target(db: AsyncSession, store: models.Store, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    """Combine an optional saved profile with the explicit request. The profile supplies the
    defaults (courier + parcels + weight + dims + content); anything explicit in the payload
    wins over it. Validates that a courier was resolved from one source or the other."""
    account_key = (payload.get("account_key") or "").strip()
    options = dict(payload.get("options") or {})
    profile_id = payload.get("profile_id")
    if profile_id:
        p_key, p_opts = await _profile_base(db, store, profile_id)
        account_key = account_key or p_key
        options = {**p_opts, **options}  # profile UNDER explicit options
    if not account_key:
        raise HTTPException(400, "account_key or profile_id is required.")
    return account_key, options


async def _load_order(db: AsyncSession, store: models.Store, order_id: int) -> models.Order:
    # Org-aware: the order may belong to any store linked into the requester's organization.
    allowed = await org_service.org_store_ids(db, store)
    o = (await db.execute(
        select(models.Order)
        .options(selectinload(models.Order.line_items), selectinload(models.Order.shipments),
                 selectinload(models.Order.store))
        .where(models.Order.id == order_id, models.Order.store_id.in_(allowed))
    )).scalar_one_or_none()
    if not o:
        raise HTTPException(404, f"Order {order_id} not found.")
    return o


def _cod_for(order: models.Order) -> float:
    """COD = order total unless already paid online."""
    if (order.financial_status or "").lower() == "paid":
        return 0.0
    try:
        return float(order.total_price or 0.0)
    except (TypeError, ValueError):
        return 0.0


async def _auto_parcels(store: models.Store, order: models.Order,
                        per_product_override: Optional[bool] = None) -> Optional[int]:
    """Parcels from the store's packing-density metafield (parcels-per-piece), computed live from
    Shopify. Rounds shared (ceil of the sum) by default, or PER PRODUCT (sum of ceils) when the
    caller passes `per_product_override` (a manual per-order choice) or — absent that — when the
    order carries the store's configured tag. Returns None on any miss so the caller keeps the
    profile/default parcel count."""
    ns_key = (getattr(store, "packing_metafield", None) or "").strip()
    if not ns_key or "." not in ns_key or not getattr(order, "shopify_order_id", None):
        return None
    ns, key = ns_key.split(".", 1)
    if per_product_override is not None:
        per_product = bool(per_product_override)
    else:
        tag = (getattr(store, "packing_per_product_tag", None) or "").strip().lower()
        order_tags = [t.strip().lower() for t in (getattr(order, "tags", "") or "").split(",") if t.strip()]
        per_product = bool(tag) and tag in order_tags
    try:
        items = await shopify_service.get_order_packing_units(store, order.shopify_order_id, ns.strip(), key.strip())
        return shopify_service.parcels_from_units(items, per_product=per_product)
    except Exception as e:
        logger.info("auto-parcels metafield read failed for %s: %s", getattr(order, "name", "?"), e)
        return None


async def _create_one(
    db: AsyncSession, store: models.Store, order: models.Order,
    account_key: str, options: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Create an AWB for one order via the right adapter, persist a Shipment. Caller commits.
    Per-store settings (sender, content, paper size) come from the ORDER'S OWN store, so a
    portfolio action on a linked store's order uses that store's branding."""
    # Plan quota — the single choke point for every AWB (single, bulk, split, background auto-AWB),
    # so the Free monthly cap is enforced everywhere and Pro genuinely unlocks unlimited labels.
    await shopify_billing.assert_label_quota(db, store)
    owner = order.store or store
    acct = await _get_account(db, store, account_key)
    svc = get_courier_service(acct.courier_type or account_key)
    if not svc:
        raise HTTPException(400, f"Unsupported courier: {acct.courier_type or account_key}")

    opts = dict(options or {})
    opts.setdefault("cod_amount", _cod_for(order))
    # In-app packing → automatic parcel count + box dimensions + total weight on the AWB. Anything
    # the user set explicitly for THIS order (per-order override) is preserved via `_explicit`.
    explicit = set(opts.pop("_explicit", []) or [])
    if opts.pop("parcels_count_explicit", False):
        explicit.add("parcels_count")  # back-compat flag
    per_prod = packing.rounding_is_per_product(owner, order, opts.pop("per_product", None))
    pack = await packing.compute(db, owner, order, per_prod)
    # Advanced fallback: if no in-app pieces rule produced a count, use the Shopify metafield.
    if pack.get("parcels") is None and getattr(owner, "packing_metafield", None) and "parcels_count" not in explicit:
        mf = await _auto_parcels(owner, order, per_product_override=per_prod)
        if mf:
            pack["parcels"] = mf
    # Ultimul fallback = harta CENTRALĂ SKU→cutii (`sku_box_map`, ex. HA-0047 = 1 colet/bucată), calculată
    # LIVE ca în cron. Fără ea, o comandă a cărei valoare n-a fost memorată (comandă veche, sau detectorul
    # n-a apucat să ruleze) pleca cu 1 colet deși are 3 — tăcut. Ordinea: override explicit > parcel_count
    # per comandă (metafield depozit) > reguli de packing ale magazinului > hartă SKU > 1.
    if (pack.get("parcels") is None and not getattr(order, "parcel_count", None)
            and "parcels_count" not in explicit):
        try:
            from services.cron_parity import parcels as _p
            n_map = _p.parcel_count(order, await _p._box_map(db))
            if n_map and n_map > 1:
                pack["parcels"] = n_map
        except Exception as e:
            logger.info("sku_box_map fallback a picat pt %s: %s", getattr(order, "name", "?"), e)
    packing.apply_to_options(opts, pack, skip=explicit)
    opts.setdefault("parcels_count", 1)
    opts.setdefault("total_weight", 1.0)
    if getattr(owner, "sender_name", None) and not opts.get("sender_name"):
        opts["sender_name"] = owner.sender_name  # per-store expeditor name on the AWB
    if not opts.get("content"):
        from services.utils import render_content
        tmpl = opts.pop("content_template", None) or getattr(owner, "content_template", None)
        opts["content"] = render_content(tmpl, order)
    else:
        opts.pop("content_template", None)

    try:
        res = await svc.create_awb(db=db, order=order, account_key=account_key, options=opts)
    except NotImplementedError:
        raise HTTPException(400, f"Creating an AWB isn't supported for {acct.courier_type}.")
    except Exception as e:
        raise HTTPException(400, f"{account_key}: {e}")

    awb = res.get("awb") if isinstance(res, dict) else None
    if not awb:
        raise HTTPException(400, f"{account_key}: the response contained no AWB ({res}).")

    shipment = models.Shipment(
        order_id=order.id,
        courier=(acct.courier_type or account_key).upper(),
        account_key=account_key,
        awb=str(awb),
        courier_specific_data=(res.get("raw") if isinstance(res, dict) else None),
        paper_size=(opts.get("label_size") or owner.paper_size or "A6"),
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
    Body: {order_id, profile_id? | account_key, options?, request_pickup? (default true)}.
    A saved profile fills courier + parcels + weight + dims + content in one pick."""
    order_id = payload.get("order_id")
    if not order_id:
        raise HTTPException(400, "order_id is required.")
    account_key, options = await _resolve_target(db, store, payload)
    order = await _load_order(db, store, int(order_id))
    try:
        r = await _create_one(db, store, order, account_key, options)
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    pickup = None
    if payload.get("request_pickup", True):
        pickup = await _request_pickup(db, store, account_key, r["awb"], options)
    return {"success": True, **r, "pickup": pickup}


@router.post("/awb/bulk")
async def bulk_create(
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Create AWBs for many selected orders. Body: {order_ids:[...], profile_id? | account_key, options?}.
    Commits per order so one failure doesn't drop the rest; returns per-order results."""
    order_ids = payload.get("order_ids") or []
    if not order_ids:
        raise HTTPException(400, "order_ids is required.")
    account_key, options = await _resolve_target(db, store, payload)

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


class _OrderView:
    """Read-only proxy over a real Order that overrides line_items / name / total_price for a
    single location group and delegates every other attribute to the underlying order. The
    courier adapters read the order purely via `getattr`, so this is a safe stand-in — no DB
    access, no lazy-load — letting one AWB carry only its location's items."""

    def __init__(self, order: models.Order, line_items, name, total_price):
        object.__setattr__(self, "_order", order)
        object.__setattr__(self, "line_items", line_items)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "total_price", total_price)

    def __getattr__(self, item):
        return getattr(object.__getattribute__(self, "_order"), item)


@router.post("/awb/split")
async def split_create(
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Create one AWB PER fulfillment location for an order whose items ship from 2+ locations.
    Each Shopify fulfillment order (= one location) becomes its own AWB carrying only that
    location's items, and that specific fulfillment order is fulfilled with the AWB's tracking.
    Body: {order_id, profile_id? | account_key, options?, request_pickup?}. For a single-location
    order this simply makes one AWB (still fulfilling the right FO)."""
    order_id = payload.get("order_id")
    if not order_id:
        raise HTTPException(400, "order_id is required.")
    account_key, options = await _resolve_target(db, store, payload)

    order = await _load_order(db, store, int(order_id))
    if not order.shopify_order_id:
        raise HTTPException(400, "This order has no shopify_order_id (not synced from Shopify).")
    # Split makes labels directly (not via _create_one) — enforce the plan cap here too.
    await shopify_billing.assert_label_quota(db, store)
    owner = order.store or store  # the order's own store — its Shopify token + branding
    acct = await _get_account(db, store, account_key)
    svc = get_courier_service(acct.courier_type or account_key)
    if not svc:
        raise HTTPException(400, f"Unsupported courier: {acct.courier_type or account_key}")

    try:
        groups = await shopify_service.get_fulfillment_order_groups(owner, order.shopify_order_id)
    except Exception as e:
        raise HTTPException(400, f"Couldn't read the fulfillment locations from Shopify: {e}")
    open_groups = [g for g in groups if g.get("open")]
    if not open_groups:
        raise HTTPException(400, "Nothing to ship — no location has items left to fulfill.")

    from services.status_sync_service import _tracking_url  # local import avoids any import cycle

    created: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    total_cod = _cod_for(order)
    courier_name = (acct.courier_type or account_key).upper()

    for idx, g in enumerate(open_groups):
        loc = g.get("location") or f"loc{idx + 1}"
        # Re-check the cap PER LABEL. A split creates one AWB per fulfillment location, so the single
        # pre-loop check let a shop at 149/150 create 6 more and land at 155/150. Checked OUTSIDE the
        # try so hitting the cap stops the split once, instead of logging the same 402 against every
        # remaining location.
        try:
            await shopify_billing.assert_label_quota(db, store)
        except HTTPException as he:
            errors.append({"location": loc, "error": he.detail})
            break
        try:
            subset = [SimpleNamespace(sku=it.get("sku"), title=it.get("name"), quantity=it.get("qty"))
                      for it in g.get("items", [])]
            view = _OrderView(order, subset, f"{order.name} · {loc}", order.total_price)
            opts = dict(options)
            # COD is collected ONCE — on the first parcel only (never double-charge the customer).
            opts["cod_amount"] = total_cod if idx == 0 else 0.0
            opts.setdefault("parcels_count", 1)
            opts.setdefault("total_weight", 1.0)
            if getattr(owner, "sender_name", None) and not opts.get("sender_name"):
                opts["sender_name"] = owner.sender_name
            if not opts.get("content"):
                from services.utils import render_content
                tmpl = opts.pop("content_template", None) or getattr(owner, "content_template", None)
                opts["content"] = render_content(tmpl, view)
            else:
                opts.pop("content_template", None)

            res = await svc.create_awb(db=db, order=view, account_key=account_key, options=opts)
            awb = res.get("awb") if isinstance(res, dict) else None
            if not awb:
                raise HTTPException(400, f"The response contained no AWB ({res})")

            csd = dict((res.get("raw") if isinstance(res, dict) else None) or {})
            csd["location"] = loc
            csd["fulfillment_order_id"] = g["fo_id"]
            ship = models.Shipment(
                order_id=order.id, courier=courier_name, account_key=account_key,
                awb=str(awb), courier_specific_data=csd, paper_size=(owner.paper_size or "A6"),
            )
            # Fulfill just THIS location's fulfillment order with its AWB as tracking — doar dacă
            # platforma curierului NU o face ea (xConnector/Frisbo fulfill-uiesc singure → dublu).
            try:
                if getattr(svc, "owns_shopify_fulfillment", False):
                    raise StopIteration
                gid = await shopify_service.create_fulfillment_for_fo(
                    owner, g["fo_id"], tracking_number=str(awb),
                    tracking_company=courier_name, tracking_url=_tracking_url(courier_name, str(awb)),
                    notify_customer=bool(getattr(owner, "fulfill_notify_customer", False)),
                )
                if gid:
                    ship.shopify_fulfillment_id = str(gid).split("/")[-1]
            except StopIteration:
                logger.info("split fulfill FO %s: %s fulfill-uiește singur în Shopify — nu împing",
                            g["fo_id"], courier_name)
            except Exception as fe:
                logger.info("split fulfill FO %s failed: %s", g["fo_id"], fe)

            db.add(ship)
            if not order.assigned_courier:
                order.assigned_courier = acct.courier_type or account_key
            order.processing_status = "Procesată"
            await db.commit()
            created.append({"location": loc, "awb": str(awb), "fulfillment_order_id": g["fo_id"]})
        except HTTPException as he:
            await db.rollback()
            errors.append({"location": loc, "error": he.detail})
        except Exception as e:
            await db.rollback()
            errors.append({"location": loc, "error": str(e)})

    pickup = None
    if created and payload.get("request_pickup", True):
        pickup = await _request_pickup(db, store, account_key, [c["awb"] for c in created], options)
    return {"success": bool(created), "split": len(open_groups) > 1,
            "locations": len(open_groups), "created": created, "errors": errors, "pickup": pickup}


async def _load_shipment(db: AsyncSession, store: models.Store, shipment_id: int) -> models.Shipment:
    allowed = await org_service.org_store_ids(db, store)
    ship = (await db.execute(
        select(models.Shipment)
        .join(models.Order, models.Order.id == models.Shipment.order_id)
        .options(selectinload(models.Shipment.order).selectinload(models.Order.store))
        .where(models.Shipment.id == shipment_id, models.Order.store_id.in_(allowed))
    )).scalar_one_or_none()
    if not ship:
        raise HTTPException(404, "Shipment not found.")
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
        raise HTTPException(400, "shipment_id is required.")
    ship = await _load_shipment(db, store, int(shipment_id))
    awb = ship.awb
    svc = get_courier_service(ship.courier or ship.account_key or "")
    if not svc:
        raise HTTPException(400, "This courier doesn't support cancelling.")
    try:
        v = await svc.void_awb(db, ship.awb, ship.account_key)
    except NotImplementedError:
        raise HTTPException(400, f"Cancelling isn't supported for {ship.courier}.")
    if not v.success:
        raise HTTPException(400, f"Cancellation failed: {v.message}")
    await db.delete(ship)
    await db.commit()
    return {"success": True, "voided_awb": awb}


@router.post("/awb/regen")
async def regen_awb(
    payload: Dict[str, Any] = Body(...),
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Regenerate an AWB in one action: cancel the existing label, then create a fresh one (optionally
    a different parcel count). Body: {shipment_id, parcels?, account_key?}. Same courier by default —
    regen = the xConnector/DPD 'void + create' pair, kept atomic so the order is never left label-less."""
    shipment_id = payload.get("shipment_id")
    if not shipment_id:
        raise HTTPException(400, "shipment_id is required.")
    ship = await _load_shipment(db, store, int(shipment_id))
    order_id, old_awb = ship.order_id, ship.awb
    account_key = payload.get("account_key") or ship.account_key
    svc = get_courier_service(ship.courier or ship.account_key or account_key or "")
    if not svc:
        raise HTTPException(400, "This courier doesn't support regenerating.")
    try:
        v = await svc.void_awb(db, ship.awb, ship.account_key)
    except NotImplementedError:
        raise HTTPException(400, f"Cancelling isn't supported for {ship.courier}.")
    if not v.success:
        raise HTTPException(400, f"Couldn't cancel the old AWB: {v.message}")
    await db.delete(ship)
    await db.flush()                       # drop the old shipment BEFORE reloading, so create sees no AWB
    order = await _load_order(db, store, order_id)
    opts: Dict[str, Any] = {}
    if payload.get("parcels"):
        opts["parcels"] = int(payload["parcels"])
    try:
        r = await _create_one(db, store, order, account_key, opts)
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    return {"success": True, "voided_awb": old_awb, **r}


@router.get("/awb/label")
async def get_label(
    shipment_id: int,
    size: Optional[str] = None,
    store: models.Store = Depends(require_shop),
    db: AsyncSession = Depends(get_db),
):
    """Return the courier label PDF for a shipment (inline). `size` (A4/A6) overrides the
    shipment default where the courier renders the label at print time."""
    ship = await _load_shipment(db, store, shipment_id)
    acct = await _get_account(db, store, ship.account_key)
    svc = get_courier_service(ship.courier or ship.account_key or "")
    if not svc:
        raise HTTPException(400, "This courier doesn't support labels.")
    # Merge courier-specific label handles saved at create time (GLS label_b64, Econt pdf_url,
    # Packeta packet_id, GLS parcel_id) into the creds passed to the adapter.
    creds = dict(acct.credentials or {})
    csd = ship.courier_specific_data or {}
    for k in ("label_b64", "pdf_url", "packet_id", "parcel_id"):
        if isinstance(csd, dict) and csd.get(k):
            creds[k] = csd[k]
    paper = (size or ship.paper_size or "A6")
    try:
        pdf = await svc.get_label(ship.awb, creds, paper)
    except NotImplementedError:
        raise HTTPException(400, f"Labels aren't supported for {ship.courier}.")
    except Exception as e:
        raise HTTPException(400, f"Label error: {e}")
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{ship.awb}.pdf"'},
    )
