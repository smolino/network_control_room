import { Fragment, useMemo, useState } from "react";
import { deleteRouter } from "../api.js";
import { useHasRole } from "../auth.js";

const STATUS_ORDER = ["up", "down", "flapping", "unknown"];
const UNASSIGNED = "unassigned";

// A region is one primary router plus the customer routers homed to it,
// named after the primary's city. Customers whose parent no longer exists
// fall into a trailing "Unassigned" group.
function groupByRegion(rows, routerById) {
  const groups = new Map();
  for (const r of rows) {
    const primaryId = r.router_type === "primary" ? r.id : r.parent_router_id;
    const primary = routerById[primaryId];
    const key = primary ? String(primary.id) : UNASSIGNED;
    if (!groups.has(key)) {
      groups.set(key, {
        key,
        label: primary ? primary.city || primary.hostname : "Unassigned",
        country: primary?.country,
        rows: [],
      });
    }
    groups.get(key).rows.push(r);
  }
  for (const g of groups.values()) {
    g.rows.sort(
      (a, b) =>
        (a.router_type === "primary" ? 0 : 1) - (b.router_type === "primary" ? 0 : 1) ||
        a.hostname.localeCompare(b.hostname)
    );
  }
  return [...groups.values()].sort(
    (a, b) => (a.key === UNASSIGNED) - (b.key === UNASSIGNED) || a.label.localeCompare(b.label)
  );
}

export default function RouterList({ routers, selectedRouterId, onSelectRouter, onRouterDeleted }) {
  const [filter, setFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("primary");
  const [deletingId, setDeletingId] = useState(null);
  const canDelete = useHasRole("admin");
  const [collapsed, setCollapsed] = useState(() => new Set());

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

  const regions = useMemo(() => groupByRegion(filtered, routerById), [filtered, routerById]);

  const toggleRegion = (key) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  const allCollapsed = regions.length > 0 && regions.every((g) => collapsed.has(g.key));
  const toggleAll = () => setCollapsed(allCollapsed ? new Set() : new Set(regions.map((g) => g.key)));

  const handleDelete = async (e, r) => {
    e.stopPropagation();
    if (!window.confirm(`Remove ${r.hostname} (${r.mgmt_ip})? Its BGP peerings will be removed too. This can't be undone.`)) {
      return;
    }
    setDeletingId(r.id);
    try {
      await deleteRouter(r.id);
      await onRouterDeleted?.(r.id);
    } catch (err) {
      window.alert(`Failed to remove ${r.hostname}: ${err.message}`);
    } finally {
      setDeletingId(null);
    }
  };

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
        <button type="button" onClick={toggleAll} disabled={regions.length === 0}>
          {allCollapsed ? "Expand all regions" : "Collapse all regions"}
        </button>
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
              <th></th>
            </tr>
          </thead>
          <tbody>
            {regions.map((g) => {
              const isCollapsed = collapsed.has(g.key);
              const counts = STATUS_ORDER.map((status) => [status, g.rows.filter((r) => r.status === status).length]).filter(
                ([, n]) => n > 0
              );
              return (
                <Fragment key={g.key}>
                  <tr className="region-row" onClick={() => toggleRegion(g.key)}>
                    <td colSpan={9}>
                      <span className="region-caret">{isCollapsed ? "▶" : "▼"}</span>
                      <strong>{g.label}</strong>
                      {g.country && <span className="region-meta">{g.country}</span>}
                      <span className="region-meta">
                        {g.rows.length} router{g.rows.length === 1 ? "" : "s"}
                      </span>
                      {counts.map(([status, n]) => (
                        <span key={status} className={`badge ${status}`} style={{ marginLeft: "0.4rem" }}>
                          {n} {status}
                        </span>
                      ))}
                    </td>
                  </tr>
                  {!isCollapsed &&
                    g.rows.map((r) => (
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
                        <td>
                          {canDelete && (
                            <span className="link" onClick={(e) => handleDelete(e, r)}>
                              {deletingId === r.id ? "Removing…" : "Remove"}
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                </Fragment>
              );
            })}
            {filtered.length === 0 && (
              <tr><td colSpan={9}>No routers match this filter.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
