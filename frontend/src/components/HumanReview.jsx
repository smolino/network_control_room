import { useEffect, useMemo, useState } from "react";
import {
  acknowledgeIncident,
  bulkResolveIncidents,
  fetchIncident,
  fetchIncidentAnalysis,
  fetchIncidentNotifications,
  notifyIncidentTeam,
  resolveIncident,
  RESOLUTION_REASONS,
} from "../api.js";
import { useCurrentUser, useHasRole } from "../auth.js";
import { FaultLocationNote, StageBadge } from "./IncidentList.jsx";

const KIND_LABEL = { maintenance: "Maintenance", soc: "SOC" };

// One column per human stage - see backend/app/incidents/service.py.
const COLUMNS = [
  { stage: "triage", title: "Triage", hint: "Nobody has picked these up yet" },
  { stage: "acknowledged", title: "Acknowledged", hint: "An operator owns these" },
  { stage: "dispatched", title: "Dispatched", hint: "Handed to a SOC/maintenance team, waiting on them" },
];

export const PRIORITY_COLOR = { P1: "#f87171", P2: "#fb923c", P3: "#facc15", P4: "#9aa4bf" };

export function PriorityTag({ priority }) {
  if (!priority) return null;
  const color = PRIORITY_COLOR[priority] ?? "#9aa4bf";
  return (
    <span className="prio" style={{ color, borderColor: color }} title="Priority: severity, layer, router tier and customer impact">
      {priority}
    </span>
  );
}

function since(iso) {
  if (!iso) return "—";
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours}h ${minutes % 60}m` : `${Math.floor(hours / 24)}d`;
}

function ImpactLine({ impact }) {
  if (!impact) return null;
  const outage = impact.level === "outage";
  return (
    <div style={{ color: outage ? "#f87171" : "#facc15" }}>
      {outage ? "⛔" : "⚠"}{" "}
      {impact.customers_down > 0
        ? `${impact.customers_down}/${impact.customer_count} customers down`
        : `${impact.customer_count} customer${impact.customer_count === 1 ? "" : "s"} at risk`}
    </div>
  );
}

function IncidentCard({ incident, router, selected, onSelect, onAck, canAct }) {
  return (
    <div
      className={`incident-card${selected ? " selected" : ""}`}
      style={{ borderLeftColor: PRIORITY_COLOR[incident.priority] ?? "#262f45" }}
      onClick={onSelect}
    >
      <div className="incident-card-head">
        <PriorityTag priority={incident.priority} />
        <strong>{incident.incident_type}</strong>
        <span className="muted" style={{ marginLeft: "auto" }} title={`Opened ${new Date(incident.opened_at).toLocaleString()}`}>
          #{incident.id} · {since(incident.opened_at)}
        </span>
      </div>
      <div>
        {router?.hostname || `router #${incident.router_id}`}
        {incident.interface_name && <span className="muted"> · {incident.interface_name}</span>}{" "}
        <span className={`badge ${incident.layer === "L1" ? "down" : "unknown"}`}>{incident.layer}</span>
      </div>
      <ImpactLine impact={incident.impact} />
      {incident.remediation && (
        <div className="muted">
          Auto-heal: {incident.remediation.action_type} · {incident.remediation.status}
        </div>
      )}
      {incident.symptom_count > 0 && (
        <div style={{ color: "#fb923c" }}>
          Root cause of {incident.symptom_count} suppressed symptom{incident.symptom_count === 1 ? "" : "s"}
        </div>
      )}
      {incident.owner_username && <div className="muted">Owner: {incident.owner_username}</div>}
      {canAct && incident.stage === "triage" && (
        <button
          style={{ alignSelf: "flex-start", marginTop: "0.2rem" }}
          onClick={(e) => {
            e.stopPropagation();
            onAck();
          }}
        >
          Acknowledge
        </button>
      )}
    </div>
  );
}

function ResolveControls({ onResolve, busy, label = "Resolve" }) {
  const [reason, setReason] = useState("fixed");
  const [note, setNote] = useState("");
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem", alignItems: "center" }}>
      <select value={reason} onChange={(e) => setReason(e.target.value)} aria-label="Resolution reason">
        {RESOLUTION_REASONS.map((r) => (
          <option key={r.value} value={r.value}>{r.label}</option>
        ))}
      </select>
      <input
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Note (optional)"
        maxLength={255}
        style={{ flex: "1 1 12rem" }}
      />
      <button disabled={busy} onClick={() => onResolve({ reason, note })}>
        {busy ? "Resolving…" : label}
      </button>
    </div>
  );
}

function IncidentDetail({ incident, router, teams, onClose, onSelectRouter, onShowImpact, onLocateFault }) {
  const currentUser = useCurrentUser();
  const canAct = useHasRole("operator");
  const [loading, setLoading] = useState(false);
  const [analysis, setAnalysis] = useState(null);
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [teamKind, setTeamKind] = useState("maintenance");
  const [teamId, setTeamId] = useState("");
  const [notifications, setNotifications] = useState([]);
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);

  const teamsForKind = useMemo(() => teams.filter((t) => t.kind === teamKind), [teams, teamKind]);
  const open = incident.status === "open";
  const isOwner = incident.owner_user_id != null && incident.owner_user_id === currentUser?.id;

  useEffect(() => {
    let cancelled = false;
    setAnalysis(null);
    setError(null);
    setTeamId("");
    setLoading(true);
    Promise.all([fetchIncidentAnalysis(incident.id), fetchIncidentNotifications(incident.id)])
      .then(([a, n]) => {
        if (cancelled) return;
        setAnalysis(a);
        setSubject(a.subject);
        setBody(`${a.description}\n\nSuggested next step:\n${a.suggested_solution}`);
        setTeamKind(a.recommended_team_kind);
        setNotifications(n);
      })
      .catch((err) => !cancelled && setError(err.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [incident.id]);

  // The board updates itself from the WebSocket event each action
  // broadcasts - these only need to surface errors.
  const run = async (key, action) => {
    setBusy(key);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  };

  const handleSend = () =>
    run("send", async () => {
      await notifyIncidentTeam(incident.id, { teamId: Number(teamId), subject, body });
      setNotifications(await fetchIncidentNotifications(incident.id));
    });

  return (
    <div className="review-detail wrap-block">
      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem", alignItems: "center", marginBottom: "0.6rem" }}>
        <PriorityTag priority={incident.priority} />
        <strong>
          #{incident.id} {incident.incident_type}
        </strong>
        <StageBadge incident={incident} />
        <span className="muted">
          on{" "}
          <span className="link" onClick={() => onSelectRouter(incident.router_id)}>
            {router?.hostname || `#${incident.router_id}`}
          </span>
          {incident.interface_name && ` (${incident.interface_name})`} · opened {new Date(incident.opened_at).toLocaleString()}
        </span>
        <button style={{ marginLeft: "auto" }} onClick={onClose} aria-label="Close details">
          ✕
        </button>
      </div>

      <div className="muted" style={{ marginBottom: "0.4rem" }}>{incident.description}</div>
      <ImpactLine impact={incident.impact} />
      {incident.impact && (
        <span className="link" style={{ fontSize: "0.78rem" }} onClick={() => onShowImpact(incident)}>
          Show impact on map
        </span>
      )}
      <FaultLocationNote location={incident.fault_location} onLocate={() => onLocateFault(incident)} />
      {incident.remediation && (
        <div className="muted" style={{ marginTop: "0.4rem" }}>
          Auto-heal ({incident.remediation.status}): {incident.remediation.summary}
        </div>
      )}
      {incident.owner_username && (
        <div className="muted" style={{ marginTop: "0.25rem" }}>
          Owner: {incident.owner_username}
          {incident.acknowledged_at && ` since ${new Date(incident.acknowledged_at).toLocaleString()}`}
        </div>
      )}
      {!open && (
        <div style={{ marginTop: "0.5rem", color: "#4ade80" }}>
          Resolved by {incident.resolved_by ?? "—"}
          {incident.resolution_reason && ` (${incident.resolution_reason.replace(/_/g, " ")})`}
          {incident.closed_at && ` at ${new Date(incident.closed_at).toLocaleString()}`}
          {incident.resolution_note && ` — ${incident.resolution_note}`}
        </div>
      )}

      {error && <div style={{ color: "#f87171", marginTop: "0.5rem" }}>{error}</div>}

      {open && canAct && (
        <div className="review-section">
          <div style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem", alignItems: "center" }}>
            {!isOwner && (
              <button disabled={busy !== null} onClick={() => run("ack", () => acknowledgeIncident(incident.id))}>
                {incident.owner_user_id != null ? "Take ownership" : "Acknowledge"}
              </button>
            )}
          </div>
          <div style={{ marginTop: "0.6rem" }}>
            <ResolveControls
              busy={busy === "resolve"}
              label={incident.symptom_count > 0 ? `Resolve with ${incident.symptom_count} symptom(s)` : "Resolve"}
              onResolve={(params) => run("resolve", () => resolveIncident(incident.id, params))}
            />
          </div>
        </div>
      )}

      <div className="review-section">
        <h3 style={{ fontSize: "0.9rem", margin: "0 0 0.5rem" }}>Analysis & hand-off</h3>
        {loading && <div>Analyzing…</div>}
        {!loading && analysis && (
          <>
            <div style={{ marginBottom: "0.75rem", color: "#9aa4bf" }}>
              <strong style={{ color: "#e6e9f0" }}>Suggested solution: </strong>
              {analysis.suggested_solution}
            </div>

            {open && (
              <>
                <div style={{ display: "flex", flexWrap: "wrap", gap: "0.75rem", marginBottom: "0.5rem", alignItems: "center" }}>
                  <select value={teamKind} onChange={(e) => { setTeamKind(e.target.value); setTeamId(""); }}>
                    <option value="maintenance">Send to Maintenance</option>
                    <option value="soc">Send to SOC</option>
                  </select>
                  <select value={teamId} onChange={(e) => setTeamId(e.target.value)}>
                    <option value="">Select team…</option>
                    {teamsForKind.map((t) => (
                      <option key={t.id} value={t.id}>{t.name} ({t.email})</option>
                    ))}
                  </select>
                  <button
                    onClick={handleSend}
                    disabled={!canAct || !teamId || busy !== null}
                    title={canAct ? "Emails the team and moves the incident to Dispatched" : "Requires the operator role"}
                  >
                    {busy === "send" ? "Sending…" : "Send & dispatch"}
                  </button>
                </div>
                {teamsForKind.length === 0 && (
                  <div style={{ color: "#f87171", marginBottom: "0.5rem" }}>
                    No {KIND_LABEL[teamKind].toLowerCase()} teams configured yet — add one in Settings → Teams.
                  </div>
                )}
                <input value={subject} onChange={(e) => setSubject(e.target.value)} style={{ width: "100%", marginBottom: "0.4rem" }} />
                <textarea
                  value={body}
                  onChange={(e) => setBody(e.target.value)}
                  rows={5}
                  style={{ width: "100%", fontFamily: "inherit", fontSize: "0.82rem" }}
                />
              </>
            )}

            {notifications.length > 0 && (
              <div style={{ marginTop: "0.6rem" }}>
                <div style={{ color: "#9aa4bf", marginBottom: "0.25rem" }}>Notification history:</div>
                {notifications.map((n) => (
                  <div key={n.id} style={{ color: "#9aa4bf", fontSize: "0.78rem" }}>
                    {new Date(n.sent_at).toLocaleString()} — {n.subject} ·{" "}
                    <span className={`badge ${n.status === "failed" ? "down" : n.status === "sent" ? "up" : "unknown"}`}>
                      {n.status}
                    </span>
                    {n.error && <span style={{ color: "#f87171" }}> ({n.error})</span>}
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export default function HumanReview({
  queue,
  routers,
  teams,
  focusIncidentId,
  onFocusIncident,
  onSelectRouter,
  onShowImpact,
  onLocateFault,
}) {
  const currentUser = useCurrentUser();
  const canAct = useHasRole("operator");
  const [onlyMine, setOnlyMine] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [error, setError] = useState(null);
  // The selected incident once it's no longer on the board (resolved, or
  // opened from an email link after the fact) - fetched so its final state
  // still shows instead of the panel just vanishing.
  const [offBoard, setOffBoard] = useState(null);

  const routerById = useMemo(() => Object.fromEntries(routers.map((r) => [r.id, r])), [routers]);
  const onBoard = focusIncidentId != null ? queue.find((i) => i.id === focusIncidentId) : null;

  useEffect(() => {
    if (focusIncidentId == null || onBoard) {
      setOffBoard(null);
      return;
    }
    let cancelled = false;
    fetchIncident(focusIncidentId)
      .then((i) => !cancelled && setOffBoard(i))
      .catch((err) => !cancelled && setError(`Couldn't load incident #${focusIncidentId}: ${err.message}`));
    return () => {
      cancelled = true;
    };
  }, [focusIncidentId, Boolean(onBoard)]);

  const selected = onBoard ?? offBoard;

  const columns = useMemo(
    () =>
      COLUMNS.map((c) => ({
        ...c,
        incidents: queue.filter(
          (i) => i.stage === c.stage && (!onlyMine || c.stage === "triage" || i.owner_user_id === currentUser?.id)
        ),
      })),
    [queue, onlyMine, currentUser]
  );
  const triage = columns[0].incidents;

  const handleAck = async (id) => {
    setError(null);
    try {
      await acknowledgeIncident(id);
    } catch (err) {
      setError(err.message);
    }
  };

  const handleBulkResolve = async (params) => {
    if (!window.confirm(`Resolve all ${triage.length} incident(s) in Triage (and their symptoms)?`)) return;
    setBulkBusy(true);
    setError(null);
    try {
      await bulkResolveIncidents(
        triage.map((i) => i.id),
        params
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setBulkBusy(false);
    }
  };

  return (
    <div className="page">
      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.75rem", alignItems: "center", marginBottom: "0.75rem" }}>
        <h2 style={{ fontSize: "1.05rem", margin: 0 }}>Human review ({queue.length})</h2>
        <label style={{ display: "flex", alignItems: "center", gap: "0.4rem", cursor: "pointer" }}>
          <input type="checkbox" checked={onlyMine} onChange={(e) => setOnlyMine(e.target.checked)} />
          Only mine
        </label>
        {canAct && triage.length > 0 && (
          <div style={{ marginLeft: "auto", minWidth: "min(100%, 28rem)" }}>
            <ResolveControls busy={bulkBusy} label={`Resolve all ${triage.length} in Triage`} onResolve={handleBulkResolve} />
          </div>
        )}
      </div>
      {error && <div style={{ color: "#f87171", marginBottom: "0.5rem" }}>{error}</div>}

      <div className="board">
        {columns.map((c) => (
          <section key={c.stage} className="board-column">
            <header className="board-column-head" title={c.hint}>
              <strong>{c.title}</strong>
              <span className="muted">{c.incidents.length}</span>
            </header>
            <div className="board-column-body">
              {c.incidents.map((i) => (
                <IncidentCard
                  key={i.id}
                  incident={i}
                  router={routerById[i.router_id]}
                  selected={i.id === focusIncidentId}
                  canAct={canAct}
                  onSelect={() => onFocusIncident(i.id === focusIncidentId ? null : i.id)}
                  onAck={() => handleAck(i.id)}
                />
              ))}
              {c.incidents.length === 0 && <div className="muted" style={{ padding: "0.5rem" }}>Nothing here.</div>}
            </div>
          </section>
        ))}
      </div>

      {selected && (
        <IncidentDetail
          key={selected.id}
          incident={selected}
          router={routerById[selected.router_id]}
          teams={teams}
          onClose={() => onFocusIncident(null)}
          onSelectRouter={onSelectRouter}
          onShowImpact={onShowImpact}
          onLocateFault={onLocateFault}
        />
      )}
    </div>
  );
}
