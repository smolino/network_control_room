"""Human-readable incident analysis for the Human Review tab.

Auto-heal (see engine.py) already decides, per incident type, whether a
config change can fix the problem or whether it should go to a human
instead (NOTIFY_ONLY_REASONS). This module is the human-facing half of
that same decision: a plain-English description of what happened and a
suggested next step, plus which kind of team (SOC vs. maintenance)
normally owns that kind of problem - used to pre-select a sensible
default in the "send to team" dropdown, not to hard-block sending to the
other kind.
"""

from app.fault_location import locate_fault
from app.models import AlarmClass
from app.snmp.alarm_classes import condition_for
from app.models import Incident, IncidentType, Router, TeamKind

# incident_type -> (team_kind, "what happened" template, "what to do" template)
# Templates may reference {router}, {iface} and {traps}.
GUIDANCE: dict[IncidentType, tuple[TeamKind, str, str]] = {
    IncidentType.AUTH_FAILURE: (
        TeamKind.SOC,
        "Repeated authentication failures were recorded on {router}"
        " ({traps} trap(s) so far). This may indicate a brute-force attempt"
        " or the use of stale/compromised credentials against the device's"
        " management plane.",
        "Pull the AAA/TACACS+ logs for {router} around the trap timestamps to"
        " identify the source address and account involved. If the attempts"
        " don't match a known change window, lock the account, rotate the"
        " credential, and check whether the same source hit other devices.",
    ),
    IncidentType.ENV_ALARM: (
        TeamKind.MAINTENANCE,
        "An environmental alarm (temperature, humidity, or power feed) was"
        " raised on {router}. This is a site-condition issue, not something"
        " fixable with a configuration change.",
        "Dispatch a technician to check site HVAC and power, and confirm the"
        " chassis's own temperature/voltage sensors are back within spec"
        " before considering the incident closed.",
    ),
    IncidentType.CONFIG_CHANGE: (
        TeamKind.MAINTENANCE,
        "{router}'s running configuration changed outside of the normal"
        " change process. A pre-change snapshot was captured automatically"
        " for diffing, but no corrective action was taken.",
        "Diff the captured backup against the previous revision (see the"
        " router's Config Backups panel) to confirm the change was"
        " authorized. If not, restore the prior configuration and follow up"
        " on how the out-of-band access was obtained.",
    ),
    IncidentType.BFD_SESSION_DOWN: (
        TeamKind.MAINTENANCE,
        "A BFD session on {router}{iface_clause} dropped, indicating a"
        " fast-detected link or forwarding failure between {router} and its"
        " peer.",
        "Check the physical layer first (fiber/optics, patch panel,"
        " last-mile circuit) between {router} and its neighbor, then confirm"
        " the peer's BFD/interface state before re-enabling the session.",
    ),
    IncidentType.ISIS_NBR_DOWN: (
        TeamKind.MAINTENANCE,
        "An IS-IS adjacency on {router}{iface_clause} was lost. This is"
        " usually a symptom of an underlying physical or BFD issue on that"
        " bundle member rather than an IS-IS-specific misconfiguration -"
        " the BGP peering itself is only affected once every member of the"
        " bundle has lost adjacency.",
        "Correlate with any BFD/interface events on the same link around the"
        " same time; once the physical path is confirmed healthy, verify the"
        " adjacency re-establishes on its own before closing this out.",
    ),
    IncidentType.OPTICAL_ALARM: (
        TeamKind.MAINTENANCE,
        "A transceiver on {router}{iface_clause} crossed an optical power"
        " threshold, pointing to a degrading fiber run or optic rather than"
        " a software issue.",
        "Have a technician measure Rx/Tx power on the affected optic and"
        " inspect the fiber run for damage or contamination; replace the"
        " transceiver if power remains out of spec after cleaning.",
    ),
    IncidentType.FAN_FAILURE: (
        TeamKind.MAINTENANCE,
        "A chassis fan failure was reported on {router}. Continued"
        " operation risks a thermal shutdown if the remaining fans can't"
        " keep up.",
        "Schedule a field visit to replace the failed fan tray/module."
        " Monitor chassis temperature in the meantime and prioritize the"
        " dispatch if temperatures start trending up.",
    ),
    IncidentType.PSU_FAILURE: (
        TeamKind.MAINTENANCE,
        "A power supply failure was reported on {router}. If this router"
        " isn't dual-fed, it is now running without power redundancy.",
        "Dispatch a technician to replace the failed PSU as soon as possible."
        " Until then, treat {router} as a single point of failure for power"
        " and avoid scheduling other disruptive maintenance on it.",
    ),
    IncidentType.HIGH_MEMORY: (
        TeamKind.MAINTENANCE,
        "{router} is reporting sustained high memory utilization"
        " ({traps} trap(s) so far), which risks process instability or a"
        " crash if it continues climbing.",
        "Check `show processes memory` (or the vendor equivalent) for the"
        " top consumer and confirm whether it matches a known memory leak"
        " for this platform/version. Plan a maintenance-window reload if"
        " memory doesn't recover on its own.",
    ),
}

