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

    return {
        "incident_id": incident.id,
        "description": description,
        "suggested_solution": solution,
        "recommended_team_kind": team_kind,
        "subject": f"[NCR] {incident.incident_type.value} on {router.hostname} (incident #{incident.id}) needs review",
    }
