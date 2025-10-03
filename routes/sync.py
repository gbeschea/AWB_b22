# routes/sync.py
import asyncio
import logging
from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from database import get_db
from services import sync_service, address_service
from schemas import SyncPayload # Asigură-te că acest import este corect
from pydantic import BaseModel
from typing import List, Optional





router = APIRouter(
    prefix="/sync",
    tags=["Sync"],
)

# O variabilă simplă pentru a preveni rularea a două sincronizări simultan
sync_in_progress = False


@router.post("/orders")
# --- MODIFICARE AICI ---
# Funcția va primi acum un `payload` la fel ca `trigger_full_sync`
async def trigger_orders_sync(payload: SyncPayload, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """ Pornește o sincronizare doar pentru comenzi, în fundal. """
    global sync_in_progress
    if sync_in_progress:
        raise HTTPException(status_code=409, detail="O altă sincronizare este deja în curs.")
    
    async def run_sync():
        global sync_in_progress
        sync_in_progress = True
        try:
            # --- MODIFICARE AICI ---
            # Folosim `payload.days` în loc de valoarea fixă 30
            await sync_service.run_orders_sync(db, days=payload.days)
        finally:
            sync_in_progress = False
            
    background_tasks.add_task(run_sync)
    return JSONResponse(status_code=202, content={"message": "Sincronizarea comenzilor a început."})

@router.post("/couriers")
async def trigger_couriers_sync(background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """ Pornește o sincronizare doar pentru curieri, în fundal. """
    global sync_in_progress
    if sync_in_progress:
        raise HTTPException(status_code=409, detail="O altă sincronizare este deja în curs.")

    async def run_sync():
        global sync_in_progress
        sync_in_progress = True
        try:
            await sync_service.run_couriers_sync(db)
        finally:
            sync_in_progress = False

    background_tasks.add_task(run_sync)
    return JSONResponse(status_code=202, content={"message": "Sincronizarea curierilor a început."})

@router.post("/full")
async def trigger_full_sync(payload: SyncPayload, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """ Pornește o sincronizare completă (comenzi + curieri) în fundal. """
    global sync_in_progress
    if sync_in_progress:
        raise HTTPException(status_code=409, detail="O altă sincronizare este deja în curs.")

    async def run_sync():
        global sync_in_progress
        sync_in_progress = True
        try:
            logging.warning(f"Sincronizare completă pornită pentru magazinele: {payload.store_ids} pe o perioada de {payload.days} zile")
            # --- MODIFICARE AICI ---
            # Folosim `payload.days` în loc de valoarea fixă 30
            await sync_service.run_full_sync(db, days=payload.days)
        finally:
            sync_in_progress = False

    background_tasks.add_task(run_sync)
    return JSONResponse(status_code=202, content={"message": "Sincronizarea completă a început."})


@router.post("/validate-addresses", status_code=202)
async def trigger_address_validation(
    payload: SyncPayload,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """
    Pornește validarea adreselor pentru comenzi 'nevalidate', filtrate opțional
    pe ultimele N zile și pe lista de magazine (store_ids).
    """
    background_tasks.add_task(
        address_service.validate_unvalidated_orders,
        db,
        days=payload.days,
        store_ids=getattr(payload, "store_ids", None)
    )
    return {"message": "Validarea adreselor a început în fundal."}


