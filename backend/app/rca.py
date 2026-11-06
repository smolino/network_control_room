"""Root-cause analysis on modelled optical corridors.

A fiber fault never announces itself: a cable cut on one span makes every
element downstream of it lose light, every transponder and router on the
affected wavelengths complain, and the alarms arrive interleaved and out of
order (a single cut on the Bilbao-Madrid corridor is ~60 alarms from 15
elements). This module turns the alarms that are active on a corridor into
diagnoses, by walking the corridor's real optical paths (app.corridors):

* **Where light is first lost.** Per direction, the first line element
  along the path whose line input is dark (LOS-P / LOS / OPWR-LFAIL).
  Everything after it is dark too - that's propagation, not more faults.
* **Fiber or amplifier.** The OSC (supervisory channel) is regenerated at
  every site, so it only dies with the fiber of its own span. Dark input +
  LOS-O on the same span = the span's fiber is broken. Dark input with the
  OSC still up = the fiber is fine, the amplifier feeding it failed.
* **Both directions on the same span** = the whole cable (CABLE-CUT); one
  direction = a single fiber (FIBER-CUT).
* **One wavelength only** (amplifiers lit by the others): the mux ADD port
  dark too = the source transponder's trunk (TRUNK-FAIL); only the far
  drop/trunk dark = the channel between mux and demux (CHANNEL-FAIL).
* **Client side only** (a transponder client port with no light from its
  router, trunks fine) = the router <-> transponder patch cord (PATCH-CORD).
* **OSC lost but traffic fine** = the supervisory channel itself (OSC-FAIL).

Each diagnosis becomes one root incident (OPTICAL_ALARM, transport, the
diagnosis code in `condition`, keyed by `rca_key` so it's refined in place
as alarms keep arriving), and every open incident it explains - the optical
alarms downstream, client faults, router link/IS-IS/BGP - is linked to it as
a symptom. When the alarms clear, the root resolves.

Evidence is read from the alarm history (TrapEvent), not from incidents, so
alarms the equipment filter keeps out of the incident list (a card's EQPT)
still count as evidence. Runs from the correlator's sweep (see evaluate_all),
after the corridor's optical alarms have been quiet for RCA_HOLD_SECONDS -
diagnosing the first few of 60 alarms would point at the wrong span.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    ClientCircuit,
    Corridor,
    FiberSpan,
    Incident,
    IncidentStatus,
    InterfaceBundle,
    OpticalPath,
    Router,
    TrapEvent,
)
from app.snmp.oid_map import OPTICAL_ALARM_CLEAR_OID, OPTICAL_ALARM_OID

AZ, ZA = "AZ", "ZA"
DARK = ("LOS-P", "LOS", "OPWR-LFAIL")
# How long a corridor's optical alarms must be quiet before it's diagnosed,
# and how far back the alarm history is read for still-active conditions.
RCA_HOLD_SECONDS = 5
ACTIVE_WINDOW = timedelta(hours=24)

# Diagnosis code -> severity of the root incident.
KINDS = {
    "CABLE-CUT": "critical",
    "FIBER-CUT": "critical",
    "AMP-FAIL": "critical",
    "TRUNK-FAIL": "critical",
    "CHANNEL-FAIL": "critical",
    "PATCH-CORD": "critical",
    "OSC-FAIL": "warning",
}


# --- the corridor, as the analyzer sees it -------------------------------------


@dataclass
class Hop:
    device_id: int | None
    port: str | None
    kind: str | None  # port kind, None for a span hop
    span_seq: int | None


@dataclass
class Path:
    name: str
    kind: str
    direction: str
    wavelength_nm: float | None
    hops: list[Hop]


@dataclass
class Circuit:
    id: int
    wavelength_nm: float
    a_router_id: int
    a_router_port: str
    a_txp_id: int
    a_client_port: str
    z_txp_id: int
    z_client_port: str
    z_router_id: int
    z_router_port: str
    peering_id: int | None


@dataclass
class CorridorView:
    id: int
    name: str
    site_a: str
    chain: list[int]  # line elements, A to Z
    spans: dict[int, FiberSpan]
    och: list[Path]
    osc: list[Path]
    circuits: list[Circuit]
    bundles: dict[tuple[int, int], str]  # (peering_id, router_id) -> bundle name
    hostnames: dict[int, str]
    sites: dict[int, str]  # device id -> site name

    @property
    def device_ids(self) -> set[int]:
        ids = set(self.chain)
        for p in self.och:
            ids |= {h.device_id for h in p.hops if h.device_id is not None}
        for c in self.circuits:
            ids |= {c.a_router_id, c.z_router_id, c.a_txp_id, c.z_txp_id}
        return ids


def load_view(db: Session, corridor: Corridor) -> CorridorView:
    spans = {s.seq: s for s in corridor.spans}
    chain = [corridor.spans[0].from_device_id] + [s.to_device_id for s in corridor.spans] if corridor.spans else []
    och, osc = [], []
    for p in db.query(OpticalPath).filter(OpticalPath.corridor_id == corridor.id).order_by(OpticalPath.id):
        hops = [
            Hop(h.port.device_id, h.port.name, h.port.kind, None) if h.port_id is not None else Hop(None, None, None, h.span.seq)
            for h in p.hops
        ]
        (och if p.kind == "och" else osc).append(Path(p.name, p.kind, p.direction, p.wavelength_nm, hops))
    circuits = [
        Circuit(
            c.id, c.wavelength_nm, c.a_router_port.device_id, c.a_router_port.name, c.a_client_port.device_id,
            c.a_client_port.name, c.z_client_port.device_id, c.z_client_port.name, c.z_router_port.device_id,
            c.z_router_port.name, c.peering_id,
        )
        for c in db.query(ClientCircuit).filter(ClientCircuit.corridor_id == corridor.id).order_by(ClientCircuit.id)
    ]
    peering_ids = {c.peering_id for c in circuits if c.peering_id}
    bundles = {
        (b.peering_id, b.router_id): b.name
        for b in db.query(InterfaceBundle).filter(InterfaceBundle.peering_id.in_(peering_ids))
    } if peering_ids else {}
    view = CorridorView(corridor.id, corridor.name, corridor.site_a.city or corridor.site_a.name, chain, spans, och, osc, circuits, bundles, {}, {})
    for r in db.query(Router).filter(Router.id.in_(view.device_ids)):
        view.hostnames[r.id] = r.hostname
        view.sites[r.id] = r.site.name if r.site else (r.city or r.hostname)
    return view


# --- evidence ------------------------------------------------------------------

Alarm = tuple[int, str, str]  # (device_id, port, condition)


def active_conditions(db: Session, device_ids: set[int], now: datetime) -> set[Alarm]:
    """Optical conditions raised and not yet cleared on these devices,
    folded from the alarm history in arrival order."""
    state: dict[Alarm, bool] = {}
    rows = (
        db.query(TrapEvent.router_id, TrapEvent.interface_name, TrapEvent.condition, TrapEvent.oid)
        .filter(
            TrapEvent.router_id.in_(device_ids),
            TrapEvent.oid.in_((OPTICAL_ALARM_OID, OPTICAL_ALARM_CLEAR_OID)),
            TrapEvent.condition.isnot(None),
            TrapEvent.received_at >= now - ACTIVE_WINDOW,
        )
        .order_by(TrapEvent.id)
    )
    for router_id, port, condition, oid in rows:
        state[(router_id, port or "", condition)] = oid == OPTICAL_ALARM_OID
    return {key for key, raised in state.items() if raised}


# --- analysis ------------------------------------------------------------------


@dataclass
class Diagnosis:
    kind: str
    key: str
    device_id: int  # the element the root incident is attached to
    summary: str
    span_seq: int | None = None
    directions: list[str] = field(default_factory=list)
    evidence: list[Alarm] = field(default_factory=list)
    # (device_id, interface name) of every incident this explains.
    explains: set[tuple[int, str]] = field(default_factory=set)
    circuit_ids: set[int] = field(default_factory=set)
    peering_ids: set[int] = field(default_factory=set)


def _is_dark(active: set[Alarm], device_id: int, port: str) -> bool:
    return any((device_id, port, c) in active for c in DARK)


def _line_input(view: CorridorView, direction: str, device_id: int) -> str | None:
    for p in view.och:
        if p.direction == direction:
            for h in p.hops:
                if h.device_id == device_id and h.kind == "line_in":
                    return h.port
    return None


def _osc_rx(view: CorridorView, seq: int, direction: str) -> tuple[int, str] | None:
    for p in view.osc:
        if p.direction == direction and any(h.span_seq == seq for h in p.hops):
            rx = p.hops[-1]
            return rx.device_id, rx.port
    return None


def _span_label(view: CorridorView, seq: int) -> str:
    """Short enough to leave room for the impact in a 255-char description;
    the full detail (sites, provider ends, coordinates) is in the analysis."""
    s = view.spans[seq]
    km_from = sum(view.spans[k].length_km or 0 for k in range(1, seq))
    length = s.length_km or 0
    return (
        f"span {seq} {view.hostnames[s.from_device_id]} -> {view.hostnames[s.to_device_id]} "
        f"({s.provider or 'unknown provider'}, km {km_from:.1f}-{km_from + length:.1f} from {view.site_a})"
    )


def _explain(view: CorridorView, d: Diagnosis, dead: dict[str, int], down_circuits: set[int] = frozenset()) -> None:
    """Fills in what a set of dead paths (path name -> first dead hop)
    takes with it: every port after the break, the far trunks and client
    ports of dead wavelengths, the BDI at their source, and the circuits,
    router ports and L3 links that ride them."""
    dead_wl_dir = set()
    for p in view.och:
        if p.name not in dead:
            continue
        dead_wl_dir.add((p.wavelength_nm, p.direction))
        for i, h in enumerate(p.hops):
            if i >= dead[p.name] and h.device_id is not None:
                d.explains.add((h.device_id, h.port))
        d.explains.add((p.hops[0].device_id, p.hops[0].port))  # BDI back at the source trunk
    circuits = set(down_circuits)
    for c in view.circuits:
        if (c.wavelength_nm, AZ) in dead_wl_dir or (c.wavelength_nm, ZA) in dead_wl_dir:
            circuits.add(c.id)
    for c in view.circuits:
        if c.id not in circuits:
            continue
        d.circuit_ids.add(c.id)
        d.explains |= {
            (c.a_txp_id, c.a_client_port), (c.z_txp_id, c.z_client_port),
            (c.a_router_id, c.a_router_port), (c.z_router_id, c.z_router_port),
        }
    # An L3 link is down once every circuit under it is.
    by_peering: dict[int, list[Circuit]] = {}
    for c in view.circuits:
        if c.peering_id:
            by_peering.setdefault(c.peering_id, []).append(c)
    for peering_id, cs in by_peering.items():
        if all(c.id in circuits for c in cs):
            d.peering_ids.add(peering_id)
            for c in cs[:1]:
                for router_id in (c.a_router_id, c.z_router_id):
                    if (peering_id, router_id) in view.bundles:
                        d.explains.add((router_id, view.bundles[(peering_id, router_id)]))


def _impact(d: Diagnosis) -> str:
    parts = []
    if d.circuit_ids:
        parts.append(f"{len(d.circuit_ids)} client circuit(s) down")
    if d.peering_ids:
        parts.append(f"{len(d.peering_ids)} L3 link(s) down")
    return "; ".join(parts) if parts else "no service affected"


def analyze(view: CorridorView, active: set[Alarm]) -> list[Diagnosis]:
    diagnoses: list[Diagnosis] = []
    H = view.hostnames

    # 1. Line breaks: per direction, the first element whose line input is dark.
    line_break: dict[str, tuple[str, int, int]] = {}  # direction -> ("fiber", seq, rx device) | ("amp", device, line idx)
    for direction in (AZ, ZA):
        line = view.chain if direction == AZ else list(reversed(view.chain))
        for i in range(1, len(line)):
            port = _line_input(view, direction, line[i])
            if port is None or not _is_dark(active, line[i], port):
                continue
            a, b = (line[i - 1], line[i]) if direction == AZ else (line[i], line[i - 1])
            seq = next(s.seq for s in view.spans.values() if s.from_device_id == a and s.to_device_id == b)
            osc = _osc_rx(view, seq, direction)
            if osc and (osc[0], osc[1], "LOS-O") in active:
                line_break[direction] = ("fiber", seq, line[i])
            else:
                line_break[direction] = ("amp", line[i - 1], seq)
            break

    def dead_from_span(seq: int, directions) -> dict[str, int]:
        out = {}
        for p in view.och:
            if p.direction in directions:
                for idx, h in enumerate(p.hops):
                    if h.span_seq == seq:
                        out[p.name] = idx
        return out

    fiber = {d: v for d, v in line_break.items() if v[0] == "fiber"}
    handled: set[str] = set()
    if len(fiber) == 2 and fiber[AZ][1] == fiber[ZA][1]:
        seq = fiber[AZ][1]
        d = Diagnosis("CABLE-CUT", f"span:{seq}", view.spans[seq].from_device_id, "", seq, [AZ, ZA])
        d.evidence = sorted(a for a in active if a[2] in (*DARK, "LOS-O") and a[0] in (fiber[AZ][2], fiber[ZA][2]))
        _explain(view, d, dead_from_span(seq, (AZ, ZA)))
        for direction in (AZ, ZA):
            d.explains.add(_osc_rx(view, seq, direction))
        d.summary = (
            f"Cable cut on {_span_label(view, seq)}: both directions dark from the next site on, OSC lost too - {_impact(d)}"
        )
        diagnoses.append(d)
        handled = {AZ, ZA}
    for direction, (what, a, b) in line_break.items():
        if direction in handled:
            continue
        if what == "fiber":
            seq, rx = a, b
            d = Diagnosis("FIBER-CUT", f"span:{seq}", view.spans[seq].from_device_id, "", seq, [direction])
            d.evidence = sorted(x for x in active if x[0] == rx and x[2] in (*DARK, "LOS-O"))
            _explain(view, d, dead_from_span(seq, (direction,)))
            d.explains.add(_osc_rx(view, seq, direction))
            d.summary = (
                f"Fiber cut on {_span_label(view, seq)}, {direction} only: {H[rx]} lost light and OSC, return fiber OK - {_impact(d)}"
            )
        else:
            amp, seq = a, b
            dead = {}
            for p in view.och:
                if p.direction == direction:
                    for idx, h in enumerate(p.hops):
                        if h.device_id == amp and h.kind in ("line_out",):
                            dead[p.name] = idx
            d = Diagnosis("AMP-FAIL", f"amp:{amp}:{direction}", amp, "", seq, [direction])
            d.evidence = sorted(x for x in active if x[0] == amp and x[2] == "EQPT") + sorted(
                x for x in active if x[2] in DARK and x[0] == (view.spans[seq].to_device_id if direction == AZ else view.spans[seq].from_device_id)
            )
            _explain(view, d, dead)
            d.explains |= {(x[0], x[1]) for x in active if x[0] == amp and x[2] == "EQPT"}
            card = next((x[1] for x in active if x[0] == amp and x[2] == "EQPT"), None)
            d.summary = (
                f"Amplifier failure at {H[amp]} ({view.sites[amp]}){f', slot {card}' if card else ''}, {direction}: next site "
                f"lost light but kept the OSC of span {seq}, so the fiber is intact - {_impact(d)}"
            )
        diagnoses.append(d)

    # 2. One wavelength dark while the line is lit.
    for p in view.och:
        if p.direction in line_break:
            continue
        add = next((h for h in p.hops if h.kind == "add"), None)
        drop = next((h for h in p.hops if h.kind == "drop"), None)
        src, dst = p.hops[0], p.hops[-1]
        add_dark = add is not None and _is_dark(active, add.device_id, add.port)
        drop_dark = drop is not None and _is_dark(active, drop.device_id, drop.port)
        trunk_dark = _is_dark(active, dst.device_id, dst.port)
        if not (add_dark or drop_dark or trunk_dark):
            continue
        wl = f"{p.wavelength_nm:.2f} nm"
        if add_dark:
            d = Diagnosis("TRUNK-FAIL", f"trunk:{src.device_id}:{src.port}", src.device_id, "", None, [p.direction])
            d.evidence = sorted(x for x in active if (x[0], x[1]) in {(add.device_id, add.port), (src.device_id, src.port)})
            _explain(view, d, {p.name: 1})
            d.summary = (
                f"Transponder trunk failure at {H[src.device_id]} {src.port} ({wl}, {p.direction}): no light reached the "
                f"mux add port, the other wavelengths are fine - transmitter or the patch to the mux - {_impact(d)}"
            )
        else:
            first = drop if drop_dark else dst
            idx = p.hops.index(first)
            d = Diagnosis("CHANNEL-FAIL", f"channel:{p.name}", first.device_id, "", None, [p.direction])
            d.evidence = sorted(x for x in active if (x[0], x[1]) in {(first.device_id, first.port), (dst.device_id, dst.port)})
            _explain(view, d, {p.name: idx})
            where = "between the mux and the far demux" if drop_dark else f"between the demux and {H[dst.device_id]}"
            d.summary = f"Channel {wl} ({p.direction}) lost {where}, the line itself is fine - {_impact(d)}"
        diagnoses.append(d)

    # 3. Client side: a transponder client port with no light from its router.
    explained = set().union(*(dg.explains for dg in diagnoses)) if diagnoses else set()
    for c in view.circuits:
        for side, txp, port in (("A", c.a_txp_id, c.a_client_port), ("Z", c.z_txp_id, c.z_client_port)):
            if (txp, port, "LOS") in active and (txp, port) not in explained:
                d = Diagnosis("PATCH-CORD", f"patch:{c.id}:{side}", txp, "", None, [])
                d.evidence = [(txp, port, "LOS")]
                _explain(view, d, {}, down_circuits={c.id})
                router, rport = (c.a_router_id, c.a_router_port) if side == "A" else (c.z_router_id, c.z_router_port)
                d.summary = (
                    f"Patch cord {H[router]} {rport} <-> {H[txp]} {port} broken: the transponder gets no light from the "
                    f"router, the optical line is fine - {_impact(d)}"
                )
                diagnoses.append(d)

    # 4. OSC lost on a span whose traffic still flows.
    explained = set().union(*(dg.explains for dg in diagnoses)) if diagnoses else set()
    for p in view.osc:
        rx = p.hops[-1]
        if (rx.device_id, rx.port, "LOS-O") in active and (rx.device_id, rx.port) not in explained:
            seq = p.hops[1].span_seq
            d = Diagnosis("OSC-FAIL", f"osc:{seq}:{p.direction}", rx.device_id, "", seq, [p.direction])
            d.evidence = [(rx.device_id, rx.port, "LOS-O")]
            d.explains.add((rx.device_id, rx.port))
            d.summary = (
                f"Supervisory channel (OSC) lost on {_span_label(view, seq)}, {p.direction}, while traffic still flows - "
                "OSC card or filter, not the fiber - no service affected"
            )
            diagnoses.append(d)
    return diagnoses


# --- what a root takes down -------------------------------------------------------


@dataclass
class Scope:
    circuit_ids: set[int] = field(default_factory=set)
    peering_ids: set[int] = field(default_factory=set)
    router_ids: set[int] = field(default_factory=set)  # L3 routers on those circuits


def root_scopes(db: Session, roots: list[Incident]) -> dict[int, Scope]:
    """What each corridor root takes down, from the symptoms linked to it:
    the client circuits whose router ports lost their link (and so their
    routers), and the L3 links whose bundle went with them. Read from the
    linked incidents rather than re-diagnosing, so it matches what the
    incident list shows."""
    roots = [r for r in roots if r.corridor_id is not None]
    if not roots:
        return {}
    ids = [r.id for r in roots]
    symptoms: dict[int, set[tuple[int, str]]] = {r.id: set() for r in roots}
    for root_id, router_id, iface in db.query(Incident.root_cause_incident_id, Incident.router_id, Incident.interface_name).filter(
        Incident.root_cause_incident_id.in_(ids)
    ):
        symptoms[root_id].add((router_id, iface))
    corridor_ids = {r.corridor_id for r in roots}
    circuits = [
        (c.corridor_id, c.id, c.peering_id, c.a_router_port.device_id, c.a_router_port.name, c.z_router_port.device_id, c.z_router_port.name)
        for c in db.query(ClientCircuit).filter(ClientCircuit.corridor_id.in_(corridor_ids))
    ]
    bundles = {(b.router_id, b.name): b.peering_id for b in db.query(InterfaceBundle)}
    out = {}
    for r in roots:
        scope = Scope()
        seen = symptoms[r.id]
        for corridor_id, cid, peering_id, ar, ap, zr, zp in circuits:
            if corridor_id == r.corridor_id and ((ar, ap) in seen or (zr, zp) in seen):
                scope.circuit_ids.add(cid)
                scope.router_ids |= {ar, zr}
        for router_id, iface in seen:
            if (router_id, iface) in bundles:
                scope.peering_ids.add(bundles[(router_id, iface)])
        out[r.id] = scope
    return out


# --- acting on it --------------------------------------------------------------


def _open_roots(db: Session, corridor_id: int) -> dict[str, Incident]:
    return {
        i.rca_key: i
        for i in db.query(Incident).filter(Incident.corridor_id == corridor_id, Incident.status == IncidentStatus.OPEN)
    }


def apply(db: Session, view: CorridorView, diagnoses: list[Diagnosis], now: datetime) -> None:
    """Opens/updates one root incident per diagnosis, links what it
    explains, and resolves roots whose fault is gone."""
    from app.incidents import service

    roots = _open_roots(db, view.id)
    wanted = {d.key: d for d in diagnoses}
    for key, root in roots.items():
        if key not in wanted:
            service.clear_rca_root(db, root, now)

    for d in diagnoses:
        root = roots.get(d.key)
        router = db.get(Router, d.device_id)
        if root is None:
            root = service.open_rca_root(
                db, router, view.id, d.key, d.kind, KINDS[d.kind], d.summary, d.span_seq, now
            )
        elif root.condition != d.kind or root.description != d.summary[:255]:
            service.update_rca_root(db, root, router, d.kind, KINDS[d.kind], d.summary, now)
        service.link_symptoms(db, root, _explained_incidents(db, view, d, root), now)
        db.flush()
        # Its impact (and so its priority) depends on what's linked to it.
        service.refresh_priorities(db, [root])


def _explained_incidents(db: Session, view: CorridorView, d: Diagnosis, root: Incident) -> list[Incident]:
    if not d.explains:
        return []
    candidates = (
        db.query(Incident)
        .filter(
            Incident.router_id.in_({dev for dev, _ in d.explains}),
            Incident.status == IncidentStatus.OPEN,
            Incident.id != root.id,
            Incident.corridor_id.is_(None),
        )
        .all()
    )
    return [i for i in candidates if (i.router_id, i.interface_name) in d.explains]


# --- driving it ------------------------------------------------------------------

_last_seen: dict[int, int] = {}  # corridor id -> newest trap id evaluated


def evaluate(db: Session, corridor: Corridor, now: datetime, force: bool = False) -> list[Diagnosis] | None:
    """Diagnoses one corridor and acts on it, if anything changed since the
    last time and its optical alarms have been quiet for RCA_HOLD_SECONDS
    (or `force`). Returns the diagnoses, or None if it was skipped."""
    view = load_view(db, corridor)
    ids = view.device_ids
    if not ids:
        return None
    newest = db.query(func.max(TrapEvent.id)).filter(TrapEvent.router_id.in_(ids)).scalar() or 0
    if not force:
        if newest == _last_seen.get(corridor.id):
            return None
        last_optical = (
            db.query(func.max(TrapEvent.received_at))
            .filter(TrapEvent.router_id.in_(ids), TrapEvent.oid.in_((OPTICAL_ALARM_OID, OPTICAL_ALARM_CLEAR_OID)))
            .scalar()
        )
        if last_optical is not None:
            if last_optical.tzinfo is None:
                last_optical = last_optical.replace(tzinfo=now.tzinfo)
            if now - last_optical < timedelta(seconds=RCA_HOLD_SECONDS):
                return None
    diagnoses = analyze(view, active_conditions(db, ids, now))
    apply(db, view, diagnoses, now)
    _last_seen[corridor.id] = newest
    return diagnoses


def evaluate_all(db: Session, now: datetime) -> None:
    for corridor in db.query(Corridor).all():
        evaluate(db, corridor, now)


def diagnose(db: Session, corridor: Corridor, now: datetime) -> list[Diagnosis]:
    """Read-only: what the engine concludes from the alarms active right now."""
    view = load_view(db, corridor)
    return analyze(view, active_conditions(db, view.device_ids, now))
