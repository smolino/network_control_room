"""Rule-based auto-heal engine.

For every newly-opened incident of an "actionable" type, the pipeline is
always the same two steps:

1. Back up the router's running configuration (`backup_router_config`) -
   an audit-safe snapshot taken *before* anything else happens, so any
   change made below (or by a human afterwards) can be diffed/rolled back.
2. Run whatever remediation the incident type calls for
   (`REMEDIATION_PLAN`), or explicitly skip with a reason if no automated
   fix is appropriate (e.g. security or hardware incidents, which should
   go to a human rather than be auto-remediated).

These are simulated Cisco routers with no real management plane behind
them, so "backup" and "remediation" here are synthetic stand-ins for what
would, against real hardware, be an SSH/NETCONF/RESTCONF session that
pulls `show running-config` and then pushes the fix. The audit trail
(what was backed up, what action was attempted, whether it succeeded) is
real and persisted the same way it would be in production.
"""

import random
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.bundles import ISIS_PROCESS_TAG, bundle_and_member_names, isis_net
from app.models import (
    BgpPeering,
    Incident,
    IncidentStatus,
    IncidentType,
    RemediationAction,
    RemediationActionType,
    RemediationStatus,
    Router,
    RouterConfigBackup,
    RouterType,
)

# Incident types worth running the auto-heal pipeline for at all. Pure
# "good news" / informational events (link recovered, device booted,
# unrecognized trap) don't need a backup or a healing decision.
AUTO_HEAL_ELIGIBLE_TYPES = {
    IncidentType.LINK_DOWN,
    IncidentType.LINK_FLAP,
    IncidentType.HIGH_CPU,
    IncidentType.BGP_STATE_CHANGE,
    IncidentType.AUTH_FAILURE,
    IncidentType.ENV_ALARM,
    IncidentType.CONFIG_CHANGE,
    IncidentType.BFD_SESSION_DOWN,
    IncidentType.ISIS_NBR_DOWN,
    IncidentType.OPTICAL_ALARM,
    IncidentType.FAN_FAILURE,
    IncidentType.PSU_FAILURE,
    IncidentType.HIGH_MEMORY,
}

# incident_type -> (action_type, "what we did" template, "{iface}" optional)
REMEDIATION_PLAN: dict[IncidentType, tuple[RemediationActionType, str]] = {
    IncidentType.LINK_DOWN: (
        RemediationActionType.INTERFACE_BOUNCE,
        "Bounced {iface} (shutdown / no shutdown) to attempt recovery",
    ),
    IncidentType.LINK_FLAP: (
        RemediationActionType.CLEAR_COUNTERS,
        "Cleared interface counters and applied error-disable dampening on {iface}",
    ),
    IncidentType.HIGH_CPU: (
        RemediationActionType.PROCESS_RESTART,
        "Restarted the offending high-CPU process",
    ),
    IncidentType.BGP_STATE_CHANGE: (
        RemediationActionType.BGP_NEIGHBOR_RESET,
        "Issued a soft reset of the BGP neighbor session",
    ),
}

# Types with no automated fix — explain why rather than silently skipping.
NOTIFY_ONLY_REASONS: dict[IncidentType, str] = {
    IncidentType.AUTH_FAILURE: "Possible security event — flagged for human review instead of auto-remediating",
    IncidentType.ENV_ALARM: "Environmental/hardware alarm — cannot be fixed by config change, NOC notified",
    IncidentType.CONFIG_CHANGE: "Configuration was already changed out-of-band — snapshot kept for diffing, no action taken",
    IncidentType.BFD_SESSION_DOWN: "Fast-failure-detection event — carrier-side/physical, needs NOC/carrier engagement rather than a config push",
    IncidentType.ISIS_NBR_DOWN: "IS-IS adjacency lost on a bundle member interface — likely a transient physical issue; the peering itself is only affected once every member of the bundle has lost adjacency, but NOC is notified for the individual member too",
    IncidentType.OPTICAL_ALARM: "Transceiver power threshold crossed — hardware/fiber issue, cannot be fixed by config change, NOC notified",
    IncidentType.FAN_FAILURE: "Chassis fan failure — hardware issue, NOC/dispatch notified",
    IncidentType.PSU_FAILURE: "Redundant power supply failure — hardware issue, NOC/dispatch notified",
    IncidentType.HIGH_MEMORY: "Memory pool exhaustion — flagged for human review rather than an automated process restart",
}

