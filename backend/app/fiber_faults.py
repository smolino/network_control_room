"""Fiber-segment fault generator for the "fiber cut -> downstream interface
flap" root-cause correlation scenario (see app.correlation, app.bundles).

Every 15-40s, this knocks out one interior span of fiber - strictly between
two adjacent repeaters on a BGP peering's physical SMF run (see
BgpPeering.repeater_count) - for FAULT_DURATION_SECONDS, then heals it. Two
things happen while a fault is active:

1. A real L1 `Incident` (OPTICAL_ALARM, tied to the peering via
   `peering_id`) is opened and run through the normal auto-heal pipeline
   (NOTIFY_ONLY, same as any other optical alarm) - this is the root cause.
2. One member interface on each side's InterfaceBundle for that peering is
   flapped down, then back up, via the exact same pipeline a real IS-IS
   down/up trap would take (`classify_and_store`) - so the resulting
   ISIS_NBR_DOWN incidents get correlated (see
   app.correlation.try_link_root_cause) and marked symptomatic of the fiber
   cut instead of treated as independent problems, and never get their own
   auto-heal attempt.

The map's orange "faulty segment" overlay (active_faults / the
`fiber_fault` WS message below) is unchanged and purely visual - it now
just reflects a real incident underneath it instead of nothing.
"""

import asyncio
import logging
import random
from datetime import datetime, timezone

from app.db import SessionLocal
from app.models import BgpPeering, BgpSessionStatus, Incident, IncidentStatus, IncidentType, InterfaceBundle, TrapEvent
from app.remediation.engine import maybe_remediate
from app.snmp.classifier import classify_and_store
from app.snmp.oid_map import IF_NAME_OID, ISIS_ADJACENCY_DOWN_OID, ISIS_ADJACENCY_UP_OID, ROUTER_ID_OID
from app.ws import manager

logger = logging.getLogger(__name__)

FAULT_DURATION_SECONDS = 10
FAULT_INTERVAL_MIN_SECONDS = 15
FAULT_INTERVAL_MAX_SECONDS = 40

# Synthetic OID for the TrapEvent audit row backing a fiber-fault's L1
# incident - never added to TRAP_OID_MAP, since it's written directly here
# rather than classified from an actual received trap.
FIBER_CUT_OID = "1.3.6.1.4.1.9.9.9999.4.1"

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


def _bundle_for(db, peering_id: int, router_id: int) -> InterfaceBundle | None:
    return (
        db.query(InterfaceBundle)
        .filter(InterfaceBundle.peering_id == peering_id, InterfaceBundle.router_id == router_id)
        .first()
    )


def _start_fault(db, peering_id: int, segment_index: int) -> int | None:
    """Opens the L1 incident and degrades one bundle member per side.
    Returns the L1 incident's id, or None if the peering no longer exists."""
    peering = db.query(BgpPeering).filter(BgpPeering.id == peering_id).first()
    if peering is None:
        return None

    incident = Incident(
        router_id=peering.router_a_id,
        peering_id=peering.id,
        incident_type=IncidentType.OPTICAL_ALARM,
        status=IncidentStatus.OPEN,
        trap_count=1,
        description=(
            f"Fiber cut on span between routers #{peering.router_a_id} and #{peering.router_b_id} "
            f"(interior segment {segment_index})"
        ),
    )
    db.add(incident)
    db.flush()
    l1_incident_id = incident.id

    db.add(
        TrapEvent(
            router_id=peering.router_a_id,
            oid=FIBER_CUT_OID,
            trap_name="opticalLossOfSignal",
            severity="critical",
            raw_varbinds=str([("segment_index", segment_index), ("peering_id", peering.id)]),
            incident_id=l1_incident_id,
        )
    )
    db.flush()

    # NOTIFY_ONLY, same as any other real OPTICAL_ALARM - still worth
    # running for the config-backup audit trail and WS-visible remediation
    # summary, matching every other incident type's pipeline.
    maybe_remediate(db, peering.router_a, incident)

    for router, bundle in (
        (peering.router_a, _bundle_for(db, peering.id, peering.router_a_id)),
        (peering.router_b, _bundle_for(db, peering.id, peering.router_b_id)),
    ):
        if not bundle or not bundle.members:
            continue
        member = bundle.members[segment_index % len(bundle.members)]
        classify_and_store(
            db,
            source_ip=router.mgmt_ip,
            trap_oid=ISIS_ADJACENCY_DOWN_OID,
            varbinds=[(ROUTER_ID_OID, router.mgmt_ip), (IF_NAME_OID, member.name)],
        )

    return l1_incident_id


def _incident_resolved_payload(incident: Incident) -> dict:
    return {
        "type": "incident_resolved",
        "incident": {
            "id": incident.id,
            "router_id": incident.router_id,
            "incident_type": incident.incident_type.value,
            "status": incident.status.value,
            "trap_count": incident.trap_count,
            "description": incident.description,
            "closed_at": incident.closed_at.isoformat() if incident.closed_at else None,
            "resolved_manually": False,
            "layer": incident.layer.value,
            "peering_id": incident.peering_id,
            "root_cause_incident_id": incident.root_cause_incident_id,
        },
    }


def _resolve_fault(db, peering_id: int, segment_index: int, l1_incident_id: int | None) -> dict | None:
    """Restores both degraded bundle members and resolves the L1 incident.
    Returns an incident_resolved WS payload for the L1 incident, or None if
    it was already gone/resolved by the time this ran."""
    peering = db.query(BgpPeering).filter(BgpPeering.id == peering_id).first()
    if peering is not None:
        for router, bundle in (
            (peering.router_a, _bundle_for(db, peering.id, peering.router_a_id)),
            (peering.router_b, _bundle_for(db, peering.id, peering.router_b_id)),
        ):
            if not bundle or not bundle.members:
                continue
            member = bundle.members[segment_index % len(bundle.members)]
            classify_and_store(
                db,
                source_ip=router.mgmt_ip,
                trap_oid=ISIS_ADJACENCY_UP_OID,
                varbinds=[(ROUTER_ID_OID, router.mgmt_ip), (IF_NAME_OID, member.name)],
            )

    if l1_incident_id is None:
        return None
    incident = db.query(Incident).filter(Incident.id == l1_incident_id).first()
    if incident is None or incident.status != IncidentStatus.OPEN:
        return None

    incident.status = IncidentStatus.RESOLVED
    incident.closed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(incident)
    return _incident_resolved_payload(incident)


async def _run_one_fault(peering_id: int, segment_index: int) -> None:
    active_faults[peering_id] = segment_index
    logger.info("fiber fault: peering=%s segment=%s", peering_id, segment_index)

    db = SessionLocal()
    try:
        l1_incident_id = _start_fault(db, peering_id, segment_index)
    finally:
        db.close()

    await manager.broadcast(
        {"type": "fiber_fault", "action": "start", "peering_id": peering_id, "segment_index": segment_index}
    )

    await asyncio.sleep(FAULT_DURATION_SECONDS)

    db = SessionLocal()
    try:
        resolved_payload = _resolve_fault(db, peering_id, segment_index, l1_incident_id)
    finally:
        db.close()

    active_faults.pop(peering_id, None)
    if resolved_payload:
        await manager.broadcast(resolved_payload)
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
