import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.config import settings
from app.db import SessionLocal, get_db
from app.models import (
    AppSetting,
    BgpPeering,
    Incident,
    IncidentNotification,
    Interface,
    InterfaceBundle,
    RemediationAction,
    Router,
    RouterConfigBackup,
    TrapEvent,
)
from app.topology_graph import clear_topology
from app.ws import manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/simulation", tags=["simulation"])

# In-memory only, not persisted: the simulator container polls this flag
# (see simulator/trap_simulator.py:simulation_control_loop) and pauses/
# resumes sending traps accordingly. A backend restart resets it back to
# enabled, which is the right default for a demo control switch.
_state = {"enabled": True}

SEED_DEMO_FLEET = "seed_demo_fleet"
# How often simulated incidents happen, as a multiplier of the default pace
# (1 = default, 0 = none, MAX_RATE = five times as many) - one for the L1
# fiber cuts (app.fiber_faults) and one for the L3 trap simulator
# (simulator/trap_simulator.py). Persisted, unlike the on/off flag above, so
# a tuned demo keeps its pace across redeploys.
L1_RATE = "l1_incident_rate"
L3_RATE = "l3_incident_rate"
MAX_RATE = 5.0


class SimulationStatus(BaseModel):
    enabled: bool


class SimulationStatusOut(SimulationStatus):
    # Whether the simulator seeds its built-in demo fleet when it starts -
    # turned off for good by reset_fleet, so a cleared map stays clear.
    seed_demo_fleet: bool = True
    l1_rate: float = 1.0
    l3_rate: float = 1.0
    # Average fiber cuts per hour at l1_rate 1, for the Settings slider's
    # estimate.
    l1_base_per_hour: float = 0.0


class SimulationRates(BaseModel):
    l1_rate: float | None = Field(default=None, ge=0, le=MAX_RATE)
    l3_rate: float | None = Field(default=None, ge=0, le=MAX_RATE)


class FleetResetOut(BaseModel):
    routers: int
    bgp_links: int
    incidents: int


def is_enabled() -> bool:
    return _state["enabled"]


def _seed_demo_fleet(db: Session) -> bool:
    row = db.get(AppSetting, SEED_DEMO_FLEET)
    return row is None or row.value == "true"


def _rate(db: Session, key: str) -> float:
    row = db.get(AppSetting, key)
    return float(row.value) if row else 1.0


def l1_rate() -> float:
    """Current L1 multiplier, for app.fiber_faults - cached here so its
    polling doesn't hit the database every few seconds."""
    if "l1_rate" not in _state:
        db = SessionLocal()
        try:
            _state["l1_rate"] = _rate(db, L1_RATE)
        finally:
            db.close()
    return _state["l1_rate"]


def _status(db: Session) -> SimulationStatusOut:
    mean_interval = (settings.fiber_fault_interval_min_seconds + settings.fiber_fault_interval_max_seconds) / 2
    return SimulationStatusOut(
        enabled=_state["enabled"],
        seed_demo_fleet=_seed_demo_fleet(db),
        l1_rate=_rate(db, L1_RATE),
        l3_rate=_rate(db, L3_RATE),
        l1_base_per_hour=3600 / mean_interval if settings.fiber_fault_simulation and mean_interval > 0 else 0.0,
    )


# Deliberately unauthenticated (see app.main) - the simulator polls it, and
# a few demo knobs reveal nothing worth protecting.
@router.get("/status", response_model=SimulationStatusOut)
def get_status(db: Session = Depends(get_db)):
    return _status(db)


@router.post("/status", response_model=SimulationStatusOut, dependencies=[Depends(require_admin)])
def set_status(payload: SimulationStatus, db: Session = Depends(get_db)):
    _state["enabled"] = payload.enabled
    return _status(db)


@router.post("/rates", response_model=SimulationStatusOut, dependencies=[Depends(require_admin)])
def set_rates(payload: SimulationRates, db: Session = Depends(get_db)):
    for key, value in ((L1_RATE, payload.l1_rate), (L3_RATE, payload.l3_rate)):
        if value is None:
            continue
        setting = db.get(AppSetting, key) or AppSetting(key=key)
        setting.value = str(value)
        db.add(setting)
    db.commit()
    _state["l1_rate"] = _rate(db, L1_RATE)
    return _status(db)


# Children before parents - none of these FKs cascade at the DB level.
_FLEET_MODELS = [
    Interface,
    InterfaceBundle,
    IncidentNotification,
    RemediationAction,
    RouterConfigBackup,
    TrapEvent,
    Incident,
    BgpPeering,
    Router,
]


@router.post("/reset-fleet", response_model=FleetResetOut, dependencies=[Depends(require_admin)])
async def reset_fleet(db: Session = Depends(get_db)):
    """Deletes the whole fleet - every router, BGP link (and with it its
    repeaters and interface bundles) and everything recorded about them:
    incidents, traps, config backups, auto-heal history, notifications -
    plus the Neo4j topology graph. Users, teams and router models are kept.
    Also stops the simulator re-seeding its demo fleet on its next start,
    so the map stays empty until routers are added again."""
    counts = FleetResetOut(
        routers=db.query(Router).count(),
        bgp_links=db.query(BgpPeering).count(),
        incidents=db.query(Incident).count(),
    )
    tables = [m.__tablename__ for m in _FLEET_MODELS]
    if settings.is_postgres:
        # TRUNCATE rather than DELETE: the trap/incident history runs to
        # hundreds of thousands of rows. RESTART IDENTITY so new routers and
        # incidents start again from #1.
        db.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY"))
    else:
        db.query(Incident).update({Incident.root_cause_incident_id: None}, synchronize_session=False)
        db.query(Router).update({Router.parent_router_id: None}, synchronize_session=False)
        for model in _FLEET_MODELS:
            db.query(model).delete(synchronize_session=False)

    setting = db.get(AppSetting, SEED_DEMO_FLEET) or AppSetting(key=SEED_DEMO_FLEET)
    setting.value = "false"
    db.add(setting)
    db.commit()

    # Imported here: app.fiber_faults imports this module (is_enabled).
    from app.fiber_faults import active_faults

    clear_topology()
    active_faults.clear()
    logger.info("Fleet reset: %s", counts)
    await manager.broadcast({"type": "fleet_reset"})
    return counts
