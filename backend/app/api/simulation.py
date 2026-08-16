from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/simulation", tags=["simulation"])

# In-memory only, not persisted: the simulator container polls this flag
# (see simulator/trap_simulator.py:simulation_control_loop) and pauses/
# resumes sending traps accordingly. A backend restart resets it back to
# enabled, which is the right default for a demo control switch.
_state = {"enabled": True}


class SimulationStatus(BaseModel):
    enabled: bool


@router.get("/status", response_model=SimulationStatus)
def get_status():
    return _state


@router.post("/status", response_model=SimulationStatus)
def set_status(payload: SimulationStatus):
    _state["enabled"] = payload.enabled
    return _state
