from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.bundles import isis_net
from app.config import settings
from app.db import get_db
from app.models import (
    BgpPeering,
    Incident,
    IncidentNotification,
    Interface,
    InterfaceBundle,
    RemediationAction,
    Router,
    RouterConfigBackup,
    RouterType,
    TrapEvent,
)
from app.remediation.engine import needs_attention_router_ids
from app.schemas import (
    RemediationActionOut,
    RouterConfigBackupOut,
    RouterIn,
    RouterNearestOut,
    RouterOut,
    TrapEventOut,
)
from app.snmp.oid_map import COLD_START_OID, ROUTER_ID_OID
from app.streaming.producer import publish_raw_alarm
from app.topology_graph import delete_peering_from_neo4j, delete_router_from_neo4j

router = APIRouter(prefix="/api/routers", tags=["routers"])


def _to_router_out(r: Router, attention_ids: set[int]) -> RouterOut:
    out = RouterOut.model_validate(r)
    out.needs_attention = r.id in attention_ids
    out.isis_net = isis_net(r) if r.router_type == RouterType.PRIMARY else None
    return out


@router.get("", response_model=list[RouterOut])
def list_routers(db: Session = Depends(get_db)):
    routers = db.query(Router).order_by(Router.id).all()
    attention_ids = needs_attention_router_ids(db)
    return [_to_router_out(r, attention_ids) for r in routers]


@router.get("/{router_id}", response_model=RouterOut)
def get_router(router_id: int, db: Session = Depends(get_db)):
    obj = db.query(Router).filter(Router.id == router_id).first()
    if obj is None:
        raise HTTPException(status_code=404, detail="Router not found")
    attention_ids = needs_attention_router_ids(db, [router_id])
    return _to_router_out(obj, attention_ids)


@router.delete("/{router_id}")
def delete_router(router_id: int, db: Session = Depends(get_db)):
    """Removes a router along with everything that has no meaning once it's
    gone: its trap/incident/backup/remediation history, any BGP peering it
    was a side of (and that peering's interface bundles, on both sides), and
    the mirror of those peerings in the Neo4j topology graph (see
    app.topology_graph). None of these FKs cascade at the DB level (no
    alembic/ondelete in this project - see app.db.init_db), so each has to
    be cleared explicitly, children first."""
    obj = db.query(Router).filter(Router.id == router_id).first()
    if obj is None:
        raise HTTPException(status_code=404, detail="Router not found")

    incident_ids = [row[0] for row in db.query(Incident.id).filter(Incident.router_id == router_id).all()]

    # Other incidents (anywhere) that named one of this router's incidents
    # as their root cause, or reroute customer routers onto no parent -
    # both self-referential FKs that would otherwise block the deletes below.
    db.query(Incident).filter(Incident.root_cause_incident_id.in_(incident_ids)).update(
        {"root_cause_incident_id": None}, synchronize_session=False
    )
    db.query(Router).filter(Router.parent_router_id == router_id).update(
        {"parent_router_id": None}, synchronize_session=False
    )

    db.query(IncidentNotification).filter(IncidentNotification.incident_id.in_(incident_ids)).delete(
        synchronize_session=False
    )
    db.query(RemediationAction).filter(RemediationAction.router_id == router_id).delete(synchronize_session=False)
    db.query(RouterConfigBackup).filter(RouterConfigBackup.router_id == router_id).delete(synchronize_session=False)
    db.query(TrapEvent).filter(TrapEvent.router_id == router_id).delete(synchronize_session=False)
    db.query(Incident).filter(Incident.router_id == router_id).delete(synchronize_session=False)

    peerings = (
        db.query(BgpPeering)
        .filter(or_(BgpPeering.router_a_id == router_id, BgpPeering.router_b_id == router_id))
        .all()
    )
    peering_ids = [p.id for p in peerings]
    for peering in peerings:
        # A still-open L1 incident on the *other* router of this peering
        # can point at it too (see Incident.peering_id) - detach rather
        # than delete, that incident's own history isn't this router's.
        db.query(Incident).filter(Incident.peering_id == peering.id).update(
            {"peering_id": None}, synchronize_session=False
        )
        bundle_ids = [
            row[0] for row in db.query(InterfaceBundle.id).filter(InterfaceBundle.peering_id == peering.id).all()
        ]
        db.query(Interface).filter(Interface.bundle_id.in_(bundle_ids)).delete(synchronize_session=False)
        db.query(InterfaceBundle).filter(InterfaceBundle.peering_id == peering.id).delete(synchronize_session=False)
        db.delete(peering)

    db.delete(obj)
    db.commit()

    # Mirror into the Neo4j topology graph only after the Postgres commit
    # succeeds, same ordering seed_peerings uses (app/api/bgp.py) - so the
    # graph never gets ahead of the alarm store's own view of what exists.
    for peering_id in peering_ids:
        delete_peering_from_neo4j(peering_id)
    delete_router_from_neo4j(router_id)
    return {"ok": True}


