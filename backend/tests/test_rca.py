"""Root-cause engine, end to end: every fault scenario the simulator can
produce (simulator/optical_faults.py) is replayed as traps through the real
classifier into SQLite, the engine diagnoses the corridor, and the result is
checked against the scenario's ground truth - right diagnosis, one root
incident, every other incident linked to it, and all resolved on repair.

Needs the simulator sources and the seed spec mounted:
    docker run --rm -v "$PWD/backend:/app" -v "$PWD/seed:/seed" -v "$PWD/simulator:/sim" -w /app <backend image> \\
        sh -c "pip install -q -r requirements-dev.txt && pytest -q"
"""

import json
import os
import random
import sys
from datetime import datetime, timezone

import pytest

SIM = next((p for p in ("/sim", os.path.join(os.path.dirname(__file__), "../../simulator")) if os.path.exists(os.path.join(p, "optical_faults.py"))), None)
pytestmark = pytest.mark.skipif(SIM is None, reason="simulator sources not mounted")
if SIM:
    sys.path.insert(0, SIM)

from test_corridors import SPEC_FILE  # noqa: E402


@pytest.fixture
def world(monkeypatch):
    from app import topology_graph
    from app.corridors import corridor_out, import_corridor
    from app.db import Base, SessionLocal, engine
    from app.models import BgpPeering, Corridor, InterfaceBundle, Router
    from app.schemas import CorridorSpec

    monkeypatch.setattr(topology_graph, "peering_ids_for_interface", lambda *a: [])
    monkeypatch.setattr(topology_graph, "peering_ids_for_router", lambda *a: [])
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    with open(SPEC_FILE) as f:
        import_corridor(db, CorridorSpec.model_validate(json.load(f)))
    db.commit()
    corridor = db.query(Corridor).one()
    detail = json.loads(corridor_out(db, corridor, detail=True).model_dump_json())
    devices = {r.id: {"hostname": r.hostname, "mgmt_ip": r.mgmt_ip, "element_role": r.element_role.value if r.element_role else None}
               for r in db.query(Router)}
    bundles = {}
    for b in db.query(InterfaceBundle):
        p = db.get(BgpPeering, b.peering_id)
        peer = p.router_b if p.router_a_id == b.router_id else p.router_a
        bundles.setdefault(b.router.mgmt_ip, {})[peer.mgmt_ip] = {"bundle": b.name, "members": [m.name for m in b.members]}
    import optical_faults

    yield db, corridor, optical_faults.Topology(detail, devices, bundles)
    db.close()


def play(db, events):
    from app.snmp.classifier import classify_and_store
    from app.snmp.oid_map import BGP_PEER_OID, CONDITION_OID, IF_NAME_OID, ROUTER_ID_OID

    for e in events:
        varbinds = [(ROUTER_ID_OID, e.ip)]
        if e.interface:
            varbinds.append((IF_NAME_OID, e.interface))
        if e.condition:
            varbinds.append((CONDITION_OID, e.condition))
        if e.bgp_peer_ip:
            varbinds.append((BGP_PEER_OID, e.bgp_peer_ip))
        classify_and_store(db, source_ip=e.ip, trap_oid=e.oid, varbinds=varbinds)


def run(db, corridor, scenario):
    from app import rca
    from app.models import Incident, IncidentStatus

    play(db, scenario.raise_events)
    diagnoses = rca.evaluate(db, corridor, datetime.now(timezone.utc), force=True)
    db.commit()
    open_ = db.query(Incident).filter(Incident.status == IncidentStatus.OPEN).all()
    roots = [i for i in open_ if i.corridor_id is not None]
    unlinked = [i for i in open_ if i.corridor_id is None and i.root_cause_incident_id is None]
    return diagnoses, roots, unlinked


def repair(db, corridor, scenario):
    from app import rca
    from app.models import Incident, IncidentStatus

    play(db, scenario.clear_events)
    rca.evaluate(db, corridor, datetime.now(timezone.utc), force=True)
    db.commit()
    return db.query(Incident).filter(Incident.status == IncidentStatus.OPEN).all()


def describe(i):
    return f"#{i.id} {i.router.hostname} {i.incident_type.value} {i.condition} {i.interface_name}"


CASES = (
    [("cable_cut", {"seq": s}, "CABLE-CUT", f"span:{s}") for s in range(1, 9)]
    + [("fiber_cut", {"seq": s, "direction": d}, "FIBER-CUT", f"span:{s}") for s, d in ((1, "ZA"), (3, "AZ"), (6, "ZA"), (8, "AZ"))]
)


@pytest.mark.parametrize("kind,args,expect,key", CASES)
def test_span_faults_are_located_on_the_right_span(world, kind, args, expect, key):
    import optical_faults as of

    db, corridor, topo = world
    random.seed(hash(key) % 1000)
    scenario = of.cable_cut(topo, args["seq"]) if kind == "cable_cut" else of.fiber_cut(topo, args["seq"], args["direction"])
    diagnoses, roots, unlinked = run(db, corridor, scenario)
    assert [(d.kind, d.key) for d in diagnoses] == [(expect, key)], scenario.summary
    assert len(roots) == 1 and roots[0].condition == expect and roots[0].fault_segment_index == args["seq"]
    assert not unlinked, [describe(i) for i in unlinked]
    assert len(roots[0].symptomatic_incidents) > 10
    assert not repair(db, corridor, scenario), "everything resolves on repair"


