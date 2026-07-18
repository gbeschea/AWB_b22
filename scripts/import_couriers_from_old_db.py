"""One-off: copy courier_accounts from the OLD (Gigi) AWB DB into THIS app's order_hub DB.

Why: the old internal AWB tool stored the complete, working courier credential blobs
(DPD pickupOfficeId/clientSecret, Sameday service_id/pickup_point, Econt/Packeta sender
address). The KB secrets are login-only, so we seed the real blobs for verification.

Safety:
- Reads the OLD DB URL from STDIN (line 1) so it never lands in argv/env/process list.
- Upserts through the app ORM → credentials get AES-GCM encrypted with THIS deploy's key.
- Scopes every account to --store (default 1, the installed dev store) for tenancy.
- READ-ONLY on the old DB (a single SELECT); only writes to order_hub.
"""
import sys
import json
import asyncio
import argparse

import asyncpg
from sqlalchemy import select

from database import AsyncSessionLocal
import models


async def main(store_id: int):
    old_url = sys.stdin.readline().strip().replace("postgresql+asyncpg://", "postgresql://")
    if not old_url:
        print("no OLD DB url on stdin")
        return

    con = await asyncpg.connect(old_url, timeout=20)
    try:
        rows = await con.fetch("SELECT * FROM courier_accounts ORDER BY account_key")
    finally:
        await con.close()

    imported, skipped = [], []
    async with AsyncSessionLocal() as db:
        for rec in rows:
            r = dict(rec)
            ak = r.get("account_key")
            if not ak:
                continue
            creds = r.get("credentials")
            if isinstance(creds, str):
                try:
                    creds = json.loads(creds)
                except Exception:
                    creds = None
            if not creds:
                skipped.append(ak)
                continue

            existing = (await db.execute(
                select(models.CourierAccount).where(models.CourierAccount.account_key == ak)
            )).scalar_one_or_none()

            name = r.get("name") or ak
            courier_type = r.get("courier_type") or ak.split("-")[0]
            tracking_url = r.get("tracking_url")

            if existing:
                existing.credentials = creds
                existing.name = name
                existing.courier_type = courier_type
                existing.tracking_url = tracking_url
                existing.is_active = True
                existing.store_id = store_id
            else:
                db.add(models.CourierAccount(
                    store_id=store_id,
                    account_key=ak,
                    name=name,
                    courier_type=courier_type,
                    tracking_url=tracking_url,
                    credentials=creds,
                    is_active=True,
                ))
            imported.append(f"{ak}({courier_type})")
        await db.commit()

    print("imported:", imported)
    if skipped:
        print("skipped (no creds):", skipped)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", type=int, default=1)
    args = ap.parse_args()
    asyncio.run(main(args.store))
