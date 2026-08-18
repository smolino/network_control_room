from datetime import datetime, timedelta

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Incident, IncidentStatus, IncidentType, Router, RouterStatus, TrapEvent
from app.snmp.oid_map import ISIS_ADJACENCY_DOWN_OID, LINK_DOWN_OID, LINK_UP_OID


def open_incident(
    db: Session,
    router: Router,
    incident_type: IncidentType,
    interface_name: str | None,
    description: str,
    peering_id: int | None = None,
) -> tuple[Incident, bool]:
    """Returns (incident, created) - `created` is False when an already-open
    incident of this type/interface was found and just had its trap_count
    bumped, so callers can trigger auto-heal only on the first occurrence.
    When `peering_id` is set (only for the peering-scoped OPTICAL_ALARM
    incidents app.fiber_faults raises), it's added to the dedup key too -
    otherwise two peerings that happen to share the same router_a could
    collide and bump each other's incident instead of getting their own."""
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
        return incident, False

    incident = Incident(
        router_id=router.id,
        incident_type=incident_type,
        interface_name=interface_name,
        peering_id=peering_id,
        status=IncidentStatus.OPEN,
        trap_count=1,
        description=description,
    )
    db.add(incident)
    db.flush()
    return incident, True


def resolve_incident_for_peering(db: Session, peering_id: int, incident_type: IncidentType, now: datetime) -> Incident | None:
    """Resolves the open incident of `incident_type` scoped to `peering_id`
    rather than router_id+interface_name - used to close the L1
    OPTICAL_ALARM incident app.fiber_faults opened for a peering once its
    FIBER_CUT_CLEAR_OID arrives, mirroring _resolve_open_incidents' router-
    scoped pattern below."""
    incident = (
        db.query(Incident)
        .filter(
            Incident.peering_id == peering_id,
            Incident.incident_type == incident_type,
            Incident.status == IncidentStatus.OPEN,
        )
        .first()
    )
    if incident is None:
        return None
    incident.status = IncidentStatus.RESOLVED
    incident.closed_at = now
    db.flush()
    return incident


def _resolve_open_incidents(
    db: Session,
    router: Router,
    interface_name: str | None,
    now: datetime,
    incident_types: list[IncidentType] = (IncidentType.LINK_DOWN, IncidentType.LINK_FLAP),
) -> None:
    open_incidents = (
        db.query(Incident)
        .filter(
            Incident.router_id == router.id,
            Incident.interface_name == interface_name,
            Incident.status == IncidentStatus.OPEN,
            Incident.incident_type.in_(incident_types),
        )
        .all()
    )
    for incident in open_incidents:
        incident.status = IncidentStatus.RESOLVED
        incident.closed_at = now


def evaluate_link_event(db: Session, router: Router, interface_name: str | None, trap_oid: str, now: datetime) -> tuple[Incident, bool]:
    """Sliding-window flap detection for linkDown/linkUp events on one interface."""
    window_start = now - timedelta(seconds=settings.flap_window_seconds)
    recent_count = (
        db.query(TrapEvent)
        .filter(
            TrapEvent.router_id == router.id,
            TrapEvent.interface_name == interface_name,
            TrapEvent.oid.in_([LINK_DOWN_OID, LINK_UP_OID]),
            TrapEvent.received_at >= window_start,
        )
        .count()
    )
    # +1 to account for the event currently being processed (not yet committed
    # when this runs, since it is flushed just before this call).
    is_flapping = recent_count >= settings.flap_transition_threshold

    if is_flapping:
        router.status = RouterStatus.FLAPPING
        return open_incident(
            db,
            router,
            IncidentType.LINK_FLAP,
            interface_name,
            f"{router.hostname} interface {interface_name or 'unknown'} is flapping ({recent_count} transitions in {settings.flap_window_seconds}s)",
        )

    if trap_oid == LINK_DOWN_OID:
        router.status = RouterStatus.DOWN
        return open_incident(
            db,
            router,
            IncidentType.LINK_DOWN,
            interface_name,
            f"{router.hostname} interface {interface_name or 'unknown'} went down",
        )

    # linkUp: resolve any open LINK_DOWN/LINK_FLAP incident on this interface.
    # linkUp itself is a point-in-time notification, not an ongoing condition,
    # so it is logged as an already-resolved incident.
    _resolve_open_incidents(db, router, interface_name, now)
    still_open = (
        db.query(Incident)
        .filter(
            Incident.router_id == router.id,
            Incident.status == IncidentStatus.OPEN,
            Incident.incident_type.in_([IncidentType.LINK_DOWN, IncidentType.LINK_FLAP]),
        )
        .count()
    )
    router.status = RouterStatus.UP if still_open == 0 else router.status

    incident = Incident(
        router_id=router.id,
        incident_type=IncidentType.LINK_UP,
        interface_name=interface_name,
        status=IncidentStatus.RESOLVED,
        closed_at=now,
        trap_count=1,
        description=f"{router.hostname} interface {interface_name or 'unknown'} came back up",
    )
    db.add(incident)
    db.flush()
    return incident, True


def evaluate_isis_adjacency_event(
    db: Session, router: Router, interface_name: str | None, trap_oid: str, now: datetime
) -> tuple[Incident, bool]:
    """Per-bundle-member IS-IS adjacency up/down. Unlike evaluate_link_event,
    this never touches router.status - a single bundle member losing
    adjacency no longer means the router (or even the peering) is down, now
    that redundancy exists (see app.bundles for the bundle-level rollup)."""
    if trap_oid == ISIS_ADJACENCY_DOWN_OID:
        return open_incident(
            db,
            router,
            IncidentType.ISIS_NBR_DOWN,
            interface_name,
            f"{router.hostname} interface {interface_name or 'unknown'} lost IS-IS adjacency",
        )

    # isis adjacency up: resolve any open ISIS_NBR_DOWN on this interface and
    # log a resolved "recovered" marker, same pattern as linkUp/LINK_UP.
    _resolve_open_incidents(db, router, interface_name, now, incident_types=[IncidentType.ISIS_NBR_DOWN])
    incident = Incident(
        router_id=router.id,
        incident_type=IncidentType.ISIS_NBR_UP,
        interface_name=interface_name,
        status=IncidentStatus.RESOLVED,
        closed_at=now,
        trap_count=1,
        description=f"{router.hostname} interface {interface_name or 'unknown'} IS-IS adjacency recovered",
    )
    db.add(incident)
    db.flush()
    return incident, True
