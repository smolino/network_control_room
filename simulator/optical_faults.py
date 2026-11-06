"""Fault scenarios on a modelled optical corridor, and the alarms each
network element would raise because of them.

The corridor comes from the backend (GET /api/corridors/{id}: every
wavelength's route hop by hop in both directions, the OSC of every span,
and the client circuits). A scenario says what physically broke; this
module works out who notices and what they report - each element only
knows about its own ports, exactly like the real network:

* a port whose light disappears raises loss of signal: LOS-P on an
  amplifier/terminal line input, a mux ADD or demux DROP port; LOS on a
  transponder trunk; LOS-O on the OSC input of the span that lost its
  supervisory channel;
* a shared line port (every wavelength goes through it) only goes dark when
  every wavelength through it is dark - one dead wavelength leaves the
  amplifiers lit;
* an amplifier whose input went dark shuts its output down (ALS - state,
  not a fault);
* a transponder whose trunk stopped receiving signals local fault (LF) on
  the client ports of that wavelength, and its far end - still receiving
  in the other direction - gets a backward defect (OTU-BDI);
* routers on a circuit that's down lose the link and the IS-IS adjacency
  on that bundle member, and BGP once every member of the bundle is gone.

None of the traps say where the fault is - that's for the correlator to
work out. The ground truth is only logged by the simulator.

Pure: no SNMP, no I/O - see trap_simulator.optical_fault_loop for the
sending side, tests/test_optical_faults.py for the expected cascades.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from oids import (
    BGP_BACKWARD_TRANSITION_OID,
    BGP_ESTABLISHED_OID,
    ISIS_ADJACENCY_DOWN_OID,
    ISIS_ADJACENCY_UP_OID,
    LINK_DOWN_OID,
    LINK_UP_OID,
    OPTICAL_ALARM_CLEAR_OID,
    OPTICAL_ALARM_OID,
)

AZ, ZA = "AZ", "ZA"
SCENARIOS = ("cable_cut", "fiber_cut", "amp_failure", "trunk_failure", "patch_cord")
# How often each kind of fault happens, relative to the others. Cable cuts
# (backhoes, rodents, civil works) dominate real outages on rented fiber.
SCENARIO_WEIGHTS = {"cable_cut": 40, "fiber_cut": 15, "amp_failure": 15, "trunk_failure": 15, "patch_cord": 15}


@dataclass(frozen=True)
class Event:
    delay: float  # seconds after the fault (or repair)
    oid: str
    ip: str
    interface: str | None = None
    condition: str | None = None
    bgp_peer_ip: str | None = None
    who: str = ""  # hostname, for logging only


@dataclass
class Scenario:
    kind: str
    corridor: str
    summary: str  # the ground truth, for the simulator's log only
    raise_events: list[Event] = field(default_factory=list)
    clear_events: list[Event] = field(default_factory=list)


class Topology:
    """Index over one corridor's detail + the fleet's devices/bundles."""

    def __init__(self, corridor: dict, devices: dict[int, dict], bundles: dict[str, dict[str, dict]]):
        self.corridor = corridor
        self.devices = devices  # id -> {"hostname", "mgmt_ip", "element_role"}
        self.by_hostname = {d["hostname"]: d for d in devices.values()}
        self.bundles = bundles  # router ip -> peer ip -> {"bundle", "members"}
        self.och = [p for p in corridor["paths"] if p["kind"] == "och"]
        self.osc = [p for p in corridor["paths"] if p["kind"] == "osc"]
        self.spans = corridor["spans"]

    def ip(self, hostname: str) -> str:
        return self.by_hostname[hostname]["mgmt_ip"]

    def role(self, device_id: int) -> str:
        return self.devices.get(device_id, {}).get("element_role") or "router"


def _span_index(path: dict, seq: int) -> int | None:
    for i, hop in enumerate(path["hops"]):
        if hop.get("span_seq") == seq:
            return i
    return None


def _port_index(path: dict, hostname: str, kind: str) -> int | None:
    for i, hop in enumerate(path["hops"]):
        if hop.get("hostname") == hostname and hop.get("port_kind") == kind:
            return i
    return None


def _jitter(lo: float, hi: float) -> float:
    return round(random.uniform(lo, hi), 2)


def cascade(
    topo: Topology,
    dead_och: dict[str, int],
    dead_osc: set[str],
    down_circuits: set[int] | None = None,
    extra: list[Event] | None = None,
) -> tuple[list[Event], list[Event]]:
    """(raise, clear) events for a set of broken paths.

    dead_och: och path name -> index of its first dead hop (everything from
    there on gets no light). dead_osc: names of OSC paths that lost light.
    down_circuits: circuits taken down without any path dying (a patch
    cord). extra: the scenario's own root alarms (e.g. EQPT on a card)."""
    events: dict[tuple, Event] = {}

    def add(delay, ip, port, condition, who):
        key = (ip, port, condition)
        if key not in events or events[key].delay > delay:
            events[key] = Event(delay, OPTICAL_ALARM_OID, ip, port, condition, who=who)

    # Which (path, hop index) pairs go through each port - a shared line
    # port is dark only when every path through it is.
    through: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for p in topo.och:
        for i, hop in enumerate(p["hops"]):
            if hop.get("port") is not None and i > 0:  # index 0 is a transmitting trunk
                through.setdefault((hop["hostname"], hop["port"]), []).append((p["name"], i))

    def dark(hostname: str, port: str) -> bool:
        uses = through.get((hostname, port), [])
        return bool(uses) and all(name in dead_och and dead_och[name] <= i for name, i in uses)

    for p in topo.och:
        if p["name"] not in dead_och:
            continue
        for i, hop in enumerate(p["hops"]):
            if i < dead_och[p["name"]] or hop.get("port") is None or i == 0:
                continue
            host, port, kind = hop["hostname"], hop["port"], hop["port_kind"]
            if not dark(host, port):
                continue
            ip = topo.ip(host)
            if kind in ("line_in", "add", "drop"):
                add(_jitter(0, 4), ip, port, "LOS-P", host)
            elif kind == "line_out":
                # The amplifier lost its input and shut its laser down.
                add(_jitter(0.5, 5), ip, port, "ALS", host)
            elif kind == "trunk":
                add(_jitter(1, 6), ip, port, "LOS", host)

    for p in topo.osc:
        if p["name"] in dead_osc:
            rx = p["hops"][-1]
            add(_jitter(0, 4), topo.ip(rx["hostname"]), rx["port"], "LOS-O", rx["hostname"])

    # Per wavelength and direction: is the far trunk receiving?
    trunk_dark: dict[tuple[float, str], bool] = {}
    for p in topo.och:
        last = p["hops"][-1]
        trunk_dark[(p["wavelength_nm"], p["direction"])] = dark(last["hostname"], last["port"])
    for p in topo.och:
        wl, direction = p["wavelength_nm"], p["direction"]
        reverse = ZA if direction == AZ else AZ
        if trunk_dark[(wl, direction)] and not trunk_dark.get((wl, reverse), False):
            src = p["hops"][0]
            add(_jitter(1.5, 7), topo.ip(src["hostname"]), src["port"], "OTU-BDI", src["hostname"])

    # Client circuits: down if their wavelength is dark either way (or
    # explicitly, for a patch cord).
    down = set(down_circuits or ())
    router_events: list[Event] = []
    for c in topo.corridor["circuits"]:
        wl = c["wavelength_nm"]
        az_dark, za_dark = trunk_dark.get((wl, AZ), False), trunk_dark.get((wl, ZA), False)
        if az_dark or za_dark:
            down.add(c["id"])
        for is_dark, client in ((az_dark, c["z_client"]), (za_dark, c["a_client"])):
            if is_dark:
                host, port = client.split(" ", 1)
                add(_jitter(1.5, 7), topo.ip(host), port, "LF", host)

    peer_members: dict[tuple[str, str], int] = {}
    for c in topo.corridor["circuits"]:
        if c["id"] not in down:
            continue
        for host, port, peer in ((c["a_router"], c["a_router_port"], c["z_router"]), (c["z_router"], c["z_router_port"], c["a_router"])):
            ip, peer_ip = topo.ip(host), topo.ip(peer)
            t_link = _jitter(2, 8)
            router_events.append(Event(t_link, LINK_DOWN_OID, ip, port, who=host))
            router_events.append(Event(round(t_link + _jitter(0.5, 3), 2), ISIS_ADJACENCY_DOWN_OID, ip, port, bgp_peer_ip=peer_ip, who=host))
            peer_members[(ip, peer_ip)] = peer_members.get((ip, peer_ip), 0) + 1
    # BGP goes once the whole bundle is gone (hold time after IS-IS).
    for (ip, peer_ip), n_down in peer_members.items():
        bundle = topo.bundles.get(ip, {}).get(peer_ip)
        if bundle and n_down >= len(bundle["members"]):
            who = next((d["hostname"] for d in topo.devices.values() if d["mgmt_ip"] == ip), "")
            router_events.append(Event(_jitter(9, 16), BGP_BACKWARD_TRANSITION_OID, ip, bundle["bundle"], bgp_peer_ip=peer_ip, who=who))

    raises = sorted([*events.values(), *(extra or []), *router_events], key=lambda e: e.delay)

    # Clears: optical first, routers once light is back, BGP last.
    clears = []
    for e in raises:
        if e.oid == OPTICAL_ALARM_OID:
            clears.append(Event(_jitter(0, 5), OPTICAL_ALARM_CLEAR_OID, e.ip, e.interface, e.condition, who=e.who))
        elif e.oid == LINK_DOWN_OID:
            clears.append(Event(_jitter(5, 12), LINK_UP_OID, e.ip, e.interface, who=e.who))
        elif e.oid == ISIS_ADJACENCY_DOWN_OID:
            clears.append(Event(_jitter(8, 16), ISIS_ADJACENCY_UP_OID, e.ip, e.interface, bgp_peer_ip=e.bgp_peer_ip, who=e.who))
        elif e.oid == BGP_BACKWARD_TRANSITION_OID:
            clears.append(Event(_jitter(18, 30), BGP_ESTABLISHED_OID, e.ip, e.interface, bgp_peer_ip=e.bgp_peer_ip, who=e.who))
    return raises, sorted(clears, key=lambda e: e.delay)


