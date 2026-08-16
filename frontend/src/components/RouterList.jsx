import { useMemo, useState } from "react";

export default function RouterList({ routers, selectedRouterId, onSelectRouter }) {
  const [filter, setFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("primary");

  const routerById = useMemo(() => Object.fromEntries(routers.map((r) => [r.id, r])), [routers]);

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return routers.filter((r) => {
      if (typeFilter !== "all" && r.router_type !== typeFilter) return false;
      if (!q) return true;
      return (
        r.hostname.toLowerCase().includes(q) ||
        r.mgmt_ip.includes(q) ||
        (r.city || "").toLowerCase().includes(q) ||
        (r.country || "").toLowerCase().includes(q)
      );
    });
  }, [routers, filter, typeFilter]);

  return (
    <div className="page">
      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.75rem", marginBottom: "1rem" }}>
        <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
          <option value="primary">Primary routers</option>
          <option value="customer">Customer routers</option>
          <option value="all">All ({routers.length})</option>
        </select>
        <input
          placeholder="Filter by hostname, IP, city, country…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          style={{
            flex: 1,
            minWidth: 200,
            maxWidth: 420,
            padding: "0.5rem 0.75rem",
            background: "#161c2c",
            border: "1px solid #262f45",
            borderRadius: 6,
            color: "#e6e9f0",
          }}
        />
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Hostname</th>
              <th>Type</th>
              <th>Mgmt IP</th>
              <th>Model</th>
              <th>Site</th>
              <th>Country</th>
              <th>Status</th>
              <th>Last seen</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((r) => (
              <tr
                key={r.id}
                onClick={() => onSelectRouter(r.id)}
                style={{
                  cursor: "pointer",
                  background: r.id === selectedRouterId ? "#1c2438" : undefined,
                }}
              >
                <td>{r.hostname}</td>
                <td>
                  {r.router_type === "primary"
                    ? "Primary"
                    : `Customer of ${routerById[r.parent_router_id]?.hostname || "?"}`}
                </td>
                <td>{r.mgmt_ip}</td>
                <td>{r.model}</td>
                <td>{r.site_name}</td>
                <td>{r.city}, {r.country}</td>
                <td><span className={`badge ${r.status}`}>{r.status}</span></td>
                <td>{r.last_seen_at ? new Date(r.last_seen_at).toLocaleString() : "never"}</td>
              </tr>
            ))}
            {filtered.length === 0 && (
              <tr><td colSpan={8}>No routers match this filter.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
