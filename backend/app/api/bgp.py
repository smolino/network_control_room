from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.bgp_topology import build_established_adjacency, shortest_reroute_path
from app.bundles import ensure_bundles_for_peering
from app.db import get_db
from app.fiber_faults import active_faults
from app.models import AlarmLayer, BgpPeering, BgpSessionStatus, Incident, IncidentStatus, InterfaceBundle, Router
from app.schemas import BgpPeerIn, BgpPeeringOut, InterfaceBundleOut, InterfaceOut
from app.topology_graph import sync_peering_to_neo4j

router = APIRouter(prefix="/api/bgp", tags=["bgp"])


def _to_bundle_out(bundle: InterfaceBundle | None) -> InterfaceBundleOut | None:
    if bundle is None:
        return None
    members = [InterfaceOut.model_validate(m) for m in bundle.members]
    total_bandwidth_mbps = sum(m.bandwidth_mbps for m in members if m.isis_adjacency_up)
    return InterfaceBundleOut(
        id=bundle.id,
        name=bundle.name,
        status=bundle.status,
        members=members,
        total_bandwidth_mbps=total_bandwidth_mbps,
    )


def _to_peering_out(db: Session, peering: BgpPeering, adjacency: dict[int, list[int]]) -> BgpPeeringOut:
    reroute_path = None
    if peering.status == BgpSessionStatus.DOWN:
        reroute_path = shortest_reroute_path(adjacency, peering.router_a_id, peering.router_b_id)
    out = BgpPeeringOut.model_validate(peering)
    out.reroute_path = reroute_path
    out.active_fault_segment = active_faults.get(peering.id)
    open_l1_incident = (
        db.query(Incident.id)
        .filter(Incident.peering_id == peering.id, Incident.layer == AlarmLayer.L1, Incident.status == IncidentStatus.OPEN)
        .first()
    )
    out.open_l1_incident_id = open_l1_incident[0] if open_l1_incident else None

    bundle_a = db.query(InterfaceBundle).filter(
        InterfaceBundle.peering_id == peering.id, InterfaceBundle.router_id == peering.router_a_id
    ).first()
    bundle_b = db.query(InterfaceBundle).filter(
        InterfaceBundle.peering_id == peering.id, InterfaceBundle.router_id == peering.router_b_id
    ).first()
    out.bundle_a = _to_bundle_out(bundle_a)
    out.bundle_b = _to_bundle_out(bundle_b)
    return out


@router.get("/peerings", response_model=list[BgpPeeringOut])
def list_peerings(db: Session = Depends(get_db)):
    peerings = db.query(BgpPeering).all()
    adjacency = build_established_adjacency(db)
    return [_to_peering_out(db, p, adjacency) for p in peerings]


@router.post("/seed", response_model=list[BgpPeeringOut])
def seed_peerings(pairs: list[BgpPeerIn], db: Session = Depends(get_db)):
    """Bulk idempotent insert used by the trap simulator on startup, once
    the routers themselves have already been seeded. Also derives and seeds
    each side's interface bundle (Port-channel + FortyGigE members) from the
    same peer adjacency, using the identical mgmt_ip-sorted numbering the
    simulator computes independently (see app.bundles / build_bgp_bundles)."""
    peer_adjacency: dict[str, list[str]] = {}
    for pair in pairs:
        peer_adjacency.setdefault(pair.router_a_mgmt_ip, []).append(pair.router_b_mgmt_ip)
        peer_adjacency.setdefault(pair.router_b_mgmt_ip, []).append(pair.router_a_mgmt_ip)

    result = []
    seeded_bundles = []
    for pair in pairs:
        router_a = db.query(Router).filter(Router.mgmt_ip == pair.router_a_mgmt_ip).first()
        router_b = db.query(Router).filter(Router.mgmt_ip == pair.router_b_mgmt_ip).first()
        if router_a is None or router_b is None or router_a.id == router_b.id:
            continue

        id_a, id_b = sorted([router_a.id, router_b.id])
        peering = (
            db.query(BgpPeering)
            .filter(BgpPeering.router_a_id == id_a, BgpPeering.router_b_id == id_b)
            .first()
        )
        if peering is None:
            peering = BgpPeering(router_a_id=id_a, router_b_id=id_b, status=BgpSessionStatus.ESTABLISHED)
            db.add(peering)
        peering.distance_km = pair.distance_km
        peering.repeater_count = pair.repeater_count
        db.flush()

        a_index = sorted(peer_adjacency[router_a.mgmt_ip]).index(router_b.mgmt_ip)
        b_index = sorted(peer_adjacency[router_b.mgmt_ip]).index(router_a.mgmt_ip)
        bundle_a = ensure_bundles_for_peering(db, peering.id, router_a, a_index)
        bundle_b = ensure_bundles_for_peering(db, peering.id, router_b, b_index)

        result.append(peering)
        seeded_bundles.append((peering, bundle_a, bundle_b))

    db.commit()
    for obj in result:
        db.refresh(obj)

    # Mirror the same topology into the Neo4j graph app.correlation
    # traverses for root-cause lookups - see app/topology_graph.py. Runs
    # after the Postgres commit so the graph never gets ahead of the
    # alarm store's own view of what peerings/bundles exist.
    for peering, bundle_a, bundle_b in seeded_bundles:
        sync_peering_to_neo4j(peering, bundle_a, bundle_b)

    return result
