from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import TrapEvent
from app.schemas import TrapEventOut

router = APIRouter(prefix="/api/traps", tags=["traps"])


@router.get("", response_model=list[TrapEventOut])
def list_traps(limit: int = 200, offset: int = 0, db: Session = Depends(get_db)):
    return (
        db.query(TrapEvent)
        .order_by(TrapEvent.received_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