# --- scenarios -----------------------------------------------------------------


def cable_cut(topo: Topology, seq: int) -> Scenario:
    """Both fibers of span `seq` cut (a backhoe through the duct)."""
    dead = {p["name"]: _span_index(p, seq) for p in topo.och if _span_index(p, seq) is not None}
    osc = {p["name"] for p in topo.osc if _span_index(p, seq) is not None}
    s = topo.spans[seq - 1]
    raises, clears = cascade(topo, dead, osc)
    return Scenario("cable_cut", topo.corridor["name"],
                    f"cable cut on span {seq} ({s['from_hostname']} -> {s['to_hostname']}, {s['provider']})", raises, clears)


def fiber_cut(topo: Topology, seq: int, direction: str) -> Scenario:
    """One fiber of span `seq` broken - only `direction` loses light."""
    dead = {p["name"]: _span_index(p, seq) for p in topo.och if p["direction"] == direction and _span_index(p, seq) is not None}
    osc = {p["name"] for p in topo.osc if p["direction"] == direction and _span_index(p, seq) is not None}
    s = topo.spans[seq - 1]
    raises, clears = cascade(topo, dead, osc)
    return Scenario("fiber_cut", topo.corridor["name"],
                    f"single fiber cut on span {seq} ({s['from_hostname']} -> {s['to_hostname']}), {direction} direction only",
                    raises, clears)


