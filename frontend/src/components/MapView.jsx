import { useEffect, useMemo, useState } from "react";
import L from "leaflet";
import { CircleMarker, MapContainer, Polyline, Popup, TileLayer } from "react-leaflet";

// Marker fill colors - kept vivid so routers pop against any tile background.
const STATUS_COLOR = {
  up: "#22c55e",
  down: "#ef4444",
  flapping: "#facc15",
  unknown: "#9aa4bf",
};

// Customer CPE markers use the same palette except when healthy, where they
// go blue instead of green - lets an operator tell the two router tiers
// apart on the map at a glance (in addition to the size difference), while
// down/flapping customers still turn the same red/yellow as any other
// incident.
const CUSTOMER_STATUS_COLOR = {
  ...STATUS_COLOR,
  up: "#3b82f6",
};

// Line colors are intentionally different from the marker palette: a plain
// "up" line in the same green as the marker all but disappears against
// green/tan map tiles, so healthy links use a neutral slate instead and
// only an actual incident (down/flapping) turns the line a loud color.
const LINE_COLOR = {
  up: "#64748b",
  down: "#ef4444",
  flapping: "#f59e0b",
  unknown: "#475569",
};

const BGP_COLOR = {
  established: "#3b82f6",
  down: "#ef4444",
};

// Traffic rerouted around a down primary-to-primary link - a distinct color
// from both link states so an operator can tell "detour in progress" apart
// from "healthy" or "still broken" at a glance.
const REROUTE_COLOR = "#a855f7";
const REROUTE_DASH = "10 8";
const REROUTE_DASH_LENGTH = 18; // sum of REROUTE_DASH - keeps the offset wrap seamless

const MARKER_STROKE = "#0b0f1a";
const ALERT_STROKE = "#f97316";
// Dashed ring for a router that's an endpoint of a peering with an open L1
// (fiber) incident, but hasn't itself failed auto-heal - distinguishes
// "symptomatic of an upstream physical fault" from the solid ALERT_STROKE
// ring, which means this router's own remediation attempt failed.
const SYMPTOMATIC_STROKE = "#fb923c";

// Blink cadence for routers whose auto-heal attempt failed to fix the
// problem - stops on its own once the router recovers, since
// needs_attention flips back to false server-side at that point.
const BLINK_INTERVAL_MS = 500;

// Marching-ants cadence for animated reroute lines - stops on its own once
// no peering is currently rerouted (see activeReroutes below).
const REROUTE_ANIM_INTERVAL_MS = 60;

// Leaflet draws a straight numeric interpolation between two lng values,
// with no awareness that -180/180 is the same meridian. Two points near
// opposite sides of it (e.g. San Francisco ~-122 and Wellington ~+174, only
// ~64deg apart across the Pacific) would otherwise get drawn going the long
// way around through the Atlantic. Shifting the later point by +-360 when
// the raw gap exceeds 180deg makes the line take the true shorter path.
function unwrapLng(prevLng, lng) {
  const diff = lng - prevLng;
  if (diff > 180) return lng - 360;
  if (diff < -180) return lng + 360;
  return lng;
}

function unwrapPositions(points) {
  if (points.length === 0) return points;
  const result = [points[0]];
  for (let i = 1; i < points.length; i++) {
    const [lat, lng] = points[i];
    result.push([lat, unwrapLng(result[i - 1][1], lng)]);
  }
  return result;
}

// At low zoom the map is narrower than the world (e.g. zoom 2 is 1024px vs
// a typical viewport well over that), so Leaflet tiles the world side by
// side and repeats every marker across each visible copy automatically -
// but NOT vector layers like Polyline, which only ever draw at their one
// literal coordinate. A line unwrapped past +-180 (see unwrapPositions
// above) only happens to land in whichever single copy matches its shifted
// longitude, so it silently vanishes from every other copy on screen.
// Drawing the same line shifted a further +-360 covers the adjacent copies
// too. Only lines that actually got unwrapped need this - a normal local
// line's coordinates never leave +-180, so it only needs the one copy it
// already renders in.
function worldCopies(positions) {
  const wrapped = positions.some(([, lng]) => lng < -180 || lng > 180);
  if (!wrapped) return [positions];
  return [-360, 0, 360].map((delta) => positions.map(([lat, lng]) => [lat, lng + delta]));
}

// Evenly-spaced points strictly between two (already-unwrapped) endpoints -
// one straight-line stand-in per SMF regenerator a link that length needs
// (see BgpPeeringOut.repeater_count), spaced so they never land on top of
// either router marker.
function repeaterPositions(a, b, count) {
  const points = [];
  for (let k = 1; k <= count; k++) {
    const t = k / (count + 1);
    points.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]);
  }
  return points;
}

