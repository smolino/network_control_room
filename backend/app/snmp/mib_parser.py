"""A small purpose-built SMI (MIB) parser - not a full MIB compiler.

It only extracts what trap handling needs: OID assignments, and every
SNMPv2 NOTIFICATION-TYPE / SNMPv1 TRAP-TYPE with its name, varbinds, status
and description. Symbols are resolved across all modules parsed together
(plus BASE_SYMBOLS for the standard SMI tree), with no import validation.

Pure standard library on purpose: it is used both by the backend, to import
MIBs uploaded in the "MIBs & Traps" tab (app.api.mibs), and by
scripts/build_trap_catalog.py, which builds the bundled Cisco catalogue
without the backend's dependencies installed.
"""

import re
from dataclasses import dataclass, field

DESCRIPTION_MAX = 300

# Well-known nodes, so a vendor MIB uploaded on its own still resolves as
# long as it hangs off the standard tree (SNMPv2-SMI, SNMPv2-MIB, IF-MIB,
# CISCO-SMI). Vendor SMI roots (JUNIPER-SMI, HUAWEI-MIB, ...) must be
# uploaded alongside, or the traps under them are reported as unresolved.
BASE_SYMBOLS: dict[str, list[int]] = {
    "ccitt": [0],
    "zeroDotZero": [0, 0],
    "iso": [1],
    "joint-iso-ccitt": [2],
    "org": [1, 3],
    "dod": [1, 3, 6],
    "internet": [1, 3, 6, 1],
    "directory": [1, 3, 6, 1, 1],
    "mgmt": [1, 3, 6, 1, 2],
    "mib-2": [1, 3, 6, 1, 2, 1],
    "system": [1, 3, 6, 1, 2, 1, 1],
    "interfaces": [1, 3, 6, 1, 2, 1, 2],
    "transmission": [1, 3, 6, 1, 2, 1, 10],
    "snmp": [1, 3, 6, 1, 2, 1, 11],
    "ifMIB": [1, 3, 6, 1, 2, 1, 31],
    "experimental": [1, 3, 6, 1, 3],
    "private": [1, 3, 6, 1, 4],
    "enterprises": [1, 3, 6, 1, 4, 1],
    "security": [1, 3, 6, 1, 5],
    "snmpV2": [1, 3, 6, 1, 6],
    "snmpDomains": [1, 3, 6, 1, 6, 1],
    "snmpProxys": [1, 3, 6, 1, 6, 2],
    "snmpModules": [1, 3, 6, 1, 6, 3],
    "snmpMIB": [1, 3, 6, 1, 6, 3, 1],
    "snmpMIBObjects": [1, 3, 6, 1, 6, 3, 1, 1],
    "snmpTraps": [1, 3, 6, 1, 6, 3, 1, 1, 5],
    "cisco": [1, 3, 6, 1, 4, 1, 9],
    "ciscoProducts": [1, 3, 6, 1, 4, 1, 9, 1],
    "ciscoMgmt": [1, 3, 6, 1, 4, 1, 9, 9],
    "ciscoExperiment": [1, 3, 6, 1, 4, 1, 9, 10],
}

ASSIGN_RE = re.compile(
    r"([a-zA-Z][\w-]*)\s+"
    r"(OBJECT\s+IDENTIFIER|MODULE-IDENTITY|OBJECT-IDENTITY|OBJECT-TYPE|NOTIFICATION-TYPE|"
    r"OBJECT-GROUP|NOTIFICATION-GROUP|MODULE-COMPLIANCE|AGENT-CAPABILITIES)\b"
    r"((?:(?!::=).)*?)::=\s*\{([^}]*)\}",
    re.S,
)
TRAP_RE = re.compile(r"([a-zA-Z][\w-]*)\s+TRAP-TYPE\b((?:(?!::=).)*?)::=\s*(\d+)", re.S)
IMPORTS_RE = re.compile(r"\bIMPORTS\b[^;]*;", re.S)
MODULE_RE = re.compile(r"([A-Z][\w-]*)\s+DEFINITIONS\s*::=\s*BEGIN", re.S)
MACRO_RE = re.compile(r"\b[\w-]+\s+MACRO\s*::=\s*BEGIN.*?\bEND\b", re.S)


