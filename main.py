
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from routes import (
    store_categories, printing, logs, orders, sync, labels,
    settings as settings_router, validation, webhooks, couriers, background, processing
)
from websocket_manager import manager
from settings import settings

app = FastAPI(
    title="AWB Hub",
    description="Aplicator pentru managementul comenzilor și generarea de AWB-uri.",
    version="1.0.0"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static
app.mount("/static", StaticFiles(directory="static"), name="static")

# Routers
app.include_router(orders.router, tags=["Orders"])
app.include_router(processing.router, tags=["Processing"])
app.include_router(sync.router, tags=["Sync"])
app.include_router(labels.router, tags=["Labels"])
app.include_router(settings_router.router, tags=["Settings"])
app.include_router(validation.router, tags=["Validation"])
app.include_router(webhooks.router, tags=["Webhooks"])
app.include_router(couriers.router, tags=["Couriers"])
app.include_router(printing.router, tags=["Printing"])
app.include_router(logs.router, tags=["Logs"])
app.include_router(store_categories.router, tags=["Store Categories"])
app.include_router(background.router, tags=["Background Tasks"])

@app.websocket("/ws/status")
async def websocket_endpoint(websocket: WebSocket):
    """Endpoint pentru status live."""
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.get("/", tags=["Root"])
def read_root():
    return {"message": "AWB Hub is running."}
