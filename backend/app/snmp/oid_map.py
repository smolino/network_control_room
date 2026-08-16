"""Known trap OIDs -> (trap_name, incident_type, severity).

OIDs cover the standard SNMPv2-MIB traps plus a representative set of
Cisco enterprise traps (CISCO-PROCESS-MIB, CISCO-ENVMON-MIB,
CISCO-CONFIG-MAN-MIB, BGP4-MIB). Good enough for classification/demo
purposes without requiring a full MIB compiler.
"""

from app.models import IncidentType

# Standard SNMPv2-MIB traps
LINK_DOWN_OID = "1.3.6.1.6.3.1.1.5.3"
LINK_UP_OID = "1.3.6.1.6.3.1.1.5.4"
COLD_START_OID = "1.3.6.1.6.3.1.1.5.1"
WARM_START_OID = "1.3.6.1.6.3.1.1.5.2"
AUTH_FAILURE_OID = "1.3.6.1.6.3.1.1.5.5"

# BGP4-MIB
BGP_ESTABLISHED_OID = "1.3.6.1.2.1.15.7.1"
BGP_BACKWARD_TRANSITION_OID = "1.3.6.1.2.1.15.7.2"

# BFD-STANDARD-MIB / ISIS-MIB - IGP/fast-failure-detection traps, alongside
# the BGP4-MIB traps above, on the same backbone mesh. IS-IS is the IGP that
# manages physical adjacency fleet-wide; unlike the other traps here, this
# one is per-bundle-*member* (see app.bundles) - a down/up pair rather than
# ISIS-MIB's single isisAdjacencyChange notification, so state-change
# direction is unambiguous without a full MIB compiler (same simplification
# already used for BGP established vs. backward-transition above).
BFD_SESSION_DOWN_OID = "1.3.6.1.2.1.10.246.0.2"
ISIS_ADJACENCY_DOWN_OID = "1.3.6.1.2.1.138.0.3"
ISIS_ADJACENCY_UP_OID = "1.3.6.1.2.1.138.0.4"

# Cisco enterprise traps (representative OIDs under Cisco's 1.3.6.1.4.1.9 arc)
CISCO_CPU_RISING_OID = "1.3.6.1.4.1.9.9.109.2.0.1"
CISCO_ENV_TEMP_OID = "1.3.6.1.4.1.9.9.13.3.0.3"
CISCO_ENV_FAN_OID = "1.3.6.1.4.1.9.9.13.3.0.4"
CISCO_ENV_SUPPLY_OID = "1.3.6.1.4.1.9.9.13.3.0.5"
CISCO_CONFIG_MAN_OID = "1.3.6.1.4.1.9.9.43.2.0.1"
CISCO_MEMORY_LOW_OID = "1.3.6.1.4.1.9.9.48.2.0.1"
CISCO_OPTICAL_RX_POWER_OID = "1.3.6.1.4.1.9.9.91.2.0.1"

# Private OIDs used only by our own trap simulator to identify which
# simulated router sent a trap (real devices are identified by UDP source
# IP instead; the simulator runs from a single container IP so it embeds
# the router's mgmt IP and the affected interface as varbinds).
ROUTER_ID_OID = "1.3.6.1.4.1.9.9.9999.1.1"
IF_NAME_OID = "1.3.6.1.4.1.9.9.9999.1.2"
BGP_PEER_OID = "1.3.6.1.4.1.9.9.9999.1.3"  # mgmt IP of the BGP neighbor this trap is about

TRAP_OID_MAP: dict[str, tuple[str, IncidentType, str]] = {
    LINK_DOWN_OID: ("linkDown", IncidentType.LINK_DOWN, "critical"),
    LINK_UP_OID: ("linkUp", IncidentType.LINK_UP, "info"),
    COLD_START_OID: ("coldStart", IncidentType.COLD_START, "warning"),
    WARM_START_OID: ("warmStart", IncidentType.WARM_START, "info"),
    AUTH_FAILURE_OID: ("authenticationFailure", IncidentType.AUTH_FAILURE, "warning"),
    BGP_ESTABLISHED_OID: ("bgpEstablished", IncidentType.BGP_STATE_CHANGE, "info"),
    BGP_BACKWARD_TRANSITION_OID: ("bgpBackwardTransition", IncidentType.BGP_STATE_CHANGE, "warning"),
    BFD_SESSION_DOWN_OID: ("bfdSessDown", IncidentType.BFD_SESSION_DOWN, "critical"),
    ISIS_ADJACENCY_DOWN_OID: ("isisAdjacencyDown", IncidentType.ISIS_NBR_DOWN, "warning"),
    ISIS_ADJACENCY_UP_OID: ("isisAdjacencyUp", IncidentType.ISIS_NBR_UP, "info"),
    CISCO_CPU_RISING_OID: ("cpmCPURisingThreshold", IncidentType.HIGH_CPU, "warning"),
    CISCO_ENV_TEMP_OID: ("ciscoEnvMonTemperatureNotification", IncidentType.ENV_ALARM, "critical"),
    CISCO_ENV_FAN_OID: ("ciscoEnvMonFanNotification", IncidentType.FAN_FAILURE, "critical"),
    CISCO_ENV_SUPPLY_OID: ("ciscoEnvMonRedundantSupplyNotification", IncidentType.PSU_FAILURE, "critical"),
    CISCO_CONFIG_MAN_OID: ("ciscoConfigManEvent", IncidentType.CONFIG_CHANGE, "info"),
    CISCO_MEMORY_LOW_OID: ("ciscoMemoryPoolLowMemory", IncidentType.HIGH_MEMORY, "warning"),
    CISCO_OPTICAL_RX_POWER_OID: ("entSensorThresholdNotification", IncidentType.OPTICAL_ALARM, "warning"),
}

UNKNOWN_TRAP = ("unknownTrap", IncidentType.UNKNOWN, "info")
