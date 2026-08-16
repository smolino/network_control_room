"""Trap OID constants, mirrored from backend/app/snmp/oid_map.py so the
simulator can run as a fully standalone service/container without
importing the backend package."""

LINK_DOWN_OID = "1.3.6.1.6.3.1.1.5.3"
LINK_UP_OID = "1.3.6.1.6.3.1.1.5.4"
COLD_START_OID = "1.3.6.1.6.3.1.1.5.1"
WARM_START_OID = "1.3.6.1.6.3.1.1.5.2"
AUTH_FAILURE_OID = "1.3.6.1.6.3.1.1.5.5"

BGP_ESTABLISHED_OID = "1.3.6.1.2.1.15.7.1"
BGP_BACKWARD_TRANSITION_OID = "1.3.6.1.2.1.15.7.2"

CISCO_CPU_RISING_OID = "1.3.6.1.4.1.9.9.109.2.0.1"
CISCO_ENV_TEMP_OID = "1.3.6.1.4.1.9.9.13.3.0.3"
CISCO_CONFIG_MAN_OID = "1.3.6.1.4.1.9.9.43.2.0.1"
CISCO_ENV_FAN_OID = "1.3.6.1.4.1.9.9.13.3.0.4"
CISCO_ENV_SUPPLY_OID = "1.3.6.1.4.1.9.9.13.3.0.5"
CISCO_MEMORY_LOW_OID = "1.3.6.1.4.1.9.9.48.2.0.1"
CISCO_OPTICAL_RX_POWER_OID = "1.3.6.1.4.1.9.9.91.2.0.1"

# BFD-STANDARD-MIB / ISIS-MIB. IS-IS is the IGP that manages physical
# adjacency fleet-wide; this is per-bundle-member (see build_bgp_bundles) -
# a down/up pair rather than ISIS-MIB's single isisAdjacencyChange
# notification, so state-change direction is unambiguous.
BFD_SESSION_DOWN_OID = "1.3.6.1.2.1.10.246.0.2"
ISIS_ADJACENCY_DOWN_OID = "1.3.6.1.2.1.138.0.3"
ISIS_ADJACENCY_UP_OID = "1.3.6.1.2.1.138.0.4"

ROUTER_ID_OID = "1.3.6.1.4.1.9.9.9999.1.1"
IF_NAME_OID = "1.3.6.1.4.1.9.9.9999.1.2"
BGP_PEER_OID = "1.3.6.1.4.1.9.9.9999.1.3"

INTERFACES = [
    "GigabitEthernet0/0",
    "GigabitEthernet0/1",
    "FortyGigE0/0/0",
    "FortyGigE0/0/1",
    "Loopback0",
]
