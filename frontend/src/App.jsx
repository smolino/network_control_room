import { useEffect, useState, useCallback } from "react";
import {
  connectEvents,
  fetchBgpPeerings,
  fetchIncidentImpact,
  fetchIncidentQueue,
  fetchIncidents,
  fetchRouters,
  fetchStats,
  fetchTeams,
} from "./api.js";
import { clearSession, hasRole, loadSession, saveSession, SessionContext, UNAUTHORIZED_EVENT } from "./auth.js";
import MapView from "./components/MapView.jsx";
import Dashboard from "./components/Dashboard.jsx";
import RouterList from "./components/RouterList.jsx";
import AddFleet from "./components/AddFleet.jsx";
import IncidentList from "./components/IncidentList.jsx";
import RouterDetail from "./components/RouterDetail.jsx";
import HumanReview from "./components/HumanReview.jsx";
import Login from "./components/Login.jsx";
import SettingsPanel from "./components/SettingsPanel.jsx";

// minRole only hides tabs the backend would refuse anyway (see app/auth.py).
const TABS = [
  { key: "map", label: "Map" },
  { key: "dashboard", label: "Dashboard" },
  { key: "routers", label: "Routers" },
  { key: "add-fleet", label: "Add Fleet", minRole: "admin" },
  { key: "incidents", label: "Incidents" },
  { key: "customer-incidents", label: "Customer Incidents" },
  { key: "human-review", label: "Human Review" },
];

// Open incidents a person has to act on - the Human Review board (see
// backend/app/incidents/service.py).
const HUMAN_STAGES = new Set(["triage", "acknowledged", "dispatched"]);
const inQueue = (i) => i.status === "open" && HUMAN_STAGES.has(i.stage);

// Same order as GET /api/incidents/queue: P1 first, then oldest first.
function sortQueue(list) {
  return [...list].sort(
    (a, b) =>
      (a.priority ?? "P9").localeCompare(b.priority ?? "P9") || (a.opened_at ?? "").localeCompare(b.opened_at ?? "")
  );
}

// Every WebSocket event carries changed incidents in full (the same shape
// as the REST API), so merging is a plain upsert.
function upsertIncidents(prev, updates) {
  if (updates.length === 0) return prev;
  const byId = new Map(updates.map((i) => [i.id, i]));
  const known = new Set(prev.map((i) => i.id));
  const added = updates.filter((i) => !known.has(i.id));
  const merged = prev.map((i) => (byId.has(i.id) ? { ...i, ...byId.get(i.id) } : i));
  return [...added, ...merged].slice(0, 200);
}

function applyToQueue(prev, updates) {
  if (updates.length === 0) return prev;
  const byId = new Map(updates.map((i) => [i.id, i]));
  const known = new Set(prev.map((i) => i.id));
  const kept = prev
    .map((i) => (byId.has(i.id) ? { ...i, ...byId.get(i.id) } : i))
    .filter(inQueue);
  return sortQueue([...kept, ...updates.filter((i) => !known.has(i.id) && inQueue(i))]);
}

function patchRouters(prev, updates) {
  if (!updates?.length) return prev;
  const byId = new Map(updates.map((r) => [r.id, r]));
  return prev.map((r) =>
    byId.has(r.id) ? { ...r, status: byId.get(r.id).status, needs_attention: byId.get(r.id).needs_attention } : r
  );
}

// ?incident=<id> - the link in team notification emails (see
// api/incidents.py:notify_team) opens that incident on the board.
function incidentIdFromUrl() {
  const id = Number(new URLSearchParams(window.location.search).get("incident"));
  return Number.isInteger(id) && id > 0 ? id : null;
}

