"""Builds a proximity-based BGP peering mesh over the 400 primary routers
in routers_seed.json: each primary peers with its ~3 geographically
closest primaries (for redundancy - not a full mesh), not random ones.
Writes bgp_topology.json as a flat list of {router_a_mgmt_ip,
router_b_mgmt_ip} pairs - what trap_simulator.py seeds into the backend
and picks real peers from when sending BGP traps. Customer routers
(generate_customer_routers.py) are single-homed to one primary instead
and never join this mesh.
"""

import json
import math

from land import is_land

PEERS_PER_ROUTER = 3
EARTH_RADIUS_KM = 6371.0

# All primary-to-primary links run over Single Mode Fiber (SMF). Unamplified
# long-haul SMF needs a regenerator roughly every 80km, so longer links carry
# proportionally more of them - this is a physical fact about the link, not
# something that varies by traffic or config. Doesn't apply to submarine
# cable (see is_oceanic below): those use repeaters too, but they're
# pressure-housed units spliced into the cable itself, not the same
# roadside-hut regenerators this model is simulating for terrestrial spans.
REPEATER_SPACING_KM = 80.0

# How many interior points to sample along a link's straight-line path (see
# is_oceanic) when deciding whether it's a submarine cable.
OCEAN_SAMPLE_POINTS = 20


def load_routers() -> list[dict]:
    with open("routers_seed.json") as f:
        return json.load(f)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _unwrap_lon(prev_lon: float, lon: float) -> float:
    diff = lon - prev_lon
    if diff > 180:
        return lon - 360
    if diff < -180:
        return lon + 360
    return lon


