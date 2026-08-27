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
TRAP_HOST = os.getenv("TRAP_HOST", "backend")
TRAP_PORT = int(os.getenv("TRAP_PORT", "1162"))
COMMUNITY = os.getenv("TRAP_COMMUNITY", "public")
TICK_MIN_SECONDS = float(os.getenv("TICK_MIN_SECONDS", "3"))
TICK_MAX_SECONDS = float(os.getenv("TICK_MAX_SECONDS", "8"))
FLAPPING_ROUTER_COUNT = int(os.getenv("FLAPPING_ROUTER_COUNT", "5"))
DOWN_ROUTER_COUNT = int(os.getenv("DOWN_ROUTER_COUNT", "3"))
# How many specific BGP-peering bundles get a chronically-flapping member -
# mirrors FLAPPING_ROUTER_COUNT's "pick a few problem children up front"
# pattern, but at the IS-IS/bundle-member layer (see bundle_flap_loop).
FLAPPING_BUNDLE_COUNT = int(os.getenv("FLAPPING_BUNDLE_COUNT", "3"))
CUSTOMER_TICK_MIN_SECONDS = float(os.getenv("CUSTOMER_TICK_MIN_SECONDS", "20"))
CUSTOMER_TICK_MAX_SECONDS = float(os.getenv("CUSTOMER_TICK_MAX_SECONDS", "45"))
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

# Optimistic default so traps flow immediately on startup, before the first
# poll lands; simulation_control_loop corrects this from the backend's
# actual flag within SIMULATION_POLL_SECONDS.
_simulation_enabled = True

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
            resp = requests.post(url, json=payload, timeout=120)
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
    global _simulation_enabled
    while True:
        try:
            resp = await asyncio.to_thread(requests.get, f"{BACKEND_API_URL}/api/simulation/status", timeout=5)
            resp.raise_for_status()
            enabled = resp.json()["enabled"]
            if enabled != _simulation_enabled:
                logger.info("Simulation %s", "resumed" if enabled else "paused")
            _simulation_enabled = enabled
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
        for _ in range(random.randint(4, 6)):
            await send_trap(engine, target, LINK_DOWN_OID, router, interface)
            await asyncio.sleep(random.uniform(1, 3))
            await send_trap(engine, target, LINK_UP_OID, router, interface)
            await asyncio.sleep(random.uniform(1, 3))
        # quiet period between flap bursts
        await asyncio.sleep(random.uniform(60, 180))


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
        for _ in range(random.randint(4, 6)):
            await send_trap(engine, target, ISIS_ADJACENCY_DOWN_OID, router, interface=member, bgp_peer_ip=peer_ip)
            await asyncio.sleep(random.uniform(1, 3))
            await send_trap(engine, target, ISIS_ADJACENCY_UP_OID, router, interface=member, bgp_peer_ip=peer_ip)
            await asyncio.sleep(random.uniform(1, 3))
        # quiet period between flap bursts
        await asyncio.sleep(random.uniform(60, 180))


async def down_router_loop(engine, target, router: dict, interfaces: list[str]) -> None:
    interface = random.choice(interfaces) if interfaces else random.choice(INTERFACES)
    while True:
        await send_trap(engine, target, LINK_DOWN_OID, router, interface)
        await asyncio.sleep(random.uniform(120, 300))
        await send_trap(engine, target, LINK_UP_OID, router, interface)
        await asyncio.sleep(random.uniform(300, 600))


async def random_event_loop(
    engine,
    target,
    routers: list[dict],
    peer_adjacency: dict[str, list[str]],
    primary_interfaces: dict[str, list[str]],
    bgp_bundles: dict[str, dict[str, dict]],
) -> None:
    while True:
        await asyncio.sleep(random.uniform(TICK_MIN_SECONDS, TICK_MAX_SECONDS))
        router = random.choice(routers)
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


async def scheduled_bgp_incident_loop(
    engine,
    target,
    routers: list[dict],
    peer_adjacency: dict[str, list[str]],
    bgp_bundles: dict[str, dict[str, dict]],
) -> None:
    """Forces a primary-to-primary BGP incident at a fixed cadence (default
    every 10 minutes), independent of random_event_loop's dice rolls - so
    there's always a guaranteed, bounded-wait way to see a peering fail and
    the map's reroute-path animation kick in, rather than depending on
    chance. Fires once immediately on startup, then every interval after.
    This is a purely logical/BGP-level flap (session reset), independent of
    the bundle's own physical redundancy - see scheduled_bundle_failure_loop
    for the "transport actually gone" scenario."""
    while True:
        candidates = [r for r in routers if peer_adjacency.get(r["mgmt_ip"])]
        if candidates:
            router = random.choice(candidates)
            peer_ip = random.choice(peer_adjacency[router["mgmt_ip"]])
            iface = bgp_bundles.get(router["mgmt_ip"], {}).get(peer_ip, {}).get("bundle")
            logger.info("Forcing scheduled BGP incident: %s <-> %s", router["mgmt_ip"], peer_ip)
            await send_trap(engine, target, BGP_BACKWARD_TRANSITION_OID, router, interface=iface, bgp_peer_ip=peer_ip)
            await asyncio.sleep(random.uniform(10, 30))
            await send_trap(engine, target, BGP_ESTABLISHED_OID, router, interface=iface, bgp_peer_ip=peer_ip)
        await asyncio.sleep(BGP_INCIDENT_INTERVAL_SECONDS)


