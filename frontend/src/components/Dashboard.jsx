import { useEffect, useMemo, useState } from "react";
import { fetchStatsTimeseries } from "../api.js";

// Dashboard = the live map as a backdrop, KPI cards floating over it, and a
// telemetry strip along the bottom. The map itself is passed in as children
// (MapView), so it keeps all its controls.

const WINDOWS = [
  { minutes: 60, label: "1h" },
  { minutes: 360, label: "6h" },
  { minutes: 1440, label: "24h" },
];
const POLL_MS = 30000;

const fmt = (n) => (n ?? 0).toLocaleString();

export default function Dashboard({ stats, children }) {
  const [windowMinutes, setWindowMinutes] = useState(60);
  const [series, setSeries] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      fetchStatsTimeseries(windowMinutes)
        .then((data) => {
          if (cancelled) return;
          setSeries(data);
          setError(null);
        })
        .catch((err) => !cancelled && setError(err.message));
    load();
    const timer = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [windowMinutes]);

  return (
    <div className="dashboard">
      <div className="dashboard-map">{children}</div>

      {stats && (
        <div className="dash-kpis">
          <div className="dash-kpi" title="Telco-owned backbone (primary) routers">
            <div className="value">{fmt(stats.total_routers)}</div>
            <div className="label">Backbone</div>
            <div className="sub">
              <span style={{ color: "#4ade80" }}>{fmt(stats.routers_up)} up</span>
              {" · "}
              <span style={{ color: stats.routers_down ? "#f87171" : undefined }}>{fmt(stats.routers_down)} down</span>
              {" · "}
              <span style={{ color: stats.routers_flapping ? "#facc15" : undefined }}>
                {fmt(stats.routers_flapping)} flapping
              </span>
            </div>
          </div>
          <div className="dash-kpi" title="Last-mile customer CPE routers">
            <div className="value" style={{ color: "#4ade80" }}>{fmt(stats.total_customer_routers)}</div>
            <div className="label">Customers</div>
          </div>
          <div className="dash-kpi" title="Open incidents on backbone routers">
            <div className="value">{fmt(stats.open_incidents)}</div>
            <div className="label">Open incidents</div>
            {stats.incidents_in_triage > 0 && (
              <div className="sub" style={{ color: "#fb923c" }}>{fmt(stats.incidents_in_triage)} in triage</div>
            )}
          </div>
          <div className="dash-kpi" title="Distinct customer routers inside the blast radius of an open incident">
            <div className="value" style={{ color: stats.customers_impacted ? "#facc15" : undefined }}>
              {fmt(stats.customers_impacted)}
            </div>
            <div className="label">Customers impacted</div>
          </div>
          <div className="dash-kpi" title="Impacted customers that have actually lost service">
            <div className="value" style={{ color: stats.customers_down ? "#f87171" : undefined }}>
              {fmt(stats.customers_down)}
            </div>
            <div className="label">Customers down</div>
          </div>
        </div>
      )}

      <div className="dash-bottom">
        <div className="dash-window" role="group" aria-label="Time window">
          {WINDOWS.map((w) => (
            <button
              key={w.minutes}
              className={w.minutes === windowMinutes ? "active" : ""}
              onClick={() => setWindowMinutes(w.minutes)}
            >
              {w.label}
            </button>
          ))}
        </div>
        <div className="dash-charts">
        {error && !series && <div className="dash-panel dash-error">Telemetry unavailable: {error}</div>}
        {series && (
          <>
            <ChartPanel
              title="SNMP traps"
              unit={`/ ${series.bucket_minutes} min`}
              buckets={series.buckets}
              values={series.traps}
              color="#60a5fa"
              showAverage
            />
            <ChartPanel
              title="Incidents opened"
              unit={`/ ${series.bucket_minutes} min`}
              buckets={series.buckets}
              values={series.incidents_opened}
              color="#4ade80"
            />
            <ChartPanel
              title="Open incidents"
              unit="fleet-wide"
              buckets={series.buckets}
              values={series.open_incidents}
              color="#f87171"
              step
              area={false}
            />
            <ChartPanel
              title="Auto-heal actions"
              unit={`/ ${series.bucket_minutes} min`}
              buckets={series.buckets}
              values={series.autoheal_actions}
              secondary={series.autoheal_failed}
              secondaryLabel="failed"
              color="#38bdf8"
            />
            <AlertsPanel rows={series.open_by_type} />
          </>
        )}
        </div>
      </div>
    </div>
  );
}

// --- charts -----------------------------------------------------------------

const W = 240;
const H = 120;
const PAD = { top: 8, right: 6, bottom: 18, left: 28 };
const PLOT_W = W - PAD.left - PAD.right;
const PLOT_H = H - PAD.top - PAD.bottom;

// A round axis max a bit above the data, so ticks land on nice numbers.
function niceMax(v) {
  if (v <= 4) return 4;
  const mag = 10 ** Math.floor(Math.log10(v));
  const step = [1, 2, 2.5, 5, 10].find((s) => s * mag * 4 >= v) * mag;
  return step * 4;
}

const timeLabel = (iso) =>
  new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

