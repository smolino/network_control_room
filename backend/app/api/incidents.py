from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    AlarmLayer,
    Incident,
    IncidentNotification,
    IncidentStatus,
    IncidentType,
    RemediationAction,
    Router,
    RouterStatus,
    RouterType,
    Team,
)
from app.notifications.email import send_email
from app.remediation.engine import needs_attention_router_ids
from app.remediation.guidance import generate_analysis
from app.schemas import (
    BulkResolveIn,
    IncidentAnalysisOut,
    IncidentNotificationOut,
    IncidentOut,
    NotifyTeamIn,
    RemediationActionOut,
    RemediationSummary,
)
from app.ws import manager

router = APIRouter(prefix="/api/incidents", tags=["incidents"])


def _latest_remediation_by_incident(db: Session, incident_ids: list[int]) -> dict[int, RemediationAction]:
    if not incident_ids:
        return {}
    rows = (
        db.query(RemediationAction)
        .filter(RemediationAction.incident_id.in_(incident_ids))
        .order_by(RemediationAction.started_at.desc())
        .all()
    )
    latest: dict[int, RemediationAction] = {}
    for row in rows:
        latest.setdefault(row.incident_id, row)
    return latest


def _to_incident_out(incident: Incident, latest_by_incident: dict[int, RemediationAction]) -> IncidentOut:
    out = IncidentOut.model_validate(incident)
    action = latest_by_incident.get(incident.id)
    if action:
        out.remediation = RemediationSummary.model_validate(action)
    return out


@router.get("", response_model=list[IncidentOut])
def list_incidents(
    status: IncidentStatus | None = None,
    incident_type: IncidentType | None = None,
    router_id: int | None = None,
    router_type: RouterType | None = None,
    layer: AlarmLayer | None = None,
    # Excludes incidents already linked as a symptom of an open L1 incident
    # (see app.correlation) - the "suppress symptomatic from the top-level
    # view, keep it visible on drill-down" behavior the Incident List's
    # "Hide symptomatic" filter uses.
    root_cause_only: bool = False,
    limit: int = 200,
    db: Session = Depends(get_db),
):
    query = db.query(Incident)
    if status is not None:
        query = query.filter(Incident.status == status)
    if incident_type is not None:
        query = query.filter(Incident.incident_type == incident_type)
    if router_id is not None:
        query = query.filter(Incident.router_id == router_id)
    if router_type is not None:
        query = query.join(Router, Incident.router_id == Router.id).filter(Router.router_type == router_type)
    if layer is not None:
        query = query.filter(Incident.layer == layer)
    if root_cause_only:
        query = query.filter(Incident.root_cause_incident_id.is_(None))
    incidents = query.order_by(Incident.updated_at.desc()).limit(limit).all()

    latest_by_incident = _latest_remediation_by_incident(db, [i.id for i in incidents])
    return [_to_incident_out(i, latest_by_incident) for i in incidents]


@router.get("/{incident_id}", response_model=IncidentOut)
def get_incident(incident_id: int, db: Session = Depends(get_db)):
    obj = db.query(Incident).filter(Incident.id == incident_id).first()
    if obj is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    latest_by_incident = _latest_remediation_by_incident(db, [obj.id])
    return _to_incident_out(obj, latest_by_incident)


@router.get("/{incident_id}/tree")
def get_incident_tree(incident_id: int, db: Session = Depends(get_db)):
    """Root cause + everything correlated to it (see app.correlation) - the
    drill-down panel's incident tree. Walks up to the true root first if
    the given incident is itself a symptom, then returns every incident
    (across any router) currently linked to that root."""
    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")

    root = incident
    if root.root_cause_incident_id is not None:
        root = db.query(Incident).filter(Incident.id == root.root_cause_incident_id).first() or incident

    symptomatic = db.query(Incident).filter(Incident.root_cause_incident_id == root.id).order_by(Incident.opened_at.asc()).all()
    latest_by_incident = _latest_remediation_by_incident(db, [root.id] + [i.id for i in symptomatic])
    return {
        "root_cause": _to_incident_out(root, latest_by_incident),
        "symptomatic": [_to_incident_out(i, latest_by_incident) for i in symptomatic],
    }