@dataclass
class TrapDef:
    name: str
    module: str
    oid: str | None
    objects: list[str]
    status: str
    description: str
    missing: str | None = None  # the symbol that couldn't be resolved, when oid is None


@dataclass
class ParsedModule:
    module: str
    defs: dict[str, tuple[str | None, list[int]]] = field(default_factory=dict)
    notifications: list[dict] = field(default_factory=list)
    traps: list[dict] = field(default_factory=list)


def preprocess(text: str) -> tuple[str, list[str]]:
    """One pass over the module: quoted strings are swapped for \\x00N\\x00
    placeholders (kept in `strings`), and ASN.1 comments - "--" to the next
    "--" or end of line - are dropped. Doing both in the same scan matters:
    a stray quote inside a comment must not open a string, and "--" inside
    a string must not open a comment."""
    strings: list[str] = []
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = text.find('"', i + 1)
            j = n if j == -1 else j
            strings.append(text[i + 1 : j])
            out.append(f"\x00{len(strings) - 1}\x00")
            i = j + 1
        elif text.startswith("--", i):
            i += 2
            while i < n and text[i] != "\n" and not text.startswith("--", i):
                i += 1
            if text.startswith("--", i):
                i += 2
        else:
            out.append(ch)
            i += 1
    text = "".join(out)
    text = MACRO_RE.sub(" ", text)
    # "IMPORTS MODULE-IDENTITY, ..." would otherwise parse as a definition.
    text = IMPORTS_RE.sub(" ", text)
    return text, strings


def _clause(body: str, keyword: str) -> str | None:
    m = re.search(rf"\b{keyword}\b\s*(\S[^\n]*)", body)
    return m.group(1).strip() if m else None


def _description(body: str, strings: list[str]) -> str:
    m = re.search(r"\bDESCRIPTION\s*\x00(\d+)\x00", body)
    if not m:
        return ""
    text = " ".join(strings[int(m.group(1))].split())
    if len(text) > DESCRIPTION_MAX:
        text = text[:DESCRIPTION_MAX].rsplit(" ", 1)[0] + "…"
    return text


def _objects(body: str, keyword: str) -> list[str]:
    m = re.search(rf"\b{keyword}\s*\{{([^}}]*)\}}", body)
    return [o.strip() for o in m.group(1).split(",") if o.strip()] if m else []


def _parse_oid_value(value: str) -> tuple[str | None, list[int]] | None:
    """`{ parent 3 }`, `{ iso org(3) dod(6) 1 }`, `{ 1 3 6 }` ->
    (parent symbol or None, trailing arcs)."""
    parts = value.split()
    if not parts:
        return None
    parent, arcs = None, []
    for i, part in enumerate(parts):
        m = re.fullmatch(r"[\w-]*\((\d+)\)", part)
        if m:
            arcs.append(int(m.group(1)))
        elif part.isdigit():
            arcs.append(int(part))
        elif i == 0:
            parent = part
        else:
            return None
    return parent, arcs


def module_name(text: str) -> str | None:
    """The module name a MIB file declares (`FOO-MIB DEFINITIONS ::= BEGIN`)."""
    m = MODULE_RE.search(preprocess(text)[0])
    return m.group(1) if m else None