function ChartPanel({ title, unit, buckets, values, color, secondary, secondaryLabel, showAverage, step, area = true }) {
  const [hover, setHover] = useState(null);
  const n = values.length;
  const max = niceMax(Math.max(0, ...values));
  const x = (i) => PAD.left + (n <= 1 ? 0 : (i / (n - 1)) * PLOT_W);
  const y = (v) => PAD.top + PLOT_H - (v / max) * PLOT_H;

  const path = useMemo(() => {
    if (n === 0) return "";
    return values
      .map((v, i) => {
        if (i === 0) return `M${x(0)},${y(v)}`;
        return step ? `H${x(i)}V${y(v)}` : `L${x(i)},${y(v)}`;
      })
      .join("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [values, max, step]);

  const secondaryPath = useMemo(() => {
    if (!secondary || !secondary.some(Boolean)) return "";
    return secondary.map((v, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(v)}`).join("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [secondary, max]);

  const avg = n ? values.reduce((a, b) => a + b, 0) / n : 0;
  const latest = n ? values[n - 1] : 0;
  const gradientId = `grad-${title.replace(/\W/g, "")}`;
  const xTicks = n > 1 ? [0, Math.round((n - 1) / 3), Math.round((2 * (n - 1)) / 3), n - 1] : [];

  const onMove = (e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * W;
    const i = Math.round(((px - PAD.left) / PLOT_W) * (n - 1));
    setHover(i >= 0 && i < n ? i : null);
  };

  return (
    <div className="dash-panel">
      <div className="dash-panel-head">
        <span className="title">{title}</span>
        <span className="latest" style={{ color }}>
          {hover != null ? fmt(values[hover]) : fmt(latest)}
        </span>
      </div>
      <div className="dash-panel-sub">
        {hover != null ? (
          <>
            {timeLabel(buckets[hover])}
            {secondary && ` · ${fmt(secondary[hover])} ${secondaryLabel}`}
          </>
        ) : (
          <>
            {unit}
            {showAverage && ` · avg ${avg.toFixed(1)}`}
          </>
        )}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="dash-chart" onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.45" />
            <stop offset="100%" stopColor={color} stopOpacity="0.02" />
          </linearGradient>
        </defs>
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <g key={f}>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(max * f)} y2={y(max * f)} className="grid" />
            <text x={PAD.left - 4} y={y(max * f) + 3} className="tick" textAnchor="end">
              {fmt(max * f)}
            </text>
          </g>
        ))}
        {xTicks.map((i, k) => (
          <text
            key={i}
            x={x(i)}
            y={H - 4}
            className="tick"
            textAnchor={k === 0 ? "start" : k === xTicks.length - 1 ? "end" : "middle"}
          >
            {timeLabel(buckets[i])}
          </text>
        ))}
        {area && path && (
          <path d={`${path}L${x(n - 1)},${y(0)}L${x(0)},${y(0)}Z`} fill={`url(#${gradientId})`} />
        )}
        {path && <path d={path} fill="none" stroke={color} strokeWidth="1.6" strokeLinejoin="round" />}
        {secondaryPath && <path d={secondaryPath} fill="none" stroke="#f87171" strokeWidth="1.2" />}
        {showAverage && avg > 0 && (
          <line x1={PAD.left} x2={W - PAD.right} y1={y(avg)} y2={y(avg)} className="avg-line" />
        )}
        {hover != null && (
          <g>
            <line x1={x(hover)} x2={x(hover)} y1={PAD.top} y2={PAD.top + PLOT_H} className="hover-line" />
            <circle cx={x(hover)} cy={y(values[hover])} r="3" fill={color} />
          </g>
        )}
      </svg>
    </div>
  );
}

const LAYER_COLOR = { L1: "#fb923c", L3: "#f87171" };

function AlertsPanel({ rows }) {
  const top = rows.slice(0, 8);
  const max = Math.max(1, ...top.map((r) => r.count));
  const total = rows.reduce((a, r) => a + r.count, 0);
  return (
    <div className="dash-panel">
      <div className="dash-panel-head">
        <span className="title">Open alerts by type</span>
        <span className="latest" style={{ color: total ? "#f87171" : "#4ade80" }}>{fmt(total)}</span>
      </div>
      <div className="dash-panel-sub">
        <span style={{ color: LAYER_COLOR.L3 }}>■</span> L3 logical{" "}
        <span style={{ color: LAYER_COLOR.L1 }}>■</span> L1 physical
      </div>
      <div className="dash-bars">
        {top.length === 0 && <div className="dash-empty">No open alerts 🎉</div>}
        {top.map((r) => (
          <div className="dash-bar-row" key={`${r.incident_type}-${r.layer}`} title={`${r.incident_type} (${r.layer})`}>
            <span className="name">{r.incident_type.replace(/_/g, " ")}</span>
            <span className="track">
              <span
                className="fill"
                style={{ width: `${(r.count / max) * 100}%`, background: LAYER_COLOR[r.layer] ?? LAYER_COLOR.L3 }}
              />
            </span>
            <span className="count">{fmt(r.count)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