@pytest.mark.parametrize("ila", ["a-0006-MAD01-BIO01", "a-0007-MAD01-BIO01", "a-0001-MAD01-BIO01"])
@pytest.mark.parametrize("direction", ["AZ", "ZA"])
def test_amp_failure_is_told_apart_from_a_cut(world, ila, direction):
    import optical_faults as of
    from app.models import Router

    db, corridor, topo = world
    scenario = of.amp_failure(topo, ila, direction)
    diagnoses, roots, unlinked = run(db, corridor, scenario)
    amp_id = db.query(Router).filter(Router.hostname == ila).one().id
    assert [(d.kind, d.key) for d in diagnoses] == [("AMP-FAIL", f"amp:{amp_id}:{direction}")], scenario.summary
    assert roots[0].router_id == amp_id
    assert not unlinked, [describe(i) for i in unlinked]
    assert not repair(db, corridor, scenario)


@pytest.mark.parametrize("path", ["OCH-1560.61-AZ", "OCH-1561.42-ZA"])
def test_trunk_failure(world, path):
    import optical_faults as of

    db, corridor, topo = world
    scenario = of.trunk_failure(topo, path)
    diagnoses, roots, unlinked = run(db, corridor, scenario)
    assert [d.kind for d in diagnoses] == ["TRUNK-FAIL"], scenario.summary
    assert len(diagnoses[0].circuit_ids) == 3 and diagnoses[0].peering_ids  # one wavelength = one router pair
    assert not unlinked, [describe(i) for i in unlinked]
    assert not repair(db, corridor, scenario)


@pytest.mark.parametrize("circuit,side", [(1, "A"), (5, "Z")])
def test_patch_cord(world, circuit, side):
    import optical_faults as of
    from app.models import ClientCircuit

    db, corridor, topo = world
    cid = db.query(ClientCircuit).order_by(ClientCircuit.id).all()[circuit - 1].id
    scenario = of.patch_cord(topo, cid, side)
    diagnoses, roots, unlinked = run(db, corridor, scenario)
    assert [(d.kind, d.key) for d in diagnoses] == [("PATCH-CORD", f"patch:{cid}:{side}")], scenario.summary
    assert diagnoses[0].circuit_ids == {cid} and not diagnoses[0].peering_ids  # 2 of 3 bundle members survive
    assert not unlinked, [describe(i) for i in unlinked]
    assert not repair(db, corridor, scenario)


def test_noise_elsewhere_is_not_swallowed_by_a_cut(world):
    """A power degrade upstream of the cut is a separate (minor) problem -
    it must not be linked to the cut as a symptom."""
    import optical_faults as of
    from app.snmp.oid_map import OPTICAL_ALARM_OID

    db, corridor, topo = world
    noise = of.Event(0, OPTICAL_ALARM_OID, topo.ip("a-0006-MAD01-BIO01"), "2/LINE-IN", "OPWR-LDEG")
    play(db, [noise])
    diagnoses, roots, unlinked = run(db, corridor, of.cable_cut(topo, 5))
    assert [d.kind for d in diagnoses] == ["CABLE-CUT"]
    assert [(i.condition, i.interface_name) for i in unlinked] == [("OPWR-LDEG", "2/LINE-IN")]


def test_partial_alarms_are_refined_in_place(world):
    """Only one direction's alarms have arrived: a fiber cut. Then the other
    direction's arrive: the same root becomes a cable cut."""
    import optical_faults as of
    from app import rca
    from app.models import Incident

    db, corridor, topo = world
    full = of.cable_cut(topo, 4)
    az = of.fiber_cut(topo, 4, "AZ")
    play(db, [e for e in az.raise_events])
    rca.evaluate(db, corridor, datetime.now(timezone.utc), force=True)
    db.commit()
    root = db.query(Incident).filter(Incident.corridor_id.isnot(None)).one()
    assert root.condition == "FIBER-CUT"
    play(db, full.raise_events)
    rca.evaluate(db, corridor, datetime.now(timezone.utc), force=True)
    db.commit()
    db.refresh(root)
    assert root.condition == "CABLE-CUT" and db.query(Incident).filter(Incident.corridor_id.isnot(None)).count() == 1


def test_root_description_fits_and_analysis_names_provider_and_sites(world):
    import optical_faults as of
    from app.remediation.guidance import generate_analysis

    db, corridor, topo = world
    _, roots, _ = run(db, corridor, of.cable_cut(topo, 4))
    root = roots[0]
    assert len(root.description) <= 255 and root.description.endswith("2 L3 link(s) down")
    analysis = generate_analysis(root, root.router)
    assert "Fiberprov" in analysis["suggested_solution"] and "OTDR" in analysis["suggested_solution"]
    assert "a-0007-MAD01-BIO01" in analysis["suggested_solution"] and "a-0004-MAD01-BIO01" in analysis["suggested_solution"]
