import enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import relationship

from app.config import settings
from app.db import Base

if settings.is_postgres:
    from geoalchemy2 import Geography
    from geoalchemy2.elements import WKTElement


class RouterStatus(str, enum.Enum):
    UP = "up"
    DOWN = "down"
    FLAPPING = "flapping"
    UNKNOWN = "unknown"


class IncidentType(str, enum.Enum):
    LINK_DOWN = "LINK_DOWN"
    LINK_UP = "LINK_UP"
    LINK_FLAP = "LINK_FLAP"
    COLD_START = "COLD_START"
    WARM_START = "WARM_START"
    AUTH_FAILURE = "AUTH_FAILURE"
    BGP_STATE_CHANGE = "BGP_STATE_CHANGE"
    HIGH_CPU = "HIGH_CPU"
    ENV_ALARM = "ENV_ALARM"
    CONFIG_CHANGE = "CONFIG_CHANGE"
    BFD_SESSION_DOWN = "BFD_SESSION_DOWN"
    ISIS_NBR_DOWN = "ISIS_NBR_DOWN"
    ISIS_NBR_UP = "ISIS_NBR_UP"
    OPTICAL_ALARM = "OPTICAL_ALARM"
    FAN_FAILURE = "FAN_FAILURE"
    PSU_FAILURE = "PSU_FAILURE"
    HIGH_MEMORY = "HIGH_MEMORY"
    UNKNOWN = "UNKNOWN"


