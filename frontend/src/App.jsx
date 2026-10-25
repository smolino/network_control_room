import { useEffect, useState, useCallback, useRef } from "react";
import { connectEvents, fetchBgpPeerings, fetchIncidentImpact, fetchIncidents, fetchRouters, fetchStats, fetchTeams } from "./api.js";
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

export default function App() {
  const [session, setSession] = useState(loadSession);
  const currentUser = session?.user ?? null;
  const authenticated = !!currentUser;
  const [view, setView] = useState("map");
  const [routers, setRouters] = useState([]);
  const [incidents, setIncidents] = useState([]);
  const [customerIncidents, setCustomerIncidents] = useState([]);
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

  // The WS handler below is only ever set up once (empty effect deps), so
  // it can't read fresh `routers` state directly without going stale - a
  // ref sidesteps that without re-subscribing the socket on every routers
  // update.
  const routersRef = useRef([]);
  useEffect(() => {
    routersRef.current = routers;
  }, [routers]);

  const reloadTeams = useCallback(async () => {
    setTeams(await fetchTeams());
  }, []);

  const reloadAll = useCallback(async () => {
    const [r, i, ci, s, p] = await Promise.all([
      fetchRouters(),
      fetchIncidents({ limit: 200, routerType: "primary" }),
      fetchIncidents({ limit: 200, routerType: "customer" }),
      fetchStats(),
      fetchBgpPeerings(),
    ]);
    setRouters(r);
    setIncidents(i);
    setCustomerIncidents(ci);
    setStats(s);
    setPeerings(p);
    await reloadTeams();
  }, [reloadTeams]);

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
        if (msg.type === "incident_resolved") {
          const routerType =
            msg.router?.router_type ??
            routersRef.current.find((r) => r.id === msg.incident.router_id)?.router_type;
          const setTarget = routerType === "customer" ? setCustomerIncidents : setIncidents;
          setTarget((prev) => {
            const idx = prev.findIndex((i) => i.id === msg.incident.id);
            if (idx === -1) return prev;
            const next = [...prev];
            next[idx] = {
              ...next[idx],
              status: msg.incident.status,
              closed_at: msg.incident.closed_at,
              resolved_manually: msg.incident.resolved_manually,
              impact: null,
            };
            return next;
          });

          if (msg.router) {
            setRouters((prev) => {
              const idx = prev.findIndex((r) => r.id === msg.router.id);
              if (idx === -1) return prev;
              const next = [...prev];
              next[idx] = {
                ...next[idx],
                status: msg.router.status,
                needs_attention: msg.router.needs_attention,
              };
              return next;
            });
          }
          return;
        }

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

        if (msg.type !== "trap") return;

        setRouters((prev) => {
          const idx = prev.findIndex((r) => r.id === msg.router.id);
          if (idx === -1) return prev;
          const next = [...prev];
          next[idx] = {
            ...next[idx],
            status: msg.router.status,
            needs_attention: msg.router.needs_attention,
          };
          return next;
        });

        const setIncidentsTarget = msg.router.router_type === "customer" ? setCustomerIncidents : setIncidents;
        setIncidentsTarget((prev) => {
          const idx = prev.findIndex((i) => i.id === msg.incident.id);
          const merged = {
            ...(idx !== -1 ? prev[idx] : {}),
            id: msg.incident.id,
            router_id: msg.incident.router_id,
            incident_type: msg.incident.incident_type,
            status: msg.incident.status,
            trap_count: msg.incident.trap_count,
            description: msg.incident.description,
            updated_at: msg.trap.received_at,
            opened_at: idx !== -1 ? prev[idx].opened_at : msg.trap.received_at,
            remediation: msg.incident.remediation ?? (idx !== -1 ? prev[idx].remediation : undefined),
            impact: msg.incident.impact ?? null,
            fault_location: msg.incident.fault_location ?? (idx !== -1 ? prev[idx].fault_location : null),
          };
          if (idx === -1) return [merged, ...prev].slice(0, 200);
          const next = [...prev];
          next[idx] = merged;
          return next;
        });

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
  }, [authenticated, logout]);

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
              incidents={[...incidents, ...customerIncidents]}
              routers={routers}
              teams={teams}
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
