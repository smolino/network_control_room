from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import (
    AlarmClass,
    AlarmLayer,
    BgpSessionStatus,
    BundleStatus,
    ElementRole,
    IncidentStage,
    IncidentStatus,
    IncidentType,
    NotificationStatus,
    RemediationActionType,
    RemediationStatus,
    ResolutionReason,
    RouterStatus,
    RouterType,
    TeamKind,
    UserRole,
)


class RouterIn(BaseModel):
    hostname: str
    mgmt_ip: str
    asn: int | None = None
    router_type: RouterType = RouterType.PRIMARY
    # For customer routers only: the mgmt IP of the primary they're
    # single-homed to. Resolved to parent_router_id at seed time, once the
    # parent row exists - so primaries must be seeded before their customers.
    parent_mgmt_ip: str | None = None
    vendor: str = "Cisco"
    model: str | None = None
    site_name: str | None = None
    country: str | None = None
    city: str | None = None
    latitude: float
    longitude: float


class RouterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    hostname: str
    mgmt_ip: str
    asn: int | None = None
    router_type: RouterType
    parent_router_id: int | None = None
    vendor: str
    model: str | None = None
    site_name: str | None = None
    country: str | None = None
    city: str | None = None
    latitude: float
    longitude: float
    status: RouterStatus
    last_seen_at: datetime | None = None
    # True while the router has an open incident in triage - one nobody has
    # picked up yet (see app.incidents.service.needs_attention_router_ids).
    # The UI blinks the router's marker until it's acknowledged or clears.
    needs_attention: bool = False
    # True while an open incident on this router has been cleared by the
    # device itself (e.g. linkUp) but not yet resolved by an engineer - the
    # map shows it as recovered, pending review.
    device_cleared: bool = False
    # This router's IS-IS NET (Network Entity Title) - primaries only, since
    # customer CPE doesn't run the backbone IGP. See app.bundles.isis_net.
    isis_net: str | None = None
    # router (also when NULL) | transponder | terminal | ila - see
    # models.ElementRole. Optical elements are L1 only: no IS-IS/BGP.
    element_role: ElementRole | None = None
    site_id: int | None = None


class RouterNearestOut(RouterOut):
    distance_km: float


class TrapEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    router_id: int
    received_at: datetime
    oid: str
    trap_name: str
    interface_name: str | None = None
    severity: str
    incident_id: int | None = None
    alarm_class: AlarmClass | None = None
    condition: str | None = None
    filtered: bool | None = None


class RemediationSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    action_type: RemediationActionType
    status: RemediationStatus
    summary: str | None = None
    backup_id: int | None = None


class IncidentImpact(BaseModel):
    """Current blast radius of an open, service-affecting incident - see
    app.impact. customers_down counts CPE that has actually lost service
    (its primary is cut off from the backbone, or the CPE itself is down);
    the rest of customer_count is degraded/at risk only."""

    level: str  # "outage" | "degraded"
    primary_router_ids: list[int]
    customer_count: int
    customers_down: int


class IncidentImpactDetail(IncidentImpact):
    # Every router in the blast radius (primaries + customers) - only on
    # GET /api/incidents/{id}/impact, for the map highlight.
    router_ids: list[int]


class FaultPoint(BaseModel):
    kind: str  # "repeater" | "router"
    label: str  # "R3", or the router's hostname at either end of the run
    repeater_index: int | None = None
    km_from_a: float
    latitude: float
    longitude: float


class FaultLocation(BaseModel):
    """Which span of a peering's fiber run a fault is in - see
    app.fault_location."""

    peering_id: int
    router_a: str
    router_b: str
    distance_km: float | None = None
    repeater_count: int
    segment_index: int
    segment_count: int
    segment_length_km: float
    start: FaultPoint
    end: FaultPoint
    repeaters_to_check: list[str]