class IncidentStatus(str, enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class AlarmLayer(str, enum.Enum):
    L1 = "L1"  # physical/optical (fiber, amplifiers, transceivers)
    L3 = "L3"  # control-plane/logical (BGP, IS-IS, interfaces)


# Which layer each incident type belongs to for topology-based root-cause
# correlation (see app.correlation) - anything not listed here defaults to
# L3, since most of this codebase's incident types are control-plane/
# interface events. Only the genuinely physical-layer alarm types are L1.
INCIDENT_LAYER: dict[IncidentType, AlarmLayer] = {
    IncidentType.OPTICAL_ALARM: AlarmLayer.L1,
    IncidentType.FAN_FAILURE: AlarmLayer.L1,
    IncidentType.PSU_FAILURE: AlarmLayer.L1,
}


def _default_incident_layer(context) -> AlarmLayer:
    incident_type = context.get_current_parameters()["incident_type"]
    return INCIDENT_LAYER.get(incident_type, AlarmLayer.L3)


class RemediationActionType(str, enum.Enum):
    INTERFACE_BOUNCE = "INTERFACE_BOUNCE"
    CLEAR_COUNTERS = "CLEAR_COUNTERS"
    BGP_NEIGHBOR_RESET = "BGP_NEIGHBOR_RESET"
    PROCESS_RESTART = "PROCESS_RESTART"
    NOTIFY_ONLY = "NOTIFY_ONLY"


class RemediationStatus(str, enum.Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class BgpSessionStatus(str, enum.Enum):
    ESTABLISHED = "established"
    DOWN = "down"


class BundleStatus(str, enum.Enum):
    UP = "up"
    DOWN = "down"


class RouterType(str, enum.Enum):
    PRIMARY = "primary"  # telco-owned backbone router, BGP-meshed with other primaries
    CUSTOMER = "customer"  # last-mile CPE, single-homed to one primary, not in the BGP mesh


class TeamKind(str, enum.Enum):
    MAINTENANCE = "maintenance"
    SOC = "soc"


class NotificationStatus(str, enum.Enum):
    SENT = "sent"
    SIMULATED = "simulated"  # no SMTP configured - recorded for audit but not actually delivered
    FAILED = "failed"


class Router(Base):
    __tablename__ = "routers"

    id = Column(Integer, primary_key=True, index=True)
    hostname = Column(String(128), nullable=False)
    mgmt_ip = Column(String(64), nullable=False, unique=True, index=True)
    asn = Column(Integer, nullable=True)
    router_type = Column(Enum(RouterType), default=RouterType.PRIMARY, nullable=False, index=True)
    parent_router_id = Column(Integer, ForeignKey("routers.id"), nullable=True, index=True)
    vendor = Column(String(64), default="Cisco")
    model = Column(String(64), nullable=True)
    site_name = Column(String(128), nullable=True)
    country = Column(String(64), nullable=True)
    city = Column(String(128), nullable=True)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    status = Column(Enum(RouterStatus), default=RouterStatus.UNKNOWN, nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    trap_events = relationship("TrapEvent", back_populates="router", cascade="all, delete-orphan")
    incidents = relationship("Incident", back_populates="router", cascade="all, delete-orphan")

    if settings.is_postgres:
        # Kept in sync with latitude/longitude by _sync_router_location below
        # rather than being writable directly - backs the PostGIS nearest-
        # router lookup in api/routers.py (ST_Distance needs a geography
        # column to operate on, not two independent float columns).
        location = Column(Geography(geometry_type="POINT", srid=4326), nullable=True)


if settings.is_postgres:

    @event.listens_for(Router, "before_insert")
    @event.listens_for(Router, "before_update")
    def _sync_router_location(mapper, connection, target: "Router") -> None:
        target.location = WKTElement(f"POINT({target.longitude} {target.latitude})", srid=4326)


class TrapEvent(Base):
    __tablename__ = "trap_events"

    id = Column(Integer, primary_key=True, index=True)
    router_id = Column(Integer, ForeignKey("routers.id"), nullable=False, index=True)
    received_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    oid = Column(String(128), nullable=False)
    trap_name = Column(String(64), nullable=False)
    interface_name = Column(String(64), nullable=True)
    severity = Column(String(32), default="info")
    raw_varbinds = Column(Text, nullable=True)
    incident_id = Column(Integer, ForeignKey("incidents.id"), nullable=True, index=True)

    router = relationship("Router", back_populates="trap_events")
    incident = relationship("Incident", back_populates="trap_events")


class Incident(Base):
    __tablename__ = "incidents"

    id = Column(Integer, primary_key=True, index=True)
    router_id = Column(Integer, ForeignKey("routers.id"), nullable=False, index=True)
    incident_type = Column(Enum(IncidentType), nullable=False, index=True)
    interface_name = Column(String(64), nullable=True)
    status = Column(Enum(IncidentStatus), default=IncidentStatus.OPEN, nullable=False, index=True)
    opened_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    closed_at = Column(DateTime(timezone=True), nullable=True)
    trap_count = Column(Integer, default=1)
    description = Column(String(255), nullable=True)
    # True when a human resolved this via the UI/API rather than the
    # classifier itself (e.g. a linkUp trap, or auto-heal marking a
    # transient event as handled).
    resolved_manually = Column(Boolean, default=False, nullable=False)
    # Common-Alarm-Model-style layer, derived automatically from
    # incident_type (see INCIDENT_LAYER above) - used by app.correlation to
    # rank an L1 (physical) alarm above any L3 (control-plane) symptom on
    # the same link.
    layer = Column(Enum(AlarmLayer), nullable=False, default=_default_incident_layer, index=True)
    # Set only for L1 incidents that stem from a specific fiber link (see
    # app.fiber_faults) - lets root-cause lookup walk "this peering has an
    # open physical fault" without guessing which endpoint router owns it.
    peering_id = Column(Integer, ForeignKey("bgp_peerings.id"), nullable=True, index=True)
    # Set by app.correlation.try_link_root_cause when this (L3) incident
    # opened while an L1 incident was already open on a peering it belongs
    # to - marks it as a symptom rather than an independent problem, so it's
    # excluded from auto-heal and can be suppressed from the top-level view.
    root_cause_incident_id = Column(Integer, ForeignKey("incidents.id"), nullable=True, index=True)

    router = relationship("Router", back_populates="incidents")
    trap_events = relationship("TrapEvent", back_populates="incident")
    remediation_actions = relationship("RemediationAction", back_populates="incident", cascade="all, delete-orphan")
    peering = relationship("BgpPeering")
    root_cause = relationship("Incident", remote_side=[id], back_populates="symptomatic_incidents")
    symptomatic_incidents = relationship("Incident", back_populates="root_cause")


class RouterConfigBackup(Base):
    __tablename__ = "router_config_backups"

    id = Column(Integer, primary_key=True, index=True)
    router_id = Column(Integer, ForeignKey("routers.id"), nullable=False, index=True)
    incident_id = Column(Integer, ForeignKey("incidents.id"), nullable=True, index=True)
    taken_at = Column(DateTime(timezone=True), server_default=func.now())
    reason = Column(String(255), nullable=True)
    config_text = Column(Text, nullable=False)

    router = relationship("Router")


class RemediationAction(Base):
    """A rule-based auto-heal step: a config backup is always taken first
    (see RouterConfigBackup), then this records whatever action (or
    deliberate no-action) was taken in response to the incident."""

    __tablename__ = "remediation_actions"

    id = Column(Integer, primary_key=True, index=True)
    incident_id = Column(Integer, ForeignKey("incidents.id"), nullable=False, index=True)
    router_id = Column(Integer, ForeignKey("routers.id"), nullable=False, index=True)
    backup_id = Column(Integer, ForeignKey("router_config_backups.id"), nullable=True)
    incident_type = Column(String(32), nullable=False)  # denormalized for cheap display
    action_type = Column(Enum(RemediationActionType), nullable=False)
    status = Column(Enum(RemediationStatus), nullable=False)
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    finished_at = Column(DateTime(timezone=True), nullable=True)
    summary = Column(String(255), nullable=True)
    log = Column(Text, nullable=True)

    router = relationship("Router")
    incident = relationship("Incident", back_populates="remediation_actions")
    backup = relationship("RouterConfigBackup")


class RouterModel(Base):
    """Catalog of selectable (vendor, model) pairs backing the Add Fleet
    form's Vendor/Model dropdowns - a UI convenience list only, not linked
    to Router.vendor/model by FK, so CSV bulk upload, the seed API, and the
    trap simulator's own seed data are never constrained to it."""

    __tablename__ = "router_models"
    __table_args__ = (UniqueConstraint("vendor", "model", name="uq_router_model_vendor_model"),)

    id = Column(Integer, primary_key=True, index=True)
    vendor = Column(String(64), nullable=False, index=True)
    model = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Team(Base):
    """A maintenance or SOC team an operator can hand an open incident off
    to for human review, along with the email address it should be
    notified at."""

    __tablename__ = "teams"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(Enum(TeamKind), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    email = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class IncidentNotification(Base):
    """Audit trail of every "send to SOC/maintenance" action taken on an
    incident from the Human Review tab - what was sent, to whom, and
    whether the email actually went out or was only simulated (no SMTP
    configured)."""

    __tablename__ = "incident_notifications"

    id = Column(Integer, primary_key=True, index=True)
    incident_id = Column(Integer, ForeignKey("incidents.id"), nullable=False, index=True)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=False, index=True)
    subject = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    status = Column(Enum(NotificationStatus), nullable=False)
    error = Column(String(255), nullable=True)
    sent_at = Column(DateTime(timezone=True), server_default=func.now())

    incident = relationship("Incident")
    team = relationship("Team")


class BgpPeering(Base):
    """An undirected BGP session between two routers. Stored once per pair
    with router_a_id < router_b_id (enforced by whoever creates the row,
    see api/bgp.py) so there's exactly one row per link regardless of
    which side reports a state change."""

    __tablename__ = "bgp_peerings"
    __table_args__ = (UniqueConstraint("router_a_id", "router_b_id", name="uq_bgp_peering_pair"),)

    id = Column(Integer, primary_key=True, index=True)
    router_a_id = Column(Integer, ForeignKey("routers.id"), nullable=False, index=True)
    router_b_id = Column(Integer, ForeignKey("routers.id"), nullable=False, index=True)
    status = Column(Enum(BgpSessionStatus), default=BgpSessionStatus.ESTABLISHED, nullable=False)
    last_changed_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    # Great-circle distance between the two sites and the number of SMF
    # regenerators the physical fiber run needs at ~80km spacing - both
    # computed once by simulator/generate_bgp_topology.py from real
    # coordinates, not simulated/random.
    distance_km = Column(Float, nullable=True)
    repeater_count = Column(Integer, default=0, nullable=False)

    router_a = relationship("Router", foreign_keys=[router_a_id])
    router_b = relationship("Router", foreign_keys=[router_b_id])


class InterfaceBundle(Base):
    """A Port-channel (EtherChannel/LACP-style) logical interface on one
    side of a BGP peering - each router configures its own bundle, so a
    single BgpPeering has two of these (bundle_a, bundle_b). Status is
    "true redundancy": up as long as at least one member interface still
    has IS-IS adjacency, only down once every member has lost it."""

    __tablename__ = "interface_bundles"
    __table_args__ = (UniqueConstraint("router_id", "name", name="uq_bundle_router_name"),)

    id = Column(Integer, primary_key=True, index=True)
    router_id = Column(Integer, ForeignKey("routers.id"), nullable=False, index=True)
    peering_id = Column(Integer, ForeignKey("bgp_peerings.id"), nullable=False, index=True)
    name = Column(String(64), nullable=False)  # e.g. "Port-channel1"
    status = Column(Enum(BundleStatus), default=BundleStatus.UP, nullable=False)
    last_changed_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    router = relationship("Router")
    peering = relationship("BgpPeering")
    members = relationship(
        "Interface", back_populates="bundle", cascade="all, delete-orphan", order_by="Interface.name"
    )


class Interface(Base):
    """One physical member link of an InterfaceBundle (e.g. a 40G FortyGigE
    port). isis_adjacency_up tracks that member's own IS-IS adjacency state
    independent of its siblings - the bundle only goes down once every
    member is down."""

    __tablename__ = "interfaces"
    __table_args__ = (UniqueConstraint("router_id", "name", name="uq_interface_router_name"),)

    id = Column(Integer, primary_key=True, index=True)
    router_id = Column(Integer, ForeignKey("routers.id"), nullable=False, index=True)
    bundle_id = Column(Integer, ForeignKey("interface_bundles.id"), nullable=False, index=True)
    name = Column(String(64), nullable=False)  # e.g. "FortyGigE0/0/0"
    bandwidth_mbps = Column(Integer, nullable=False, default=40000)
    isis_adjacency_up = Column(Boolean, default=True, nullable=False)
    last_changed_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    router = relationship("Router")
    bundle = relationship("InterfaceBundle", back_populates="members")
