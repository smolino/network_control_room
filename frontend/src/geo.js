// Shared great-circle distance and fiber-repeater spacing model. Used by
// both the Add Fleet manual/bulk forms and the map's click-to-link BGP flow
// so a manually-created link gets the same repeater density the simulator
// itself generates (see simulator/generate_bgp_topology.py).
export const REPEATER_SPACING_KM = 80;

export function haversineKm(aLat, aLon, bLat, bLon) {
  const R = 6371;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(bLat - aLat);
  const dLon = toRad(bLon - aLon);
  const a =
    Math.sin(dLat / 2) ** 2 + Math.cos(toRad(aLat)) * Math.cos(toRad(bLat)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}
