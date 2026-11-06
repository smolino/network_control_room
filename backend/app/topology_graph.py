"""Static topology graph (Router/Interface/FiberSpan + SUPPORTED_BY) used
by app.correlation for root-cause traversal - the explicit "L3 interface X
is carried by fiber span Y" mapping the design doc calls the most important
part of the data model, and the one thing the SQL-only version of this app
never had.

Neo4j only ever holds *structure* here - mutable alarm state (which
incident is open, when) stays in Postgres (see app.models.Incident) and is
looked up there once the graph has narrowed down which peerings are even
relevant; see app.correlation.find_open_l1_root_cause.

Written once, in api/bgp.py:seed_peerings, right after the same topology is
written to Postgres.
"""

import logging

from neo4j import GraphDatabase

from app.config import settings

logger = logging.getLogger(__name__)

_driver = None


def get_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
    return _driver


def sync_peering_to_neo4j(peering, bundle_a, bundle_b) -> None:
    """Mirrors one peering + its two interface bundles into the graph -
    idempotent (MERGE throughout), safe to call every time /api/bgp/seed
    runs, same as the Postgres side it follows."""
    interfaces = [
        {"router_id": bundle.router_id, "name": member.name}
        for bundle in (bundle_a, bundle_b)
        if bundle is not None
        for member in bundle.members
    ]
    if not interfaces:
        return
    with get_driver().session() as session:
        session.run(
            """
            MERGE (a:Router {id: $router_a_id})
            MERGE (b:Router {id: $router_b_id})
            MERGE (a)-[:PEERS_WITH]-(b)
            MERGE (span:FiberSpan {peering_id: $peering_id})
            SET span.distance_km = $distance_km, span.repeater_count = $repeater_count
            WITH span
            UNWIND $interfaces AS iface
            MERGE (i:Interface {router_id: iface.router_id, name: iface.name})
            MERGE (i)-[:SUPPORTED_BY]->(span)
            """,
            router_a_id=peering.router_a_id,
            router_b_id=peering.router_b_id,
            peering_id=peering.id,
            distance_km=peering.distance_km,
            repeater_count=peering.repeater_count,
            interfaces=interfaces,
        )


def resync_peering_to_neo4j(peering, bundle_a, bundle_b) -> None:
    """Like sync_peering_to_neo4j, but first drops every SUPPORTED_BY edge
    into this peering's span - for repairing a span that had another
    peering's interfaces wrongly attached to it (see
    api/bgp.py:repair_peering_bundles)."""
    with get_driver().session() as session:
        session.run(
            "MATCH (:Interface)-[r:SUPPORTED_BY]->(:FiberSpan {peering_id: $peering_id}) DELETE r",
            peering_id=peering.id,
        )
    sync_peering_to_neo4j(peering, bundle_a, bundle_b)


def delete_peering_from_neo4j(peering_id: int) -> None:
    """Removes the FiberSpan for this peering and the Interface nodes that
    were only SUPPORTED_BY it - an interface is only ever seeded onto one
    peering's span (see sync_peering_to_neo4j), so this always removes
    exactly the pair sync_peering_to_neo4j created. Mirrors the peering
    being removed from Postgres - see api/routers.py:delete_router."""
    with get_driver().session() as session:
        session.run(
            """
            MATCH (span:FiberSpan {peering_id: $peering_id})
            OPTIONAL MATCH (i:Interface)-[:SUPPORTED_BY]->(span)
            DETACH DELETE span, i
            """,
            peering_id=peering_id,
        )


def delete_router_from_neo4j(router_id: int) -> None:
    """Removes the Router node (and any dangling PEERS_WITH edge) once
    every peering referencing it has already been removed via
    delete_peering_from_neo4j - see api/routers.py:delete_router."""
    with get_driver().session() as session:
        session.run("MATCH (r:Router {id: $router_id}) DETACH DELETE r", router_id=router_id)
        session.run("MATCH (s:Site) WHERE NOT (s)<-[:AT]-() DETACH DELETE s")


