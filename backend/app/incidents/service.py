"""Incident workflow - the one place incident state changes.

Lifecycle (see models.IncidentStage):

    trap ─▶ alarm only (TrapEvent) for ups/boots/unknown traps - never an incident
         └▶ NEW ── settle window ──▶ auto-heal (backup + playbook)
             │                         ├─ fixed, has a clear trap ─▶ MONITORING ─ clear ─▶ RESOLVED
             │                         │                               └─ timeout ─▶ TRIAGE
             │                         ├─ fixed, no clear trap ─────▶ RESOLVED
             │                         └─ failed / notify-only ─────▶ TRIAGE
             └▶ SUPPRESSED (symptom of an open L1 root cause)
    TRIAGE ─ ack ─▶ ACKNOWLEDGED ─ send to team ─▶ DISPATCHED ─ resolve (reason) ─▶ RESOLVED

A clear trap (linkUp, isisAdjacencyUp, bgpEstablished, opticalSignalRestored)
resolves an incident from any open stage. When a root cause resolves, its
open symptoms either go with it (a human resolved it) or are released back
to NEW to be judged on their own (the device cleared it - anything still
down a settle window later is a separate problem).

The settle window and the MONITORING timeout are driven by sweep(), which
the correlator calls between Kafka polls - see app.streaming.correlator.

Every change made here is recorded on the session (touch/touch_router), so
callers don't have to track what moved: after committing they call
pop_event() for the one WebSocket payload describing all of it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.correlation import find_l3_symptoms, find_open_l1_root_cause
from app.fault_location import locate_fault
from app.impact import Impact, compute_impacts
from app.models import (
    USER_RESOLUTION_REASONS,
    AlarmLayer,
    Incident,
    IncidentStage,
    IncidentStatus,
    IncidentType,
    RemediationAction,
    RemediationActionType,
    RemediationStatus,
    ResolutionReason,
    Router,
    RouterStatus,
    RouterType,
)
from app.remediation.engine import AUTO_HEAL_ELIGIBLE_TYPES, maybe_remediate
from app.schemas import FaultLocation, IncidentImpact, IncidentOut, RemediationSummary

if TYPE_CHECKING:
    # Type-only: the correlator imports this module and has no use for auth.
    from app.auth import Principal

# Stages a person has to act on - the Human Review queue.
HUMAN_STAGES = (IncidentStage.TRIAGE, IncidentStage.ACKNOWLEDGED, IncidentStage.DISPATCHED)

# Types whose condition the device itself reports clearing, so a
# successful fix can be confirmed rather than assumed. OPTICAL_ALARM only
# counts when it's a peering-scoped fiber cut (opticalSignalRestored) - see
# has_clear_signal.
CLEARABLE_TYPES = {IncidentType.LINK_DOWN, IncidentType.LINK_FLAP, IncidentType.BGP_STATE_CHANGE, IncidentType.ISIS_NBR_DOWN}

# Skip the settle window: flap detection already needed several
# transitions inside FLAP_WINDOW_SECONDS to fire, so there's nothing left
# to wait for - and the next linkUp would otherwise clear it before
# dampening is ever applied.
SETTLE_EXEMPT_TYPES = {IncidentType.LINK_FLAP}

LINK_TYPES = (IncidentType.LINK_DOWN, IncidentType.LINK_FLAP)

SYSTEM = "system"

_TOUCHED_INCIDENTS = "ncr_touched_incidents"
_TOUCHED_ROUTERS = "ncr_touched_routers"


class WorkflowError(Exception):
    """An action that isn't valid for the incident's current state."""


def is_incident_type(incident_type: IncidentType) -> bool:
    """Whether a trap of this type opens an incident at all. Everything
    else (linkUp, coldStart, unknown traps, ...) stays an alarm - a
    TrapEvent row - and never shows up as an incident."""
    return incident_type in AUTO_HEAL_ELIGIBLE_TYPES


def has_clear_signal(incident: Incident) -> bool:
    if incident.incident_type == IncidentType.OPTICAL_ALARM:
        return incident.peering_id is not None
    return incident.incident_type in CLEARABLE_TYPES


# --- change tracking ---------------------------------------------------------


def touch(db: Session, incident: Incident) -> None:
    db.info.setdefault(_TOUCHED_INCIDENTS, {})[incident.id] = incident


def touch_router(db: Session, router: Router) -> None:
    db.info.setdefault(_TOUCHED_ROUTERS, {})[router.id] = router


def _set_stage(db: Session, incident: Incident, stage: IncidentStage, now: datetime) -> None:
    incident.stage = stage
    incident.stage_changed_at = now
    touch(db, incident)


# --- priority ----------------------------------------------------------------

_SEVERITY_SCORE = {"critical": 2, "warning": 1}


def compute_priority(incident: Incident, router_type: RouterType | None, impact: Impact | None) -> str:
    """P1..P4 from how bad the alarm is (severity, physical layer, backbone
    router) and how many customers it hurts right now (see app.impact)."""
    score = _SEVERITY_SCORE.get(incident.severity or "", 0)
    if incident.layer == AlarmLayer.L1:
        score += 1
    if router_type == RouterType.PRIMARY:
        score += 1
    if impact is not None:
        score += 2 if impact.level == "outage" else 1 if impact.customer_ids else 0
    if score >= 5:
        return "P1"
    if score == 4:
        return "P2"
    if score >= 2:
        return "P3"
    return "P4"


def _router_types(db: Session, router_ids: set[int]) -> dict[int, RouterType]:
    if not router_ids:
        return {}
    return dict(db.query(Router.id, Router.router_type).filter(Router.id.in_(router_ids)).all())


def refresh_priorities(db: Session, incidents: list[Incident]) -> None:
    """Recomputed on open and on every sweep transition, so it tracks
    impact as it grows/shrinks without being recomputed on every read."""
    open_incidents = [i for i in incidents if i.status == IncidentStatus.OPEN]
    if not open_incidents:
        return
    impacts = compute_impacts(db, open_incidents)
    router_types = _router_types(db, {i.router_id for i in open_incidents})
    for incident in open_incidents:
        incident.priority = compute_priority(incident, router_types.get(incident.router_id), impacts.get(incident.id))


# --- opening and correlation -------------------------------------------------


def open_or_bump(
    db: Session,
    router: Router,
    incident_type: IncidentType,
    interface_name: str | None,
    description: str,
    severity: str,
    now: datetime,
    peering_id: int | None = None,
    fault_segment_index: int | None = None,
) -> tuple[Incident, bool]:
    """Returns (incident, created) - `created` is False when an already-open
    incident of this type/interface was found and just had its trap_count
    bumped (one open incident per distinct problem, not a row per repeat
    trap). When `peering_id` is set (only for the peering-scoped
    OPTICAL_ALARM incidents app.fiber_faults raises), it's added to the
    dedup key too - otherwise two peerings that happen to share the same
    router_a could collide and bump each other's incident instead of
    getting their own."""
    query = db.query(Incident).filter(
        Incident.router_id == router.id,
        Incident.incident_type == incident_type,
        Incident.interface_name == interface_name,
        Incident.status == IncidentStatus.OPEN,
    )
    if peering_id is not None:
        query = query.filter(Incident.peering_id == peering_id)
    incident = query.first()
    if incident:
        incident.trap_count += 1
        touch(db, incident)
        return incident, False

    incident = Incident(
        router_id=router.id,
        incident_type=incident_type,
        interface_name=interface_name,
        peering_id=peering_id,
        fault_segment_index=fault_segment_index,
        status=IncidentStatus.OPEN,
        stage=IncidentStage.NEW,
        stage_changed_at=now,
        severity=severity,
        trap_count=1,
        description=description,
    )
    db.add(incident)
    db.flush()
    touch(db, incident)
    _correlate(db, incident, now)
    refresh_priorities(db, [incident])
    return incident, True


def _link_symptom(db: Session, symptom: Incident, root: Incident, now: datetime) -> None:
    symptom.root_cause_incident_id = root.id
    _set_stage(db, symptom, IncidentStage.SUPPRESSED, now)
    touch_router(db, symptom.router)


def _correlate(db: Session, incident: Incident, now: datetime) -> None:
    """Both directions (see app.correlation): a new L3 incident looks for an
    open L1 root; a new peering-scoped L1 incident claims the L3 symptoms
    that opened just before it."""
    if incident.layer == AlarmLayer.L3:
        root = find_open_l1_root_cause(db, incident.router_id, incident.interface_name, now)
        if root is not None:
            _link_symptom(db, incident, root, now)
    elif incident.peering_id is not None:
        for symptom in find_l3_symptoms(db, incident, now):
            _link_symptom(db, symptom, incident, now)


# --- resolving ---------------------------------------------------------------


def refresh_router_link_status(db: Session, router: Router) -> None:
    """Flips the router back to UP once no open LINK_DOWN/LINK_FLAP incident
    is keeping it down/flapping - after a linkUp, or after one of those
    incidents is resolved by hand (without this, a router resolved that way
    would stay DOWN forever, since nothing else would ever clear it)."""
    if router.status == RouterStatus.UP:
        return
    still_open = (
        db.query(Incident)
        .filter(
            Incident.router_id == router.id,
            Incident.status == IncidentStatus.OPEN,
            Incident.incident_type.in_(LINK_TYPES),
        )
        .count()
    )
    if still_open == 0:
        router.status = RouterStatus.UP
        touch_router(db, router)


def _open_symptoms(db: Session, root: Incident) -> list[Incident]:
    return (
        db.query(Incident)
        .filter(Incident.root_cause_incident_id == root.id, Incident.status == IncidentStatus.OPEN)
        .all()
    )


def _resolve(
    db: Session, incident: Incident, now: datetime, reason: ResolutionReason, by: str, note: str | None = None
) -> bool:
    """Resolves one incident if it's still open, and deals with its open
    symptoms (see module docstring). Returns whether anything changed."""
    if incident.status != IncidentStatus.OPEN:
        return False
    incident.status = IncidentStatus.RESOLVED
    incident.closed_at = now
    incident.resolved_by = by
    incident.resolution_reason = reason
    incident.resolution_note = note
    incident.resolved_manually = by != SYSTEM
    _set_stage(db, incident, IncidentStage.RESOLVED, now)
    touch_router(db, incident.router)

    for symptom in _open_symptoms(db, incident):
        if reason == ResolutionReason.AUTO_CLEARED:
            symptom.root_cause_incident_id = None
            _set_stage(db, symptom, IncidentStage.NEW, now)
        else:
            _resolve(db, symptom, now, ResolutionReason.ROOT_CAUSE_RESOLVED, by)

    if incident.incident_type in LINK_TYPES:
        refresh_router_link_status(db, incident.router)
    return True


def clear(
    db: Session, router: Router, incident_types: list[IncidentType], interface_name: str | None, now: datetime
) -> list[Incident]:
    """The device reported the condition gone (linkUp, isisAdjacencyUp,
    bgpEstablished) - resolves the matching open incidents, whatever stage
    they're in."""
    incidents = (
        db.query(Incident)
        .filter(
            Incident.router_id == router.id,
            Incident.interface_name == interface_name,
            Incident.status == IncidentStatus.OPEN,
            Incident.incident_type.in_(incident_types),
        )
        .all()
    )
    for incident in incidents:
        _resolve(db, incident, now, ResolutionReason.AUTO_CLEARED, SYSTEM)
    return incidents


