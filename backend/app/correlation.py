"""Topology-based root-cause correlation.

An open L1 (physical/optical) incident on a BGP peering's fiber run
outranks any L3 (control-plane/interface) incident on either endpoint of
that same peering within CORRELATION_WINDOW_SECONDS of it - the
fiber-cut-causes-downstream-flaps scenario this module exists to
recognize. See app.fiber_faults for the only current source of real L1
incidents.

Correlation runs in both directions, since alarms don't always arrive in
causal order: an L3 incident opening looks for an L1 root that's already
open (find_open_l1_root_cause), and an L1 incident opening looks back for
L3 symptoms that beat it there (find_l3_symptoms). The actual linking (and
the stage change that goes with it) is app.incidents.service's job - this
module only answers the lookups.

The topology traversal (which peering a router/interface's fiber path
belongs to) is delegated to app.topology_graph, backed by Neo4j - this
module only ever queries Postgres for the *mutable* alarm state (is there
actually an open incident on that peering right now), never the graph.
"""

from datetime import datetime, timedelta

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app import topology_graph
from app.models import AlarmLayer, Incident, IncidentStage, IncidentStatus

# How far apart an L1 incident and an L3 incident on the same peering can
# open and still be considered cause and symptom, rather than independent
# problems. Tuned to the propagation delay of consequential alarms in this
# simulated topology, not a real fiber network's.
CORRELATION_WINDOW_SECONDS = 60

# A human already owns these - correlation doesn't take them away.
_HUMAN_OWNED_STAGES = (IncidentStage.ACKNOWLEDGED, IncidentStage.DISPATCHED)


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


def find_l3_symptoms(db: Session, root: Incident, now: datetime) -> list[Incident]:
    """Open, not-yet-linked L3 incidents that opened within the correlation
    window before `root` (a just-opened, peering-scoped L1 incident) on an
    interface its fiber span carries - or with no interface at all on one
    of those routers, mirroring find_open_l1_root_cause's fallback."""
    if root.peering_id is None:
        return []
    names_by_router: dict[int, list[str]] = {}
    for router_id, name in topology_graph.interfaces_for_peering(root.peering_id):
        names_by_router.setdefault(router_id, []).append(name)
    if not names_by_router:
        return []

    window_start = now - timedelta(seconds=CORRELATION_WINDOW_SECONDS)
    return (
        db.query(Incident)
        .filter(
            or_(
                *(
                    and_(
                        Incident.router_id == router_id,
                        or_(Incident.interface_name.in_(names), Incident.interface_name.is_(None)),
                    )
                    for router_id, names in names_by_router.items()
                )
            ),
            Incident.id != root.id,
            Incident.layer == AlarmLayer.L3,
            Incident.status == IncidentStatus.OPEN,
            Incident.root_cause_incident_id.is_(None),
            Incident.opened_at >= window_start,
            or_(Incident.stage.is_(None), Incident.stage.notin_(_HUMAN_OWNED_STAGES)),
        )
        .all()
    )
