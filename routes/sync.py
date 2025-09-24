# routes/sync.py
import asyncio
import logging
from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from database import get_db
from services import sync_service
from schemas import SyncPayload # Asigură-te că acest import este corect


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

# Adaugă această funcție nouă în fișier (de ex. după funcția run_sync)
@router.post("/validate-addresses", status_code=202)
async def trigger_address_validation(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """
    Pornește un task în fundal pentru a valida adresele tuturor comenzilor.
    """
    background_tasks.add_task(sync_service.run_address_validation_for_all_orders, db)
    return {"message": "Validarea adreselor a început în fundal. Verifică log-urile pentru progres."}
