"""Land/sea lookup used only by the fleet generator scripts, so no
primary or customer router ends up sitting in the ocean after its random
jitter is applied. Backed by `global-land-mask` (a bundled low-resolution
land/sea raster - no network access needed at generation time). This is
a dev-time dependency for regenerating the seed JSON files; it is not
part of the runtime Docker images, which only ever read the already-
generated static JSON (see requirements-dev.txt / README).
"""

from global_land_mask import globe


def is_land(lat: float, lon: float) -> bool:
    return bool(globe.is_land(lat, lon))


def find_land_point(base_lat: float, base_lon: float, sample_offset, max_attempts: int = 100) -> tuple[float, float]:
    """Repeatedly calls `sample_offset()` (returning a (d_lat, d_lon)
    offset) until (base_lat + d_lat, base_lon + d_lon) lands on land.
    Falls back to shrinking the last attempted offset back toward
    (base_lat, base_lon) - real-world city coordinates, assumed to be on
    land - if nothing sampled lands on land within max_attempts."""
    last_offset = (0.0, 0.0)
    for _ in range(max_attempts):
        d_lat, d_lon = sample_offset()
        if is_land(base_lat + d_lat, base_lon + d_lon):
            return base_lat + d_lat, base_lon + d_lon
        last_offset = (d_lat, d_lon)

    for shrink in (0.5, 0.25, 0.1, 0.0):
        d_lat, d_lon = last_offset[0] * shrink, last_offset[1] * shrink
        if is_land(base_lat + d_lat, base_lon + d_lon):
            return base_lat + d_lat, base_lon + d_lon

    return base_lat, base_lon
