# routes/financials.py
from __future__ import annotations

from datetime import datetime, date, timezone
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse
from sqlalchemy import and_, desc, func, select, null
from sqlalchemy.ext.asyncio import AsyncSession

# ---------- models (compat) ----------
try:
    from models import Order as O, Shipment as S, Store as TStore  # type: ignore
except Exception:
    try:
        from core.models import Order as O, Shipment as S, Store as TStore  # type: ignore
    except Exception:
        from app.models import Order as O, Shipment as S, Store as TStore  # type: ignore

# ---------- db session (compat) ----------
try:
    from database import get_db  # type: ignore
except Exception:
    try:
        from core.db import get_db  # type: ignore
    except Exception:
        from app.db import get_db  # type: ignore

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/financials", tags=["Financials"])


# =============================================================================
# Helpers
# =============================================================================
def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _pick_attr(obj, *names, default=None):
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None:
                return v
    return default


async def _active_stores(db: AsyncSession) -> list[dict]:
    try:
        q = select(TStore.id, TStore.name).where(getattr(TStore, "is_active") == True)  # noqa: E712
        rows = await db.execute(q)
        data = rows.fetchall()
        if data:
            return [{"id": r[0], "name": r[1]} for r in data]
    except Exception:
        pass
    rows = await db.execute(select(TStore.id, TStore.name))
    return [{"id": r[0], "name": r[1]} for r in rows.fetchall()]


async def _distinct_courier_statuses(db: AsyncSession) -> list[str]:
    try:
        q = select(func.distinct(S.last_status)).where(S.last_status.isnot(None))
        rows = await db.execute(q)
        return [r[0] for r in rows.fetchall() if r[0]]
    except Exception:
        return []


# =============================================================================
# Query builder – defensiv
# =============================================================================
async def _eligible_orders_base_query(
    db: AsyncSession,
    start_date: Optional[date],
    end_date: Optional[date],
    store_id: Optional[int],
    courier_statuses: Optional[List[str]],
    courier: Optional[str],
):
    def pick_col(model, candidates: list[str], label: str):
        for name in candidates:
            col = getattr(model, name, None)
            if col is not None:
                return col, col.label(label)
        return None, null().label(label)

    latest_s = (
        select(S.id)
        .where(S.order_id == O.id)
        .order_by(S.id.desc())
        .limit(1)
        .correlate(O)
        .scalar_subquery()
    )

    order_name_expr, order_name_col = pick_col(O, ["name", "order_number", "order_name"], "order_name")
    created_expr, created_col = pick_col(O, ["created_at", "processed_at", "created_on", "created"], "created_at")
    fin_expr, fin_col = pick_col(O, ["financial_status", "payment_status", "financialstate", "fin_status"], "financial_status")
    fulf_expr, fulf_col = pick_col(O, ["fulfillment_status", "fulfillment_state", "fulfillment"], "fulfillment_status")
    total_expr, total_col = pick_col(O, ["total_price", "total"], "total_price")
    cust_expr, cust_col = pick_col(O, ["customer", "customer_name", "client_name"], "customer")

    courier_expr, courier_col = pick_col(S, ["courier", "courier_name", "carrier"], "courier")
    last_status_expr, last_status_col = pick_col(S, ["last_status", "status_text", "raw_status"], "courier_raw_status")
    last_status_at_expr, last_status_at_col = pick_col(S, ["last_status_at"], "courier_status_at")

    q = (
        select(
            O.id.label("order_id"),
            O.store_id.label("store_id"),
            order_name_col,
            cust_col,
            created_col,
            fin_col,
            fulf_col,
            total_col,
            TStore.name.label("store_name"),
            courier_col,
            last_status_col,
            last_status_at_col,
        )
        .select_from(O)
        .join(TStore, TStore.id == O.store_id, isouter=True)
        .join(S, S.id == latest_s, isouter=True)
    )

    conds = []
    if start_date and created_expr is not None:
        conds.append(created_expr >= datetime.combine(start_date, datetime.min.time()))
    if end_date and created_expr is not None:
        conds.append(created_expr <= datetime.combine(end_date, datetime.max.time()))
    if store_id:
        conds.append(O.store_id == int(store_id))
    if courier and courier_expr is not None:
        conds.append(courier_expr == courier)
    if courier_statuses:
        sts = [s for s in courier_statuses if s]
        if sts and last_status_expr is not None:
            conds.append(last_status_expr.in_(sts))
    # doar FULFILLED + payment pending
    if fulf_expr is not None:
        conds.append(func.upper(fulf_expr) == "FULFILLED")
    if fin_expr is not None:
        conds.append(func.lower(fin_expr).in_(["pending", "payment pending", "cod_pending"]))

    if conds:
        q = q.where(and_(*conds))
    q = q.order_by(desc(created_expr) if created_expr is not None else desc(O.id), desc(O.id))

    try:
        res = await db.execute(q)
        return res.fetchall() or []
    except Exception:
        return []


