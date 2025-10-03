# /services/couriers/base.py
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from datetime import datetime
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
import models

class TrackingResponse:
    def __init__(self, status: str, date: Optional[datetime], raw_data: Optional[Dict] = None):
        self.status = status
        self.date = date
        self.raw_data = raw_data

class BaseCourier(ABC):
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    @abstractmethod
    async def create_awb(self, *args, **kwargs) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def track_awb(self, db: AsyncSession, awb: str, account_key: Optional[str]) -> TrackingResponse:
        raise NotImplementedError

    @abstractmethod
    async def get_label(self, awb: str, creds: dict, paper_size: str) -> bytes:
        raise NotImplementedError

    async def get_credentials(self, db: AsyncSession, account_key: Optional[str]) -> dict:
        """
        Caută credențialele după:
        1) match exact pe account_key
        2) normalizări (lower/upper, '-' <-> '_')
        3) aliasuri uzuale pe vendor (ex: 'dpd' -> dpdromania/dpd-ro/dpd_jg/dpd_px)
        4) fallback: primul cont cu prefix de vendor (dpd% / sameday%)
        """
        from models import CourierAccount

        def norms(k: str) -> list[str]:
            k = (k or "").strip()
            return list(dict.fromkeys([k, k.lower(), k.upper(), k.replace("_", "-"), k.replace("-", "_")]))

        # 1) exact
        if account_key:
            res = await db.execute(select(CourierAccount).where(CourierAccount.account_key == account_key))
            acc = res.scalar_one_or_none()
            if acc and acc.credentials:
                return acc.credentials

        # 2) normalizări
        for k in norms(account_key or ""):
            if not k:
                continue
            res = await db.execute(select(CourierAccount).where(CourierAccount.account_key == k))
            acc = res.scalar_one_or_none()
            if acc and acc.credentials:
                return acc.credentials

        # 3) aliasuri de vendor
        ak = (account_key or "").strip().lower()
        vendor_aliases: list[str] = []
        if ak.startswith("dpd"):
            vendor_aliases = ["dpdromania", "dpd-ro", "dpd_jg", "dpd-jg", "dpd_px", "dpd-px", "dpd"]
        elif ak.startswith("sameday"):
            vendor_aliases = ["sameday"]

        for alias in vendor_aliases:
            for k in norms(alias):
                res = await db.execute(select(CourierAccount).where(CourierAccount.account_key == k))
                acc = res.scalar_one_or_none()
                if acc and acc.credentials:
                    return acc.credentials

        # 4) fallback pe prefix
        prefix = "dpd%" if ak.startswith("dpd") else ("sameday%" if ak.startswith("sameday") else None)
        if prefix:
            stmt = select(CourierAccount).where(
                and_(CourierAccount.account_key.ilike(prefix), CourierAccount.credentials.isnot(None))
            ).limit(1)
            res = await db.execute(stmt)
            acc = res.scalar_one_or_none()
            if acc and acc.credentials:
                return acc.credentials

        raise ValueError(f"Nu s-au găsit credențiale pentru contul '{account_key}'")
