"""Alarm classes, the equipment filter and optical-element conditions.
Runs the real classifier against SQLite (see test_corridors.py for how)."""

import json

import pytest

from app.models import AlarmClass, AlarmLayer, ElementRole, IncidentType
from app.snmp import alarm_classes
from app.snmp.oid_map import (
    CISCO_ENV_FAN_OID,
    CISCO_OPTICAL_RX_POWER_OID,
    CONDITION_OID,
    FIBER_CUT_OID,
    IF_NAME_OID,
    ISIS_ADJACENCY_DOWN_OID,
    LINK_DOWN_OID,
    OPTICAL_ALARM_CLEAR_OID,
    OPTICAL_ALARM_OID,
    ROUTER_ID_OID,
)


def test_builtin_traps_are_classified():
    c = alarm_classes.classify
    assert c(LINK_DOWN_OID, IncidentType.LINK_DOWN) == AlarmClass.LOGICAL
    assert c(ISIS_ADJACENCY_DOWN_OID, IncidentType.ISIS_NBR_DOWN) == AlarmClass.LOGICAL
    assert c(CISCO_ENV_FAN_OID, IncidentType.FAN_FAILURE) == AlarmClass.EQUIPMENT
    assert c(CISCO_OPTICAL_RX_POWER_OID, IncidentType.ENV_ALARM) == AlarmClass.EQUIPMENT
    assert c(FIBER_CUT_OID, IncidentType.OPTICAL_ALARM) == AlarmClass.TRANSPORT
    # A custom rule falls back to the type it raises.
    assert c("1.3.6.1.4.1.99999.1", IncidentType.PSU_FAILURE) == AlarmClass.EQUIPMENT


def test_conditions():
    los = alarm_classes.condition_for("los-p")
    assert los.alarm_class == AlarmClass.TRANSPORT and los.incident_type == IncidentType.OPTICAL_ALARM
    assert alarm_classes.condition_for("EQPT").alarm_class == AlarmClass.EQUIPMENT
    assert alarm_classes.condition_for("ALS").opens_incident is False
    assert alarm_classes.condition_for("NOPE") is None


@pytest.fixture
def db():
    from app.db import Base, SessionLocal, engine
    from app.models import Router, RouterStatus

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        session.add_all([
            Router(hostname="ila-1", mgmt_ip="10.0.0.1", latitude=0, longitude=0, status=RouterStatus.UP, element_role=ElementRole.ILA),
            Router(hostname="rtr-1", mgmt_ip="10.0.0.2", latitude=0, longitude=0, status=RouterStatus.UP),
        ])
        session.commit()
        yield session


def trap(db, ip, oid, port=None, condition=None):
    from app.snmp.classifier import classify_and_store

    varbinds = [(ROUTER_ID_OID, ip)]
    if port:
        varbinds.append((IF_NAME_OID, port))
    if condition:
        varbinds.append((CONDITION_OID, condition))
    return classify_and_store(db, source_ip=ip, trap_oid=oid, varbinds=varbinds)


def test_default_filter_is_equipment(db):
    assert alarm_classes.filtered_classes(db) == {AlarmClass.EQUIPMENT}


def test_equipment_alarm_is_kept_but_opens_no_incident(db):
    from app.models import Incident, TrapEvent

    event = trap(db, "10.0.0.2", CISCO_ENV_FAN_OID)
    assert event["trap"]["alarm_class"] == "equipment" and event["trap"]["filtered"] is True
    assert db.query(Incident).count() == 0
    stored = db.query(TrapEvent).one()
    assert stored.alarm_class == AlarmClass.EQUIPMENT and stored.filtered


def test_equipment_alarm_opens_an_incident_when_not_filtered(db):
    from app.models import Incident

    alarm_classes.set_filtered_classes(db, [])
    db.commit()
    trap(db, "10.0.0.1", OPTICAL_ALARM_OID, port="2", condition="EQPT")
    incident = db.query(Incident).one()
    # Hardware on an optical element: equipment class, L1 layer.
    assert incident.alarm_class == AlarmClass.EQUIPMENT and incident.layer == AlarmLayer.L1
    assert incident.incident_type == IncidentType.ENV_ALARM and incident.condition == "EQPT"