class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    router_id: int
    incident_type: IncidentType
    interface_name: str | None = None
    status: IncidentStatus
    opened_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    trap_count: int
    description: str | None = None
    remediation: RemediationSummary | None = None
    # Workflow - see models.IncidentStage and app.incidents.service.
    stage: IncidentStage | None = None
    stage_changed_at: datetime | None = None
    severity: str | None = None
    priority: str | None = None
    owner_user_id: int | None = None
    owner_username: str | None = None
    acknowledged_at: datetime | None = None
    device_cleared_at: datetime | None = None
    resolved_by: str | None = None
    resolution_reason: ResolutionReason | None = None
    resolution_note: str | None = None
    # Lets the UI file a live update under the right list without looking
    # the router up first.
    router_type: RouterType | None = None
    # Open incidents currently suppressed as symptoms of this one.
    symptom_count: int = 0
    # Common-Alarm-Model-style layer + topology-based root-cause linkage -
    # see app.correlation. peering_id is set only for L1 incidents that
    # stem from a specific fiber link (app.fiber_faults); root_cause_
    # incident_id is set on an L3 incident once it's been linked as a
    # symptom of an open L1 incident on the same peering.
    layer: AlarmLayer
    # transport | logical | equipment | management - see models.AlarmClass.
    alarm_class: AlarmClass | None = None
    # Optical condition code (LOS-P, LOS-O, OPWR-LFAIL, ...), if any - or,
    # on a root cause app.rca inferred, the diagnosis (CABLE-CUT, ...).
    condition: str | None = None
    # Set on root causes inferred on a modelled corridor (see app.rca).
    corridor_id: int | None = None
    rca_key: str | None = None
    peering_id: int | None = None
    root_cause_incident_id: int | None = None
    # None for resolved and non-service-affecting incidents - see app.impact.
    impact: IncidentImpact | None = None
    # Fiber cuts only: the faulty span and the repeaters bounding it.
    fault_segment_index: int | None = None
    fault_location: FaultLocation | None = None


class ResolveIn(BaseModel):
    # One of models.USER_RESOLUTION_REASONS.
    reason: ResolutionReason
    note: str | None = None


class BulkResolveIn(ResolveIn):
    incident_ids: list[int]


class RemediationActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    incident_id: int
    router_id: int
    backup_id: int | None = None
    incident_type: str
    action_type: RemediationActionType
    status: RemediationStatus
    started_at: datetime
    finished_at: datetime | None = None
    summary: str | None = None
    log: str | None = None


class RouterConfigBackupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    router_id: int
    incident_id: int | None = None
    taken_at: datetime
    reason: str | None = None
    config_text: str


class BgpPeerIn(BaseModel):
    router_a_mgmt_ip: str
    router_b_mgmt_ip: str
    distance_km: float
    repeater_count: int


class InterfaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    bandwidth_mbps: int
    isis_adjacency_up: bool


class InterfaceBundleOut(BaseModel):
    id: int
    name: str
    status: BundleStatus
    members: list[InterfaceOut]
    # Summed over currently-up members only, computed at serialization time
    # rather than persisted (see api/bgp.py:_to_bundle_out).
    total_bandwidth_mbps: int


class BgpPeeringOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    router_a_id: int
    router_b_id: int
    status: BgpSessionStatus
    last_changed_at: datetime
    # Fiber run length and SMF regenerator count for this link - see
    # models.BgpPeering.
    distance_km: float | None = None
    repeater_count: int = 0
    # Set while app.fiber_faults' chaos generator has knocked out the
    # interior span between the segment_index-th and (segment_index+1)-th
    # repeater on this link (1-based, both interior points) - None the rest
    # of the time. In-memory/computed at serialize time, not persisted -
    # see api/bgp.py:_to_peering_out.
    active_fault_segment: int | None = None
    # Id of this peering's currently-open L1 (fiber) incident, if any - lets
    # the frontend distinguish "genuinely faulted right now" from a resolved
    # fault whose segment marker just hasn't updated yet. In-memory/computed
    # at serialize time, like active_fault_segment - see api/bgp.py:_to_peering_out.
    open_l1_incident_id: int | None = None
    # Router ids for the surviving path traffic is rerouted over while this
    # peering is down (router_a_id ... router_b_id inclusive), or None if
    # it's established or no alternate path exists in the current mesh.
    reroute_path: list[int] | None = None
    # This peering's two interface bundles (one per side) - see
    # app/bundles.py. None until POST /api/bgp/seed has run.
    bundle_a: InterfaceBundleOut | None = None
    bundle_b: InterfaceBundleOut | None = None
    # Set when this link rides a modelled optical corridor (see
    # models.Corridor, GET /api/corridors) - its physical path is then the
    # corridor's sites/spans, and repeater_count is just its ILA count.
    corridor_id: int | None = None