def clear_peering(db: Session, peering_id: int, incident_type: IncidentType, now: datetime) -> list[Incident]:
    """Peering-scoped clear() - for the L1 fiber incident, which isn't
    attached to any one interface."""
    incidents = (
        db.query(Incident)
        .filter(
            Incident.peering_id == peering_id,
            Incident.incident_type == incident_type,
            Incident.status == IncidentStatus.OPEN,
        )
        .all()
    )
    for incident in incidents:
        _resolve(db, incident, now, ResolutionReason.AUTO_CLEARED, SYSTEM)
    return incidents


def resolve_by_user(
    db: Session,
    incidents: list[Incident],
    principal: Principal,
    reason: ResolutionReason,
    note: str | None,
    now: datetime,
) -> list[Incident]:
    """Resolves each root incident in `incidents` (and, with it, its
    symptoms). Symptoms are skipped - they only ever close through their
    root, so resolving one alone can't leave the tree half-closed. Returns
    the incidents that actually changed."""
    if reason not in USER_RESOLUTION_REASONS:
        raise WorkflowError(f"'{reason.value}' isn't a reason an operator can pick")
    return [
        incident
        for incident in incidents
        if incident.root_cause_incident_id is None and _resolve(db, incident, now, reason, principal.username, note)
    ]


