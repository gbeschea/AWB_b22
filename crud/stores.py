from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
import models
from typing import Optional, List

# ── Helpers
def _normalize_domain(domain: str) -> str:
    d = (domain or "").strip().lower()
    if d.startswith("https://"):
        d = d[8:]
    elif d.startswith("http://"):
        d = d[7:]
    if d.startswith("www."):
        d = d[4:]
    if d.endswith("/"):
        d = d[:-1]
    return d



# ── READ
async def get_stores(db: AsyncSession):
    """Returnează toate magazinele ordonate după nume, cu categoriile preîncărcate."""
    result = await db.execute(
        select(models.Store)
        .options(selectinload(models.Store.categories))
        .order_by(models.Store.name)
    )
    return result.scalars().all()

async def get_store_by_id(db: AsyncSession, store_id: int):
    result = await db.execute(
        select(models.Store)
        .options(selectinload(models.Store.categories))
        .where(models.Store.id == store_id)
    )
    return result.scalar_one_or_none()


async def create_store(
    db: AsyncSession,
    name: str,
    domain: str,
    shared_secret: str,
    access_token: str,
):
    """
    Creează un magazin Shopify nou.
    """
    store = models.Store(
        name=name.strip(),
        domain=domain.strip(),
        shared_secret=shared_secret.strip(),
        access_token=access_token.strip(),
        is_active=True,        # implicit activ
        paper_size="A4",       # default
        pii_source="shopify",  # default
    )
    db.add(store)
    await db.commit()
    await db.refresh(store)
    return store


async def get_all_store_categories(db: AsyncSession):
    """
    Returnează toate categoriile de magazin (pentru formularul din setări).
    """
    result = await db.execute(select(models.StoreCategory).order_by(models.StoreCategory.name))
    return result.scalars().all()


async def update_store(
    db: AsyncSession,
    store_id: int,
    *,
    name: Optional[str] = None,
    domain: Optional[str] = None,
    shared_secret: Optional[str] = None,
    access_token: Optional[str] = None,
    api_version: Optional[str] = None,
    pii_source: Optional[str] = None,
    is_active: Optional[bool] = None,
    paper_size: Optional[str] = None,
    dpd_client_id: Optional[str] = None,
    category_ids: Optional[List[int]] = None,   # <-- important
):
    store = await get_store_by_id(db, store_id)
    if not store:
        return None

    if name is not None:
        store.name = name.strip()
    if domain is not None:
        store.domain = _normalize_domain(domain)
    if shared_secret is not None:
        store.shared_secret = shared_secret
    if access_token is not None:
        store.access_token = access_token
    if api_version is not None:
        store.api_version = api_version
    if pii_source is not None:
        store.pii_source = pii_source
    if is_active is not None:
        store.is_active = bool(is_active)
    if paper_size is not None:
        store.paper_size = paper_size
    if dpd_client_id is not None:
        store.dpd_client_id = dpd_client_id

    # Actualizează relația many-to-many cu categoriile
    if category_ids is not None:
        res = await db.execute(
            select(models.StoreCategory).where(models.StoreCategory.id.in_(category_ids))
        )
        store.categories = res.scalars().all()

    await db.commit()
    await db.refresh(store)
    return store


# ── alias ca să nu mai „pice” dacă alt cod cere alt nume
list_stores = get_stores
