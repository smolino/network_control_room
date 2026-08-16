"""Builds 10 last-mile "customer" CPE routers per primary (4000 total),
spread across the wider region the primary serves - not clustered right
on top of it - and single-homed to it via `parent_mgmt_ip`. These are not
part of the BGP mesh - they're simple customer-facing routers dedicated
to that primary, not peers of the backbone. Writes
customer_routers_seed.json, which trap_simulator.py seeds *after* the
primaries (so parent_mgmt_ip can be resolved to a real router id).
"""

import json
import math
import random

from generate_routers import slugify
from land import find_land_point

random.seed(11)

CUSTOMERS_PER_PRIMARY = 10

# Small-office/home-office CPE-class Cisco gear, distinct from the
# carrier-grade models used for the primaries.
CUSTOMER_MODELS = [
    "ISR1100-4G",
    "ISR1101",
    "C1111-8P",
    "Catalyst 1300",
    "RV340",
    "RV160",
]

# Customers are scattered in a ring around their primary rather than
# right next to it, so the 10 dots actually trace out the region the
# primary serves (a metro area + its surroundings) instead of a tight
# cluster sitting on top of the primary's own marker.
MIN_RADIUS_KM = 30.0
MAX_RADIUS_KM = 250.0
KM_PER_DEGREE_LAT = 111.0


def load_primaries() -> list[dict]:
    with open("routers_seed.json") as f:
        return json.load(f)


def random_offset(lat: float, lon: float) -> tuple[float, float]:
    """A uniformly-area-distributed random point in the ring between
    MIN_RADIUS_KM and MAX_RADIUS_KM from (lat, lon), retried until it
    lands on land (a pure radius+angle scatter around a coastal primary
    would otherwise happily drop customers in the ocean)."""

    def sample_offset() -> tuple[float, float]:
        angle = random.uniform(0, 2 * math.pi)
        radius_km = math.sqrt(random.uniform(MIN_RADIUS_KM**2, MAX_RADIUS_KM**2))
        d_lat = (radius_km * math.cos(angle)) / KM_PER_DEGREE_LAT
        km_per_degree_lon = KM_PER_DEGREE_LAT * math.cos(math.radians(lat))
        d_lon = (radius_km * math.sin(angle)) / max(km_per_degree_lon, 1.0)
        return d_lat, d_lon

    return find_land_point(lat, lon, sample_offset)


def build_customers(primaries: list[dict]) -> list[dict]:
    customers = []
    for p_idx, primary in enumerate(primaries):
        for c_idx in range(1, CUSTOMERS_PER_PRIMARY + 1):
            lat, lon = random_offset(primary["latitude"], primary["longitude"])
            customers.append(
                {
                    "hostname": f"cust-{slugify(primary['city'])}-{p_idx + 1:03d}-{c_idx:02d}",
                    # p_idx can exceed 255, so split it across two octets
                    # the same way routers_seed.json's mgmt_ip does.
                    "mgmt_ip": f"10.{20 + p_idx // 250}.{p_idx % 250}.{c_idx}",
                    "router_type": "customer",
                    "parent_mgmt_ip": primary["mgmt_ip"],
                    "vendor": "Cisco",
                    "model": random.choice(CUSTOMER_MODELS),
                    "site_name": f"{primary['city']} region, site {c_idx}",
                    "country": primary["country"],
                    "city": primary["city"],
                    "latitude": round(lat, 4),
                    "longitude": round(lon, 4),
                }
            )
    return customers


if __name__ == "__main__":
    primaries = load_primaries()
    customers = build_customers(primaries)
    with open("customer_routers_seed.json", "w") as f:
        json.dump(customers, f, indent=2)
    print(f"Wrote {len(customers)} customer routers for {len(primaries)} primaries")
