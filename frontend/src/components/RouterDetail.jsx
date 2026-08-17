import { useEffect, useState } from "react";
import { fetchRouterBackups, fetchRouterRemediation, fetchRouterTraps } from "../api.js";

const REMEDIATION_BADGE = {
  success: "up",
  failed: "down",
  skipped: "unknown",
};

function BackupRow({ backup }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ borderBottom: "1px solid #262f45", padding: "0.5rem 0" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <div>{new Date(backup.taken_at).toLocaleString()}</div>
          <div style={{ color: "#9aa4bf", fontSize: "0.78rem" }}>{backup.reason}</div>
        </div>
        <span className="link" onClick={() => setOpen((o) => !o)}>
          {open ? "hide config" : "view config"}
        </span>
      </div>
      {open && (
        <pre
          style={{
            whiteSpace: "pre-wrap",
            background: "#0f1420",
            border: "1px solid #262f45",
            borderRadius: 6,
            padding: "0.6rem 0.75rem",
            marginTop: "0.5rem",
            fontSize: "0.75rem",
            color: "#9aa4bf",
          }}
        >
          {backup.config_text}
        </pre>
      )}
    </div>
  );
}

export default function RouterDetail({ router, routers = [], peerings = [], onClose, onSelectRouter }) {
  const [traps, setTraps] = useState([]);
  const [backups, setBackups] = useState([]);
  const [remediation, setRemediation] = useState([]);
  const [loading, setLoading] = useState(true);

  const routerById = Object.fromEntries(routers.map((r) => [r.id, r]));
  const isPrimary = router.router_type === "primary";
  const parent = !isPrimary ? routerById[router.parent_router_id] : null;
  const bgpPeers = isPrimary
    ? peerings
        .filter((p) => p.router_a_id === router.id || p.router_b_id === router.id)
        .map((p) => ({
          peer: routerById[p.router_a_id === router.id ? p.router_b_id : p.router_a_id],
          status: p.status,
          bundle: p.router_a_id === router.id ? p.bundle_a : p.bundle_b,
          distanceKm: p.distance_km,
          repeaterCount: p.repeater_count,
          openL1IncidentId: p.open_l1_incident_id,
        }))
        .filter((p) => p.peer)
    : [];

  useEffect(() => {
    setLoading(true);
    Promise.all([
      fetchRouterTraps(router.id),
      fetchRouterBackups(router.id),
      fetchRouterRemediation(router.id),
    ])
      .then(([t, b, r]) => {
        setTraps(t);
        setBackups(b);
        setRemediation(r);
      })
      .finally(() => setLoading(false));
  }, [router.id]);

  return (
    <div
      style={{
        position: "fixed",
        top: 0,
        right: 0,
        bottom: 0,
        width: "min(520px, 100%)",
        background: "#111726",
        borderLeft: "1px solid #262f45",
        padding: "1.25rem",
        overflowY: "auto",
        boxShadow: "-8px 0 24px rgba(0,0,0,0.4)",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h2 style={{ margin: 0 }}>{router.hostname}</h2>
        <button onClick={onClose} style={{ background: "none", border: "none", color: "#9aa4bf", fontSize: "1.2rem", cursor: "pointer" }}>✕</button>
      </div>
      <p style={{ color: "#9aa4bf" }}>
        {router.site_name} — {router.city}, {router.country}
      </p>
      <div className="table-scroll" style={{ marginBottom: "1.25rem" }}>
        <table>
          <tbody>
            <tr><th>Mgmt IP</th><td>{router.mgmt_ip}</td></tr>
            <tr><th>Vendor / Model</th><td>{router.vendor} {router.model}</td></tr>
            <tr>
              <th>Type</th>
              <td>
                {isPrimary ? "Primary (backbone)" : "Customer CPE"}
                {router.asn && ` · AS${router.asn}`}
              </td>
            </tr>
            {parent && (
              <tr>
                <th>Uplink</th>
                <td>
                  <span className="link" onClick={() => onSelectRouter(parent.id)}>{parent.hostname}</span>
                </td>
              </tr>
            )}
            {router.isis_net && <tr><th>IS-IS NET</th><td>{router.isis_net}</td></tr>}
            <tr><th>Status</th><td><span className={`badge ${router.status}`}>{router.status}</span></td></tr>
            <tr><th>Last seen</th><td>{router.last_seen_at ? new Date(router.last_seen_at).toLocaleString() : "never"}</td></tr>
            <tr><th>Coordinates</th><td>{router.latitude.toFixed(3)}, {router.longitude.toFixed(3)}</td></tr>
          </tbody>
        </table>
      </div>

      {isPrimary && (
        <>
          <h3>BGP peers ({bgpPeers.length})</h3>
          <div className="table-scroll" style={{ marginBottom: "1.25rem" }}>
            <table>
              <tbody>
                {bgpPeers.map(({ peer, status, bundle, distanceKm, repeaterCount, openL1IncidentId }) => (
                  <tr key={peer.id}>
                    <td>
                      <span className="link" onClick={() => onSelectRouter(peer.id)}>{peer.hostname}</span>
                    </td>
                    <td>{peer.city}, {peer.country}</td>
                    <td><span className={`badge ${status === "down" ? "down" : "up"}`}>{status}</span></td>
                    <td>
                      {bundle
                        ? `${bundle.name} — ${bundle.members.filter((m) => m.isis_adjacency_up).length}/${bundle.members.length} up · ${(bundle.total_bandwidth_mbps / 1000).toFixed(0)} Gbps`
                        : "—"}
                    </td>
                    <td>
                      {distanceKm != null
                        ? `${distanceKm.toFixed(0)}km SMF · ${repeaterCount} repeater${repeaterCount === 1 ? "" : "s"}`
                        : "—"}
                    </td>
                    <td>
                      {openL1IncidentId != null ? (
                        <span className="badge down" title={`L1 incident #${openL1IncidentId}`}>
                          ⚠ fiber fault (L1)
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                ))}
                {bgpPeers.length === 0 && (
                  <tr><td colSpan={5}>No BGP peers seeded yet.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}

      {loading && <p>Loading…</p>}

      {!loading && (
        <>
          <h3>Auto-heal history</h3>
          <p style={{ color: "#9aa4bf", fontSize: "0.78rem", marginTop: "-0.5rem" }}>
            A config backup is always taken before any remediation is attempted.
          </p>
          <div className="table-scroll" style={{ marginBottom: "1.25rem" }}>
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Incident</th>
                  <th>Action</th>
                  <th>Result</th>
                </tr>
              </thead>
              <tbody>
                {remediation.map((r) => (
                  <tr key={r.id}>
                    <td>{new Date(r.started_at).toLocaleString()}</td>
                    <td>{r.incident_type}</td>
                    <td>{r.action_type}</td>
                    <td>
                      <span className={`badge ${REMEDIATION_BADGE[r.status] || "unknown"}`}>{r.status}</span>
                    </td>
                  </tr>
                ))}
                {remediation.length === 0 && (
                  <tr><td colSpan={4}>No auto-heal actions yet.</td></tr>
                )}
              </tbody>
            </table>
          </div>

          <h3>Configuration backups</h3>
          <div style={{ marginBottom: "1.25rem" }}>
            {backups.map((b) => (
              <BackupRow key={b.id} backup={b} />
            ))}
            {backups.length === 0 && <p style={{ color: "#9aa4bf" }}>No backups taken yet.</p>}
          </div>

          <h3>Recent traps</h3>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Trap</th>
                  <th>Interface</th>
                  <th>Severity</th>
                </tr>
              </thead>
              <tbody>
                {traps.map((t) => (
                  <tr key={t.id}>
                    <td>{new Date(t.received_at).toLocaleString()}</td>
                    <td>{t.trap_name}</td>
                    <td>{t.interface_name || "—"}</td>
                    <td>{t.severity}</td>
                  </tr>
                ))}
                {traps.length === 0 && (
                  <tr><td colSpan={4}>No traps received yet.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
