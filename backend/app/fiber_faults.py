"""Fiber-segment fault generator for the "fiber cut -> downstream interface
flap" root-cause correlation scenario (see app.correlation, app.bundles).

Every FIBER_FAULT_INTERVAL_MIN/MAX_SECONDS (3-6 min by default), this knocks out one interior span of fiber - strictly between
two adjacent repeaters on a BGP peering's physical SMF run (see
BgpPeering.repeater_count) - for FAULT_DURATION_SECONDS, then heals it. Like
app.snmp.trap_listener, this module is pure mediation now: it only decides
*when* and *which* peering/segment/members are affected, and publishes raw
alarms for it (see app.streaming.producer) - it never touches Postgres for
writes. Two things happen downstream, in app.streaming.correlator, once
those alarms flow through the pipeline:

1. A real L1 `Incident` (OPTICAL_ALARM, tied to the peering via
   `peering_id`) is opened and run through the normal auto-heal pipeline
   (NOTIFY_ONLY, same as any other optical alarm) - this is the root cause.
2. One member interface on each side's InterfaceBundle for that peering is
   flapped down, then back up - the resulting ISIS_NBR_DOWN incidents get
   correlated (see app.incidents.service, app.correlation) via the Neo4j
   topology graph and marked symptomatic of the fiber cut instead of
   treated as independent problems, and never get their own auto-heal
   attempt.

The map's orange "faulty segment" overlay (active_faults / the
`fiber_fault` WS message below) is unrelated to the alarm pipeline and
unchanged - it's still broadcast directly from here, since this module
still runs as a background task inside the `backend` process.
"""

import asyncio
import logging
import random

from app.api.simulation import is_enabled as simulation_enabled
from app.api.simulation import l1_rate
from app.config import settings
from app.db import SessionLocal
from app.models import BgpPeering, BgpSessionStatus, InterfaceBundle
from app.snmp.oid_map import FIBER_CUT_CLEAR_OID, FIBER_CUT_OID, FIBER_SEGMENT_OID, IF_NAME_OID, ISIS_ADJACENCY_DOWN_OID, ISIS_ADJACENCY_UP_OID, ROUTER_ID_OID
from app.streaming.producer import publish_raw_alarm
from app.ws import manager

logger = logging.getLogger(__name__)

FAULT_DURATION_SECONDS = settings.fiber_fault_duration_seconds
FAULT_INTERVAL_MIN_SECONDS = settings.fiber_fault_interval_min_seconds
FAULT_INTERVAL_MAX_SECONDS = settings.fiber_fault_interval_max_seconds
# How often the wait between faults re-reads the Settings > Simulation L1
# slider, so a change applies within seconds rather than after the current
# (possibly long) wait.
RATE_POLL_SECONDS = 2.0

# peering_id -> segment_index (1-based: the fault sits between the
# segment_index-th and (segment_index+1)-th repeater along that peering's
# line, both interior points - see api/bgp.py:_to_peering_out and
# MapView.jsx's repeaterPositions for how that's turned into two lat/lon
# points to draw the orange overlay between).
active_faults: dict[int, int] = {}


def _eligible_peering_ids_and_counts(db) -> dict[str, list[tuple[int, int]]]:
    """Faultable peerings grouped by their router_a's country, so the
    caller can pick a country first - otherwise a region with many links
    (e.g. a freshly bulk-uploaded country) would soak up nearly every
    fault and the rest of the map would almost never see one."""
    peerings = (
        db.query(BgpPeering)
        .filter(BgpPeering.status == BgpSessionStatus.ESTABLISHED, BgpPeering.repeater_count >= 2)
        .all()
    )
    by_region: dict[str, list[tuple[int, int]]] = {}
    for p in peerings:
        if p.id not in active_faults:
            by_region.setdefault(p.router_a.country or "", []).append((p.id, p.repeater_count))
    return by_region


def _bundle_for(db, peering_id: int, router_id: int) -> InterfaceBundle | None:
    return (
        db.query(InterfaceBundle)
        .filter(InterfaceBundle.peering_id == peering_id, InterfaceBundle.router_id == router_id)
        .first()
    )


