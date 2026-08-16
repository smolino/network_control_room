export default function Dashboard({ stats, incidents }) {
  if (!stats) return <div className="page">Loading…</div>;

  const typeEntries = Object.entries(stats.incidents_by_type).sort((a, b) => b[1] - a[1]);
  const maxCount = Math.max(1, ...typeEntries.map(([, c]) => c));
  const openIncidents = incidents.filter((i) => i.status === "open").slice(0, 15);

  return (
    <div className="page">
      <div className="card-grid">
        <div className="card">
          <div className="value">{stats.total_routers}</div>
          <div className="label">Total routers</div>
        </div>
        <div className="card">
          <div className="value" style={{ color: "#4ade80" }}>{stats.routers_up}</div>
          <div className="label">Up</div>
        </div>
        <div className="card">
          <div className="value" style={{ color: "#f87171" }}>{stats.routers_down}</div>
          <div className="label">Down</div>
        </div>
        <div className="card">
          <div className="value" style={{ color: "#facc15" }}>{stats.routers_flapping}</div>
          <div className="label">Flapping</div>
        </div>
        <div className="card">
          <div className="value">{stats.open_incidents}</div>
          <div className="label">Open incidents</div>
        </div>
      </div>

      <h3>Incidents by type</h3>
      <div style={{ marginBottom: "2rem" }}>
        {typeEntries.map(([type, count]) => (
          <div className="type-bar-row" key={type}>
            <div className="label">{type}</div>
            <div className="type-bar-track">
              <div className="type-bar-fill" style={{ width: `${(count / maxCount) * 100}%` }} />
            </div>
            <div className="count">{count}</div>
          </div>
        ))}
      </div>

      <h3>Currently open incidents</h3>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Type</th>
              <th>Router</th>
              <th>Interface</th>
              <th>Traps</th>
              <th>Description</th>
            </tr>
          </thead>
          <tbody>
            {openIncidents.map((i) => (
              <tr key={i.id}>
                <td>{i.incident_type}</td>
                <td>#{i.router_id}</td>
                <td>{i.interface_name || "—"}</td>
                <td>{i.trap_count}</td>
                <td>{i.description}</td>
              </tr>
            ))}
            {openIncidents.length === 0 && (
              <tr>
                <td colSpan={5}>No open incidents right now.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