def parse_module(text: str) -> ParsedModule | None:
    text, strings = preprocess(text)
    mod = MODULE_RE.search(text)
    if not mod:
        return None
    parsed = ParsedModule(module=mod.group(1))
    for m in ASSIGN_RE.finditer(text):
        name, kind, body, value = m.group(1), " ".join(m.group(2).split()), m.group(3), m.group(4)
        oid_value = _parse_oid_value(value)
        if oid_value is None:
            continue
        parsed.defs[name] = oid_value
        if kind == "NOTIFICATION-TYPE":
            parsed.notifications.append(
                {
                    "name": name,
                    "objects": _objects(body, "OBJECTS"),
                    "status": (_clause(body, "STATUS") or "current").split()[0],
                    "description": _description(body, strings),
                }
            )
    for m in TRAP_RE.finditer(text):
        name, body, specific = m.group(1), m.group(2), int(m.group(3))
        enterprise = _clause(body, "ENTERPRISE")
        if not enterprise:
            continue
        parsed.traps.append(
            {
                "name": name,
                "enterprise": enterprise.split()[0],
                "specific": specific,
                "objects": _objects(body, "VARIABLES"),
                "status": "current",
                "description": _description(body, strings),
            }
        )
    return parsed


class Resolver:
    """Resolves symbols to numeric OIDs, preferring the module's own
    definitions, then any other parsed module, then `base`."""

    def __init__(self, modules: list[ParsedModule], base: dict[str, list[int]] | None = None):
        self.base = BASE_SYMBOLS if base is None else base
        self.local = {m.module: m.defs for m in modules}
        self.globl: dict[str, tuple[str, tuple]] = {}
        for m in modules:
            for name, value in m.defs.items():
                self.globl.setdefault(name, (m.module, value))
        self.cache: dict[tuple[str, str], list[int] | None] = {}

    def resolve(self, module: str, name: str, depth: int = 0) -> list[int] | None:
        key = (module, name)
        if key in self.cache:
            return self.cache[key]
        if depth > 64:
            return None
        if name in self.local.get(module, {}):
            owner, value = module, self.local[module][name]
        elif name in self.globl:
            owner, value = self.globl[name]
        elif name in self.base:
            return self.base[name]
        else:
            self.cache[key] = None
            return None
        parent, arcs = value
        base = [] if parent is None else self.resolve(owner, parent, depth + 1)
        oid = None if base is None else base + arcs
        self.cache[key] = oid
        return oid

    def missing_root(self, module: str, name: str) -> str:
        """The first symbol up `name`'s parent chain that nothing defines -
        what the user needs to upload (usually a vendor SMI module)."""
        seen = set()
        while name not in seen:
            seen.add(name)
            if name in self.local.get(module, {}):
                owner, value = module, self.local[module][name]
            elif name in self.globl:
                owner, value = self.globl[name]
            else:
                return name
            parent = value[0]
            if parent is None or parent in self.base:
                return name
            module, name = owner, parent
        return name


def resolve_traps(modules: list[ParsedModule], base: dict[str, list[int]] | None = None) -> list[TrapDef]:
    """Every trap the modules define, with its numeric OID (None if its
    parent chain can't be resolved). SNMPv1 TRAP-TYPEs get their RFC 3584
    v2 OID (enterprise.0.specific); v1 copies of a v2 notification
    (e.g. *-V1SMI modules) are skipped."""
    resolver = Resolver(modules, base)
    result: list[TrapDef] = []

    def make(module: str, item: dict, oid: list[int] | None, symbol: str) -> TrapDef:
        return TrapDef(
            name=item["name"],
            module=module,
            oid=".".join(map(str, oid)) if oid is not None else None,
            objects=item["objects"],
            status=item["status"],
            description=item["description"],
            missing=None if oid is not None else resolver.missing_root(module, symbol),
        )

    v2_names = {n["name"] for m in modules for n in m.notifications}
    for m in modules:
        for n in m.notifications:
            result.append(make(m.module, n, resolver.resolve(m.module, n["name"]), n["name"]))
    for m in modules:
        for t in m.traps:
            if t["name"] in v2_names:
                continue
            ent = resolver.resolve(m.module, t["enterprise"])
            if ent is None:
                oid = None
            elif ent[-1] == 0:
                # Converted v1 MIBs name the v2 notification branch
                # (...mib.0) as ENTERPRISE; its traps live directly below it.
                oid = ent + [t["specific"]]
            else:
                oid = ent + [0, t["specific"]]
            result.append(make(m.module, t, oid, t["enterprise"]))
    return result
