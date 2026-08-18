"""Bridges the alarm pipeline's final Kafka topic back onto the WebSocket
connections `backend` already holds. app.streaming.correlator publishes
here after writing the alarm store; this module is the only thing that
turns those messages back into a `manager.broadcast()` call, since it's
the process that actually owns the open WebSocket sockets.

Runs as a daemon thread *inside* the `backend` process (unlike normalizer/
correlator, which are their own containers) for exactly that reason - it
needs direct, in-process access to app.ws.manager, and bridges into the
asyncio event loop via run_coroutine_threadsafe since kafka-python's
consumer is a plain blocking iterator, not asyncio-native.

Single-backend-instance assumption: uses one fixed consumer group so a
restart doesn't replay history, which is exactly what's wanted here since
there's only one `backend` process holding WebSocket connections to relay
to. Running multiple `backend` replicas behind a load balancer would need
a unique group per replica instead, so each one's own connected clients
still see every event.
"""

import asyncio
import json
import logging
import threading

from kafka import KafkaConsumer
from kafka.errors import NoBrokersAvailable

from app.config import settings
from app.streaming.topics import INCIDENT_EVENTS
from app.ws import manager

logger = logging.getLogger(__name__)


def _connect(stop_event: threading.Event) -> KafkaConsumer | None:
    while not stop_event.is_set():
        try:
            return KafkaConsumer(
                INCIDENT_EVENTS,
                bootstrap_servers=settings.kafka_bootstrap_servers,
                group_id="backend-ws-relay",
                value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                auto_offset_reset="latest",
                consumer_timeout_ms=1000,
            )
        except NoBrokersAvailable:
            logger.warning("Kafka not reachable yet at %s, retrying", settings.kafka_bootstrap_servers)
            stop_event.wait(2)
    return None


def _run(loop: asyncio.AbstractEventLoop, stop_event: threading.Event) -> None:
    consumer = _connect(stop_event)
    if consumer is None:
        return
    logger.info("ws relay: consuming %s -> /ws/events", INCIDENT_EVENTS)
    try:
        while not stop_event.is_set():
            # consumer_timeout_ms above makes this loop return every ~1s
            # even with nothing new, so stop_event is checked promptly on
            # shutdown rather than blocking indefinitely on the next record.
            for record in consumer:
                asyncio.run_coroutine_threadsafe(manager.broadcast(record.value), loop)
                if stop_event.is_set():
                    break
    finally:
        consumer.close()


def start(loop: asyncio.AbstractEventLoop) -> tuple[threading.Thread, threading.Event]:
    stop_event = threading.Event()
    thread = threading.Thread(target=_run, args=(loop, stop_event), daemon=True, name="incident-events-relay")
    thread.start()
    return thread, stop_event


def stop(thread: threading.Thread, stop_event: threading.Event) -> None:
    stop_event.set()
    thread.join(timeout=5)
