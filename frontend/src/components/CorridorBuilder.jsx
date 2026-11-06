import { useEffect, useMemo, useState } from "react";
import { fetchCorridorSpec, fetchCorridors, geocodeCity, importCorridor, validateCorridor } from "../api.js";
import { CITY_BY_NAME } from "../cities.js";
import {
  DEFAULT_WAVELENGTHS,
  clientPorts,
  emptyForm,
  formToSpec,
  newCircuit,
  newRouter,
  newSite,
  newSpan,
  newWavelength,
  siteCode,
  specToForm,
  suggestedHostname,
  syncSpans,
  trunkPort,
} from "../corridorForm.js";

// Add Fleet > Optical corridor: build (or edit) a DWDM corridor - its sites
// in order, the elements, the rented spans, transponders, and which routers
// each wavelength connects - and import it (POST /api/corridors/import).
// The form model and the line-card preset live in corridorForm.js.

const small = { fontSize: "0.75rem", color: "#9aa4bf" };
const row = { display: "grid", gap: "0.5rem", alignItems: "end" };
const input = { width: "100%", minWidth: 0 };

function Field({ label, children, style }) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: "0.2rem", fontSize: "0.75rem", color: "#9aa4bf", ...style }}>
      {label}
      {children}
    </label>
  );
}

function errorLines(err) {
  const d = err?.detail;
  if (Array.isArray(d)) return d.map((x) => (typeof x === "string" ? x : `${(x.loc || []).slice(1).join(".")}: ${x.msg}`));
  return [err?.message || String(err)];
}

