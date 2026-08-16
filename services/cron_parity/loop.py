"""
loop.py — bucla de fundal SHADOW a parității de cron: la fiecare 15 min rulează cele 4 detectoare
(duplicate, COD capture, surpriză, colete) pe fiecare magazin, LOG-ONLY (logger `cron_parity`).
Pornită din main.py DOAR cu env CRON_PARITY_SHADOW=1. Fail-safe: orice excepție e logată și bucla
continuă — nu poate afecta aplicația (modelul ADDR_SHADOW).
"""
from __future__ import annotations
import asyncio
import logging
import os

from sqlalchemy import select

import models

logger = logging.getLogger("cron_parity")

INTERVAL_S = int(os.environ.get("CRON_PARITY_INTERVAL_S", "900"))


async def _pass_once() -> None:
    from database import AsyncSessionLocal
    from . import cod_capture, duplicates, parcels, surprise
    async with AsyncSessionLocal() as db:
        stores = (await db.execute(select(models.Store))).scalars().all()
        for store in stores:
            for name, mod in (("duplicates", duplicates), ("cod_capture", cod_capture),
                              ("surprise", surprise), ("parcels", parcels)):
                try:
                    stats = await mod.run_shadow(db, store)
                    interesting = {k: v for k, v in (stats or {}).items() if v}
                    if interesting and set(interesting) - {"orders", "leave", "pending_cod", "skipped"}:
                        logger.info("PASS %s store=%s %s", name, store.id, interesting)
                except Exception as e:      # shadow — nu rupem NICIODATĂ bucla
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                    logger.warning("PASS-ERR %s store=%s: %s", name, store.id, e)


async def run_forever() -> None:
    logger.info("cron-parity SHADOW loop pornit (interval=%ss)", INTERVAL_S)
    while True:
        try:
            await _pass_once()
        except Exception as e:
            logger.warning("pass failed: %s", e)
        await asyncio.sleep(INTERVAL_S)
