#!/usr/bin/env python3
"""Builds backend/app/snmp/data/trap_catalog.json from Cisco's public MIB
repository (https://github.com/cisco/cisco-mibs).

The catalogue maps every trap OID the MIBs define - SNMPv2 NOTIFICATION-TYPE
and SNMPv1 TRAP-TYPE (as its RFC 3584 v2 OID, enterprise.0.specific) - to
its name, defining MIB, varbinds, status and a short description. The
backend uses it to name traps it has no classification rule for (see
app.snmp.oid_map.lookup_trap). Parsing is done by app/snmp/mib_parser.py,
the same parser the "MIBs & Traps" tab uses for uploaded MIBs.

    git clone --depth 1 https://github.com/cisco/cisco-mibs.git /tmp/cisco-mibs
    python3 scripts/build_trap_catalog.py /tmp/cisco-mibs
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "backend" / "app" / "snmp" / "data" / "trap_catalog.json"

# Imported by file location so the backend package (and its dependencies)
# isn't needed - mib_parser is pure standard library.
sys.path.insert(0, str(REPO_ROOT / "backend" / "app" / "snmp"))
import mib_parser  # noqa: E402

# SMIv2 (v2/) wins over SMIv1 (v1/) when a module exists in both.
MIB_DIRS = ("v2", "v1")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mib_repo", type=Path, help="path to a checkout of github.com/cisco/cisco-mibs")
    ap.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    modules, seen = [], set()
    for sub in MIB_DIRS:
        for path in sorted((args.mib_repo / sub).glob("*.my")):
            mod = mib_parser.parse_module(path.read_text(encoding="latin-1"))
            if mod and mod.module not in seen:
                seen.add(mod.module)
                modules.append(mod)
    if not modules:
        print(f"No MIB modules found under {args.mib_repo}/{{v2,v1}}", file=sys.stderr)
        return 1

    catalog: dict[str, dict] = {}
    unresolved = 0
    for trap in mib_parser.resolve_traps(modules, base={"iso": [1], "ccitt": [0], "joint-iso-ccitt": [2]}):
        if trap.oid is None:
            unresolved += 1
            continue
        # First definition wins (v2 modules are loaded before v1 ones).
        catalog.setdefault(
            trap.oid,
            {
                "name": trap.name,
                "mib": trap.module,
                "objects": trap.objects,
                "status": trap.status,
                "description": trap.description,
            },
        )

    ordered = dict(sorted(catalog.items(), key=lambda kv: [int(a) for a in kv[0].split(".")]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"source": "https://github.com/cisco/cisco-mibs", "modules": len(modules), "traps": ordered}
    args.out.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(modules)} modules, {len(ordered)} traps -> {args.out} ({unresolved} unresolved)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
