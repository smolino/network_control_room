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
from sqlalchemy.dialects.mysql import MEDIUMTEXT
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


class IncidentStage(str, enum.Enum):
    """Where an incident is in the handling workflow - finer-grained than
    `status`, which stays the coarse open/resolved flag everything else
    (impact, stats, map) filters on. See app.incidents.service for every
    transition."""

    NEW = "new"  # just opened, waiting out the settle window before auto-heal
    SUPPRESSED = "suppressed"  # symptom of an open L1 root cause, no action of its own
    MONITORING = "monitoring"  # auto-heal succeeded, waiting for the clear trap to confirm it
    TRIAGE = "triage"  # needs a human: notify-only type, failed fix, or unconfirmed fix
    ACKNOWLEDGED = "acknowledged"  # an operator owns it
    DISPATCHED = "dispatched"  # handed to a SOC/maintenance team, waiting on them
    RESOLVED = "resolved"


class ResolutionReason(str, enum.Enum):
    # Picked by an operator when resolving by hand.
    FIXED = "fixed"
    FALSE_POSITIVE = "false_positive"
    MAINTENANCE = "maintenance"
    DUPLICATE = "duplicate"
    # Set by the system.
    AUTO_CLEARED = "auto_cleared"  # the device sent its own clear trap (linkUp, bgpEstablished, ...)
    AUTO_HEALED = "auto_healed"  # auto-heal succeeded and the type has no clear trap to wait for
    ROOT_CAUSE_RESOLVED = "root_cause_resolved"  # resolved along with its root cause
    RESET = "reset"  # admin "reset all open incidents"


USER_RESOLUTION_REASONS = {
    ResolutionReason.FIXED,
    ResolutionReason.FALSE_POSITIVE,
    ResolutionReason.MAINTENANCE,
    ResolutionReason.DUPLICATE,
}


class AlarmLayer(str, enum.Enum):
    L1 = "L1"  # physical/optical (fiber, amplifiers, transceivers)
    L3 = "L3"  # control-plane/logical (BGP, IS-IS, interfaces)


class AlarmClass(str, enum.Enum):
    """What kind of problem an alarm reports - orthogonal to AlarmLayer
    (which network layer the element sits on). Only TRANSPORT alarms can be
    the root cause of a fiber fault; EQUIPMENT (hardware/environment) and
    MANAGEMENT alarms can be filtered out entirely (see
    app.snmp.alarm_classes). Assigned per trap, not per incident type: the
    same OPTICAL_ALARM type covers LOS (transport) and a sensor threshold
    (equipment)."""

    TRANSPORT = "transport"  # loss of light/frame on the optical path: LOS, LOS-O, LOF, AIS, LF/RF...
    LOGICAL = "logical"  # L3 consequences: link down, IS-IS, BFD, BGP
    EQUIPMENT = "equipment"  # hardware/environment: card, fan, power, temperature, CPU, memory, sensors
    MANAGEMENT = "management"  # device management: boot, authentication, configuration