# Simulated success rate for actions that do attempt a fix.
SIMULATED_SUCCESS_RATE = 0.85


def _bgp_peers_sorted(db: Session, router: Router) -> list[tuple[Router, BgpPeering]]:
    """The router's (peer, peering) pairs, in the same mgmt_ip-ascending
    order the simulator uses to number bundle/FortyGigE interfaces
    (build_bgp_bundles in trap_simulator.py, app.bundles.bundle_and_member_names
    here) - so a trap naming "Port-channel2" or "FortyGigE0/0/2" and this
    router's config snapshot always agree on which peer that interface is
    for."""
    peerings = (
        db.query(BgpPeering)
        .filter(or_(BgpPeering.router_a_id == router.id, BgpPeering.router_b_id == router.id))
        .all()
    )
    if not peerings:
        return []
    peering_by_peer_id = {p.router_b_id if p.router_a_id == router.id else p.router_a_id: p for p in peerings}
    peers = db.query(Router).filter(Router.id.in_(peering_by_peer_id.keys())).all()
    return sorted(((peer, peering_by_peer_id[peer.id]) for peer in peers), key=lambda pair: pair[0].mgmt_ip)


def _simulated_config_snapshot(db: Session, router: Router, revision: int) -> str:
    """One `interface` block per real connection - a BGP-peer-facing 80Gb
    Port-channel (2x40G FortyGigE members, channel-group bundled for
    redundancy, IS-IS-enabled) for each of this primary's BGP neighbors,
    plus a customer-facing GigabitEthernet port for each of its customer
    routers - built fresh from the current topology every time a backup
    is taken, so it always reflects reality rather than a static template.
    IS-IS runs only on the bundles (and Loopback0, so it's reachable) - it's
    the IGP that provides underlying reachability for the eBGP-over-loopback
    sessions below, and the physical adjacency that app.bundles tracks per
    member interface; customer-facing ports and CPE don't run it."""
    lines = [
        f"! Simulated running-config snapshot for {router.hostname}",
        f"! Backup revision {revision} - {datetime.now(timezone.utc).isoformat()}",
        f"hostname {router.hostname}",
        "!",
    ]

    if router.router_type == RouterType.CUSTOMER:
        lines += [
            "interface GigabitEthernet0/0",
            " description uplink to primary",
            f" ip address {router.mgmt_ip} 255.255.255.252",
            " no shutdown",
            "!",
            "end",
            "",
        ]
        return "\n".join(lines)

    lines += [
        f"router isis {ISIS_PROCESS_TAG}",
        f" net {isis_net(router)}",
        " is-type level-2-only",
        " metric-style wide",
        "!",
    ]

    peers = _bgp_peers_sorted(db, router)
    for i, (peer, peering) in enumerate(peers):
        bundle_name, member_names = bundle_and_member_names(i)
        fiber_note = ""
        if peering.distance_km is not None:
            fiber_note = f" — {peering.distance_km:.0f}km SMF, {peering.repeater_count} repeater(s)"
        lines += [
            f"interface {bundle_name}",
            f" description 80Gb (2x40GbE) BGP peer to {peer.hostname} (AS{peer.asn}){fiber_note}",
            f" ip address 10.255.{router.id % 250}.{i * 4 + 1} 255.255.255.252",
            f" ip router isis {ISIS_PROCESS_TAG}",
            " isis network point-to-point",
            " isis metric 10 level-2",
            " no shutdown",
            "!",
        ]
        for member_name in member_names:
            lines += [
                f"interface {member_name}",
                f" description member of {bundle_name} (BGP peer {peer.hostname})",
                f" channel-group {i + 1} mode active",
                " bandwidth 40000000",
                " no shutdown",
                "!",
            ]

    customers = sorted(
        db.query(Router).filter(Router.parent_router_id == router.id).all(),
        key=lambda c: int(c.mgmt_ip.rsplit(".", 1)[-1]),
    )
    for customer in customers:
        c_idx = int(customer.mgmt_ip.rsplit(".", 1)[-1])
        lines += [
            f"interface GigabitEthernet0/{c_idx}",
            f" description customer uplink to {customer.hostname}",
            f" ip address {customer.mgmt_ip} 255.255.255.252",
            " no shutdown",
            "!",
        ]

    lines += [
        "interface Loopback0",
        f" ip address {router.mgmt_ip} 255.255.255.255",
        f" ip router isis {ISIS_PROCESS_TAG}",
        " isis metric 0 level-2",
        "!",
        f"router bgp {router.asn or 65000}",
    ]
    for peer, _peering in peers:
        lines.append(f" neighbor {peer.mgmt_ip} remote-as {peer.asn or 65000}")
    lines += ["!", "end", ""]

    return "\n".join(lines)


