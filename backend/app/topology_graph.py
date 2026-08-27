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
