from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.bgp_topology import build_established_adjacency, shortest_reroute_path
from app.bundles import update_member_and_bundle
from app.correlation import try_link_root_cause
from app.models import BgpPeering, BgpSessionStatus, BundleStatus, Incident, IncidentStatus, IncidentType, Router, RouterStatus, TrapEvent
from app.remediation.engine import NOTIFY_ONLY_REASONS, maybe_remediate, needs_attention_router_ids
from app.snmp.flapping import evaluate_isis_adjacency_event, evaluate_link_event, open_incident, resolve_incident_for_peering
from app.snmp.oid_map import (
    BGP_ESTABLISHED_OID,
    BGP_BACKWARD_TRANSITION_OID,
    BGP_PEER_OID,
    FIBER_CUT_CLEAR_OID,
    IF_NAME_OID,
    ISIS_ADJACENCY_DOWN_OID,
    ISIS_ADJACENCY_UP_OID,
    LINK_DOWN_OID,
    LINK_UP_OID,
    ROUTER_ID_OID,
    TRAP_OID_MAP,
    UNKNOWN_TRAP,
)


def _extract_varbind(varbinds: list[tuple[str, str]], oid: str) -> str | None:
    for vb_oid, value in varbinds:
        if vb_oid == oid:
            return value
    return None


def _update_bgp_peering(db: Session, router: Router, peer_mgmt_ip: str | None, new_status: BgpSessionStatus, now: datetime) -> BgpPeering | None:
    """Updates the BgpPeering row between `router` and the neighbor named
    by `peer_mgmt_ip`, if both the peer and a seeded peering between them
    exist. Returns None otherwise (e.g. an as-yet-unseeded topology, or a
    trap that doesn't carry peer info)."""
    if not peer_mgmt_ip:
        return None

    peer = db.query(Router).filter(Router.mgmt_ip == peer_mgmt_ip).first()
    if peer is None or peer.id == router.id:
        return None

    id_a, id_b = sorted([router.id, peer.id])
    # Locked: this row is shared between both peers, and a trap from either
    # side can update it (see classify_and_store's concurrent trap
    # processing) - without the lock, two near-simultaneous traps from
    # opposite ends of the same peering could race read-modify-write and
    # leave it on whichever side happened to commit last.
    peering = (
        db.query(BgpPeering)
        .filter(BgpPeering.router_a_id == id_a, BgpPeering.router_b_id == id_b)
        .with_for_update()
        .first()
    )
    if peering is None:
        return None

    peering.status = new_status
    peering.last_changed_at = now
    db.flush()
    return peering


def _log_transient_incident(
    db: Session,
    router: Router,
    incident_type: IncidentType,
    description: str,
    now: datetime,
    interface_name: str | None = None,
    peering_id: int | None = None,
) -> tuple[Incident, bool]:
    # Types with no automated fix (see NOTIFY_ONLY_REASONS) are genuinely
    # still unhandled - nothing has resolved them, they're just waiting on
    # a human - so they stay OPEN, deduped/bumped the same way
    # LINK_DOWN/LINK_FLAP are (one open incident per router+type+interface,
    # not a fresh row per repeat trap) - otherwise the same recurring
    # condition (e.g. a chronically-flapping BFD session) would inflate the
    # open-incident count indefinitely instead of reflecting the number of
    # distinct problems. Everything else here is a point-in-time
    # notification whose auto-heal action is presumed to have addressed it,
    # so it's always logged as a fresh already-resolved entry.
    if incident_type in NOTIFY_ONLY_REASONS:
        return open_incident(db, router, incident_type, interface_name, description, peering_id=peering_id)

    incident = Incident(
        router_id=router.id,
        incident_type=incident_type,
        interface_name=interface_name,
        status=IncidentStatus.RESOLVED,
        closed_at=now,
        trap_count=1,
        description=description,
    )
    db.add(incident)
    db.flush()
    return incident, True