function RouterMarker({ r, radius, fillOpacity, blinkOn, onSelectRouter, parentHostname, symptomatic }) {
  // Customer CPE is single-homed, so a down/flapping status IS the outage -
  // there's no separate peering line to carry that signal the way a
  // primary's BGP mesh does, so the marker itself blinks to flag it.
  const connectionIssue = r.router_type === "customer" && (r.status === "down" || r.status === "flapping");
  const shouldBlink = r.needs_attention || connectionIssue;
  const alerting = shouldBlink && blinkOn;
  const statusColor = r.router_type === "customer" ? CUSTOMER_STATUS_COLOR : STATUS_COLOR;
  // needs_attention (solid ring, own remediation failed) always wins over
  // symptomatic (dashed ring, upstream fiber cut correlated to this router).
  const showSymptomaticRing = symptomatic && !r.needs_attention;
  return (
    <CircleMarker
      center={[r.latitude, r.longitude]}
      radius={r.status === "flapping" || alerting ? radius + 3 : radius}
      pathOptions={{
        color: r.needs_attention ? ALERT_STROKE : showSymptomaticRing ? SYMPTOMATIC_STROKE : MARKER_STROKE,
        weight: alerting ? 3 : showSymptomaticRing ? 2 : 1.25,
        dashArray: showSymptomaticRing ? "3 3" : undefined,
        fillColor: statusColor[r.status] || statusColor.unknown,
        fillOpacity: shouldBlink ? (blinkOn ? 1 : 0.35) : fillOpacity,
      }}
    >
      <Popup>
        <div style={{ fontSize: "0.85rem" }}>
          <strong>{r.hostname}</strong>
          <div>{r.site_name} — {r.city}, {r.country}</div>
          <div>{r.mgmt_ip} · {r.model}</div>
          {r.asn && <div>AS{r.asn}</div>}
          <div>{r.router_type === "primary" ? "Primary (backbone)" : "Customer CPE"}</div>
          {parentHostname && <div>Uplink to: {parentHostname}</div>}
          <div>
            status: <span className={`badge ${r.status}`}>{r.status}</span>
          </div>
          {r.needs_attention && (
            <div style={{ color: ALERT_STROKE, fontWeight: 600, marginTop: "0.25rem" }}>
              ⚠ auto-heal failed — needs attention
            </div>
          )}
          {connectionIssue && !r.needs_attention && (
            <div style={{ color: STATUS_COLOR[r.status], fontWeight: 600, marginTop: "0.25rem" }}>
              ⚠ connection issue
            </div>
          )}
          {showSymptomaticRing && (
            <div style={{ color: SYMPTOMATIC_STROKE, fontWeight: 600, marginTop: "0.25rem" }}>
              ↳ symptomatic of a fiber fault on this link (L1 root cause)
            </div>
          )}
          <div style={{ marginTop: "0.4rem" }}>
            <span className="link" onClick={() => onSelectRouter(r.id)}>
              View details →
            </span>
          </div>
        </div>
      </Popup>
    </CircleMarker>
  );
}

