#!/usr/bin/env python3
"""Imports seed/routers.csv + seed/bgp_links.csv into a running backend -
the production counterpart of the dev-only simulator's startup seed (see
docker-compose.dev.yml). Same CSV format as the Add Fleet bulk-upload UI,
but posts in chunks so a 4000-row upload doesn't hit nginx's body-size or
proxy-timeout limits. Idempotent: re-running skips routers that already
exist and updates existing links in place (see api/routers.py:seed_routers,
api/bgp.py:seed_peerings).

Usage (stdlib only, no pip install needed):
    ./scripts/import-fleet.py                         # http://localhost:8000
    ./scripts/import-fleet.py https://controlroom.point2point.org.uk
"""
import csv
import json
import os
import sys
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTERS_CSV = os.path.join(REPO_ROOT, "seed", "routers.csv")
LINKS_CSV = os.path.join(REPO_ROOT, "seed", "bgp_links.csv")
CHUNK_SIZE = 500


def read_csv(path: str) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def post(url: str, payload: list[dict]) -> list:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)


def router_payload(r: dict) -> dict:
    return {
        "hostname": r["hostname"],
        "mgmt_ip": r["mgmt_ip"],
        "router_type": r["router_type"] or "primary",
        "parent_mgmt_ip": r["parent_mgmt_ip"] or None,
        "asn": int(r["asn"]) if r["asn"] else None,
        "vendor": r["vendor"] or "Cisco",
        "model": r["model"] or None,
        "site_name": r["site_name"] or None,
        "country": r["country"] or None,
        "city": r["city"] or None,
        "latitude": float(r["latitude"]),
        "longitude": float(r["longitude"]),
    }


def link_payload(r: dict) -> dict:
    return {
        "router_a_mgmt_ip": r["router_a_mgmt_ip"],
        "router_b_mgmt_ip": r["router_b_mgmt_ip"],
        "distance_km": float(r["distance_km"]),
        "repeater_count": int(r["repeater_count"]),
    }


def seed_in_chunks(url: str, rows: list[dict], what: str) -> None:
    for i in range(0, len(rows), CHUNK_SIZE):
        chunk = rows[i : i + CHUNK_SIZE]
        post(url, chunk)
        print(f"  {what}: {min(i + CHUNK_SIZE, len(rows))}/{len(rows)}")


def main() -> None:
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
    routers = [router_payload(r) for r in read_csv(ROUTERS_CSV)]
    links = [link_payload(r) for r in read_csv(LINKS_CSV)]

    # Primaries strictly before customers: a customer's parent_mgmt_ip is
    # resolved to a router id at seed time, so the parent must already exist.
    # send_boot_trap so each new router leaves status "unknown" - there's no
    # simulator here to send its own coldStart.
    routers_url = f"{base}/api/routers/seed?send_boot_trap=true"
    seed_in_chunks(routers_url, [r for r in routers if r["router_type"] == "primary"], "primaries")
    seed_in_chunks(routers_url, [r for r in routers if r["router_type"] == "customer"], "customers")
    # Links last: both endpoints must already exist.
    seed_in_chunks(f"{base}/api/bgp/seed", links, "BGP links")
    print("Done.")


if __name__ == "__main__":
    main()
