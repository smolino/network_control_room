"""Builds the telco-owned "primary" (backbone) routers - a synthetic
Cisco fleet spread across real-world locations - and writes them to
routers_seed.json. Run once (or re-run to regenerate) - the JSON file is
what trap_simulator.py actually loads at runtime. Each primary is later
peered with ~3 others over BGP (generate_bgp_topology.py) and gets 10
customer CPE routers of its own (generate_customer_routers.py).

Scoped to Mexico only - see git history for the full 400-city global list.
"""

import json
import random

from land import find_land_point

random.seed(42)

# (city, country, latitude, longitude)
LOCATIONS = [
    ("Mexico City", "Mexico", 19.4326, -99.1332),
    ("Tijuana", "Mexico", 32.5149, -117.0382),
    ("Guadalajara", "Mexico", 20.6597, -103.3496),
    ("Monterrey", "Mexico", 25.6866, -100.3161),
    ("Puebla", "Mexico", 19.0414, -98.2063),
    ("Leon", "Mexico", 21.1250, -101.6860),
    ("Ciudad Juarez", "Mexico", 31.6904, -106.4245),
    ("Cancun", "Mexico", 21.1619, -86.8515),
    ("Merida", "Mexico", 20.9674, -89.5926),
]

# (vendor, model) pairs to draw a primary's device from - a realistic
# multi-vendor backbone mix, not a single-vendor fleet: Cisco IP/MPLS core
# routers (ASR 9000/8000 series) alongside its ONS 15454 optical transport
# platform, plus Arista routers and Ciena optical gear for vendor diversity.
# Weighted by list length below, not explicitly - Cisco keeps the majority
# share since that's still the common case for a backbone this size.
VENDOR_DEVICES = [
    ("Cisco", "ASR1001-X"),
    ("Cisco", "ASR1002-HX"),
    ("Cisco", "ASR9001"),
    ("Cisco", "ASR9010"),
    ("Cisco", "ASR9903"),
    ("Cisco", "8201"),
    ("Cisco", "8202"),
    ("Cisco", "8712"),
    ("Cisco", "ONS 15454"),
    ("Cisco", "ISR4451-X"),
    ("Cisco", "ISR4331"),
    ("Cisco", "ISR4321"),
    ("Cisco", "Catalyst 8300-1N1S"),
    ("Cisco", "Catalyst 8500L-8S4X"),
    ("Cisco", "CRS-1000"),
    ("Cisco", "NCS 5501"),
    ("Arista", "7280R3"),
    ("Arista", "7280SR3"),
    ("Arista", "7500R3"),
    ("Arista", "7050X3"),
    ("Ciena", "6500"),
    ("Ciena", "5170"),
    ("Ciena", "8180"),
    ("Ciena", "WaveLogic 5"),
]


def slugify(city: str) -> str:
    return city.lower().replace(" ", "-").replace(".", "")


def build_routers() -> list[dict]:
    routers = []
    for i, (city, country, lat, lon) in enumerate(LOCATIONS):
        jitter_lat, jitter_lon = find_land_point(
            lat, lon, lambda: (random.uniform(-0.4, 0.4), random.uniform(-0.4, 0.4))
        )
        vendor, model = random.choice(VENDOR_DEVICES)
        routers.append(
            {
                "hostname": f"rtr-{slugify(city)}-{i + 1:03d}",
                "mgmt_ip": f"10.10.{i // 250}.{(i % 250) + 1}",
                "router_type": "primary",
                "vendor": vendor,
                "model": model,
                "site_name": f"{city} PoP",
                "country": country,
                "city": city,
                "latitude": round(jitter_lat, 4),
                "longitude": round(jitter_lon, 4),
                # private ASN range (64512-65534) - one per router, used for
                # the BGP peering mesh (see generate_bgp_topology.py)
                "asn": 64512 + i,
            }
        )
    return routers


if __name__ == "__main__":
    routers = build_routers()
    with open("routers_seed.json", "w") as f:
        json.dump(routers, f, indent=2)
    print(f"Wrote {len(routers)} routers to routers_seed.json")
