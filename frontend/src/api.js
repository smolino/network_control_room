import { getToken, UNAUTHORIZED_EVENT } from "./auth.js";

const API_BASE = "/api";

// Rejects with the backend's `detail` message when there is one, so callers
// can show it as-is (e.g. "Requires the admin role"). A 401 on anything but
// the login call itself means the session is gone - App listens for
// UNAUTHORIZED_EVENT and drops back to the login screen.
async function request(method, path, body) {
  const headers = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!resp.ok) {
    let detail = null;
    try {
      detail = (await resp.json()).detail;
    } catch {
      // non-JSON error body
    }
    if (resp.status === 401 && path !== "/auth/login") {
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    }
    const err = new Error(typeof detail === "string" ? detail : `${path} -> ${resp.status}`);
    err.status = resp.status;
    err.detail = detail;
    throw err;
  }
  return resp.json();
}

const getJSON = (path) => request("GET", path);
const postJSON = (path, body) => request("POST", path, body);
const putJSON = (path, body) => request("PUT", path, body);
const patchJSON = (path, body) => request("PATCH", path, body);
const deleteJSON = (path) => request("DELETE", path);

// --- auth / users (backend/app/api/auth.py, api/users.py) ---

// Rejects with err.detail === "otp_required" when the account has two-factor
// auth on and no otpCode was given - ask for it and call again with both.
export function login(username, password, otpCode) {
  return postJSON("/auth/login", { username, password, otp_code: otpCode || null });
}

export function changeOwnPassword(currentPassword, newPassword) {
  return postJSON("/auth/password", { current_password: currentPassword, new_password: newPassword });
}

export function startOtpSetup() {
  return postJSON("/auth/otp/setup");
}

export function enableOtp(secret, code) {
  return postJSON("/auth/otp/enable", { secret, code });
}

export function disableOtp(code) {
  return postJSON("/auth/otp/disable", { code });
}

export function fetchUsers() {
  return getJSON("/users");
}

export function createUser({ username, password, role }) {
  return postJSON("/users", { username, password, role });
}

export function updateUser(id, changes) {
  return patchJSON(`/users/${id}`, changes);
}

export function deleteUser(id) {
  return deleteJSON(`/users/${id}`);
}

// City -> coordinates via the backend's Nominatim proxy - for cities that
// aren't in the built-in table (cities.js). Rejects when nothing matches.
export function geocodeCity(city, country) {
  const params = new URLSearchParams({ city });
  if (country) params.set("country", country);
  return getJSON(`/geocode?${params}`);
}

export function fetchRouters() {
  return getJSON("/routers");
}

export function fetchRouter(id) {
  return getJSON(`/routers/${id}`);
}

// Also removes any BGP peering (and its interface bundles) this router was
// a side of - see backend/app/api/routers.py:delete_router.
export function deleteRouter(id) {
  return deleteJSON(`/routers/${id}`);
}

export function fetchRouterTraps(id, limit = 100) {
  return getJSON(`/routers/${id}/traps?limit=${limit}`);
}

export function fetchRouterBackups(id, limit = 20) {
  return getJSON(`/routers/${id}/backups?limit=${limit}`);
}

export function fetchRouterRemediation(id, limit = 20) {
  return getJSON(`/routers/${id}/remediation?limit=${limit}`);
}

export function fetchIncidentRemediation(id) {
  return getJSON(`/incidents/${id}/remediation`);
}

export function fetchIncidents({ status, stage, incidentType, routerId, routerType, limit = 200 } = {}) {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (stage) params.set("stage", stage);
  if (incidentType) params.set("incident_type", incidentType);
  if (routerId) params.set("router_id", routerId);
  if (routerType) params.set("router_type", routerType);
  if (limit) params.set("limit", limit);
  const qs = params.toString();
  return getJSON(`/incidents${qs ? `?${qs}` : ""}`);
}

export function fetchIncident(id) {
  return getJSON(`/incidents/${id}`);
}

// Everything a person has to act on (triage / acknowledged / dispatched),
// most urgent first - see backend/app/incidents/service.py:queue.
export function fetchIncidentQueue() {
  return getJSON("/incidents/queue");
}

// Current blast radius incl. every affected router id, or null - see
// backend/app/impact.py.
export function fetchIncidentImpact(id) {
  return getJSON(`/incidents/${id}/impact`);
}