def amp_failure(topo: Topology, hostname: str, direction: str) -> Scenario:
    """The line amplifier at ILA `hostname` for `direction` dies: no output,
    but the OSC (a separate card) keeps working - so the next site sees
    LOS-P without LOS-O. The card itself raises EQPT (equipment)."""
    dead, slot = {}, None
    for p in topo.och:
        if p["direction"] != direction:
            continue
        i = _port_index(p, hostname, "line_in")
        if i is not None:
            dead[p["name"]] = i + 1  # its LINE-OUT onwards
            slot = p["hops"][i]["port"].split("/", 1)[0]
    root = [Event(_jitter(0, 2), OPTICAL_ALARM_OID, topo.ip(hostname), slot, "EQPT", who=hostname)]
    raises, clears = cascade(topo, dead, set(), extra=root)
    return Scenario("amp_failure", topo.corridor["name"],
                    f"line amplifier failure at {hostname} (slot {slot}, {direction})", raises, clears)


def trunk_failure(topo: Topology, path_name: str) -> Scenario:
    """The transmitting trunk laser of one wavelength fails at its source
    transponder: only that wavelength, only that direction."""
    path = next(p for p in topo.och if p["name"] == path_name)
    src = path["hops"][0]
    root = [Event(_jitter(0, 2), OPTICAL_ALARM_OID, topo.ip(src["hostname"]), src["port"], "EQPT", who=src["hostname"])]
    raises, clears = cascade(topo, {path_name: 1}, set(), extra=root)
    return Scenario("trunk_failure", topo.corridor["name"],
                    f"trunk transmitter failure on {src['hostname']} {src['port']} ({path['wavelength_nm']} nm, {path['direction']})",
                    raises, clears)