def peering_ids_for_interface(router_id: int, interface_name: str) -> list[int]:
    """The peering(s) whose fiber span this specific interface is
    SUPPORTED_BY - the precise case (§5.1's topology traversal)."""
    with get_driver().session() as session:
        result = session.run(
            """
            MATCH (i:Interface {router_id: $router_id, name: $interface_name})-[:SUPPORTED_BY]->(span:FiberSpan)
            RETURN DISTINCT span.peering_id AS peering_id
            """,
            router_id=router_id,
            interface_name=interface_name,
        )
        return [record["peering_id"] for record in result]


def peering_ids_for_router(router_id: int) -> list[int]:
    """Every peering any interface on this router is SUPPORTED_BY - the
    fallback used when an incident carries no specific interface (e.g.
    BGP_STATE_CHANGE), same breadth the old SQL-only lookup always had."""
    with get_driver().session() as session:
        result = session.run(
            """
            MATCH (i:Interface {router_id: $router_id})-[:SUPPORTED_BY]->(span:FiberSpan)
            RETURN DISTINCT span.peering_id AS peering_id
            """,
            router_id=router_id,
        )
        return [record["peering_id"] for record in result]


def interfaces_for_peering(peering_id: int) -> list[tuple[int, str]]:
    """(router_id, interface_name) for every interface SUPPORTED_BY this
    peering's fiber span - the reverse of peering_ids_for_interface, used
    when an L1 alarm opens to find L3 symptoms that got there first."""
    with get_driver().session() as session:
        result = session.run(
            """
            MATCH (i:Interface)-[:SUPPORTED_BY]->(:FiberSpan {peering_id: $peering_id})
            RETURN i.router_id AS router_id, i.name AS name
            """,
            peering_id=peering_id,
        )
        return [(record["router_id"], record["name"]) for record in result]


def clear_topology() -> None:
    """Drops the whole topology graph - "Delete entire fleet" (see
    api/simulation.py:reset_fleet). Re-populated by api/bgp.py:seed_peerings
    as links are added again."""
    with get_driver().session() as session:
        session.run(
            "MATCH (n) WHERE n:Router OR n:Interface OR n:FiberSpan OR n:Site OR n:Port OR n:Span"
            " OR n:OpticalPath OR n:Circuit DETACH DELETE n"
        )


# --- Optical corridors ---------------------------------------------------------
#
# The physical path of a modelled corridor (see app.corridors):
#
#   (:Router {id, hostname, role})-[:AT]->(:Site {code})
#   (:Router)-[:HAS]->(:Port {id, name, kind, direction, wavelength_nm})
#   (:Span {id, corridor_id, seq, provider, ...})-[:FROM]->(:Router)  A side
#   (:Span)-[:TO]->(:Router)                                          Z side
#   (:OpticalPath {id, name, kind, direction})-[:HOP {seq}]->(:Port | :Span)
#   (:Circuit {id})-[:RIDES]->(:OpticalPath)        both directions of its wavelength
#   (:Circuit)-[:END {side, role}]->(:Port)         router + transponder client ports
#   (:Interface {router_id, name})-[:IS_PORT]->(:Port)  bundle member = router port
#
# Router nodes are the same ones sync_peering_to_neo4j creates (one id
# space: routers.id) - optical elements just get a `role`.


def delete_corridor_from_neo4j(corridor_id: int) -> None:
    with get_driver().session() as session:
        session.run(
            """
            MATCH (n) WHERE (n:Span OR n:OpticalPath OR n:Circuit) AND n.corridor_id = $corridor_id
            DETACH DELETE n
            """,
            corridor_id=corridor_id,
        )
        # Ports nothing references any more (an optical element's, once its
        # corridor is gone).
        session.run("MATCH (p:Port) WHERE NOT (p)<-[:HOP]-() AND NOT (p)<-[:END]-() DETACH DELETE p")
        # Sites no element is at any more.
        session.run("MATCH (s:Site) WHERE NOT (s)<-[:AT]-() DETACH DELETE s")