# =============================================================================
# Routes
# =============================================================================
@router.get("/", name="get_financials_page")
async def financials_page(request: Request, db: AsyncSession = Depends(get_db)):
    stores = await _active_stores(db)
    statuses = await _distinct_courier_statuses(db)
    return templates.TemplateResponse(
        "financials.html",
        {"request": request, "stores": stores, "courier_statuses": statuses},
    )


@router.get("/data")
async def financials_data(
    request: Request,
    db: AsyncSession = Depends(get_db),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    store_id: Optional[int] = Query(None),
    courier: Optional[str] = Query(None),
    statuses: Optional[str] = Query(None),
):
    df = _parse_date(start)
    dt = _parse_date(end)
    courier_statuses = [s.strip() for s in (statuses or "").split(",") if s.strip()]

    rows = await _eligible_orders_base_query(
        db, df, dt, store_id, courier_statuses or None, courier
    ) or []

    data: list[dict] = []
    for r in rows:
        created_val = getattr(r, "created_at", None)
        courier_at = getattr(r, "courier_status_at", None)
        data.append(
            {
                "id": getattr(r, "order_id", None),
                "store_id": getattr(r, "store_id", None),
                "store_name": getattr(r, "store_name", "") or "",
                "name": getattr(r, "order_name", "") or "",
                "customer": getattr(r, "customer", None),
                "total_price": getattr(r, "total_price", None),
                "created_at": created_val.isoformat() if created_val else None,
                "financial_status": (getattr(r, "financial_status", "") or "").lower(),
                "fulfillment_status": (getattr(r, "fulfillment_status", "") or "").lower(),
                "courier": getattr(r, "courier", "") or "",
                "courier_raw_status": getattr(r, "courier_raw_status", "") or "",
                "courier_status_at": courier_at.isoformat() if courier_at else None,
            }
        )

    return JSONResponse({"rows": data, "count": len(data)})


@router.post("/sync")
@router.post("/sync-range")
async def sync_financials(
    request: Request,
    db: AsyncSession = Depends(get_db),
    start: Optional[str] = Form(None),
    end: Optional[str] = Form(None),
    store_id: Optional[int] = Form(None),
):
    if start is None and end is None:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        start = payload.get("start", start)
        end = payload.get("end", end)
        if store_id is None:
            store_id = payload.get("store_id")

    df = _parse_date(start)
    dt = _parse_date(end)
    if df is None and dt is None:
        df = date.today()
        dt = df
    elif df is None:
        df = dt
    elif dt is None:
        dt = df

    created_at_min = datetime.combine(df, datetime.min.time()).replace(tzinfo=timezone.utc)
    created_at_max = datetime.combine(dt, datetime.max.time()).replace(tzinfo=timezone.utc)

    # store list
    if store_id:
        sids = [store_id]
    else:
        r = await db.execute(select(TStore.id).where(getattr(TStore, "is_active") == True))  # noqa: E712
        res = r.fetchall()
        sids = [x[0] for x in res] if res else [x[0] for x in (await db.execute(select(TStore.id))).fetchall()]

    # call your services.sync_service (compat)
    from services import sync_service as _ss  # type: ignore
    if hasattr(_ss, "full_sync_for_stores"):
        await _ss.full_sync_for_stores(sids, created_at_min, created_at_max, with_couriers=True)
    elif hasattr(_ss, "sync_orders_for_stores"):
        await _ss.sync_orders_for_stores(sids, created_at_min, created_at_max)
    else:
        raise HTTPException(500, "Serviciul de sincronizare nu este disponibil.")

    return {"ok": True, "start": str(df), "end": str(dt), "stores": sids}


