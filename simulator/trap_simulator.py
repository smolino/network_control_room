"""Seeds a two-tier test fleet into the backend and then continuously sends
real SNMP traps at the backend's trap listener.

- 400 "primary" (backbone) routers, each BGP-peered with ~3 others for
  redundancy. These get the rich event mix: boot-time coldStarts, a
  handful of chronically-flapping interfaces, a handful of routers that
  sit down for a while, and a long tail of occasional link/BGP/CPU/
  env/config events.
- 4000 "customer" CPE routers (10 per primary, single-homed to it, not
  part of the BGP mesh). These just get a boot coldStart and a much
  quieter trickle of occasional link blips - realistic last-mile noise
  without drowning out the backbone signal.
"""

import asyncio
import json
import logging
import os
import random
import time

import requests
from pysnmp.hlapi.asyncio import (
    CommunityData,
    ContextData,
    NotificationType,
    ObjectIdentity,
    ObjectType,
    OctetString,
    SnmpEngine,
    UdpTransportTarget,
    sendNotification,
)

from oids import (
    AUTH_FAILURE_OID,
    BFD_SESSION_DOWN_OID,
    BGP_BACKWARD_TRANSITION_OID,
    BGP_ESTABLISHED_OID,
    BGP_PEER_OID,
    CISCO_CONFIG_MAN_OID,
    CISCO_CPU_RISING_OID,
    CISCO_ENV_FAN_OID,
    CISCO_ENV_SUPPLY_OID,
    CISCO_ENV_TEMP_OID,
    CISCO_MEMORY_LOW_OID,
    CISCO_OPTICAL_RX_POWER_OID,
    COLD_START_OID,
    IF_NAME_OID,
    INTERFACES,
    ISIS_ADJACENCY_DOWN_OID,
    ISIS_ADJACENCY_UP_OID,
    LINK_DOWN_OID,
    LINK_UP_OID,
    ROUTER_ID_OID,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("trap-simulator")

BACKEND_API_URL = os.getenv("BACKEND_API_URL", "http://backend:8000")
# Bearer token for the backend's admin-only fleet seed endpoints - must match
# the backend's own SERVICE_API_TOKEN.
SERVICE_API_TOKEN = os.getenv("SERVICE_API_TOKEN", "")
TRAP_HOST = os.getenv("TRAP_HOST", "backend")
TRAP_PORT = int(os.getenv("TRAP_PORT", "1162"))
COMMUNITY = os.getenv("TRAP_COMMUNITY", "public")
TICK_MIN_SECONDS = float(os.getenv("TICK_MIN_SECONDS", "20"))
TICK_MAX_SECONDS = float(os.getenv("TICK_MAX_SECONDS", "40"))
FLAPPING_ROUTER_COUNT = int(os.getenv("FLAPPING_ROUTER_COUNT", "2"))
DOWN_ROUTER_COUNT = int(os.getenv("DOWN_ROUTER_COUNT", "1"))
# How many specific BGP-peering bundles get a chronically-flapping member -
# mirrors FLAPPING_ROUTER_COUNT's "pick a few problem children up front"
# pattern, but at the IS-IS/bundle-member layer (see bundle_flap_loop).
FLAPPING_BUNDLE_COUNT = int(os.getenv("FLAPPING_BUNDLE_COUNT", "1"))
# Quiet period between flap bursts in flap_loop / bundle_flap_loop.
FLAP_QUIET_MIN_SECONDS = float(os.getenv("FLAP_QUIET_MIN_SECONDS", "300"))
FLAP_QUIET_MAX_SECONDS = float(os.getenv("FLAP_QUIET_MAX_SECONDS", "600"))
CUSTOMER_TICK_MIN_SECONDS = float(os.getenv("CUSTOMER_TICK_MIN_SECONDS", "60"))
CUSTOMER_TICK_MAX_SECONDS = float(os.getenv("CUSTOMER_TICK_MAX_SECONDS", "120"))
# Scale the random event streams with fleet size: the average time between
# random events for any *one* router. The TICK_* ranges above fix the total
# rate regardless of fleet size, so after a bulk upload to thousands of
# routers each one would otherwise wait hours (primaries) or days
# (customers) for its first event. The tick shrinks to interval/fleet_size
# once that's shorter than the TICK_* range; a small fleet keeps the TICK_*
# pace. 0 turns scaling off.
PRIMARY_EVENT_INTERVAL_SECONDS = float(os.getenv("PRIMARY_EVENT_INTERVAL_SECONDS", "7200"))
CUSTOMER_EVENT_INTERVAL_SECONDS = float(os.getenv("CUSTOMER_EVENT_INTERVAL_SECONDS", "172800"))
# Guarantees a primary-to-primary BGP incident at a fixed cadence, on top of
# whatever random_event_loop's dice rolls happen to produce - so the
# reroute-path feature always has something to demo within a bounded wait
# instead of depending on chance.
BGP_INCIDENT_INTERVAL_SECONDS = float(os.getenv("BGP_INCIDENT_INTERVAL_SECONDS", "600"))
# Same "guaranteed, bounded-wait" idea as BGP_INCIDENT_INTERVAL_SECONDS,
# but for a genuine redundancy-exhausting bundle failure (every member down,
# not just one) - see scheduled_bundle_failure_loop.
BUNDLE_FAILURE_INTERVAL_SECONDS = float(os.getenv("BUNDLE_FAILURE_INTERVAL_SECONDS", "900"))
# How often to poll the backend's simulation on/off flag (toggled from the
# frontend's Settings > Simulation tab) - see simulation_control_loop.
SIMULATION_POLL_SECONDS = float(os.getenv("SIMULATION_POLL_SECONDS", "3"))
# How often to re-read the fleet (routers, BGP links and their bundles) from
# the backend, so routers added from the UI or by bulk upload start getting
# traps too - see Fleet / fleet_refresh_loop.
FLEET_REFRESH_SECONDS = float(os.getenv("FLEET_REFRESH_SECONDS", "120"))

# Optimistic default so traps flow immediately on startup, before the first
# poll lands; simulation_control_loop corrects this from the backend's
# actual flag within SIMULATION_POLL_SECONDS.
_simulation_enabled = True
# Settings > Simulation's L3 slider: multiplies how often every event loop
# below fires (1 = default pace, 0 = no events) - see rated_sleep. Also
# polled by simulation_control_loop.
_l3_rate = 1.0

# Bundle (Port-channel) members per BGP peering link - see
# backend/app/bundles.py, which independently derives the identical
# Port-channel{i+1}/FortyGigE0/0/{2i,2i+1} names from the same mgmt_ip-sorted
# peer ordering.
BUNDLE_MEMBER_COUNT = 2

SEED_FILE = os.path.join(os.path.dirname(__file__), "routers_seed.json")
CUSTOMER_SEED_FILE = os.path.join(os.path.dirname(__file__), "customer_routers_seed.json")
BGP_TOPOLOGY_FILE = os.path.join(os.path.dirname(__file__), "bgp_topology.json")


def load_primary_routers() -> list[dict]:
    with open(SEED_FILE) as f:
        return json.load(f)


def load_customer_routers() -> list[dict]:
    with open(CUSTOMER_SEED_FILE) as f:
        return json.load(f)


def load_bgp_topology() -> list[dict]:
    with open(BGP_TOPOLOGY_FILE) as f:
        return json.load(f)


def build_peer_adjacency(pairs: list[dict]) -> dict[str, list[str]]:
    adjacency: dict[str, list[str]] = {}
    for pair in pairs:
        a, b = pair["router_a_mgmt_ip"], pair["router_b_mgmt_ip"]
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)
    return adjacency