def reset_all_open(db: Session, principal: Principal, now: datetime) -> list[Incident]:
    incidents = db.query(Incident).filter(Incident.status == IncidentStatus.OPEN).all()
    return [i for i in incidents if _resolve(db, i, now, ResolutionReason.RESET, principal.username)]


# --- human actions -----------------------------------------------------------


def require_actionable(incident: Incident) -> None:
    if incident.status != IncidentStatus.OPEN:
        raise WorkflowError("Incident is already resolved")
    if incident.root_cause_incident_id is not None:
        raise WorkflowError(f"Incident is a symptom of #{incident.root_cause_incident_id} - act on that one instead")


def acknowledge(db: Session, incident: Incident, principal: Principal, now: datetime) -> None:
    """The caller takes ownership. A dispatched incident stays dispatched -
    acknowledging it only changes who owns the follow-up."""
    require_actionable(incident)
    incident.owner_user_id = principal.user.id if principal.user else None
    incident.acknowledged_at = now
    if incident.stage == IncidentStage.DISPATCHED:
        touch(db, incident)
    else:
        _set_stage(db, incident, IncidentStage.ACKNOWLEDGED, now)
    touch_router(db, incident.router)


def dispatch(db: Session, incident: Incident, principal: Principal, now: datetime) -> None:
    """Handed off to a team (see api/incidents.py:notify_team). Whoever
    sent it owns the follow-up unless someone already did."""
    require_actionable(incident)
    if incident.owner_user_id is None and principal.user is not None:
        incident.owner_user_id = principal.user.id
        incident.acknowledged_at = incident.acknowledged_at or now
    _set_stage(db, incident, IncidentStage.DISPATCHED, now)
    touch_router(db, incident.router)


