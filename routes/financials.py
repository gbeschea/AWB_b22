# routes/financials.py
from __future__ import annotations

from datetime import datetime, date
from typing import List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Form
from fastapi.responses import JSONResponse
from sqlalchemy import (
    and_,
    desc,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession




# --- Models resolver (în funcție de unde sunt declarate) ---
try:
    from models import Order as O, Shipment as S, Store as TStore
except Exception:
    from core.models import Order as O, Shipment as S, Store as TStore


# --- DB dependency resolver (evită ModuleNotFoundError) ---
try:
    # cele mai frecvente denumiri din proiecte
    from database import get_db  # <- încearcă mai întâi "database"
except Exception:
    try:
        from core.db import get_db  # <- sau "core.db"
    except Exception:
        from db import get_db       # <- fallback "db"

try:
    from core.templates import templates  # type: ignore
except Exception:  # pragma: no cover
    from fastapi.templating import Jinja2Templates

    templates = Jinja2Templates(directory="templates")

from models import Order as O, Shipment as S, Store as TStore  # type: ignore

# servicii auxiliare (nume stabil; dacă diferă în proiectul tău, adaptează importurile)
try:
    from services import sync_service, shopify_service  # type: ignore
except Exception:
    sync_service = None  # type: ignore
    shopify_service = None  # type: ignore

router = APIRouter(prefix="/financials", tags=["Financials"])


# ---------- helpers ----------


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    # suportăm formate tipice: YYYY-MM-DD sau DD.MM.YYYY
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


async def _latest_shipment_join() -> Tuple:
    """
    Creează expresia necesară pentru a uni comanda cu **ultimul** shipment
    (după updated_at, apoi id).
    Returnează (join_condition_subquery, alias S).
    """
    # va fi folosit ca scalar_subquery în condiția de join
    # SELECT id FROM shipments WHERE order_id = O.id ORDER BY updated_at DESC, id DESC LIMIT 1
    return


async def _eligible_orders_base_query(
    db: AsyncSession,
    start_date: Optional[date],
    end_date: Optional[date],
    store_id: Optional[int],
    courier_statuses: Optional[List[str]],
    courier: Optional[str],
):
    """
    Construiește un SELECT care întoarce **o singură linie pe comandă**,
    unită cu **ultimul shipment** (dacă există).
    Filtre:
      - doar comenzi cu cel puțin un shipment (=> Fulfilled)
      - și cu financial_status 'pending' (case-insensitive)
      - opțional magazin, interval de date, curier, status curier (multi)
    """

    # subquery: ultimul shipment per comandă
    latest_shp_id = (
        select(S.id)
        .where(S.order_id == O.id)
        .order_by(desc(S.updated_at), desc(S.id))
        .limit(1)
        .scalar_subquery()
    )

    # coloane returnate
    cols = [
        O.id.label("order_id"),
        O.name.label("order_name"),
        O.customer_name.label("customer_name"),
        O.created_at.label("created_at"),
        O.financial_status.label("financial_status"),
        O.store_id.label("store_id"),
        func.coalesce(S.courier, "").label("courier"),
        func.coalesce(S.last_status, "Unknown").label("courier_raw_status"),
        func.case((S.id.isnot(None), "FULFILLED"), else_="UNFULFILLED").label(
            "fulfillment_status"
        ),
    ]

    q = (
        select(*cols)
        .join(S, S.id == latest_shp_id, isouter=True)
        # Fulfilled + Payment Pending
        .where(
            S.id.isnot(None),  # are cel puțin un shipment
            func.lower(O.financial_status) == "pending",
        )
    )

    if start_date:
        q = q.where(O.created_at >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        q = q.where(O.created_at <= datetime.combine(end_date, datetime.max.time()))
    if store_id:
        q = q.where(O.store_id == store_id)
    if courier:
        q = q.where(func.lower(S.courier) == courier.lower())
    if courier_statuses:
        # status-urile pot veni cu spații; curățăm
        cleaned = [s.strip() for s in courier_statuses if s and s.strip()]
        if cleaned:
            q = q.where(S.last_status.in_(cleaned))

    # pentru siguranță (unele DB pot dubla la joinuri exotice)
    q = q.group_by(
        O.id,
        O.name,
        O.customer_name,
        O.created_at,
        O.financial_status,
        O.store_id,
        S.id,
        S.courier,
        S.last_status,
    ).order_by(desc(O.created_at))

    rows = (await db.execute(q)).all()
    return rows


async def _distinct_courier_statuses(db):
    """
    Returnează statusurile de curier (case-insensitive), sortate alfabetic.
    Evită eroarea 'for SELECT DISTINCT, ORDER BY expressions must appear in select list'
    folosind GROUP BY pe lower(last_status).
    """
    status_lower = func.lower(S.last_status)

    q = (
        select(func.min(S.last_status).label("status"))  # un reprezentant per grup
        .where(S.last_status.isnot(None))
        .group_by(status_lower)                          # unic pe lower(...)
        .order_by(status_lower)                          # sortare alfabetic, case-insensitive
    )

    rows = (await db.execute(q)).scalars().all()
    # curăță valori goale și pune "Unknown" la final, dacă există
    statuses = [s for s in rows if s and str(s).strip()]
    if "Unknown" in statuses:
        statuses = [s for s in statuses if s != "Unknown"] + ["Unknown"]
    return statuses


async def _active_stores(db: AsyncSession) -> List[Tuple[int, str]]:
    q = select(TStore.id, TStore.name).where(TStore.is_active.is_(True)).order_by(
        func.lower(TStore.name)
    )
    res = await db.execute(q)
    return [(r[0], r[1]) for r in res.fetchall()]


# ---------- routes ----------


@router.get("/financials", name="get_financials_page") 
async def financials_page(request: Request, db: AsyncSession = Depends(get_db)):
    stores = await _active_stores(db)
    statuses = await _distinct_courier_statuses(db)
    return templates.TemplateResponse(
        "financials.html",
        {
            "request": request,
            "stores": stores,
            "courier_statuses": statuses,
        },
    )


@router.get("/data")
async def financials_data(
    request: Request,
    db: AsyncSession = Depends(get_db),
    start: Optional[str] = Query(None, description="YYYY-MM-DD sau dd.mm.yyyy"),
    end: Optional[str] = Query(None, description="YYYY-MM-DD sau dd.mm.yyyy"),
    store_id: Optional[int] = Query(None),
    courier: Optional[str] = Query(None),
    courier_status: Optional[List[str]] = Query(None),
):
    df = _parse_date(start)
    dt = _parse_date(end)

    rows = await _eligible_orders_base_query(
        db, df, dt, store_id, courier_status, courier
    )

    # aranjăm payloadul pentru tabel
    data = []
    for r in rows:
        data.append(
            {
                "id": r.order_id,
                "order_name": r.order_name,
                "customer_name": r.customer_name,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "financial_status": r.financial_status or "",
                "fulfillment_status": r.fulfillment_status,
                "courier": r.courier or "",
                "courier_raw_status": r.courier_raw_status or "Unknown",
            }
        )
    return JSONResponse({"items": data, "count": len(data)})


@router.post("/sync")
async def sync_financials(
    request: Request,
    db: AsyncSession = Depends(get_db),
    start: Optional[str] = Form(None),
    end: Optional[str] = Form(None),
    store_id: Optional[int] = Form(None),
):
    if sync_service is None:
        raise HTTPException(
            status_code=500, detail="Serviciul de sincronizare nu este disponibil."
        )

    df = _parse_date(start)
    dt = _parse_date(end)

    # dacă store_id e None => toate magazinele active
    # apelul efectiv depinde de cum e structurat services/sync_service.py în proiectul tău
    # încercăm întâi variante sigure de nume; dacă nu există, ridicăm 500 clar
    if hasattr(sync_service, "full_sync_for_stores"):
        ok = await sync_service.full_sync_for_stores(db, df, dt, [store_id] if store_id else None)  # type: ignore
    elif hasattr(sync_service, "full_sync"):
        ok = await sync_service.full_sync(db, df, dt, [store_id] if store_id else None)  # type: ignore
    else:
        raise HTTPException(
            status_code=500,
            detail="Nu găsesc funcția de sincronizare în services.sync_service.",
        )

    return {"ok": bool(ok)}


@router.post("/mark-as-paid")
async def mark_orders_as_paid(
    request: Request,
    db: AsyncSession = Depends(get_db),
    order_ids: List[int] = Form(...),
):
    if not order_ids:
        return {"updated": 0}

    # preluăm comenzi + store_id, pentru a nu declanșa lazy-load (evităm MissingGreenlet)
    q = select(O.id, O.store_id, O.name).where(O.id.in_(order_ids))
    res = await db.execute(q)
    items = res.fetchall()
    if not items:
        return {"updated": 0}

    # grupăm pe store
    by_store: dict[int, List[int]] = {}
    for oid, sid, _ in items:
        if sid is None:
            continue
        by_store.setdefault(sid, []).append(oid)

    updated_total = 0

    if shopify_service is None:
        raise HTTPException(
            status_code=500, detail="Serviciul Shopify nu este disponibil."
        )

    # delegăm către serviciul Shopify al tău (adaptează dacă numele e diferit)
    if hasattr(shopify_service, "mark_orders_as_paid"):
        for sid, ids in by_store.items():  # type: ignore
            updated_total += await shopify_service.mark_orders_as_paid(  # type: ignore
                db, store_id=sid, order_ids=ids
            )
    elif hasattr(shopify_service, "set_orders_paid"):
        for sid, ids in by_store.items():  # type: ignore
            updated_total += await shopify_service.set_orders_paid(  # type: ignore
                db, store_id=sid, order_ids=ids
            )
    else:
        raise HTTPException(
            status_code=500,
            detail="Nu găsesc funcția de marcare ca plătit în services.shopify_service.",
        )

    return {"updated": updated_total}


@router.post("/clear")
async def clear_view(_: Request):
    """
    Nu ștergem nimic din DB. Doar întoarcem un flag pe care UI îl folosește
    ca să afișeze mesajul „View golit ... pornește o nouă sincronizare”.
    Persistența „stării de view gol” ține de UI (ex. localStorage).
    """
    return {"cleared": True}
