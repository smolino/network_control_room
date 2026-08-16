"""Chaos-demo generator for fiber-segment faults strictly between two
adjacent repeaters on a BGP peering's physical SMF run (see
BgpPeering.repeater_count). Deliberately not modeled as a full Incident:
it never touches BgpPeering.status - a fault on one interior span isn't
the same as the whole session dropping - and it self-heals on a fixed
10-second timer rather than the probabilistic recovery real incidents get,
since this exists purely to demonstrate the map's orange "faulty segment"
rendering. State is in-memory only (peering_id -> segment_index), the
same "cosmetic, resets on restart" pattern app.api.simulation already uses
for the pause/resume toggle.
"""

import asyncio
import logging
import random

from app.db import SessionLocal
from app.models import BgpPeering, BgpSessionStatus
from app.ws import manager

logger = logging.getLogger(__name__)

FAULT_DURATION_SECONDS = 10
FAULT_INTERVAL_MIN_SECONDS = 15
FAULT_INTERVAL_MAX_SECONDS = 40

# peering_id -> segment_index (1-based: the fault sits between the
# segment_index-th and (segment_index+1)-th repeater along that peering's
# line, both interior points - see api/bgp.py:_to_peering_out and
# MapView.jsx's repeaterPositions for how that's turned into two lat/lon
# points to draw the orange overlay between).
active_faults: dict[int, int] = {}


def _eligible_peering_ids_and_counts(db) -> list[tuple[int, int]]:
    peerings = (
        db.query(BgpPeering)
        .filter(BgpPeering.status == BgpSessionStatus.ESTABLISHED, BgpPeering.repeater_count >= 2)
        .all()
    )
    return [(p.id, p.repeater_count) for p in peerings if p.id not in active_faults]


async def _run_one_fault(peering_id: int, segment_index: int) -> None:
    active_faults[peering_id] = segment_index
    logger.info("fiber fault: peering=%s segment=%s", peering_id, segment_index)
    await manager.broadcast(
        {"type": "fiber_fault", "action": "start", "peering_id": peering_id, "segment_index": segment_index}
    )
    await asyncio.sleep(FAULT_DURATION_SECONDS)
    active_faults.pop(peering_id, None)
    await manager.broadcast(
        {"type": "fiber_fault", "action": "resolved", "peering_id": peering_id, "segment_index": segment_index}
    )


async def fiber_fault_loop() -> None:
    """Runs for the lifetime of the app: every 15-40s, knocks out one
    interior span of fiber on a random established peering long enough to
    have such a span, then heals it exactly FAULT_DURATION_SECONDS later."""
    while True:
        await asyncio.sleep(random.uniform(FAULT_INTERVAL_MIN_SECONDS, FAULT_INTERVAL_MAX_SECONDS))
        db = SessionLocal()
        try:
            candidates = _eligible_peering_ids_and_counts(db)
        finally:
            db.close()
        if not candidates:
            continue
        peering_id, repeater_count = random.choice(candidates)
        segment_index = random.randint(1, repeater_count - 1)
        asyncio.create_task(_run_one_fault(peering_id, segment_index))
