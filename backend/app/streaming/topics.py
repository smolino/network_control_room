"""Kafka topic names shared across the alarm pipeline - see the module
docstrings in app.streaming.normalizer/correlator and app.snmp.trap_listener/
app.fiber_faults for what flows through each one.
"""

RAW_ALARMS = "raw-alarms"
NORM_ALARMS = "norm-alarms"
INCIDENT_EVENTS = "incident-events"