def patch_cord(topo: Topology, circuit_id: int, side: str) -> Scenario:
    """The patch cord between a router port and its transponder client at
    `side` ("A"/"Z") is broken: the transponder client sees no light from
    the router; the far end gets a local fault; both routers drop the link."""
    c = next(c for c in topo.corridor["circuits"] if c["id"] == circuit_id)
    near, far = (c["a_client"], c["z_client"]) if side == "A" else (c["z_client"], c["a_client"])
    near_host, near_port = near.split(" ", 1)
    far_host, far_port = far.split(" ", 1)
    root = [
        Event(_jitter(0, 2), OPTICAL_ALARM_OID, topo.ip(near_host), near_port, "LOS", who=near_host),
        Event(_jitter(1, 5), OPTICAL_ALARM_OID, topo.ip(far_host), far_port, "LF", who=far_host),
    ]
    raises, clears = cascade(topo, {}, set(), down_circuits={circuit_id}, extra=root)
    router = c["a_router"] if side == "A" else c["z_router"]
    port = c["a_router_port"] if side == "A" else c["z_router_port"]
    return Scenario("patch_cord", topo.corridor["name"],
                    f"patch cord {router} {port} <-> {near} broken", raises, clears)


def random_scenario(topo: Topology, kind: str | None = None) -> Scenario:
    kind = kind or random.choices(list(SCENARIO_WEIGHTS), weights=list(SCENARIO_WEIGHTS.values()))[0]
    if kind == "cable_cut":
        return cable_cut(topo, random.choice(topo.spans)["seq"])
    if kind == "fiber_cut":
        return fiber_cut(topo, random.choice(topo.spans)["seq"], random.choice((AZ, ZA)))
    if kind == "amp_failure":
        ilas = [s["to_hostname"] for s in topo.spans[:-1]]
        return amp_failure(topo, random.choice(ilas), random.choice((AZ, ZA)))
    if kind == "trunk_failure":
        return trunk_failure(topo, random.choice(topo.och)["name"])
    return patch_cord(topo, random.choice(topo.corridor["circuits"])["id"], random.choice("AZ"))


# --- background noise ------------------------------------------------------------

# Real optical networks are never quiet: power drifts below the degrade
# threshold, a fan wears out. None of it is a fiber fault.
NOISE_CONDITIONS = (("OPWR-LDEG", "line_in", 0.6), ("FAN", None, 0.15), ("HITEMP", None, 0.1), ("LASER-BIAS", "trunk", 0.15))


def random_noise(topo: Topology) -> tuple[Event, Event, str]:
    """One stray alarm on a random optical element, and its clear."""
    condition, kind, _ = random.choices(NOISE_CONDITIONS, weights=[w for *_, w in NOISE_CONDITIONS])[0]
    ports = [
        (hop["hostname"], hop["port"])
        for p in topo.och
        for hop in p["hops"]
        if hop.get("port") and (kind is None or hop.get("port_kind") == kind)
    ]
    host, port = random.choice(ports)
    if kind is None:
        port = None
    raise_ = Event(0, OPTICAL_ALARM_OID, topo.ip(host), port, condition, who=host)
    clear = Event(0, OPTICAL_ALARM_CLEAR_OID, topo.ip(host), port, condition, who=host)
    return raise_, clear, f"{condition} on {host}{' ' + port if port else ''}"