# --- auto-heal and the sweeper -----------------------------------------------


def _run_auto_heal(db: Session, incident: Incident, now: datetime) -> None:
    action = maybe_remediate(db, incident.router, incident)
    if action is None or action["action_type"] == RemediationActionType.NOTIFY_ONLY.value:
        _set_stage(db, incident, IncidentStage.TRIAGE, now)
    elif action["status"] != RemediationStatus.SUCCESS.value:
        _set_stage(db, incident, IncidentStage.TRIAGE, now)
    elif has_clear_signal(incident):
        _set_stage(db, incident, IncidentStage.MONITORING, now)
    else:
        _resolve(db, incident, now, ResolutionReason.AUTO_HEALED, SYSTEM)
    touch_router(db, incident.router)


SWEEP_BATCH = 100


def _lock_for_sweep(query):
    # Skips rows an API request (ack/resolve) is changing right now - they
    # get picked up on the next sweep instead of blocking this one.
    return query.with_for_update(skip_locked=True) if settings.is_postgres else query


def sweep(db: Session, now: datetime) -> dict | None:
    """Runs auto-heal on incidents whose settle window has passed and
    escalates fixes that were never confirmed by a clear trap. Commits, and
    returns the WebSocket event for whatever moved (None if nothing did)."""
    settled = now - timedelta(seconds=settings.incident_settle_seconds)
    ready = _lock_for_sweep(
        db.query(Incident)
        .filter(
            Incident.status == IncidentStatus.OPEN,
            Incident.stage == IncidentStage.NEW,
            Incident.root_cause_incident_id.is_(None),
            (Incident.stage_changed_at <= settled) | Incident.incident_type.in_(SETTLE_EXEMPT_TYPES),
        )
        .order_by(Incident.stage_changed_at.asc())
        .limit(SWEEP_BATCH)
    ).all()
    for incident in ready:
        _run_auto_heal(db, incident, now)

    unconfirmed_since = now - timedelta(seconds=settings.incident_monitoring_timeout_seconds)
    overdue = _lock_for_sweep(
        db.query(Incident)
        .filter(
            Incident.status == IncidentStatus.OPEN,
            Incident.stage == IncidentStage.MONITORING,
            Incident.stage_changed_at <= unconfirmed_since,
        )
        .limit(SWEEP_BATCH)
    ).all()
    for incident in overdue:
        _set_stage(db, incident, IncidentStage.TRIAGE, now)
        touch_router(db, incident.router)

    refresh_priorities(db, ready + overdue)
    db.commit()
    return pop_event(db)


# --- reads -------------------------------------------------------------------


def needs_attention_router_ids(db: Session, router_ids: list[int] | None = None) -> set[int]:
    """Routers with an open incident in triage - one nobody has picked up
    yet. Clears itself once it's acknowledged, dispatched, or resolved."""
    query = (
        db.query(Incident.router_id)
        .filter(Incident.status == IncidentStatus.OPEN, Incident.stage == IncidentStage.TRIAGE)
        .distinct()
    )
    if router_ids is not None:
        query = query.filter(Incident.router_id.in_(router_ids))
    return {row[0] for row in query.all()}


def queue(db: Session, limit: int = 500) -> list[Incident]:
    """The Human Review queue: everything a person has to act on, most
    urgent first."""
    return (
        db.query(Incident)
        .filter(Incident.status == IncidentStatus.OPEN, Incident.stage.in_(HUMAN_STAGES))
        .order_by(Incident.priority.is_(None), Incident.priority.asc(), Incident.opened_at.asc())
        .limit(limit)
        .all()
    )


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