# Which layer each incident type belongs to when nothing more specific is
# known - app.snmp.alarm_classes.layer_for sets it from the alarm class and
# the element's role when an incident is opened. Anything not listed
# defaults to L3.
INCIDENT_LAYER: dict[IncidentType, AlarmLayer] = {
    IncidentType.OPTICAL_ALARM: AlarmLayer.L1,
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


class ElementRole(str, enum.Enum):
    """What kind of network element a `routers` row really is. Optical
    elements live in the same table as routers so the alarm pipeline (which
    resolves a trap's sender by mgmt_ip there) and incidents work for them
    unchanged - but only ROUTER runs IS-IS/BGP/bundles. NULL means ROUTER,
    so rows created before this column existed keep behaving as routers.
    See app.corridors for how the optical ones are wired together."""

    ROUTER = "router"  # L3: e.g. Cisco ASR9K
    TRANSPONDER = "transponder"  # L1: e.g. Cisco NCS1004 - client <-> DWDM trunk
    TERMINAL = "terminal"  # L1: DWDM line terminal (mux/demux + booster/preamp), e.g. ONS 15454 MSTP
    ILA = "ila"  # L1: in-line amplifier site, e.g. ONS 15454 with one amp per direction


L1_ELEMENT_ROLES = (ElementRole.TRANSPONDER, ElementRole.TERMINAL, ElementRole.ILA)


class TeamKind(str, enum.Enum):
    MAINTENANCE = "maintenance"
    SOC = "soc"


class UserRole(str, enum.Enum):
    # Ordered least -> most privileged; see app.auth.ROLE_RANK.
    VIEWER = "viewer"  # read-only: map, dashboards, incidents, routers
    OPERATOR = "operator"  # + resolve incidents, hand them off to SOC/maintenance
    ADMIN = "admin"  # + fleet changes, teams, simulation, user management


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
    # NULL = a router (see ElementRole). Stored as a plain string so adding
    # a role later needs no enum migration.
    element_role = Column(Enum(ElementRole, native_enum=False, length=16), nullable=True, index=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=True, index=True)

    site = relationship("Site")
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
    # See AlarmClass / app.snmp.alarm_classes. `condition` is the optical
    # condition code (LOS-P, LOS-O, OPWR-LFAIL, ...) when the trap carries one.
    alarm_class = Column(Enum(AlarmClass, native_enum=False, length=16), nullable=True, index=True)
    condition = Column(String(32), nullable=True)
    # True when the trap's class is filtered out (Settings > Alarm filter):
    # kept as history, but it opened no incident.
    filtered = Column(Boolean, nullable=True)

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
    # Superseded by resolved_by/resolution_reason below - still written
    # (it's NOT NULL in existing databases) but no longer read.
    resolved_manually = Column(Boolean, default=False, nullable=False)
    # Workflow stage + ownership - see IncidentStage and
    # app.incidents.service. All nullable so add_missing_columns() can add
    # them to an existing database; rows from before they existed are
    # backfilled by app.incidents.service.backfill_stages.
    stage = Column(Enum(IncidentStage, native_enum=False, length=16), nullable=True, index=True)
    stage_changed_at = Column(DateTime(timezone=True), nullable=True)
    # From the trap that opened it (critical/warning/info - see oid_map).
    severity = Column(String(16), nullable=True)
    # "P1" (most urgent) .. "P4" - see app.incidents.service.compute_priority.
    priority = Column(String(2), nullable=True, index=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    # When the device itself reported the condition gone (linkUp,
    # isisAdjacencyUp, ...) while auto-heal is off - the incident stays open
    # for an engineer to confirm, but the router is shown as recovered.
    # Reset if the condition comes back. See app.incidents.service.clear.
    device_cleared_at = Column(DateTime(timezone=True), nullable=True)
    # "system", or the username of whoever resolved it.
    resolved_by = Column(String(64), nullable=True)
    resolution_reason = Column(Enum(ResolutionReason, native_enum=False, length=32), nullable=True)
    resolution_note = Column(String(255), nullable=True)
    # Common-Alarm-Model-style layer, derived automatically from
    # incident_type (see INCIDENT_LAYER above) - used by app.correlation to
    # rank an L1 (physical) alarm above any L3 (control-plane) symptom on
    # the same link.
    layer = Column(Enum(AlarmLayer), nullable=False, default=_default_incident_layer, index=True)
    # Set only for L1 incidents that stem from a specific fiber link (see
    # app.fiber_faults) - lets root-cause lookup walk "this peering has an
    # open physical fault" without guessing which endpoint router owns it.
    peering_id = Column(Integer, ForeignKey("bgp_peerings.id"), nullable=True, index=True)
    # Set when this (L3) incident is correlated with an L1 incident on a
    # peering it belongs to, whichever of the two opened first (see
    # app.correlation, app.incidents.service) - marks it as a symptom
    # rather than an independent problem, so it's excluded from auto-heal
    # and suppressed from the top-level view.
    root_cause_incident_id = Column(Integer, ForeignKey("incidents.id"), nullable=True, index=True)
    # See AlarmClass. NULL on incidents opened before classes existed.
    alarm_class = Column(Enum(AlarmClass, native_enum=False, length=16), nullable=True, index=True)
    # Optical condition code (LOS-P, LOS-O, LOF, ...) - part of the dedup
    # key, so LOS and LOF on the same transponder trunk stay separate. On a
    # root cause the corridor engine inferred, the diagnosis instead
    # (CABLE-CUT, FIBER-CUT, AMP-FAIL, ...) - see app.rca.
    condition = Column(String(32), nullable=True)
    # Set on root causes inferred by app.rca: the corridor, and which fault
    # this is ("span:4", "amp:12:AZ", ...) - stable while the diagnosis is
    # refined (a fiber cut that turns out to be a cable cut keeps its key).
    corridor_id = Column(Integer, ForeignKey("corridors.id"), nullable=True, index=True)
    rca_key = Column(String(64), nullable=True)
    # For a peering-scoped fiber cut: which span of the run lost signal, as
    # reported by the alarm (FIBER_SEGMENT_OID) - see app.fault_location.
    fault_segment_index = Column(Integer, nullable=True)

    router = relationship("Router", back_populates="incidents")
    trap_events = relationship("TrapEvent", back_populates="incident")
    remediation_actions = relationship("RemediationAction", back_populates="incident", cascade="all, delete-orphan")
    peering = relationship("BgpPeering")
    root_cause = relationship("Incident", remote_side=[id], back_populates="symptomatic_incidents")
    symptomatic_incidents = relationship("Incident", back_populates="root_cause")
    # selectin, not joined: the sweeper locks incident rows FOR UPDATE, which
    # Postgres refuses on the nullable side of an outer join.
    owner = relationship("User", lazy="selectin")

    @property
    def owner_username(self) -> str | None:
        return self.owner.username if self.owner else None


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
    # Set when this link's physical path is a modelled optical corridor
    # (see Corridor) rather than the abstract evenly-spaced repeater run
    # repeater_count describes.
    corridor_id = Column(Integer, ForeignKey("corridors.id"), nullable=True, index=True)

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


# --- Optical corridor topology ---------------------------------------------
#
# A corridor is one DWDM line system between two POPs, modelled the way the
# signal actually travels so a fault can be located from *where along the
# path* alarms start (see app.corridors, app.topology_graph):
#
#   router port -> transponder client -> transponder trunk (one wavelength)
#     -> terminal mux -> booster -> span 1 -> ILA amp -> span 2 -> ...
#     -> far terminal preamp -> demux -> far transponder trunk -> client -> router
#
# Everything is directional: "AZ" runs from the corridor's site_a to site_z,
# "ZA" back. Devices are `routers` rows with an element_role; written by
# POST /api/corridors/import from a corridor spec (seed/corridors/*.json).


class Site(Base):
    """A physical location: a POP (routers + transponder + terminal) or an
    in-line amplifier hut."""

    __tablename__ = "sites"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(32), nullable=False, unique=True, index=True)
    name = Column(String(128), nullable=False)
    kind = Column(String(16), nullable=False)  # "pop" | "ila"
    city = Column(String(128), nullable=True)
    country = Column(String(64), nullable=True)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)


