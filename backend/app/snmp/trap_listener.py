"""Minimal asyncio SNMP trap receiver.

Decodes SNMPv1/v2c trap PDUs at the BER level with pysnmp's protocol
modules directly, instead of using pysnmp's own dispatcher/engine, so it
can share FastAPI's asyncio event loop via a plain
`loop.create_datagram_endpoint` UDP server.

Pure mediation: this is the "collection" box in the design doc's
architecture - it decodes the wire format and publishes a raw alarm to
Kafka (see app.streaming.producer), nothing else. Classification,
enrichment, persistence, and correlation all happen downstream in
app.streaming.normalizer/correlator.
"""

import asyncio
import logging
import socket

from pyasn1.codec.ber import decoder
from pysnmp.proto import api

from app.config import settings
from app.streaming.producer import publish_raw_alarm

logger = logging.getLogger(__name__)

SNMP_TRAP_OID_PREFIX = "1.3.6.1.6.3.1.1.4.1"


def _decode_varbinds(proto_mod, req_pdu, msg_ver) -> list[tuple[str, str]]:
    # NB: `getVarBinds` (plural) returns resolved (name, value) pairs;
    # `getVarBindList` returns raw VarBind ASN.1 objects whose default
    # iteration yields component *names*, not values.
    if msg_ver == api.protoVersion1:
        var_binds = proto_mod.apiTrapPDU.getVarBinds(req_pdu)
    else:
        var_binds = proto_mod.apiPDU.getVarBinds(req_pdu)
    return [(str(name), value.prettyPrint()) for name, value in var_binds]


def _extract_trap_oid(proto_mod, req_pdu, msg_ver, varbinds: list[tuple[str, str]]) -> str:
    if msg_ver == api.protoVersion1:
        generic = int(proto_mod.apiTrapPDU.getGenericTrap(req_pdu))
        specific = int(proto_mod.apiTrapPDU.getSpecificTrap(req_pdu))
        enterprise = str(proto_mod.apiTrapPDU.getEnterprise(req_pdu))
        if generic == 6:  # enterpriseSpecific
            return f"{enterprise}.0.{specific}"
        # map SNMPv1 generic trap numbers onto the SNMPv2 standard trap OIDs
        return {
            0: "1.3.6.1.6.3.1.1.5.1",  # coldStart
            1: "1.3.6.1.6.3.1.1.5.2",  # warmStart
            2: "1.3.6.1.6.3.1.1.5.3",  # linkDown
            3: "1.3.6.1.6.3.1.1.5.4",  # linkUp
            4: "1.3.6.1.6.3.1.1.5.5",  # authenticationFailure
        }.get(generic, f"{enterprise}.generic.{generic}")

    for name, value in varbinds:
        if name.startswith(SNMP_TRAP_OID_PREFIX):
            return value
    return varbinds[0][0] if varbinds else "unknown"


def _publish_sync(source_ip: str, trap_oid: str, varbinds: list[tuple[str, str]]) -> None:
    try:
        publish_raw_alarm(source_ip, trap_oid, varbinds)
    except Exception:
        logger.exception("Failed to publish trap from %s (oid=%s)", source_ip, trap_oid)


async def _process_trap(source_ip: str, trap_oid: str, varbinds: list[tuple[str, str]]) -> None:
    # Off the event loop: the producer is pre-warmed at startup (see
    # main.py's lifespan) so this is normally just a non-blocking buffered
    # send, but run it via to_thread anyway in case a burst of traps (e.g.
    # thousands of routers cold-booting at once) arrives before that
    # warm-up finishes and the producer falls back to its blocking retry
    # loop - that must never stall the event loop that's also decoding
    # incoming UDP datagrams.
    await asyncio.to_thread(_publish_sync, source_ip, trap_oid, varbinds)


async def _handle_datagram(data: bytes, addr: tuple[str, int]) -> None:
    source_ip = addr[0]
    whole_msg = data
    while whole_msg:
        try:
            msg_ver = api.decodeMessageVersion(whole_msg)
        except Exception:
            return
        proto_mod = api.protoModules.get(msg_ver)
        if proto_mod is None:
            return

        try:
            req_msg, whole_msg = decoder.decode(whole_msg, asn1Spec=proto_mod.Message())
        except Exception:
            logger.warning("Could not decode SNMP packet from %s", source_ip)
            return

        req_pdu = proto_mod.apiMessage.getPDU(req_msg)
        if not req_pdu.isSameTypeWith(proto_mod.TrapPDU()):
            continue

        varbinds = _decode_varbinds(proto_mod, req_pdu, msg_ver)
        trap_oid = _extract_trap_oid(proto_mod, req_pdu, msg_ver, varbinds)
        await _process_trap(source_ip, trap_oid, varbinds)


class TrapProtocol(asyncio.DatagramProtocol):
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.loop.create_task(_handle_datagram(data, addr))

    def error_received(self, exc: Exception) -> None:
        logger.warning("SNMP trap listener socket error: %s", exc)


async def start_trap_listener():
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: TrapProtocol(loop),
        local_addr=(settings.trap_host, settings.trap_port),
    )
    # Best-effort: absorbs bursts (e.g. a real fleet-wide event, or a batch
    # of routers cold-booting together) in the kernel's socket buffer
    # rather than dropping datagrams the moment processing falls slightly
    # behind. The kernel silently clamps this to net.core.rmem_max if lower.
    sock = transport.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        except OSError:
            logger.warning("Could not raise SNMP trap socket's receive buffer size")
    logger.info("SNMP trap listener listening on %s:%s/udp", settings.trap_host, settings.trap_port)
    return transport