def _symptom_counts(db: Session, incident_ids: list[int]) -> dict[int, int]:
    if not incident_ids:
        return {}
    return dict(
        db.query(Incident.root_cause_incident_id, func.count(Incident.id))
        .filter(Incident.root_cause_incident_id.in_(incident_ids), Incident.status == IncidentStatus.OPEN)
        .group_by(Incident.root_cause_incident_id)
        .all()
    )


def serialize(db: Session, incidents: list[Incident]) -> list[IncidentOut]:
    """IncidentOut for each incident - batched (a handful of queries for any
    number of rows). The REST API and every WebSocket event go through
    this, so both always carry the same fields."""
    ids = [i.id for i in incidents]
    latest = _latest_remediation_by_incident(db, ids)
    impacts = compute_impacts(db, incidents)
    router_types = _router_types(db, {i.router_id for i in incidents})
    symptom_counts = _symptom_counts(db, ids)

    out = []
    for incident in incidents:
        item = IncidentOut.model_validate(incident)
        if incident.id in latest:
            item.remediation = RemediationSummary.model_validate(latest[incident.id])
        if incident.id in impacts:
            item.impact = IncidentImpact(**impacts[incident.id].summary())
        if incident.peering is not None and incident.fault_segment_index is not None:
            location = locate_fault(incident.peering, incident.fault_segment_index)
            item.fault_location = FaultLocation(**location) if location else None
        item.router_type = router_types.get(incident.router_id)
        item.symptom_count = symptom_counts.get(incident.id, 0)
        out.append(item)
    return out


def router_payload(router: Router, needs_attention: bool) -> dict:
    return {
        "id": router.id,
        "hostname": router.hostname,
        "status": router.status.value,
        "needs_attention": needs_attention,
        "router_type": router.router_type.value,
    }


def pop_event(db: Session, event_type: str = "incident_updated", routers: list[Router] = (), **extra) -> dict | None:
    """The WebSocket payload for everything touched on this session since
    the last pop - every changed incident in full (see serialize) plus every
    router whose status or needs_attention may have moved. Call after
    committing. None when nothing was touched and no routers were passed."""
    incidents = list(db.info.pop(_TOUCHED_INCIDENTS, {}).values())
    touched_routers = {**db.info.pop(_TOUCHED_ROUTERS, {}), **{r.id: r for r in routers}}
    for incident in incidents:
        touched_routers.setdefault(incident.router_id, incident.router)
    if not incidents and not touched_routers:
        return None
    attention = needs_attention_router_ids(db, list(touched_routers))
    return {
        "type": event_type,
        "incidents": [item.model_dump(mode="json") for item in serialize(db, incidents)],
        "routers": [router_payload(r, r.id in attention) for r in touched_routers.values()],
        **extra,
    }


# --- one-off migration -------------------------------------------------------


def backfill_stages(db: Session) -> None:
    """Gives incidents from before the workflow existed a stage, so they
    show up in the right place (open ones in particular - they'd otherwise
    be invisible to both the sweeper and the Human Review queue)."""
    unstaged = db.query(Incident).filter(Incident.stage.is_(None))
    unstaged.filter(Incident.status == IncidentStatus.RESOLVED, Incident.resolved_manually.is_(True)).update(
        {
            Incident.stage: IncidentStage.RESOLVED,
            Incident.resolved_by: "user",
            Incident.resolution_reason: ResolutionReason.FIXED,
        },
        synchronize_session=False,
    )
    unstaged.filter(Incident.status == IncidentStatus.RESOLVED).update(
        {
            Incident.stage: IncidentStage.RESOLVED,
            Incident.resolved_by: SYSTEM,
            Incident.resolution_reason: ResolutionReason.AUTO_CLEARED,
        },
        synchronize_session=False,
    )

    open_incidents = unstaged.filter(Incident.status == IncidentStatus.OPEN).all()
    latest = _latest_remediation_by_incident(db, [i.id for i in open_incidents])
    for incident in open_incidents:
        incident.stage_changed_at = incident.updated_at
        action = latest.get(incident.id)
        if incident.root_cause_incident_id is not None:
            incident.stage = IncidentStage.SUPPRESSED
        elif action is None:
            incident.stage = IncidentStage.NEW
        elif action.action_type == RemediationActionType.NOTIFY_ONLY or action.status == RemediationStatus.FAILED:
            incident.stage = IncidentStage.TRIAGE
        else:
            incident.stage = IncidentStage.MONITORING
    refresh_priorities(db, open_incidents)
    db.commit()
