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
from app.streaming import ws_relay
from app.streaming.producer import get_producer
from app.ws import manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Pre-warm the Kafka producer off the event loop before anything can
    # try to use it - get_producer() blocks with a retry loop while `kafka`
    # is still starting up, and trap_listener/fiber_faults must never stall
    # the event loop waiting on that.
    await asyncio.to_thread(get_producer)
    transport = await start_trap_listener()
    fault_task = asyncio.create_task(fiber_fault_loop())
    relay_thread, relay_stop = ws_relay.start(asyncio.get_running_loop())
    try:
        yield
    finally:
        transport.close()
        fault_task.cancel()
        ws_relay.stop(relay_thread, relay_stop)


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