def build_bgp_bundles(peer_adjacency: dict[str, list[str]]) -> dict[str, dict[str, dict]]:
    """router_mgmt_ip -> {peer_mgmt_ip: {"bundle": name, "members": [names]}}.
    Peers are numbered in mgmt_ip sort order, matching how the backend
    independently derives the same Port-channel/FortyGigE names when it
    seeds a peering and builds a config backup (see backend/app/bundles.py,
    backend/app/remediation/engine.py) - so a trap naming a bundle or member
    interface and the backend's own bookkeeping always agree on what it's
    for. Each peer gets a 2-member Port-channel instead of one dedicated
    link, so a single member losing IS-IS adjacency doesn't take the
    peering down."""
    result: dict[str, dict[str, dict]] = {}
    for router_ip, peers in peer_adjacency.items():
        per_peer: dict[str, dict] = {}
        for i, peer_ip in enumerate(sorted(peers)):
            base = i * BUNDLE_MEMBER_COUNT
            members = [f"FortyGigE0/0/{base + m}" for m in range(BUNDLE_MEMBER_COUNT)]
            per_peer[peer_ip] = {"bundle": f"Port-channel{i + 1}", "members": members}
        result[router_ip] = per_peer
    return result


def customer_interface(customer: dict) -> str:
    """The primary-side interface dedicated to this customer, derived from
    the customer's own last IP octet (10.20.{primary_idx}.{c_idx}) - no
    extra bookkeeping needed, and it matches the backend's numbering."""
    return f"GigabitEthernet0/{int(customer['mgmt_ip'].rsplit('.', 1)[-1])}"


