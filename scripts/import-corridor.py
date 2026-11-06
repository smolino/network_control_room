#!/usr/bin/env python3
"""Imports an optical corridor spec (seed/corridors/*.json, produced by
convert-corridor-sheet.py) into a running backend via
POST /api/corridors/import. Idempotent - re-running updates in place.

Usage (stdlib only):
    ./scripts/import-corridor.py seed/corridors/bio1-mad1.json                    # http://localhost:8000
    ./scripts/import-corridor.py seed/corridors/bio1-mad1.json http://host:8000 --prune
    ./scripts/import-corridor.py seed/corridors/bio1-mad1.json --dry-run          # validate only

--prune also removes L3 peerings that have one of the corridor's optical
elements on a side (e.g. a terminal modelled as a router before).
The endpoint is admin-only: export SERVICE_API_TOKEN first.
"""
import json
import os
import sys
import urllib.error
import urllib.request


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        raise SystemExit(__doc__)
    spec_path = args[0]
    base = (args[1] if len(args) > 1 else "http://localhost:8000").rstrip("/")
    with open(spec_path) as f:
        spec = json.load(f)
    if "--dry-run" in flags:
        url = f"{base}/api/corridors/validate"
    else:
        url = f"{base}/api/corridors/import" + ("?prune_l1_peerings=true" if "--prune" in flags else "")
    req = urllib.request.Request(
        url,
        data=json.dumps(spec).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {os.environ.get('SERVICE_API_TOKEN', '')}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            print(json.dumps(json.load(resp), indent=2))
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode()}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
