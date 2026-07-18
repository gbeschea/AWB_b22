# crud/couriers.py
from __future__ import annotations

from typing import Optional, Dict, Any, List
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models


# ============ Courier Accounts ============

async def get_courier_accounts(db: AsyncSession) -> List[models.CourierAccount]:
    res = await db.execute(
        select(models.CourierAccount)
        .options(selectinload(models.CourierAccount.mappings))
        .order_by(models.CourierAccount.name)
    )
    return res.scalars().all()


async def get_courier_account_by_key(db: AsyncSession, account_key: str) -> Optional[models.CourierAccount]:
    res = await db.execute(
        select(models.CourierAccount).where(models.CourierAccount.account_key == account_key)
    )
    return res.scalar_one_or_none()


# ---- Multi-tenant scoped reads (embedded app). During transition a NULL store_id means
# "legacy/shared", so we return rows owned by the shop OR not-yet-assigned. ----

async def get_courier_accounts_for_store(db: AsyncSession, store_id: int) -> List[models.CourierAccount]:
    res = await db.execute(
        select(models.CourierAccount)
        .where((models.CourierAccount.store_id == store_id) | (models.CourierAccount.store_id.is_(None)))
        .options(selectinload(models.CourierAccount.mappings))
        .order_by(models.CourierAccount.name)
    )
    return res.scalars().all()


async def get_courier_mappings_for_store(db: AsyncSession, store_id: int) -> List[models.CourierMapping]:
    res = await db.execute(
        select(models.CourierMapping)
        .where((models.CourierMapping.store_id == store_id) | (models.CourierMapping.store_id.is_(None)))
    )
    return res.scalars().all()


async def get_shipment_profiles_for_store(db: AsyncSession, store_id: int) -> List[models.ShipmentProfile]:
    res = await db.execute(
        select(models.ShipmentProfile)
        .where((models.ShipmentProfile.store_id == store_id) | (models.ShipmentProfile.store_id.is_(None)))
        .order_by(models.ShipmentProfile.id)
    )
    return res.scalars().all()


async def create_courier_account(
    db: AsyncSession,
    name: str,
    account_key: str,
    courier_type: str,
    credentials_dict: Optional[Dict[str, Any]] = None,
    tracking_url: Optional[str] = None,
    is_active: bool = True,
) -> models.CourierAccount:
    acc = models.CourierAccount(
        name=name.strip(),
        account_key=account_key.strip(),
        courier_type=courier_type.strip(),
        tracking_url=(tracking_url or None),
        credentials=(credentials_dict or {}),
        is_active=is_active,
    )
    db.add(acc)
    await db.commit()
    await db.refresh(acc)
    return acc


async def update_courier_account(
    db: AsyncSession,
    account_id: int,
    name: str,
    account_key: str,
    courier_type: str,
    credentials_dict: Optional[Dict[str, Any]] = None,
    tracking_url: Optional[str] = None,
    is_active: bool = True,
) -> Optional[models.CourierAccount]:
    res = await db.execute(
        select(models.CourierAccount).where(models.CourierAccount.id == account_id)
    )
    acc = res.scalar_one_or_none()
    if not acc:
        return None

    acc.name = name.strip()
    acc.account_key = account_key.strip()
    acc.courier_type = courier_type.strip()
    acc.tracking_url = tracking_url or None
    acc.is_active = bool(is_active)

    # păstrează secretele dacă nu vin în request
    keep_keys = {"password", "api_password", "token", "api_key", "secret"}
    existing = acc.credentials or {}
    new_creds = dict(credentials_dict or {})
    for k in keep_keys:
        if not new_creds.get(k) and k in existing:
            new_creds[k] = existing[k]
    existing.update({k: v for k, v in new_creds.items() if v is not None})
    acc.credentials = existing

    await db.commit()
    await db.refresh(acc)
    return acc


async def upsert_courier_account(
    db: AsyncSession,
    *,
    account_key: str,
    name: str,
    courier_type: str,
    credentials: Optional[Dict[str, Any]] = None,
    tracking_url: Optional[str] = None,
    is_active: bool = True,
) -> models.CourierAccount:
    acc = await get_courier_account_by_key(db, account_key)
    if acc is None:
        return await create_courier_account(
            db,
            name=name,
            account_key=account_key,
            courier_type=courier_type,
            credentials_dict=credentials,
            tracking_url=tracking_url,
            is_active=is_active,
        )

    return await update_courier_account(
        db,
        account_id=acc.id,
        name=name,
        account_key=account_key,
        courier_type=courier_type,
        credentials_dict=credentials,
        tracking_url=tracking_url,
        is_active=is_active,
    )


# ============ Courier Mappings ============

async def get_courier_mappings(db: AsyncSession) -> List[models.CourierMapping]:
    res = await db.execute(select(models.CourierMapping).order_by(models.CourierMapping.id))
    return res.scalars().all()


async def create_courier_mapping(db: AsyncSession, shopify_name: str, account_key: str) -> models.CourierMapping:
    m = models.CourierMapping(
        shopify_name=shopify_name.strip(),
        account_key=account_key.strip(),
    )
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


# ============ Aux ============

async def get_courier_categories(db: AsyncSession) -> List[models.CourierCategory]:
    res = await db.execute(select(models.CourierCategory).order_by(models.CourierCategory.name))
    return res.scalars().all()


async def get_all_shipment_profiles(db: AsyncSession) -> List[models.ShipmentProfile]:
    res = await db.execute(select(models.ShipmentProfile).order_by(models.ShipmentProfile.name))
    return res.scalars().all()
