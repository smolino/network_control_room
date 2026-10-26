from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import Principal, require_admin, require_operator
from app.config import settings
from app.db import get_db
from app.impact import compute_impacts
from app.incidents import service as incident_service
from app.models import (
    AlarmLayer,
    BgpPeering,
    BgpSessionStatus,
    BundleStatus,
    Incident,
    IncidentNotification,
    IncidentStage,
    IncidentStatus,
    IncidentType,
    Interface,
    InterfaceBundle,
    NotificationStatus,
    RemediationAction,
    Router,
    RouterType,
    Team,
)
from app.notifications.email import send_email
from app.remediation.guidance import generate_analysis
from app.schemas import (
    BulkResolveIn,
    IncidentAnalysisOut,
    IncidentImpactDetail,
    IncidentNotificationOut,
    IncidentOut,
    NotifyTeamIn,
    RemediationActionOut,
    ResolveIn,
)
from app.ws import manager

router = APIRouter(prefix="/api/incidents", tags=["incidents"])


def _get_incident(db: Session, incident_id: int) -> Incident:
    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


async def _commit_and_broadcast(db: Session) -> None:
    """Commits whatever the service changed on this request and broadcasts
    it as one incident_updated event (see incident_service.pop_event)."""
    db.commit()
    event = incident_service.pop_event(db)
    if event:
        await manager.broadcast(event)


@router.get("", response_model=list[IncidentOut])
def list_incidents(
    status: IncidentStatus | None = None,
    stage: IncidentStage | None = None,
    incident_type: IncidentType | None = None,
    router_id: int | None = None,
    router_type: RouterType | None = None,
    layer: AlarmLayer | None = None,
    # Excludes incidents linked as a symptom of an L1 incident (see
    # app.correlation) - the "suppress symptomatic from the top-level view,
    # keep it visible on drill-down" behavior.
    root_cause_only: bool = False,
    limit: int = 200,
    db: Session = Depends(get_db),
):
    query = db.query(Incident)
    if status is not None:
        query = query.filter(Incident.status == status)
    if stage is not None:
        query = query.filter(Incident.stage == stage)
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
    return incident_service.serialize(db, incidents)


@router.get("/queue", response_model=list[IncidentOut])
def get_queue(limit: int = 500, db: Session = Depends(get_db)):
    """Every open incident a person has to act on (triage, acknowledged,
    dispatched), most urgent first - the Human Review board."""
    return incident_service.serialize(db, incident_service.queue(db, limit))


@router.get("/{incident_id}", response_model=IncidentOut)
def get_incident(incident_id: int, db: Session = Depends(get_db)):
    return incident_service.serialize(db, [_get_incident(db, incident_id)])[0]


@router.get("/{incident_id}/impact", response_model=IncidentImpactDetail | None)
def get_incident_impact(incident_id: int, db: Session = Depends(get_db)):
    """Blast radius of this incident right now, including every affected
    router id (for the map highlight) - null once it's resolved or if its
    type isn't service-affecting. See app.impact."""
    incident = _get_incident(db, incident_id)
    impact = compute_impacts(db, [incident]).get(incident.id)
    return impact.detail() if impact else None


@router.get("/{incident_id}/tree")
def get_incident_tree(incident_id: int, db: Session = Depends(get_db)):
    """Root cause + everything correlated to it (see app.correlation) - the
    drill-down panel's incident tree. Walks up to the true root first if
    the given incident is itself a symptom, then returns every incident
    (across any router) currently linked to that root."""
    incident = _get_incident(db, incident_id)
    root = incident
    if root.root_cause_incident_id is not None:
        root = db.query(Incident).filter(Incident.id == root.root_cause_incident_id).first() or incident

    symptomatic = db.query(Incident).filter(Incident.root_cause_incident_id == root.id).order_by(Incident.opened_at.asc()).all()
    out = incident_service.serialize(db, [root] + symptomatic)
    return {"root_cause": out[0], "symptomatic": out[1:]}


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
    """Human-facing description + suggested fix for the Human Review board.
    See app/remediation/guidance.py for how this is generated."""
    incident = _get_incident(db, incident_id)
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


@router.post("/{incident_id}/ack", response_model=IncidentOut)
async def acknowledge_incident(
    incident_id: int, principal: Principal = Depends(require_operator), db: Session = Depends(get_db)
):
    """The caller takes ownership of the incident."""
    incident = _get_incident(db, incident_id)
    try:
        incident_service.acknowledge(db, incident, principal, datetime.now(timezone.utc))
    except incident_service.WorkflowError as err:
        raise HTTPException(status_code=409, detail=str(err))
    await _commit_and_broadcast(db)
    return incident_service.serialize(db, [incident])[0]


