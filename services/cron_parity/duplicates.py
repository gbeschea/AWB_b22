"""
duplicates.py — detecția + REZOLUȚIA duplicatelor, paritate cu cronul (resolve_duplicate/
cancel_duplicate/customer_is_newest/awbprint_recent_dup din xconnector.py).

REGULILE (decise cu owner-ul, în producție):
 • candidat = ≥2 comenzi ale ACELUIAȘI client (telefon, altfel email — blind-index, fără decriptare)
   cu ACELAȘI set de SKU-uri, în fereastra magazinului (duplicate_window_hours, default 24h);
 • keeper = cea mai NOUĂ comandă; EXCEPȚIE: dacă cea veche are DEJA AWB/expediere → ea pleacă,
   cea nouă e dublura; comanda plătită cu CARDUL (financial=paid) nu se anulează NICIODATĂ;
 • ANULEZ doar dublura IDENTICĂ (aceleași SKU-uri ȘI aceeași sumă ±0.01) = dublură tehnică sigură;
   altă sumă/conținut = poate fi comandă REALĂ → HOLD la CS (CSQueueItem reason=duplicate);
 • PROTECȚIE LIVRARE (lecțiile ghost-AWB #549/#550/#557): NIMIC nu se anulează/HOLD-uiește dacă
   comanda A PLECAT — în OH sursa de adevăr = shipments-urile PROPRII (status canonic), nu un
   sistem terț cu lag.
În shadow: identice → log "would-cancel"; diferite → CSQueueItem (intern, sigur) + log "held".
"""
from __future__ import annotations
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import selectinload

import models
from services.settings import resolver

logger = logging.getLogger("cron_parity.duplicates")

# statusuri de shipment care înseamnă „a plecat / e la curier" — echivalentul PLECATA din cron
_SHIPPED = {"in_transit", "shipped", "pickup_office", "delivered", "refused", "out_for_delivery"}


def _skuset(order: Any) -> frozenset:
    return frozenset((li.sku or "").strip() for li in (order.line_items or []) if (li.sku or "").strip())


def _identity(order: Any, match: str) -> Optional[str]:
    """Cheia de identitate client, pe blind-index (fără decriptare PII)."""
    phone = getattr(order, "shipping_phone_bidx", None)
    email = getattr(order, "shipping_email_bidx", None)
    if match == "email":
        return email or phone
    return phone or email          # default: telefonul întâi (cronul compară pe client real)


def _has_shipped(order: Any) -> bool:
    if getattr(order, "fulfilled_at", None):
        return True
    for sh in (order.shipments or []):
        st = (getattr(sh, "last_status", None) or getattr(sh, "derived_status", "") or "").lower()
        # AWB creat (ne-anulat) = comanda pleacă / e la curier → protecție ghost-AWB (#549/#550/#557).
        # Shipment are `awb`/`last_status`/`derived_status` — NU `status`/`tracking_number` (nu le mai citim).
        if getattr(sh, "awb", None) and st not in ("canceled", "cancelled", "failed", "void", "voided"):
            return True
        if st in _SHIPPED:
            return True
    return False


def _is_paid(order: Any) -> bool:
    return (order.financial_status or "").lower() == "paid"


def resolve_group(orders: List[Any]) -> List[Tuple[Any, str, str]]:
    """Aplică arborele de decizie al cronului pe un grup de comenzi cu aceeași identitate+SKU-uri.
    Întoarce [(order, decizie, motiv)] pentru NON-keeperi: would-cancel | held | shipped-skip.
    Keeperul nu apare în rezultat."""
    live = [o for o in orders if not o.cancelled_at]
    if len(live) < 2:
        return []
    # keeper: comanda plătită cu cardul dacă există (garda „cardul nu se anulează niciodată"),
    # altfel una DEJA expediată (nu dublăm AWB-ul care pleacă), altfel cea mai NOUĂ (regula owner)
    paid = [o for o in live if _is_paid(o)]
    shipped = [o for o in live if _has_shipped(o)]
    if paid:
        keeper = paid[0]
    elif shipped:
        keeper = shipped[0]
    else:
        keeper = max(live, key=lambda o: o.created_at or datetime.min.replace(tzinfo=timezone.utc))
    out = []
    for o in live:
        if o.id == keeper.id:
            continue
        if _is_paid(o):
            continue                    # card → nu se atinge (poate fi și el keeper legitim; CS decide)
        if _has_shipped(o):
            out.append((o, "shipped-skip", "a plecat — protecție livrare (ghost-AWB #549/#550)"))
            continue
        same_total = (o.total_price is not None and keeper.total_price is not None
                      and abs(float(o.total_price) - float(keeper.total_price)) < 0.01)
        if same_total:
            out.append((o, "would-cancel", "identică cu %s (aceleași SKU-uri + aceeași sumă)" % keeper.name))
        else:
            out.append((o, "held", "dup-suma-diferita vs %s — poate fi comandă reală → CS" % keeper.name))
    return out


async def run_shadow(db, store: models.Store) -> Dict[str, int]:
    """Detectează + rezolvă în LOG-ONLY pe comenzile din fereastra magazinului. HOLD → CSQueueItem."""
    cfg = await resolver.resolve_capability(db, store, "duplicates")   # preset moștenit org→magazin
    if not cfg.get("enabled"):
        return {"skipped": "off"}
    hours = int(cfg.get("duplicate_window_hours") or 24)
    match = (cfg.get("duplicate_match") or "phone").lower()
    floor = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = (await db.execute(
        select(models.Order)
        .options(selectinload(models.Order.line_items), selectinload(models.Order.shipments))
        .where(models.Order.store_id == store.id,
               models.Order.created_at >= floor,
               models.Order.cancelled_at.is_(None))
    )).scalars().all()
    groups: Dict[Tuple[str, frozenset], List[Any]] = defaultdict(list)
    for o in rows:
        ident, skus = _identity(o, match), _skuset(o)
        if ident and skus:
            groups[(ident, skus)].append(o)
    stats = {"groups": 0, "would_cancel": 0, "held": 0, "shipped_skip": 0}
    for (_ident, _skus), grp in groups.items():
        decisions = resolve_group(grp)
        if not decisions:
            continue
        stats["groups"] += 1
        for o, decision, why in decisions:
            stats[decision.replace("-", "_")] += 1
            logger.info("DUP store=%s order=%s -> %s (%s)", store.id, o.name, decision, why)
            if decision == "held":
                # order_id e UNIC pe cs_queue_items (o intrare per comandă) → nu adaug peste una existentă
                exists = (await db.execute(
                    select(models.CSQueueItem.id).where(models.CSQueueItem.order_id == o.id)
                )).first()
                if not exists:
                    db.add(models.CSQueueItem(store_id=store.id, order_id=o.id, reason="duplicate",
                                              status="open", reason_detail=why, created_by="auto"))
    await db.commit()
    return stats
