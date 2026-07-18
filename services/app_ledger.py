"""Reinstall-proof per-shop flags (free-trial guard).

Keyed by a salted SHA-256 of the shop domain (not the domain itself, and no FK to Store),
so the record survives uninstall + shop/redact and a shop can't reset its trial by
reinstalling. Mirrors the fleet's free-ledger pattern.
"""
import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from settings import settings

_SALT = (settings.SESSION_SECRET or "order-hub-static-salt").encode()


def domain_hash(domain: str) -> str:
    return hashlib.sha256(_SALT + (domain or "").strip().lower().encode()).hexdigest()


async def _get(db: AsyncSession, domain: str) -> models.AppLedger | None:
    res = await db.execute(
        select(models.AppLedger).where(models.AppLedger.domain_hash == domain_hash(domain))
    )
    return res.scalar_one_or_none()


async def has_used_trial(db: AsyncSession, domain: str) -> bool:
    row = await _get(db, domain)
    return bool(row and row.trial_used)


async def mark_trial_used(db: AsyncSession, domain: str) -> None:
    row = await _get(db, domain)
    if row is None:
        row = models.AppLedger(domain_hash=domain_hash(domain), trial_used=True)
        db.add(row)
    else:
        row.trial_used = True
    await db.commit()
