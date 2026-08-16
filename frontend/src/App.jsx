import { useEffect, useState, useCallback, useRef } from "react";
import { connectEvents, fetchBgpPeerings, fetchIncidents, fetchRouters, fetchStats, fetchTeams } from "./api.js";
import MapView from "./components/MapView.jsx";
import Dashboard from "./components/Dashboard.jsx";
import RouterList from "./components/RouterList.jsx";
import IncidentList from "./components/IncidentList.jsx";
import RouterDetail from "./components/RouterDetail.jsx";
import HumanReview from "./components/HumanReview.jsx";
import Login from "./components/Login.jsx";
import SettingsPanel from "./components/SettingsPanel.jsx";

const CURRENT_USER_KEY = "ncr_current_user";

const TABS = [
  { key: "map", label: "Map" },
  { key: "dashboard", label: "Dashboard" },
  { key: "routers", label: "Routers" },
  { key: "incidents", label: "Incidents" },
  { key: "customer-incidents", label: "Customer Incidents" },
  { key: "human-review", label: "Human Review" },
];

export default function App() {
  const [currentUser, setCurrentUser] = useState(
    () => sessionStorage.getItem(CURRENT_USER_KEY) || ""
  );
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
      ws.onclose = () => {
        setConnected(false);
        retryTimer = setTimeout(connect, 3000);
      };
      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      clearTimeout(retryTimer);
      ws?.close();
    };
  }, [authenticated]);

  const selectedRouter = routers.find((r) => r.id === selectedRouterId) || null;

  if (!authenticated) {
    return (
      <Login
        onLogin={(username) => {
          sessionStorage.setItem(CURRENT_USER_KEY, username);
          setCurrentUser(username);
        }}
      />
    );
  }

  return (
    <>
      <header className="topbar">
        <h1>Network Control Room</h1>
        <nav className="tabs">
          {TABS.map((t) => (
            <button
              key={t.key}
              className={view === t.key ? "active" : ""}
              onClick={() => setView(t.key)}
            >
              {t.label}
            </button>
          ))}
        </nav>
        {stats && (
          <div className="stats-strip">
            <span><strong>{stats.total_routers}</strong> primaries</span>
            <span><strong>{stats.total_customer_routers}</strong> customers</span>
            <span><strong>{stats.routers_up}</strong> up</span>
            <span><strong>{stats.routers_down}</strong> down</span>
            <span><strong>{stats.routers_flapping}</strong> flapping</span>
            <span><strong>{stats.open_incidents}</strong> open incidents</span>
            <span>{connected ? "🟢 live" : "🔴 reconnecting"}</span>
          </div>
        )}
        <div className="topbar-actions">
          <span className="current-user">{currentUser}</span>
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
            onClick={() => {
              sessionStorage.removeItem(CURRENT_USER_KEY);
              setCurrentUser("");
            }}
          >
            Log out
          </button>
        </div>
      </header>
      {showSettings && (
        <SettingsPanel
          currentUser={currentUser}
          teams={teams}
          onTeamsChanged={reloadTeams}
          onClose={() => setShowSettings(false)}
        />
      )}
      <main>
        {view === "map" && (
          <MapView
            routers={routers}
            peerings={peerings}
            onSelectRouter={(id) => {
              setSelectedRouterId(id);
              setView("routers");
            }}
          />
        )}
        {view === "dashboard" && <Dashboard stats={stats} incidents={incidents} />}
        {view === "routers" && (
          <RouterList
            routers={routers}
            selectedRouterId={selectedRouterId}
            onSelectRouter={setSelectedRouterId}
          />
        )}
        {view === "incidents" && (
          <IncidentList
            incidents={incidents}
            routers={routers}
            onSelectRouter={(id) => {
              setSelectedRouterId(id);
              setView("routers");
            }}
          />
        )}
        {view === "customer-incidents" && (
          <IncidentList
            incidents={customerIncidents}
            routers={routers}
            onSelectRouter={(id) => {
              setSelectedRouterId(id);
              setView("routers");
            }}
          />
        )}
        {view === "human-review" && (
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
      {view === "routers" && selectedRouter && (
        <RouterDetail
          router={selectedRouter}
          routers={routers}
          peerings={peerings}
          onClose={() => setSelectedRouterId(null)}
          onSelectRouter={setSelectedRouterId}
        />
      )}
    </>
  );
}