def backup_router_config(db: Session, router: Router, incident: Incident, reason: str) -> RouterConfigBackup:
    revision = db.query(RouterConfigBackup).filter(RouterConfigBackup.router_id == router.id).count() + 1
    backup = RouterConfigBackup(
        router_id=router.id,
        incident_id=incident.id,
        reason=reason,
        config_text=_simulated_config_snapshot(db, router, revision),
    )
    db.add(backup)
    db.flush()
    return backup


def maybe_remediate(db: Session, router: Router, incident: Incident) -> dict | None:
    """Runs the auto-heal playbook for a newly-opened incident: backup
    first, then act (or explicitly decline to act). Returns a
    JSON-serializable summary for the WebSocket broadcast, or None if this
    incident type isn't auto-heal eligible at all."""

    if incident.incident_type not in AUTO_HEAL_ELIGIBLE_TYPES:
        return None

    backup = backup_router_config(
        db,
        router,
        incident,
        reason=f"Pre-remediation backup for {incident.incident_type.value} incident #{incident.id}",
    )

    plan = REMEDIATION_PLAN.get(incident.incident_type)
    now = datetime.now(timezone.utc)

    if plan is None:
        action = RemediationAction(
            incident_id=incident.id,
            router_id=router.id,
            backup_id=backup.id,
            incident_type=incident.incident_type.value,
            action_type=RemediationActionType.NOTIFY_ONLY,
            status=RemediationStatus.SKIPPED,
            finished_at=now,
            summary=NOTIFY_ONLY_REASONS.get(incident.incident_type, "No automated remediation defined"),
            log=f"[auto-heal] router={router.hostname} backup=#{backup.id}\ndecision=notify-only, no config change attempted",
        )
    else:
        action_type, template = plan
        iface = incident.interface_name or "the affected interface"
        summary = template.format(iface=iface)
        succeeded = random.random() < SIMULATED_SUCCESS_RATE
        action = RemediationAction(
            incident_id=incident.id,
            router_id=router.id,
            backup_id=backup.id,
            incident_type=incident.incident_type.value,
            action_type=action_type,
            status=RemediationStatus.SUCCESS if succeeded else RemediationStatus.FAILED,
            finished_at=now,
            summary=summary if succeeded else f"{summary} — did not clear the condition, escalated to NOC",
            log=(
                f"[auto-heal] router={router.hostname} action={action_type.value}\n"
                f"backup=#{backup.id} taken before this change\n"
                f"result={'ok' if succeeded else 'failed'}"
            ),
        )

    db.add(action)
    db.flush()
    db.commit()
    db.refresh(action)

    return {
        "id": action.id,
        "incident_id": incident.id,
        "router_id": router.id,
        "backup_id": backup.id,
        "action_type": action.action_type.value,
        "status": action.status.value,
        "summary": action.summary,
    }


def needs_attention_router_ids(db: Session, router_ids: list[int] | None = None) -> set[int]:
    """Router ids whose auto-heal attempt did NOT fix the problem: they
    still have an open incident whose (one-shot) remediation action came
    back FAILED. Since a router's status flips back to `up` (and its
    LINK_DOWN/LINK_FLAP incident resolves) independently of whether the
    remediation succeeded, this naturally clears itself once the router
    actually recovers - no separate "un-mark" step needed."""
    query = (
        db.query(Incident.router_id)
        .join(RemediationAction, RemediationAction.incident_id == Incident.id)
        .filter(Incident.status == IncidentStatus.OPEN, RemediationAction.status == RemediationStatus.FAILED)
        .distinct()
    )
    if router_ids is not None:
        query = query.filter(Incident.router_id.in_(router_ids))
    return {row[0] for row in query.all()}
