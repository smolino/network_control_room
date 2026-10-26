"""City -> coordinates lookup for the Add Fleet forms, for cities outside the
frontend's built-in table (frontend/src/cities.js).

Proxies OpenStreetMap's Nominatim rather than calling it from the browser so
requests carry an identifying User-Agent and stay within its usage policy
(https://operations.osmfoundation.org/policies/nominatim/): at most one
request per second, and repeated lookups served from an in-process cache.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import settings

router = APIRouter(prefix="/api/geocode", tags=["geocode"])

_MIN_INTERVAL_SECONDS = 1.0
_rate_lock = threading.Lock()
_last_request_at = 0.0


class GeocodeOut(BaseModel):
    city: str
    country: str | None
    latitude: float
    longitude: float
    display_name: str


class _LookupError(Exception):
    pass


@lru_cache(maxsize=1024)
def _lookup(city: str, country: str) -> GeocodeOut | None:
    global _last_request_at
    params = {"city": city, "format": "jsonv2", "limit": 1, "addressdetails": 1, "accept-language": "en"}
    if country:
        params["country"] = country
    req = urllib.request.Request(
        f"{settings.nominatim_url}/search?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": "network-control-room/1.0 (router fleet geocoding)"},
    )
    with _rate_lock:
        wait = _MIN_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                results = json.load(resp)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise _LookupError(str(exc)) from exc
        finally:
            _last_request_at = time.monotonic()

    if not results:
        return None
    hit = results[0]
    address = hit.get("address", {})
    return GeocodeOut(
        city=address.get("city") or address.get("town") or address.get("village") or city,
        country=address.get("country"),
        latitude=round(float(hit["lat"]), 4),
        longitude=round(float(hit["lon"]), 4),
        display_name=hit.get("display_name", city),
    )


@router.get("", response_model=GeocodeOut)
def geocode_city(city: str = Query(..., min_length=2), country: str = ""):
    """Best match for a city name, optionally narrowed by country. 404 when
    nothing matches; 502 when the geocoder itself can't be reached (failed
    lookups aren't cached, so a retry tries again)."""
    try:
        result = _lookup(city.strip().lower(), country.strip().lower())
    except _LookupError as exc:
        raise HTTPException(status_code=502, detail=f"Geocoding service unavailable: {exc}")
    if result is None:
        raise HTTPException(status_code=404, detail=f"No location found for {city!r}")
    return result
