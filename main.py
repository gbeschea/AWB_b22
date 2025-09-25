from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Settings (dotenv/env based)
try:
    from settings import settings  # type: ignore
except Exception:  # pragma: no cover - keep app bootable even if settings fails during codegen
    class _Dummy:
        PROJECT_NAME = "AWB Hub"
        DEBUG = True
    settings = _Dummy()  # type: ignore

# ----- App -----
app = FastAPI(
    title=getattr(settings, "PROJECT_NAME", "AWB Hub"),
    description="""Hub pentru managementul comenzilor, generarea AWB-urilor
    și integrarea cu curieri (DPD, Sameday etc.).""".strip(),
    version="1.0.0",
)

# CORS (lax pentru dev; poți restrânge ulterior)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static
_static_dir_candidates = [Path("static"), Path("./static")]
for _d in _static_dir_candidates:
    if _d.exists():
        app.mount("/static", StaticFiles(directory=str(_d)), name="static")
        break

# Routers – încearcă mai întâi din pachetul `routes/`, apoi fallback la module top-level
def _include_router_if_exists(module_name: str, alias: str | None = None):
    mod = None
    try:
        mod = __import__(f"routes.{module_name}", fromlist=["router"])  # type: ignore
    except Exception:
        try:
            mod = __import__(module_name, fromlist=["router"])  # type: ignore
        except Exception:
            logging.getLogger(__name__).info("Router '%s' nu a fost găsit; îl sar.", module_name)
            return
    if hasattr(mod, "router"):
        app.include_router(getattr(mod, "router"), tags=[alias or module_name])
    else:
        logging.getLogger(__name__).warning("Modulul '%s' nu are `router`.", module_name)

for name, alias in [
    ("orders", "Orders"),
    ("labels", "Labels"),
    ("printing", "Print Hub"),
    ("validation", "Validation Hub"),
    ("settings", "Settings"),
    ("logs", "Print Logs"),
    ("sync", "Sync"),
    ("webhooks", "Webhooks"),
    ("processing", "Processing"),
    ("stores", "Stores"),
    ("store_categories", "Store Categories"),
    ("couriers", "Couriers"),
    ("actions", "Actions"),
    ("background", "Background"),
]:
    _include_router_if_exists(name, alias)

# WebSocket: status progres sincronizări/printări
try:
    from websocket_manager import manager  # type: ignore
except Exception:
    manager = None  # type: ignore

@app.websocket("/ws/status")
async def ws_status(websocket: WebSocket):
    if manager is None:
        # fallback: accept & ignoră mesaje, ca să nu crape UI-ul
        await websocket.accept()
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            return
    else:
        await manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            manager.disconnect(websocket)

# Root + health
@app.get("/", tags=["Root"])
def root():
    return {"message": "AWB Hub is running"}

@app.get("/healthz", tags=["Root"])
def healthz():
    return {"status": "ok"}
