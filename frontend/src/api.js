const API_BASE = "/api";

async function getJSON(path) {
  const resp = await fetch(`${API_BASE}${path}`);
  if (!resp.ok) throw new Error(`${path} -> ${resp.status}`);
  return resp.json();
}

async function postJSON(path, body) {
  const resp = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!resp.ok) throw new Error(`${path} -> ${resp.status}`);
  return resp.json();
}

async function putJSON(path, body) {
  const resp = await fetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`${path} -> ${resp.status}`);
  return resp.json();
}

async function deleteJSON(path) {
  const resp = await fetch(`${API_BASE}${path}`, { method: "DELETE" });
  if (!resp.ok) throw new Error(`${path} -> ${resp.status}`);
  return resp.json();
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

export function fetchIncidents({ status, incidentType, routerId, routerType, limit = 200 } = {}) {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (incidentType) params.set("incident_type", incidentType);
  if (routerId) params.set("router_id", routerId);
  if (routerType) params.set("router_type", routerType);
  if (limit) params.set("limit", limit);
  const qs = params.toString();
  return getJSON(`/incidents${qs ? `?${qs}` : ""}`);
}

export function fetchStats() {
  return getJSON("/stats/summary");
}

export function resolveIncident(id) {
  return postJSON(`/incidents/${id}/resolve`);
}

export function bulkResolveIncidents(incidentIds) {
  return postJSON("/incidents/resolve", { incident_ids: incidentIds });
}

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

export function connectEvents(onMessage) {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${protocol}//${window.location.host}/ws/events`);
  ws.onmessage = (event) => {
    try {
      onMessage(JSON.parse(event.data));
    } catch {
      // ignore malformed events
    }
  };
  return ws;
}