export default function MapView({ routers, peerings = [], onSelectRouter }) {
  // Top-level L1/L3 layer toggles (§7.2 of the design doc this map is
  // modeled on) - L1 is the physical fiber plane (repeater dots + fault
  // segment), L3 is the logical/control-plane plane (routers, BGP/customer
  // links, reroutes). The finer-grained toggles below still apply within
  // whichever layers are on.
  const [showL1, setShowL1] = useState(true);
  const [showL3, setShowL3] = useState(true);
  const [showBgpLinks, setShowBgpLinks] = useState(true);
  const [showRepeaters, setShowRepeaters] = useState(true);
  const [showCustomers, setShowCustomers] = useState(true);
  const [showReroutes, setShowReroutes] = useState(true);
  const [blinkOn, setBlinkOn] = useState(true);
  const [dashOffset, setDashOffset] = useState(0);
  const routerById = useMemo(() => Object.fromEntries(routers.map((r) => [r.id, r])), [routers]);
  // Leaflet's canvas renderer (used everywhere else via preferCanvas, for
  // performance across ~2500 markers/lines) never applies the dashOffset
  // path option - only the SVG renderer does. Reroutes are always a
  // handful at once, so they get their own SVG renderer just so the
  // marching-ants animation actually animates.
  const svgRenderer = useMemo(() => L.svg(), []);

  const primaries = useMemo(() => routers.filter((r) => r.router_type !== "customer"), [routers]);
  const customers = useMemo(() => routers.filter((r) => r.router_type === "customer"), [routers]);
  // Peerings with a currently-open L1 (fiber) incident - drives both the
  // pulsing fault-segment line and the affected endpoint routers' dashed
  // "symptomatic" ring (see app.correlation on the backend).
  const openL1Peerings = useMemo(() => peerings.filter((p) => p.open_l1_incident_id), [peerings]);
  const symptomaticRouterIds = useMemo(() => {
    const ids = new Set();
    for (const p of openL1Peerings) {
      ids.add(p.router_a_id);
      ids.add(p.router_b_id);
    }
    return ids;
  }, [openL1Peerings]);

  // Gates the blink interval below - covers the existing "auto-heal failed"
  // case, a customer CPE's own down/flapping status (see connectionIssue in
  // RouterMarker), and now an open L1 fiber incident's pulsing fault
  // segment, so the interval only runs while something actually needs it
  // blinking.
  const anyBlinking = useMemo(
    () =>
      openL1Peerings.length > 0 ||
      routers.some(
        (r) => r.needs_attention || (r.router_type === "customer" && (r.status === "down" || r.status === "flapping"))
      ),
    [routers, openL1Peerings]
  );

  // Down peerings that currently have an alternate path through the mesh -
  // this is what traffic is actually rerouted over while the direct
  // session is unavailable. Naturally disappears (and the animated line
  // with it) the moment the peering re-establishes, with no extra
  // bookkeeping needed - see reroute_path in the backend's BgpPeeringOut.
  const activeReroutes = useMemo(
    () =>
      peerings.filter(
        (p) => p.status === "down" && Array.isArray(p.reroute_path) && p.reroute_path.length > 1
      ),
    [peerings]
  );

  const totalRepeaters = useMemo(
    () => peerings.reduce((sum, p) => sum + (p.repeater_count || 0), 0),
    [peerings]
  );

  useEffect(() => {
    if (!anyBlinking) return undefined;
    const timer = setInterval(() => setBlinkOn((v) => !v), BLINK_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [anyBlinking]);

  useEffect(() => {
    if (activeReroutes.length === 0) return undefined;
    const timer = setInterval(
      () => setDashOffset((v) => (v - 1 + REROUTE_DASH_LENGTH) % REROUTE_DASH_LENGTH),
      REROUTE_ANIM_INTERVAL_MS
    );
    return () => clearInterval(timer);
  }, [activeReroutes.length]);

  return (
    <div style={{ position: "relative", height: "100%", width: "100%" }}>
      <div
        style={{
          position: "absolute",
          top: 12,
          left: 50,
          zIndex: 1000,
          background: "#161c2cdd",
          border: "1px solid #262f45",
          borderRadius: 8,
          padding: "0.4rem 0.7rem",
          fontSize: "0.8rem",
          display: "flex",
          flexDirection: "column",
          gap: "0.3rem",
        }}
      >
        <div style={{ display: "flex", gap: "0.75rem", paddingBottom: "0.3rem", borderBottom: "1px solid #262f45" }}>
          <label style={{ display: "flex", alignItems: "center", gap: "0.4rem", cursor: "pointer" }}>
            <input type="checkbox" checked={showL1} onChange={(e) => setShowL1(e.target.checked)} />
            L1 physical
          </label>
          <label style={{ display: "flex", alignItems: "center", gap: "0.4rem", cursor: "pointer" }}>
            <input type="checkbox" checked={showL3} onChange={(e) => setShowL3(e.target.checked)} />
            L3 logical
          </label>
        </div>
        <label style={{ display: "flex", alignItems: "center", gap: "0.4rem", cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={showBgpLinks}
            onChange={(e) => setShowBgpLinks(e.target.checked)}
          />
          Show BGP links ({peerings.length})
        </label>
        <label style={{ display: "flex", alignItems: "center", gap: "0.4rem", cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={showRepeaters}
            onChange={(e) => setShowRepeaters(e.target.checked)}
          />
          Show repeaters ({totalRepeaters})
        </label>
        <label style={{ display: "flex", alignItems: "center", gap: "0.4rem", cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={showCustomers}
            onChange={(e) => setShowCustomers(e.target.checked)}
          />
          Show customer routers ({customers.length})
        </label>
        <label style={{ display: "flex", alignItems: "center", gap: "0.4rem", cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={showReroutes}
            onChange={(e) => setShowReroutes(e.target.checked)}
          />
          Show reroutes ({activeReroutes.length})
        </label>
      </div>

      <MapContainer
        center={[20, 10]}
        zoom={2}
        minZoom={2}
        worldCopyJump
        preferCanvas
        style={{ height: "100%", width: "100%" }}
      >
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
          url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
          subdomains="abcd"
        />

        {showCustomers &&
          showL3 &&
          customers.flatMap((c) => {
            if (!c.parent_router_id) return [];
            const parent = routerById[c.parent_router_id];
            if (!parent) return [];
            const incident = c.status === "down" || c.status === "flapping";
            const positions = unwrapPositions([
              [c.latitude, c.longitude],
              [parent.latitude, parent.longitude],
            ]);
            return worldCopies(positions).map((pos, i) => (
              <Polyline
                key={`uplink-${c.id}-${i}`}
                positions={pos}
                pathOptions={{
                  color: LINE_COLOR[c.status] || LINE_COLOR.unknown,
                  weight: incident ? 2.5 : 1.25,
                  opacity: incident ? 0.9 : 0.55,
                  dashArray: "4 5",
                }}
              />
            ));
          })}

        {(showL1 || (showL3 && showBgpLinks)) &&
          peerings.flatMap((p) => {
            const a = routerById[p.router_a_id];
            const b = routerById[p.router_b_id];
            if (!a || !b) return [];
            const down = p.status === "down";
            const positions = unwrapPositions([
              [a.latitude, a.longitude],
              [b.latitude, b.longitude],
            ]);
            const lines =
              showL3 && showBgpLinks
                ? worldCopies(positions).map((pos, i) => (
                    <Polyline
                      key={`${p.id}-${i}`}
                      positions={pos}
                      pathOptions={{
                        color: BGP_COLOR[p.status] || BGP_COLOR.established,
                        weight: down ? 2.5 : 1.5,
                        opacity: down ? 0.9 : 0.6,
                      }}
                    />
                  ))
                : [];
            const reps = repeaterPositions(positions[0], positions[1], p.repeater_count || 0);
            const repeaters = showL1
              ? reps.flatMap((point, ri) =>
                  worldCopies([point]).map((copy, ci) => (
                    <CircleMarker
                      key={`repeater-${p.id}-${ri}-${ci}`}
                      center={copy[0]}
                      radius={2}
                      pathOptions={{
                        color: MARKER_STROKE,
                        weight: showRepeaters ? 0.5 : 0,
                        fillColor: "#ffffff",
                        fillOpacity: showRepeaters ? 0.9 : 0.08,
                        opacity: showRepeaters ? 1 : 0.08,
                      }}
                    />
                  ))
                )
              : [];
            // active_fault_segment is 1-based: the fault sits strictly
            // between the (segment-1)-th and segment-th repeater (0-based
            // into `reps`), never touching either router endpoint - see
            // app/fiber_faults.py. open_l1_incident_id means this is a real,
            // still-open OPTICAL_ALARM incident (not just the cosmetic
            // overlay) - pulse it via the same blink timer as needs_attention
            // routers so it visually reads as "root cause, actively faulted".
            const fault = p.active_fault_segment;
            const hasOpenL1Incident = Boolean(p.open_l1_incident_id);
            const faultLine =
              showL1 && fault && reps[fault - 1] && reps[fault]
                ? worldCopies(unwrapPositions([reps[fault - 1], reps[fault]])).map((pos, i) => (
                    <Polyline
                      key={`fault-${p.id}-${i}`}
                      positions={pos}
                      pathOptions={{
                        color: ALERT_STROKE,
                        weight: hasOpenL1Incident && blinkOn ? 5 : 4,
                        opacity: hasOpenL1Incident ? (blinkOn ? 1 : 0.45) : 1,
                      }}
                    />
                  ))
                : [];
            return [...lines, ...repeaters, ...faultLine];
          })}

        {showReroutes &&
          showL3 &&
          activeReroutes.flatMap((p) => {
            const positions = unwrapPositions(
              p.reroute_path
                .map((id) => routerById[id])
                .filter(Boolean)
                .map((r) => [r.latitude, r.longitude])
            );
            if (positions.length < 2) return [];
            return worldCopies(positions).map((pos, i) => (
              <Polyline
                key={`reroute-${p.id}-${i}`}
                positions={pos}
                renderer={svgRenderer}
                pathOptions={{
                  color: REROUTE_COLOR,
                  weight: 3,
                  opacity: 0.9,
                  dashArray: REROUTE_DASH,
                  dashOffset: String(dashOffset),
                }}
              />
            ));
          })}

        {showCustomers &&
          showL3 &&
          customers.map((c) => (
            <RouterMarker
              key={c.id}
              r={c}
              radius={4}
              fillOpacity={0.95}
              blinkOn={blinkOn}
              onSelectRouter={onSelectRouter}
              parentHostname={routerById[c.parent_router_id]?.hostname}
              symptomatic={symptomaticRouterIds.has(c.id)}
            />
          ))}

        {showL3 &&
          primaries.map((r) => (
            <RouterMarker
              key={r.id}
              r={r}
              radius={6.5}
              fillOpacity={0.95}
              blinkOn={blinkOn}
              onSelectRouter={onSelectRouter}
              symptomatic={symptomaticRouterIds.has(r.id)}
            />
          ))}
      </MapContainer>
    </div>
  );
}