async def scheduled_bundle_failure_loop(
    engine,
    target,
    routers: list[dict],
    peer_adjacency: dict[str, list[str]],
    bgp_bundles: dict[str, dict[str, dict]],
) -> None:
    """Forces a genuine redundancy-exhausting bundle failure at a fixed
    cadence (default every 15 minutes): every member of a random bundle goes
    down in turn (not just one), so the backend's true-redundancy rollup
    (app.bundles) actually flips the bundle - and the BGP peering it
    carries - to down, unlike the routine single-member blips in
    random_event_loop. Demonstrates that redundancy holds for a single
    member but not when it's genuinely exhausted. Fires once on startup,
    then every interval after."""
    while True:
        candidates = [r for r in routers if peer_adjacency.get(r["mgmt_ip"])]
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
        await asyncio.sleep(BUNDLE_FAILURE_INTERVAL_SECONDS)


# A customer CPE is single-homed: it only ever has the one uplink port.
CUSTOMER_UPLINK_INTERFACE = "GigabitEthernet0/0"


async def customer_event_loop(engine, target, customers: list[dict]) -> None:
    """Same trap variety as random_event_loop for primaries, minus BGP and
    the protocols that ride on the BGP mesh (BFD, IS-IS) - a customer CPE is
    single-homed to one primary with no other peer, so there's no adjacency
    for those three to protect. Slower cadence than the primary loop (see
    CUSTOMER_TICK_MIN/MAX_SECONDS) since this is last-mile noise, not
    backbone-grade telemetry - and the uplink link-down/up blip stays the
    dominant event, matching a CPE's single physical port."""
    while True:
        await asyncio.sleep(random.uniform(CUSTOMER_TICK_MIN_SECONDS, CUSTOMER_TICK_MAX_SECONDS))
        customer = random.choice(customers)
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


async def main() -> None:
    routers = load_primary_routers()
    seed_routers(routers)

    # Customers reference their primary by mgmt_ip, so they must be seeded
    # after the primaries already exist in the backend.
    customers = load_customer_routers()
    seed_routers(customers)

    bgp_pairs = load_bgp_topology()
    seed_bgp_topology(bgp_pairs)
    peer_adjacency = build_peer_adjacency(bgp_pairs)
    bgp_bundles = build_bgp_bundles(peer_adjacency)
    primary_interfaces = build_customer_facing_interfaces(routers, customers)

    engine = SnmpEngine()
    target = UdpTransportTarget((TRAP_HOST, TRAP_PORT))

    await boot_sequence(engine, target, routers, delay=0.05)
    await boot_sequence(engine, target, customers, delay=0.01)

    pool = routers.copy()
    random.shuffle(pool)
    flapping_routers = pool[:FLAPPING_ROUTER_COUNT]
    down_routers = pool[FLAPPING_ROUTER_COUNT : FLAPPING_ROUTER_COUNT + DOWN_ROUTER_COUNT]

    routers_by_ip = {r["mgmt_ip"]: r for r in routers}
    all_bundle_links = [
        (routers_by_ip[router_ip], peer_ip, bundle_info)
        for router_ip, peers in bgp_bundles.items()
        for peer_ip, bundle_info in peers.items()
    ]
    random.shuffle(all_bundle_links)
    flapping_bundles = all_bundle_links[:FLAPPING_BUNDLE_COUNT]

    logger.info(
        "Simulating %d chronically-flapping routers, %d intermittently-down routers, and "
        "%d chronically-flapping bundle members, plus quiet last-mile noise across %d customer CPE routers",
        len(flapping_routers),
        len(down_routers),
        len(flapping_bundles),
        len(customers),
    )

    tasks = [
        simulation_control_loop(),
        random_event_loop(engine, target, routers, peer_adjacency, primary_interfaces, bgp_bundles),
        customer_event_loop(engine, target, customers),
        scheduled_bgp_incident_loop(engine, target, routers, peer_adjacency, bgp_bundles),
        scheduled_bundle_failure_loop(engine, target, routers, peer_adjacency, bgp_bundles),
    ]
    tasks += [flap_loop(engine, target, r, primary_interfaces.get(r["mgmt_ip"], [])) for r in flapping_routers]
    tasks += [down_router_loop(engine, target, r, primary_interfaces.get(r["mgmt_ip"], [])) for r in down_routers]
    tasks += [bundle_flap_loop(engine, target, router, peer_ip, bundle_info) for router, peer_ip, bundle_info in flapping_bundles]

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
