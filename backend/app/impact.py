"""Customer-impact ("blast radius") of open incidents.

Answers the question a NOC actually triages by - "how many customers does
this hurt right now?" - deterministically from the fleet's two-tier
topology:

- Scope: an incident on a primary puts that primary's single-homed
  customer CPE in its blast radius; a peering-scoped L1 fiber incident (see
  app.fiber_faults) puts both endpoint primaries' customers in it; an
  incident on a customer router only ever affects that one customer.
- Severity: a customer has actually lost service ("down") when its primary
  is cut off from the backbone - the router itself is DOWN, or every
  BGP session it has is gone so it's no longer in the mesh's main connected
  component - or when the customer CPE itself is DOWN. Anything else in
  the blast radius is "degraded": still reachable, but over reduced
  redundancy (e.g. a fiber cut on one of a primary's ~3 peerings).

Computed on read from live router/BGP state rather than stored, so it
always reflects the network as it is now - impact grows if a second
peering of the same primary fails and shrinks as sessions re-establish,
with no extra bookkeeping. Only open, service-affecting incidents get one;
resolved incidents and pure control/management events (CPU, auth,
config-change, ...) have no blast radius.
"""

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import BgpPeering, BgpSessionStatus, Incident, IncidentStatus, IncidentType, Router, RouterStatus, RouterType

# Incident types that take traffic down or reduce forwarding redundancy.
SERVICE_AFFECTING_TYPES = {
    IncidentType.LINK_DOWN,
    IncidentType.LINK_FLAP,
    IncidentType.BGP_STATE_CHANGE,
    IncidentType.BFD_SESSION_DOWN,
    IncidentType.ISIS_NBR_DOWN,
    IncidentType.OPTICAL_ALARM,
    IncidentType.PSU_FAILURE,
}


@dataclass
class Impact:
    level: str  # "outage" | "degraded"
    primary_router_ids: list[int]
    customer_ids: set[int] = field(default_factory=set)
    down_customer_ids: set[int] = field(default_factory=set)

    def summary(self) -> dict:
        return {
            "level": self.level,
            "primary_router_ids": self.primary_router_ids,
            "customer_count": len(self.customer_ids),
            "customers_down": len(self.down_customer_ids),
        }

    def detail(self) -> dict:
        return {**self.summary(), "router_ids": self.primary_router_ids + sorted(self.customer_ids)}


def _isolated_primary_ids(db: Session, primary_ids: set[int]) -> set[int]:
    """Which of `primary_ids` have no path into the backbone right now - not
    in the largest connected component of the established-session graph.
    Primaries with no peerings at all (e.g. freshly added, not linked yet)
    are never counted as isolated, since they were never in the mesh."""
    peerings = db.query(BgpPeering.router_a_id, BgpPeering.router_b_id, BgpPeering.status).all()
    meshed: set[int] = set()
    adjacency: dict[int, list[int]] = {}
    for a, b, status in peerings:
        meshed.update((a, b))
        if status == BgpSessionStatus.ESTABLISHED:
            adjacency.setdefault(a, []).append(b)
            adjacency.setdefault(b, []).append(a)

    largest: set[int] = set()
    seen: set[int] = set()
    for start in adjacency:
        if start in seen:
            continue
        component = {start}
        stack = [start]
        while stack:
            node = stack.pop()
            for neighbor in adjacency[node]:
                if neighbor not in component:
                    component.add(neighbor)
                    stack.append(neighbor)
        seen |= component
        if len(component) > len(largest):
            largest = component

    return {pid for pid in primary_ids if pid in meshed and pid not in largest}


def compute_impacts(db: Session, incidents: list[Incident]) -> dict[int, Impact]:
    """Blast radius for each open, service-affecting incident in
    `incidents`, keyed by incident id - batched so a 200-row incident list
    costs a handful of queries, not a few per row."""
    candidates = [
        i for i in incidents if i.status == IncidentStatus.OPEN and i.incident_type in SERVICE_AFFECTING_TYPES
    ]
    if not candidates:
        return {}

    peering_ids = {i.peering_id for i in candidates if i.peering_id is not None}
    peering_endpoints = {
        pid: [a, b]
        for pid, a, b in db.query(BgpPeering.id, BgpPeering.router_a_id, BgpPeering.router_b_id)
        .filter(BgpPeering.id.in_(peering_ids))
        .all()
    } if peering_ids else {}

    router_ids = {i.router_id for i in candidates} | {rid for ends in peering_endpoints.values() for rid in ends}
    routers = {
        rid: (rtype, status, parent)
        for rid, rtype, status, parent in db.query(Router.id, Router.router_type, Router.status, Router.parent_router_id)
        .filter(Router.id.in_(router_ids))
        .all()
    }

    scopes: dict[int, list[int]] = {}
    for incident in candidates:
        if incident.peering_id is not None and incident.peering_id in peering_endpoints:
            scopes[incident.id] = peering_endpoints[incident.peering_id]
        elif routers.get(incident.router_id, (None,))[0] == RouterType.PRIMARY:
            scopes[incident.id] = [incident.router_id]

    primary_ids = {pid for scope in scopes.values() for pid in scope}
    customer_parent_ids = {
        parent for rtype, _, parent in routers.values() if rtype == RouterType.CUSTOMER and parent is not None
    }
    all_primary_ids = primary_ids | customer_parent_ids
    primary_status = dict(
        db.query(Router.id, Router.status).filter(Router.id.in_(all_primary_ids)).all()
    ) if all_primary_ids else {}
    isolated = _isolated_primary_ids(db, all_primary_ids) if all_primary_ids else set()
    cut_off = isolated | {pid for pid, status in primary_status.items() if status == RouterStatus.DOWN}

    customers_by_primary: dict[int, list[tuple[int, RouterStatus]]] = {}
    if primary_ids:
        for cid, parent, status in (
            db.query(Router.id, Router.parent_router_id, Router.status)
            .filter(Router.router_type == RouterType.CUSTOMER, Router.parent_router_id.in_(primary_ids))
            .all()
        ):
            customers_by_primary.setdefault(parent, []).append((cid, status))

    impacts: dict[int, Impact] = {}
    for incident in candidates:
        rtype, status, parent = routers.get(incident.router_id, (None, None, None))
        if incident.id in scopes:
            impact = Impact(level="degraded", primary_router_ids=scopes[incident.id])
            for pid in impact.primary_router_ids:
                for cid, cstatus in customers_by_primary.get(pid, []):
                    impact.customer_ids.add(cid)
                    if pid in cut_off or cstatus == RouterStatus.DOWN:
                        impact.down_customer_ids.add(cid)
        elif rtype == RouterType.CUSTOMER:
            impact = Impact(level="degraded", primary_router_ids=[], customer_ids={incident.router_id})
            if status == RouterStatus.DOWN or parent in cut_off:
                impact.down_customer_ids.add(incident.router_id)
        else:
            continue
        if impact.down_customer_ids:
            impact.level = "outage"
        impacts[incident.id] = impact
    return impacts


def impact_totals(db: Session) -> tuple[int, int]:
    """(customers_impacted, customers_down) across every open incident,
    each customer counted once even if several incidents cover it."""
    open_incidents = (
        db.query(Incident)
        .filter(Incident.status == IncidentStatus.OPEN, Incident.incident_type.in_(SERVICE_AFFECTING_TYPES))
        .all()
    )
    impacted: set[int] = set()
    down: set[int] = set()
    for impact in compute_impacts(db, open_incidents).values():
        impacted |= impact.customer_ids
        down |= impact.down_customer_ids
    return len(impacted), len(down)

