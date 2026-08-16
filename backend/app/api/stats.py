from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Incident, IncidentStatus, Router, RouterStatus, RouterType
from app.schemas import StatsSummary

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/summary", response_model=StatsSummary)
def summary(db: Session = Depends(get_db)):
    """Scoped to primary (backbone) routers - these are the telco's own
    fleet and what the dashboard is meant to represent. Customer CPE detail
    lives on the map/router list instead, so it doesn't drown out the
    backbone signal here."""
    primary_routers = db.query(Router).filter(Router.router_type == RouterType.PRIMARY)
    total_routers = primary_routers.count()
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

    return StatsSummary(
        total_routers=total_routers,
        total_customer_routers=total_customer_routers,
        routers_up=routers_up,
        routers_down=routers_down,
        routers_flapping=routers_flapping,
        open_incidents=open_incidents,
        incidents_by_type=incidents_by_type,
    )
