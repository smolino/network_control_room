"""MIBs uploaded for other appliances, and custom trap rules - the backend
of the "MIBs & Traps" tab (app.api.mibs).

- Importing re-parses *all* stored modules together (app.snmp.mib_parser),
  so a vendor SMI root uploaded later resolves the traps of modules that
  were uploaded before it, and rewrites the mib_traps table.
- The classifier (which runs in the correlator process, not the API one)
  reads rules and imported trap names through a small cache refreshed every
  CACHE_TTL_SECONDS, so an edit made in the UI applies within that window
  without a per-trap query or cross-process signalling.
"""

import json
import threading
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import IncidentType, MibModule, MibTrap, TrapRule, TrapRuleAction
from app.snmp import mib_parser, trap_catalog

CACHE_TTL_SECONDS = 15


@dataclass(frozen=True)
class RuleSnapshot:
    oid: str
    name: str
    action: TrapRuleAction
    incident_type: IncidentType | None
    severity: str
    interface_varbind: str | None


# --- import -----------------------------------------------------------------


def import_modules(db: Session, appliance: str, files: list[tuple[str, str]], username: str | None) -> dict:
    """Stores each uploaded file as a MibModule (replacing a module of the
    same name), then rebuilds the trap table. Returns per-file results."""
    errors: list[dict] = []
    imported: list[str] = []
    for filename, text in files:
        parsed = mib_parser.parse_module(text)
        if parsed is None:
            errors.append({"filename": filename, "error": "No 'MODULE-NAME DEFINITIONS ::= BEGIN' found - not a MIB file?"})
            continue
        module = db.query(MibModule).filter(MibModule.name == parsed.module).first()
        if module is None:
            module = MibModule(name=parsed.module)
            db.add(module)
        module.appliance = appliance
        module.filename = filename[:255]
        module.source = text
        module.uploaded_by = username
        imported.append(parsed.module)
    db.flush()
    rebuild(db)
    modules = db.query(MibModule).filter(MibModule.name.in_(imported)).all() if imported else []
    by_name = {m.name: m for m in modules}
    return {
        "modules": [module_summary(by_name[name], with_unresolved=True) for name in imported if name in by_name],
        "errors": errors,
    }


def rebuild(db: Session) -> None:
    """Re-resolves every stored module and rewrites mib_traps. On an OID
    defined twice, the first module (by upload order) keeps it."""
    modules = db.query(MibModule).order_by(MibModule.id).all()
    parsed_by_id = {}
    for m in modules:
        parsed = mib_parser.parse_module(m.source)
        if parsed is not None:
            parsed_by_id[m.id] = parsed
    traps = mib_parser.resolve_traps(list(parsed_by_id.values()))

    db.query(MibTrap).delete(synchronize_session=False)
    id_by_name = {p.module: mid for mid, p in parsed_by_id.items()}
    counts = {m.id: 0 for m in modules}
    unresolved: dict[int, list[dict]] = {m.id: [] for m in modules}
    seen: set[str] = set()
    for t in traps:
        module_id = id_by_name[t.module]
        if t.oid is None:
            unresolved[module_id].append({"name": t.name, "missing": t.missing})
            continue
        if t.oid in seen:
            continue
        seen.add(t.oid)
        counts[module_id] += 1
        db.add(
            MibTrap(
                oid=t.oid,
                name=t.name[:128],
                module_id=module_id,
                objects=json.dumps(t.objects),
                status=t.status[:32],
                description=t.description,
            )
        )
    for m in modules:
        m.trap_count = counts[m.id]
        m.unresolved_count = len(unresolved[m.id])
        m.unresolved = json.dumps(unresolved[m.id])
    db.flush()
    invalidate()


def module_summary(m: MibModule, with_unresolved: bool = False) -> dict:
    out = {
        "id": m.id,
        "name": m.name,
        "appliance": m.appliance,
        "filename": m.filename,
        "trap_count": m.trap_count,
        "unresolved_count": m.unresolved_count,
        "uploaded_by": m.uploaded_by,
        "updated_at": m.updated_at or m.created_at,
    }
    if with_unresolved:
        out["unresolved"] = json.loads(m.unresolved or "[]")
    return out


# --- search -----------------------------------------------------------------


def search(db: Session, q: str, source: str, limit: int) -> list[dict]:
    """Traps from uploaded MIBs and/or the bundled Cisco catalogue whose
    name, OID or MIB matches `q` (case-insensitive; empty = everything)."""
    needle = q.strip().lower()
    results: list[dict] = []
    if source in ("all", "imported"):
        query = db.query(MibTrap, MibModule).join(MibModule, MibTrap.module_id == MibModule.id)
        if needle:
            like = f"%{needle}%"
            query = query.filter(
                MibTrap.name.ilike(like) | MibTrap.oid.ilike(like) | MibModule.name.ilike(like) | MibModule.appliance.ilike(like)
            )
        for trap, module in query.order_by(MibModule.name, MibTrap.name).limit(limit).all():
            results.append(
                {
                    "oid": trap.oid,
                    "name": trap.name,
                    "mib": module.name,
                    "appliance": module.appliance,
                    "source": "imported",
                    "objects": json.loads(trap.objects or "[]"),
                    "status": trap.status,
                    "description": trap.description or "",
                }
            )
    if source in ("all", "cisco") and len(results) < limit:
        for oid, d in trap_catalog.all_traps().items():
            if needle and needle not in d["name"].lower() and needle not in oid and needle not in d["mib"].lower():
                continue
            results.append({"oid": oid, "appliance": "Cisco", "source": "cisco", **d})
            if len(results) >= limit:
                break
    return results


# --- classifier lookups (cached) ---------------------------------------------

_lock = threading.Lock()
_cache: dict = {"loaded_at": 0.0, "rules": {}, "names": {}}


def invalidate() -> None:
    with _lock:
        _cache["loaded_at"] = 0.0


def _snapshot(db: Session) -> dict:
    with _lock:
        if time.monotonic() - _cache["loaded_at"] < CACHE_TTL_SECONDS:
            return _cache
    rules = {
        r.oid: RuleSnapshot(r.oid, r.name, r.action, r.incident_type, r.severity, r.interface_varbind)
        for r in db.query(TrapRule).filter(TrapRule.enabled.is_(True)).all()
    }
    names = dict(db.query(MibTrap.oid, MibTrap.name).all())
    with _lock:
        _cache.update(loaded_at=time.monotonic(), rules=rules, names=names)
        return _cache


def rule_for(db: Session, oid: str) -> RuleSnapshot | None:
    return _snapshot(db)["rules"].get(oid.lstrip("."))


def imported_name(db: Session, oid: str) -> str | None:
    return _snapshot(db)["names"].get(oid.lstrip("."))


def interface_from_varbinds(rule: RuleSnapshot, varbinds: list[tuple[str, str]]) -> str | None:
    """The value of the rule's interface varbind - matched exactly or as a
    prefix (table columns arrive as column.ifIndex)."""
    if not rule.interface_varbind:
        return None
    prefix = rule.interface_varbind.lstrip(".")
    for vb_oid, value in varbinds:
        vb = str(vb_oid).lstrip(".")
        if vb == prefix or vb.startswith(prefix + "."):
            return str(value)[:64] or None  # interface_name columns are 64 chars
    return None
