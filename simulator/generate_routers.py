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

CISCO_MODELS = [
    "ASR1001-X",
    "ASR1002-HX",
    "ASR9001",
    "ISR4451-X",
    "ISR4331",
    "ISR4321",
    "Catalyst 8300-1N1S",
    "Catalyst 8500L-8S4X",
    "CRS-1000",
    "NCS 5501",
]


def slugify(city: str) -> str:
    return city.lower().replace(" ", "-").replace(".", "")


def build_routers() -> list[dict]:
    routers = []
    for i, (city, country, lat, lon) in enumerate(LOCATIONS):
        jitter_lat, jitter_lon = find_land_point(
            lat, lon, lambda: (random.uniform(-0.4, 0.4), random.uniform(-0.4, 0.4))
        )
        routers.append(
            {
                "hostname": f"rtr-{slugify(city)}-{i + 1:03d}",
                "mgmt_ip": f"10.10.{i // 250}.{(i % 250) + 1}",
                "router_type": "primary",
                "vendor": "Cisco",
                "model": random.choice(CISCO_MODELS),
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