def build_customer_facing_interfaces(routers: list[dict], customers: list[dict]) -> dict[str, list[str]]:
    """primary_mgmt_ip -> list of its customer-facing interface names only
    (one per customer, single-homed/unprotected). Deliberately excludes BGP
    bundle member interfaces: those are protected by IS-IS/bundle redundancy
    (see build_bgp_bundles) and get their own adjacency event stream instead
    of the generic linkDown/linkUp used here, which still directly flips
    router.status - a single bundle member should no longer be able to do
    that now that redundancy exists."""
    customers_by_parent: dict[str, list[dict]] = {}
    for c in customers:
        customers_by_parent.setdefault(c["parent_mgmt_ip"], []).append(c)

    return {
        r["mgmt_ip"]: [customer_interface(c) for c in customers_by_parent.get(r["mgmt_ip"], [])] for r in routers
    }


class Fleet:
    """What the event loops pick routers/links from. Starts as the seed
    files' fleet, then mirrors the backend's actual fleet (see
    fetch_fleet_from_backend) so everything on the map is simulated - not
    just the routers this container seeded itself. Updated in place, so
    long-running loops always see the latest version."""

    def __init__(self, primaries: list[dict], customers: list[dict], peer_adjacency: dict, bgp_bundles: dict) -> None:
        self.update(primaries, customers, peer_adjacency, bgp_bundles)

    def update(self, primaries: list[dict], customers: list[dict], peer_adjacency: dict, bgp_bundles: dict) -> None:
        self.primaries = primaries
        self.customers = customers
        self.peer_adjacency = peer_adjacency
        self.bgp_bundles = bgp_bundles
        self.primary_interfaces = build_customer_facing_interfaces(primaries, customers)


def _get_with_auth(path: str):
    resp = requests.get(
        f"{BACKEND_API_URL}{path}", timeout=60, headers={"Authorization": f"Bearer {SERVICE_API_TOKEN}"}
    )
    resp.raise_for_status()
    return resp.json()


def fetch_fleet_from_backend() -> tuple[list[dict], list[dict], dict, dict]:
    """(primaries, customers, peer_adjacency, bgp_bundles) as the backend
    has them right now. Bundle/member names come from the backend's own
    InterfaceBundle rows rather than being re-derived here, so an IS-IS
    trap always names an interface the backend (and its topology graph)
    knows belongs to that exact peering - links added after the initial
    seed don't follow the sorted-peer numbering (see backend/app/bundles.py)."""
    routers = _get_with_auth("/api/routers")
    peerings = _get_with_auth("/api/bgp/peerings")
    by_id = {r["id"]: r for r in routers}

    def slim(r: dict) -> dict:
        return {"hostname": r["hostname"], "mgmt_ip": r["mgmt_ip"]}

    primaries = [slim(r) for r in routers if r["router_type"] == "primary"]
    customers = [
        {**slim(r), "parent_mgmt_ip": by_id[r["parent_router_id"]]["mgmt_ip"]}
        for r in routers
        if r["router_type"] == "customer" and r.get("parent_router_id") in by_id
    ]
    peer_adjacency: dict[str, list[str]] = {}
    bgp_bundles: dict[str, dict[str, dict]] = {}
    for p in peerings:
        a, b = by_id.get(p["router_a_id"]), by_id.get(p["router_b_id"])
        if a is None or b is None:
            continue
        peer_adjacency.setdefault(a["mgmt_ip"], []).append(b["mgmt_ip"])
        peer_adjacency.setdefault(b["mgmt_ip"], []).append(a["mgmt_ip"])
        for side, peer, bundle in ((a, b, p.get("bundle_a")), (b, a, p.get("bundle_b"))):
            if bundle:
                bgp_bundles.setdefault(side["mgmt_ip"], {})[peer["mgmt_ip"]] = {
                    "bundle": bundle["name"],
                    "members": [m["name"] for m in bundle["members"]],
                }
    return primaries, customers, peer_adjacency, bgp_bundles


async def fleet_refresh_loop(fleet: Fleet) -> None:
    while True:
        await asyncio.sleep(FLEET_REFRESH_SECONDS)
        try:
            before = (len(fleet.primaries), len(fleet.customers))
            fleet.update(*await asyncio.to_thread(fetch_fleet_from_backend))
            if (len(fleet.primaries), len(fleet.customers)) != before:
                logger.info("Fleet changed: now simulating %d primaries and %d customers", len(fleet.primaries), len(fleet.customers))
        except (requests.RequestException, KeyError) as exc:
            logger.warning("Couldn't refresh the fleet from the backend, keeping the previous one: %s", exc)


