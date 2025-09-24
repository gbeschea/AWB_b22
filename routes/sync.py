from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

import models
from database import get_db
from services import sync_service

router = APIRouter(
    prefix="/sync",
    tags=["Sync"]
)

class SyncPayload(BaseModel):
    days: int = 30

class FullSyncPayload(BaseModel):
    store_ids: list[int]
    days: int = 30

@router.post("/orders", status_code=202)
async def run_orders_sync_route(
    payload: SyncPayload,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Rulează o sincronizare standard de comenzi pentru TOATE magazinele active.
    """
    stmt = select(models.Store).where(models.Store.is_active == True)
    active_stores = (await db.execute(stmt)).scalars().all()

    if not active_stores:
        raise HTTPException(status_code=404, detail="Niciun magazin activ nu a fost găsit pentru sincronizare.")

    background_tasks.add_task(
        sync_service.run_orders_sync, 
        db=db, 
        stores_to_sync=active_stores, 
        days=payload.days, 
        full_sync=False
    )
    return {"message": "Sincronizarea comenzilor a început în fundal pentru magazinele active."}

@router.post("/couriers", status_code=202)
async def run_couriers_sync_route(
    background_tasks: BackgroundTasks, 
    db: AsyncSession = Depends(get_db)
):
    """
    Rulează o sincronizare a statusurilor de la curieri.
    """
    background_tasks.add_task(sync_service.run_couriers_sync, db=db, full_sync=True)
    return {"message": "Sincronizarea statusurilor de la curieri a început în fundal."}


@router.post("/full", status_code=202)
async def run_full_sync_route(
    payload: FullSyncPayload,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Rulează o sincronizare completă pentru magazinele selectate.
    """
    if not payload.store_ids:
        raise HTTPException(status_code=400, detail="Te rog selectează cel puțin un magazin.")

    background_tasks.add_task(
        sync_service.run_full_sync,
        db=db,
        store_ids=payload.store_ids,
        days=payload.days
    )
    return {"message": f"Sincronizarea completă a început pentru {len(payload.store_ids)} magazine."}

@router.post("/validate-addresses", status_code=202)
async def trigger_address_validation(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """
    Pornește un task în fundal pentru a valida adresele tuturor comenzilor.
    """
    background_tasks.add_task(sync_service.run_address_validation_for_all_orders, db)
    return {"message": "Validarea adreselor a început în fundal."}