def sync_corridor_to_neo4j(db, corridor_id: int) -> None:
    """Rewrites one corridor's part of the graph from Postgres - called
    after every import (idempotent: everything corridor-scoped is dropped
    and re-created, nodes shared with other links are MERGEd)."""
    from app.models import ClientCircuit, Corridor, FiberSpan, OpticalPath, Port, Router, Site  # noqa: F401

    corridor = db.get(Corridor, corridor_id)
    if corridor is None:
        return
    port_ids: set[int] = set()
    paths = []
    for p in corridor.paths:
        hops = []
        for h in p.hops:
            if h.port_id is not None:
                hops.append({"seq": h.seq, "port_id": h.port_id, "span_id": None})
                port_ids.add(h.port_id)
            else:
                hops.append({"seq": h.seq, "port_id": None, "span_id": h.span_id})
        paths.append(
            {"id": p.id, "name": p.name, "kind": p.kind, "direction": p.direction, "wavelength_nm": p.wavelength_nm, "hops": hops}
        )
    circuits = []
    for c in corridor.circuits:
        ends = [
            {"port_id": c.a_router_port_id, "side": "A", "role": "router"},
            {"port_id": c.a_client_port_id, "side": "A", "role": "client"},
            {"port_id": c.z_client_port_id, "side": "Z", "role": "client"},
            {"port_id": c.z_router_port_id, "side": "Z", "role": "router"},
        ]
        port_ids |= {e["port_id"] for e in ends}
        circuits.append({"id": c.id, "wavelength_nm": c.wavelength_nm, "peering_id": c.peering_id, "ends": ends})
    ports = [db.get(Port, pid) for pid in sorted(port_ids)]
    device_ids = {p.device_id for p in ports} | {s.from_device_id for s in corridor.spans} | {s.to_device_id for s in corridor.spans}
    devices = []
    for did in sorted(device_ids):
        d = db.get(Router, did)
        site = d.site
        devices.append(
            {
                "id": d.id,
                "hostname": d.hostname,
                "role": (d.element_role.value if d.element_role else "router"),
                "site": None if site is None else {"code": site.code, "name": site.name, "kind": site.kind},
            }
        )
    spans = [
        {
            "id": s.id, "seq": s.seq, "from_id": s.from_device_id, "to_id": s.to_device_id, "provider": s.provider,
            "a_end": s.a_end, "b_end": s.b_end, "length_km": s.length_km, "provider_circuit_id": s.provider_circuit_id,
        }
        for s in corridor.spans
    ]
    port_rows = [
        {
            "id": p.id, "device_id": p.device_id, "name": p.name, "kind": p.kind, "direction": p.direction,
            "wavelength_nm": p.wavelength_nm,
        }
        for p in ports
    ]

    delete_corridor_from_neo4j(corridor_id)
    with get_driver().session() as session:
        session.run(
            """
            UNWIND $devices AS d
            MERGE (r:Router {id: d.id})
            SET r.hostname = d.hostname, r.role = d.role
            WITH r, d
            // A device is at one site: drop the old link if it moved.
            OPTIONAL MATCH (r)-[old:AT]->(:Site)
            DELETE old
            WITH DISTINCT r, d WHERE d.site IS NOT NULL
            MERGE (s:Site {code: d.site.code})
            SET s.name = d.site.name, s.kind = d.site.kind
            MERGE (r)-[:AT]->(s)
            """,
            devices=devices,
        )
        session.run(
            """
            UNWIND $ports AS p
            MATCH (r:Router {id: p.device_id})
            MERGE (port:Port {id: p.id})
            SET port.device_id = p.device_id, port.name = p.name, port.kind = p.kind,
                port.direction = p.direction, port.wavelength_nm = p.wavelength_nm
            MERGE (r)-[:HAS]->(port)
            WITH port, p WHERE p.kind = 'router_eth'
            MERGE (i:Interface {router_id: p.device_id, name: p.name})
            MERGE (i)-[:IS_PORT]->(port)
            """,
            ports=port_rows,
        )
        session.run(
            """
            UNWIND $spans AS s
            MATCH (a:Router {id: s.from_id}), (z:Router {id: s.to_id})
            CREATE (span:Span {id: s.id, corridor_id: $corridor_id, seq: s.seq, provider: s.provider,
                               a_end: s.a_end, b_end: s.b_end, length_km: s.length_km,
                               provider_circuit_id: s.provider_circuit_id})
            CREATE (span)-[:FROM]->(a), (span)-[:TO]->(z)
            """,
            spans=spans,
            corridor_id=corridor_id,
        )
        session.run(
            """
            UNWIND $paths AS p
            CREATE (o:OpticalPath {id: p.id, corridor_id: $corridor_id, name: p.name, kind: p.kind,
                                   direction: p.direction, wavelength_nm: p.wavelength_nm})
            WITH o, p
            UNWIND p.hops AS h
            OPTIONAL MATCH (port:Port {id: h.port_id})
            OPTIONAL MATCH (span:Span {id: h.span_id})
            WITH o, h, coalesce(port, span) AS target
            CREATE (o)-[:HOP {seq: h.seq}]->(target)
            """,
            paths=paths,
            corridor_id=corridor_id,
        )
        session.run(
            """
            UNWIND $circuits AS c
            CREATE (circuit:Circuit {id: c.id, corridor_id: $corridor_id, wavelength_nm: c.wavelength_nm,
                                     peering_id: c.peering_id})
            WITH circuit, c
            UNWIND c.ends AS e
            MATCH (port:Port {id: e.port_id})
            CREATE (circuit)-[:END {side: e.side, role: e.role}]->(port)
            WITH DISTINCT circuit
            MATCH (o:OpticalPath {corridor_id: $corridor_id, kind: 'och'})
            WHERE o.wavelength_nm = circuit.wavelength_nm
            CREATE (circuit)-[:RIDES]->(o)
            """,
            circuits=circuits,
            corridor_id=corridor_id,
        )