def _seed_with_retry(url: str, payload: list[dict], what: str) -> None:
    # 120s, not the old 10s: a bulk seed (up to 4000 routers, or 600 BGP
    # peerings each writing an interface bundle + a Neo4j sync) can
    # legitimately take well over 10s on a cold-starting stack, especially
    # with the dashboard already polling the same backend. A too-tight
    # client timeout doesn't just log a false "not ready" - it fires a
    # duplicate concurrent POST at a request that's still running server
    # side, which is what actually produced the duplicate-key 500s below.
    for attempt in range(1, 31):
        try:
            resp = requests.post(
                url, json=payload, timeout=120, headers={"Authorization": f"Bearer {SERVICE_API_TOKEN}"}
            )
            resp.raise_for_status()
            logger.info("Seeded %d %s into the backend", len(resp.json()), what)
            return
        except requests.RequestException as exc:
            logger.info("Backend not ready yet for %s (attempt %d/30): %s", what, attempt, exc)
            time.sleep(2)
    raise RuntimeError(f"Backend never became ready to accept the {what} seed")


def seed_routers(routers: list[dict]) -> None:
    _seed_with_retry(f"{BACKEND_API_URL}/api/routers/seed", routers, "routers")


def seed_bgp_topology(pairs: list[dict]) -> None:
    _seed_with_retry(f"{BACKEND_API_URL}/api/bgp/seed", pairs, "BGP peerings")


async def simulation_control_loop() -> None:
    """Polls the backend's simulation on/off flag and updates
    _simulation_enabled, which send_trap checks before sending anything.
    Gating at that single choke point pauses/resumes every event loop
    (random, scheduled, chronically-flapping) uniformly, without needing to
    cancel or restart any of their asyncio tasks. Uses requests (already a
    dependency for seeding) off the event loop via to_thread so a slow poll
    can't stall trap sending."""
    global _simulation_enabled, _l3_rate
    while True:
        try:
            resp = await asyncio.to_thread(requests.get, f"{BACKEND_API_URL}/api/simulation/status", timeout=5)
            resp.raise_for_status()
            status = resp.json()
            enabled = status["enabled"]
            if enabled != _simulation_enabled:
                logger.info("Simulation %s", "resumed" if enabled else "paused")
            _simulation_enabled = enabled
            l3_rate = float(status.get("l3_rate", 1.0))
            if l3_rate != _l3_rate:
                logger.info("L3 event rate set to x%g", l3_rate)
            _l3_rate = l3_rate
        except requests.RequestException as exc:
            logger.warning("Couldn't reach backend for simulation status: %s", exc)
        await asyncio.sleep(SIMULATION_POLL_SECONDS)


async def send_trap(
    engine: SnmpEngine,
    target: UdpTransportTarget,
    trap_oid: str,
    router: dict,
    interface: str | None = None,
    bgp_peer_ip: str | None = None,
) -> None:
    if not _simulation_enabled:
        return

    varbinds = [ObjectType(ObjectIdentity(ROUTER_ID_OID), OctetString(router["mgmt_ip"]))]
    if interface:
        varbinds.append(ObjectType(ObjectIdentity(IF_NAME_OID), OctetString(interface)))
    if bgp_peer_ip:
        varbinds.append(ObjectType(ObjectIdentity(BGP_PEER_OID), OctetString(bgp_peer_ip)))

    try:
        error_indication, error_status, _, _ = await sendNotification(
            engine,
            CommunityData(COMMUNITY, mpModel=1),
            target,
            ContextData(),
            "trap",
            NotificationType(ObjectIdentity(trap_oid)).addVarBinds(*varbinds),
            lookupMib=False,
        )
        if error_indication:
            logger.warning("Trap send error to %s: %s", router["hostname"], error_indication)
        elif error_status:
            logger.warning("Trap send error status from %s: %s", router["hostname"], error_status)
    except Exception:
        logger.exception("Failed sending trap %s for %s", trap_oid, router["hostname"])


async def flap_loop(engine, target, router: dict, interfaces: list[str]) -> None:
    interface = random.choice(interfaces) if interfaces else random.choice(INTERFACES)
    while True:
        await wait_while_l3_off()
        for _ in range(random.randint(4, 6)):
            await send_trap(engine, target, LINK_DOWN_OID, router, interface)
            await asyncio.sleep(random.uniform(1, 3))
            await send_trap(engine, target, LINK_UP_OID, router, interface)
            await asyncio.sleep(random.uniform(1, 3))
        # quiet period between flap bursts
        await rated_sleep(random.uniform(FLAP_QUIET_MIN_SECONDS, FLAP_QUIET_MAX_SECONDS))


