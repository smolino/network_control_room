"""The "MIBs & Traps" tab: import MIBs for other appliances, browse their
traps alongside the bundled Cisco catalogue, and manage custom trap rules.
Reading needs the viewer role (applied in app.main); every change is
admin-only. See app.snmp.mib_registry for how imports and rules are
applied to incoming traps."""

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.auth import Principal, require_admin
from app.db import get_db
from app.incidents.service import is_incident_type
from app.models import IncidentType, MibModule, TrapRule, TrapRuleAction
from app.snmp import mib_registry, trap_catalog
from app.snmp.oid_map import TRAP_OID_MAP

router = APIRouter(prefix="/api/mibs", tags=["mibs"])

MAX_FILES = 50
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 9 * 1024 * 1024  # under the frontend nginx's 10m body limit
SEVERITIES = ["critical", "warning", "info"]
OID_RE = re.compile(r"^\.?\d+(\.\d+){2,}$")
# Varbind OIDs the simulator uses to identify routers - never a trap of their own.
RESERVED_PREFIX = "1.3.6.1.4.1.9.9.9999."


def _incident_types() -> list[IncidentType]:
    return sorted((t for t in IncidentType if is_incident_type(t)), key=lambda t: t.value)


# --- schemas ------------------------------------------------------------------


class MibFileIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content: str


class MibImportIn(BaseModel):
    appliance: str = Field(default="", max_length=128)
    files: list[MibFileIn] = Field(min_length=1, max_length=MAX_FILES)


class TrapRuleIn(BaseModel):
    oid: str
    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    appliance: str = Field(default="", max_length=128)
    action: TrapRuleAction = TrapRuleAction.ALARM
    incident_type: IncidentType | None = None
    severity: str = "warning"
    interface_varbind: str | None = None
    enabled: bool = True

    @field_validator("oid")
    @classmethod
    def _oid(cls, v: str) -> str:
        v = v.strip()
        if not OID_RE.match(v):
            raise ValueError("OID must be dotted numbers, e.g. 1.3.6.1.4.1.2636.4.1.1")
        return v.lstrip(".")

    @field_validator("interface_varbind")
    @classmethod
    def _varbind(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        if not v:
            return None
        if not OID_RE.match(v):
            raise ValueError("Interface varbind must be a dotted OID, e.g. 1.3.6.1.2.1.31.1.1.1.1 (ifName)")
        return v.lstrip(".")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name is required")
        return v

    @field_validator("severity")
    @classmethod
    def _severity(cls, v: str) -> str:
        if v not in SEVERITIES:
            raise ValueError(f"Severity must be one of {', '.join(SEVERITIES)}")
        return v


def _rule_out(r: TrapRule) -> dict:
    return {
        "id": r.id,
        "oid": r.oid,
        "name": r.name,
        "description": r.description or "",
        "appliance": r.appliance,
        "action": r.action.value,
        "incident_type": r.incident_type.value if r.incident_type else None,
        "severity": r.severity,
        "interface_varbind": r.interface_varbind,
        "enabled": r.enabled,
        "created_by": r.created_by,
        "updated_at": r.updated_at or r.created_at,
    }


def _builtin_out(oid: str) -> dict | None:
    known = TRAP_OID_MAP.get(oid)
    if not known:
        return None
    name, incident_type, severity = known
    return {"name": name, "incident_type": incident_type.value, "severity": severity}


# --- options / modules ----------------------------------------------------------


@router.get("/options")
def options(db: Session = Depends(get_db)):
    return {
        "incident_types": [t.value for t in _incident_types()],
        "severities": SEVERITIES,
        "actions": [a.value for a in TrapRuleAction],
        "cisco_catalog_size": trap_catalog.size(),
        "max_files": MAX_FILES,
        "max_file_bytes": MAX_FILE_BYTES,
        "max_total_bytes": MAX_TOTAL_BYTES,
        "cache_ttl_seconds": mib_registry.CACHE_TTL_SECONDS,
    }


@router.get("/modules")
def list_modules(db: Session = Depends(get_db)):
    modules = db.query(MibModule).order_by(MibModule.appliance, MibModule.name).all()
    return [mib_registry.module_summary(m, with_unresolved=True) for m in modules]


@router.post("/modules")
def import_modules(payload: MibImportIn, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)):
    total = 0
    for f in payload.files:
        size = len(f.content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail=f"{f.filename} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
        total += size
    if total > MAX_TOTAL_BYTES:
        raise HTTPException(status_code=413, detail="Upload is too large - import the files in smaller batches")
    files = [(f.filename, f.content.replace("\x00", "")) for f in payload.files]
    result = mib_registry.import_modules(db, payload.appliance.strip(), files, principal.username)
    db.commit()
    return result


@router.delete("/modules/{module_id}")
def delete_module(module_id: int, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)):
    module = db.query(MibModule).filter(MibModule.id == module_id).first()
    if module is None:
        raise HTTPException(status_code=404, detail="MIB module not found")
    db.delete(module)
    db.flush()
    mib_registry.rebuild(db)
    db.commit()
    return {"ok": True}


