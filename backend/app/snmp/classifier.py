from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.bgp_topology import build_established_adjacency, shortest_reroute_path
from app.bundles import update_member_and_bundle
from app.fault_location import describe_fault, locate_fault
from app.incidents import service as incident_service
from app.models import BgpPeering, BgpSessionStatus, BundleStatus, IncidentType, Router, RouterStatus, TrapEvent, TrapRuleAction
from app.snmp import alarm_classes, mib_registry
from app.snmp.flapping import evaluate_isis_adjacency_event, evaluate_link_event
from app.snmp.oid_map import (
    BGP_ESTABLISHED_OID,
    BGP_BACKWARD_TRANSITION_OID,
    BGP_PEER_OID,
    CONDITION_OID,
    FIBER_CUT_CLEAR_OID,
    FIBER_CUT_OID,
    FIBER_SEGMENT_OID,
    IF_NAME_OID,
    ISIS_ADJACENCY_DOWN_OID,
    ISIS_ADJACENCY_UP_OID,
    LINK_DOWN_OID,
    LINK_UP_OID,
    OPTICAL_ALARM_CLEAR_OID,
    OPTICAL_ALARM_OID,
    ROUTER_ID_OID,
    TRAP_OID_MAP,
    lookup_trap,
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


def _parse_segment(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def classify_and_store(
    db: Session, source_ip: str, trap_oid: str, varbinds: list[tuple[str, str]], peering_id: int | None = None
) -> dict | None:
    """Classify one decoded trap, persist it as an alarm (TrapEvent), open/
    bump/clear whatever incident it affects, and return a JSON-serializable
    payload for the WebSocket broadcast (or None if the trap could not be
    attributed to a known router). Auto-heal doesn't run here - a new
    incident waits out its settle window first (see app.incidents.service).
    `peering_id` is set only for app.fiber_faults' synthetic FIBER_CUT_OID/
    FIBER_CUT_CLEAR_OID events - it attributes the resulting L1 incident to
    a specific link rather than a router, and scopes FIBER_CUT_CLEAR_OID's
    resolve lookup."""

    router_id_value = _extract_varbind(varbinds, ROUTER_ID_OID)
    interface_name = _extract_varbind(varbinds, IF_NAME_OID)

    lookup_ip = router_id_value or source_ip
    # Locked: traps are processed concurrently across routers, and
    # everything below - status, flap-window counts, incident dedup, bundle
    # members - is scoped to this one router. Holding its row lock for the
    # rest of the transaction serializes any other trap for the *same*
    # router behind this one, while traps for different routers still run
    # fully in parallel.
    router = db.query(Router).filter(Router.mgmt_ip == lookup_ip).with_for_update().first()
    if router is None:
        return None

    trap_name, incident_type, severity = lookup_trap(trap_oid)
    # Custom trap rules and uploaded MIBs (the "MIBs & Traps" tab) only
    # apply to OIDs the built-in TRAP_OID_MAP doesn't handle.
    rule = None
    if trap_oid not in TRAP_OID_MAP:
        rule = mib_registry.rule_for(db, trap_oid)
        if rule is not None:
            trap_name, severity = rule.name, rule.severity
            incident_type = rule.incident_type if rule.action == TrapRuleAction.RAISE else IncidentType.UNKNOWN
            interface_name = interface_name or mib_registry.interface_from_varbinds(rule, varbinds)
        else:
            trap_name = mib_registry.imported_name(db, trap_oid) or trap_name
    # Optical-element alarms carry a condition code that decides what they
    # are (see app.snmp.alarm_classes); an unknown code stays alarm-only.
    condition = None
    condition_code = None
    if alarm_classes.is_optical_alarm(trap_oid):
        condition_code = (_extract_varbind(varbinds, CONDITION_OID) or "").strip().upper() or None
        condition = alarm_classes.condition_for(condition_code)
        if condition is not None:
            incident_type = condition.incident_type
            if trap_oid == OPTICAL_ALARM_OID:
                severity = condition.severity
        else:
            incident_type = IncidentType.UNKNOWN
        trap_name = f"{trap_name}:{condition_code or '?'}"[:64]
    alarm_class = alarm_classes.classify(trap_oid, incident_type, condition)
    filtered = alarm_class in alarm_classes.filtered_classes(db)

    now = datetime.now(timezone.utc)
    router.last_seen_at = now

    trap_event = TrapEvent(
        router_id=router.id,
        oid=trap_oid,
        trap_name=trap_name,
        interface_name=interface_name,
        severity=severity,
        raw_varbinds=str(varbinds),
        alarm_class=alarm_class,
        condition=condition_code,
        filtered=filtered or None,
    )
    db.add(trap_event)
    db.flush()

    bgp_peering = None
    incident = None
    if trap_oid in (LINK_DOWN_OID, LINK_UP_OID):
        incident = evaluate_link_event(db, router, interface_name, trap_oid, severity, now)
    elif trap_oid in (ISIS_ADJACENCY_DOWN_OID, ISIS_ADJACENCY_UP_OID):
        incident = evaluate_isis_adjacency_event(db, router, interface_name, trap_oid, severity, now)
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
        # fiber cut isn't attached to any one interface. Dropped without a
        # broadcast if it's already resolved or the peering vanished, same
        # as any other event that arrives too late to matter. With auto-heal
        # off nothing is resolved, but the alarm is still kept so engineers
        # can see the signal came back.
        if peering_id is None or (
            not incident_service.clear_peering(db, peering_id, IncidentType.OPTICAL_ALARM, now)
            and incident_service.auto_heal_enabled(db)
        ):
            db.rollback()
            return None
    elif trap_oid == OPTICAL_ALARM_CLEAR_OID:
        # Clears are never filtered: an incident raised before the filter
        # was switched on still deserves its clear.
        if condition is not None:
            incident_service.clear(db, router, [condition.incident_type], interface_name, now, condition=condition.code)
    elif trap_oid == BGP_ESTABLISHED_OID:
        bgp_peering = _update_bgp_peering(
            db, router, _extract_varbind(varbinds, BGP_PEER_OID), BgpSessionStatus.ESTABLISHED, now
        )
        incident_service.clear(db, router, [IncidentType.BGP_STATE_CHANGE], interface_name, now)
    elif rule is not None and rule.action == TrapRuleAction.CLEAR:
        incident_service.clear(db, router, [rule.incident_type], interface_name, now)
    else:
        if incident_type in (IncidentType.COLD_START, IncidentType.WARM_START):
            router.status = RouterStatus.UP
        elif trap_oid == BGP_BACKWARD_TRANSITION_OID:
            bgp_peering = _update_bgp_peering(
                db, router, _extract_varbind(varbinds, BGP_PEER_OID), BgpSessionStatus.DOWN, now
            )
        opens_incident = incident_service.is_incident_type(incident_type) and not filtered
        if condition is not None and not condition.opens_incident:
            opens_incident = False
        if opens_incident:
            description = f"{trap_name} on {router.hostname}"
            if interface_name:
                description += f" (interface {interface_name})"
            if condition is not None:
                where = f" port {interface_name}" if interface_name else ""
                description = f"{condition.code} on {router.hostname}{where}: {condition.meaning}"[:255]
            fault_segment_index = None
            if trap_oid == FIBER_CUT_OID and peering_id is not None:
                fault_segment_index = _parse_segment(_extract_varbind(varbinds, FIBER_SEGMENT_OID))
                peering = db.query(BgpPeering).filter(BgpPeering.id == peering_id).first()
                location = locate_fault(peering, fault_segment_index) if peering and fault_segment_index is not None else None
                if location:
                    description = f"{trap_name}: fiber fault in {describe_fault(location)}"[:255]
            incident, _ = incident_service.open_or_bump(
                db,
                router,
                incident_type,
                interface_name,
                description,
                severity,
                now,
                peering_id=peering_id,
                fault_segment_index=fault_segment_index,
                alarm_class=alarm_class,
                condition=condition.code if condition is not None else None,
            )

    trap_event.incident_id = incident.id if incident else None

    db.commit()
    db.refresh(trap_event)
    db.refresh(router)
    if bgp_peering:
        db.refresh(bgp_peering)

    extra = {
        "trap": {
            "id": trap_event.id,
            "router_id": router.id,
            "hostname": router.hostname,
            "trap_name": trap_event.trap_name,
            "interface_name": trap_event.interface_name,
            "severity": trap_event.severity,
            "received_at": trap_event.received_at.isoformat() if trap_event.received_at else None,
            "incident_id": trap_event.incident_id,
            "alarm_class": alarm_class.value,
            "condition": condition_code,
            "filtered": filtered,
        }
    }
    if bgp_peering:
        reroute_path = None
        if bgp_peering.status == BgpSessionStatus.DOWN:
            adjacency = build_established_adjacency(db)
            reroute_path = shortest_reroute_path(adjacency, bgp_peering.router_a_id, bgp_peering.router_b_id)
        extra["bgp_peering"] = {
            "id": bgp_peering.id,
            "router_a_id": bgp_peering.router_a_id,
            "router_b_id": bgp_peering.router_b_id,
            "status": bgp_peering.status.value,
            "reroute_path": reroute_path,
        }
    return incident_service.pop_event(db, "trap", routers=[router], **extra)
