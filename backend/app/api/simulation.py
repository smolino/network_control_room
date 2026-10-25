from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import require_admin

router = APIRouter(prefix="/api/simulation", tags=["simulation"])

# In-memory only, not persisted: the simulator container polls this flag
# (see simulator/trap_simulator.py:simulation_control_loop) and pauses/
# resumes sending traps accordingly. A backend restart resets it back to
# enabled, which is the right default for a demo control switch.
_state = {"enabled": True}


class SimulationStatus(BaseModel):
    enabled: bool


def is_enabled() -> bool:
    return _state["enabled"]


# Deliberately unauthenticated (see app.main) - the simulator polls it, and
# a single on/off flag reveals nothing worth protecting.
@router.get("/status", response_model=SimulationStatus)
def get_status():
    return _state


@router.post("/status", response_model=SimulationStatus, dependencies=[Depends(require_admin)])
def set_status(payload: SimulationStatus):
    _state["enabled"] = payload.enabled
    return _state
