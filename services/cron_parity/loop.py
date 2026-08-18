"""
loop.py — bucla de fundal SHADOW a parității de cron: la fiecare 15 min rulează detectoarele
(duplicate, blocklist, COD capture, surpriză, colete) pe fiecare magazin, LOG-ONLY (logger `cron_parity`).
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
    from services import automation_config
    from . import blocklist, cod_capture, duplicates, parcels, surprise
    # Bucla e executorul modului `cron`. Automatizările cu mod `on_order` rulează la comandă (webhook →
    # order_shadow); cod_capture cu `on_delivered` rulează tot aici (poll = detecția livrării). Cu
    # default-urile (detectoare=on_order, cod_capture=on_delivered), bucla rulează implicit doar cod_capture.
    MODULES = {"duplicates": duplicates, "parcels": parcels, "surprise": surprise,
               "blocklist": blocklist, "cod_capture": cod_capture}
    async with AsyncSessionLocal() as db:
        stores = (await db.execute(select(models.Store))).scalars().all()
        for store in stores:
            for name, mod in MODULES.items():
                mode = automation_config.mode_of(store, name)
                run_it = (mode == "cron") or (name == "cod_capture" and mode == "on_delivered")
                if not run_it:
                    continue
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
