"""Topology-based root-cause correlation.

An open L1 (physical/optical) incident on a BGP peering's fiber run
outranks any L3 (control-plane/interface) incident that opens on either
endpoint of that same peering shortly after - the fiber-cut-causes-
downstream-flaps scenario this module exists to recognize. See
app.fiber_faults for the only current source of real L1 incidents.

The topology traversal (which peering a router/interface's fiber path
belongs to) is delegated to app.topology_graph, backed by Neo4j - this
module only ever queries Postgres for the *mutable* alarm state (is there
actually an open incident on that peering right now), never the graph.
"""

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import topology_graph
from app.models import AlarmLayer, Incident, IncidentStatus

# How long after an L1 incident opens an L3 incident on the same peering is
# still considered one of its symptoms, rather than an independent problem.
# Tuned to the propagation delay of consequential alarms in this simulated
# topology, not a real fiber network's.
CORRELATION_WINDOW_SECONDS = 60


def find_open_l1_root_cause(db: Session, router_id: int, interface_name: str | None, now: datetime) -> Incident | None:
    """The oldest still-open L1 incident on a peering `router_id`'s fiber
    path belongs to, opened within the correlation window - the most
    likely root cause for any L3 incident on that router/interface right
    now. None if there isn't one.

    When `interface_name` is known, the candidate peerings come from just
    that interface's SUPPORTED_BY edge in the topology graph (precise);
    otherwise every peering any of the router's interfaces are supported
    by (the old SQL-only lookup's breadth, kept as a fallback for incident
    types with no specific interface, e.g. BGP_STATE_CHANGE)."""
    peering_ids = (
        topology_graph.peering_ids_for_interface(router_id, interface_name)
        if interface_name
        else topology_graph.peering_ids_for_router(router_id)
    )
    if not peering_ids:
        return None

    window_start = now - timedelta(seconds=CORRELATION_WINDOW_SECONDS)
    return (
        db.query(Incident)
        .filter(
            Incident.peering_id.in_(peering_ids),
            Incident.layer == AlarmLayer.L1,
            Incident.status == IncidentStatus.OPEN,
            Incident.opened_at >= window_start,
        )
        .order_by(Incident.opened_at.asc())
        .first()
    )


def try_link_root_cause(db: Session, incident: Incident, now: datetime) -> None:
    """Links `incident` to an open L1 root cause on the same peering, if
    any - no-op for L1 incidents themselves or ones already linked."""
    if incident.layer != AlarmLayer.L3 or incident.root_cause_incident_id is not None:
        return

    root_cause = find_open_l1_root_cause(db, incident.router_id, incident.interface_name, now)
    if root_cause is not None:
        incident.root_cause_incident_id = root_cause.id
        db.flush()