def classify_and_store(
    db: Session, source_ip: str, trap_oid: str, varbinds: list[tuple[str, str]], peering_id: int | None = None
) -> dict | None:
    """Classify one decoded trap, persist it, run flap detection, and return
    a JSON-serializable payload for the WebSocket broadcast (or None if the
    trap could not be attributed to a known router). `peering_id` is set
    only for app.fiber_faults' synthetic FIBER_CUT_OID/FIBER_CUT_CLEAR_OID
    events - it attributes the resulting L1 incident to a specific link
    rather than a router, and scopes FIBER_CUT_CLEAR_OID's resolve lookup."""

    router_id_value = _extract_varbind(varbinds, ROUTER_ID_OID)
    interface_name = _extract_varbind(varbinds, IF_NAME_OID)

    lookup_ip = router_id_value or source_ip
    # Locked: traps are now processed concurrently across routers (see
    # snmp/trap_listener.py), and everything below - status, flap-window
    # counts, incident dedup, bundle members - is scoped to this one
    # router. Holding its row lock for the rest of the transaction
    # serializes any other trap for the *same* router behind this one,
    # exactly as if it were still processed one at a time, while traps for
    # different routers still run fully in parallel.
    router = db.query(Router).filter(Router.mgmt_ip == lookup_ip).with_for_update().first()
    if router is None:
        return None

    trap_name, incident_type, severity = TRAP_OID_MAP.get(trap_oid, UNKNOWN_TRAP)
    now = datetime.now(timezone.utc)
    router.last_seen_at = now

    trap_event = TrapEvent(
        router_id=router.id,
        oid=trap_oid,
        trap_name=trap_name,
        interface_name=interface_name,
        severity=severity,
        raw_varbinds=str(varbinds),
    )
    db.add(trap_event)
    db.flush()

    bgp_peering = None
    if trap_oid in (LINK_DOWN_OID, LINK_UP_OID):
        incident, created = evaluate_link_event(db, router, interface_name, trap_oid, now)
    elif trap_oid in (ISIS_ADJACENCY_DOWN_OID, ISIS_ADJACENCY_UP_OID):
        incident, created = evaluate_isis_adjacency_event(db, router, interface_name, trap_oid, now)
        # True redundancy: only cascades into the BGP peering once the whole
        # bundle actually flips (i.e. every member has lost/regained
        # adjacency), not on every single-member blip. No separate
        # BGP_STATE_CHANGE incident/remediation here - if the transport
        # itself is gone, a BGP soft reset wouldn't fix anything.
        bundle, bundle_changed = update_member_and_bundle(
            db, router, interface_name, trap_oid == ISIS_ADJACENCY_UP_OID, now
        )
        if bundle is not None and bundle_changed:
            peer_router = (
                bundle.peering.router_b if bundle.peering.router_a_id == router.id else bundle.peering.router_a
            )
            new_status = BgpSessionStatus.ESTABLISHED if bundle.status == BundleStatus.UP else BgpSessionStatus.DOWN
            bgp_peering = _update_bgp_peering(db, router, peer_router.mgmt_ip, new_status, now)
    elif trap_oid == FIBER_CUT_CLEAR_OID:
        # Resolves the L1 incident app.fiber_faults opened for this
        # peering - peering-scoped, not router+interface-scoped, since a
        # fiber cut isn't attached to any one interface. No-op (drop the
        # trap without a broadcast) if it's already resolved or the
        # peering vanished, same as any other event that arrives too late
        # to matter.
        resolved = resolve_incident_for_peering(db, peering_id, IncidentType.OPTICAL_ALARM, now) if peering_id is not None else None
        if resolved is None:
            db.rollback()
            return None
        incident, created = resolved, False
    else:
        if incident_type in (IncidentType.COLD_START, IncidentType.WARM_START):
            router.status = RouterStatus.UP
        elif trap_oid == BGP_ESTABLISHED_OID:
            bgp_peering = _update_bgp_peering(
                db, router, _extract_varbind(varbinds, BGP_PEER_OID), BgpSessionStatus.ESTABLISHED, now
            )
        elif trap_oid == BGP_BACKWARD_TRANSITION_OID:
            bgp_peering = _update_bgp_peering(
                db, router, _extract_varbind(varbinds, BGP_PEER_OID), BgpSessionStatus.DOWN, now
            )
        description = f"{trap_name} on {router.hostname}"
        if interface_name:
            description += f" (interface {interface_name})"
        incident, created = _log_transient_incident(
            db,
            router,
            incident_type,
            description,
            now,
            interface_name=interface_name,
            peering_id=peering_id,
        )

    trap_event.incident_id = incident.id

    # Topology-based root-cause correlation: only relevant the first time an
    # incident opens (a repeat trap just bumping trap_count is already
    # linked or already independent), and only for L3 incidents - see
    # app.correlation.
    if created:
        try_link_root_cause(db, incident, now)

    db.commit()
    db.refresh(trap_event)
    db.refresh(incident)
    db.refresh(router)
    if bgp_peering:
        db.refresh(bgp_peering)

    # Auto-heal pipeline: only on the first occurrence of a given open
    # incident, never on repeat traps that just bump an existing incident's
    # trap_count (otherwise every re-trigger would take a fresh backup), and
    # never for an incident that's just a symptom of an already-open L1
    # root cause - bouncing a downstream interface wouldn't fix a fiber cut.
    remediation = maybe_remediate(db, router, incident) if created and incident.root_cause_incident_id is None else None

    # Computed fresh after remediation/incident state settles, so a router
    # whose incident just got resolved (e.g. by this same linkUp) correctly
    # drops off the "needs attention" list without any extra bookkeeping.
    needs_attention = router.id in needs_attention_router_ids(db, [router.id])

    incident_payload = {
        "id": incident.id,
        "router_id": router.id,
        "incident_type": incident.incident_type.value,
        "status": incident.status.value,
        "trap_count": incident.trap_count,
        "description": incident.description,
        "layer": incident.layer.value,
        "peering_id": incident.peering_id,
        "root_cause_incident_id": incident.root_cause_incident_id,
    }
    if remediation:
        incident_payload["remediation"] = remediation

    result = {
        "type": "trap",
        "trap": {
            "id": trap_event.id,
            "router_id": router.id,
            "hostname": router.hostname,
            "trap_name": trap_event.trap_name,
            "interface_name": trap_event.interface_name,
            "severity": trap_event.severity,
            "received_at": trap_event.received_at.isoformat() if trap_event.received_at else None,
        },
        "incident": incident_payload,
        "router": {
            "id": router.id,
            "hostname": router.hostname,
            "status": router.status.value,
            "needs_attention": needs_attention,
            "router_type": router.router_type.value,
        },
    }
    if bgp_peering:
        reroute_path = None
        if bgp_peering.status == BgpSessionStatus.DOWN:
            adjacency = build_established_adjacency(db)
            reroute_path = shortest_reroute_path(adjacency, bgp_peering.router_a_id, bgp_peering.router_b_id)
        result["bgp_peering"] = {
            "id": bgp_peering.id,
            "router_a_id": bgp_peering.router_a_id,
            "router_b_id": bgp_peering.router_b_id,
            "status": bgp_peering.status.value,
            "reroute_path": reroute_path,
        }
    return result
