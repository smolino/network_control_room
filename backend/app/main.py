import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.api import backups, bgp, incidents, routers, simulation, stats, teams, traps
from app.config import settings
from app.db import init_db
from app.fiber_faults import fiber_fault_loop
from app.snmp.trap_listener import start_trap_listener
from app.ws import manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    transport = await start_trap_listener()
    fault_task = asyncio.create_task(fiber_fault_loop())
    try:
        yield
    finally:
        transport.close()
        fault_task.cancel()


app = FastAPI(title="Network Control Room", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routers.router)
app.include_router(incidents.router)
app.include_router(traps.router)
app.include_router(stats.router)
app.include_router(backups.router)
app.include_router(bgp.router)
app.include_router(teams.router)
app.include_router(simulation.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Client doesn't need to send anything; just keep the socket open.
            await websocket.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
