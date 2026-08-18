"""Enrichment + correlation + alarm store - the design doc's combined
"ENRICHMENT" and "CORRELATION / ROOT-CAUSE ENGINE" boxes (drawn as adjacent
with no queue between them, unlike raw-alarms/norm-alarms which do have a
named topic - so they're one service here too). Consumes normalized alarms
from `norm-alarms` and hands each one to
app.snmp.classifier.classify_and_store - exactly what used to run
synchronously in the trap-listener's request path before this pipeline
existed: router lookup (enrichment), TrapEvent/Incident persistence (the
alarm store), flap/bundle/BGP-peering updates, and Neo4j-backed root-cause
correlation (app.correlation). The final WS-ready payload is published to
`incident-events` for `backend`'s relay thread to broadcast - this service
never touches the WebSocket directly, since it isn't the process holding
those connections.

Run as its own process/container (`python -m app.streaming.correlator`),
not inside the `backend` FastAPI process.
"""

import json
import logging
import time

from kafka import KafkaConsumer, KafkaProducer
from kafka.errors import NoBrokersAvailable

from app.config import settings
from app.db import SessionLocal
from app.snmp.classifier import classify_and_store
from app.streaming.topics import INCIDENT_EVENTS, NORM_ALARMS

logger = logging.getLogger(__name__)


def _connect() -> tuple[KafkaConsumer, KafkaProducer]:
    delay = 1
    while True:
        try:
            consumer = KafkaConsumer(
                NORM_ALARMS,
                bootstrap_servers=settings.kafka_bootstrap_servers,
                group_id="correlator",
                value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                auto_offset_reset="earliest",
            )
            producer = KafkaProducer(
                bootstrap_servers=settings.kafka_bootstrap_servers,
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            )
            return consumer, producer
        except NoBrokersAvailable:
            logger.warning("Kafka not reachable yet at %s, retrying in %ss", settings.kafka_bootstrap_servers, delay)
            time.sleep(delay)
            delay = min(delay * 2, 15)


def _process(db, message: dict) -> dict | None:
    # classify_and_store re-derives trap_name/incident_type/severity from
    # trap_oid itself (via the same TRAP_OID_MAP the normalizer already
    # consulted) rather than trusting message["incident_type"] etc. - the
    # normalizer's added fields are still published for
    # observability/CAM-shape fidelity, just not the source of truth this
    # service acts on. varbinds round-trip through JSON as lists, not
    # tuples; classify_and_store only ever unpacks them, so that's fine.
    varbinds = [tuple(vb) for vb in message["varbinds"]]
    return classify_and_store(
        db,
        source_ip=message["source_ip"],
        trap_oid=message["trap_oid"],
        varbinds=varbinds,
        peering_id=message.get("peering_id"),
    )


def main() -> None:
    consumer, producer = _connect()
    logger.info("correlator: consuming %s, producing %s", NORM_ALARMS, INCIDENT_EVENTS)
    for record in consumer:
        db = SessionLocal()
        try:
            result = _process(db, record.value)
        except Exception:
            db.rollback()
            logger.exception("Failed to process message %r", record.value)
            result = None
        finally:
            db.close()
        if result:
            producer.send(INCIDENT_EVENTS, result)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