def _carried(session, path_ids: list[int]) -> dict:
    """Everything riding the given optical paths: the circuits on their
    wavelengths and those circuits' router interfaces/L3 links."""
    result = session.run(
        """
        MATCH (o:OpticalPath) WHERE o.id IN $path_ids
        OPTIONAL MATCH (c:Circuit)-[:RIDES]->(o)
        OPTIONAL MATCH (c)-[:END {role: 'router'}]->(port:Port)<-[:HAS]-(r:Router)
        RETURN collect(DISTINCT o.name) AS paths,
               collect(DISTINCT o.wavelength_nm) AS wavelengths,
               collect(DISTINCT c.id) AS circuit_ids,
               collect(DISTINCT c.peering_id) AS peering_ids,
               collect(DISTINCT {router_id: r.id, hostname: r.hostname, interface: port.name}) AS interfaces
        """,
        path_ids=path_ids,
    ).single()
    return {
        "optical_paths": sorted(result["paths"]),
        "wavelengths_nm": sorted(w for w in result["wavelengths"] if w is not None),
        "circuit_ids": sorted(result["circuit_ids"]),
        "peering_ids": sorted(p for p in result["peering_ids"] if p is not None),
        "router_interfaces": sorted(
            (i for i in result["interfaces"] if i["router_id"] is not None),
            key=lambda i: (i["hostname"], i["interface"]),
        ),
    }


def paths_through_span(span_id: int) -> dict:
    """What a cut of this span (both fibers) takes down: every optical path
    over it, both directions - the OSC paths included, since losing those
    too is what tells a cut apart from an amplifier failure."""
    with get_driver().session() as session:
        ids = [r["id"] for r in session.run("MATCH (o:OpticalPath)-[:HOP]->(:Span {id: $id}) RETURN o.id AS id", id=span_id)]
        return _carried(session, ids)