def _publish_member_events(db, peering_id: int, segment_index: int, trap_oid: str) -> None:
    """Publishes one ISIS adjacency event per side for whichever bundle
    member `segment_index` maps to - shared by fault start (DOWN) and
    resolve (UP), same member both times so the pair actually cancels out."""
    peering = db.query(BgpPeering).filter(BgpPeering.id == peering_id).first()
    if peering is None:
        return
    for router, bundle in (
        (peering.router_a, _bundle_for(db, peering.id, peering.router_a_id)),
        (peering.router_b, _bundle_for(db, peering.id, peering.router_b_id)),
    ):
        if not bundle or not bundle.members:
            continue
        member = bundle.members[segment_index % len(bundle.members)]
        publish_raw_alarm(
            source_ip=router.mgmt_ip,
            trap_oid=trap_oid,
            varbinds=[(ROUTER_ID_OID, router.mgmt_ip), (IF_NAME_OID, member.name)],
        )


def _start_fault(db, peering_id: int, segment_index: int) -> None:
    peering = db.query(BgpPeering).filter(BgpPeering.id == peering_id).first()
    if peering is None:
        return
    publish_raw_alarm(
        source_ip=peering.router_a.mgmt_ip,
        trap_oid=FIBER_CUT_OID,
        varbinds=[(ROUTER_ID_OID, peering.router_a.mgmt_ip), (FIBER_SEGMENT_OID, str(segment_index))],
        peering_id=peering.id,
    )
    _publish_member_events(db, peering_id, segment_index, ISIS_ADJACENCY_DOWN_OID)


def _resolve_fault(db, peering_id: int, segment_index: int) -> None:
    _publish_member_events(db, peering_id, segment_index, ISIS_ADJACENCY_UP_OID)
    peering = db.query(BgpPeering).filter(BgpPeering.id == peering_id).first()
    if peering is None:
        return
    publish_raw_alarm(
        source_ip=peering.router_a.mgmt_ip,
        trap_oid=FIBER_CUT_CLEAR_OID,
        varbinds=[(ROUTER_ID_OID, peering.router_a.mgmt_ip)],
        peering_id=peering.id,
    )


async def _run_one_fault(peering_id: int, segment_index: int) -> None:
    active_faults[peering_id] = segment_index
    logger.info("fiber fault: peering=%s segment=%s", peering_id, segment_index)

    db = SessionLocal()
    try:
        _start_fault(db, peering_id, segment_index)
    finally:
        db.close()

    await manager.broadcast(
        {"type": "fiber_fault", "action": "start", "peering_id": peering_id, "segment_index": segment_index}
    )

    await asyncio.sleep(FAULT_DURATION_SECONDS)

    db = SessionLocal()
    try:
        _resolve_fault(db, peering_id, segment_index)
    finally:
        db.close()

    active_faults.pop(peering_id, None)
    await manager.broadcast(
        {"type": "fiber_fault", "action": "resolved", "peering_id": peering_id, "segment_index": segment_index}
    )


async def _rated_wait(seconds: float) -> None:
    """Waits `seconds` of default-pace time, sped up or slowed down by the
    L1 rate - re-read every RATE_POLL_SECONDS, so moving the slider takes
    effect on the wait already in progress."""
    remaining = seconds
    while remaining > 0:
        step = RATE_POLL_SECONDS
        rate = l1_rate()
        if rate > 0 and remaining / rate < step:
            step = remaining / rate
        await asyncio.sleep(step)
        remaining -= step * rate


async def fiber_fault_loop() -> None:
    """Runs for the lifetime of the app: every FAULT_INTERVAL_MIN/MAX_SECONDS, knocks out one
    interior span of fiber on a random established peering long enough to
    have such a span (random country first, then a random link in it), then
    heals it exactly FAULT_DURATION_SECONDS later.
    Only started when FIBER_FAULT_SIMULATION is on (see app.main), and
    pauses with the Settings > Simulation toggle like the trap simulator.
    The wait between faults is divided by the L1 rate slider (0 = none)."""
    while True:
        await _rated_wait(random.uniform(FAULT_INTERVAL_MIN_SECONDS, FAULT_INTERVAL_MAX_SECONDS))
        if not simulation_enabled():
            continue
        db = SessionLocal()
        try:
            candidates = _eligible_peering_ids_and_counts(db)
        finally:
            db.close()
        if not candidates:
            continue
        peering_id, repeater_count = random.choice(candidates[random.choice(list(candidates))])
        segment_index = random.randint(1, repeater_count - 1)
        asyncio.create_task(_run_one_fault(peering_id, segment_index))