DEFAULT_GUIDANCE: tuple[TeamKind, str, str] = (
    TeamKind.MAINTENANCE,
    "{router} raised a {incident_type} incident{iface_clause}"
    " ({traps} trap(s) so far) that wasn't auto-remediated.",
    "Review the recent trap history and current status for {router} and"
    " decide whether manual intervention is required.",
)


def generate_analysis(incident: Incident, router: Router) -> dict:
    team_kind, summary_tmpl, solution_tmpl = GUIDANCE.get(incident.incident_type, DEFAULT_GUIDANCE)

    iface_clause = f" on interface {incident.interface_name}" if incident.interface_name else ""
    fmt_kwargs = {
        "router": router.hostname,
        "iface": incident.interface_name or "the affected interface",
        "iface_clause": iface_clause,
        "traps": incident.trap_count,
        "incident_type": incident.incident_type.value,
    }

    description = summary_tmpl.format(**fmt_kwargs)
    solution = solution_tmpl.format(**fmt_kwargs)

    # A fiber cut whose alarm reported the span (see app.fault_location)
    # gets a dispatch the field crew can act on directly instead of the
    # generic "measure the optic" advice.
    location = (
        locate_fault(incident.peering, incident.fault_segment_index)
        if incident.peering is not None and incident.fault_segment_index is not None
        else None
    )
    if location:
        description, solution = _fiber_fault_guidance(location)
    elif incident.corridor_id is not None:
        description, solution = _rca_guidance(incident, router)
    elif incident.condition:
        description, solution = _condition_guidance(incident, router)

    return {
        "incident_id": incident.id,
        "description": description,
        "suggested_solution": solution,
        "recommended_team_kind": team_kind,
        "subject": f"[NCR] {incident.incident_type.value} on {router.hostname} (incident #{incident.id}) needs review",
    }


def _fiber_fault_guidance(location: dict) -> tuple[str, str]:
    start, end = location["start"], location["end"]

    def where(p: dict) -> str:
        return f"{p['label']} (km {p['km_from_a']} from {location['router_a']}, {p['latitude']}, {p['longitude']})"

    description = (
        f"Loss of optical signal on the fiber run {location['router_a']} <-> {location['router_b']}"
        f" ({location['distance_km']} km, {location['repeater_count']} repeaters). The fault is isolated"
        f" to span {location['segment_index'] + 1} of {location['segment_count']}, between {where(start)} and"
        f" {where(end)} - a {location['segment_length_km']} km stretch. The BGP session is still carried"
        " by the bundle's other member link(s) or rerouted, so this is a repair, not an outage."
    )
    checks = location["repeaters_to_check"]
    if checks:
        targets = " and ".join(checks)
        solution = (
            f"Dispatch the fiber crew to the {start['label']}-{end['label']} span. Check {targets} first:"
            " confirm power and the alarm/OTDR readings at each site, and replace the repeater that"
            " shows no output (upstream) or no input (downstream). If both repeaters test good, run an"
            " OTDR trace from either end to locate the break in the fiber itself and splice it."
        )
    else:
        solution = (
            f"The fault is on the span between {start['label']} and {end['label']}, with no repeater in"
            " between - check the line card optics at the router end, then OTDR the span to locate the"
            " break."
        )
    return description, solution