@router.get("/{router_id}/nearest", response_model=list[RouterNearestOut])
def get_nearest_routers(router_id: int, limit: int = 5, db: Session = Depends(get_db)):
    """The `limit` geographically closest other primaries, nearest first -
    candidate backup/reroute sites for an operator looking at a router
    with degraded peerings. Great-circle distance via PostGIS ST_Distance
    on Router.location, not the BGP mesh - so it surfaces the nearest
    site regardless of whether it's actually peered with this router."""
    if not settings.is_postgres:
        raise HTTPException(status_code=501, detail="Nearest-router lookup requires Postgres+PostGIS")

    obj = db.query(Router).filter(Router.id == router_id).first()
    if obj is None:
        raise HTTPException(status_code=404, detail="Router not found")

    distance_m = func.ST_Distance(Router.location, obj.location).label("distance_m")
    rows = (
        db.query(Router, distance_m)
        .filter(Router.id != router_id, Router.router_type == RouterType.PRIMARY)
        .order_by(distance_m)
        .limit(limit)
        .all()
    )
    attention_ids = needs_attention_router_ids(db, [r.id for r, _ in rows])
    return [
        RouterNearestOut(**_to_router_out(r, attention_ids).model_dump(), distance_km=round(dist_m / 1000, 1))
        for r, dist_m in rows
    ]


@router.get("/{router_id}/traps", response_model=list[TrapEventOut])
def get_router_traps(router_id: int, limit: int = 100, db: Session = Depends(get_db)):
    return (
        db.query(TrapEvent)
        .filter(TrapEvent.router_id == router_id)
        .order_by(TrapEvent.received_at.desc())
        .limit(limit)
        .all()
    )


@router.get("/{router_id}/backups", response_model=list[RouterConfigBackupOut])
def get_router_backups(router_id: int, limit: int = 50, db: Session = Depends(get_db)):
    """Config backups taken before each auto-heal action on this router."""
    return (
        db.query(RouterConfigBackup)
        .filter(RouterConfigBackup.router_id == router_id)
        .order_by(RouterConfigBackup.taken_at.desc())
        .limit(limit)
        .all()
    )


@router.get("/{router_id}/remediation", response_model=list[RemediationActionOut])
def get_router_remediation(router_id: int, limit: int = 50, db: Session = Depends(get_db)):
    """Auto-heal action history for this router, most recent first."""
    return (
        db.query(RemediationAction)
        .filter(RemediationAction.router_id == router_id)
        .order_by(RemediationAction.started_at.desc())
        .limit(limit)
        .all()
    )


@router.post("/seed", response_model=list[RouterOut])
def seed_routers(routers_in: list[RouterIn], send_boot_trap: bool = False, db: Session = Depends(get_db)):
    """Bulk idempotent insert used by the trap simulator on startup. Customer
    routers carry `parent_mgmt_ip` instead of a DB id (the simulator doesn't
    know one yet) - it's resolved to `parent_router_id` here, so primaries
    must already exist (seed them first) by the time their customers are
    seeded.

    `send_boot_trap` publishes a synthetic coldStart for each row actually
    created by this call (not pre-existing ones, and not one a concurrent
    request already won the race to create - see the IntegrityError branch
    below) - otherwise a router seeded outside the simulator's own boot
    sequence (e.g. the Add Fleet UI) would sit at RouterStatus.UNKNOWN
    forever, since that's the only trap that ever flips it (see
    app.snmp.classifier). Defaults to False so the simulator's own startup
    seed - immediately followed by its own real boot_sequence traps for the
    same routers - doesn't double up."""
    created_or_existing = []
    newly_created: list[Router] = []
    for r in routers_in:
        existing = db.query(Router).filter(Router.mgmt_ip == r.mgmt_ip).first()
        if existing:
            created_or_existing.append(existing)
            continue

        data = r.model_dump(exclude={"parent_mgmt_ip"})
        parent_router_id = None
        if r.parent_mgmt_ip:
            parent = db.query(Router).filter(Router.mgmt_ip == r.parent_mgmt_ip).first()
            parent_router_id = parent.id if parent else None

        obj = Router(**data, parent_router_id=parent_router_id)
        db.add(obj)
        try:
            with db.begin_nested():
                db.flush()
            newly_created.append(obj)
        except IntegrityError:
            # Lost a race against a concurrent seed request for this same
            # mgmt_ip (e.g. the simulator retrying after a slow response) -
            # not a real conflict, just use the row that won (which is
            # responsible for its own boot trap, not us).
            db.expunge(obj)
            obj = db.query(Router).filter(Router.mgmt_ip == r.mgmt_ip).first()
        created_or_existing.append(obj)
    db.commit()
    for obj in created_or_existing:
        db.refresh(obj)

    if send_boot_trap:
        for obj in newly_created:
            publish_raw_alarm(source_ip=obj.mgmt_ip, trap_oid=COLD_START_OID, varbinds=[(ROUTER_ID_OID, obj.mgmt_ip)])

    attention_ids = needs_attention_router_ids(db, [obj.id for obj in created_or_existing])
    return [_to_router_out(obj, attention_ids) for obj in created_or_existing]
