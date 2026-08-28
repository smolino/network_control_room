from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
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


@router.put("/{router_model_id}", response_model=RouterModelOut)
def update_router_model(router_model_id: int, payload: RouterModelIn, db: Session = Depends(get_db)):
    """Edits an existing catalog entry in place - e.g. fixing a typo'd
    model name doesn't leave the wrong one behind for anyone who already
    picked it (Router.vendor/model are free-text, not FK'd to this table -
    see app.models.RouterModel - so existing routers keep whatever they
    were saved with; only the dropdown's own list changes)."""
    obj = db.query(RouterModel).filter(RouterModel.id == router_model_id).first()
    if obj is None:
        raise HTTPException(status_code=404, detail="Router model not found")

    vendor = payload.vendor.strip()
    model = payload.model.strip()
    obj.vendor = vendor
    obj.model = model
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"{vendor} {model} already exists") from None
    db.refresh(obj)
    return obj