@router.post("/{incident_id}/notify", response_model=IncidentNotificationOut)
async def notify_team(
    incident_id: int,
    payload: NotifyTeamIn,
    principal: Principal = Depends(require_operator),
    db: Session = Depends(get_db),
):
    """Sends (or, without SMTP configured, records as simulated) the
    subject/body the operator reviewed and edited on the Human Review board
    to the chosen SOC/maintenance team, and moves the incident to
    DISPATCHED - it's now waiting on that team. A failed send is recorded
    but leaves the stage alone, since nobody was actually handed it."""
    incident = _get_incident(db, incident_id)
    team = db.query(Team).filter(Team.id == payload.team_id).first()
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")
    try:
        incident_service.require_actionable(incident)
    except incident_service.WorkflowError as err:
        raise HTTPException(status_code=409, detail=str(err))

    body = payload.body
    if settings.public_base_url:
        body += f"\n\nIncident #{incident.id}: {settings.public_base_url}/?incident={incident.id}"
    status, error = send_email(team.email, payload.subject, body)
    if status != NotificationStatus.FAILED:
        incident_service.dispatch(db, incident, principal, datetime.now(timezone.utc))

    notification = IncidentNotification(
        incident_id=incident.id,
        team_id=team.id,
        subject=payload.subject,
        body=body,
        status=status,
        error=error,
    )
    db.add(notification)
    await _commit_and_broadcast(db)
    db.refresh(notification)
    return notification


@router.post("/{incident_id}/resolve", response_model=list[IncidentOut])
async def resolve_incident(
    incident_id: int,
    payload: ResolveIn,
    principal: Principal = Depends(require_operator),
    db: Session = Depends(get_db),
):
    """Resolves the incident and its symptoms. Returns everything that
    changed (the incident first)."""
    incident = _get_incident(db, incident_id)
    if incident.status != IncidentStatus.OPEN:
        raise HTTPException(status_code=409, detail="Incident is already resolved")
    if incident.root_cause_incident_id is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Incident is a symptom of #{incident.root_cause_incident_id} - resolve that one instead",
        )
    try:
        incident_service.resolve_by_user(db, [incident], principal, payload.reason, payload.note, datetime.now(timezone.utc))
    except incident_service.WorkflowError as err:
        raise HTTPException(status_code=422, detail=str(err))
    await _commit_and_broadcast(db)
    changed = [incident] + db.query(Incident).filter(Incident.root_cause_incident_id == incident.id).all()
    return incident_service.serialize(db, changed)


@router.post("/resolve", response_model=list[IncidentOut])
async def bulk_resolve_incidents(
    payload: BulkResolveIn, principal: Principal = Depends(require_operator), db: Session = Depends(get_db)
):
    """Resolves every open root incident among the given ids (symptoms go
    with their root; symptom ids passed on their own are skipped). Returns
    the root incidents that were actually resolved."""
    incidents = db.query(Incident).filter(Incident.id.in_(payload.incident_ids)).all()
    try:
        changed = incident_service.resolve_by_user(
            db, incidents, principal, payload.reason, payload.note, datetime.now(timezone.utc)
        )
    except incident_service.WorkflowError as err:
        raise HTTPException(status_code=422, detail=str(err))
    await _commit_and_broadcast(db)
    return incident_service.serialize(db, changed)


def _restore_link_state(db: Session, now: datetime) -> None:
    """Brings every bundle member's IS-IS adjacency, every bundle and every
    BGP peering back up. Resolving incidents alone leaves this state behind
    (e.g. a fiber fault interrupted mid-way by a backend restart never sends
    its "up" events), so a peering would stay red with a reroute path on the
    map after a full reset. The frontend picks this up on its periodic
    resync - no per-peering broadcast needed."""
    db.query(Interface).filter(Interface.isis_adjacency_up.is_(False)).update({Interface.isis_adjacency_up: True, Interface.last_changed_at: now}, synchronize_session=False)
    db.query(InterfaceBundle).filter(InterfaceBundle.status != BundleStatus.UP).update(
        {InterfaceBundle.status: BundleStatus.UP, InterfaceBundle.last_changed_at: now}, synchronize_session=False
    )
    db.query(BgpPeering).filter(BgpPeering.status != BgpSessionStatus.ESTABLISHED).update(
        {BgpPeering.status: BgpSessionStatus.ESTABLISHED, BgpPeering.last_changed_at: now}, synchronize_session=False
    )
    db.commit()


@router.post("/resolve-all", response_model=list[IncidentOut])
async def resolve_all_open_incidents(principal: Principal = Depends(require_admin), db: Session = Depends(get_db)):
    """Admin reset (Settings > Simulation): resolves every open incident
    system-wide, whatever the UI has loaded, and marks every link and BGP
    session back up."""
    now = datetime.now(timezone.utc)
    changed = incident_service.reset_all_open(db, principal, now)
    await _commit_and_broadcast(db)
    _restore_link_state(db, now)
    return incident_service.serialize(db, changed)