def is_oceanic(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> bool:
    """True if the straight-line path between two routers (matching how the
    map actually draws BGP links - see MapView.jsx and the README's note on
    straight-line rendering) is over open water for most of its length,
    i.e. it's a submarine cable rather than a terrestrial fiber run. A
    terrestrial link only ever crosses short water gaps (a strait, a river,
    a lake) along the way, which the majority vote below tolerates; a real
    transoceanic link is over water for nearly its entire length. Both
    router endpoints are real city sites and always on land, so only
    interior sample points are checked. Longitude is unwrapped the same
    way the map does, so a Pacific-crossing link near +-180 samples the
    true short path instead of the long way around through Africa/Europe."""
    b_lon_unwrapped = _unwrap_lon(a_lon, b_lon)
    over_water = 0
    for k in range(1, OCEAN_SAMPLE_POINTS):
        t = k / OCEAN_SAMPLE_POINTS
        lat = a_lat + (b_lat - a_lat) * t
        lon = a_lon + (b_lon_unwrapped - a_lon) * t
        lon = ((lon + 180) % 360) - 180
        if not is_land(lat, lon):
            over_water += 1
    return over_water / (OCEAN_SAMPLE_POINTS - 1) > 0.5


def build_nearest_neighbor_edges(routers: list[dict], k: int) -> tuple[set[tuple[int, int]], list[int]]:
    """Greedy minimum-distance b-matching: consider every possible pair,
    sorted nearest-first, and take a pair as soon as both endpoints still
    have spare capacity. Processing globally in distance order (rather
    than only ever looking at each node's own top-k, which can deadlock
    when popular nearby routers fill up first) means every primary ends
    up peered with routers that are close to it, while still reliably
    reaching degree k for virtually all of them."""
    n = len(routers)
    pairs_by_distance = []
    for i in range(n):
        for j in range(i + 1, n):
            dist = haversine_km(
                routers[i]["latitude"], routers[i]["longitude"],
                routers[j]["latitude"], routers[j]["longitude"],
            )
            pairs_by_distance.append((dist, i, j))
    pairs_by_distance.sort(key=lambda p: p[0])

    remaining = [k] * n
    edges: set[tuple[int, int]] = set()
    for _dist, i, j in pairs_by_distance:
        if remaining[i] > 0 and remaining[j] > 0:
            edges.add((i, j))
            remaining[i] -= 1
            remaining[j] -= 1

    return edges, remaining


def local_improve(edges: set[tuple[int, int]], routers: list[dict], max_passes: int = 2000) -> set[tuple[int, int]]:
    """2-opt style cleanup: the greedy pass above is nearest-first but not
    globally optimal - a node whose close neighbors fill up early can get
    stuck paired with another leftover node from a completely different
    part of the world (e.g. Reykjavik ending up paired with Lima instead
    of Oslo). For every pair of edges (a,b) and (c,d) with 4 distinct
    endpoints, check whether re-wiring to (a,c)+(b,d) or (a,d)+(b,c) is
    shorter overall, and apply it if so and it doesn't collide with an
    existing edge. Repeat until no improving swap is found."""

    def dist(i: int, j: int) -> float:
        return haversine_km(
            routers[i]["latitude"], routers[i]["longitude"],
            routers[j]["latitude"], routers[j]["longitude"],
        )

    edges = set(edges)
    for _ in range(max_passes):
        edge_list = sorted(edges)
        improved = False
        for idx1 in range(len(edge_list)):
            a, b = edge_list[idx1]
            if (a, b) not in edges:
                continue
            for idx2 in range(idx1 + 1, len(edge_list)):
                c, d = edge_list[idx2]
                if (c, d) not in edges or len({a, b, c, d}) < 4:
                    continue

                current = dist(a, b) + dist(c, d)
                option_a = (tuple(sorted((a, c))), tuple(sorted((b, d))))
                option_b = (tuple(sorted((a, d))), tuple(sorted((b, c))))
                best = min(
                    (dist(*option_a[0]) + dist(*option_a[1]), option_a),
                    (dist(*option_b[0]) + dist(*option_b[1]), option_b),
                    key=lambda x: x[0],
                )

                if best[0] < current - 1e-6:
                    new_e1, new_e2 = best[1]
                    if new_e1 in edges or new_e2 in edges or new_e1 == new_e2:
                        continue
                    edges.discard((a, b))
                    edges.discard((c, d))
                    edges.add(new_e1)
                    edges.add(new_e2)
                    improved = True
                    break
            if improved:
                break
        if not improved:
            break

    return edges


def find_components(edges: set[tuple[int, int]], n: int) -> list[list[int]]:
    adj: list[list[int]] = [[] for _ in range(n)]
    for a, b in edges:
        adj[a].append(b)
        adj[b].append(a)

    seen = [False] * n
    components = []
    for start in range(n):
        if seen[start]:
            continue
        seen[start] = True
        stack = [start]
        component = []
        while stack:
            u = stack.pop()
            component.append(u)
            for v in adj[u]:
                if not seen[v]:
                    seen[v] = True
                    stack.append(v)
        components.append(component)
    return components


def connect_components(edges: set[tuple[int, int]], routers: list[dict]) -> set[tuple[int, int]]:
    """Guarantees a single connected mesh: build_nearest_neighbor_edges/
    local_improve only ever optimize local edge length, so a tight regional
    cluster (e.g. 4-6 cities close together) can fully saturate each
    other's degree-3 budget before ever reaching out to the wider world,
    stranding it as its own island - degree-3-everywhere doesn't imply
    connected. For each extra island (smallest first), find the closest
    cross-component pair of edges - (a,b) inside the island, (c,d) inside
    the main network - and rewire them to (a,c)+(b,d) or (a,d)+(b,c),
    whichever is shorter: the same edge-crossing move local_improve already
    uses, just picked to merge components instead of only to shorten them.
    Degree is preserved exactly, since each of the 4 endpoints loses one
    edge and gains one."""

    def dist(i: int, j: int) -> float:
        return haversine_km(
            routers[i]["latitude"], routers[i]["longitude"],
            routers[j]["latitude"], routers[j]["longitude"],
        )

    edges = set(edges)
    while True:
        components = find_components(edges, len(routers))
        if len(components) <= 1:
            return edges
        components.sort(key=len, reverse=True)
        main = set(components[0])
        island = set(components[-1])

        best = None
        for a, b in edges:
            if a not in island or b not in island:
                continue
            for c, d in edges:
                if c not in main or d not in main:
                    continue
                for new1, new2 in (
                    (tuple(sorted((a, c))), tuple(sorted((b, d)))),
                    (tuple(sorted((a, d))), tuple(sorted((b, c)))),
                ):
                    if new1 == new2 or new1 in edges or new2 in edges:
                        continue
                    cost = dist(*new1) + dist(*new2)
                    if best is None or cost < best[0]:
                        best = (cost, (a, b), (c, d), new1, new2)

        _, old1, old2, new1, new2 = best
        edges.discard(old1)
        edges.discard(old2)
        edges.add(new1)
        edges.add(new2)


if __name__ == "__main__":
    routers = load_routers()
    n = len(routers)
    edges, remaining = build_nearest_neighbor_edges(routers, PEERS_PER_ROUTER)
    edges = local_improve(edges, routers)
    island_count_before = len(find_components(edges, n))
    edges = connect_components(edges, routers)

    edge_distances = {
        (a, b): haversine_km(
            routers[a]["latitude"], routers[a]["longitude"], routers[b]["latitude"], routers[b]["longitude"]
        )
        for a, b in edges
    }

    pairs = []
    oceanic_count = 0
    for a, b in sorted(edges):
        dist = edge_distances[(a, b)]
        oceanic = is_oceanic(
            routers[a]["latitude"], routers[a]["longitude"], routers[b]["latitude"], routers[b]["longitude"]
        )
        oceanic_count += oceanic
        pairs.append(
            {
                "router_a_mgmt_ip": routers[a]["mgmt_ip"],
                "router_b_mgmt_ip": routers[b]["mgmt_ip"],
                "distance_km": round(dist, 1),
                "repeater_count": 0 if oceanic else int(dist // REPEATER_SPACING_KM),
            }
        )

    with open("bgp_topology.json", "w") as f:
        json.dump(pairs, f, indent=2)

    degrees = [PEERS_PER_ROUTER - r for r in remaining]
    short = [routers[i]["hostname"] for i, r in enumerate(remaining) if r > 0]
    distances = list(edge_distances.values())
    print(
        f"Wrote {len(pairs)} BGP peering edges for {n} routers "
        f"(degree per router: min={min(degrees)}, max={max(degrees)}; "
        f"peer distance: median={sorted(distances)[len(distances) // 2]:.0f}km, max={max(distances):.0f}km)"
    )
    if short:
        print(f"{len(short)} router(s) below target degree: {short}")
    if island_count_before > 1:
        print(f"Bridged {island_count_before - 1} isolated island(s) back into the main mesh (now 1 connected network)")
    print(f"{oceanic_count} link(s) classified as submarine cable (no terrestrial repeaters)")
