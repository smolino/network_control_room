import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, backups, bgp, geocode, incidents, router_models, routers, simulation, stats, teams, traps, users
from app.auth import authenticate_websocket_token, require_viewer, seed_default_admin
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
    seed_default_admin()
    # Pre-warm the Kafka producer off the event loop before anything can
    # try to use it - get_producer() blocks with a retry loop while `kafka`
    # is still starting up, and trap_listener/fiber_faults must never stall
    # the event loop waiting on that.
    await asyncio.to_thread(get_producer)
    transport = await start_trap_listener()
    fault_task = asyncio.create_task(fiber_fault_loop()) if settings.fiber_fault_simulation else None
    relay_thread, relay_stop = ws_relay.start(asyncio.get_running_loop())
    try:
        yield
    finally:
        transport.close()
        if fault_task:
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

# Every data router needs at least the viewer role; endpoints that change
# anything add a stricter operator/admin check of their own (see app.auth).
# auth (login), simulation (polled by the simulator) and users (admin-only
# throughout) handle access themselves.
for data_router in (routers, incidents, traps, stats, backups, bgp, teams, router_models, geocode):
    app.include_router(data_router.router, dependencies=[Depends(require_viewer)])
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(simulation.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket, token: str | None = None):
    # Browsers can't send an Authorization header on a WebSocket, so the
    # bearer token rides along as ?token=. Accepted first and then closed
    # with 4401 (rather than rejected at the handshake, which the browser
    # only sees as an opaque 1006) so the UI knows to sign in again.
    if await asyncio.to_thread(authenticate_websocket_token, token) is None:
        await websocket.accept()
        await websocket.close(code=4401)
        return
    await manager.connect(websocket)
    try:
        while True:
            # Client doesn't need to send anything; just keep the socket open.
            await websocket.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