function ipToInt(ip) {
  const p = ip.split(".").map(Number);
  return p.length === 4 && p.every((n) => n >= 0 && n <= 255) ? ((p[0] << 24) >>> 0) + (p[1] << 16) + (p[2] << 8) + p[3] : null;
}
const intToIp = (n) => [n >>> 24, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join(".");

export default function CorridorBuilder({ routers, onFleetChanged }) {
  const [form, setForm] = useState(emptyForm);
  const [corridors, setCorridors] = useState([]);
  const [loadId, setLoadId] = useState("");
  const [baseIp, setBaseIp] = useState("");
  const [check, setCheck] = useState(null);
  const [errors, setErrors] = useState([]);
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(false);
  const [prune, setPrune] = useState(false);
  const [lookup, setLookup] = useState({});

  useEffect(() => {
    fetchCorridors().then(setCorridors).catch(() => setCorridors([]));
  }, []);

  const fleetRouters = useMemo(
    () => routers.filter((r) => r.router_type === "primary" && (!r.element_role || r.element_role === "router")),
    [routers]
  );
  const editing = corridors.find((c) => c.name === form.name.trim());
  // Sites other corridors already use - a POP shared by two corridors must
  // be the same site (its routers can only be in one place).
  const knownSites = useMemo(() => {
    const out = new Map();
    for (const c of corridors) for (const pt of c.route) if (!out.has(pt.site_code)) out.set(pt.site_code, pt);
    return [...out.values()].sort((a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === "pop" ? -1 : 1));
  }, [corridors]);
  const useKnownSite = (site, code) => {
    const pt = knownSites.find((k) => k.site_code === code);
    if (!pt) return;
    setSite(site.id, { code: pt.site_code, name: pt.name, latitude: String(pt.latitude), longitude: String(pt.longitude) });
  };

  // Any edit invalidates the last check.
  const update = (fn) => {
    setForm((f) => syncSpans(fn(f)));
    setCheck(null);
    setErrors([]);
    setStatus(null);
  };
  const setTop = (key) => (e) => update((f) => ({ ...f, [key]: e.target.value }));
  const setSite = (sid, patch) => update((f) => ({ ...f, sites: f.sites.map((s) => (s.id === sid ? { ...s, ...patch } : s)) }));
  const setSpan = (i, key) => (e) => update((f) => ({ ...f, spans: f.spans.map((s, j) => (j === i ? { ...s, [key]: e.target.value } : s)) }));
  const setTxp = (end, key) => (e) => update((f) => ({ ...f, [end]: { ...f[end], [key]: e.target.value } }));
  const setRouter = (rid, patch) => update((f) => ({ ...f, routers: f.routers.map((r) => (r.id === rid ? { ...r, ...patch } : r)) }));
  const setWl = (wid, patch) => update((f) => ({ ...f, wavelengths: f.wavelengths.map((w) => (w.id === wid ? { ...w, ...patch } : w)) }));
  const setCircuit = (wid, cid, patch) =>
    update((f) => ({
      ...f,
      wavelengths: f.wavelengths.map((w) =>
        w.id === wid ? { ...w, circuits: w.circuits.map((c) => (c.id === cid ? { ...c, ...patch } : c)) } : w
      ),
    }));

  const changeCity = (site, value) => {
    const match = CITY_BY_NAME[value.trim().toLowerCase()];
    setSite(site.id, match
      ? { city: value, latitude: String(match.latitude), longitude: String(match.longitude), country: site.country || match.country }
      : { city: value });
  };

  const findCity = async (site) => {
    if (!site.city.trim()) return;
    setLookup((l) => ({ ...l, [site.id]: "Looking up…" }));
    try {
      const m = await geocodeCity(site.city.trim(), site.country.trim());
      setSite(site.id, { latitude: String(m.latitude), longitude: String(m.longitude), country: site.country || m.country || "" });
      setLookup((l) => ({ ...l, [site.id]: m.display_name }));
    } catch {
      setLookup((l) => ({ ...l, [site.id]: "Not found - enter latitude/longitude" }));
    }
  };

  // Inserting a site at `index` splits the span that crossed that point:
  // the original keeps its details up to the new site, a new span (same
  // provider and ends, no length) continues from it.
  const addIla = (index) =>
    update((f) => {
      const sites = [...f.sites];
      sites.splice(index, 0, newSite("ila"));
      const spans = [...f.spans];
      const crossed = spans[index - 1] || newSpan();
      spans.splice(index, 0, { ...newSpan(), provider: crossed.provider, a_end: crossed.a_end, b_end: crossed.b_end });
      return { ...f, sites, spans };
    });
  // Removing site `index` merges its two spans into the one before it.
  const removeSite = (index) =>
    update((f) => ({ ...f, sites: f.sites.filter((_, i) => i !== index), spans: f.spans.filter((_, i) => i !== index) }));
  const moveSite = (index, delta) =>
    update((f) => {
      const sites = [...f.sites];
      const j = index + delta;
      if (j <= 0 || j >= sites.length - 1) return f;
      [sites[index], sites[j]] = [sites[j], sites[index]];
      return { ...f, sites };
    });

  const autoName = () =>
    update((f) => ({
      ...f,
      sites: f.sites.map((s, i) => (s.hostname.trim() ? s : { ...s, hostname: suggestedHostname(f, i) })),
      txpA: f.txpA.hostname.trim() ? f.txpA : { ...f.txpA, hostname: `txp1.${(f.sites[0].city || "a").toLowerCase().replace(/[^a-z0-9]+/g, "").slice(0, 5)}1` },
      txpZ: f.txpZ.hostname.trim()
        ? f.txpZ
        : { ...f.txpZ, hostname: `txp1.${(f.sites[f.sites.length - 1].city || "z").toLowerCase().replace(/[^a-z0-9]+/g, "").slice(0, 5)}1` },
    }));

  const fillIps = () => {
    const start = ipToInt(baseIp.trim());
    if (start === null) return setErrors(["Base IP must look like 10.10.2.1"]);
    const used = new Set(routers.map((r) => r.mgmt_ip));
    let n = start;
    const next = () => {
      while (used.has(intToIp(n))) n++;
      const ip = intToIp(n);
      used.add(ip);
      n++;
      return ip;
    };
    update((f) => ({
      ...f,
      sites: f.sites.map((s) => (s.mgmt_ip.trim() ? s : { ...s, mgmt_ip: next() })),
      txpA: f.txpA.mgmt_ip.trim() ? f.txpA : { ...f.txpA, mgmt_ip: next() },
      txpZ: f.txpZ.mgmt_ip.trim() ? f.txpZ : { ...f.txpZ, mgmt_ip: next() },
      routers: f.routers.map((r) => (r.mgmt_ip.trim() || !r.hostname.trim() ? r : { ...r, mgmt_ip: next() })),
    }));
  };

  const addFleetRouter = (end, id) => {
    const r = fleetRouters.find((x) => String(x.id) === id);
    if (!r) return;
    // Fills this POP's first empty row, or adds one - other rows untouched.
    update((f) => {
      const picked = { hostname: r.hostname, mgmt_ip: r.mgmt_ip, model: r.model || "" };
      const empty = f.routers.find((x) => x.end === end && !x.hostname.trim());
      return {
        ...f,
        routers: empty
          ? f.routers.map((x) => (x.id === empty.id ? { ...x, ...picked } : x))
          : [...f.routers, { ...newRouter(end), ...picked }],
      };
    });
  };

  const load = async () => {
    if (!loadId) return;
    setBusy(true);
    try {
      setForm(specToForm(await fetchCorridorSpec(loadId)));
      setCheck(null);
      setErrors([]);
      setStatus({ type: "success", text: "Loaded - edit and re-import; elements are matched on management IP." });
    } catch (err) {
      setErrors(errorLines(err));
    } finally {
      setBusy(false);
    }
  };

  const runCheck = async () => {
    setBusy(true);
    setErrors([]);
    setStatus(null);
    try {
      setCheck(await validateCorridor(formToSpec(form)));
    } catch (err) {
      setCheck(null);
      setErrors(errorLines(err));
    } finally {
      setBusy(false);
    }
  };

  const submit = async () => {
    setBusy(true);
    setErrors([]);
    try {
      const r = await importCorridor(formToSpec(form), { pruneL1Peerings: prune });
      setStatus({
        type: "success",
        text:
          `${editing ? "Updated" : "Created"} ${form.name}: ${r.devices_created.length} device(s) created, ` +
          `${r.devices_updated.length} updated, ${r.spans} span(s), ${r.optical_paths} optical path(s), ` +
          `${r.client_circuits} circuit(s), ${r.l3_peering_ids.length} L3 link(s).` +
          (r.warnings.length ? ` ${r.warnings.length} note(s): ${r.warnings.slice(0, 3).join("; ")}${r.warnings.length > 3 ? "…" : ""}` : ""),
      });
      setCheck(null);
      setCorridors(await fetchCorridors());
      await onFleetChanged();
    } catch (err) {
      setErrors(errorLines(err));
    } finally {
      setBusy(false);
    }
  };

  const clientsA = clientPorts(form, "A");
  const clientsZ = clientPorts(form, "Z");
  const routersAt = (end) => form.routers.filter((r) => r.end === end && r.hostname.trim());
  const last = form.sites.length - 1;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <div className="card">
        <h3>Optical corridor</h3>
        <p className="settings-hint">
          A DWDM line between two POPs: the sites in order from A to Z, the dark-fiber spans between them, a
          transponder at each POP, and the routers each wavelength connects. Line cards are filled in from the
          Cisco ONS 15454 preset (booster/preamp, 40-channel mux/demux, OSC, one amplifier per direction at each
          amplifier site). The root-cause engine locates faults on it from the elements' alarms.
        </p>
        <div style={{ ...row, gridTemplateColumns: "2fr 3fr 2fr" }}>
          <Field label="Corridor name *">
            <input style={input} value={form.name} onChange={setTop("name")} placeholder="BIO1-MAD1" />
          </Field>
          <Field label="Description">
            <input style={input} value={form.description} onChange={setTop("description")} />
          </Field>
          <Field label="Fiber provider (default for every span)">
            <input style={input} value={form.provider} onChange={setTop("provider")} placeholder="Fiberprov" />
          </Field>
        </div>
        <div style={{ ...row, gridTemplateColumns: "auto 1fr auto auto", marginTop: "0.75rem" }}>
          <span style={small}>Edit an existing corridor:</span>
          <select value={loadId} onChange={(e) => setLoadId(e.target.value)}>
            <option value="">Choose…</option>
            {corridors.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} ({c.site_a} – {c.site_z})
              </option>
            ))}
          </select>
          <button type="button" onClick={load} disabled={!loadId || busy}>
            Load
          </button>
          <button
            type="button"
            onClick={() => {
              setForm(emptyForm());
              setLoadId("");
              setCheck(null);
              setErrors([]);
              setStatus(null);
            }}
          >
            New corridor
          </button>
        </div>
        {editing && (
          <p className="settings-hint" style={{ marginTop: "0.5rem" }}>
            Importing will update <strong>{editing.name}</strong> in place (same name). Rename it to create a new
            corridor instead.
          </p>
        )}
      </div>

      <div className="card">
        <h3>Sites, A → Z</h3>
        <div style={{ ...row, gridTemplateColumns: "auto auto 1fr auto auto", marginBottom: "0.75rem" }}>
          <button type="button" onClick={autoName}>
            Auto-name elements
          </button>
          <span style={small}>Fill empty IPs from</span>
          <input style={input} value={baseIp} onChange={(e) => setBaseIp(e.target.value)} placeholder="10.10.2.1" />
          <button type="button" onClick={fillIps}>
            Fill IPs
          </button>
          <span style={small}>(skips IPs already in the fleet)</span>
        </div>
        {form.sites.map((s, i) => (
          <div key={s.id}>
            <div
              style={{
                ...row,
                gridTemplateColumns: "6.5rem 1.4fr 1.4fr 0.8fr 0.8fr 1.6fr 1fr auto",
                padding: "0.5rem",
                border: "1px solid #262f45",
                borderRadius: 8,
                background: s.kind === "pop" ? "#1b2236" : "transparent",
              }}
            >
              <div style={{ fontSize: "0.8rem", fontWeight: 600 }}>
                {i === 0 ? "POP A" : i === last ? "POP Z" : `Amp site ${i}`}
                <div style={small}>{s.kind === "pop" ? "terminal" : "in-line amp"}</div>
              </div>
              <Field label="Site name">
                <input style={input} value={s.name} onChange={(e) => setSite(s.id, { name: e.target.value })} placeholder={s.kind === "pop" ? "Bilbao POP" : "Herena hut"} />
              </Field>
              <Field label={lookup[s.id] ? `City · ${lookup[s.id]}` : "City"}>
                <div style={{ display: "flex", gap: "0.25rem" }}>
                  <input style={input} list="city-options" value={s.city} onChange={(e) => changeCity(s, e.target.value)} />
                  <button type="button" title="Look up the coordinates online" onClick={() => findCity(s)}>
                    📍
                  </button>
                </div>
              </Field>
              <Field label="Latitude *">
                <input style={input} value={s.latitude} onChange={(e) => setSite(s.id, { latitude: e.target.value })} />
              </Field>
              <Field label="Longitude *">
                <input style={input} value={s.longitude} onChange={(e) => setSite(s.id, { longitude: e.target.value })} />
              </Field>
              <Field label={s.kind === "pop" ? "Terminal hostname" : "Amplifier hostname"}>
                <input style={input} value={s.hostname} onChange={(e) => setSite(s.id, { hostname: e.target.value })} placeholder={suggestedHostname(form, i)} />
              </Field>
              <Field label="Management IP *">
                <input style={input} value={s.mgmt_ip} onChange={(e) => setSite(s.id, { mgmt_ip: e.target.value })} />
              </Field>
              <div style={{ display: "flex", gap: "0.2rem" }}>
                {s.kind === "ila" && (
                  <>
                    <button type="button" title="Move towards A" onClick={() => moveSite(i, -1)} disabled={i <= 1}>
                      ↑
                    </button>
                    <button type="button" title="Move towards Z" onClick={() => moveSite(i, 1)} disabled={i >= last - 1}>
                      ↓
                    </button>
                    <button type="button" title="Remove this amplifier site" onClick={() => removeSite(i)}>
                      ✕
                    </button>
                  </>
                )}
              </div>
            </div>
            <div style={{ ...small, margin: "0.25rem 0 0 7rem", display: "flex", gap: "0.75rem", alignItems: "center", flexWrap: "wrap" }}>
              {knownSites.length > 0 && (
                <select value={knownSites.some((k) => k.site_code === s.code) ? s.code : ""} onChange={(e) => useKnownSite(s, e.target.value)}>
                  <option value="">Use an existing site…</option>
                  {knownSites.map((k) => (
                    <option key={k.site_code} value={k.site_code}>
                      {k.name} ({k.kind === "pop" ? "POP" : "amp site"}, {k.site_code})
                    </option>
                  ))}
                </select>
              )}
              <span>site code {siteCode(form, i)}</span>
            </div>
            {s.kind === "ila" && (
              <div style={{ ...small, margin: "0.25rem 0 0 7rem" }}>
                Amplifier card{" "}
                <select value={s.ampType} onChange={(e) => setSite(s.id, { ampType: e.target.value, cards: s.cards && s.cards.map((c) => (c.function === "line_amp" ? { ...c, type: e.target.value } : c)) })}>
                  {["OPT-AMP-C", "OPT-EDFA-17", "OPT-EDFA-24", "OPT-AMP-17-C"].map((t) => (
                    <option key={t}>{t}</option>
                  ))}
                </select>
              </div>
            )}
            {i < last && (
              <div style={{ ...row, gridTemplateColumns: "6.5rem 1.4fr 0.6fr 0.6fr 0.8fr 1.4fr auto", margin: "0.4rem 0 0.4rem 1.5rem", paddingLeft: "0.75rem", borderLeft: "3px solid #a855f7" }}>
                <div style={{ ...small, color: "#c084fc" }}>Span {i + 1} (dark fiber)</div>
                <Field label="Provider">
                  <input style={input} value={form.spans[i]?.provider ?? ""} onChange={setSpan(i, "provider")} placeholder={form.provider || "provider"} />
                </Field>
                <Field label="A end">
                  <input style={input} value={form.spans[i]?.a_end ?? ""} onChange={setSpan(i, "a_end")} />
                </Field>
                <Field label="B end">
                  <input style={input} value={form.spans[i]?.b_end ?? ""} onChange={setSpan(i, "b_end")} />
                </Field>
                <Field label="Length km">
                  <input style={input} value={form.spans[i]?.length_km ?? ""} onChange={setSpan(i, "length_km")} placeholder="auto" />
                </Field>
                <Field label="Provider circuit ID">
                  <input style={input} value={form.spans[i]?.provider_circuit_id ?? ""} onChange={setSpan(i, "provider_circuit_id")} />
                </Field>
                <button type="button" title="Insert an amplifier site here" onClick={() => addIla(i + 1)}>
                  + amp site
                </button>
              </div>
            )}
          </div>
        ))}
        <p className="settings-hint">Leave a span's length empty to use the straight-line distance between its sites.</p>
      </div>

      <div className="card">
        <h3>Transponders and routers</h3>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem" }}>
          {[
            ["A", "txpA", form.sites[0]],
            ["Z", "txpZ", form.sites[last]],
          ].map(([end, key, pop]) => (
            <div key={end}>
              <div style={{ fontWeight: 600, marginBottom: "0.4rem" }}>
                POP {end}
                {pop.name || pop.city ? ` · ${pop.name || pop.city}` : ""}
              </div>
              <div style={{ ...row, gridTemplateColumns: "1.4fr 1fr 0.8fr" }}>
                <Field label="Transponder hostname *">
                  <input style={input} value={form[key].hostname} onChange={setTxp(key, "hostname")} placeholder={`txp1.${end.toLowerCase()}`} />
                </Field>
                <Field label="Management IP *">
                  <input style={input} value={form[key].mgmt_ip} onChange={setTxp(key, "mgmt_ip")} />
                </Field>
                <Field label="Model">
                  <input style={input} value={form[key].model} onChange={setTxp(key, "model")} />
                </Field>
              </div>
              <div style={{ ...small, margin: "0.75rem 0 0.3rem" }}>Routers at POP {end}</div>
              {form.routers
                .filter((r) => r.end === end)
                .map((r) => (
                  <div key={r.id} style={{ ...row, gridTemplateColumns: "1.4fr 1fr 0.8fr auto", marginBottom: "0.4rem" }}>
                    <input style={input} value={r.hostname} onChange={(e) => setRouter(r.id, { hostname: e.target.value })} placeholder="cor1.pop" />
                    <input style={input} value={r.mgmt_ip} onChange={(e) => setRouter(r.id, { mgmt_ip: e.target.value })} placeholder="management IP" />
                    <input style={input} value={r.model} onChange={(e) => setRouter(r.id, { model: e.target.value })} placeholder="model" />
                    <button type="button" onClick={() => update((f) => ({ ...f, routers: f.routers.filter((x) => x.id !== r.id) }))}>
                      ✕
                    </button>
                  </div>
                ))}
              <div style={{ display: "flex", gap: "0.5rem" }}>
                <button type="button" onClick={() => update((f) => ({ ...f, routers: [...f.routers, newRouter(end)] }))}>
                  + router
                </button>
                <select value="" onChange={(e) => addFleetRouter(end, e.target.value)}>
                  <option value="">+ from the fleet…</option>
                  {fleetRouters.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.hostname} ({r.mgmt_ip}{r.city ? `, ${r.city}` : ""})
                    </option>
                  ))}
                </select>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="card">
        <h3>Wavelengths and circuits</h3>
        <p className="settings-hint">
          Each wavelength runs transponder trunk to transponder trunk and carries client circuits between one router
          pair; the circuits' router ports become that pair's bundle members (IS-IS/BGP). Transponder ports are
          numbered automatically unless you type them.
        </p>
        {form.wavelengths.map((w) => (
          <div key={w.id} style={{ border: "1px solid #262f45", borderRadius: 8, padding: "0.6rem", marginBottom: "0.75rem" }}>
            <div style={{ ...row, gridTemplateColumns: "0.8fr 0.8fr 0.8fr 1.2fr 1fr 1.2fr 1fr auto" }}>
              <Field label="Wavelength nm *">
                <input style={input} list="wavelength-options" value={w.nm} onChange={(e) => setWl(w.id, { nm: e.target.value })} />
              </Field>
              <Field label="Trunk port A">
                <input style={input} value={w.trunkA} onChange={(e) => setWl(w.id, { trunkA: e.target.value })} placeholder={trunkPort(form, w, "A")} />
              </Field>
              <Field label="Trunk port Z">
                <input style={input} value={w.trunkZ} onChange={(e) => setWl(w.id, { trunkZ: e.target.value })} placeholder={trunkPort(form, w, "Z")} />
              </Field>
              <Field label="Router at A *">
                <select value={w.routerA} onChange={(e) => setWl(w.id, { routerA: e.target.value })}>
                  <option value="">Choose…</option>
                  {routersAt("A").map((r) => (
                    <option key={r.id}>{r.hostname}</option>
                  ))}
                </select>
              </Field>
              <Field label="Bundle at A">
                <input style={input} value={w.bundleA} onChange={(e) => setWl(w.id, { bundleA: e.target.value })} />
              </Field>
              <Field label="Router at Z *">
                <select value={w.routerZ} onChange={(e) => setWl(w.id, { routerZ: e.target.value })}>
                  <option value="">Choose…</option>
                  {routersAt("Z").map((r) => (
                    <option key={r.id}>{r.hostname}</option>
                  ))}
                </select>
              </Field>
              <Field label="Bundle at Z">
                <input style={input} value={w.bundleZ} onChange={(e) => setWl(w.id, { bundleZ: e.target.value })} />
              </Field>
              <button type="button" title="Remove this wavelength" onClick={() => update((f) => ({ ...f, wavelengths: f.wavelengths.filter((x) => x.id !== w.id) }))} disabled={form.wavelengths.length === 1}>
                ✕
              </button>
            </div>
            {w.circuits.map((c, ci) => (
              <div key={c.id} style={{ ...row, gridTemplateColumns: "6rem 1.3fr 1fr 1fr 1.3fr auto", marginTop: "0.4rem" }}>
                <span style={small}>Circuit {ci + 1}</span>
                <input style={input} value={c.aPort} onChange={(e) => setCircuit(w.id, c.id, { aPort: e.target.value })} placeholder={`router A port, e.g. HundredGigE0/0/0/${ci}`} />
                <input style={input} value={c.aClient} onChange={(e) => setCircuit(w.id, c.id, { aClient: e.target.value })} placeholder={`txp A ${clientsA.get(c.id)}`} />
                <input style={input} value={c.zClient} onChange={(e) => setCircuit(w.id, c.id, { zClient: e.target.value })} placeholder={`txp Z ${clientsZ.get(c.id)}`} />
                <input style={input} value={c.zPort} onChange={(e) => setCircuit(w.id, c.id, { zPort: e.target.value })} placeholder="router Z port" />
                <button type="button" onClick={() => setWl(w.id, { circuits: w.circuits.filter((x) => x.id !== c.id) })} disabled={w.circuits.length === 1}>
                  ✕
                </button>
              </div>
            ))}
            <button type="button" style={{ marginTop: "0.4rem" }} onClick={() => setWl(w.id, { circuits: [...w.circuits, newCircuit()] })}>
              + circuit
            </button>
          </div>
        ))}
        <datalist id="wavelength-options">
          {DEFAULT_WAVELENGTHS.map((nm) => (
            <option key={nm} value={nm} />
          ))}
        </datalist>
        <button
          type="button"
          onClick={() =>
            update((f) => ({
              ...f,
              wavelengths: [...f.wavelengths, newWavelength(DEFAULT_WAVELENGTHS.find((nm) => !f.wavelengths.some((w) => w.nm === nm)) || "")],
            }))
          }
        >
          + wavelength
        </button>
      </div>

      <div className="card">
        <h3>Check and {editing ? "update" : "create"}</h3>
        <div style={{ display: "flex", gap: "0.75rem", alignItems: "center", flexWrap: "wrap" }}>
          <button type="button" onClick={runCheck} disabled={busy || !form.name.trim()}>
            Check
          </button>
          <label style={{ display: "flex", alignItems: "center", gap: "0.4rem", fontSize: "0.85rem" }}>
            <input type="checkbox" checked={prune} onChange={(e) => setPrune(e.target.checked)} />
            Also remove BGP links that have an optical element on one side
          </label>
          <button className="login-submit" type="button" onClick={submit} disabled={busy || !check}>
            {busy ? "Working…" : editing ? `Update ${form.name}` : `Create ${form.name || "corridor"}`}
          </button>
        </div>
        {!check && !errors.length && <p className="settings-hint">Check first - nothing is written until you create it.</p>}
        {check && (
          <div className="form-success" style={{ marginTop: "0.75rem" }}>
            Ready: {check.chain.join(" → ")} · {check.wavelengths_nm.length} wavelength(s) · {check.optical_paths.length} optical
            paths (each wavelength and every span's OSC, both directions).
            {check.warnings.length > 0 && <div>Notes: {check.warnings.join("; ")}</div>}
          </div>
        )}
        {errors.length > 0 && (
          <div className="login-error" style={{ marginTop: "0.75rem" }}>
            {errors.map((e) => (
              <div key={e}>{e}</div>
            ))}
          </div>
        )}
        {status && <div className={status.type === "error" ? "login-error" : "form-success"} style={{ marginTop: "0.75rem" }}>{status.text}</div>}
      </div>
    </div>
  );
}