export default function App() {
  const [session, setSession] = useState(loadSession);
  const currentUser = session?.user ?? null;
  const authenticated = !!currentUser;
  const [focusIncidentId, setFocusIncidentId] = useState(incidentIdFromUrl);
  const [view, setView] = useState(() => (incidentIdFromUrl() ? "human-review" : "map"));
  const [routers, setRouters] = useState([]);
  const [incidents, setIncidents] = useState([]);
  const [customerIncidents, setCustomerIncidents] = useState([]);
  const [queue, setQueue] = useState([]);
  const [stats, setStats] = useState(null);
  const [peerings, setPeerings] = useState([]);
  const [teams, setTeams] = useState([]);
  const [selectedRouterId, setSelectedRouterId] = useState(null);
  const [connected, setConnected] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  // Blast radius of one incident, highlighted on the map - see
  // backend/app/impact.py and IncidentList's "Show on map".
  const [impactHighlight, setImpactHighlight] = useState(null);
  // The faulty span of one fiber-cut incident, zoomed to on the map - see
  // backend/app/fault_location.py and IncidentList's "Locate on map".
  const [faultHighlight, setFaultHighlight] = useState(null);

  const handleSessionChanged = useCallback((next) => {
    saveSession(next);
    setSession(next);
  }, []);

  const logout = useCallback(() => {
    clearSession();
    setSession(null);
    setImpactHighlight(null);
    setFaultHighlight(null);
  }, []);

  // Drop ?incident= once read, so a reload doesn't keep jumping back to it.
  useEffect(() => {
    if (incidentIdFromUrl()) window.history.replaceState(null, "", window.location.pathname);
  }, []);

  // Any API call coming back 401 (expired token, password reset by an
  // admin, account deleted) lands here - see request() in api.js.
  useEffect(() => {
    window.addEventListener(UNAUTHORIZED_EVENT, logout);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, logout);
  }, [logout]);

  const visibleTabs = TABS.filter((t) => !t.minRole || hasRole(currentUser, t.minRole));
  const activeView = visibleTabs.some((t) => t.key === view) ? view : "map";

  const showImpactOnMap = useCallback(async (incident) => {
    try {
      const impact = await fetchIncidentImpact(incident.id);
      if (!impact) {
        window.alert(`Incident #${incident.id} no longer has any customer impact.`);
        return;
      }
      setFaultHighlight(null);
      setImpactHighlight({ incidentId: incident.id, incidentType: incident.incident_type, ...impact });
      setView("map");
    } catch (err) {
      window.alert(`Couldn't load the impact of incident #${incident.id}: ${err.message}`);
    }
  }, []);

  const locateFaultOnMap = useCallback((incident) => {
    if (!incident.fault_location) return;
    setImpactHighlight(null);
    setFaultHighlight({ incidentId: incident.id, status: incident.status, ...incident.fault_location });
    setView("map");
  }, []);

  const reloadTeams = useCallback(async () => {
    setTeams(await fetchTeams());
  }, []);

  const reloadAll = useCallback(async () => {
    const [r, i, ci, q, s, p] = await Promise.all([
      fetchRouters(),
      fetchIncidents({ limit: 200, routerType: "primary" }),
      fetchIncidents({ limit: 200, routerType: "customer" }),
      fetchIncidentQueue(),
      fetchStats(),
      fetchBgpPeerings(),
    ]);
    setRouters(r);
    setIncidents(i);
    setCustomerIncidents(ci);
    setQueue(q);
    setStats(s);
    setPeerings(p);
    await reloadTeams();
  }, [reloadTeams]);

  // After "Delete entire fleet": drop anything pointing at routers or
  // incidents that no longer exist, then reload the (now empty) fleet.
  const resetAfterFleetDelete = useCallback(() => {
    setSelectedRouterId(null);
    setImpactHighlight(null);
    setFaultHighlight(null);
    setFocusIncidentId(null);
    reloadAll().catch(console.error);
  }, [reloadAll]);

  useEffect(() => {
    if (!authenticated) return;
    reloadAll().catch(console.error);
    // Full resync, not just stats: routers/peerings/incidents are otherwise
    // only ever patched incrementally over the WebSocket, so a tab that
    // loaded before the simulator finished seeding (or that missed a
    // message during a reconnect) would stay stuck with stale/empty data
    // forever without this.
    const resyncTimer = setInterval(() => {
      reloadAll().catch(console.error);
    }, 10000);
    return () => clearInterval(resyncTimer);
  }, [reloadAll, authenticated]);

  useEffect(() => {
    if (!authenticated) return;
    let ws;
    let retryTimer;

    const connect = () => {
      ws = connectEvents((msg) => {
        if (msg.type === "fiber_fault") {
          setPeerings((prev) => {
            const idx = prev.findIndex((p) => p.id === msg.peering_id);
            if (idx === -1) return prev;
            const next = [...prev];
            next[idx] = {
              ...next[idx],
              active_fault_segment: msg.action === "start" ? msg.segment_index : null,
            };
            return next;
          });
          return;
        }

        // Every router and link is gone (Settings > Simulation > Delete
        // entire fleet, possibly from another browser) - start clean.
        if (msg.type === "fleet_reset") {
          resetAfterFleetDelete();
          return;
        }

        // "trap" (a new alarm and whatever it changed) and "incident_updated"
        // (sweeper/operator actions) carry the same incidents/routers lists.
        if (msg.type !== "trap" && msg.type !== "incident_updated") return;

        const changed = msg.incidents ?? [];
        if (changed.length > 0) {
          setIncidents((prev) => upsertIncidents(prev, changed.filter((i) => i.router_type !== "customer")));
          setCustomerIncidents((prev) => upsertIncidents(prev, changed.filter((i) => i.router_type === "customer")));
          setQueue((prev) => applyToQueue(prev, changed));
        }
        setRouters((prev) => patchRouters(prev, msg.routers));

        if (msg.bgp_peering) {
          setPeerings((prev) => {
            const idx = prev.findIndex((p) => p.id === msg.bgp_peering.id);
            if (idx === -1) return prev;
            const next = [...prev];
            next[idx] = { ...next[idx], status: msg.bgp_peering.status, reroute_path: msg.bgp_peering.reroute_path };
            return next;
          });
        }
      });
      ws.onopen = () => setConnected(true);
      ws.onclose = (event) => {
        setConnected(false);
        // 4401: the backend rejected our token - see ws_events in main.py.
        if (event.code === 4401) {
          logout();
          return;
        }
        retryTimer = setTimeout(connect, 3000);
      };
      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      clearTimeout(retryTimer);
      ws?.close();
    };
  }, [authenticated, logout, resetAfterFleetDelete]);

  const selectedRouter = routers.find((r) => r.id === selectedRouterId) || null;

  if (!authenticated) {
    return (
      <Login onLogin={handleSessionChanged} />
    );
  }

  return (
    <SessionContext.Provider value={currentUser}>
      <header className="topbar">
        <h1>Network Control Room</h1>
        {stats && (
          <div className="stats-strip">
            <span><strong>{stats.total_routers}</strong> primaries</span>
            <span><strong>{stats.total_customer_routers}</strong> customers</span>
            <span><strong>{stats.routers_up}</strong> up</span>
            <span><strong>{stats.routers_down}</strong> down</span>
            <span><strong>{stats.routers_flapping}</strong> flapping</span>
            <span><strong>{stats.open_incidents}</strong> open incidents</span>
            {stats.incidents_in_triage > 0 && (
              <span
                className="link"
                onClick={() => setView("human-review")}
                title="Open incidents nobody has picked up yet"
              >
                <strong style={{ color: "#fb923c" }}>{stats.incidents_in_triage}</strong> in triage
              </span>
            )}
            {stats.customers_impacted > 0 && (
              <span title="Distinct customer routers inside the blast radius of an open incident">
                <strong style={{ color: stats.customers_down > 0 ? "#f87171" : "#facc15" }}>
                  {stats.customers_impacted}
                </strong>{" "}
                customers impacted
                {stats.customers_down > 0 && ` (${stats.customers_down} down)`}
              </span>
            )}
            <span>{connected ? "🟢 live" : "🔴 reconnecting"}</span>
          </div>
        )}
        <div className="topbar-actions">
          <span className="current-user">
            {currentUser.username} <span className="role-tag">{currentUser.role}</span>
          </span>
          <button
            className="icon-button"
            onClick={() => setShowSettings(true)}
            aria-label="Settings"
            title="Settings"
          >
            ⚙
          </button>
          <button
            className="logout-button"
            onClick={logout}
          >
            Log out
          </button>
        </div>
      </header>
      {showSettings && (
        <SettingsPanel
          onSessionChanged={handleSessionChanged}
          teams={teams}
          onTeamsChanged={reloadTeams}
          onFleetReset={resetAfterFleetDelete}
          onClose={() => setShowSettings(false)}
        />
      )}
      <div className="app-body">
        <nav className="sidebar">
          {visibleTabs.map((t) => (
            <button
              key={t.key}
              className={activeView === t.key ? "active" : ""}
              onClick={() => setView(t.key)}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <main>
          {activeView === "map" && (
            <MapView
              routers={routers}
              peerings={peerings}
              onFleetChanged={reloadAll}
              impactHighlight={impactHighlight}
              onClearImpact={() => setImpactHighlight(null)}
              faultHighlight={faultHighlight}
              onClearFault={() => setFaultHighlight(null)}
              onSelectRouter={(id) => {
                setSelectedRouterId(id);
                setView("routers");
              }}
            />
          )}
          {activeView === "dashboard" && <Dashboard stats={stats} incidents={incidents} />}
          {activeView === "routers" && (
            <RouterList
              routers={routers}
              selectedRouterId={selectedRouterId}
              onSelectRouter={setSelectedRouterId}
              onRouterDeleted={async (id) => {
                if (id === selectedRouterId) setSelectedRouterId(null);
                await reloadAll();
              }}
            />
          )}
          {activeView === "add-fleet" && <AddFleet routers={routers} onFleetChanged={reloadAll} />}
          {activeView === "incidents" && (
            <IncidentList
              incidents={incidents}
              routers={routers}
              onShowImpact={showImpactOnMap}
              onLocateFault={locateFaultOnMap}
              onSelectRouter={(id) => {
                setSelectedRouterId(id);
                setView("routers");
              }}
            />
          )}
          {activeView === "customer-incidents" && (
            <IncidentList
              incidents={customerIncidents}
              routers={routers}
              onShowImpact={showImpactOnMap}
              onSelectRouter={(id) => {
                setSelectedRouterId(id);
                setView("routers");
              }}
            />
          )}
          {activeView === "human-review" && (
            <HumanReview
              queue={queue}
              routers={routers}
              teams={teams}
              focusIncidentId={focusIncidentId}
              onFocusIncident={setFocusIncidentId}
              onShowImpact={showImpactOnMap}
              onLocateFault={locateFaultOnMap}
              onSelectRouter={(id) => {
                setSelectedRouterId(id);
                setView("routers");
              }}
            />
          )}
        </main>
      </div>
      {activeView === "routers" && selectedRouter && (
        <RouterDetail
          router={selectedRouter}
          routers={routers}
          peerings={peerings}
          onClose={() => setSelectedRouterId(null)}
          onSelectRouter={setSelectedRouterId}
        />
      )}
    </SessionContext.Provider>
  );
}
