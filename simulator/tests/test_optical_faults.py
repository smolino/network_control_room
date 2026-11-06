"""Alarm cascades for each fault scenario, on a small synthetic corridor
shaped like GET /api/corridors/{id}:

    tA ==S1== i1 ==S2== i2 ==S3== tZ        (2 wavelengths, 2 directions)
    rA1 -(2 circuits, wl1)- rZ1             rA2 -(1 circuit, wl2)- rZ2

Run: docker run --rm -v "$PWD/simulator:/app" -w /app <simulator image> \\
         sh -c "pip install -q pytest && pytest -q"
"""

import random

import pytest

from oids import BGP_BACKWARD_TRANSITION_OID, ISIS_ADJACENCY_DOWN_OID, LINK_DOWN_OID, OPTICAL_ALARM_OID
import optical_faults as of

WL1, WL2 = 1560.61, 1561.42
LINE = ["tA", "i1", "i2", "tZ"]


def _och(wl, direction):
    line = LINE if direction == "AZ" else LINE[::-1]
    spans = [1, 2, 3] if direction == "AZ" else [3, 2, 1]
    src, dst = (("txpA", "txpZ") if direction == "AZ" else ("txpZ", "txpA"))
    trunk = "T1" if wl == WL1 else "T2"
    amp = "2" if direction == "AZ" else "7"
    hops = [
        (src, trunk, "trunk"),
        (line[0], f"4/ADD-{wl:.2f}", "add"), (line[0], "4/COM-OUT", "com_out"),
        (line[0], "2/COM-IN", "com_in"), (line[0], "2/LINE-OUT", "line_out"),
    ]
    for i, seq in enumerate(spans):
        hops.append(seq)
        nxt = line[i + 1]
        if nxt == line[-1]:
            hops += [(nxt, "3/LINE-IN", "line_in"), (nxt, "3/COM-OUT", "com_out"), (nxt, "6/COM-IN", "com_in"),
                     (nxt, f"6/DROP-{wl:.2f}", "drop"), (dst, trunk, "trunk")]
        else:
            hops += [(nxt, f"{amp}/LINE-IN", "line_in"), (nxt, f"{amp}/LINE-OUT", "line_out")]
    return {"name": f"OCH-{wl:.2f}-{direction}", "kind": "och", "direction": direction, "wavelength_nm": wl, "hops": [
        {"span_seq": h} if isinstance(h, int) else {"hostname": h[0], "port": h[1], "port_kind": h[2]} for h in hops]}


def _osc(seq, direction):
    a, z = LINE[seq - 1], LINE[seq]
    up, down = (a, z) if direction == "AZ" else (z, a)
    return {"name": f"OSC-S{seq}-{direction}", "kind": "osc", "direction": direction, "wavelength_nm": None, "hops": [
        {"hostname": up, "port": "8/OSC-OUT", "port_kind": "osc_out"}, {"span_seq": seq},
        {"hostname": down, "port": "1/OSC-IN", "port_kind": "osc_in"}]}


CORRIDOR = {
    "name": "TEST",
    "spans": [{"seq": s, "from_hostname": LINE[s - 1], "to_hostname": LINE[s], "provider": "P"} for s in (1, 2, 3)],
    "paths": [_och(wl, d) for d in ("AZ", "ZA") for wl in (WL1, WL2)] + [_osc(s, d) for d in ("AZ", "ZA") for s in (1, 2, 3)],
    "circuits": [
        {"id": 1, "wavelength_nm": WL1, "a_router": "rA1", "a_router_port": "Hu0/1", "a_client": "txpA C1",
         "z_client": "txpZ C1", "z_router": "rZ1", "z_router_port": "Hu0/1"},
        {"id": 2, "wavelength_nm": WL1, "a_router": "rA1", "a_router_port": "Hu0/2", "a_client": "txpA C2",
         "z_client": "txpZ C2", "z_router": "rZ1", "z_router_port": "Hu0/2"},
        {"id": 3, "wavelength_nm": WL2, "a_router": "rA2", "a_router_port": "Hu0/1", "a_client": "txpA C3",
         "z_client": "txpZ C3", "z_router": "rZ2", "z_router_port": "Hu0/1"},
    ],
}
NAMES = ["tA", "i1", "i2", "tZ", "txpA", "txpZ", "rA1", "rZ1", "rA2", "rZ2"]
DEVICES = {i: {"hostname": h, "mgmt_ip": f"10.0.0.{i}", "element_role": "router" if h.startswith("r") else "ila"}
           for i, h in enumerate(NAMES, 1)}
IP = {d["hostname"]: d["mgmt_ip"] for d in DEVICES.values()}
BUNDLES = {
    IP["rA1"]: {IP["rZ1"]: {"bundle": "BE1", "members": ["Hu0/1", "Hu0/2"]}},
    IP["rZ1"]: {IP["rA1"]: {"bundle": "BE1", "members": ["Hu0/1", "Hu0/2"]}},
    IP["rA2"]: {IP["rZ2"]: {"bundle": "BE1", "members": ["Hu0/1"]}},
    IP["rZ2"]: {IP["rA2"]: {"bundle": "BE1", "members": ["Hu0/1"]}},
}
HOST = {v: k for k, v in IP.items()}


@pytest.fixture
def topo():
    random.seed(7)
    return of.Topology(CORRIDOR, DEVICES, BUNDLES)


def optical(scenario):
    return {(HOST[e.ip], e.interface, e.condition) for e in scenario.raise_events if e.oid == OPTICAL_ALARM_OID}


