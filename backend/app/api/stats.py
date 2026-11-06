from bisect import bisect_right
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.db import get_db
from app.impact import impact_totals
from app.models import (
    ElementRole,
    Incident,
    IncidentStage,
    IncidentStatus,
    RemediationAction,
    RemediationStatus,
    Router,
    RouterStatus,
    RouterType,
    TrapEvent,
)
from app.schemas import OpenTypeCount, StatsSummary, StatsTimeseries

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/summary", response_model=StatsSummary)
def summary(db: Session = Depends(get_db)):
    """Scoped to primary (backbone) routers - these are the telco's own
    fleet and what the dashboard is meant to represent. Customer CPE detail
    lives on the map/router list instead, so it doesn't drown out the
    backbone signal here."""
    is_router = or_(Router.element_role.is_(None), Router.element_role == ElementRole.ROUTER)
    primary_routers = db.query(Router).filter(Router.router_type == RouterType.PRIMARY, is_router)
    total_routers = primary_routers.count()
    optical = db.query(Router).filter(~is_router)
    total_optical_elements = optical.count()
    optical_elements_alarmed = (
        db.query(Incident.router_id)
        .join(Router, Incident.router_id == Router.id)
        .filter(~is_router, Incident.status == IncidentStatus.OPEN)
        .distinct()
        .count()
    )
    routers_up = primary_routers.filter(Router.status == RouterStatus.UP).count()
    routers_down = primary_routers.filter(Router.status == RouterStatus.DOWN).count()
    routers_flapping = primary_routers.filter(Router.status == RouterStatus.FLAPPING).count()
    total_customer_routers = db.query(Router).filter(Router.router_type == RouterType.CUSTOMER).count()

    open_incidents = (
        db.query(Incident)
        .join(Router, Incident.router_id == Router.id)
        .filter(Incident.status == IncidentStatus.OPEN, Router.router_type == RouterType.PRIMARY)
        .count()
    )

    rows = (
        db.query(Incident.incident_type, func.count(Incident.id))
        .join(Router, Incident.router_id == Router.id)
        .filter(Router.router_type == RouterType.PRIMARY)
        .group_by(Incident.incident_type)
        .all()
    )
    incidents_by_type = {incident_type.value: count for incident_type, count in rows}
    # Fleet-wide, not primary-scoped: the whole point is customer impact.
    customers_impacted, customers_down = impact_totals(db)
    # Fleet-wide too: anything waiting on a human, wherever it is.
    incidents_in_triage = (
        db.query(Incident).filter(Incident.status == IncidentStatus.OPEN, Incident.stage == IncidentStage.TRIAGE).count()
    )

    return StatsSummary(
        total_routers=total_routers,
        total_optical_elements=total_optical_elements,
        optical_elements_alarmed=optical_elements_alarmed,
        total_customer_routers=total_customer_routers,
        routers_up=routers_up,
        routers_down=routers_down,
        routers_flapping=routers_flapping,
        open_incidents=open_incidents,
        incidents_by_type=incidents_by_type,
        customers_impacted=customers_impacted,
        customers_down=customers_down,
        incidents_in_triage=incidents_in_triage,
    )


# Window (minutes) -> bucket size (minutes): ~60-100 points per chart.
TIMESERIES_WINDOWS = {60: 1, 360: 5, 1440: 15}


def _utc(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything here is UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@router.get("/timeseries", response_model=StatsTimeseries)
def timeseries(
    window_minutes: int = Query(60, description="One of 60, 360, 1440"),
    db: Session = Depends(get_db),
):
    """Fleet-wide telemetry for the Dashboard charts, bucketed in Python so
    it works the same on SQLite and Postgres."""
    bucket_minutes = TIMESERIES_WINDOWS.get(window_minutes)
    if bucket_minutes is None:
        window_minutes, bucket_minutes = 60, 1
    bucket = timedelta(minutes=bucket_minutes)
    n = window_minutes // bucket_minutes

    now = datetime.now(timezone.utc)
    # Align to bucket boundaries so the points don't shift on every poll.
    epoch_min = int(now.timestamp() // 60)
    end = datetime.fromtimestamp((epoch_min - epoch_min % bucket_minutes) * 60, timezone.utc) + bucket
    start = end - bucket * n
    buckets = [start + bucket * k for k in range(n)]

    def histogram(times) -> list[int]:
        counts = [0] * n
        for t in times:
            t = _utc(t)
            if t is None or t < start or t >= end:
                continue
            counts[int((t - start) / bucket)] += 1
        return counts

    traps = histogram(t for (t,) in db.query(TrapEvent.received_at).filter(TrapEvent.received_at >= start))

    incident_rows = (
        db.query(Incident.opened_at, Incident.closed_at)
        .filter(Incident.opened_at < end, or_(Incident.closed_at.is_(None), Incident.closed_at >= start))
        .all()
    )
    incidents_opened = histogram(o for o, _ in incident_rows)
    opened_sorted = sorted(_utc(o) for o, _ in incident_rows if o is not None)
    closed_sorted = sorted(_utc(c) for _, c in incident_rows if c is not None)
    open_incidents = []
    for k in range(n):
        t = min(buckets[k] + bucket, now)
        open_incidents.append(bisect_right(opened_sorted, t) - bisect_right(closed_sorted, t))

    action_rows = (
        db.query(RemediationAction.started_at, RemediationAction.status)
        .filter(RemediationAction.started_at >= start, RemediationAction.status != RemediationStatus.SKIPPED)
        .all()
    )
    autoheal_actions = histogram(t for t, _ in action_rows)
    autoheal_failed = histogram(t for t, status in action_rows if status == RemediationStatus.FAILED)

    by_type = (
        db.query(Incident.incident_type, Incident.layer, func.count(Incident.id))
        .filter(Incident.status == IncidentStatus.OPEN)
        .group_by(Incident.incident_type, Incident.layer)
        .all()
    )
    open_by_type = [
        OpenTypeCount(incident_type=t.value, layer=layer.value, count=c)
        for t, layer, c in sorted(by_type, key=lambda row: -row[2])
    ]

    return StatsTimeseries(
        window_minutes=window_minutes,
        bucket_minutes=bucket_minutes,
        buckets=buckets,
        traps=traps,
        incidents_opened=incidents_opened,
        open_incidents=open_incidents,
        autoheal_actions=autoheal_actions,
        autoheal_failed=autoheal_failed,
        open_by_type=open_by_type,
    )