class Corridor(Base):
    __tablename__ = "corridors"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(64), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    site_a_id = Column(Integer, ForeignKey("sites.id"), nullable=False)
    site_z_id = Column(Integer, ForeignKey("sites.id"), nullable=False)
    # JSON list of strings: what the spec author had to assume (missing or
    # contradictory source data), kept so the UI/API can show it.
    assumptions = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    site_a = relationship("Site", foreign_keys=[site_a_id])
    site_z = relationship("Site", foreign_keys=[site_z_id])
    spans = relationship("FiberSpan", back_populates="corridor", order_by="FiberSpan.seq")
    paths = relationship("OpticalPath", back_populates="corridor", order_by="OpticalPath.name")
    circuits = relationship("ClientCircuit", back_populates="corridor", order_by="ClientCircuit.id")


class Card(Base):
    """One card in a chassis slot. `function` is what the card does on this
    path (mux, booster, preamp, demux, line_amp, osc, transponder) - the
    same card type plays different roles in different places (an OPT-AMP-C
    is a preamp in a terminal but a line amp in an ILA). `direction` is the
    direction of travel it amplifies/multiplexes (AZ/ZA), NULL if both/none;
    for an OSC card it's the side of the site it faces instead ("A"/"Z")."""

    __tablename__ = "cards"
    __table_args__ = (UniqueConstraint("device_id", "slot", name="uq_card_device_slot"),)

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("routers.id"), nullable=False, index=True)
    slot = Column(Integer, nullable=False)
    card_type = Column(String(32), nullable=False)
    function = Column(String(16), nullable=True)
    direction = Column(String(2), nullable=True)

    device = relationship("Router")
    ports = relationship("Port", back_populates="card")


class Port(Base):
    """A physical port. kind: router_eth | client | trunk | line_in |
    line_out | com_in | com_out | add | drop | osc_in | osc_out."""

    __tablename__ = "ports"
    __table_args__ = (UniqueConstraint("device_id", "name", name="uq_port_device_name"),)

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("routers.id"), nullable=False, index=True)
    card_id = Column(Integer, ForeignKey("cards.id"), nullable=True, index=True)
    name = Column(String(64), nullable=False)
    kind = Column(String(16), nullable=False)
    direction = Column(String(2), nullable=True)
    wavelength_nm = Column(Float, nullable=True)

    device = relationship("Router")
    card = relationship("Card", back_populates="ports")