# --- trap browser ----------------------------------------------------------------


@router.get("/traps")
def search_traps(q: str = "", source: str = "all", limit: int = 100, db: Session = Depends(get_db)):
    if source not in ("all", "imported", "cisco"):
        raise HTTPException(status_code=422, detail="source must be all, imported or cisco")
    limit = max(1, min(limit, 500))
    results = mib_registry.search(db, q, source, limit)
    oids = [r["oid"] for r in results]
    rules = {r.oid: r for r in db.query(TrapRule).filter(TrapRule.oid.in_(oids)).all()} if oids else {}
    for r in results:
        r["builtin"] = _builtin_out(r["oid"])
        rule = rules.get(r["oid"])
        r["rule"] = _rule_out(rule) if rule else None
    return results


# --- custom trap rules -------------------------------------------------------------


@router.get("/rules")
def list_rules(db: Session = Depends(get_db)):
    return [_rule_out(r) for r in db.query(TrapRule).order_by(TrapRule.appliance, TrapRule.name).all()]


def _validate_rule(payload: TrapRuleIn, db: Session, rule_id: int | None) -> None:
    if payload.oid in TRAP_OID_MAP or payload.oid.startswith(RESERVED_PREFIX):
        raise HTTPException(
            status_code=409,
            detail=f"{payload.oid} is handled by a built-in rule ({TRAP_OID_MAP.get(payload.oid, ('reserved',))[0]}) and can't be overridden",
        )
    if payload.action in (TrapRuleAction.RAISE, TrapRuleAction.CLEAR):
        if payload.incident_type is None or not is_incident_type(payload.incident_type):
            raise HTTPException(status_code=422, detail="Raising or clearing needs an incident type")
    existing = db.query(TrapRule).filter(TrapRule.oid == payload.oid).first()
    if existing is not None and existing.id != rule_id:
        raise HTTPException(status_code=409, detail=f"A custom trap for {payload.oid} already exists ({existing.name})")


def _apply(rule: TrapRule, payload: TrapRuleIn) -> None:
    rule.oid = payload.oid
    rule.name = payload.name
    rule.description = payload.description.strip()
    rule.appliance = payload.appliance.strip()
    rule.action = payload.action
    rule.incident_type = payload.incident_type if payload.action != TrapRuleAction.ALARM else None
    rule.severity = payload.severity
    rule.interface_varbind = payload.interface_varbind
    rule.enabled = payload.enabled


@router.post("/rules")
def create_rule(payload: TrapRuleIn, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)):
    _validate_rule(payload, db, None)
    rule = TrapRule(created_by=principal.username)
    _apply(rule, payload)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    mib_registry.invalidate()
    return _rule_out(rule)


@router.put("/rules/{rule_id}")
def update_rule(rule_id: int, payload: TrapRuleIn, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)):
    rule = db.query(TrapRule).filter(TrapRule.id == rule_id).first()
    if rule is None:
        raise HTTPException(status_code=404, detail="Custom trap not found")
    _validate_rule(payload, db, rule_id)
    _apply(rule, payload)
    db.commit()
    db.refresh(rule)
    mib_registry.invalidate()
    return _rule_out(rule)


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)):
    rule = db.query(TrapRule).filter(TrapRule.id == rule_id).first()
    if rule is None:
        raise HTTPException(status_code=404, detail="Custom trap not found")
    db.delete(rule)
    db.commit()
    mib_registry.invalidate()
    return {"ok": True}
