from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.incidents import service as incident_service
from app.models import Incident, IncidentType, Router, RouterStatus, TrapEvent
from app.snmp.oid_map import ISIS_ADJACENCY_DOWN_OID, LINK_DOWN_OID, LINK_UP_OID

# A flap is detected on whichever transition crosses the threshold - often
# a linkUp, whose own "info" severity would understate it.
FLAP_SEVERITY = "warning"


def evaluate_link_event(
    db: Session, router: Router, interface_name: str | None, trap_oid: str, severity: str, now: datetime
) -> Incident | None:
    """Sliding-window flap detection for linkDown/linkUp events on one
    interface. Returns the incident opened/bumped, or None for a linkUp
    that just cleared things (linkUp itself is an alarm, not an incident)."""
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
    # Includes the event currently being processed (flushed just before
    # this call, not yet committed).
    is_flapping = recent_count >= settings.flap_transition_threshold

    if is_flapping:
        router.status = RouterStatus.FLAPPING
        incident, _ = incident_service.open_or_bump(
            db,
            router,
            IncidentType.LINK_FLAP,
            interface_name,
            f"{router.hostname} interface {interface_name or 'unknown'} is flapping ({recent_count} transitions in {settings.flap_window_seconds}s)",
            FLAP_SEVERITY,
            now,
        )
        return incident

    if trap_oid == LINK_DOWN_OID:
        router.status = RouterStatus.DOWN
        incident, _ = incident_service.open_or_bump(
            db,
            router,
            IncidentType.LINK_DOWN,
            interface_name,
            f"{router.hostname} interface {interface_name or 'unknown'} went down",
            severity,
            now,
        )
        return incident

    incident_service.clear(db, router, [IncidentType.LINK_DOWN, IncidentType.LINK_FLAP], interface_name, now)
    incident_service.refresh_router_link_status(db, router)
    return None


def evaluate_isis_adjacency_event(
    db: Session, router: Router, interface_name: str | None, trap_oid: str, severity: str, now: datetime
) -> Incident | None:
    """Per-bundle-member IS-IS adjacency up/down. Unlike evaluate_link_event,
    this never touches router.status - a single bundle member losing
    adjacency no longer means the router (or even the peering) is down, now
    that redundancy exists (see app.bundles for the bundle-level rollup)."""
    if trap_oid == ISIS_ADJACENCY_DOWN_OID:
        incident, _ = incident_service.open_or_bump(
            db,
            router,
            IncidentType.ISIS_NBR_DOWN,
            interface_name,
            f"{router.hostname} interface {interface_name or 'unknown'} lost IS-IS adjacency",
            severity,
            now,
        )
        return incident

    incident_service.clear(db, router, [IncidentType.ISIS_NBR_DOWN], interface_name, now)
    return None
