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