class RouterModelIn(BaseModel):
    vendor: str
    model: str


class RouterModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    vendor: str
    model: str


class TeamIn(BaseModel):
    kind: TeamKind
    name: str
    email: str


class TeamOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: TeamKind
    name: str
    email: str
    created_at: datetime


class IncidentAnalysisOut(BaseModel):
    incident_id: int
    description: str
    suggested_solution: str
    recommended_team_kind: TeamKind
    subject: str


class NotifyTeamIn(BaseModel):
    team_id: int
    subject: str
    body: str


class IncidentNotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    incident_id: int
    team_id: int
    subject: str
    body: str
    status: NotificationStatus
    error: str | None = None
    sent_at: datetime


class OpenTypeCount(BaseModel):
    incident_type: str
    layer: str  # "L1" or "L3"
    count: int


class StatsTimeseries(BaseModel):
    """Per-bucket telemetry for the Dashboard's chart strip - see
    app.api.stats.timeseries. All lists line up with `buckets` (each a
    bucket's start time, UTC)."""

    window_minutes: int
    bucket_minutes: int
    buckets: list[datetime]
    traps: list[int]  # SNMP traps received
    incidents_opened: list[int]
    open_incidents: list[int]  # open at the end of each bucket
    autoheal_actions: list[int]  # remediation attempts that ran (not skipped)
    autoheal_failed: list[int]
    # Currently open incidents, fleet-wide, by type - most first.
    open_by_type: list[OpenTypeCount]


class StatsSummary(BaseModel):
    total_routers: int
    total_customer_routers: int
    routers_up: int
    routers_down: int
    routers_flapping: int
    open_incidents: int
    incidents_by_type: dict[str, int]
    # Distinct customer routers inside the blast radius of any open incident
    # (see app.impact), and how many of those have actually lost service.
    customers_impacted: int = 0
    customers_down: int = 0
    # Fleet-wide open incidents waiting for someone to pick them up.
    incidents_in_triage: int = 0


class LoginIn(BaseModel):
    username: str
    password: str
    otp_code: str | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: UserRole
    otp_enabled: bool = False
    created_at: datetime | None = None

    @classmethod
    def from_user(cls, user) -> "UserOut":
        return cls(
            id=user.id, username=user.username, role=user.role, otp_enabled=bool(user.otp_secret), created_at=user.created_at
        )


class LoginOut(BaseModel):
    token: str
    user: UserOut


class UserCreateIn(BaseModel):
    username: str
    password: str
    role: UserRole = UserRole.VIEWER


class UserUpdateIn(BaseModel):
    role: UserRole | None = None
    password: str | None = None
    # Turns off two-factor auth for someone who lost their authenticator.
    reset_otp: bool = False


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str


class OtpSetupOut(BaseModel):
    secret: str
    otpauth_uri: str


class OtpEnableIn(BaseModel):
    secret: str
    code: str


class OtpDisableIn(BaseModel):
    code: str


# --- Optical corridors (see models.Corridor, app.corridors) -----------------
#
# CorridorSpec is the import format (seed/corridors/*.json): devices are
# referred to by hostname and sites by code inside the spec, resolved to
# ids by the importer.


class SiteSpec(BaseModel):
    code: str
    name: str
    kind: str  # "pop" | "ila"
    city: str | None = None
    country: str | None = None
    latitude: float
    longitude: float


class CardSpec(BaseModel):
    slot: int
    type: str
    # mux | demux | booster | preamp | line_amp | osc - None for a card that
    # isn't on the optical path (it's recorded, but gets no ports).
    function: str | None = None
    # Direction of travel for amps/mux/demux ("AZ"/"ZA"); for osc, the side
    # of the site the card faces ("A"/"Z"). None for a standby OSC card.
    direction: str | None = None


class PortSpec(BaseModel):
    name: str
    kind: str  # router_eth | client | trunk
    wavelength_nm: float | None = None


