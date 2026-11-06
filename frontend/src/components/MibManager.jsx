import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  createTrapRule,
  deleteMibModule,
  deleteTrapRule,
  fetchMibModules,
  fetchMibOptions,
  fetchTrapRules,
  importMibs,
  searchTraps,
  updateTrapRule,
} from "../api.js";
import { useHasRole } from "../auth.js";

// "MIBs & Traps" tab - see backend/app/api/mibs.py and
// backend/app/snmp/mib_registry.py. Everyone can browse; only admins can
// import MIBs or change custom traps (the backend enforces it too).

const ACTION_LABELS = {
  raise: "Raise incident",
  clear: "Clear incident",
  alarm: "Alarm only",
};

const EMPTY_RULE = {
  id: null,
  oid: "",
  name: "",
  description: "",
  appliance: "",
  action: "alarm",
  incident_type: "",
  severity: "warning",
  interface_varbind: "",
  enabled: true,
};

const formatBytes = (n) => (n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.ceil(n / 1024)} KB`);
const formatDate = (v) => (v ? new Date(v).toLocaleString() : "");

function HandlingBadge({ trap }) {
  if (trap.builtin) {
    return <span className="badge mib-badge builtin">Built-in · {trap.builtin.incident_type}</span>;
  }
  if (trap.rule) {
    const r = trap.rule;
    const label = r.action === "alarm" ? "Custom · alarm" : `Custom · ${r.action} ${r.incident_type}`;
    return <span className={`badge mib-badge ${r.enabled ? "custom" : "disabled"}`}>{label}{r.enabled ? "" : " (off)"}</span>;
  }
  return <span className="badge mib-badge named">Named only</span>;
}

// ---------------------------------------------------------------------------

function ImportPanel({ options, modules, onChanged, isAdmin }) {
  const [appliance, setAppliance] = useState("");
  const [files, setFiles] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);
  const [expanded, setExpanded] = useState(null);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef(null);

  const appliances = useMemo(
    () => [...new Set(modules.map((m) => m.appliance).filter(Boolean))].sort(),
    [modules]
  );
  const totalBytes = files.reduce((sum, f) => sum + f.size, 0);

  const addFiles = (list) => {
    setResult(null);
    setError("");
    setFiles((prev) => {
      const byName = new Map(prev.map((f) => [f.name, f]));
      for (const f of list) byName.set(f.name, f);
      return [...byName.values()];
    });
  };

  const submit = async (e) => {
    e.preventDefault();
    if (!files.length) return;
    if (options && files.length > options.max_files) {
      setError(`Import at most ${options.max_files} files at a time.`);
      return;
    }
    if (options && totalBytes > options.max_total_bytes) {
      setError(`That's ${formatBytes(totalBytes)} - import at most ${formatBytes(options.max_total_bytes)} at a time.`);
      return;
    }
    setBusy(true);
    setError("");
    try {
      const payload = await Promise.all(files.map(async (f) => ({ filename: f.name, content: await f.text() })));
      const res = await importMibs(appliance.trim(), payload);
      setResult(res);
      setFiles([]);
      if (inputRef.current) inputRef.current.value = "";
      onChanged();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (m) => {
    if (!window.confirm(`Remove ${m.name}? Its ${m.trap_count} traps will no longer be recognised. Custom traps are kept.`)) return;
    try {
      await deleteMibModule(m.id);
      onChanged();
    } catch (err) {
      window.alert(err.message);
    }
  };

  return (
    <>
      {isAdmin && (
        <form className="mib-card" onSubmit={submit}>
          <h3>Import MIBs</h3>
          <p className="settings-hint">
            Upload the MIB files of any appliance (Juniper, Nokia, Huawei, Ciena, an optical shelf, ...). Every
            NOTIFICATION-TYPE and TRAP-TYPE they define is recognised by name from then on. Include the vendor&apos;s
            root module too (for example <code>JUNIPER-SMI</code>), or upload it later; traps that hang off a missing
            module are listed as unresolved until it arrives.
          </p>
          <div className="field-grid">
            <label>
              Appliance / vendor
              <input
                list="mib-appliances"
                value={appliance}
                placeholder="e.g. Juniper MX"
                maxLength={128}
                onChange={(e) => setAppliance(e.target.value)}
              />
              <datalist id="mib-appliances">
                {appliances.map((a) => (
                  <option key={a} value={a} />
                ))}
              </datalist>
            </label>
          </div>
          <div
            className={`mib-drop${dragging ? " dragging" : ""}`}
            onClick={() => inputRef.current?.click()}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              addFiles([...e.dataTransfer.files]);
            }}
          >
            <input
              ref={inputRef}
              type="file"
              multiple
              accept=".mib,.my,.txt,.smi,.asn,.asn1,text/plain"
              hidden
              onChange={(e) => addFiles([...e.target.files])}
            />
            <strong>Drop MIB files here</strong> or click to choose (.mib, .my, .txt)
          </div>
          {files.length > 0 && (
            <ul className="mib-file-list">
              {files.map((f) => (
                <li key={f.name}>
                  <span>{f.name}</span>
                  <span className="settings-hint">{formatBytes(f.size)}</span>
                  <button type="button" onClick={() => setFiles(files.filter((x) => x !== f))}>
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}
          {error && <div className="login-error">{error}</div>}
          <div className="mib-actions">
            <button type="submit" className="login-submit" disabled={busy || !files.length}>
              {busy ? "Importing…" : `Import ${files.length || ""} file${files.length === 1 ? "" : "s"}`}
            </button>
            {files.length > 0 && <span className="settings-hint">{formatBytes(totalBytes)} total</span>}
          </div>

          {result && (
            <div className="mib-result">
              {result.modules.map((m) => (
                <div key={m.name} className={m.unresolved_count ? "mib-result-warn" : "form-success"}>
                  <strong>{m.name}</strong>: {m.trap_count} trap{m.trap_count === 1 ? "" : "s"} imported
                  {m.unresolved_count > 0 && (
                    <>
                      , {m.unresolved_count} unresolved - missing{" "}
                      {[...new Set(m.unresolved.map((u) => u.missing))].map((s) => <code key={s}>{s}</code>)}. Upload
                      the module that defines it.
                    </>
                  )}
                </div>
              ))}
              {result.errors.map((e) => (
                <div key={e.filename} className="login-error">
                  <strong>{e.filename}</strong>: {e.error}
                </div>
              ))}
            </div>
          )}
        </form>
      )}

      <div className="mib-card">
        <h3>Imported modules</h3>
        {modules.length === 0 ? (
          <p className="settings-hint">
            No MIBs imported yet. The {options?.cisco_catalog_size?.toLocaleString() ?? ""} traps from Cisco&apos;s public
            MIBs are always available in the Trap catalogue.
          </p>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Module</th>
                  <th>Appliance</th>
                  <th>File</th>
                  <th>Traps</th>
                  <th>Unresolved</th>
                  <th>Uploaded</th>
                  {isAdmin && <th />}
                </tr>
              </thead>
              <tbody>
                {modules.map((m) => (
                  <Fragment key={m.id}>
                    <tr>
                      <td>
                        <strong>{m.name}</strong>
                      </td>
                      <td>{m.appliance || "—"}</td>
                      <td className="settings-hint">{m.filename}</td>
                      <td>{m.trap_count}</td>
                      <td>
                        {m.unresolved_count > 0 ? (
                          <span className="link" onClick={() => setExpanded(expanded === m.id ? null : m.id)}>
                            {m.unresolved_count} ▾
                          </span>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td className="settings-hint">
                        {formatDate(m.updated_at)}
                        {m.uploaded_by ? ` · ${m.uploaded_by}` : ""}
                      </td>
                      {isAdmin && (
                        <td>
                          <button onClick={() => remove(m)}>Remove</button>
                        </td>
                      )}
                    </tr>
                    {expanded === m.id && (
                      <tr>
                        <td colSpan={isAdmin ? 7 : 6} className="wrap-block settings-hint">
                          {m.unresolved.map((u) => (
                            <div key={u.name}>
                              <code>{u.name}</code> needs <code>{u.missing}</code>
                            </div>
                          ))}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------

function CatalogPanel({ onCreateRule, isAdmin, refreshKey }) {
  const [q, setQ] = useState("");
  const [source, setSource] = useState("all");
  const [traps, setTraps] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(async () => {
      setLoading(true);
      try {
        const res = await searchTraps({ q, source, limit: 200 });
        if (!cancelled) {
          setTraps(res);
          setError("");
        }
      } catch (err) {
        if (!cancelled) setError(err.message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [q, source, refreshKey]);

  return (
    <div className="mib-card">
      <h3>Trap catalogue</h3>
      <p className="settings-hint">
        Traps from imported MIBs and Cisco&apos;s public MIBs. A trap without a rule is stored as a named alarm. Turn it
        into a custom trap to give it a severity, or to raise or clear incidents.
      </p>
      <div className="mib-toolbar">
        <input
          type="search"
          placeholder="Search by name, OID, MIB or appliance"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <select value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="all">All sources</option>
          <option value="imported">Imported MIBs</option>
          <option value="cisco">Cisco catalogue</option>
        </select>
        <span className="settings-hint">{loading ? "Searching…" : `${traps.length}${traps.length >= 200 ? "+" : ""} traps`}</span>
      </div>
      {error && <div className="login-error">{error}</div>}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Trap</th>
              <th>OID</th>
              <th>MIB</th>
              <th>Handling</th>
              <th>Description</th>
              {isAdmin && <th />}
            </tr>
          </thead>
          <tbody>
            {traps.map((t) => (
              <tr key={t.oid}>
                <td>
                  <strong>{t.name}</strong>
                  {t.status !== "current" && <span className="settings-hint"> ({t.status})</span>}
                  {t.objects?.length > 0 && <div className="mib-varbinds">{t.objects.join(", ")}</div>}
                </td>
                <td className="mib-oid">{t.oid}</td>
                <td>
                  {t.mib}
                  <div className="settings-hint">{t.appliance || (t.source === "cisco" ? "Cisco" : "")}</div>
                </td>
                <td>
                  <HandlingBadge trap={t} />
                </td>
                <td className="wrap-cell settings-hint">{t.description}</td>
                {isAdmin && (
                  <td>
                    {!t.builtin && (
                      <button onClick={() => onCreateRule(t)}>{t.rule ? "Edit rule" : "Make custom trap"}</button>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------

function RuleForm({ initial, options, onSaved, onCancel }) {
  const [form, setForm] = useState(initial);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (key) => (e) => setForm({ ...form, [key]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const needsType = form.action !== "alarm";

  useEffect(() => setForm(initial), [initial]);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    const body = {
      oid: form.oid,
      name: form.name,
      description: form.description,
      appliance: form.appliance,
      action: form.action,
      incident_type: needsType ? form.incident_type || null : null,
      severity: form.severity,
      interface_varbind: form.interface_varbind || null,
      enabled: form.enabled,
    };
    try {
      if (form.id) await updateTrapRule(form.id, body);
      else await createTrapRule(body);
      onSaved();
    } catch (err) {
      const detail = Array.isArray(err.detail) ? err.detail.map((d) => d.msg.replace(/^Value error, /, "")).join("; ") : err.message;
      setError(detail);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="mib-card mib-rule-form" onSubmit={submit}>
      <h3>{form.id ? `Edit ${initial.name}` : "New custom trap"}</h3>
      <div className="field-grid">
        <label>
          Trap name
          <input value={form.name} onChange={set("name")} maxLength={64} required placeholder="e.g. jnxPowerSupplyFailure" />
        </label>
        <label>
          Trap OID
          <input value={form.oid} onChange={set("oid")} required placeholder="1.3.6.1.4.1.2636.4.1.1" className="mib-oid" />
        </label>
        <label>
          Appliance / vendor
          <input value={form.appliance} onChange={set("appliance")} maxLength={128} placeholder="e.g. Juniper MX" />
        </label>
        <label>
          When received
          <select value={form.action} onChange={set("action")}>
            {(options?.actions ?? Object.keys(ACTION_LABELS)).map((a) => (
              <option key={a} value={a}>
                {ACTION_LABELS[a] ?? a}
              </option>
            ))}
          </select>
        </label>
        {needsType && (
          <label>
            Incident type
            <select value={form.incident_type} onChange={set("incident_type")} required>
              <option value="">Choose…</option>
              {(options?.incident_types ?? []).map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
        )}
        <label>
          Severity
          <select value={form.severity} onChange={set("severity")}>
            {(options?.severities ?? ["critical", "warning", "info"]).map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label>
          Interface varbind (optional)
          <input
            value={form.interface_varbind}
            onChange={set("interface_varbind")}
            placeholder="ifName 1.3.6.1.2.1.31.1.1.1.1"
            className="mib-oid"
          />
        </label>
        <label className="mib-check">
          <input type="checkbox" checked={form.enabled} onChange={set("enabled")} />
          Enabled
        </label>
        <label className="mib-wide">
          Description
          <textarea rows={2} value={form.description} onChange={set("description")} />
        </label>
      </div>
      <p className="settings-hint">
        {form.action === "raise" &&
          "Opens an incident of this type on the router that sent the trap, or bumps the one already open, and follows the normal incident workflow."}
        {form.action === "clear" &&
          "Resolves the open incident of this type on the same router and interface, like linkUp does for linkDown."}
        {form.action === "alarm" && "Stored in the router's alarm history under this name and severity. No incident."}{" "}
        The interface varbind tells the classifier which varbind names the interface (matched exactly or as a prefix,
        since table columns arrive as column.index). Changes reach the alarm pipeline within{" "}
        {options?.cache_ttl_seconds ?? 15} seconds.
      </p>
      {error && <div className="login-error">{error}</div>}
      <div className="mib-actions">
        <button type="submit" className="login-submit" disabled={busy}>
          {busy ? "Saving…" : form.id ? "Save changes" : "Create custom trap"}
        </button>
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function RulesPanel({ rules, options, isAdmin, editing, setEditing, onChanged }) {
  const toggle = async (r) => {
    try {
      await updateTrapRule(r.id, { ...r, interface_varbind: r.interface_varbind || null, enabled: !r.enabled });
      onChanged();
    } catch (err) {
      window.alert(err.message);
    }
  };

  const remove = async (r) => {
    if (!window.confirm(`Delete the custom trap ${r.name} (${r.oid})?`)) return;
    try {
      await deleteTrapRule(r.id);
      onChanged();
    } catch (err) {
      window.alert(err.message);
    }
  };

  return (
    <>
      {isAdmin && editing && (
        <RuleForm
          initial={editing}
          options={options}
          onCancel={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            onChanged();
          }}
        />
      )}
      <div className="mib-card">
        <div className="mib-card-head">
          <h3>Custom traps</h3>
          {isAdmin && !editing && (
            <button className="login-submit" onClick={() => setEditing({ ...EMPTY_RULE })}>
              + New custom trap
            </button>
          )}
        </div>
        {rules.length === 0 ? (
          <p className="settings-hint">
            No custom traps yet. Create one from scratch, or pick a trap in the Trap catalogue and choose &quot;Make
            custom trap&quot;. Built-in traps (linkDown, BGP, IS-IS, ...) can&apos;t be overridden.
          </p>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Trap</th>
                  <th>OID</th>
                  <th>Appliance</th>
                  <th>When received</th>
                  <th>Severity</th>
                  <th>Status</th>
                  {isAdmin && <th />}
                </tr>
              </thead>
              <tbody>
                {rules.map((r) => (
                  <tr key={r.id}>
                    <td>
                      <strong>{r.name}</strong>
                      {r.description && <div className="settings-hint wrap-cell">{r.description}</div>}
                    </td>
                    <td className="mib-oid">{r.oid}</td>
                    <td>{r.appliance || "—"}</td>
                    <td>
                      {ACTION_LABELS[r.action]}
                      {r.incident_type && <div className="settings-hint">{r.incident_type}</div>}
                    </td>
                    <td>
                      <span className={`badge sev-${r.severity}`}>{r.severity}</span>
                    </td>
                    <td>
                      <span className={`badge mib-badge ${r.enabled ? "custom" : "disabled"}`}>
                        {r.enabled ? "Enabled" : "Disabled"}
                      </span>
                    </td>
                    {isAdmin && (
                      <td className="mib-row-actions">
                        <button onClick={() => setEditing({ ...EMPTY_RULE, ...r, incident_type: r.incident_type || "", interface_varbind: r.interface_varbind || "" })}>
                          Edit
                        </button>
                        <button onClick={() => toggle(r)}>{r.enabled ? "Disable" : "Enable"}</button>
                        <button onClick={() => remove(r)}>Delete</button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------

export default function MibManager() {
  const isAdmin = useHasRole("admin");
  const [tab, setTab] = useState("catalog");
  const [options, setOptions] = useState(null);
  const [modules, setModules] = useState([]);
  const [rules, setRules] = useState([]);
  const [editing, setEditing] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [error, setError] = useState("");

  const reload = useCallback(async () => {
    try {
      const [opts, mods, rls] = await Promise.all([fetchMibOptions(), fetchMibModules(), fetchTrapRules()]);
      setOptions(opts);
      setModules(mods);
      setRules(rls);
      setRefreshKey((k) => k + 1);
      setError("");
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const createFromTrap = (t) => {
    setEditing(
      t.rule
        ? { ...EMPTY_RULE, ...t.rule, incident_type: t.rule.incident_type || "", interface_varbind: t.rule.interface_varbind || "" }
        : {
            ...EMPTY_RULE,
            oid: t.oid,
            name: t.name.slice(0, 64),
            description: t.description,
            appliance: t.appliance || (t.source === "cisco" ? "Cisco" : ""),
          }
    );
    setTab("rules");
  };

  const importedTraps = modules.reduce((sum, m) => sum + m.trap_count, 0);

  return (
    <div className="page mib-page">
      <div className="mib-header">
        <div>
          <h2>MIBs &amp; Traps</h2>
          <p className="settings-hint">
            Teach the control room the traps of every appliance in your network.
          </p>
        </div>
        <div className="card-grid mib-stats">
          <div className="card">
            <div className="value">{modules.length}</div>
            <div className="label">imported MIB modules</div>
          </div>
          <div className="card">
            <div className="value">{importedTraps.toLocaleString()}</div>
            <div className="label">traps from imported MIBs</div>
          </div>
          <div className="card">
            <div className="value">{options?.cisco_catalog_size?.toLocaleString() ?? "—"}</div>
            <div className="label">Cisco catalogue traps</div>
          </div>
          <div className="card">
            <div className="value">{rules.length}</div>
            <div className="label">custom traps</div>
          </div>
        </div>
      </div>

      {error && <div className="login-error">{error}</div>}

      <div className="settings-tabs mib-tabs">
        <button className={tab === "catalog" ? "active" : ""} onClick={() => setTab("catalog")}>
          Trap catalogue
        </button>
        <button className={tab === "import" ? "active" : ""} onClick={() => setTab("import")}>
          {isAdmin ? "Import MIBs" : "Imported MIBs"}
        </button>
        <button className={tab === "rules" ? "active" : ""} onClick={() => setTab("rules")}>
          Custom traps ({rules.length})
        </button>
      </div>

      {tab === "catalog" && <CatalogPanel onCreateRule={createFromTrap} isAdmin={isAdmin} refreshKey={refreshKey} />}
      {tab === "import" && <ImportPanel options={options} modules={modules} onChanged={reload} isAdmin={isAdmin} />}
      {tab === "rules" && (
        <RulesPanel
          rules={rules}
          options={options}
          isAdmin={isAdmin}
          editing={editing}
          setEditing={setEditing}
          onChanged={reload}
        />
      )}
    </div>
  );
}
