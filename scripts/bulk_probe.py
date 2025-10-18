# scripts/bulk_probe.py
import os
import asyncio
import time
import argparse
import logging
from collections import defaultdict
from typing import Dict, List, Tuple

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.dialects import postgresql as pg


# Folosim serviciile existente
from services.couriers import get_courier_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)


# ---------- Utils ----------
def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


class RateLimiter:
    """simplu: așteaptă ~1/rps între lansările task-urilor"""
    def __init__(self, rps: float):
        self.interval = 1.0 / max(0.001, rps)
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self):
        async with self._lock:
            now = time.perf_counter()
            delay = max(0.0, self._last + self.interval - now)
            if delay:
                await asyncio.sleep(delay)
            self._last = time.perf_counter()


async def get_session(db_url: str):
    if not db_url:
        raise RuntimeError("DATABASE_URL lipsește din env")
    engine = create_async_engine(db_url, future=True, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    db = Session()
    return db, Session


# ---------- Fetch ----------
async def fetch_shipments_for_probe(
    db: AsyncSession,
    days: int,
    couriers: List[str],
    per_group: int,
    only_existing_accounts: bool = True,
) -> Dict[Tuple[str, str], List[str]]:
    """
    Returnează {(courier, account_key): [awb, ...]} tăiat la per_group/cheie.
    """
    # NOTĂ: folosim coloane sigure: last_status_at / printed_at (schema ta nu are created_at pe shipments)
    join_accounts = "JOIN courier_accounts ca ON ca.account_key = s.account_key" if only_existing_accounts else ""
    and_couriers = "AND LOWER(COALESCE(s.courier,'')) = ANY(:couriers)" if couriers else ""

    sql_txt = f"""
        SELECT
            LOWER(COALESCE(s.courier,'')) AS courier,
            COALESCE(s.account_key,'')     AS account_key,
            s.awb
        FROM shipments s
        {join_accounts}
        WHERE s.awb IS NOT NULL
          AND s.awb <> ''
          {and_couriers}
          AND COALESCE(s.last_status_at, s.printed_at) >= NOW() - (:days * INTERVAL '1 day')
        ORDER BY COALESCE(s.last_status_at, s.printed_at) DESC
    """

    sql = sa.text(sql_txt)

    # tipăm parametrii ca să nu mai dea eroare la driver
    sql = sql.bindparams(sa.bindparam("days", type_=sa.Integer()))
    if couriers:
        sql = sql.bindparams(sa.bindparam("couriers", type_=pg.ARRAY(sa.Text())))

    params = {"days": int(days)}
    if couriers:
        params["couriers"] = [c.lower() for c in couriers]

    res = await db.execute(sql, params)

    params = {"days": int(days)}
    if couriers:
        params["couriers"] = [c.lower() for c in couriers]

    res = await db.execute(sql, params)
    rows = res.fetchall()


    groups: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for courier, account_key, awb in rows:
        key = (courier or "", account_key or "")
        if len(groups[key]) < per_group:
            groups[key].append(awb)

    return groups


# ---------- Runner ----------
async def run_batches_for_group(
    db: AsyncSession,
    courier: str,
    account_key: str,
    awbs: List[str],
    batch_size: int,
    concurrency: int,
    limiter: RateLimiter,
    verbose: bool = False,
):
    svc = get_courier_service(courier)
    if svc is None:
        log.warning(f"[{courier}/{account_key}] nu există serviciu configurat")
        return

    sem = asyncio.Semaphore(concurrency)

    async def one(awb: str):
        async with sem:
            await limiter.wait()
            try:
                resp = await svc.track_awb(db, awb, account_key)
                status = getattr(resp, "status", None) or getattr(resp, "state", "")
                dt = getattr(resp, "date", None)
                print(f"[OK] {courier}/{account_key} {awb} -> {status} {dt if dt else ''}")
            except Exception as e:
                print(f"[ERR] {courier}/{account_key} {awb} -> {e}")

    batches = list(chunked(awbs, batch_size))
    for i, batch in enumerate(batches, 1):
        if verbose:
            print(f"[RUN] {courier}/{account_key} batch#{i} size={len(batch)}")
        await asyncio.gather(*(one(a) for a in batch))


# ---------- Main ----------
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--per-group", type=int, default=300)
    ap.add_argument("--rps", type=float, default=3.0)
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--batch-size", type=int, default=25)
    ap.add_argument("--couriers", type=str, default="")  # ex: "packeta,dpd,sameday,econt"
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--only-existing-accounts", action="store_true", default=True)
    ap.add_argument("--plan-only", action="store_true", help="doar afișează planul și iese")
    args = ap.parse_args()

    db_url = os.getenv("DATABASE_URL")
    log.info("Se conectează la DB...")
    db, _Session = await get_session(db_url)

    couriers = [c.strip().lower() for c in args.couriers.split(",") if c.strip()] if args.couriers else []

    groups = await fetch_shipments_for_probe(
        db=db,
        days=args.days,
        couriers=couriers,
        per_group=args.per_group,
        only_existing_accounts=args.only_existing_accounts,
    )

    total = sum(len(v) for v in groups.values())
    log.info(f"Grupe găsite: {len(groups)} | AWB total: {total}")
    log.info(
        f"Parametri: days={args.days} per_group={args.per_group} batch_size={args.batch_size} "
        f"rps={args.rps:.2f} concurrency={args.concurrency}"
    )

    # Afișează planul
    for (courier, account_key), awbs in groups.items():
        nb = (len(awbs) + args.batch_size - 1) // args.batch_size
        log.info(f"Plan [{courier} / {account_key}]: {len(awbs)} AWB în {nb} batch-uri")
        if args.verbose:
            for i, chunk in enumerate(chunked(awbs, args.batch_size), 1):
                print(f"[PLAN] {courier}/{account_key} batch#{i} size={len(chunk)} -> {chunk}")

    if args.plan_only:
        return  # doar plan

    # Execuție tracking
    limiter = RateLimiter(args.rps)
    for (courier, account_key), awbs in groups.items():
        await run_batches_for_group(
            db=db,
            courier=courier,
            account_key=account_key,
            awbs=awbs,
            batch_size=args.batch_size,
            concurrency=args.concurrency,
            limiter=limiter,
            verbose=args.verbose,
        )

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
