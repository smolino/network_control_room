"""Shared Kafka producer for the mediation layer (app.snmp.trap_listener,
app.fiber_faults) - both live inside the `backend` process and just need to
turn a decoded/synthetic trap into a raw-alarms message, with no DB access
of their own (that starts in app.streaming.normalizer).
"""

import json
import logging
import time

from kafka import KafkaProducer
from kafka.errors import NoBrokersAvailable

from app.config import settings
from app.streaming.topics import RAW_ALARMS

logger = logging.getLogger(__name__)

_producer: KafkaProducer | None = None


def get_producer() -> KafkaProducer:
    """Lazily creates the singleton producer, retrying while the `kafka`
    container is still starting up (its healthcheck passing doesn't
    guarantee the broker has finished internal setup the instant a
    dependent container starts)."""
    global _producer
    if _producer is not None:
        return _producer

    delay = 1
    while True:
        try:
            _producer = KafkaProducer(
                bootstrap_servers=settings.kafka_bootstrap_servers,
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            )
            return _producer
        except NoBrokersAvailable:
            logger.warning("Kafka not reachable yet at %s, retrying in %ss", settings.kafka_bootstrap_servers, delay)
            time.sleep(delay)
            delay = min(delay * 2, 15)


def publish_raw_alarm(
    source_ip: str,
    trap_oid: str,
    varbinds: list[tuple[str, str]],
    peering_id: int | None = None,
) -> None:
    """Publishes one raw (unclassified) alarm to `raw-alarms` - the only
    thing the mediation layer does with a trap now, real or synthetic. See
    app.streaming.normalizer for what happens to it next."""
    get_producer().send(
        RAW_ALARMS,
        {"source_ip": source_ip, "trap_oid": trap_oid, "varbinds": varbinds, "peering_id": peering_id},
    )