class FiberSpan(Base):
    """One rented dark-fiber span between two adjacent sites of a corridor
    (a fiber pair: one fiber per direction). seq 1 starts at site_a."""

    __tablename__ = "fiber_spans"
    __table_args__ = (UniqueConstraint("corridor_id", "seq", name="uq_span_corridor_seq"),)

    id = Column(Integer, primary_key=True, index=True)
    corridor_id = Column(Integer, ForeignKey("corridors.id"), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    from_device_id = Column(Integer, ForeignKey("routers.id"), nullable=False)  # the A-side element
    to_device_id = Column(Integer, ForeignKey("routers.id"), nullable=False)  # the Z-side element
    provider = Column(String(128), nullable=True)
    a_end = Column(String(64), nullable=True)  # provider's label for the A-side termination
    b_end = Column(String(64), nullable=True)
    length_km = Column(Float, nullable=True)
    provider_circuit_id = Column(String(128), nullable=True)

    corridor = relationship("Corridor", back_populates="spans")
    from_device = relationship("Router", foreign_keys=[from_device_id])
    to_device = relationship("Router", foreign_keys=[to_device_id])


class OpticalPath(Base):
    """An ordered route through the corridor in one direction. kind "och" is
    one wavelength from transponder trunk to transponder trunk; kind "osc"
    is the supervisory channel over a single span (OSC card to OSC card) -
    losing both on the same span input is what tells a fiber cut apart from
    an amplifier failure."""

    __tablename__ = "optical_paths"
    __table_args__ = (UniqueConstraint("corridor_id", "name", name="uq_path_corridor_name"),)

    id = Column(Integer, primary_key=True, index=True)
    corridor_id = Column(Integer, ForeignKey("corridors.id"), nullable=False, index=True)
    name = Column(String(64), nullable=False)  # e.g. "OCH-1560.61-AZ", "OSC-S3-AZ"
    kind = Column(String(8), nullable=False)  # "och" | "osc"
    direction = Column(String(2), nullable=False)  # "AZ" | "ZA"
    wavelength_nm = Column(Float, nullable=True)

    corridor = relationship("Corridor", back_populates="paths")
    hops = relationship("PathHop", back_populates="path", order_by="PathHop.seq", cascade="all, delete-orphan")


class PathHop(Base):
    """One step of an OpticalPath: either a port or a fiber span."""

    __tablename__ = "path_hops"
    __table_args__ = (UniqueConstraint("path_id", "seq", name="uq_hop_path_seq"),)

    id = Column(Integer, primary_key=True, index=True)
    path_id = Column(Integer, ForeignKey("optical_paths.id"), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    port_id = Column(Integer, ForeignKey("ports.id"), nullable=True, index=True)
    span_id = Column(Integer, ForeignKey("fiber_spans.id"), nullable=True, index=True)

    path = relationship("OpticalPath", back_populates="hops")
    port = relationship("Port")
    span = relationship("FiberSpan")


class ClientCircuit(Base):
    """One end-to-end client service: router port -> transponder client at
    the A end, carried on the wavelength `wavelength_nm` (its AZ and ZA
    OpticalPaths), out of the far transponder client into the far router
    port. The router ports are also the member interfaces of the L3 link's
    bundle (see BgpPeering.corridor_id), which is how an optical fault
    reaches the IS-IS adjacencies it takes down."""

    __tablename__ = "client_circuits"

    id = Column(Integer, primary_key=True, index=True)
    corridor_id = Column(Integer, ForeignKey("corridors.id"), nullable=False, index=True)
    wavelength_nm = Column(Float, nullable=False)
    a_router_port_id = Column(Integer, ForeignKey("ports.id"), nullable=False)
    a_client_port_id = Column(Integer, ForeignKey("ports.id"), nullable=False)
    z_client_port_id = Column(Integer, ForeignKey("ports.id"), nullable=False)
    z_router_port_id = Column(Integer, ForeignKey("ports.id"), nullable=False)
    peering_id = Column(Integer, ForeignKey("bgp_peerings.id"), nullable=True, index=True)

    corridor = relationship("Corridor", back_populates="circuits")
    a_router_port = relationship("Port", foreign_keys=[a_router_port_id])
    a_client_port = relationship("Port", foreign_keys=[a_client_port_id])
    z_client_port = relationship("Port", foreign_keys=[z_client_port_id])
    z_router_port = relationship("Port", foreign_keys=[z_router_port_id])
    peering = relationship("BgpPeering")


class User(Base):
    """A login account for the web UI/API - see app.auth for password
    hashing, TOTP verification and the signed bearer tokens issued at
    login. session_epoch is baked into every token and bumped on password
    or OTP resets, so those revoke any session already handed out."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(Enum(UserRole), default=UserRole.VIEWER, nullable=False)
    otp_secret = Column(String(64), nullable=True)
    session_epoch = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AppSetting(Base):
    """Small persistent key/value settings that have to survive a restart -
    e.g. `seed_demo_fleet`, cleared by "Delete entire fleet" (see
    api/simulation.py) so the simulator doesn't re-seed its demo fleet the
    next time it starts."""

    __tablename__ = "app_settings"

    key = Column(String(64), primary_key=True)
    value = Column(String(255), nullable=False)


class MibModule(Base):
    """A MIB file uploaded in the "MIBs & Traps" tab for some appliance
    (Juniper, Huawei, Nokia, an optical shelf, ...). The source is kept so
    every import can re-resolve all uploaded modules together - a vendor's
    SMI root uploaded later fixes the traps of modules uploaded before it.
    See app.snmp.mib_registry."""

    __tablename__ = "mib_modules"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), nullable=False, unique=True, index=True)
    appliance = Column(String(128), nullable=False, default="")
    filename = Column(String(255), nullable=False)
    # Big vendor MIBs exceed MySQL's 64 KB TEXT; Postgres/SQLite TEXT is unbounded.
    source = Column(Text().with_variant(MEDIUMTEXT(), "mysql", "mariadb"), nullable=False)
    trap_count = Column(Integer, nullable=False, default=0)
    unresolved_count = Column(Integer, nullable=False, default=0)
    # JSON list of {"name", "missing"} for traps whose OID couldn't be resolved.
    unresolved = Column(Text, nullable=True)
    uploaded_by = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    traps = relationship("MibTrap", back_populates="module", cascade="all, delete-orphan")


class MibTrap(Base):
    """One trap defined by an uploaded MIB, with its resolved OID.
    Regenerated from MibModule.source on every import/delete."""

    __tablename__ = "mib_traps"

    id = Column(Integer, primary_key=True, index=True)
    oid = Column(String(255), nullable=False, unique=True, index=True)
    name = Column(String(128), nullable=False, index=True)
    module_id = Column(Integer, ForeignKey("mib_modules.id", ondelete="CASCADE"), nullable=False, index=True)
    objects = Column(Text, nullable=True)  # JSON list of varbind object names
    status = Column(String(32), nullable=False, default="current")
    description = Column(Text, nullable=True)

    module = relationship("MibModule", back_populates="traps")


class TrapRuleAction(str, enum.Enum):
    RAISE = "raise"  # open (or bump) an incident of incident_type
    CLEAR = "clear"  # resolve the open incident of incident_type on the same router/interface
    ALARM = "alarm"  # store as an alarm only, under this rule's name/severity


class TrapRule(Base):
    """A custom trap: a user-defined OID -> name/severity/action rule,
    created from scratch or from an imported/catalogued trap. Consulted by
    app.snmp.classifier for any OID the built-in TRAP_OID_MAP doesn't
    handle (built-in OIDs can't be overridden). See app.snmp.mib_registry."""

    __tablename__ = "trap_rules"

    id = Column(Integer, primary_key=True, index=True)
    oid = Column(String(255), nullable=False, unique=True, index=True)
    name = Column(String(64), nullable=False)  # becomes TrapEvent.trap_name (64 chars)
    description = Column(Text, nullable=True)
    appliance = Column(String(128), nullable=False, default="")
    action = Column(Enum(TrapRuleAction), nullable=False, default=TrapRuleAction.ALARM)
    incident_type = Column(Enum(IncidentType), nullable=True)  # required for RAISE/CLEAR
    severity = Column(String(16), nullable=False, default="warning")
    # Optional varbind OID (or OID prefix, e.g. ifName 1.3.6.1.2.1.31.1.1.1.1)
    # whose value names the interface the trap is about.
    interface_varbind = Column(String(255), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    created_by = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