async def bundle_flap_loop(engine, target, router: dict, peer_ip: str, bundle_info: dict) -> None:
    """Chronically flaps one fixed member of one specific bundle - same
    burst/quiet cadence as flap_loop, but at the IS-IS-adjacency/bundle-
    member layer instead of a plain physical link. Redundancy always
    absorbs it (the other member keeps the bundle and peering up), so this
    is a distinct, persistently-demoable "degraded but redundant" scenario
    from both random_event_loop's uniformly-random single-tick blips and
    scheduled_bundle_failure_loop's full (both-members) outage."""
    members = bundle_info.get("members") or []
    if not members:
        return
    member = random.choice(members)
    while True:
        await wait_while_l3_off()
        for _ in range(random.randint(4, 6)):
            await send_trap(engine, target, ISIS_ADJACENCY_DOWN_OID, router, interface=member, bgp_peer_ip=peer_ip)
            await asyncio.sleep(random.uniform(1, 3))
            await send_trap(engine, target, ISIS_ADJACENCY_UP_OID, router, interface=member, bgp_peer_ip=peer_ip)
            await asyncio.sleep(random.uniform(1, 3))
        # quiet period between flap bursts
        await rated_sleep(random.uniform(FLAP_QUIET_MIN_SECONDS, FLAP_QUIET_MAX_SECONDS))


async def down_router_loop(engine, target, router: dict, interfaces: list[str]) -> None:
    interface = random.choice(interfaces) if interfaces else random.choice(INTERFACES)
    while True:
        await wait_while_l3_off()
        await send_trap(engine, target, LINK_DOWN_OID, router, interface)
        await asyncio.sleep(random.uniform(120, 300))
        await send_trap(engine, target, LINK_UP_OID, router, interface)
        await rated_sleep(random.uniform(300, 600))


async def rated_sleep(seconds: float) -> None:
    """Sleeps `seconds` of default-pace time, sped up or slowed down by the
    L3 rate - re-read every SIMULATION_POLL_SECONDS, so moving the slider
    applies to waits already in progress. At rate 0 it never returns while
    the rate stays 0, which is what stops the loop's events."""
    remaining = seconds
    while remaining > 0:
        step = SIMULATION_POLL_SECONDS
        if _l3_rate > 0 and remaining / _l3_rate < step:
            step = remaining / _l3_rate
        await asyncio.sleep(step)
        remaining -= step * _l3_rate


async def wait_while_l3_off() -> None:
    """Holds a loop whose first event fires straight away on startup (the
    chronic and scheduled ones) while the L3 rate is 0."""
    while _l3_rate <= 0:
        await asyncio.sleep(SIMULATION_POLL_SECONDS)


def event_tick(tick_min: float, tick_max: float, per_router_interval: float, fleet_size: int) -> float:
    """Seconds until the next random event - see PRIMARY_EVENT_INTERVAL_SECONDS."""
    tick = random.uniform(tick_min, tick_max)
    if per_router_interval <= 0 or fleet_size == 0:
        return tick
    scaled = per_router_interval / fleet_size
    return random.uniform(0.5 * scaled, 1.5 * scaled) if scaled < tick else tick


# Strong references to in-flight events - asyncio only keeps weak ones, so an
# unreferenced task can be garbage-collected mid-way through.
_running_events: set[asyncio.Task] = set()


def spawn(coro) -> None:
    """Runs one event (e.g. a linkDown, a wait, then the linkUp) without
    blocking the loop that fired it, so the next event isn't delayed by this
    one's own down/up wait."""
    task = asyncio.create_task(coro)
    _running_events.add(task)
    task.add_done_callback(_running_events.discard)


async def random_event_loop(engine, target, fleet: Fleet) -> None:
    while True:
        await rated_sleep(
            event_tick(TICK_MIN_SECONDS, TICK_MAX_SECONDS, PRIMARY_EVENT_INTERVAL_SECONDS, len(fleet.primaries))
        )
        if not fleet.primaries:
            continue
        spawn(primary_event(engine, target, fleet, random.choice(fleet.primaries)))