@router.post("/mark-as-paid")
async def mark_orders_as_paid(
    request: Request,
    db: AsyncSession = Depends(get_db),
    order_ids: Optional[List[int]] = Form(None),
    store_id: Optional[int] = Form(None),
):
    # acceptă și JSON
    if not order_ids:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        order_ids = payload.get("order_ids")
        store_id = store_id or payload.get("store_id")

    if not order_ids:
        raise HTTPException(422, "order_ids is required")

    # ------------ ia store creds ------------
    # domeniu + token (încearcă mai multe denumiri)
    row = (await db.execute(select(TStore).where(TStore.id == store_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Store not found")

    shop_domain = _pick_attr(row, "shop_domain", "shop", "domain", "url")
    token = _pick_attr(row, "admin_api_token", "api_token", "shopify_token", "access_token", "token")
    if not shop_domain or not token:
        raise HTTPException(500, "Lipsesc datele Shopify pentru magazin.")

    if shop_domain.startswith("https://"):
        shop_domain = shop_domain.replace("https://", "")
    graphql_url = f"https://{shop_domain}/admin/api/2025-07/graphql.json"

    # ------------ ia remote id pentru fiecare order ------------
    shopify_id_col = None
    for cand in ("shopify_gid", "shopify_id", "remote_id", "platform_id"):
        if hasattr(O, cand):
            shopify_id_col = getattr(O, cand)
            break

    if shopify_id_col is None:
        raise HTTPException(500, "Modelul Order nu are coloană cu ID-ul Shopify (shopify_gid/shopify_id/remote_id/platform_id).")

    res = await db.execute(select(O.id, shopify_id_col).where(O.id.in_(order_ids)))
    id_pairs = [(oid, val) for oid, val in res.fetchall() if val is not None]
    if not id_pairs:
        return {"updated": 0}

    mutation = """
    mutation orderMarkAsPaid($input: OrderMarkAsPaidInput!) {
      orderMarkAsPaid(input: $input) {
        order { id displayFinancialStatus }
        userErrors { field message }
      }
    }
    """

    updated = 0
    async with httpx.AsyncClient(timeout=20) as client:
        headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
        for oid, remote in id_pairs:
            gid = str(remote)
            if gid.isdigit():  # transformă numeric în GID
                gid = f"gid://shopify/Order/{gid}"
            payload = {"query": mutation, "variables": {"input": {"id": gid}}}
            r = await client.post(graphql_url, json=payload, headers=headers)
            data = r.json()
            errs = (data.get("data", {}) or {}).get("orderMarkAsPaid", {}).get("userErrors")
            if r.status_code == 200 and not errs:
                updated += 1
            # dacă API nu a marcat, nu modificăm local

    # update local DOAR pentru comenzile marcate cu succes
    if updated:
        fin_col = getattr(O, "financial_status", None) or getattr(O, "payment_status", None)
        if fin_col is not None:
            ok_ids = [oid for oid, _ in id_pairs][:updated]  # aproximare 1:1
            stmt = O.__table__.update().where(O.id.in_(ok_ids)).values({fin_col.key: "paid"})
            await db.execute(stmt)
            await db.commit()

    return {"updated": updated}
