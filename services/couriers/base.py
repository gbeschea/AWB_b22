# services/couriers/base.py
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

import httpx
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

import models


@dataclass
class TrackingResponse:
    status_raw: Optional[str] = None      # text brut de la curier
    code: Optional[str] = None            # ex: AWB
    extra: Optional[Dict[str, Any]] = None
    status: Optional[str] = None
    date: Optional[datetime] = None
    success: bool = True
    raw_data: Optional[Any] = None



@dataclass
class LabelResponse:
    success: bool
    message: Optional[str] = None
    label_pdf: Optional[bytes] = None
    label_mime: Optional[str] = "application/pdf"
    extra: Optional[Dict[str, Any]] = None


@dataclass
class VoidResponse:
    success: bool
    message: Optional[str] = None
    raw: Optional[Any] = None


class BaseCourier(ABC):
    """
    Bază comună pentru curieri.
    - self.http: httpx.AsyncClient (nume preferat)
    - self.client: alias pentru compatibilitate cu cod existent
    """

    name: str
    display_name: str

    # CINE împinge fulfillment-ul în Shopify pentru acest curier?
    #   False (implicit) = curier DIRECT (DPD/Sameday/GLS/…): nimeni altcineva nu fulfill-uiește, deci OH
    #                      trebuie s-o facă (la creare pe calea split, altfel când coletul pornește).
    #   True             = platforma curierului fulfill-uiește SINGURĂ comanda în Shopify (xConnector,
    #                      Frisbo). Dacă OH ar împinge și el, ar ieși fulfillment DUBLU pe aceeași comandă.
    # Măsurat pe primul AWB creat de OH (GEN17599): xConnector a fulfill-uit în Shopify la 4 secunde după
    # crearea etichetei, fără ca OH să facă ceva.
    owns_shopify_fulfillment: bool = False

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http
        self.client = http  # compat

    # Lăsăm semnături largi ca să nu stricăm implementările existente.
    @abstractmethod
    async def track_awb(self, *args, **kwargs) -> TrackingResponse:
        ...

    @abstractmethod
    async def create_awb(self, *args, **kwargs) -> Any:
        ...

    @abstractmethod
    async def get_label(self, *args, **kwargs) -> Any:
        ...

    async def void_awb(self, *args, **kwargs) -> "VoidResponse":
        """Cancel/void an AWB. Not every courier supports it (or only before pickup is
        ordered). Default: unsupported — adapters override where the API allows it."""
        raise NotImplementedError(f"{self.__class__.__name__}.void_awb not implemented")

    async def request_pickup(self, *args, **kwargs) -> Dict[str, Any]:
        """Request a courier pickup ("cerere de ridicare") for one or more AWBs. Some couriers
        (e.g. FAN) require this as a SEPARATE step — the AWB alone isn't collected. Others
        auto-schedule collection from the AWB's pickup date. Default: not needed."""
        return {"supported": False, "requested": False,
                "message": "Pickup is scheduled automatically with the AWB (no separate request needed)."}

    async def get_credentials(self, db: AsyncSession, account_key: Optional[str]) -> Dict[str, Any]:
        """
        Returnează credențialele contului după:
          1) match exact pe account_key
          2) normalizări (lower/upper, '-' <-> '_')
          3) aliasuri pe vendor (ex: 'dpd' -> dpdromania/dpd-ro/dpd_jg/dpd_px)
          4) fallback: primul cont cu prefix de vendor (dpd% / sameday%)
        """
        from models import CourierAccount  # tipul e în models

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

        # 3) aliasuri vendor
        ak = (account_key or "").strip().lower()
        vendor_aliases: list[str] = []
        if ak.startswith("dpd"):
            vendor_aliases = ["dpdromania", "dpd-ro", "dpd_jg", "dpd-jg", "dpd_px", "dpd-px", "dpd"]
        elif ak.startswith("sameday"):
            vendor_aliases = ["sameday"]
        elif ak.startswith("packeta"):
            vendor_aliases = ["packeta", "czpacketahomehd", "plhomedeliveryhd", "cz-packeta", "pl-packeta"]
        elif ak.startswith("econt"):
            vendor_aliases = ["econt"]

        for alias in vendor_aliases:
            for k in norms(alias):
                res = await db.execute(select(CourierAccount).where(CourierAccount.account_key == k))
                acc = res.scalar_one_or_none()
                if acc and acc.credentials:
                    return acc.credentials

        # 4) fallback pe prefix
        prefix = None
        if ak.startswith("dpd"):
            prefix = "dpd%"
        elif ak.startswith("sameday"):
            prefix = "sameday%"
        elif ak.startswith("packeta"):
            prefix = "packeta%"
        elif ak.startswith("econt"):
            prefix = "econt%"

        if prefix:
            stmt = (
                select(CourierAccount)
                .where(and_(CourierAccount.account_key.ilike(prefix), CourierAccount.credentials.isnot(None)))
                .limit(1)
            )
            res = await db.execute(stmt)
            acc = res.scalar_one_or_none()
            if acc and acc.credentials:
                return acc.credentials

        raise ValueError(f"No credentials found for account '{account_key}'")


__all__ = ["BaseCourier", "TrackingResponse", "LabelResponse", "VoidResponse"]