async def primary_event(engine, target, fleet: Fleet, router: dict) -> None:
    peer_adjacency, primary_interfaces, bgp_bundles = fleet.peer_adjacency, fleet.primary_interfaces, fleet.bgp_bundles
    roll = random.random()

    if roll < 0.08:
        await send_trap(engine, target, AUTH_FAILURE_OID, router)
    elif roll < 0.16:
        peers = peer_adjacency.get(router["mgmt_ip"], [])
        if peers:
            peer_ip = random.choice(peers)
            bundle = bgp_bundles.get(router["mgmt_ip"], {}).get(peer_ip, {})
            iface = bundle.get("bundle")
            await send_trap(engine, target, BGP_BACKWARD_TRANSITION_OID, router, interface=iface, bgp_peer_ip=peer_ip)
            await asyncio.sleep(random.uniform(5, 20))
            await send_trap(engine, target, BGP_ESTABLISHED_OID, router, interface=iface, bgp_peer_ip=peer_ip)
    elif roll < 0.22:
        await send_trap(engine, target, CISCO_CPU_RISING_OID, router)
    elif roll < 0.27:
        await send_trap(engine, target, CISCO_ENV_TEMP_OID, router)
    elif roll < 0.32:
        await send_trap(engine, target, CISCO_CONFIG_MAN_OID, router)
    elif roll < 0.38:
        # BFD rides the same backbone links as BGP (fast failure detection
        # protecting the same peering), so it reuses the BGP adjacency/
        # bundle maps rather than a separate IGP topology model. It
        # names the bundle itself (session/logical layer), not a member.
        peers = peer_adjacency.get(router["mgmt_ip"], [])
        if peers:
            peer_ip = random.choice(peers)
            bundle = bgp_bundles.get(router["mgmt_ip"], {}).get(peer_ip, {})
            iface = bundle.get("bundle")
            await send_trap(engine, target, BFD_SESSION_DOWN_OID, router, interface=iface, bgp_peer_ip=peer_ip)
    elif roll < 0.44:
        # IS-IS (the IGP) manages physical adjacency per bundle member,
        # not per bundle - a routine single-member blip like this one is
        # exactly the case bundle redundancy is meant to absorb, so it
        # never affects the peering (compare scheduled_bundle_failure_loop,
        # which takes every member down together for a real outage).
        peers = peer_adjacency.get(router["mgmt_ip"], [])
        if peers:
            peer_ip = random.choice(peers)
            bundle = bgp_bundles.get(router["mgmt_ip"], {}).get(peer_ip, {})
            members = bundle.get("members") or []
            if members:
                member = random.choice(members)
                await send_trap(engine, target, ISIS_ADJACENCY_DOWN_OID, router, interface=member, bgp_peer_ip=peer_ip)
                await asyncio.sleep(random.uniform(5, 20))
                await send_trap(engine, target, ISIS_ADJACENCY_UP_OID, router, interface=member, bgp_peer_ip=peer_ip)
    elif roll < 0.50:
        ifaces = primary_interfaces.get(router["mgmt_ip"]) or INTERFACES
        await send_trap(engine, target, CISCO_OPTICAL_RX_POWER_OID, router, interface=random.choice(ifaces))
    elif roll < 0.54:
        await send_trap(engine, target, CISCO_ENV_FAN_OID, router)
    elif roll < 0.58:
        await send_trap(engine, target, CISCO_ENV_SUPPLY_OID, router)
    elif roll < 0.63:
        await send_trap(engine, target, CISCO_MEMORY_LOW_OID, router)
    elif roll < 0.82:
        ifaces = primary_interfaces.get(router["mgmt_ip"]) or INTERFACES
        interface = random.choice(ifaces)
        await send_trap(engine, target, LINK_DOWN_OID, router, interface)
        await asyncio.sleep(random.uniform(5, 20))
        await send_trap(engine, target, LINK_UP_OID, router, interface)
    # else: quiet tick, nothing happens


async def scheduled_bgp_incident_loop(engine, target, fleet: Fleet) -> None:
    """Forces a primary-to-primary BGP incident at a fixed cadence (default
    every 10 minutes), independent of random_event_loop's dice rolls - so
    there's always a guaranteed, bounded-wait way to see a peering fail and
    the map's reroute-path animation kick in, rather than depending on
    chance. Fires once immediately on startup, then every interval after.
    This is a purely logical/BGP-level flap (session reset), independent of
    the bundle's own physical redundancy - see scheduled_bundle_failure_loop
    for the "transport actually gone" scenario."""
    while True:
        await wait_while_l3_off()
        peer_adjacency, bgp_bundles = fleet.peer_adjacency, fleet.bgp_bundles
        candidates = [r for r in fleet.primaries if peer_adjacency.get(r["mgmt_ip"])]
        if candidates:
            router = random.choice(candidates)
            peer_ip = random.choice(peer_adjacency[router["mgmt_ip"]])
            iface = bgp_bundles.get(router["mgmt_ip"], {}).get(peer_ip, {}).get("bundle")
            logger.info("Forcing scheduled BGP incident: %s <-> %s", router["mgmt_ip"], peer_ip)
            await send_trap(engine, target, BGP_BACKWARD_TRANSITION_OID, router, interface=iface, bgp_peer_ip=peer_ip)
            await asyncio.sleep(random.uniform(10, 30))
            await send_trap(engine, target, BGP_ESTABLISHED_OID, router, interface=iface, bgp_peer_ip=peer_ip)
        await rated_sleep(BGP_INCIDENT_INTERVAL_SECONDS)


