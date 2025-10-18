# services/filter_service.py
import logging
from typing import Dict, Any, Tuple, List
from sqlalchemy import select, func, or_, and_, text, Table, Column, Integer, String
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.ext.asyncio import AsyncSession

import models

# view minim: id + mapped_courier_status
orders_view = Table(
    "orders_view",
    models.Base.metadata,
    Column("id", Integer, primary_key=True),
    Column("mapped_courier_status", String),
    extend_existing=True,
)

# ---------- HELPERS ----------

def _apply_basic_filters(qp: Any) -> Dict[str, Any]:
    """Extrage filtrele din query_params într-un dict curat."""
    f = {
        "store": qp.get("store"),  # domeniu store sau 'all'
        "category": qp.get("category"),
        "courier": qp.get("courier"),
        "derived_status": qp.get("derived_status"),
        "address_status": qp.get("address_status"),
        "financial_status": qp.get("financial_status"),
        "fulfillment_status": qp.get("fulfillment_status"),
        "printed_status": qp.get("printed_status"),
        "order_q": (qp.get("order_q") or "").strip(),
    }
    return f


def _where_sql_and_params(filters: Dict[str, Any], exclude: set[str]):
    clauses, params = [], {}

    # store (stores.domain)
    if filters.get("store") and filters["store"] != "all" and "store" not in exclude:
        clauses.append("st.domain = :store")
        params["store"] = filters["store"]

    # category (store_category_map.category_id)
    if filters.get("category") and filters["category"] != "all" and "category" not in exclude:
        clauses.append("scm.category_id = :category_id")
        params["category_id"] = filters["category"]

    # derived status (din orders_view)
    if filters.get("derived_status") and filters["derived_status"] != "all" and "derived_status" not in exclude:
        clauses.append("ov.mapped_courier_status = :derived_status")
        params["derived_status"] = filters["derived_status"]

    # courier (ultimul shipment)
    if filters.get("courier") and filters["courier"] != "all" and "courier" not in exclude:
        clauses.append("sh.courier = :courier")
        params["courier"] = filters["courier"]

    # address_status
    if filters.get("address_status") and filters["address_status"] != "all" and "address_status" not in exclude:
        clauses.append("o.address_status = :address_status")
        params["address_status"] = filters["address_status"]

    # financial_status
    if filters.get("financial_status") and filters["financial_status"] != "all" and "financial_status" not in exclude:
        clauses.append("o.financial_status = :financial_status")
        params["financial_status"] = filters["financial_status"]

    # fulfillment_status (shopify_status)
    if filters.get("fulfillment_status") and filters["fulfillment_status"] != "all" and "fulfillment_status" not in exclude:
        clauses.append("o.shopify_status = :shopify_status")
        params["shopify_status"] = filters["fulfillment_status"]

    # printed_status (fara_awb / neprintat / printed)
    if filters.get("printed_status") and filters["printed_status"] != "all" and "printed_status" not in exclude:
        ps = filters["printed_status"]
        if ps == "fara_awb":
            clauses.append("sh.awb IS NULL")
        elif ps in ("neprintat", "not_printed"):
            clauses.append("sh.awb IS NOT NULL AND sh.printed_at IS NULL")
        elif ps == "printed":
            clauses.append("sh.printed_at IS NOT NULL")

    # q (name / customer / shipping_phone / awb)
    if filters.get("order_q"):
        clauses.append("(o.name ILIKE :q OR o.customer ILIKE :q OR o.shipping_phone ILIKE :q OR sh.awb ILIKE :q)")
        params["q"] = f"%{filters['order_q']}%"

    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where_sql, params


BASE_FROM = """
FROM public.orders o
LEFT JOIN public.orders_view ov ON ov.id = o.id
JOIN public.stores st ON st.id = o.store_id
LEFT JOIN public.store_category_map scm ON scm.store_id = st.id
LEFT JOIN LATERAL (
  SELECT s.courier, s.awb, s.printed_at
  FROM public.shipments s
  WHERE s.order_id = o.id
  ORDER BY s.last_status_at DESC NULLS LAST, s.id DESC
  LIMIT 1
) sh ON TRUE
"""