def paths_through_device(device_id: int) -> dict:
    """What a failure of this whole element takes down: every optical path
    through any of its ports."""
    with get_driver().session() as session:
        ids = [
            r["id"]
            for r in session.run(
                "MATCH (o:OpticalPath)-[:HOP]->(:Port)<-[:HAS]-(:Router {id: $id}) RETURN DISTINCT o.id AS id", id=device_id
            )
        ]
        return _carried(session, ids)


def downstream_of(port_id: int, direction: str) -> dict:
    """Every port after `port_id` on the `direction` optical paths through
    it (in path order), plus what those paths carry - i.e. everything that
    loses light when the signal stops at this port. An alarm anywhere in
    here is a symptom of a fault at or before this port."""
    with get_driver().session() as session:
        rows = session.run(
            """
            MATCH (o:OpticalPath {direction: $direction})-[h:HOP]->(:Port {id: $port_id})
            MATCH (o)-[h2:HOP]->(x) WHERE h2.seq > h.seq
            OPTIONAL MATCH (x)<-[:HAS]-(r:Router)
            RETURN o.id AS path_id, o.name AS path, h2.seq AS seq, labels(x)[0] AS kind,
                   x.id AS id, x.name AS port, x.seq AS span_seq, r.id AS device_id, r.hostname AS hostname
            ORDER BY path, seq
            """,
            port_id=port_id,
            direction=direction,
        )
        hops, path_ids = [], set()
        for r in rows:
            path_ids.add(r["path_id"])
            hops.append(
                {
                    "path": r["path"], "seq": r["seq"], "kind": r["kind"].lower(), "id": r["id"], "port": r["port"],
                    "span_seq": r["span_seq"], "device_id": r["device_id"], "hostname": r["hostname"],
                }
            )
        return {"hops": hops, **_carried(session, sorted(path_ids))}


def upstream_span(port_id: int, direction: str) -> dict | None:
    """The span feeding `port_id` in `direction` - the nearest span hop
    before it - and the element at that span's far (upstream) end. When the
    first element to lose light is this port's device, this is the span to
    suspect. None if no span precedes it (e.g. a transmit-side port)."""
    with get_driver().session() as session:
        record = session.run(
            """
            MATCH (o:OpticalPath {direction: $direction})-[h:HOP]->(:Port {id: $port_id})
            MATCH (o)-[hs:HOP]->(s:Span) WHERE hs.seq < h.seq
            WITH s, hs ORDER BY hs.seq DESC LIMIT 1
            MATCH (s)-[:FROM]->(a:Router), (s)-[:TO]->(z:Router)
            RETURN s.id AS span_id, s.seq AS seq, s.provider AS provider, s.length_km AS length_km,
                   a.id AS a_id, a.hostname AS a_hostname, z.id AS z_id, z.hostname AS z_hostname
            """,
            port_id=port_id,
            direction=direction,
        ).single()
        if record is None:
            return None
        upstream = (record["a_id"], record["a_hostname"]) if direction == "AZ" else (record["z_id"], record["z_hostname"])
        return {
            "span_id": record["span_id"], "seq": record["seq"], "provider": record["provider"],
            "length_km": record["length_km"], "upstream_device_id": upstream[0], "upstream_hostname": upstream[1],
        }


def span_between(device_a_id: int, device_b_id: int) -> dict | None:
    """The span directly joining two adjacent line elements, either order."""
    with get_driver().session() as session:
        record = session.run(
            """
            MATCH (s:Span)-[:FROM]->(a:Router), (s)-[:TO]->(z:Router)
            WHERE (a.id = $x AND z.id = $y) OR (a.id = $y AND z.id = $x)
            RETURN s.id AS span_id, s.corridor_id AS corridor_id, s.seq AS seq, s.provider AS provider,
                   s.a_end AS a_end, s.b_end AS b_end, s.length_km AS length_km,
                   a.hostname AS from_hostname, z.hostname AS to_hostname
            """,
            x=device_a_id,
            y=device_b_id,
        ).single()
        return dict(record) if record else None
