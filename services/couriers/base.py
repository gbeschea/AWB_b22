# /services/couriers/base.py

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from datetime import datetime
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
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

    async def get_credentials(self, db: AsyncSession, account_key: str) -> dict:
        stmt = select(models.CourierAccount).where(models.CourierAccount.account_key == account_key)
        result = await db.execute(stmt)
        account = result.scalar_one_or_none()
        if not account or not account.credentials:
            raise ValueError(f"Nu s-au găsit credențiale pentru contul cu cheia '{account_key}'")
        return account.credentials