@router.get("/{incident_id}/remediation", response_model=list[RemediationActionOut])
def get_incident_remediation(incident_id: int, db: Session = Depends(get_db)):
    return (
        db.query(RemediationAction)
        .filter(RemediationAction.incident_id == incident_id)
        .order_by(RemediationAction.started_at.desc())
        .all()
    )


@router.get("/{incident_id}/analysis", response_model=IncidentAnalysisOut)
def get_incident_analysis(incident_id: int, db: Session = Depends(get_db)):
    """Human-facing description + suggested fix for the Human Review tab -
    the "Describe issue & suggest solution" button. See
    app/remediation/guidance.py for how this is generated."""
    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    router_obj = db.query(Router).filter(Router.id == incident.router_id).first()
    if router_obj is None:
        raise HTTPException(status_code=404, detail="Router not found")
    return generate_analysis(incident, router_obj)


@router.get("/{incident_id}/notifications", response_model=list[IncidentNotificationOut])
def get_incident_notifications(incident_id: int, db: Session = Depends(get_db)):
    return (
        db.query(IncidentNotification)
        .filter(IncidentNotification.incident_id == incident_id)
        .order_by(IncidentNotification.sent_at.desc())
        .all()
    )


@router.post("/{incident_id}/notify", response_model=IncidentNotificationOut)
def notify_team(incident_id: int, payload: NotifyTeamIn, db: Session = Depends(get_db)):
    """Sends (or, without SMTP configured, records as simulated) the
    subject/body the operator reviewed and edited in the Human Review tab
    to the chosen SOC/maintenance team."""
    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    team = db.query(Team).filter(Team.id == payload.team_id).first()
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")

    status, error = send_email(team.email, payload.subject, payload.body)

    notification = IncidentNotification(
        incident_id=incident.id,
        team_id=team.id,
        subject=payload.subject,
        body=payload.body,
        status=status,
        error=error,
    )
    db.add(notification)
    db.commit()
    db.refresh(notification)
    return notification


def _incident_ws_payload(incident: Incident) -> dict:
    return {
        "id": incident.id,
        "router_id": incident.router_id,
        "incident_type": incident.incident_type.value,
        "status": incident.status.value,
        "trap_count": incident.trap_count,
        "description": incident.description,
        "closed_at": incident.closed_at.isoformat() if incident.closed_at else None,
        "resolved_manually": incident.resolved_manually,
        "layer": incident.layer.value,
        "peering_id": incident.peering_id,
        "root_cause_incident_id": incident.root_cause_incident_id,
    }


def _mark_resolved(incident: Incident, now: datetime) -> bool:
    """Resolves one incident if it's currently open. Returns True if this
    call actually changed anything, so callers only broadcast/re-check
    needs_attention for incidents that really moved."""
    if incident.status != IncidentStatus.OPEN:
        return False
    incident.status = IncidentStatus.RESOLVED
    incident.closed_at = now
    incident.resolved_manually = True
    return True


def _reset_router_status_if_clear(db: Session, router_id: int, now: datetime) -> Router | None:
    """After manually resolving a LINK_DOWN/LINK_FLAP incident, flips the
    router back to UP if no other open LINK_DOWN/LINK_FLAP incidents remain
    for it - the same "still_open" check evaluate_link_event runs for a real
    linkUp trap (see flapping.py). Without this, a router resolved this way
    stays DOWN/FLAPPING (and its map line stays red) forever, since nothing
    else would ever clear it. Returns the router only if its status actually
    changed, so callers only broadcast/re-check needs_attention when
    something visibly moved."""
    still_open = (
        db.query(Incident)
        .filter(
            Incident.router_id == router_id,
            Incident.status == IncidentStatus.OPEN,
            Incident.incident_type.in_([IncidentType.LINK_DOWN, IncidentType.LINK_FLAP]),
        )
        .count()
    )
    if still_open > 0:
        return None

    db_router = db.query(Router).filter(Router.id == router_id).first()
    if db_router is None or db_router.status == RouterStatus.UP:
        return None
    db_router.status = RouterStatus.UP
    db.flush()
    return db_router