def oids(scenario, oid):
    return {(HOST[e.ip], e.interface) for e in scenario.raise_events if e.oid == oid}


def test_cable_cut_first_site_has_los_and_osc_loss_downstream_only_los(topo):
    s = of.cable_cut(topo, 2)
    alarms = optical(s)
    # AZ: i2 is the first element after span 2 - payload and OSC both gone.
    assert ("i2", "2/LINE-IN", "LOS-P") in alarms and ("i2", "1/OSC-IN", "LOS-O") in alarms
    # Further downstream: payload lost, OSC fine (it's regenerated per span).
    assert ("tZ", "3/LINE-IN", "LOS-P") in alarms and ("tZ", "1/OSC-IN", "LOS-O") not in alarms
    # ZA: i1 is first.
    assert ("i1", "7/LINE-IN", "LOS-P") in alarms and ("i1", "1/OSC-IN", "LOS-O") in alarms
    # Upstream of the cut nothing is dark.
    assert not any(h == "i1" and p == "2/LINE-IN" for h, p, _ in alarms)
    # Both trunks of both wavelengths lose signal; clients signal LF; no BDI (both ways dark).
    assert {("txpZ", "T1", "LOS"), ("txpZ", "T2", "LOS"), ("txpA", "T1", "LOS"), ("txpA", "T2", "LOS")} <= alarms
    assert ("txpZ", "C1", "LF") in alarms and not any(c == "OTU-BDI" for *_, c in alarms)
    # Every circuit down at both routers; BGP down on both bundles.
    assert oids(s, LINK_DOWN_OID) == {("rA1", "Hu0/1"), ("rA1", "Hu0/2"), ("rZ1", "Hu0/1"), ("rZ1", "Hu0/2"), ("rA2", "Hu0/1"), ("rZ2", "Hu0/1")}
    assert oids(s, ISIS_ADJACENCY_DOWN_OID) == oids(s, LINK_DOWN_OID)
    assert len(oids(s, BGP_BACKWARD_TRANSITION_OID)) == 4
    # Nothing in the traps names the span.
    assert "span 2" in s.summary
    # Every raise has a clear.
    assert len(s.clear_events) == len(s.raise_events)


def test_single_fiber_cut_only_one_direction_goes_dark(topo):
    s = of.fiber_cut(topo, 2, "AZ")
    alarms = optical(s)
    assert ("i2", "1/OSC-IN", "LOS-O") in alarms and ("i1", "1/OSC-IN", "LOS-O") not in alarms
    assert ("txpZ", "T1", "LOS") in alarms and ("txpA", "T1", "LOS") not in alarms
    # The transmitting end hears about it through the healthy reverse path.
    assert ("txpA", "T1", "OTU-BDI") in alarms
    # Ethernet takes the link down at both ends anyway.
    assert ("rA1", "Hu0/1") in oids(s, LINK_DOWN_OID) and ("rZ1", "Hu0/1") in oids(s, LINK_DOWN_OID)


def test_amp_failure_downstream_loses_payload_but_keeps_osc(topo):
    s = of.amp_failure(topo, "i1", "AZ")
    alarms = optical(s)
    assert ("i1", "2", "EQPT") in alarms
    assert ("i2", "2/LINE-IN", "LOS-P") in alarms
    assert not any(c == "LOS-O" for *_, c in alarms)  # the discriminator vs a cut
    assert not any(h == "i1" and c == "LOS-P" for h, _, c in alarms)  # its own input is fine


def test_trunk_failure_darkens_one_wavelength_and_leaves_amps_lit(topo):
    s = of.trunk_failure(topo, f"OCH-{WL1:.2f}-AZ")
    alarms = optical(s)
    assert ("txpA", "T1", "EQPT") in alarms
    assert ("tZ", f"6/DROP-{WL1:.2f}", "LOS-P") in alarms and ("txpZ", "T1", "LOS") in alarms
    assert ("tA", f"4/ADD-{WL1:.2f}", "LOS-P") in alarms
    # The other wavelength still lights every amplifier.
    assert not any(p.endswith("LINE-IN") for _, p, _ in alarms if p)
    assert ("txpZ", "T2", "LOS") not in alarms
    # Only the wl1 circuits drop; wl2's router pair is untouched.
    assert ("rA2", "Hu0/1") not in oids(s, LINK_DOWN_OID) and ("rA1", "Hu0/1") in oids(s, LINK_DOWN_OID)


def test_patch_cord_is_client_side_only(topo):
    s = of.patch_cord(topo, 3, "A")
    alarms = optical(s)
    assert alarms == {("txpA", "C3", "LOS"), ("txpZ", "C3", "LF")}
    assert oids(s, LINK_DOWN_OID) == {("rA2", "Hu0/1"), ("rZ2", "Hu0/1")}
    # rA2<->rZ2 has a single-member bundle - so BGP goes too.
    assert len(oids(s, BGP_BACKWARD_TRANSITION_OID)) == 2


def test_partial_bundle_loss_keeps_bgp_up(topo):
    s = of.patch_cord(topo, 1, "Z")
    assert oids(s, BGP_BACKWARD_TRANSITION_OID) == set()


def test_random_scenarios_all_build(topo):
    for kind in of.SCENARIOS:
        for _ in range(5):
            s = of.random_scenario(topo, kind)
            assert s.raise_events and len(s.clear_events) == len(s.raise_events)
            delays = [e.delay for e in s.raise_events]
            assert delays == sorted(delays)


def test_noise_is_one_alarm_and_its_clear(topo):
    r, c, label = of.random_noise(topo)
    assert r.condition == c.condition and r.ip == c.ip and label
