"""Optical corridor model: validation, path building, import and read-back.

A corridor spec (schemas.CorridorSpec, seed/corridors/*.json) lists sites,
devices with their cards/ports, the dark-fiber spans and the client
circuits. Everything else is derived here, so the spec only has to say what
is physically there:

* card ports, from each card's function (a booster gets COM-IN/LINE-OUT, a
  line amp LINE-IN/LINE-OUT, a mux one ADD port per wavelength, ...);
* the ordered route of every wavelength in each direction ("och" paths) -
  transponder trunk -> mux -> booster -> span -> line amps/spans ... ->
  preamp -> demux -> far transponder trunk;
* the supervisory channel of every span in each direction ("osc" paths) -
  OSC card -> span -> OSC card.

Those ordered routes are what lets the root-cause engine say "the first
element that lost light is X, so the break is on the span just before it".

build_plan() is pure (no DB) so it can be unit-tested; import_corridor()
writes the plan to Postgres and app.topology_graph.sync_corridor_to_neo4j
mirrors it into the graph.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.bundles import ensure_bundles_for_peering, sync_bundle_members
from app.models import (
    L1_ELEMENT_ROLES,
    BgpPeering,
    InterfaceBundle,
    BgpSessionStatus,
    Card,
    ClientCircuit,
    Corridor,
    ElementRole,
    FiberSpan,
    Incident,
    IncidentStatus,
    OpticalPath,
    PathHop,
    Port,
    Router,
    RouterStatus,
    RouterType,
    Site,
)
from app.schemas import (
    CarriedOut,
    CircuitOut,
    CorridorFaultOut,
    CorridorImportOut,
    CorridorOut,
    CorridorSpec,
    DeviceSpec,
    OpticalPathOut,
    PathHopOut,
    RoutePointOut,
    SpanOut,
)

AZ, ZA = "AZ", "ZA"
DIRECTIONS = (AZ, ZA)

# Card functions on the line system and the ports each one gets. "{wl}"
# expands to one port per wavelength the corridor carries.
_FUNCTION_PORTS: dict[str, list[tuple[str, str]]] = {
    "mux": [("ADD-{wl}", "add"), ("COM-OUT", "com_out")],
    "booster": [("COM-IN", "com_in"), ("LINE-OUT", "line_out")],
    "preamp": [("LINE-IN", "line_in"), ("COM-OUT", "com_out")],
    "demux": [("COM-IN", "com_in"), ("DROP-{wl}", "drop")],
    "line_amp": [("LINE-IN", "line_in"), ("LINE-OUT", "line_out")],
    "osc": [("OSC-IN", "osc_in"), ("OSC-OUT", "osc_out")],
}
FUNCTIONS = set(_FUNCTION_PORTS)


class CorridorSpecError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def wl_label(wavelength_nm: float) -> str:
    return f"{wavelength_nm:.2f}"


def card_port_name(slot: int, name: str) -> str:
    return f"{slot}/{name}"


def _flip(direction: str) -> str:
    return ZA if direction == AZ else AZ


# --- pure plan ---------------------------------------------------------------

# A hop is ("port", hostname, port_name) or ("span", seq).
Hop = tuple


@dataclass
class PathPlan:
    name: str
    kind: str
    direction: str
    wavelength_nm: float | None
    hops: list[Hop]


@dataclass
class PortPlan:
    name: str
    kind: str
    direction: str | None = None
    wavelength_nm: float | None = None
    card_slot: int | None = None


@dataclass
class Plan:
    chain: list[str]  # line devices from site A's terminal to site Z's terminal
    wavelengths: list[float]
    ports: dict[str, list[PortPlan]]  # hostname -> every port it should have
    paths: list[PathPlan]
    warnings: list[str] = field(default_factory=list)


def _cards_by_function(device: DeviceSpec) -> dict[tuple[str, str | None], list]:
    out: dict[tuple[str, str | None], list] = {}
    for card in device.cards:
        if card.function:
            out.setdefault((card.function, card.direction), []).append(card)
    return out


def _one_card(device: DeviceSpec, function: str, direction: str | None, errors: list[str]):
    cards = _cards_by_function(device).get((function, direction), [])
    if len(cards) != 1:
        where = f" for direction {direction}" if direction else ""
        errors.append(f"{device.hostname}: expected exactly one {function} card{where}, found {len(cards)}")
        return None
    return cards[0]


def build_plan(spec: CorridorSpec) -> Plan:
    """Validates the spec and derives every port and ordered path. Raises
    CorridorSpecError listing every problem found, not just the first."""
    errors: list[str] = []
    warnings: list[str] = []
    devices = {d.hostname: d for d in spec.devices}
    if len(devices) != len(spec.devices):
        errors.append("duplicate device hostnames")
    ips = [d.mgmt_ip for d in spec.devices]
    if len(set(ips)) != len(ips):
        errors.append("duplicate device mgmt_ip values")
    site_codes = [s.code for s in spec.sites]
    if len(set(site_codes)) != len(site_codes):
        errors.append("duplicate site codes")
    if len(spec.sites) < 2:
        errors.append("a corridor needs at least two sites")
    for d in spec.devices:
        if d.site not in site_codes:
            errors.append(f"{d.hostname}: unknown site {d.site!r}")
        for card in d.cards:
            if card.function and card.function not in FUNCTIONS:
                errors.append(f"{d.hostname} slot {card.slot}: unknown card function {card.function!r}")
        if len({c.slot for c in d.cards}) != len(d.cards):
            errors.append(f"{d.hostname}: duplicate card slots")
    if errors:
        raise CorridorSpecError(errors)

    # The line chain, from the spans: seq 1..n, each starting where the
    # previous one ended.
    spans = sorted(spec.spans, key=lambda s: s.seq)
    if not spans:
        raise CorridorSpecError(["a corridor needs at least one span"])
    if [s.seq for s in spans] != list(range(1, len(spans) + 1)):
        errors.append("span seq must run 1..n with no gaps")
    chain = [spans[0].from_device]
    for s in spans:
        for h in (s.from_device, s.to_device):
            if h not in devices:
                errors.append(f"span {s.seq}: unknown device {h!r}")
        if s.from_device != chain[-1]:
            errors.append(f"span {s.seq} starts at {s.from_device}, but span {s.seq - 1} ended at {chain[-1]}")
        chain.append(s.to_device)
    if errors:
        raise CorridorSpecError(errors)
    if len(set(chain)) != len(chain):
        raise CorridorSpecError(["the span chain visits a device twice"])

    term_a, term_z = devices[chain[0]], devices[chain[-1]]
    for t, site in ((term_a, spec.sites[0].code), (term_z, spec.sites[-1].code)):
        if t.role != ElementRole.TERMINAL:
            errors.append(f"{t.hostname}: a corridor must start and end on a terminal, got {t.role.value}")
        if t.site != site:
            errors.append(f"{t.hostname}: end terminal is at site {t.site}, expected {site}")
    for h in chain[1:-1]:
        if devices[h].role != ElementRole.ILA:
            errors.append(f"{h}: interior line elements must be ILAs, got {devices[h].role.value}")
    if errors:
        raise CorridorSpecError(errors)

    # Wavelengths: every trunk at site A that has a matching trunk at site Z.
    def trunks_at(site: str) -> dict[float, tuple[str, str]]:
        out: dict[float, tuple[str, str]] = {}
        for d in spec.devices:
            if d.role == ElementRole.TRANSPONDER and d.site == site:
                for p in d.ports:
                    if p.kind == "trunk":
                        if p.wavelength_nm is None:
                            errors.append(f"{d.hostname} {p.name}: trunk port without wavelength_nm")
                        elif p.wavelength_nm in out:
                            errors.append(f"wavelength {wl_label(p.wavelength_nm)} used twice at site {site}")
                        else:
                            out[p.wavelength_nm] = (d.hostname, p.name)
        return out

    trunks_a, trunks_z = trunks_at(spec.sites[0].code), trunks_at(spec.sites[-1].code)
    wavelengths = sorted(set(trunks_a) & set(trunks_z))
    for wl in sorted(set(trunks_a) ^ set(trunks_z)):
        warnings.append(f"wavelength {wl_label(wl)} has a transponder trunk at only one end - no path built")
    if not wavelengths:
        warnings.append("no wavelength has trunks at both ends - only OSC paths built")

    # Terminal and ILA cards for each direction. Terminal A transmits AZ and
    # receives ZA; terminal Z the other way round.
    tx_dir = {term_a.hostname: AZ, term_z.hostname: ZA}
    for t in (term_a, term_z):
        for function in ("mux", "booster"):
            _one_card(t, function, tx_dir[t.hostname], errors)
        for function in ("preamp", "demux"):
            _one_card(t, function, _flip(tx_dir[t.hostname]), errors)
    for h in chain[1:-1]:
        for direction in DIRECTIONS:
            _one_card(devices[h], "line_amp", direction, errors)
    if errors:
        raise CorridorSpecError(errors)

    # Ports: spec ports + the ones each line card implies.
    ports: dict[str, list[PortPlan]] = {}
    for d in spec.devices:
        plans = [PortPlan(p.name, p.kind, None, p.wavelength_nm) for p in d.ports]
        for card in d.cards:
            for name, kind in _FUNCTION_PORTS.get(card.function or "", []):
                if "{wl}" in name:
                    for wl in wavelengths:
                        plans.append(
                            PortPlan(card_port_name(card.slot, name.format(wl=wl_label(wl))), kind, card.direction, wl, card.slot)
                        )
                else:
                    plans.append(PortPlan(card_port_name(card.slot, name), kind, card.direction, None, card.slot))
        names = [p.name for p in plans]
        if len(set(names)) != len(names):
            errors.append(f"{d.hostname}: duplicate port names")
        ports[d.hostname] = plans
    if errors:
        raise CorridorSpecError(errors)

    def port_of(hostname: str, function: str, direction: str | None, port: str) -> Hop:
        card = _cards_by_function(devices[hostname])[(function, direction)][0]
        return ("port", hostname, card_port_name(card.slot, port))

    paths: list[PathPlan] = []
    for direction in DIRECTIONS:
        line = chain if direction == AZ else list(reversed(chain))
        span_seqs = [s.seq for s in spans] if direction == AZ else [s.seq for s in reversed(spans)]
        src_trunks, dst_trunks = (trunks_a, trunks_z) if direction == AZ else (trunks_z, trunks_a)
        src, dst = line[0], line[-1]
        for wl in wavelengths:
            wls = wl_label(wl)
            hops: list[Hop] = [
                ("port", *src_trunks[wl]),
                port_of(src, "mux", direction, f"ADD-{wls}"),
                port_of(src, "mux", direction, "COM-OUT"),
                port_of(src, "booster", direction, "COM-IN"),
                port_of(src, "booster", direction, "LINE-OUT"),
            ]
            for i, seq in enumerate(span_seqs):
                hops.append(("span", seq))
                nxt = line[i + 1]
                if nxt == dst:
                    hops += [
                        port_of(dst, "preamp", direction, "LINE-IN"),
                        port_of(dst, "preamp", direction, "COM-OUT"),
                        port_of(dst, "demux", direction, "COM-IN"),
                        port_of(dst, "demux", direction, f"DROP-{wls}"),
                        ("port", *dst_trunks[wl]),
                    ]
                else:
                    hops += [port_of(nxt, "line_amp", direction, "LINE-IN"), port_of(nxt, "line_amp", direction, "LINE-OUT")]
            paths.append(PathPlan(f"OCH-{wls}-{direction}", "och", direction, wl, hops))

        # OSC: per span, from the upstream element's card facing downstream
        # to the downstream element's card facing upstream.
        for s in spans:
            up, down = (s.from_device, s.to_device) if direction == AZ else (s.to_device, s.from_device)
            up_face, down_face = ("Z", "A") if direction == AZ else ("A", "Z")
            up_card = _cards_by_function(devices[up]).get(("osc", up_face))
            down_card = _cards_by_function(devices[down]).get(("osc", down_face))
            if not up_card or not down_card:
                warnings.append(f"span {s.seq} {direction}: no OSC card facing the span at both ends - no OSC path")
                continue
            paths.append(
                PathPlan(
                    f"OSC-S{s.seq}-{direction}",
                    "osc",
                    direction,
                    None,
                    [
                        ("port", up, card_port_name(up_card[0].slot, "OSC-OUT")),
                        ("span", s.seq),
                        ("port", down, card_port_name(down_card[0].slot, "OSC-IN")),
                    ],
                )
            )

    # Circuits must reference real ports on the right kind of device.
    port_names = {h: {p.name: p for p in plans} for h, plans in ports.items()}
    for i, c in enumerate(spec.circuits, 1):
        checks = (
            (c.a_router, c.a_router_port, "router_eth", ElementRole.ROUTER),
            (c.a_transponder, c.a_client_port, "client", ElementRole.TRANSPONDER),
            (c.z_transponder, c.z_client_port, "client", ElementRole.TRANSPONDER),
            (c.z_router, c.z_router_port, "router_eth", ElementRole.ROUTER),
        )
        for hostname, port, kind, role in checks:
            if hostname not in devices:
                errors.append(f"circuit {i}: unknown device {hostname!r}")
            elif devices[hostname].role != role:
                errors.append(f"circuit {i}: {hostname} is a {devices[hostname].role.value}, expected {role.value}")
            elif port not in port_names[hostname] or port_names[hostname][port].kind != kind:
                errors.append(f"circuit {i}: {hostname} has no {kind} port {port!r}")
        if c.wavelength_nm not in wavelengths:
            errors.append(f"circuit {i}: wavelength {wl_label(c.wavelength_nm)} isn't carried end to end")
        elif c.a_transponder != trunks_a[c.wavelength_nm][0] or c.z_transponder != trunks_z[c.wavelength_nm][0]:
            errors.append(f"circuit {i}: wavelength {wl_label(c.wavelength_nm)} isn't on transponders {c.a_transponder}/{c.z_transponder}")
    used = [(c.a_transponder, c.a_client_port) for c in spec.circuits] + [(c.z_transponder, c.z_client_port) for c in spec.circuits]
    used += [(c.a_router, c.a_router_port) for c in spec.circuits] + [(c.z_router, c.z_router_port) for c in spec.circuits]
    if len(set(used)) != len(used):
        errors.append("a port is used by more than one circuit")
    for link in spec.l3_links:
        members = [c for c in spec.circuits if {c.a_router, c.z_router} == {link.router_a, link.router_z}]
        if not members:
            errors.append(f"l3 link {link.router_a} <-> {link.router_z}: no circuit between those routers")
    if errors:
        raise CorridorSpecError(errors)

    return Plan(chain=chain, wavelengths=wavelengths, ports=ports, paths=paths, warnings=warnings)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# --- import ------------------------------------------------------------------


def _upsert_site(db: Session, spec) -> Site:
    site = db.query(Site).filter(Site.code == spec.code).first()
    if site is None:
        site = Site(code=spec.code)
        db.add(site)
    for attr in ("name", "kind", "city", "country", "latitude", "longitude"):
        setattr(site, attr, getattr(spec, attr))
    return site


def _sync_device_ports(db: Session, device: Router, dspec: DeviceSpec, plans: list[PortPlan]) -> dict[str, Port]:
    """Makes the device's cards and ports match the plan. Optical elements
    belong to exactly one line system, so anything not in the plan is
    removed; a router can sit on several corridors, so its ports are only
    ever added to."""
    is_l1 = dspec.role in L1_ELEMENT_ROLES
    cards = {c.slot: c for c in db.query(Card).filter(Card.device_id == device.id)}
    wanted_slots = {c.slot for c in dspec.cards}
    for cspec in dspec.cards:
        card = cards.get(cspec.slot)
        if card is None:
            card = Card(device_id=device.id, slot=cspec.slot)
            db.add(card)
            cards[cspec.slot] = card
        card.card_type, card.function, card.direction = cspec.type, cspec.function, cspec.direction
    db.flush()

    existing = {p.name: p for p in db.query(Port).filter(Port.device_id == device.id)}
    wanted = {p.name for p in plans}
    if is_l1:
        for name, port in list(existing.items()):
            if name not in wanted:
                db.delete(port)
                del existing[name]
        for slot, card in list(cards.items()):
            if slot not in wanted_slots:
                db.delete(card)
        db.flush()
    for plan in plans:
        port = existing.get(plan.name)
        if port is None:
            port = Port(device_id=device.id, name=plan.name)
            db.add(port)
            existing[plan.name] = port
        port.kind, port.direction, port.wavelength_nm = plan.kind, plan.direction, plan.wavelength_nm
        port.card_id = cards[plan.card_slot].id if plan.card_slot is not None else None
    db.flush()
    return existing


def _clear_corridor_paths(db: Session, corridor_id: int) -> None:
    path_ids = [pid for (pid,) in db.query(OpticalPath.id).filter(OpticalPath.corridor_id == corridor_id)]
    if path_ids:
        db.query(PathHop).filter(PathHop.path_id.in_(path_ids)).delete(synchronize_session=False)
    db.query(OpticalPath).filter(OpticalPath.corridor_id == corridor_id).delete(synchronize_session=False)
    db.query(ClientCircuit).filter(ClientCircuit.corridor_id == corridor_id).delete(synchronize_session=False)
    db.flush()


def l1_peerings(db: Session, device_ids: list[int]) -> list[BgpPeering]:
    """L3 peerings with an optical element on either side - leftovers from
    modelling a terminal as a router; IS-IS/BGP never run there."""
    if not device_ids:
        return []
    return (
        db.query(BgpPeering)
        .filter(or_(BgpPeering.router_a_id.in_(device_ids), BgpPeering.router_b_id.in_(device_ids)))
        .all()
    )


def import_corridor(db: Session, spec: CorridorSpec, prune_l1_peerings: bool = False) -> tuple[CorridorImportOut, list]:
    """Writes the corridor to Postgres (idempotent: re-importing the same
    spec changes nothing). Returns the summary and the (peering, bundle_a,
    bundle_b) triples whose topology-graph mirror the caller refreshes after
    commit. Devices are matched on mgmt_ip, like POST /api/routers/seed."""
    # Imported here to avoid a cycle: api.bgp imports this module's callers.
    from app.api.bgp import delete_peering

    plan = build_plan(spec)

    sites = {s.code: _upsert_site(db, s) for s in spec.sites}
    db.flush()

    created, updated = [], []
    devices: dict[str, Router] = {}
    for d in spec.devices:
        site = sites[d.site]
        device = db.query(Router).filter(Router.mgmt_ip == d.mgmt_ip).first()
        if device is None:
            device = Router(mgmt_ip=d.mgmt_ip, router_type=RouterType.PRIMARY, status=RouterStatus.UNKNOWN)
            db.add(device)
            created.append(d.hostname)
        else:
            updated.append(d.hostname)
        device.hostname = d.hostname
        device.vendor = d.vendor
        device.model = d.model
        device.element_role = d.role
        device.site_id = site.id
        device.site_name = site.name
        device.city = site.city
        device.country = site.country
        device.latitude = site.latitude
        device.longitude = site.longitude
        devices[d.hostname] = device
    db.flush()

    corridor = db.query(Corridor).filter(Corridor.name == spec.name).first()
    if corridor is None:
        corridor = Corridor(name=spec.name, site_a_id=sites[spec.sites[0].code].id, site_z_id=sites[spec.sites[-1].code].id)
        db.add(corridor)
        db.flush()
    corridor.description = spec.description
    corridor.assumptions = json.dumps(spec.assumptions)
    corridor.site_a_id = sites[spec.sites[0].code].id
    corridor.site_z_id = sites[spec.sites[-1].code].id
    _clear_corridor_paths(db, corridor.id)

    ports: dict[str, dict[str, Port]] = {}
    for d in spec.devices:
        ports[d.hostname] = _sync_device_ports(db, devices[d.hostname], d, plan.ports[d.hostname])

    spans_by_seq = {s.seq: s for s in db.query(FiberSpan).filter(FiberSpan.corridor_id == corridor.id)}
    wanted_seqs = {s.seq for s in spec.spans}
    for seq, span in list(spans_by_seq.items()):
        if seq not in wanted_seqs:
            db.delete(span)
            del spans_by_seq[seq]
    total_km = 0.0
    for s in spec.spans:
        span = spans_by_seq.get(s.seq)
        if span is None:
            span = FiberSpan(corridor_id=corridor.id, seq=s.seq)
            db.add(span)
            spans_by_seq[s.seq] = span
        span.from_device_id = devices[s.from_device].id
        span.to_device_id = devices[s.to_device].id
        span.provider, span.a_end, span.b_end = s.provider, s.a_end, s.b_end
        span.provider_circuit_id = s.provider_circuit_id
        if s.length_km is not None:
            span.length_km = s.length_km
        else:
            a, b = devices[s.from_device], devices[s.to_device]
            span.length_km = round(haversine_km(a.latitude, a.longitude, b.latitude, b.longitude), 1)
            plan.warnings.append(f"span {s.seq}: no length given - using {span.length_km} km straight-line distance")
        total_km += span.length_km
    db.flush()

    for p in plan.paths:
        path = OpticalPath(corridor_id=corridor.id, name=p.name, kind=p.kind, direction=p.direction, wavelength_nm=p.wavelength_nm)
        db.add(path)
        db.flush()
        for seq, hop in enumerate(p.hops, 1):
            if hop[0] == "span":
                db.add(PathHop(path_id=path.id, seq=seq, span_id=spans_by_seq[hop[1]].id))
            else:
                db.add(PathHop(path_id=path.id, seq=seq, port_id=ports[hop[1]][hop[2]].id))
    db.flush()

    # L3 links carried by the corridor, with the circuits' router ports as
    # bundle members.
    ila_count = len(plan.chain) - 2
    peering_ids, graph_sync = [], []
    circuits_by_pair: dict[frozenset, list] = {}
    for c in spec.circuits:
        circuits_by_pair.setdefault(frozenset((c.a_router, c.z_router)), []).append(c)
    peering_for_pair: dict[frozenset, BgpPeering] = {}
    for link in spec.l3_links:
        ra, rz = devices[link.router_a], devices[link.router_z]
        id_a, id_b = sorted([ra.id, rz.id])
        peering = db.query(BgpPeering).filter(BgpPeering.router_a_id == id_a, BgpPeering.router_b_id == id_b).first()
        if peering is None:
            peering = BgpPeering(router_a_id=id_a, router_b_id=id_b, status=BgpSessionStatus.ESTABLISHED)
            db.add(peering)
        peering.corridor_id = corridor.id
        peering.distance_km = round(total_km, 1)
        peering.repeater_count = ila_count
        db.flush()
        pair = frozenset((link.router_a, link.router_z))
        members: dict[str, list[str]] = {link.router_a: [], link.router_z: []}
        for c in circuits_by_pair[pair]:
            members[c.a_router].append(c.a_router_port)
            members[c.z_router].append(c.z_router_port)
        bundles = {}
        for hostname, bundle_name in ((link.router_a, link.bundle_a), (link.router_z, link.bundle_z)):
            router = devices[hostname]
            bundle = ensure_bundles_for_peering(
                db, peering.id, router, 0, bundle_name=bundle_name, member_names=members[hostname],
                member_bandwidth_mbps=link.member_bandwidth_mbps,
            )
            sync_bundle_members(db, bundle, members[hostname], link.member_bandwidth_mbps)
            bundles[router.id] = bundle
        peering_for_pair[pair] = peering
        peering_ids.append(peering.id)
        graph_sync.append((peering, bundles[peering.router_a_id], bundles[peering.router_b_id]))

    for c in spec.circuits:
        peering = peering_for_pair.get(frozenset((c.a_router, c.z_router)))
        db.add(
            ClientCircuit(
                corridor_id=corridor.id,
                wavelength_nm=c.wavelength_nm,
                a_router_port_id=ports[c.a_router][c.a_router_port].id,
                a_client_port_id=ports[c.a_transponder][c.a_client_port].id,
                z_client_port_id=ports[c.z_transponder][c.z_client_port].id,
                z_router_port_id=ports[c.z_router][c.z_router_port].id,
                peering_id=peering.id if peering else None,
            )
        )
    db.flush()

    l1_ids = [devices[d.hostname].id for d in spec.devices if d.role in L1_ELEMENT_ROLES]
    stale = l1_peerings(db, l1_ids)
    removed, found = [], []
    for peering in stale:
        if prune_l1_peerings:
            removed.append(peering.id)
            delete_peering(db, peering)
        else:
            found.append(peering.id)
            plan.warnings.append(
                f"peering {peering.id} has an optical element on one side - IS-IS/BGP don't run there;"
                " re-import with ?prune_l1_peerings=true to remove it"
            )
    db.flush()

    return (
        CorridorImportOut(
            corridor_id=corridor.id,
            devices_created=created,
            devices_updated=updated,
            spans=len(spec.spans),
            optical_paths=len(plan.paths),
            client_circuits=len(spec.circuits),
            l3_peering_ids=peering_ids,
            l1_peerings_removed=removed,
            l1_peerings_found=found,
            warnings=plan.warnings,
        ),
        graph_sync,
    )


# --- read back ---------------------------------------------------------------


def corridor_route(db: Session, corridor: Corridor) -> list[RoutePointOut]:
    """The corridor's sites in A->Z order along the line chain, each with
    every corridor device located there (POPs hold routers, transponder and
    terminal; ILA huts just the amp)."""
    spans = corridor.spans
    if not spans:
        return []
    chain = [spans[0].from_device] + [s.to_device for s in spans]
    route: list[RoutePointOut] = []
    pop_devices = {}
    for circuit in corridor.circuits:
        for port in (circuit.a_router_port, circuit.a_client_port, circuit.z_client_port, circuit.z_router_port):
            pop_devices.setdefault(port.device.site_id, set()).add(port.device_id)
    for device in chain:
        site = device.site
        if site is None:
            continue
        ids = sorted({device.id} | pop_devices.get(site.id, set()))
        route.append(
            RoutePointOut(
                site_code=site.code, name=site.name, kind=site.kind, latitude=site.latitude, longitude=site.longitude, device_ids=ids
            )
        )
    return route


def corridor_out(db: Session, corridor: Corridor, detail: bool = False) -> CorridorOut:
    km = 0.0
    spans = []
    for s in corridor.spans:
        spans.append(
            SpanOut(
                id=s.id, seq=s.seq, from_device_id=s.from_device_id, from_hostname=s.from_device.hostname,
                to_device_id=s.to_device_id, to_hostname=s.to_device.hostname, provider=s.provider, a_end=s.a_end,
                b_end=s.b_end, length_km=s.length_km, km_from_a=round(km, 1), provider_circuit_id=s.provider_circuit_id,
            )
        )
        km += s.length_km or 0
    peering_ids = [pid for (pid,) in db.query(BgpPeering.id).filter(BgpPeering.corridor_id == corridor.id).order_by(BgpPeering.id)]
    out = CorridorOut(
        id=corridor.id,
        name=corridor.name,
        description=corridor.description,
        assumptions=json.loads(corridor.assumptions or "[]"),
        site_a=corridor.site_a.name,
        site_z=corridor.site_z.name,
        length_km=round(km, 1),
        route=corridor_route(db, corridor),
        spans=spans,
        peering_ids=peering_ids,
    )
    roots = (
        db.query(Incident)
        .filter(Incident.corridor_id == corridor.id, Incident.status == IncidentStatus.OPEN)
        .order_by(Incident.id)
        .all()
    )
    if roots:
        from app.rca import root_scopes

        scopes = root_scopes(db, roots)
        for r in roots:
            scope = scopes.get(r.id)
            out.faults.append(
                CorridorFaultOut(
                    incident_id=r.id, kind=r.condition or "", device_id=r.router_id, span_seq=r.fault_segment_index,
                    stage=r.stage.value if r.stage else None, priority=r.priority, summary=r.description,
                    circuit_ids=sorted(scope.circuit_ids) if scope else [],
                    peering_ids=sorted(scope.peering_ids) if scope else [],
                    router_ids=sorted(scope.router_ids) if scope else [],
                )
            )
    if detail:
        for p in corridor.paths:
            hops = []
            for h in p.hops:
                if h.span is not None:
                    hops.append(PathHopOut(seq=h.seq, span_seq=h.span.seq))
                else:
                    hops.append(
                        PathHopOut(
                            seq=h.seq, port_id=h.port_id, device_id=h.port.device_id, hostname=h.port.device.hostname,
                            port=h.port.name, port_kind=h.port.kind,
                        )
                    )
            out.paths.append(OpticalPathOut(id=p.id, name=p.name, kind=p.kind, direction=p.direction, wavelength_nm=p.wavelength_nm, hops=hops))
        for c in corridor.circuits:
            out.circuits.append(
                CircuitOut(
                    id=c.id, wavelength_nm=c.wavelength_nm,
                    a_router=c.a_router_port.device.hostname, a_router_port=c.a_router_port.name,
                    a_client=f"{c.a_client_port.device.hostname} {c.a_client_port.name}",
                    z_client=f"{c.z_client_port.device.hostname} {c.z_client_port.name}",
                    z_router=c.z_router_port.device.hostname, z_router_port=c.z_router_port.name,
                    peering_id=c.peering_id,
                )
            )
    return out


def corridor_device_ids(db: Session, corridor_id: int) -> set[int]:
    ids = set()
    for s in db.query(FiberSpan).filter(FiberSpan.corridor_id == corridor_id):
        ids |= {s.from_device_id, s.to_device_id}
    for c in db.query(ClientCircuit).filter(ClientCircuit.corridor_id == corridor_id):
        for port in (c.a_router_port, c.a_client_port, c.z_client_port, c.z_router_port):
            ids.add(port.device_id)
    return ids


def corridors_using_device(db: Session, device_id: int) -> list[str]:
    names = set()
    for (name,) in (
        db.query(Corridor.name)
        .join(FiberSpan, FiberSpan.corridor_id == Corridor.id)
        .filter(or_(FiberSpan.from_device_id == device_id, FiberSpan.to_device_id == device_id))
    ):
        names.add(name)
    port_ids = [pid for (pid,) in db.query(Port.id).filter(Port.device_id == device_id)]
    if port_ids:
        for c in db.query(ClientCircuit).filter(
            or_(
                ClientCircuit.a_router_port_id.in_(port_ids),
                ClientCircuit.a_client_port_id.in_(port_ids),
                ClientCircuit.z_client_port_id.in_(port_ids),
                ClientCircuit.z_router_port_id.in_(port_ids),
            )
        ):
            names.add(c.corridor.name)
    return sorted(names)


def delete_corridor(db: Session, corridor: Corridor) -> None:
    """Removes the corridor's spans/paths/circuits and the cards/ports of
    its optical elements. The devices themselves stay (delete them from the
    fleet separately), and L3 links it carried go back to being plain links."""
    l1_ids = [
        d for d in corridor_device_ids(db, corridor.id)
        if (db.get(Router, d).element_role or ElementRole.ROUTER) in L1_ELEMENT_ROLES
    ]
    _clear_corridor_paths(db, corridor.id)
    db.query(Incident).filter(Incident.corridor_id == corridor.id).update({"corridor_id": None}, synchronize_session=False)
    db.query(FiberSpan).filter(FiberSpan.corridor_id == corridor.id).delete(synchronize_session=False)
    db.query(BgpPeering).filter(BgpPeering.corridor_id == corridor.id).update({"corridor_id": None}, synchronize_session=False)
    if l1_ids:
        db.query(Port).filter(Port.device_id.in_(l1_ids)).delete(synchronize_session=False)
        db.query(Card).filter(Card.device_id.in_(l1_ids)).delete(synchronize_session=False)
    db.delete(corridor)
    db.flush()


def export_spec(db: Session, corridor: Corridor) -> CorridorSpec:
    """The corridor as an import spec (the inverse of import_corridor) - for
    backups: POST it to /api/corridors/import to rebuild the same corridor.
    Card ports are derived on import, so only router/transponder ports are
    listed."""
    spans = corridor.spans
    chain = [spans[0].from_device] + [s.to_device for s in spans] if spans else []
    circuits = corridor.circuits
    devices: list[Router] = []
    for d in chain:
        if d not in devices:
            devices.append(d)
    for c in circuits:
        for port in (c.a_router_port, c.a_client_port, c.z_client_port, c.z_router_port):
            if port.device not in devices:
                devices.append(port.device)

    sites: list[Site] = []
    # A first, then the line's sites in order, then Z; POP-only devices
    # (routers, transponders) sit at A or Z.
    for d in chain:
        if d.site is not None and d.site not in sites:
            sites.append(d.site)
    site_z = corridor.site_z
    if site_z in sites:
        sites.remove(site_z)
        sites.append(site_z)

    used_ports: dict[int, set[int]] = {}
    for c in circuits:
        for port in (c.a_router_port, c.a_client_port, c.z_client_port, c.z_router_port):
            used_ports.setdefault(port.device_id, set()).add(port.id)

    device_specs = []
    for d in devices:
        cards = sorted(db.query(Card).filter(Card.device_id == d.id), key=lambda c: c.slot)
        ports = [
            {"name": p.name, "kind": p.kind, "wavelength_nm": p.wavelength_nm}
            for p in sorted(db.query(Port).filter(Port.device_id == d.id, Port.card_id.is_(None)), key=lambda p: p.id)
            if p.kind in ("trunk", "client") or p.id in used_ports.get(d.id, set())
        ]
        device_specs.append(
            {
                "hostname": d.hostname,
                "mgmt_ip": d.mgmt_ip,
                "role": (d.element_role or ElementRole.ROUTER).value,
                "vendor": d.vendor or "Cisco",
                "model": d.model,
                "site": d.site.code if d.site else (corridor.site_a.code),
                "cards": [{"slot": c.slot, "type": c.card_type, "function": c.function, "direction": c.direction} for c in cards],
                "ports": ports,
            }
        )

    l3_links = []
    for peering in db.query(BgpPeering).filter(BgpPeering.corridor_id == corridor.id).order_by(BgpPeering.id):
        on_link = [c for c in circuits if c.peering_id == peering.id]
        if not on_link:
            continue
        first = on_link[0]
        a, z = first.a_router_port.device, first.z_router_port.device
        bundle = {
            b.router_id: b
            for b in db.query(InterfaceBundle).filter(InterfaceBundle.peering_id == peering.id)
        }
        members = bundle[a.id].members if a.id in bundle else []
        l3_links.append(
            {
                "router_a": a.hostname,
                "router_z": z.hostname,
                "bundle_a": bundle[a.id].name if a.id in bundle else "Bundle-Ether1",
                "bundle_z": bundle[z.id].name if z.id in bundle else "Bundle-Ether1",
                "member_bandwidth_mbps": members[0].bandwidth_mbps if members else 100000,
            }
        )

    return CorridorSpec.model_validate(
        {
            "name": corridor.name,
            "description": corridor.description,
            "assumptions": json.loads(corridor.assumptions or "[]"),
            "sites": [
                {"code": s.code, "name": s.name, "kind": s.kind, "city": s.city, "country": s.country,
                 "latitude": s.latitude, "longitude": s.longitude}
                for s in sites
            ],
            "devices": device_specs,
            "spans": [
                {"seq": s.seq, "from_device": s.from_device.hostname, "to_device": s.to_device.hostname,
                 "provider": s.provider, "a_end": s.a_end, "b_end": s.b_end, "length_km": s.length_km,
                 "provider_circuit_id": s.provider_circuit_id}
                for s in spans
            ],
            "circuits": [
                {"wavelength_nm": c.wavelength_nm,
                 "a_router": c.a_router_port.device.hostname, "a_router_port": c.a_router_port.name,
                 "a_transponder": c.a_client_port.device.hostname, "a_client_port": c.a_client_port.name,
                 "z_transponder": c.z_client_port.device.hostname, "z_client_port": c.z_client_port.name,
                 "z_router": c.z_router_port.device.hostname, "z_router_port": c.z_router_port.name}
                for c in circuits
            ],
            "l3_links": l3_links,
        }
    )


def empty_carried() -> CarriedOut:
    return CarriedOut(optical_paths=[], wavelengths_nm=[], circuit_ids=[], router_interfaces=[], peering_ids=[])