async def scheduled_bundle_failure_loop(engine, target, fleet: Fleet) -> None:
    """Forces a genuine redundancy-exhausting bundle failure at a fixed
    cadence (default every 15 minutes): every member of a random bundle goes
    down in turn (not just one), so the backend's true-redundancy rollup
    (app.bundles) actually flips the bundle - and the BGP peering it
    carries - to down, unlike the routine single-member blips in
    random_event_loop. Demonstrates that redundancy holds for a single
    member but not when it's genuinely exhausted. Fires once on startup,
    then every interval after."""
    while True:
        await wait_while_l3_off()
        peer_adjacency, bgp_bundles = fleet.peer_adjacency, fleet.bgp_bundles
        candidates = [r for r in fleet.primaries if peer_adjacency.get(r["mgmt_ip"])]
        if candidates:
            router = random.choice(candidates)
            peer_ip = random.choice(peer_adjacency[router["mgmt_ip"]])
            bundle = bgp_bundles.get(router["mgmt_ip"], {}).get(peer_ip, {})
            members = bundle.get("members") or []
            if members:
                logger.info(
                    "Forcing scheduled bundle failure: %s %s (%s) <-> %s",
                    router["mgmt_ip"], bundle.get("bundle"), members, peer_ip,
                )
                for member in members:
                    await send_trap(engine, target, ISIS_ADJACENCY_DOWN_OID, router, interface=member, bgp_peer_ip=peer_ip)
                    await asyncio.sleep(random.uniform(5, 15))
                await asyncio.sleep(random.uniform(20, 60))
                for member in members:
                    await send_trap(engine, target, ISIS_ADJACENCY_UP_OID, router, interface=member, bgp_peer_ip=peer_ip)
                    await asyncio.sleep(random.uniform(5, 15))
        await rated_sleep(BUNDLE_FAILURE_INTERVAL_SECONDS)


# A customer CPE is single-homed: it only ever has the one uplink port.
CUSTOMER_UPLINK_INTERFACE = "GigabitEthernet0/0"


async def customer_event_loop(engine, target, fleet: Fleet) -> None:
    """Same trap variety as random_event_loop for primaries, minus BGP and
    the protocols that ride on the BGP mesh (BFD, IS-IS) - a customer CPE is
    single-homed to one primary with no other peer, so there's no adjacency
    for those three to protect. Slower cadence than the primary loop (see
    CUSTOMER_TICK_MIN/MAX_SECONDS) since this is last-mile noise, not
    backbone-grade telemetry - and the uplink link-down/up blip stays the
    dominant event, matching a CPE's single physical port."""
    while True:
        await rated_sleep(
            event_tick(
                CUSTOMER_TICK_MIN_SECONDS, CUSTOMER_TICK_MAX_SECONDS, CUSTOMER_EVENT_INTERVAL_SECONDS, len(fleet.customers)
            )
        )
        if not fleet.customers:
            continue
        spawn(customer_event(engine, target, random.choice(fleet.customers)))


async def customer_event(engine, target, customer: dict) -> None:
    roll = random.random()

    if roll < 0.08:
        await send_trap(engine, target, AUTH_FAILURE_OID, customer)
    elif roll < 0.14:
        await send_trap(engine, target, CISCO_CPU_RISING_OID, customer)
    elif roll < 0.19:
        await send_trap(engine, target, CISCO_ENV_TEMP_OID, customer)
    elif roll < 0.24:
        await send_trap(engine, target, CISCO_CONFIG_MAN_OID, customer)
    elif roll < 0.30:
        await send_trap(engine, target, CISCO_OPTICAL_RX_POWER_OID, customer, interface=CUSTOMER_UPLINK_INTERFACE)
    elif roll < 0.34:
        await send_trap(engine, target, CISCO_ENV_FAN_OID, customer)
    elif roll < 0.38:
        await send_trap(engine, target, CISCO_ENV_SUPPLY_OID, customer)
    elif roll < 0.43:
        await send_trap(engine, target, CISCO_MEMORY_LOW_OID, customer)
    elif roll < 0.85:
        await send_trap(engine, target, LINK_DOWN_OID, customer, CUSTOMER_UPLINK_INTERFACE)
        await asyncio.sleep(random.uniform(5, 30))
        await send_trap(engine, target, LINK_UP_OID, customer, CUSTOMER_UPLINK_INTERFACE)
    # else: quiet tick, nothing happens


