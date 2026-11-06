"""Optical corridors: import a corridor spec, read it back, ask the topology
graph what a span or device carries. See app.corridors for the model and
seed/corridors/ for the spec files."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from datetime import datetime, timezone

from app import rca, topology_graph
from app.auth import require_admin
from app.corridors import CorridorSpecError, build_plan, corridor_out, delete_corridor, export_spec, import_corridor
from app.db import get_db
from app.models import Corridor, FiberSpan, Incident, IncidentStatus, Router
from app.schemas import CarriedOut, CorridorImportOut, CorridorOut, CorridorSpec, DiagnosisOut
from app.snmp.oid_map import COLD_START_OID, ROUTER_ID_OID
from app.streaming.producer import publish_raw_alarm
from app.ws import manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/corridors", tags=["corridors"])


def _get(db: Session, corridor_id: int) -> Corridor:
    corridor = db.get(Corridor, corridor_id)
    if corridor is None:
        raise HTTPException(status_code=404, detail="Corridor not found")
    return corridor


@router.get("", response_model=list[CorridorOut])
def list_corridors(db: Session = Depends(get_db)):
    return [corridor_out(db, c) for c in db.query(Corridor).order_by(Corridor.id)]


@router.get("/{corridor_id}", response_model=CorridorOut)
def get_corridor(corridor_id: int, db: Session = Depends(get_db)):
    """With every optical path hop by hop and every client circuit."""
    return corridor_out(db, _get(db, corridor_id), detail=True)


@router.get("/{corridor_id}/spec", response_model=CorridorSpec)
def corridor_spec(corridor_id: int, db: Session = Depends(get_db)):
    """The corridor as an import spec - a backup that POST /import rebuilds
    exactly (Add Fleet > Bulk upload > Export)."""
    return export_spec(db, _get(db, corridor_id))


@router.post("/validate", dependencies=[Depends(require_admin)])
def validate_corridor(spec: CorridorSpec):
    """Dry run: what an import would build, without writing anything."""
    try:
        plan = build_plan(spec)
    except CorridorSpecError as exc:
        raise HTTPException(status_code=422, detail=exc.errors)
    return {
        "chain": plan.chain,
        "wavelengths_nm": plan.wavelengths,
        "optical_paths": [p.name for p in plan.paths],
        "warnings": plan.warnings,
    }


@router.post("/import", response_model=CorridorImportOut, dependencies=[Depends(require_admin)])
async def import_corridor_spec(spec: CorridorSpec, prune_l1_peerings: bool = False, db: Session = Depends(get_db)):
    """Creates or updates a corridor from its spec - idempotent, devices
    matched on mgmt_ip. With prune_l1_peerings, also removes L3 peerings
    that have one of the corridor's optical elements on a side."""
    try:
        result, graph_sync = import_corridor(db, spec, prune_l1_peerings=prune_l1_peerings)
    except CorridorSpecError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=exc.errors)
    db.commit()

    # Graph after Postgres, same as api/bgp.py:seed_peerings.
    for peering_id in result.l1_peerings_removed:
        topology_graph.delete_peering_from_neo4j(peering_id)
    for peering, bundle_a, bundle_b in graph_sync:
        db.refresh(bundle_a)
        db.refresh(bundle_b)
        topology_graph.resync_peering_to_neo4j(peering, bundle_a, bundle_b)
    topology_graph.sync_corridor_to_neo4j(db, result.corridor_id)

    # Same as POST /api/routers/seed?send_boot_trap=true: a device created
    # here would otherwise sit at status "unknown" until its first trap.
    created = set(result.devices_created)
    for d in spec.devices:
        if d.hostname in created:
            publish_raw_alarm(source_ip=d.mgmt_ip, trap_oid=COLD_START_OID, varbinds=[(ROUTER_ID_OID, d.mgmt_ip)])

    logger.info("Imported corridor %s: %s", spec.name, result.model_dump(exclude={"warnings"}))
    # Same message the fleet reset sends: every view refetches.
    await manager.broadcast({"type": "fleet_changed"})
    return result


@router.delete("/{corridor_id}", dependencies=[Depends(require_admin)])
async def remove_corridor(corridor_id: int, db: Session = Depends(get_db)):
    corridor = _get(db, corridor_id)
    delete_corridor(db, corridor)
    db.commit()
    topology_graph.delete_corridor_from_neo4j(corridor_id)
    await manager.broadcast({"type": "fleet_changed"})
    return {"ok": True}


@router.get("/{corridor_id}/diagnosis", response_model=list[DiagnosisOut])
def corridor_diagnosis(corridor_id: int, db: Session = Depends(get_db)):
    """What the root-cause engine concludes from the alarms active on this
    corridor right now (read-only - the correlator acts on the same
    analysis, see app.rca)."""
    corridor = _get(db, corridor_id)
    roots = {
        i.rca_key: i.id
        for i in db.query(Incident).filter(Incident.corridor_id == corridor_id, Incident.status == IncidentStatus.OPEN)
    }
    hostnames = {r.id: r.hostname for r in db.query(Router)}
    return [
        DiagnosisOut(
            kind=d.kind, key=d.key, device_id=d.device_id, hostname=hostnames.get(d.device_id, ""), summary=d.summary,
            span_seq=d.span_seq, directions=d.directions,
            evidence=[{"device_id": e[0], "hostname": hostnames.get(e[0], ""), "port": e[1], "condition": e[2]} for e in d.evidence],
            explains=len(d.explains), circuit_ids=sorted(d.circuit_ids), peering_ids=sorted(d.peering_ids),
            root_incident_id=roots.get(d.key),
        )
        for d in rca.diagnose(db, corridor, datetime.now(timezone.utc))
    ]


@router.get("/{corridor_id}/spans/{seq}/carried", response_model=CarriedOut)
def span_carried(corridor_id: int, seq: int, db: Session = Depends(get_db)):
    """What a cut of span `seq` takes down (from the topology graph)."""
    span = db.query(FiberSpan).filter(FiberSpan.corridor_id == corridor_id, FiberSpan.seq == seq).first()
    if span is None:
        raise HTTPException(status_code=404, detail="Span not found")
    return topology_graph.paths_through_span(span.id)


@router.get("/devices/{device_id}/carried", response_model=CarriedOut)
def device_carried(device_id: int, db: Session = Depends(get_db)):
    """What a failure of this optical element takes down."""
    if db.get(Router, device_id) is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return topology_graph.paths_through_device(device_id)


@router.get("/ports/{port_id}/downstream")
def port_downstream(port_id: int, direction: str):
    """Every hop after this port in `direction` (AZ/ZA), and what's carried."""
    if direction not in ("AZ", "ZA"):
        raise HTTPException(status_code=422, detail="direction must be AZ or ZA")
    return topology_graph.downstream_of(port_id, direction)


@router.get("/ports/{port_id}/upstream-span")
def port_upstream_span(port_id: int, direction: str):
    """The span feeding this port in `direction`, and its upstream element."""
    if direction not in ("AZ", "ZA"):
        raise HTTPException(status_code=422, detail="direction must be AZ or ZA")
    return topology_graph.upstream_span(port_id, direction)
