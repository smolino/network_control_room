import { useMemo, useState } from "react";
import { bulkResolveIncidents, fetchIncidentRemediation, resolveAllOpenIncidents, resolveIncident } from "../api.js";

const REMEDIATION_BADGE = {
  success: "up",
  failed: "down",
  skipped: "unknown",
};

// A synthetic filter value (not a real incident_type) selecting every
// incident whose auto-heal step declined to act - security/hardware/
// out-of-band-change events that always go to a human instead of being
// auto-remediated (see NOTIFY_ONLY_REASONS in the backend's remediation
// engine). Lets an operator pull up exactly what still needs manual
// triage regardless of which of those incident types it is.
const NEEDS_REVIEW_FILTER = "__needs_review__";

function AutoHealCell({ incident }) {
  const [expanded, setExpanded] = useState(false);
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(false);

  if (!incident.remediation) {
    return <span style={{ color: "#5b6479" }}>—</span>;
  }

  const toggle = async () => {
    const next = !expanded;
    setExpanded(next);
    if (next && !detail) {
      setLoading(true);
      try {
        const rows = await fetchIncidentRemediation(incident.id);
        setDetail(rows[0] || null);
      } finally {
        setLoading(false);
      }
    }
  };

  return (
    <div>
      <span
        className={`badge ${REMEDIATION_BADGE[incident.remediation.status] || "unknown"}`}
        style={{ cursor: "pointer" }}
        onClick={toggle}
        title="Click for details"
      >
        {incident.remediation.action_type} · {incident.remediation.status}
      </span>
      {expanded && (
        <div
          style={{
            marginTop: "0.4rem",
            padding: "0.6rem 0.75rem",
            background: "#0f1420",
            border: "1px solid #262f45",
            borderRadius: 6,
            fontSize: "0.78rem",
            maxWidth: 420,
          }}
        >
          <div style={{ marginBottom: "0.35rem" }}>{incident.remediation.summary}</div>
          {loading && <div>Loading log…</div>}
          {detail && (
            <>
              <div style={{ color: "#9aa4bf" }}>
                Backup taken first: <span className="link">#{detail.backup_id}</span> (see router's
                "Config backups" panel)
              </div>
              <pre
                style={{
                  whiteSpace: "pre-wrap",
                  margin: "0.4rem 0 0",
                  color: "#9aa4bf",
                  fontSize: "0.74rem",
                }}
              >
                {detail.log}
              </pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}

export default function IncidentList({ incidents, routers, onSelectRouter }) {
  const [statusFilter, setStatusFilter] = useState("all");
  const [typeFilter, setTypeFilter] = useState("all");
  const [resolvingIds, setResolvingIds] = useState(() => new Set());
  const [bulkResolving, setBulkResolving] = useState(false);
  const [resettingAll, setResettingAll] = useState(false);

  const routerById = useMemo(() => Object.fromEntries(routers.map((r) => [r.id, r])), [routers]);
  const types = useMemo(
    () => Array.from(new Set(incidents.map((i) => i.incident_type))).sort(),
    [incidents]
  );

  const filtered = incidents.filter((i) => {
    if (statusFilter !== "all" && i.status !== statusFilter) return false;
    if (typeFilter === "all") return true;
    if (typeFilter === NEEDS_REVIEW_FILTER) return i.remediation?.action_type === "NOTIFY_ONLY";
    return i.incident_type === typeFilter;
  });

  const openShown = filtered.filter((i) => i.status === "open");

  const handleResolve = async (id) => {
    setResolvingIds((prev) => new Set(prev).add(id));
    try {
      await resolveIncident(id);
    } catch (err) {
      console.error(err);
    } finally {
      setResolvingIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  };

  const handleBulkResolve = async () => {
    if (openShown.length === 0) return;
    if (!window.confirm(`Resolve ${openShown.length} open incident${openShown.length === 1 ? "" : "s"}?`)) {
      return;
    }
    setBulkResolving(true);
    try {
      await bulkResolveIncidents(openShown.map((i) => i.id));
    } catch (err) {
      console.error(err);
    } finally {
      setBulkResolving(false);
    }
  };

  const handleResetAll = async () => {
    if (!window.confirm("Reset ALL open incidents system-wide, across every router, regardless of the current filter? This cannot be undone.")) {
      return;
    }
    setResettingAll(true);
    try {
      const resolved = await resolveAllOpenIncidents();
      window.alert(`Reset ${resolved.length} open incident${resolved.length === 1 ? "" : "s"}.`);
    } catch (err) {
      console.error(err);
    } finally {
      setResettingAll(false);
    }
  };

  return (
    <div className="page">
      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.75rem", marginBottom: "1rem", alignItems: "center" }}>
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="all">All statuses</option>
          <option value="open">Open</option>
          <option value="resolved">Resolved</option>
        </select>
        <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
          <option value="all">All types</option>
          <option value={NEEDS_REVIEW_FILTER}>⚠ Needs manual review</option>
          {types.map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
        <button
          onClick={handleBulkResolve}
          disabled={openShown.length === 0 || bulkResolving}
          style={{
            marginLeft: "auto",
            padding: "0.4rem 0.9rem",
            background: openShown.length ? "#262f45" : "#161c2c",
            color: openShown.length ? "#fff" : "#5b6479",
            border: "1px solid #262f45",
            borderRadius: 6,
            cursor: openShown.length ? "pointer" : "not-allowed",
          }}
        >
          {bulkResolving ? "Resolving…" : `Resolve ${openShown.length} open (shown)`}
        </button>
        <button
          onClick={handleResetAll}
          disabled={resettingAll}
          title="Resolves every open incident system-wide, ignoring the filters above and the list's 200-row cap"
          style={{
            padding: "0.4rem 0.9rem",
            background: "#7c2d12",
            color: "#fff",
            border: "1px solid #9a3412",
            borderRadius: 6,
            cursor: resettingAll ? "not-allowed" : "pointer",
            opacity: resettingAll ? 0.6 : 1,
          }}
        >
          {resettingAll ? "Resetting…" : "Reset ALL open incidents"}
        </button>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Updated</th>
              <th>Type</th>
              <th>Router</th>
              <th>Interface</th>
              <th>Status</th>
              <th>Traps</th>
              <th>Description</th>
              <th>Auto-heal</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((i) => (
              <tr key={i.id}>
                <td>{i.updated_at ? new Date(i.updated_at).toLocaleString() : "—"}</td>
                <td>{i.incident_type}</td>
                <td>
                  <span className="link" onClick={() => onSelectRouter(i.router_id)}>
                    {routerById[i.router_id]?.hostname || `#${i.router_id}`}
                  </span>
                </td>
                <td>{i.interface_name || "—"}</td>
                <td>
                  <span className={`badge ${i.status}`}>{i.status}</span>
                  {i.resolved_manually && (
                    <div style={{ fontSize: "0.7rem", color: "#9aa4bf", marginTop: "0.15rem" }}>
                      resolved manually
                    </div>
                  )}
                </td>
                <td>{i.trap_count}</td>
                <td>{i.description}</td>
                <td><AutoHealCell incident={i} /></td>
                <td>
                  {i.status === "open" && (
                    <span
                      className="link"
                      style={{ opacity: resolvingIds.has(i.id) ? 0.5 : 1 }}
                      onClick={() => !resolvingIds.has(i.id) && handleResolve(i.id)}
                    >
                      {resolvingIds.has(i.id) ? "Resolving…" : "Resolve"}
                    </span>
                  )}
                </td>
              </tr>
            ))}
            {filtered.length === 0 && (
              <tr>
                <td colSpan={9}>No incidents match this filter.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
