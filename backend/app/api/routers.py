from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.bundles import isis_net
from app.config import settings
from app.db import get_db
from app.models import RemediationAction, Router, RouterConfigBackup, RouterType, TrapEvent
from app.remediation.engine import needs_attention_router_ids
from app.schemas import (
    RemediationActionOut,
    RouterConfigBackupOut,
    RouterIn,
    RouterNearestOut,
    RouterOut,
    TrapEventOut,
)

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
def seed_routers(routers_in: list[RouterIn], db: Session = Depends(get_db)):
    """Bulk idempotent insert used by the trap simulator on startup. Customer
    routers carry `parent_mgmt_ip` instead of a DB id (the simulator doesn't
    know one yet) - it's resolved to `parent_router_id` here, so primaries
    must already exist (seed them first) by the time their customers are
    seeded."""
    created_or_existing = []
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
        except IntegrityError:
            # Lost a race against a concurrent seed request for this same
            # mgmt_ip (e.g. the simulator retrying after a slow response) -
            # not a real conflict, just use the row that won.
            db.expunge(obj)
            obj = db.query(Router).filter(Router.mgmt_ip == r.mgmt_ip).first()
        created_or_existing.append(obj)
    db.commit()
    for obj in created_or_existing:
        db.refresh(obj)
    attention_ids = needs_attention_router_ids(db, [obj.id for obj in created_or_existing])
    return [_to_router_out(obj, attention_ids) for obj in created_or_existing]
