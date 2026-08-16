from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Team, TeamKind
from app.schemas import TeamIn, TeamOut

router = APIRouter(prefix="/api/teams", tags=["teams"])


@router.get("", response_model=list[TeamOut])
def list_teams(kind: TeamKind | None = None, db: Session = Depends(get_db)):
    query = db.query(Team)
    if kind is not None:
        query = query.filter(Team.kind == kind)
    return query.order_by(Team.kind, Team.name).all()


@router.post("", response_model=TeamOut)
def create_team(payload: TeamIn, db: Session = Depends(get_db)):
    team = Team(kind=payload.kind, name=payload.name, email=payload.email)
    db.add(team)
    db.commit()
    db.refresh(team)
    return team


@router.put("/{team_id}", response_model=TeamOut)
def update_team(team_id: int, payload: TeamIn, db: Session = Depends(get_db)):
    team = db.query(Team).filter(Team.id == team_id).first()
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")
    team.kind = payload.kind
    team.name = payload.name
    team.email = payload.email
    db.commit()
    db.refresh(team)
    return team


@router.delete("/{team_id}")
def delete_team(team_id: int, db: Session = Depends(get_db)):
    team = db.query(Team).filter(Team.id == team_id).first()
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")
    db.delete(team)
    db.commit()
    return {"ok": True}
