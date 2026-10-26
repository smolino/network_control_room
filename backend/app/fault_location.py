"""Turns a fiber fault's segment index into something a field crew can act
on: which two repeaters bound the faulty span, how far along the link they
sit, and where they are.

A peering's fiber run (see BgpPeering.repeater_count) is modeled as
repeater_count regenerators R1..Rn evenly spaced between router_a and
router_b, splitting it into n+1 spans. Segment k (1-based) is the span
whose far end is repeater R(k+1) - i.e. between Rk and R(k+1), with
segment 0 / n being the router-to-first/last-repeater spans. That's the
same convention app.fiber_faults uses when it picks a segment, and the
geometry matches the repeater dots MapView.jsx draws (repeaterPositions),
so the coordinates reported here land exactly on the dots on the map.

Loss of signal is reported by the first repeater downstream of the break,
so the fault is isolated to one span but not to one side of it - both
bounding repeaters are listed for inspection; the span's own fiber is the
other suspect.
"""

from app.models import BgpPeering


def _point(peering: BgpPeering, index: int) -> dict:
    """Point `index` along the run: 0 = router_a, 1..n = repeaters R1..Rn,
    n+1 = router_b."""
    a, b = peering.router_a, peering.router_b
    n = peering.repeater_count or 0
    t = index / (n + 1)
    lng_b = b.longitude
    # Same antimeridian unwrap as MapView.jsx's unwrapPositions, so a
    # trans-Pacific run's repeaters are placed along the short way round.
    if lng_b - a.longitude > 180:
        lng_b -= 360
    elif lng_b - a.longitude < -180:
        lng_b += 360
    lng = a.longitude + (lng_b - a.longitude) * t
    lng = (lng + 180) % 360 - 180
    point = {
        "latitude": round(a.latitude + (b.latitude - a.latitude) * t, 5),
        "longitude": round(lng, 5),
        "km_from_a": round((peering.distance_km or 0) * t, 1),
    }
    if index == 0:
        return {**point, "kind": "router", "label": a.hostname}
    if index == n + 1:
        return {**point, "kind": "router", "label": b.hostname}
    return {**point, "kind": "repeater", "label": f"R{index}", "repeater_index": index}


def locate_fault(peering: BgpPeering, segment_index: int) -> dict | None:
    """Where a fault on segment `segment_index` of `peering` physically is,
    or None if the index doesn't fit this run (e.g. the link was re-seeded
    with fewer repeaters since)."""
    n = peering.repeater_count or 0
    if peering.router_a is None or peering.router_b is None or not 0 <= segment_index <= n:
        return None
    start, end = _point(peering, segment_index), _point(peering, segment_index + 1)
    return {
        "peering_id": peering.id,
        "router_a": peering.router_a.hostname,
        "router_b": peering.router_b.hostname,
        "distance_km": peering.distance_km,
        "repeater_count": n,
        "segment_index": segment_index,
        "segment_count": n + 1,
        "segment_length_km": round(end["km_from_a"] - start["km_from_a"], 1),
        "start": start,
        "end": end,
        "repeaters_to_check": [p["label"] for p in (start, end) if p["kind"] == "repeater"],
    }


def describe_fault(location: dict) -> str:
    """One-line summary, e.g. "span R3-R4 (km 252.5-336.7 of 505.1) on
    rtr-madrid-001 <-> rtr-barcelona-002"."""
    start, end = location["start"], location["end"]
    return (
        f"span {start['label']}-{end['label']} (km {start['km_from_a']}-{end['km_from_a']} of"
        f" {location['distance_km']}) on {location['router_a']} <-> {location['router_b']}"
    )
