import { useMemo, useState } from "react";
import { fetchIncidentAnalysis, fetchIncidentNotifications, notifyIncidentTeam } from "../api.js";

const KIND_LABEL = { maintenance: "Maintenance", soc: "SOC" };

function IncidentReviewRow({ incident, router, teams, onSelectRouter, symptomaticCount }) {
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [analysis, setAnalysis] = useState(null);
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [teamKind, setTeamKind] = useState("maintenance");
  const [teamId, setTeamId] = useState("");
  const [notifications, setNotifications] = useState([]);
  const [sending, setSending] = useState(false);

  const teamsForKind = useMemo(() => teams.filter((t) => t.kind === teamKind), [teams, teamKind]);

  const toggle = async () => {
    const next = !expanded;
    setExpanded(next);
    if (next && !analysis) {
      setLoading(true);
      try {
        const [a, n] = await Promise.all([
          fetchIncidentAnalysis(incident.id),
          fetchIncidentNotifications(incident.id),
        ]);
        setAnalysis(a);
        setSubject(a.subject);
        setBody(`${a.description}\n\nSuggested next step:\n${a.suggested_solution}`);
        setTeamKind(a.recommended_team_kind);
        setNotifications(n);
      } finally {
        setLoading(false);
      }
    }
  };

  const handleSend = async () => {
    if (!teamId) return;
    setSending(true);
    try {
      await notifyIncidentTeam(incident.id, { teamId: Number(teamId), subject, body });
      const n = await fetchIncidentNotifications(incident.id);
      setNotifications(n);
    } finally {
      setSending(false);
    }
  };

  return (
    <>
      <tr>
        <td>{incident.updated_at ? new Date(incident.updated_at).toLocaleString() : "—"}</td>
        <td>
          <span className={`badge ${incident.layer === "L1" ? "down" : "unknown"}`}>{incident.layer}</span>
        </td>
        <td>{incident.incident_type}</td>
        <td>
          <span className="link" onClick={() => onSelectRouter(incident.router_id)}>
            {router?.hostname || `#${incident.router_id}`}
          </span>
        </td>
        <td>{incident.interface_name || "—"}</td>
        <td>{incident.trap_count}</td>
        <td>
          {incident.description}
          {symptomaticCount > 0 && (
            <div style={{ fontSize: "0.72rem", color: "#9aa4bf", marginTop: "0.2rem" }}>
              → root cause of {symptomaticCount} symptomatic incident{symptomaticCount === 1 ? "" : "s"} (suppressed
              from the Incident List by default)
            </div>
          )}
        </td>
        <td>
          <span className="link" onClick={toggle}>
            {expanded ? "Hide" : "Describe issue & suggest solution"}
          </span>
        </td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={8}>
            <div
              style={{
                padding: "0.75rem 1rem",
                background: "#0f1420",
                border: "1px solid #262f45",
                borderRadius: 6,
                fontSize: "0.85rem",
                margin: "0.25rem 0 0.75rem",
              }}
            >
              {loading && <div>Analyzing…</div>}
              {!loading && analysis && (
                <>
                  <div style={{ marginBottom: "0.5rem", color: "#9aa4bf" }}>{analysis.description}</div>
                  <div style={{ marginBottom: "0.75rem", color: "#9aa4bf" }}>
                    <strong style={{ color: "#e6e9f0" }}>Suggested solution: </strong>
                    {analysis.suggested_solution}
                  </div>

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
                    <button onClick={handleSend} disabled={!teamId || sending}>
                      {sending ? "Sending…" : "Send"}
                    </button>
                  </div>
                  {teamsForKind.length === 0 && (
                    <div style={{ color: "#f87171", marginBottom: "0.5rem" }}>
                      No {KIND_LABEL[teamKind].toLowerCase()} teams configured yet — add one in Settings → Teams.
                    </div>
                  )}

                  <input
                    value={subject}
                    onChange={(e) => setSubject(e.target.value)}
                    style={{ width: "100%", marginBottom: "0.4rem" }}
                  />
                  <textarea
                    value={body}
                    onChange={(e) => setBody(e.target.value)}
                    rows={5}
                    style={{ width: "100%", fontFamily: "inherit", fontSize: "0.82rem" }}
                  />

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
          </td>
        </tr>
      )}
    </>
  );
}

export default function HumanReview({ incidents, routers, teams, onSelectRouter }) {
  const routerById = useMemo(() => Object.fromEntries(routers.map((r) => [r.id, r])), [routers]);

  const needsReview = incidents.filter(
    (i) => i.status === "open" && i.remediation?.action_type === "NOTIFY_ONLY"
  );

  return (
    <div className="page">
      <h2 style={{ fontSize: "1.05rem", margin: "0 0 0.75rem" }}>
        Open incidents needing human review ({needsReview.length})
      </h2>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Updated</th>
              <th>Layer</th>
              <th>Type</th>
              <th>Router</th>
              <th>Interface</th>
              <th>Traps</th>
              <th>Description</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {needsReview.map((incident) => (
              <IncidentReviewRow
                key={incident.id}
                incident={incident}
                router={routerById[incident.router_id]}
                teams={teams}
                onSelectRouter={onSelectRouter}
                symptomaticCount={incidents.filter((i) => i.root_cause_incident_id === incident.id).length}
              />
            ))}
            {needsReview.length === 0 && (
              <tr>
                <td colSpan={8}>Nothing needs human review right now.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