def test_optical_los_raises_and_clears_by_condition(db):
    from app.models import Incident, IncidentStatus

    trap(db, "10.0.0.1", OPTICAL_ALARM_OID, port="2/LINE-IN", condition="LOS-P")
    trap(db, "10.0.0.1", OPTICAL_ALARM_OID, port="2/LINE-IN", condition="OPWR-LFAIL")
    trap(db, "10.0.0.1", OPTICAL_ALARM_OID, port="2/LINE-IN", condition="LOS-P")  # repeat: bumps
    los, opwr = sorted(db.query(Incident).all(), key=lambda i: i.condition)
    assert (los.condition, opwr.condition) == ("LOS-P", "OPWR-LFAIL")
    assert los.trap_count == 2 and los.alarm_class == AlarmClass.TRANSPORT and los.layer == AlarmLayer.L1
    assert los.severity == "critical" and "Loss of signal" in los.description

    trap(db, "10.0.0.1", OPTICAL_ALARM_CLEAR_OID, port="2/LINE-IN", condition="LOS-P")
    db.refresh(los)
    db.refresh(opwr)
    assert los.status == IncidentStatus.RESOLVED and opwr.status == IncidentStatus.OPEN


def test_state_conditions_and_unknown_codes_stay_alarms(db):
    from app.models import Incident

    trap(db, "10.0.0.1", OPTICAL_ALARM_OID, port="2/LINE-OUT", condition="ALS")
    trap(db, "10.0.0.1", OPTICAL_ALARM_OID, port="2/LINE-OUT", condition="WHATEVER")
    assert db.query(Incident).count() == 0


def test_filter_setting_rejects_unfilterable_classes(db):
    alarm_classes.set_filtered_classes(db, [AlarmClass.TRANSPORT, AlarmClass.MANAGEMENT])
    db.commit()
    assert alarm_classes.filtered_classes(db) == {AlarmClass.MANAGEMENT}
    from app.models import AppSetting

    assert json.loads(db.get(AppSetting, alarm_classes.ALARM_FILTER).value) == ["management"]


def test_optical_element_gets_no_config_backup(db):
    from app.models import Incident, RouterConfigBackup
    from app.remediation.engine import maybe_remediate

    trap(db, "10.0.0.1", OPTICAL_ALARM_OID, port="2/LINE-IN", condition="LOS-P")
    incident = db.query(Incident).one()
    action = maybe_remediate(db, incident.router, incident)
    assert action["action_type"] == "NOTIFY_ONLY" and action["backup_id"] is None
    assert db.query(RouterConfigBackup).count() == 0


def test_bundle_member_link_down_does_not_take_the_router_down(db, monkeypatch):
    from app import topology_graph
    from app.models import BgpPeering, Interface, InterfaceBundle, Router, RouterStatus

    # No Neo4j here: the L3 incident's root-cause lookup finds nothing.
    monkeypatch.setattr(topology_graph, "peering_ids_for_interface", lambda *a: [])
    monkeypatch.setattr(topology_graph, "peering_ids_for_router", lambda *a: [])

    rtr = db.query(Router).filter(Router.hostname == "rtr-1").one()
    other = db.query(Router).filter(Router.hostname == "ila-1").one()
    peering = BgpPeering(router_a_id=rtr.id, router_b_id=other.id)
    db.add(peering)
    db.flush()
    bundle = InterfaceBundle(router_id=rtr.id, peering_id=peering.id, name="Bundle-Ether1")
    db.add(bundle)
    db.flush()
    db.add(Interface(router_id=rtr.id, bundle_id=bundle.id, name="HundredGigE0/7/0/2", bandwidth_mbps=100000))
    db.commit()

    trap(db, "10.0.0.2", LINK_DOWN_OID, port="HundredGigE0/7/0/2")
    db.refresh(rtr)
    assert rtr.status == RouterStatus.UP
    trap(db, "10.0.0.2", LINK_DOWN_OID, port="GigabitEthernet0/1")  # an unprotected port still does
    db.refresh(rtr)
    assert rtr.status == RouterStatus.DOWN


def test_optical_rate_is_its_own_setting(db):
    from app.api.simulation import SimulationRates, get_status, set_rates

    assert get_status(db).optical_rate == 1.0
    out = set_rates(SimulationRates(optical_rate=2.5), db)
    assert out.optical_rate == 2.5 and out.l1_rate == 1.0 and out.l3_rate == 1.0
    assert out.optical_base_per_hour == 3600 / 450  # 300-600 s between faults by default
    assert out.optical_corridors == 0
    with pytest.raises(Exception):
        SimulationRates(optical_rate=9)
