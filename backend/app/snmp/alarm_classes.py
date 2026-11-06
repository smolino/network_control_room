"""Alarm classes and the optical condition catalogue.

Every trap is put in one AlarmClass (see models.AlarmClass):

* TRANSPORT - the optical path lost light or framing (LOS, LOS-O, LOF,
  AIS, client LF/RF...). The only class that can be the root cause of a
  fiber fault.
* LOGICAL - what L3 sees as a consequence (link down, IS-IS, BFD, BGP).
* EQUIPMENT - hardware and environment (card failure, fan, power,
  temperature, CPU, memory, sensor thresholds). Real problems, but never a
  fiber fault - and the noise that drowns one out.
* MANAGEMENT - device management (boot, authentication, configuration).

The alarm filter (AppSetting "alarm_filter", Settings > Simulation) lists
classes that are kept as alarm history only and never open an incident -
by default EQUIPMENT, so the incident list shows what the fiber network is
doing.

Optical elements report conditions, not one trap per problem: an ONS 15454
or NCS1004 alarm carries a condition code (Cisco's own names: LOS-P, LOS-O,
OPWR-LFAIL, EQPT, ...) and a raise/clear flag. Our mediation normalizes
those to OPTICAL_ALARM_OID / OPTICAL_ALARM_CLEAR_OID with the code in
CONDITION_OID and the port in IF_NAME_OID - the same shape the trap
simulator sends. A real deployment maps the vendor traps onto that (or
uploads the vendor MIB, see the MIBs & Traps tab).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import AlarmClass, AlarmLayer, AppSetting, ElementRole, IncidentType, Router
from app.snmp.oid_map import (
    AUTH_FAILURE_OID,
    BFD_SESSION_DOWN_OID,
    BGP_BACKWARD_TRANSITION_OID,
    BGP_ESTABLISHED_OID,
    CISCO_CONFIG_MAN_OID,
    CISCO_CPU_RISING_OID,
    CISCO_ENV_FAN_OID,
    CISCO_ENV_SUPPLY_OID,
    CISCO_ENV_TEMP_OID,
    CISCO_MEMORY_LOW_OID,
    CISCO_OPTICAL_RX_POWER_OID,
    COLD_START_OID,
    FIBER_CUT_CLEAR_OID,
    FIBER_CUT_OID,
    ISIS_ADJACENCY_DOWN_OID,
    ISIS_ADJACENCY_UP_OID,
    LINK_DOWN_OID,
    LINK_UP_OID,
    OPTICAL_ALARM_CLEAR_OID,
    OPTICAL_ALARM_OID,
    WARM_START_OID,
)


@dataclass(frozen=True)
class Condition:
    code: str
    alarm_class: AlarmClass
    incident_type: IncidentType
    severity: str
    meaning: str
    # Conditions that are state, not a fault (e.g. laser shut down on
    # purpose because there's no input) stay alarms only.
    opens_incident: bool = True


# Condition codes as ONS 15454 / NCS1004 report them.
CONDITIONS: dict[str, Condition] = {
    c.code: c
    for c in (
        # --- transport: loss of light / framing on the optical path ---
        Condition("LOS-P", AlarmClass.TRANSPORT, IncidentType.OPTICAL_ALARM, "critical",
                  "Loss of signal - no payload light at this optical input"),
        Condition("LOS-O", AlarmClass.TRANSPORT, IncidentType.OPTICAL_ALARM, "critical",
                  "Loss of signal on the optical supervisory channel (OSC) from the neighbouring site"),
        Condition("LOS", AlarmClass.TRANSPORT, IncidentType.OPTICAL_ALARM, "critical",
                  "Loss of signal - no light received on this port"),
        Condition("OPWR-LFAIL", AlarmClass.TRANSPORT, IncidentType.OPTICAL_ALARM, "critical",
                  "Optical receive power below the failure threshold"),
        Condition("OPWR-LDEG", AlarmClass.TRANSPORT, IncidentType.OPTICAL_ALARM, "warning",
                  "Optical receive power below the degrade threshold"),
        Condition("LOF", AlarmClass.TRANSPORT, IncidentType.OPTICAL_ALARM, "critical",
                  "Loss of frame - light is received but the OTU frame can't be recovered"),
        Condition("OTU-AIS", AlarmClass.TRANSPORT, IncidentType.OPTICAL_ALARM, "warning",
                  "Alarm indication signal - a failure upstream of this point"),
        Condition("ODU-AIS", AlarmClass.TRANSPORT, IncidentType.OPTICAL_ALARM, "warning",
                  "ODU alarm indication signal - the client payload is replaced because of an upstream failure"),
        Condition("OTU-BDI", AlarmClass.TRANSPORT, IncidentType.OPTICAL_ALARM, "warning",
                  "Backward defect indication - the far end is not receiving this signal"),
        Condition("LF", AlarmClass.TRANSPORT, IncidentType.OPTICAL_ALARM, "warning",
                  "Ethernet local fault on a client port - no valid signal toward the router"),
        Condition("RF", AlarmClass.TRANSPORT, IncidentType.OPTICAL_ALARM, "warning",
                  "Ethernet remote fault on a client port - the router side reports it isn't receiving"),
        Condition("ALS", AlarmClass.TRANSPORT, IncidentType.OPTICAL_ALARM, "info",
                  "Automatic laser shutdown active (laser off because there is no input)", opens_incident=False),
        # --- equipment: hardware and environment on optical elements ---
        Condition("EQPT", AlarmClass.EQUIPMENT, IncidentType.ENV_ALARM, "critical", "Card/equipment failure"),
        Condition("MEA", AlarmClass.EQUIPMENT, IncidentType.ENV_ALARM, "warning",
                  "Equipment mismatch - the card in the slot isn't the provisioned type"),
        Condition("IMPROPRMVL", AlarmClass.EQUIPMENT, IncidentType.ENV_ALARM, "critical", "Card removed while in service"),
        Condition("FAN", AlarmClass.EQUIPMENT, IncidentType.FAN_FAILURE, "critical", "Fan tray failure"),
        Condition("PWR-FAIL", AlarmClass.EQUIPMENT, IncidentType.PSU_FAILURE, "critical", "Power feed failure"),
        Condition("HITEMP", AlarmClass.EQUIPMENT, IncidentType.ENV_ALARM, "critical", "Shelf temperature too high"),
        Condition("LASER-BIAS", AlarmClass.EQUIPMENT, IncidentType.ENV_ALARM, "warning",
                  "Laser bias current out of range - the transmitter is ageing"),
    )
}

# Built-in trap OIDs that already say what they are.
OID_CLASS: dict[str, AlarmClass] = {
    LINK_DOWN_OID: AlarmClass.LOGICAL,
    LINK_UP_OID: AlarmClass.LOGICAL,
    ISIS_ADJACENCY_DOWN_OID: AlarmClass.LOGICAL,
    ISIS_ADJACENCY_UP_OID: AlarmClass.LOGICAL,
    BFD_SESSION_DOWN_OID: AlarmClass.LOGICAL,
    BGP_ESTABLISHED_OID: AlarmClass.LOGICAL,
    BGP_BACKWARD_TRANSITION_OID: AlarmClass.LOGICAL,
    COLD_START_OID: AlarmClass.MANAGEMENT,
    WARM_START_OID: AlarmClass.MANAGEMENT,
    AUTH_FAILURE_OID: AlarmClass.MANAGEMENT,
    CISCO_CONFIG_MAN_OID: AlarmClass.MANAGEMENT,
    CISCO_CPU_RISING_OID: AlarmClass.EQUIPMENT,
    CISCO_MEMORY_LOW_OID: AlarmClass.EQUIPMENT,
    CISCO_ENV_TEMP_OID: AlarmClass.EQUIPMENT,
    CISCO_ENV_FAN_OID: AlarmClass.EQUIPMENT,
    CISCO_ENV_SUPPLY_OID: AlarmClass.EQUIPMENT,
    # entSensorThresholdNotification is a generic sensor (temperature,
    # voltage, transceiver power) threshold - hardware, not a path failure.
    CISCO_OPTICAL_RX_POWER_OID: AlarmClass.EQUIPMENT,
    FIBER_CUT_OID: AlarmClass.TRANSPORT,
    FIBER_CUT_CLEAR_OID: AlarmClass.TRANSPORT,
}

# Fallback for custom trap rules / uploaded MIBs: by the incident type the
# rule raises.
TYPE_CLASS: dict[IncidentType, AlarmClass] = {
    IncidentType.OPTICAL_ALARM: AlarmClass.TRANSPORT,
    IncidentType.LINK_DOWN: AlarmClass.LOGICAL,
    IncidentType.LINK_UP: AlarmClass.LOGICAL,
    IncidentType.LINK_FLAP: AlarmClass.LOGICAL,
    IncidentType.BGP_STATE_CHANGE: AlarmClass.LOGICAL,
    IncidentType.BFD_SESSION_DOWN: AlarmClass.LOGICAL,
    IncidentType.ISIS_NBR_DOWN: AlarmClass.LOGICAL,
    IncidentType.ISIS_NBR_UP: AlarmClass.LOGICAL,
    IncidentType.HIGH_CPU: AlarmClass.EQUIPMENT,
    IncidentType.HIGH_MEMORY: AlarmClass.EQUIPMENT,
    IncidentType.ENV_ALARM: AlarmClass.EQUIPMENT,
    IncidentType.FAN_FAILURE: AlarmClass.EQUIPMENT,
    IncidentType.PSU_FAILURE: AlarmClass.EQUIPMENT,
    IncidentType.COLD_START: AlarmClass.MANAGEMENT,
    IncidentType.WARM_START: AlarmClass.MANAGEMENT,
    IncidentType.AUTH_FAILURE: AlarmClass.MANAGEMENT,
    IncidentType.CONFIG_CHANGE: AlarmClass.MANAGEMENT,
    IncidentType.UNKNOWN: AlarmClass.MANAGEMENT,
}


def is_optical_alarm(trap_oid: str) -> bool:
    return trap_oid in (OPTICAL_ALARM_OID, OPTICAL_ALARM_CLEAR_OID)


def condition_for(code: str | None) -> Condition | None:
    return CONDITIONS.get((code or "").strip().upper())


def classify(trap_oid: str, incident_type: IncidentType, condition: Condition | None = None) -> AlarmClass:
    if condition is not None:
        return condition.alarm_class
    if trap_oid in OID_CLASS:
        return OID_CLASS[trap_oid]
    return TYPE_CLASS.get(incident_type, AlarmClass.MANAGEMENT)


def is_optical_element(router: Router) -> bool:
    return router.element_role not in (None, ElementRole.ROUTER)


def layer_for(alarm_class: AlarmClass, router: Router) -> AlarmLayer:
    """Transport alarms are L1 and logical ones L3 by definition; equipment
    and management alarms sit on whatever layer their element is."""
    if alarm_class == AlarmClass.TRANSPORT:
        return AlarmLayer.L1
    if alarm_class == AlarmClass.LOGICAL:
        return AlarmLayer.L3
    return AlarmLayer.L1 if is_optical_element(router) else AlarmLayer.L3


# --- the filter ------------------------------------------------------------

ALARM_FILTER = "alarm_filter"
DEFAULT_FILTERED = (AlarmClass.EQUIPMENT,)
FILTERABLE = (AlarmClass.EQUIPMENT, AlarmClass.MANAGEMENT)


def filtered_classes(db: Session) -> set[AlarmClass]:
    """Classes that don't open incidents. Read from the database each time
    (set through the backend API, acted on in the correlator process)."""
    row = db.get(AppSetting, ALARM_FILTER)
    if row is None:
        return set(DEFAULT_FILTERED)
    try:
        return {AlarmClass(v) for v in json.loads(row.value) if v in {c.value for c in FILTERABLE}}
    except (ValueError, TypeError):
        return set(DEFAULT_FILTERED)


def set_filtered_classes(db: Session, classes: list[AlarmClass]) -> None:
    row = db.get(AppSetting, ALARM_FILTER) or AppSetting(key=ALARM_FILTER)
    row.value = json.dumps(sorted(c.value for c in classes if c in FILTERABLE))
    db.add(row)
