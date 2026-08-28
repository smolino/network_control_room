from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import RouterModel
from app.schemas import RouterModelIn, RouterModelOut

router = APIRouter(prefix="/api/router-models", tags=["router-models"])


@router.get("", response_model=list[RouterModelOut])
def list_router_models(db: Session = Depends(get_db)):
    return db.query(RouterModel).order_by(RouterModel.vendor, RouterModel.model).all()


@router.post("", response_model=RouterModelOut)
def create_router_model(payload: RouterModelIn, db: Session = Depends(get_db)):
    """Adds one (vendor, model) pair to the Add Fleet form's dropdown
    catalog. Idempotent on an exact (vendor, model) match, same as
    api/routers.py:seed_routers being a no-op for an mgmt_ip that already
    exists - resubmitting the form (e.g. a double click) just returns the
    existing row rather than erroring."""
    vendor = payload.vendor.strip()
    model = payload.model.strip()
    existing = db.query(RouterModel).filter(RouterModel.vendor == vendor, RouterModel.model == model).first()
    if existing:
        return existing

    obj = RouterModel(vendor=vendor, model=model)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj
