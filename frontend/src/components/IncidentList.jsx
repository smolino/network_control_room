import { useMemo, useState } from "react";
import { fetchIncidentRemediation } from "../api.js";

const REMEDIATION_BADGE = {
  success: "up",
  failed: "down",
  skipped: "unknown",
};

// Workflow stages, in lifecycle order - see backend/app/models.py
// IncidentStage and backend/app/incidents/service.py.
const STAGES = ["new", "suppressed", "monitoring", "triage", "acknowledged", "dispatched", "resolved"];
const STAGE_BADGE = {
  new: "unknown",
  suppressed: "unknown",
  monitoring: "flapping",
  triage: "down",
  acknowledged: "flapping",
  dispatched: "flapping",
  resolved: "up",
};

export function StageBadge({ incident }) {
  const stage = incident.stage ?? incident.status;
  return <span className={`badge ${STAGE_BADGE[stage] ?? incident.status}`}>{stage}</span>;
}

const PRIORITY_RANK = { P1: 1, P2: 2, P3: 3, P4: 4 };

const IMPACT_COLOR = { outage: "#f87171", degraded: "#facc15" };

// Open, service-affecting incidents only (see backend/app/impact.py) -
// everything else has no blast radius. Sort key: customers actually down
// first, then everyone merely degraded.
function impactScore(incident) {
  const impact = incident.impact;
  return impact ? impact.customers_down * 100000 + impact.customer_count : -1;
}

function ImpactCell({ incident, onShowImpact }) {
  const impact = incident.impact;
  if (!impact) return <span style={{ color: "#5b6479" }}>—</span>;
  const label =
    impact.customers_down > 0
      ? `${impact.customers_down}/${impact.customer_count} customers down`
      : `${impact.customer_count} customer${impact.customer_count === 1 ? "" : "s"} at risk`;
  return (
    <div>
      <span
        style={{ color: IMPACT_COLOR[impact.level], fontWeight: 600, whiteSpace: "nowrap" }}
        title={
          impact.level === "outage"
            ? "Customers that have lost service: their primary is cut off from the backbone, or the CPE itself is down"
            : "Customers still reachable, but over reduced redundancy"
        }
      >
        {impact.level === "outage" ? "⛔" : "⚠"} {label}
      </span>
      {impact.primary_router_ids.length > 0 && (
        <div style={{ fontSize: "0.72rem", color: "#9aa4bf" }}>
          via {impact.primary_router_ids.length} primar{impact.primary_router_ids.length === 1 ? "y" : "ies"}
        </div>
      )}
      {onShowImpact && (
        <span className="link" style={{ fontSize: "0.72rem" }} onClick={() => onShowImpact(incident)}>
          Show on map
        </span>
      )}
    </div>
  );
}

// Fiber cuts only: the span the alarm isolated the fault to, and the
// repeaters bounding it - see backend/app/fault_location.py.
export function FaultLocationNote({ location, onLocate }) {
  if (!location) return null;
  const { start, end } = location;
  return (
    <div style={{ fontSize: "0.74rem", color: "#fb923c", marginTop: "0.25rem" }}>
      📍 Faulty span <strong>{start.label} → {end.label}</strong> (span {location.segment_index + 1} of{" "}
      {location.segment_count}, km {start.km_from_a}–{end.km_from_a} of {location.distance_km})
      {location.repeaters_to_check.length > 0 && <> · check {location.repeaters_to_check.join(" & ")}</>}
      {onLocate && (
        <>
          {" "}
          <span className="link" onClick={onLocate}>
            Locate on map
          </span>
        </>
      )}
    </div>
  );
}

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

