"""Normalization service - the design doc's "vendor MIB -> Common Alarm
Model" box. Consumes raw (unclassified) alarms from `raw-alarms`, attaches
CAM fields (trap_name/incident_type/severity/layer) via the existing OID
lookup table, and republishes to `norm-alarms`. Deliberately has no DB or
Neo4j access at all - that starts downstream in app.streaming.correlator -
so onboarding a new vendor here is purely "add OID mappings to
app.snmp.oid_map," never a schema/infra change.

Run as its own process/container (`python -m app.streaming.normalizer`),
not inside the `backend` FastAPI process.
"""

import json
import logging
import time

from kafka import KafkaConsumer, KafkaProducer
from kafka.errors import NoBrokersAvailable

from app.config import settings
from app.models import INCIDENT_LAYER, AlarmLayer
from app.snmp.oid_map import TRAP_OID_MAP, UNKNOWN_TRAP
from app.streaming.topics import NORM_ALARMS, RAW_ALARMS

logger = logging.getLogger(__name__)


def _connect() -> tuple[KafkaConsumer, KafkaProducer]:
    delay = 1
    while True:
        try:
            consumer = KafkaConsumer(
                RAW_ALARMS,
                bootstrap_servers=settings.kafka_bootstrap_servers,
                group_id="normalizer",
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


def normalize(message: dict) -> dict:
    trap_name, incident_type, severity = TRAP_OID_MAP.get(message["trap_oid"], UNKNOWN_TRAP)
    layer = INCIDENT_LAYER.get(incident_type, AlarmLayer.L3)
    return {
        **message,
        "trap_name": trap_name,
        "incident_type": incident_type.value,
        "severity": severity,
        "layer": layer.value,
    }


def main() -> None:
    consumer, producer = _connect()
    logger.info("normalizer: consuming %s, producing %s", RAW_ALARMS, NORM_ALARMS)
    for record in consumer:
        try:
            producer.send(NORM_ALARMS, normalize(record.value))
        except Exception:
            logger.exception("Failed to normalize message %r", record.value)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
