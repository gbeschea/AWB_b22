# services/courier_service.py
# Sync curieri fără lazy-load. Rezolvă contul din profil/mapări.
# Grupează pe (courier_type, account_key) și face tracking.

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, Dict, List

from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

import models
from services.couriers import get_courier_service

logger = logging.getLogger(__name__)


# ---------------- Utils ----------------

def _norm(x: Optional[str]) -> Optional[str]:
    return x.strip().lower() if isinstance(x, str) else None


def _chunk(lst: List, size: int):
    for i in range(0, len(lst), size):
        yield lst[i: i + size]


# ---------------- Resolver cont/curier (fără lazy-load) ----------------

async def _resolve_account(
    db: AsyncSession,
    assigned_profile_id: Optional[int],
    assigned_courier: Optional[str],
) -> Optional[Tuple[str, str]]:
    """
    1) mapare pe numele de livrare din Shopify (assigned_courier) -> e cea mai specifică (alege contul corect DPD etc.)
    2) fallback: profilul asignat pe comandă
    """
    label = _norm(assigned_courier)
    if label:
        row = (await db.execute(text("""
            SELECT m.account_key, ca.courier_type
            FROM courier_mappings m
            JOIN courier_accounts ca ON ca.account_key = m.account_key
            WHERE LOWER(TRIM(m.shopify_name)) = :name
            ORDER BY m.id DESC
            LIMIT 1
        """), {"name": label})).first()
        if row:
            return row.account_key, row.courier_type

    if assigned_profile_id:
        row = (await db.execute(text("""
            SELECT sp.account_key, ca.courier_type
            FROM shipment_profiles sp
            JOIN courier_accounts ca ON ca.account_key = sp.account_key
            WHERE sp.id = :pid
            LIMIT 1
        """), {"pid": assigned_profile_id})).first()
        if row:
            return row.account_key, row.courier_type

    return None



# ---------------- Compat API pentru cod vechi ----------------

def get_courier_service_by_name(name_or_account: str, courier_name: Optional[str] = None):
    """
    Shim de compatibilitate pentru rutele vechi.
    """
    if courier_name:
        lookup = f"{name_or_account} {courier_name}".strip()
        return (
            get_courier_service(lookup)
            or get_courier_service(courier_name)
            or get_courier_service(name_or_account)
        )
    return get_courier_service(name_or_account)


# ---------------- Track & Update ----------------

async def track_and_update_shipments(
    db: AsyncSession,
    full_sync: bool = False,
    days_ago: int = 14,
    per_request_sleep: float = 0.15,
):
    logger.info("--- COURIER SYNC A PORNIT ---")

    final_statuses = {
        "delivered", "refused", "returned", "canceled",
        "livrat", "refuzat", "returnat", "anulat",
        "unknown", "not found", "error", "tracking-error"
    }

    lookback_days = 90 if full_sync else days_ago
    since_date = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    # 1) Select fără lazy-load; aducem câmpurile Order necesare
    stmt = (
        select(
            models.Shipment,                  # s
            models.Order.assigned_profile_id, # o.assigned_profile_id
            models.Order.assigned_courier,    # o.assigned_courier
        )
        .join(models.Order, models.Order.id == models.Shipment.order_id)
        .where(
            models.Shipment.awb.isnot(None),
            models.Shipment.fulfillment_created_at >= since_date,
            True if full_sync else
            func.coalesce(func.lower(models.Shipment.last_status), '').notin_(final_statuses)
        )
    )

    rows = (await db.execute(stmt)).all()
    if not rows:
        logger.info("COURIER SYNC: Nu există livrări de urmărit.")
        return

    logger.info(f"COURIER SYNC: S-au găsit {len(rows)} livrări de urmărit.")

    # 2) Completează cont/curier lipsă SAU greșit.
    #    În paralel, bufferizăm date primitive ca să nu atingem ORM după commit.
    buffered: List[Dict] = []
    fixes = 0

    for s, assigned_profile_id, assigned_courier in rows:
        awb_val = s.awb  # PRIMITIVE acum; îl păstrăm
        ak = s.account_key
        ct = s.courier     # pe shipment îl folosim ca "courier_type" țintă

        resolved = await _resolve_account(db, assigned_profile_id, assigned_courier)
        if resolved:
            r_ak, r_ct = resolved
            need_fix = (ak != r_ak) or (ct != r_ct) or (ak is None) or (ct is None)
            if need_fix:
                s.account_key = r_ak
                s.courier = r_ct
                ak, ct = r_ak, r_ct
                fixes += 1

        # stochează strict primitive pentru faza de tracking
        buffered.append({
            "id": s.id,
            "awb": awb_val,
            "account_key": ak,
            "courier_type": ct,
        })

    if fixes:
        await db.commit()
        logger.info(f"COURIER SYNC: completate/actualizate {fixes} expedieri cu account/courier din profil/mapări.")

    # 3) Grupează pe (courier_type, account_key) folosind datele bufferizate
    grouped: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)
    for r in buffered:
        if r["awb"] and r["account_key"] and r["courier_type"]:
            grouped[(r["courier_type"], r["account_key"])].append(r)

    total_updated = 0

    for (courier_type, account_key), group in grouped.items():
        # găsește serviciul; încearcă account_key, courier_type și combinația lor
        svc = (
            get_courier_service(f"{account_key} {courier_type}")
            or get_courier_service(account_key)
            or get_courier_service(courier_type)
        )
        if not svc:
            logger.warning(f"Nu s-a găsit serviciu pentru '{courier_type}' (cont: {account_key})")
            continue

        logger.info(f"Procesare {len(group)} AWB-uri pentru {courier_type} (cont: {account_key})...")

        for batch in _chunk(group, 50):
            try:
                for r in batch:
                    awb = r["awb"]  # PRIMITIVE, nu ORM
                    try:
                        resp = await svc.track_awb(db, awb, account_key)
                    except Exception as e:
                        logger.error(f"Eroare la track_awb {awb} [{courier_type}/{account_key}]: {e}")
                        continue

                    if resp:
                        new_status = getattr(resp, "status", None)
                        new_date = getattr(resp, "date", None)

                        if new_status:
                            # update direct pe DB pentru a evita reatașarea ORM expirate
                            await db.execute(text("""
                                UPDATE shipments
                                SET last_status = :st,
                                    last_status_at = COALESCE(:dt, last_status_at)
                                WHERE awb = :awb
                            """), {"st": new_status, "dt": new_date, "awb": awb})
                            total_updated += 1

                    if per_request_sleep:
                        await asyncio.sleep(per_request_sleep)

                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error(f"Eroare la grup {courier_type}/{account_key}: {e}", exc_info=True)

    logger.info("COURIER SYNC: " + ("s-au salvat actualizări" if total_updated else "nimic de actualizat"))
    logger.info("--- COURIER SYNC FINALIZAT ---")