async def boot_sequence(engine, target, routers: list[dict], delay: float = 0.05) -> None:
    logger.info("Sending boot-time coldStart traps for %d routers", len(routers))
    for router in routers:
        await send_trap(engine, target, COLD_START_OID, router)
        await asyncio.sleep(delay)


def read_initial_status() -> bool:
    """Reads the backend's simulation settings once before anything starts.
    Returns whether to seed the demo fleet - False once an admin has used
    Settings > Simulation > "Delete entire fleet", since from then on the
    map should only show what's been added since, not come back with the
    demo fleet on every restart. Also picks up the L3 rate up front, so a
    rate of 0 holds back even the loops that fire once on startup."""
    global _l3_rate
    try:
        resp = requests.get(f"{BACKEND_API_URL}/api/simulation/status", timeout=10)
        resp.raise_for_status()
        status = resp.json()
        _l3_rate = float(status.get("l3_rate", 1.0))
        return status.get("seed_demo_fleet", True)
    except requests.RequestException as exc:
        logger.warning("Couldn't read the simulation settings, seeding the demo fleet anyway: %s", exc)
        return True


async def main() -> None:
    if read_initial_status():
        routers = load_primary_routers()
        seed_routers(routers)

        # Customers reference their primary by mgmt_ip, so they must be seeded
        # after the primaries already exist in the backend.
        customers = load_customer_routers()
        seed_routers(customers)

        bgp_pairs = load_bgp_topology()
        seed_bgp_topology(bgp_pairs)
        peer_adjacency = build_peer_adjacency(bgp_pairs)
        fleet = Fleet(routers, customers, peer_adjacency, build_bgp_bundles(peer_adjacency))
    else:
        logger.info("Demo fleet seeding is off (fleet deleted from Settings) - simulating only the backend's fleet")
        fleet = Fleet([], [], {}, {})

    # From here on, simulate whatever the backend actually has - which
    # includes anything added from the UI or by bulk upload, not just the
    # seed files above. Falls back to the seed files' fleet if the backend
    # can't be read (e.g. SERVICE_API_TOKEN mismatch).
    try:
        fleet.update(*fetch_fleet_from_backend())
    except (requests.RequestException, KeyError) as exc:
        logger.warning("Couldn't load the fleet from the backend, simulating the seed files only: %s", exc)

    engine = SnmpEngine()
    target = UdpTransportTarget((TRAP_HOST, TRAP_PORT))

    await boot_sequence(engine, target, fleet.primaries, delay=0.05)
    await boot_sequence(engine, target, fleet.customers, delay=0.01)

    pool = fleet.primaries.copy()
    random.shuffle(pool)
    flapping_routers = pool[:FLAPPING_ROUTER_COUNT]
    down_routers = pool[FLAPPING_ROUTER_COUNT : FLAPPING_ROUTER_COUNT + DOWN_ROUTER_COUNT]

    routers_by_ip = {r["mgmt_ip"]: r for r in fleet.primaries}
    all_bundle_links = [
        (routers_by_ip[router_ip], peer_ip, bundle_info)
        for router_ip, peers in fleet.bgp_bundles.items()
        if router_ip in routers_by_ip
        for peer_ip, bundle_info in peers.items()
    ]
    random.shuffle(all_bundle_links)
    flapping_bundles = all_bundle_links[:FLAPPING_BUNDLE_COUNT]

    logger.info(
        "Simulating %d primaries and %d customers (%d chronically-flapping routers, %d intermittently-down "
        "routers, %d chronically-flapping bundle members); re-reading the fleet every %ds",
        len(fleet.primaries),
        len(fleet.customers),
        len(flapping_routers),
        len(down_routers),
        len(flapping_bundles),
        FLEET_REFRESH_SECONDS,
    )

    primary_interfaces = fleet.primary_interfaces
    tasks = [
        simulation_control_loop(),
        fleet_refresh_loop(fleet),
        random_event_loop(engine, target, fleet),
        customer_event_loop(engine, target, fleet),
        scheduled_bgp_incident_loop(engine, target, fleet),
        scheduled_bundle_failure_loop(engine, target, fleet),
    ]
    tasks += [flap_loop(engine, target, r, primary_interfaces.get(r["mgmt_ip"], [])) for r in flapping_routers]
    tasks += [down_router_loop(engine, target, r, primary_interfaces.get(r["mgmt_ip"], [])) for r in down_routers]
    tasks += [bundle_flap_loop(engine, target, router, peer_ip, bundle_info) for router, peer_ip, bundle_info in flapping_bundles]

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
