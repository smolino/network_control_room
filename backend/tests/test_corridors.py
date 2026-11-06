"""Corridor model: path building (pure) and import idempotency (SQLite).
Run inside the backend image:

    docker run --rm -v "$PWD/backend:/app" -v "$PWD/seed:/seed" -w /app ncr-backend \\
        sh -c "pip install -q -r requirements-dev.txt && pytest -q"
"""

import copy
import json
import os

import pytest

from app.corridors import CorridorSpecError, build_plan
from app.schemas import CorridorSpec

SPEC_FILE = next(
    p
    for p in ("/seed/corridors/bio1-mad1.json", os.path.join(os.path.dirname(__file__), "../../seed/corridors/bio1-mad1.json"))
    if os.path.exists(p)
)


@pytest.fixture
def raw():
    with open(SPEC_FILE) as f:
        return json.load(f)


@pytest.fixture
def plan(raw):
    return build_plan(CorridorSpec.model_validate(raw))


def hops(plan, name):
    path = next(p for p in plan.paths if p.name == name)
    return [h[1] if h[0] == "span" else f"{h[1]} {h[2]}" for h in path.hops]


def test_chain_and_counts(plan):
    assert plan.chain[0] == "t-bio01-mad01-bio01" and plan.chain[-1] == "t-mad01-mad01-bio01"
    assert len(plan.chain) == 9  # 2 terminals + 7 ILAs
    assert plan.wavelengths == [1560.61, 1561.42]
    och = [p for p in plan.paths if p.kind == "och"]
    osc = [p for p in plan.paths if p.kind == "osc"]
    assert len(och) == 4  # 2 wavelengths x 2 directions
    assert len(osc) == 16  # 8 spans x 2 directions


def test_och_path_az_goes_through_slot_2_amps_in_order(plan):
    h = hops(plan, "OCH-1560.61-AZ")
    assert h[:5] == [
        "txp1.bio1 0/0/0/0",
        "t-bio01-mad01-bio01 4/ADD-1560.61",
        "t-bio01-mad01-bio01 4/COM-OUT",
        "t-bio01-mad01-bio01 2/COM-IN",
        "t-bio01-mad01-bio01 2/LINE-OUT",
    ]
    assert h[5:9] == [1, "a-0006-MAD01-BIO01 2/LINE-IN", "a-0006-MAD01-BIO01 2/LINE-OUT", 2]
    assert [x for x in h if isinstance(x, int)] == list(range(1, 9))
    assert h[-6:] == [
        8,
        "t-mad01-mad01-bio01 3/LINE-IN",
        "t-mad01-mad01-bio01 3/COM-OUT",
        "t-mad01-mad01-bio01 6/COM-IN",
        "t-mad01-mad01-bio01 6/DROP-1560.61",
        "txp1.mad1 0/0/0/0",
    ]


def test_och_path_za_goes_through_slot_7_amps_backwards(plan):
    h = hops(plan, "OCH-1561.42-ZA")
    assert h[0] == "txp1.mad1 0/0/0/1"
    assert h[1:5] == [
        "t-mad01-mad01-bio01 4/ADD-1561.42",
        "t-mad01-mad01-bio01 4/COM-OUT",
        "t-mad01-mad01-bio01 2/COM-IN",
        "t-mad01-mad01-bio01 2/LINE-OUT",
    ]
    assert h[5:8] == [8, "a-0001-MAD01-BIO01 7/LINE-IN", "a-0001-MAD01-BIO01 7/LINE-OUT"]
    assert [x for x in h if isinstance(x, int)] == list(range(8, 0, -1))
    assert h[-2:] == ["t-bio01-mad01-bio01 6/DROP-1561.42", "txp1.bio1 0/0/0/1"]


def test_osc_paths_use_the_cards_facing_the_span(plan):
    assert hops(plan, "OSC-S1-AZ") == ["t-bio01-mad01-bio01 1/OSC-OUT", 1, "a-0006-MAD01-BIO01 1/OSC-IN"]
    assert hops(plan, "OSC-S3-AZ") == ["a-0005-MAD01-BIO01 8/OSC-OUT", 3, "a-0007-MAD01-BIO01 1/OSC-IN"]
    assert hops(plan, "OSC-S3-ZA") == ["a-0007-MAD01-BIO01 1/OSC-OUT", 3, "a-0005-MAD01-BIO01 8/OSC-IN"]
    assert hops(plan, "OSC-S8-ZA") == ["t-mad01-mad01-bio01 1/OSC-OUT", 8, "a-0001-MAD01-BIO01 8/OSC-IN"]