class DeviceSpec(BaseModel):
    hostname: str
    mgmt_ip: str
    role: ElementRole
    vendor: str = "Cisco"
    model: str | None = None
    site: str  # SiteSpec.code
    cards: list[CardSpec] = []
    ports: list[PortSpec] = []


class SpanSpec(BaseModel):
    seq: int
    from_device: str  # hostname, A side
    to_device: str  # hostname, Z side
    provider: str | None = None
    a_end: str | None = None
    b_end: str | None = None
    length_km: float | None = None
    provider_circuit_id: str | None = None


class CircuitSpec(BaseModel):
    wavelength_nm: float
    a_router: str
    a_router_port: str
    a_transponder: str
    a_client_port: str
    z_transponder: str
    z_client_port: str
    z_router: str
    z_router_port: str


class L3LinkSpec(BaseModel):
    """An L3 (IS-IS/BGP) link between two routers carried by this corridor;
    its bundle members are the router ports of every circuit between them."""

    router_a: str
    router_z: str
    bundle_a: str = "Bundle-Ether1"
    bundle_z: str = "Bundle-Ether1"
    member_bandwidth_mbps: int = 100000


class CorridorSpec(BaseModel):
    name: str
    description: str | None = None
    assumptions: list[str] = []
    sites: list[SiteSpec]  # ordered from site A to site Z
    devices: list[DeviceSpec]
    spans: list[SpanSpec]
    circuits: list[CircuitSpec] = []
    l3_links: list[L3LinkSpec] = []


class CorridorImportOut(BaseModel):
    corridor_id: int
    devices_created: list[str]
    devices_updated: list[str]
    spans: int
    optical_paths: int
    client_circuits: int
    l3_peering_ids: list[int]
    # Peerings removed because one side is an optical element (?prune=true),
    # or - without prune - found and left in place.
    l1_peerings_removed: list[int] = []
    l1_peerings_found: list[int] = []
    warnings: list[str] = []


class RoutePointOut(BaseModel):
    site_code: str
    name: str
    kind: str
    latitude: float
    longitude: float
    device_ids: list[int]


class SpanOut(BaseModel):
    id: int
    seq: int
    from_device_id: int
    from_hostname: str
    to_device_id: int
    to_hostname: str
    provider: str | None = None
    a_end: str | None = None
    b_end: str | None = None
    length_km: float | None = None
    km_from_a: float | None = None
    provider_circuit_id: str | None = None


class PathHopOut(BaseModel):
    seq: int
    port_id: int | None = None
    device_id: int | None = None
    hostname: str | None = None
    port: str | None = None
    port_kind: str | None = None
    span_seq: int | None = None


class OpticalPathOut(BaseModel):
    id: int
    name: str
    kind: str
    direction: str
    wavelength_nm: float | None = None
    hops: list[PathHopOut] = []


class CircuitOut(BaseModel):
    id: int
    wavelength_nm: float
    a_router: str
    a_router_port: str
    a_client: str
    z_client: str
    z_router: str
    z_router_port: str
    peering_id: int | None = None


class CorridorOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    assumptions: list[str] = []
    site_a: str
    site_z: str
    length_km: float | None = None
    route: list[RoutePointOut]
    spans: list[SpanOut]
    peering_ids: list[int] = []
    paths: list[OpticalPathOut] = []
    circuits: list[CircuitOut] = []


class DiagnosisOut(BaseModel):
    """One fault the corridor engine infers from the active alarms."""

    kind: str
    key: str
    device_id: int
    hostname: str
    summary: str
    span_seq: int | None = None
    directions: list[str] = []
    evidence: list[dict]  # {"device_id", "hostname", "port", "condition"}
    explains: int  # how many (device, interface) pairs it accounts for
    circuit_ids: list[int] = []
    peering_ids: list[int] = []
    root_incident_id: int | None = None


class CarriedOut(BaseModel):
    """What a span or a device carries - everything that goes down with it.
    Answered from the Neo4j topology graph (see app.topology_graph)."""

    optical_paths: list[str]
    wavelengths_nm: list[float]
    circuit_ids: list[int]
    router_interfaces: list[dict]  # {"router_id", "hostname", "interface"}
    peering_ids: list[int]