def _condition_guidance(incident: Incident, router: Router) -> tuple[str, str]:
    """An optical-element condition (LOS-P, LOS-O, EQPT, ...) - says what
    the condition means on this port. Where along the path the fault is
    comes from correlating the conditions along the corridor, not from one
    alarm on its own."""
    condition = condition_for(incident.condition)
    meaning = condition.meaning if condition else incident.condition
    port = f" port {incident.interface_name}" if incident.interface_name else ""
    role = router.element_role.value if router.element_role else "router"
    description = f"{incident.condition} on {role} {router.hostname}{port}: {meaning}."
    if incident.alarm_class == AlarmClass.TRANSPORT:
        solution = (
            "Check which element along the path lost light first: the span feeding the first element that"
            " reports loss of signal (with its OSC lost too) is the suspect. If only this element alarms,"
            " check its own input patching and optics before dispatching to the span."
        )
    else:
        solution = (
            f"Hardware/environment alarm on {router.hostname} - not a fiber fault. Dispatch site maintenance"
            " to inspect or replace the card/unit named in the alarm."
        )
    return description, solution


def _rca_guidance(incident: Incident, router: Router) -> tuple[str, str]:
    """A root cause app.rca inferred on a modelled corridor: who to send
    where, with the span's provider and both ends' sites when it's a span."""
    from sqlalchemy.orm import object_session

    from app.models import FiberSpan

    description = incident.description or ""
    span = None
    db = object_session(incident)
    if db is not None and incident.fault_segment_index is not None:
        span = (
            db.query(FiberSpan)
            .filter(FiberSpan.corridor_id == incident.corridor_id, FiberSpan.seq == incident.fault_segment_index)
            .first()
        )

    def site(device) -> str:
        s = device.site
        where = f"{s.name}, {s.city}" if s and s.city and s.city not in s.name else (s.name if s else device.hostname)
        coords = f" ({s.latitude:.4f}, {s.longitude:.4f})" if s else ""
        return f"{device.hostname} at {where}{coords}"

    kind = incident.condition
    if kind in ("CABLE-CUT", "FIBER-CUT") and span is not None:
        ref = f", circuit {span.provider_circuit_id}" if span.provider_circuit_id else ""
        which = "both fibers of the pair" if kind == "CABLE-CUT" else "one fiber of the pair (the other direction still carries light)"
        solution = (
            f"Open a ticket with {span.provider or 'the fiber provider'}{ref} for the dark-fiber span between"
            f" {site(span.from_device)} (end {span.a_end or 'A'}) and {site(span.to_device)} (end {span.b_end or 'B'}),"
            f" {span.length_km or '?'} km: {which} lost. Ask for an OTDR trace from the nearest end to locate the break;"
            " nothing needs replacing at either site - the amplifier after the break reports its input lost, not a"
            " failure of its own."
        )
    elif kind == "AMP-FAIL":
        solution = (
            f"Dispatch to {site(router)}: the amplifier card for that direction stopped amplifying (the next site's"
            " supervisory channel is fine, so the fiber is intact). Replace the card; traffic resumes as soon as it's"
            " back in service."
        )
    elif kind == "TRUNK-FAIL":
        solution = (
            f"Dispatch to {site(router)}: check the transponder trunk's transmit power and the patch cord from the"
            " trunk to the mux add port; replace the trunk optics if it doesn't transmit."
        )
    elif kind == "CHANNEL-FAIL":
        solution = (
            "Check the mux/demux channel for this wavelength at both terminals (add/drop patching and channel"
            " provisioning) - the line and the other wavelengths are fine."
        )
    elif kind == "PATCH-CORD":
        solution = (
            f"Dispatch to {site(router)}: clean or replace the patch cord between the router port and the transponder"
            " client port named above, and check both optics' power."
        )
    elif kind == "OSC-FAIL":
        solution = (
            f"Low urgency, traffic unaffected: check the OSC card/filter at {site(router)} - the span itself carries"
            " traffic, so the fiber is fine. Until fixed, faults on this span can't be told apart from amplifier failures."
        )
    else:
        solution = "Review the correlated alarms listed as symptoms of this incident."
    return description, solution
