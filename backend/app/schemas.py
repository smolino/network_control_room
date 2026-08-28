from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import (
    AlarmLayer,
    BgpSessionStatus,
    BundleStatus,
    IncidentStatus,
    IncidentType,
    NotificationStatus,
    RemediationActionType,
    RemediationStatus,
    RouterStatus,
    RouterType,
    TeamKind,
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
    # True when an open incident's auto-heal action came back FAILED - the
    # UI blinks the router's marker until it actually recovers.
    needs_attention: bool = False
    # This router's IS-IS NET (Network Entity Title) - primaries only, since
    # customer CPE doesn't run the backbone IGP. See app.bundles.isis_net.
    isis_net: str | None = None


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


class RemediationSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    action_type: RemediationActionType
    status: RemediationStatus
    summary: str | None = None
    backup_id: int | None = None


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
    resolved_manually: bool = False
    remediation: RemediationSummary | None = None
    # Common-Alarm-Model-style layer + topology-based root-cause linkage -
    # see app.correlation. peering_id is set only for L1 incidents that
    # stem from a specific fiber link (app.fiber_faults); root_cause_
    # incident_id is set on an L3 incident once it's been linked as a
    # symptom of an open L1 incident on the same peering.
    layer: AlarmLayer
    peering_id: int | None = None
    root_cause_incident_id: int | None = None


class BulkResolveIn(BaseModel):
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


class StatsSummary(BaseModel):
    total_routers: int
    total_customer_routers: int
    routers_up: int
    routers_down: int
    routers_flapping: int
    open_incidents: int
    incidents_by_type: dict[str, int]