export function fetchStats() {
  return getJSON("/stats/summary");
}

export function acknowledgeIncident(id) {
  return postJSON(`/incidents/${id}/ack`);
}

// reason: one of RESOLUTION_REASONS below. Also resolves the incident's
// symptoms; a symptom on its own can't be resolved (409).
export function resolveIncident(id, { reason, note }) {
  return postJSON(`/incidents/${id}/resolve`, { reason, note: note || null });
}

// Root incidents only - symptom ids are skipped and go with their root.
export function bulkResolveIncidents(incidentIds, { reason, note }) {
  return postJSON("/incidents/resolve", { incident_ids: incidentIds, reason, note: note || null });
}

// The reasons an operator can pick (backend: models.USER_RESOLUTION_REASONS).
export const RESOLUTION_REASONS = [
  { value: "fixed", label: "Fixed" },
  { value: "false_positive", label: "False positive" },
  { value: "maintenance", label: "Planned maintenance" },
  { value: "duplicate", label: "Duplicate" },
];

export function resolveAllOpenIncidents() {
  return postJSON("/incidents/resolve-all");
}

export function fetchBgpPeerings() {
  return getJSON("/bgp/peerings");
}

// Bulk idempotent upsert - same endpoints the trap simulator seeds the fleet
// with on startup (see backend/app/api/routers.py, api/bgp.py). Adding a
// router whose mgmt_ip already exists is a no-op; adding a peering that
// already exists updates its distance/repeater_count in place.
//
// send_boot_trap=true (the UI's default, unlike the simulator's own startup
// seed) makes the backend publish a synthetic coldStart for each router
// actually created here - without it, a manually- or bulk-added router
// sits at status "unknown" forever, since that's the only trap that ever
// flips it and the simulator only ever sends one, at its own startup, for
// whichever routers it knew about then.
export function seedRouters(routers, { sendBootTrap = true } = {}) {
  return postJSON(`/routers/seed${sendBootTrap ? "?send_boot_trap=true" : ""}`, routers);
}

export function seedBgpPeerings(pairs) {
  return postJSON("/bgp/seed", pairs);
}

// Vendor/model catalog backing Add Fleet's Vendor/Model dropdowns - see
// backend/app/api/router_models.py. Not linked to Router.vendor/model by
// FK, so this is purely a curated list of choices, not a validation source.
export function fetchRouterModels() {
  return getJSON("/router-models");
}

export function createRouterModel({ vendor, model }) {
  return postJSON("/router-models", { vendor, model });
}

export function updateRouterModel(id, { vendor, model }) {
  return putJSON(`/router-models/${id}`, { vendor, model });
}

export function fetchIncidentAnalysis(id) {
  return getJSON(`/incidents/${id}/analysis`);
}

export function fetchIncidentNotifications(id) {
  return getJSON(`/incidents/${id}/notifications`);
}

export function notifyIncidentTeam(id, { teamId, subject, body }) {
  return postJSON(`/incidents/${id}/notify`, { team_id: teamId, subject, body });
}

export function fetchTeams(kind) {
  return getJSON(`/teams${kind ? `?kind=${kind}` : ""}`);
}

export function createTeam({ kind, name, email }) {
  return postJSON("/teams", { kind, name, email });
}

export function updateTeam(id, { kind, name, email }) {
  return putJSON(`/teams/${id}`, { kind, name, email });
}

export function deleteTeam(id) {
  return deleteJSON(`/teams/${id}`);
}

export function fetchSimulationStatus() {
  return getJSON("/simulation/status");
}

export function setSimulationEnabled(enabled) {
  return postJSON("/simulation/status", { enabled });
}

// Admin-only: deletes every router, BGP link and their whole history, and
// stops the simulator re-seeding its demo fleet - see
// backend/app/api/simulation.py:reset_fleet.
export function resetFleet() {
  return postJSON("/simulation/reset-fleet");
}

export function connectEvents(onMessage) {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const token = encodeURIComponent(getToken() ?? "");
  const ws = new WebSocket(`${protocol}//${window.location.host}/ws/events?token=${token}`);
  ws.onmessage = (event) => {
    try {
      onMessage(JSON.parse(event.data));
    } catch {
      // ignore malformed events
    }
  };
  return ws;
}