def _router_ws_payload(db_router: Router, needs_attention: bool) -> dict:
    return {
        "id": db_router.id,
        "status": db_router.status.value,
        "needs_attention": needs_attention,
        "router_type": db_router.router_type.value,
    }


@router.post("/{incident_id}/resolve", response_model=IncidentOut)
async def resolve_incident(incident_id: int, db: Session = Depends(get_db)):
    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")

    now = datetime.now(timezone.utc)
    changed = _mark_resolved(incident, now)

    updated_router = None
    if changed and incident.incident_type in (IncidentType.LINK_DOWN, IncidentType.LINK_FLAP):
        updated_router = _reset_router_status_if_clear(db, incident.router_id, now)

    db.commit()
    db.refresh(incident)
    if updated_router:
        db.refresh(updated_router)

    if changed:
        payload = {"type": "incident_resolved", "incident": _incident_ws_payload(incident)}
        if updated_router:
            attention_ids = needs_attention_router_ids(db, [updated_router.id])
            payload["router"] = _router_ws_payload(updated_router, updated_router.id in attention_ids)
        await manager.broadcast(payload)

    latest_by_incident = _latest_remediation_by_incident(db, [incident.id])
    return _to_incident_out(incident, latest_by_incident)


async def _resolve_incidents_and_broadcast(db: Session, incidents: list[Incident], now: datetime) -> None:
    """Shared by bulk_resolve_incidents (a caller-picked id list) and
    resolve_all_open_incidents (every open incident system-wide): marks each
    resolved, resets router status where a LINK_DOWN/LINK_FLAP incident was
    the last one keeping it down, then broadcasts one incident_resolved
    message per incident that actually changed."""
    changed = [incident for incident in incidents if _mark_resolved(incident, now)]

    updated_routers: dict[int, Router] = {}
    for incident in changed:
        if incident.incident_type in (IncidentType.LINK_DOWN, IncidentType.LINK_FLAP):
            updated_router = _reset_router_status_if_clear(db, incident.router_id, now)
            if updated_router:
                updated_routers[updated_router.id] = updated_router

    db.commit()
    for incident in incidents:
        db.refresh(incident)
    for updated_router in updated_routers.values():
        db.refresh(updated_router)

    attention_ids = needs_attention_router_ids(db, list(updated_routers.keys())) if updated_routers else set()
    for incident in changed:
        payload = {"type": "incident_resolved", "incident": _incident_ws_payload(incident)}
        updated_router = updated_routers.get(incident.router_id)
        if updated_router:
            payload["router"] = _router_ws_payload(updated_router, updated_router.id in attention_ids)
        await manager.broadcast(payload)


@router.post("/resolve", response_model=list[IncidentOut])
async def bulk_resolve_incidents(payload: BulkResolveIn, db: Session = Depends(get_db)):
    """Resolves every OPEN incident among the given ids in one call - the
    UI uses this for "resolve all currently filtered", e.g. clearing out
    every incident under the "Needs manual review" filter at once."""
    incidents = db.query(Incident).filter(Incident.id.in_(payload.incident_ids)).all()
    now = datetime.now(timezone.utc)
    await _resolve_incidents_and_broadcast(db, incidents, now)

    latest_by_incident = _latest_remediation_by_incident(db, [i.id for i in incidents])
    return [_to_incident_out(i, latest_by_incident) for i in incidents]


@router.post("/resolve-all", response_model=list[IncidentOut])
async def resolve_all_open_incidents(db: Session = Depends(get_db)):
    """Resolves every OPEN incident system-wide. Distinct from bulk_resolve_
    incidents above: the frontend's incident list is capped (see the `limit`
    default on list_incidents) and often only holds a fraction of what's
    actually open, so a "reset everything" action has to query the DB
    directly rather than resolve whatever ids the client happens to have
    loaded."""
    incidents = db.query(Incident).filter(Incident.status == IncidentStatus.OPEN).all()
    now = datetime.now(timezone.utc)
    await _resolve_incidents_and_broadcast(db, incidents, now)

    latest_by_incident = _latest_remediation_by_incident(db, [i.id for i in incidents])
    return [_to_incident_out(i, latest_by_incident) for i in incidents]
