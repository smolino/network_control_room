"""Shortest-path reroute computation over the BGP mesh - used whenever a
peering is down, to show which path traffic between those two routers
would actually take over the surviving established sessions. Kept as its
own module (rather than living in api/bgp.py) since both the API and the
trap classifier need it, and api -> snmp is not an import direction we
want to introduce."""

from collections import deque

from sqlalchemy.orm import Session

from app.models import BgpPeering, BgpSessionStatus


def build_established_adjacency(db: Session) -> dict[int, list[int]]:
    adjacency: dict[int, list[int]] = {}
    peerings = db.query(BgpPeering).filter(BgpPeering.status == BgpSessionStatus.ESTABLISHED).all()
    for p in peerings:
        adjacency.setdefault(p.router_a_id, []).append(p.router_b_id)
        adjacency.setdefault(p.router_b_id, []).append(p.router_a_id)
    return adjacency


def shortest_reroute_path(adjacency: dict[int, list[int]], start: int, end: int) -> list[int] | None:
    """BFS shortest path from `start` to `end` using only established
    peerings - i.e. the path traffic would actually take once the direct
    session between them is down. Returns None if the mesh has been
    partitioned and no such path exists."""
    if start not in adjacency or end not in adjacency:
        return None

    visited = {start}
    queue = deque([[start]])
    while queue:
        path = queue.popleft()
        node = path[-1]
        for neighbor in adjacency.get(node, []):
            if neighbor == end:
                return path + [neighbor]
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(path + [neighbor])
    return None