def test_card_ports_are_derived(plan):
    names = {p.name for p in plan.ports["t-bio01-mad01-bio01"]}
    assert {"4/ADD-1560.61", "4/ADD-1561.42", "6/DROP-1560.61", "1/OSC-IN", "8/OSC-OUT"} <= names
    ila = {p.name: p.direction for p in plan.ports["a-0004-MAD01-BIO01"]}
    assert ila["2/LINE-IN"] == "AZ" and ila["7/LINE-IN"] == "ZA"


def test_broken_span_chain_is_rejected(raw):
    raw["spans"][3]["from_device"] = "a-0006-MAD01-BIO01"
    with pytest.raises(CorridorSpecError, match="span 4 starts at"):
        build_plan(CorridorSpec.model_validate(raw))


def test_ila_without_an_amp_for_each_direction_is_rejected(raw):
    ila = next(d for d in raw["devices"] if d["hostname"] == "a-0004-MAD01-BIO01")
    ila["cards"] = [c for c in ila["cards"] if c["slot"] != 7]
    with pytest.raises(CorridorSpecError, match="a-0004-MAD01-BIO01: expected exactly one line_amp card for direction ZA"):
        build_plan(CorridorSpec.model_validate(raw))


def test_circuit_on_a_wavelength_not_carried_is_rejected(raw):
    raw["circuits"][0]["wavelength_nm"] = 1530.33
    with pytest.raises(CorridorSpecError, match="isn't carried end to end"):
        build_plan(CorridorSpec.model_validate(raw))


def test_import_is_idempotent(raw):
    from app.db import Base, SessionLocal, engine
    from app.corridors import import_corridor
    from app.models import BgpPeering, ClientCircuit, Interface, InterfaceBundle, OpticalPath, PathHop, Port, Router

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    spec = CorridorSpec.model_validate(raw)

    def counts(db):
        return {m.__name__: db.query(m).count() for m in (Router, Port, OpticalPath, PathHop, ClientCircuit, BgpPeering, InterfaceBundle, Interface)}

    with SessionLocal() as db:
        first, _ = import_corridor(db, spec)
        db.commit()
        after_first = counts(db)
        second, _ = import_corridor(db, copy.deepcopy(spec))
        db.commit()
        assert counts(db) == after_first
        assert len(first.devices_created) == 15 and second.devices_created == []
        assert after_first["BgpPeering"] == 2 and after_first["ClientCircuit"] == 6
        members = sorted(i.name for i in db.query(Interface).join(InterfaceBundle).filter(InterfaceBundle.name == "Bundle-Ether1"))
        assert len(members) == 12 and "HundredGigE0/7/0/2" in members
        terminal = db.query(Router).filter(Router.hostname == "t-bio01-mad01-bio01").one()
        assert terminal.element_role.value == "terminal" and terminal.site.code == "BIO01"


def test_export_round_trips(raw):
    """Backup = export_spec; restore = import it into an empty fleet. The
    restored corridor must be the same corridor."""
    from app.corridors import export_spec, import_corridor
    from app.db import Base, SessionLocal, engine
    from app.models import ClientCircuit, Corridor, Interface, OpticalPath, PathHop, Port

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    original = CorridorSpec.model_validate(raw)
    with SessionLocal() as db:
        import_corridor(db, original)
        db.commit()
        exported = export_spec(db, db.query(Corridor).one())

    def norm(spec):
        d = spec.model_dump()
        d["devices"] = sorted(
            ({**x, "ports": sorted(x["ports"], key=lambda p: p["name"]), "cards": sorted(x["cards"], key=lambda c: c["slot"])} for x in d["devices"]),
            key=lambda x: x["hostname"],
        )
        d["circuits"] = sorted(d["circuits"], key=lambda c: (c["a_router"], c["a_router_port"]))
        d["spans"] = [{**s, "length_km": None} for s in d["spans"]]  # computed on import when absent
        return d

    assert norm(exported) == norm(original)
    a, b = build_plan(original), build_plan(exported)
    assert a.chain == b.chain and [(p.name, p.hops) for p in a.paths] == [(p.name, p.hops) for p in b.paths]

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        result, _ = import_corridor(db, exported)
        db.commit()
        assert len(result.devices_created) == 15 and result.optical_paths == 20 and result.client_circuits == 6
        assert db.query(PathHop).count() > 0 and db.query(Interface).count() == 12


def test_router_upload_keeps_element_role():
    from app.schemas import RouterIn

    r = RouterIn.model_validate({"hostname": "a-1", "mgmt_ip": "10.0.0.9", "latitude": 1, "longitude": 2, "element_role": "ila"})
    assert r.element_role.value == "ila"
    assert RouterIn.model_validate({"hostname": "r", "mgmt_ip": "10.0.0.8", "latitude": 1, "longitude": 2}).element_role is None