// Read-only history/search view - acting on incidents happens on the Human
// Review board.
export default function IncidentList({ incidents, routers, onSelectRouter, onShowImpact, onLocateFault }) {
  const [sortBy, setSortBy] = useState("updated");
  const [statusFilter, setStatusFilter] = useState("all");
  const [stageFilter, setStageFilter] = useState("all");
  const [typeFilter, setTypeFilter] = useState("all");
  const [layerFilter, setLayerFilter] = useState("all");
  // Symptomatic incidents (root_cause_incident_id set) are the L3 fallout
  // of an open L1 fiber fault - hidden by default so the list surfaces the
  // root cause instead of a wall of correlated noise, per the design doc's
  // "suppressed from top-level view, visible on drill-down" model.
  const [hideSymptomatic, setHideSymptomatic] = useState(true);

  const routerById = useMemo(() => Object.fromEntries(routers.map((r) => [r.id, r])), [routers]);
  const incidentById = useMemo(() => Object.fromEntries(incidents.map((i) => [i.id, i])), [incidents]);
  const types = useMemo(
    () => Array.from(new Set(incidents.map((i) => i.incident_type))).sort(),
    [incidents]
  );

  const matching = incidents.filter((i) => {
    if (statusFilter !== "all" && i.status !== statusFilter) return false;
    if (stageFilter !== "all" && i.stage !== stageFilter) return false;
    if (layerFilter !== "all" && i.layer !== layerFilter) return false;
    if (hideSymptomatic && i.root_cause_incident_id != null) return false;
    return typeFilter === "all" || i.incident_type === typeFilter;
  });
  const filtered =
    sortBy === "impact"
      ? [...matching].sort((a, b) => impactScore(b) - impactScore(a))
      : sortBy === "priority"
        ? [...matching].sort((a, b) => (PRIORITY_RANK[a.priority] ?? 9) - (PRIORITY_RANK[b.priority] ?? 9))
        : matching;

  return (
    <div className="page">
      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.75rem", marginBottom: "1rem", alignItems: "center" }}>
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="all">All statuses</option>
          <option value="open">Open</option>
          <option value="resolved">Resolved</option>
        </select>
        <select value={stageFilter} onChange={(e) => setStageFilter(e.target.value)}>
          <option value="all">All stages</option>
          {STAGES.map((st) => (
            <option key={st} value={st}>{st}</option>
          ))}
        </select>
        <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
          <option value="all">All types</option>
          {types.map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
        <select value={layerFilter} onChange={(e) => setLayerFilter(e.target.value)}>
          <option value="all">All layers</option>
          <option value="L1">L1 (physical)</option>
          <option value="L3">L3 (logical)</option>
        </select>
        <label style={{ display: "flex", alignItems: "center", gap: "0.4rem", cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={hideSymptomatic}
            onChange={(e) => setHideSymptomatic(e.target.checked)}
          />
          Hide symptomatic
        </label>
        <select value={sortBy} onChange={(e) => setSortBy(e.target.value)}>
          <option value="updated">Sort: most recent</option>
          <option value="priority">Sort: priority</option>
          <option value="impact">Sort: customer impact</option>
        </select>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Updated</th>
              <th>Priority</th>
              <th>Layer</th>
              <th>Type</th>
              <th>Router</th>
              <th>Interface</th>
              <th>Status</th>
              <th>Impact</th>
              <th>Traps</th>
              <th>Description</th>
              <th>Auto-heal</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((i) => {
              const rootCause = i.root_cause_incident_id != null ? incidentById[i.root_cause_incident_id] : null;
              return (
                <tr key={i.id}>
                  <td>{i.updated_at ? new Date(i.updated_at).toLocaleString() : "—"}</td>
                  <td>{i.priority ?? "—"}</td>
                  <td>
                    <span className={`badge ${i.layer === "L1" ? "down" : "unknown"}`}>{i.layer}</span>
                  </td>
                  <td>{i.incident_type}</td>
                  <td>
                    <span className="link" onClick={() => onSelectRouter(i.router_id)}>
                      {routerById[i.router_id]?.hostname || `#${i.router_id}`}
                    </span>
                  </td>
                  <td>{i.interface_name || "—"}</td>
                  <td>
                    <StageBadge incident={i} />
                    {i.owner_username && i.status === "open" && (
                      <div style={{ fontSize: "0.7rem", color: "#9aa4bf", marginTop: "0.15rem" }}>
                        owner {i.owner_username}
                      </div>
                    )}
                    {i.status === "resolved" && i.resolution_reason && (
                      <div
                        style={{ fontSize: "0.7rem", color: "#9aa4bf", marginTop: "0.15rem" }}
                        title={i.resolution_note ?? undefined}
                      >
                        {i.resolution_reason.replace(/_/g, " ")}
                        {i.resolved_by && i.resolved_by !== "system" && ` by ${i.resolved_by}`}
                      </div>
                    )}
                  </td>
                  <td><ImpactCell incident={i} onShowImpact={onShowImpact} /></td>
                  <td>{i.trap_count}</td>
                  <td className="wrap-cell">
                    {i.description}
                    <FaultLocationNote
                      location={i.fault_location}
                      onLocate={onLocateFault ? () => onLocateFault(i) : undefined}
                    />
                    {i.root_cause_incident_id != null && (
                      <div style={{ fontSize: "0.72rem", color: "#fb923c", marginTop: "0.2rem" }}>
                        ↳ symptomatic of #{i.root_cause_incident_id}
                        {rootCause ? ` (${rootCause.incident_type})` : ""}
                      </div>
                    )}
                  </td>
                  <td><AutoHealCell incident={i} /></td>
                </tr>
              );
            })}
            {filtered.length === 0 && (
              <tr>
                <td colSpan={11}>No incidents match this filter.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