# ---------- LIST + COUNTS ----------

async def get_filtered_orders(db: AsyncSession, query_params: Any) -> Tuple[List[models.Order], int, Dict[str, Any]]:
    filters = _apply_basic_filters(query_params)

    # ---- core ORM query pentru listă + total ----
    query = (
        select(models.Order, orders_view.c.mapped_courier_status)
        .options(
            joinedload(models.Order.store),
            selectinload(models.Order.line_items),
            joinedload(models.Order.shipments),
        )
        .outerjoin(orders_view, models.Order.id == orders_view.c.id)
    )

    orm_filters = []

    if filters["order_q"]:
        q = f"%{filters['order_q'].lower()}%"
        orm_filters.append(or_(
            func.lower(models.Order.name).like(q),
            func.lower(models.Order.customer).like(q),
            func.lower(models.Order.shipping_phone).like(q),
            models.Order.shipments.any(func.lower(models.Shipment.awb).like(q)),
        ))

    if filters.get("store") and filters["store"] != "all":
        # store prin domeniu
        query = query.join(models.Order.store)
        orm_filters.append(models.Store.domain == filters["store"])

    if filters.get("category") and filters["category"] != "all":
        query = query.join(models.Order.store).join(models.Store.categories)
        orm_filters.append(models.StoreCategory.id == int(filters["category"]))

    if filters.get("courier") and filters["courier"] != "all":
        orm_filters.append(models.Order.shipments.any(models.Shipment.courier == filters["courier"]))

    if filters.get("derived_status") and filters["derived_status"] != "all":
        orm_filters.append(orders_view.c.mapped_courier_status == filters["derived_status"])

    if filters.get("address_status") and filters["address_status"] != "all":
        orm_filters.append(models.Order.address_status == filters["address_status"])

    if filters.get("financial_status") and filters["financial_status"] != "all":
        orm_filters.append(models.Order.financial_status == filters["financial_status"])

    if filters.get("fulfillment_status") and filters["fulfillment_status"] != "all":
        orm_filters.append(models.Order.shopify_status == filters["fulfillment_status"])

    if filters.get("printed_status") and filters["printed_status"] != "all":
        ps = filters["printed_status"]
        if ps == "fara_awb":
            orm_filters.append(~models.Order.shipments.any())  # fără niciun shipment/awb
        elif ps in ("neprintat", "not_printed"):
            orm_filters.append(models.Order.shipments.any(and_(
                models.Shipment.awb.isnot(None),
                models.Shipment.printed_at.is_(None),
            )))
        elif ps == "printed":
            orm_filters.append(models.Order.shipments.any(models.Shipment.printed_at.isnot(None)))

    if orm_filters:
        query = query.where(and_(*orm_filters))

    total_orders = await db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))

    # sortare
    sort_by = query_params.get("sort_by", "created_at_desc")
    sort_map = {
        "created_at_desc": models.Order.created_at.desc(),
        "created_at_asc": models.Order.created_at.asc(),
        "order_name_desc": models.Order.name.desc(),
        "order_name_asc": models.Order.name.asc(),
    }
    if sort_by in sort_map:
        query = query.order_by(sort_map[sort_by])

    # paginare
    page = int(query_params.get("page", 1))
    page_size = 50
    query = query.limit(page_size).offset((page - 1) * page_size)

    result = await db.execute(query)
    rows = result.unique().mappings().all()

    orders: List[models.Order] = []
    for row in rows:
        o = row["Order"]
        o.mapped_courier_status = row["mapped_courier_status"]
        orders.append(o)

    # ---- COUNT-uri pentru dropdown-uri (RAW SQL, corect pe LEFT JOIN) ----
    where_all, params_all = _where_sql_and_params(filters, exclude=set())

    # total curent (Toate)
    total_sql = f"SELECT COUNT(DISTINCT o.id) AS cnt {BASE_FROM} {where_all}"
    total = (await db.execute(text(total_sql), params_all)).scalar() or 0

    counts: Dict[str, Dict[str, int]] = {}

    # Store
    w, p = _where_sql_and_params(filters, exclude={"store"})
    sql = f"""
      SELECT st.domain AS key, COUNT(DISTINCT o.id) AS cnt
      {BASE_FROM} {w}
      GROUP BY st.domain
      ORDER BY cnt DESC
    """
    rows = (await db.execute(text(sql), p)).all()
    counts["store"] = {"all": total} | {r.key: r.cnt for r in rows}

    # Category
    w, p = _where_sql_and_params(filters, exclude={"category"})
    sql = f"""
      SELECT COALESCE(scm.category_id::text,'none') AS key, COUNT(DISTINCT o.id) AS cnt
      {BASE_FROM} {w}
      GROUP BY COALESCE(scm.category_id::text,'none')
      ORDER BY cnt DESC
    """
    rows = (await db.execute(text(sql), p)).all()
    counts["category"] = {"all": total} | {r.key: r.cnt for r in rows}

    # Derived status
    w, p = _where_sql_and_params(filters, exclude={"derived_status"})
    sql = f"""
      SELECT COALESCE(ov.mapped_courier_status,'__null__') AS key, COUNT(DISTINCT o.id) AS cnt
      {BASE_FROM} {w}
      GROUP BY COALESCE(ov.mapped_courier_status,'__null__')
      ORDER BY cnt DESC
    """
    rows = (await db.execute(text(sql), p)).all()
    counts["derived_status"] = {"all": total} | {r.key: r.cnt for r in rows}

    # Courier
    w, p = _where_sql_and_params(filters, exclude={"courier"})
    sql = f"""
      SELECT COALESCE(sh.courier,'__null__') AS key, COUNT(DISTINCT o.id) AS cnt
      {BASE_FROM} {w}
      GROUP BY COALESCE(sh.courier,'__null__')
      ORDER BY cnt DESC
    """
    rows = (await db.execute(text(sql), p)).all()
    counts["courier"] = {"all": total} | {r.key: r.cnt for r in rows}

    # Payment
    w, p = _where_sql_and_params(filters, exclude={"financial_status"})
    sql = f"""
      SELECT COALESCE(o.financial_status,'__null__') AS key, COUNT(DISTINCT o.id) AS cnt
      {BASE_FROM} {w}
      GROUP BY COALESCE(o.financial_status,'__null__')
      ORDER BY cnt DESC
    """
    rows = (await db.execute(text(sql), p)).all()
    counts["financial_status"] = {"all": total} | {r.key: r.cnt for r in rows}

    # Fulfillment
    w, p = _where_sql_and_params(filters, exclude={"fulfillment_status"})
    sql = f"""
      SELECT COALESCE(o.shopify_status,'__null__') AS key, COUNT(DISTINCT o.id) AS cnt
      {BASE_FROM} {w}
      GROUP BY COALESCE(o.shopify_status,'__null__')
      ORDER BY cnt DESC
    """
    rows = (await db.execute(text(sql), p)).all()
    counts["fulfillment_status"] = {"all": total} | {r.key: r.cnt for r in rows}

    # Printed
    w, p = _where_sql_and_params(filters, exclude={"printed_status"})
    sql = f"""
      SELECT key, COUNT(*) AS cnt FROM (
        SELECT DISTINCT o.id,
          CASE
            WHEN sh.awb IS NULL THEN 'fara_awb'
            WHEN sh.printed_at IS NULL THEN 'neprintat'
            ELSE 'printed'
          END AS key
        {BASE_FROM} {w}
      ) t
      GROUP BY key
      ORDER BY cnt DESC
    """
    rows = (await db.execute(text(sql), p)).all()
    counts["printed_status"] = {"all": total} | {r.key: r.cnt for r in rows}

    return orders, total_orders, counts
