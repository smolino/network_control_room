from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import RouterConfigBackup
from app.schemas import RouterConfigBackupOut

router = APIRouter(prefix="/api/backups", tags=["backups"])


@router.get("/{backup_id}", response_model=RouterConfigBackupOut)
def get_backup(backup_id: int, db: Session = Depends(get_db)):
    obj = db.query(RouterConfigBackup).filter(RouterConfigBackup.id == backup_id).first()
    if obj is None:
        raise HTTPException(status_code=404, detail="Backup not found")
    return obj
