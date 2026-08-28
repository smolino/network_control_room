import { useCallback, useEffect, useMemo, useState } from "react";
import { createRouterModel, fetchRouterModels, seedBgpPeerings, seedRouters, updateRouterModel } from "../api.js";
import { CITIES, CITY_BY_NAME } from "../cities.js";
import { downloadTextFile, parseCSV, toCSV } from "../csv.js";
import { haversineKm, REPEATER_SPACING_KM } from "../geo.js";

// Shared by every city <input list="city-options"> below - one datalist
// serves any number of inputs referencing its id, so it only needs to be
// rendered once (see the CityDatalist component).
function CityDatalist() {
  return (
    <datalist id="city-options">
      {CITIES.map((c) => (
        <option key={c.city} value={c.city} />
      ))}
    </datalist>
  );
}

// Looks up an exact (case-insensitive) match in the known-city table and,
// if found, autofills latitude/longitude - and country, but only when the
// form hasn't already got one, so it never clobbers a manual entry.
function applyCityMatch(form, cityValue) {
  const match = CITY_BY_NAME[cityValue.trim().toLowerCase()];
  if (!match) return { ...form, city: cityValue };
  return {
    ...form,
    city: cityValue,
    latitude: String(match.latitude),
    longitude: String(match.longitude),
    country: form.country || match.country,
  };
}

const ROUTER_TEMPLATE_HEADERS = [
  "router_type",
  "hostname",
  "mgmt_ip",
  "parent_mgmt_ip",
  "asn",
  "vendor",
  "model",
  "site_name",
  "country",
  "city",
  "latitude",
  "longitude",
];

const ROUTER_TEMPLATE_ROWS = [
  {
    router_type: "primary",
    hostname: "rtr-example-001",
    mgmt_ip: "10.10.9.1",
    parent_mgmt_ip: "",
    asn: 64999,
    vendor: "Cisco",
    model: "ISR4331",
    site_name: "Example PoP",
    country: "Mexico",
    city: "Example City",
    latitude: 19.0,
    longitude: -99.0,
  },
  {
    router_type: "customer",
    hostname: "cust-example-001-01",
    mgmt_ip: "10.20.9.1",
    parent_mgmt_ip: "10.10.9.1",
    asn: "",
    vendor: "Cisco",
    model: "ISR1100-4G",
    site_name: "Example City region, site 1",
    country: "Mexico",
    city: "Example City",
    latitude: 19.05,
    longitude: -99.05,
  },
];

const PEERING_TEMPLATE_HEADERS = ["router_a_mgmt_ip", "router_b_mgmt_ip", "distance_km", "repeater_count"];

const PEERING_TEMPLATE_ROWS = [
  { router_a_mgmt_ip: "10.10.9.1", router_b_mgmt_ip: "10.10.0.1", distance_km: "", repeater_count: "" },
];

function StatusBanner({ status }) {
  if (!status) return null;
  return <div className={status.type === "error" ? "login-error" : "form-success"}>{status.text}</div>;
}

// Shared Vendor/Model dropdown pair for the manual-entry forms below -
// options come from the RouterModel catalog (see RouterModelsCatalog),
// not free text, so every router added here uses a known-good pair.
function VendorModelSelect({ vendor, model, onVendorChange, onModelChange, models }) {
  const vendors = useMemo(
    () => [...new Set(models.map((m) => m.vendor))].sort((a, b) => a.localeCompare(b)),
    [models]
  );
  const modelsForVendor = models.filter((m) => m.vendor === vendor);

  return (
    <>
      <label>
        Vendor
        <select value={vendor} onChange={(e) => onVendorChange(e.target.value)}>
          <option value="">Select a vendor…</option>
          {vendors.map((v) => (
            <option key={v} value={v}>
              {v}
            </option>
          ))}
        </select>
      </label>
      <label>
        Model
        <select value={model} onChange={(e) => onModelChange(e.target.value)} disabled={!vendor}>
          <option value="">{vendor ? "Select a model…" : "Select a vendor first"}</option>
          {modelsForVendor.map((m) => (
            <option key={m.id} value={m.model}>
              {m.model}
            </option>
          ))}
        </select>
      </label>
    </>
  );
}

// Add-model form + full listing for the RouterModel catalog those
// dropdowns draw from - see backend/app/api/router_models.py. A model
// added here is available in every Vendor/Model dropdown on this page
// immediately, no reload needed.
function RouterModelsCatalog({ models, onModelsChanged }) {
  const [vendor, setVendor] = useState("");
  const [model, setModel] = useState("");
  const [status, setStatus] = useState(null);
  const [saving, setSaving] = useState(false);

  const [editingId, setEditingId] = useState(null);
  const [editVendor, setEditVendor] = useState("");
  const [editModel, setEditModel] = useState("");
  const [editStatus, setEditStatus] = useState(null);
  const [editSaving, setEditSaving] = useState(false);

  const sorted = useMemo(
    () => [...models].sort((a, b) => a.vendor.localeCompare(b.vendor) || a.model.localeCompare(b.model)),
    [models]
  );

  const handleSubmit = async (e) => {
    e.preventDefault();
    setStatus(null);
    const v = vendor.trim();
    const m = model.trim();
    if (!v || !m) {
      setStatus({ type: "error", text: "Vendor and model are both required." });
      return;
    }
    setSaving(true);
    try {
      await createRouterModel({ vendor: v, model: m });
      setStatus({ type: "success", text: `Added ${v} ${m} to the catalog.` });
      setVendor("");
      setModel("");
      await onModelsChanged();
    } catch (err) {
      setStatus({ type: "error", text: `Couldn't add model: ${err.message}` });
    } finally {
      setSaving(false);
    }
  };

  const startEdit = (entry) => {
    setEditingId(entry.id);
    setEditVendor(entry.vendor);
    setEditModel(entry.model);
    setEditStatus(null);
  };

  const cancelEdit = () => setEditingId(null);

  const saveEdit = async (entry) => {
    const v = editVendor.trim();
    const m = editModel.trim();
    if (!v || !m) {
      setEditStatus({ type: "error", text: "Vendor and model are both required." });
      return;
    }
    setEditSaving(true);
    try {
      await updateRouterModel(entry.id, { vendor: v, model: m });
      setEditingId(null);
      await onModelsChanged();
    } catch (err) {
      setEditStatus({ type: "error", text: `Couldn't save: ${err.message}` });
    } finally {
      setEditSaving(false);
    }
  };

  return (
    <div className="card">
      <h3>Router models</h3>
      <p className="settings-hint">
        Vendor/model pairs available in the dropdowns on this page. Add one that's missing, or
        edit an existing one — changes are ready to select immediately, no reload needed. Editing
        a pair only changes the catalog entry itself; routers already added with the old
        vendor/model keep what they were saved with.
      </p>
      <form onSubmit={handleSubmit} className="field-grid">
        <label>
          Vendor *
          <input value={vendor} onChange={(e) => setVendor(e.target.value)} placeholder="Juniper" />
        </label>
        <label>
          Model *
          <input value={model} onChange={(e) => setModel(e.target.value)} placeholder="MX960" />
        </label>
        <StatusBanner status={status} />
        <button type="submit" className="login-submit" disabled={saving}>
          {saving ? "Adding…" : "Add model"}
        </button>
      </form>
      <StatusBanner status={editStatus} />
      <div className="table-scroll" style={{ marginTop: "1rem" }}>
        <table>
          <thead>
            <tr>
              <th>Vendor</th>
              <th>Model</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((entry) =>
              editingId === entry.id ? (
                <tr key={entry.id}>
                  <td>
                    <input value={editVendor} onChange={(e) => setEditVendor(e.target.value)} />
                  </td>
                  <td>
                    <input value={editModel} onChange={(e) => setEditModel(e.target.value)} />
                  </td>
                  <td>
                    <span className="link" onClick={() => !editSaving && saveEdit(entry)}>
                      {editSaving ? "Saving…" : "Save"}
                    </span>{" "}
                    <span className="link" onClick={cancelEdit}>
                      Cancel
                    </span>
                  </td>
                </tr>
              ) : (
                <tr key={entry.id}>
                  <td>{entry.vendor}</td>
                  <td>{entry.model}</td>
                  <td>
                    <span className="link" onClick={() => startEdit(entry)}>
                      Edit
                    </span>
                  </td>
                </tr>
              )
            )}
            {sorted.length === 0 && (
              <tr>
                <td colSpan={3}>No models yet.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function AddPrimaryForm({ routers, models, onFleetChanged }) {
  const emptyForm = {
    hostname: "",
    mgmt_ip: "",
    asn: "",
    vendor: "",
    model: "",
    site_name: "",
    country: "",
    city: "",
    latitude: "",
    longitude: "",
  };
  const [form, setForm] = useState(emptyForm);
  const [status, setStatus] = useState(null);
  const [saving, setSaving] = useState(false);

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));
  const handleCityChange = (e) => setForm((f) => applyCityMatch(f, e.target.value));

  const handleSubmit = async (e) => {
    e.preventDefault();
    setStatus(null);
    const hostname = form.hostname.trim();
    const mgmtIp = form.mgmt_ip.trim();
    if (!hostname || !mgmtIp || form.latitude === "" || form.longitude === "") {
      setStatus({ type: "error", text: "Hostname, management IP, latitude, and longitude are required." });
      return;
    }
    if (routers.some((r) => r.mgmt_ip === mgmtIp)) {
      setStatus({ type: "error", text: `A router with management IP ${mgmtIp} already exists.` });
      return;
    }
    setSaving(true);
    try {
      const [created] = await seedRouters([
        {
          hostname,
          mgmt_ip: mgmtIp,
          router_type: "primary",
          asn: form.asn === "" ? null : Number(form.asn),
          vendor: form.vendor.trim() || "Cisco",
          model: form.model.trim() || null,
          site_name: form.site_name.trim() || null,
          country: form.country.trim() || null,
          city: form.city.trim() || null,
          latitude: Number(form.latitude),
          longitude: Number(form.longitude),
        },
      ]);
      setStatus({ type: "success", text: `Added primary router ${created.hostname}.` });
      setForm(emptyForm);
      await onFleetChanged();
    } catch (err) {
      setStatus({ type: "error", text: `Couldn't add router: ${err.message}` });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="card">
      <h3>Add primary (backbone) router</h3>
      <form onSubmit={handleSubmit} className="field-grid">
        <label>
          Hostname *
          <input value={form.hostname} onChange={set("hostname")} placeholder="rtr-city-001" />
        </label>
        <label>
          Management IP *
          <input value={form.mgmt_ip} onChange={set("mgmt_ip")} placeholder="10.10.x.x" />
        </label>
        <label>
          ASN (optional)
          <input value={form.asn} onChange={set("asn")} placeholder="64512-65534" />
        </label>
        <VendorModelSelect
          vendor={form.vendor}
          model={form.model}
          onVendorChange={(v) => setForm((f) => ({ ...f, vendor: v, model: "" }))}
          onModelChange={(m) => setForm((f) => ({ ...f, model: m }))}
          models={models}
        />
        <label>
          Site name
          <input value={form.site_name} onChange={set("site_name")} placeholder="City PoP" />
        </label>
        <label>
          Country
          <input value={form.country} onChange={set("country")} />
        </label>
        <label>
          City
          <input
            value={form.city}
            onChange={handleCityChange}
            list="city-options"
            placeholder="Start typing a known city…"
          />
        </label>
        <label>
          Latitude *
          <input value={form.latitude} onChange={set("latitude")} placeholder="19.4326" />
        </label>
        <label>
          Longitude *
          <input value={form.longitude} onChange={set("longitude")} placeholder="-99.1332" />
        </label>
        <StatusBanner status={status} />
        <button type="submit" className="login-submit" disabled={saving}>
          {saving ? "Adding…" : "Add primary router"}
        </button>
      </form>
    </div>
  );
}

function AddCustomerForm({ primaries, routers, models, onFleetChanged }) {
  const emptyForm = {
    hostname: "",
    mgmt_ip: "",
    parent_mgmt_ip: "",
    vendor: "",
    model: "",
    site_name: "",
    country: "",
    city: "",
    latitude: "",
    longitude: "",
  };
  const [form, setForm] = useState(emptyForm);
  const [status, setStatus] = useState(null);
  const [saving, setSaving] = useState(false);

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));
  const handleCityChange = (e) => setForm((f) => applyCityMatch(f, e.target.value));

  const handleParentChange = (e) => {
    const mgmtIp = e.target.value;
    const parent = primaries.find((p) => p.mgmt_ip === mgmtIp);
    setForm((f) => ({
      ...f,
      parent_mgmt_ip: mgmtIp,
      country: f.country || parent?.country || "",
      city: f.city || parent?.city || "",
    }));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setStatus(null);
    const hostname = form.hostname.trim();
    const mgmtIp = form.mgmt_ip.trim();
    if (!hostname || !mgmtIp || !form.parent_mgmt_ip || form.latitude === "" || form.longitude === "") {
      setStatus({
        type: "error",
        text: "Hostname, management IP, parent primary, latitude, and longitude are required.",
      });
      return;
    }
    if (routers.some((r) => r.mgmt_ip === mgmtIp)) {
      setStatus({ type: "error", text: `A router with management IP ${mgmtIp} already exists.` });
      return;
    }
    setSaving(true);
    try {
      const [created] = await seedRouters([
        {
          hostname,
          mgmt_ip: mgmtIp,
          router_type: "customer",
          parent_mgmt_ip: form.parent_mgmt_ip,
          vendor: form.vendor.trim() || "Cisco",
          model: form.model.trim() || null,
          site_name: form.site_name.trim() || null,
          country: form.country.trim() || null,
          city: form.city.trim() || null,
          latitude: Number(form.latitude),
          longitude: Number(form.longitude),
        },
      ]);
      setStatus({ type: "success", text: `Added customer router ${created.hostname}.` });
      setForm(emptyForm);
      await onFleetChanged();
    } catch (err) {
      setStatus({ type: "error", text: `Couldn't add router: ${err.message}` });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="card">
      <h3>Add customer (last-mile CPE) router</h3>
      {primaries.length === 0 && <p className="settings-hint">Add at least one primary router first.</p>}
      <form onSubmit={handleSubmit} className="field-grid">
        <label>
          Hostname *
          <input value={form.hostname} onChange={set("hostname")} placeholder="cust-city-001-01" />
        </label>
        <label>
          Management IP *
          <input value={form.mgmt_ip} onChange={set("mgmt_ip")} placeholder="10.20.x.x" />
        </label>
        <label>
          Parent primary *
          <select value={form.parent_mgmt_ip} onChange={handleParentChange}>
            <option value="">Select a primary…</option>
            {primaries.map((p) => (
              <option key={p.id} value={p.mgmt_ip}>
                {p.hostname} ({p.mgmt_ip})
              </option>
            ))}
          </select>
        </label>
        <VendorModelSelect
          vendor={form.vendor}
          model={form.model}
          onVendorChange={(v) => setForm((f) => ({ ...f, vendor: v, model: "" }))}
          onModelChange={(m) => setForm((f) => ({ ...f, model: m }))}
          models={models}
        />
        <label>
          Site name
          <input value={form.site_name} onChange={set("site_name")} />
        </label>
        <label>
          Country
          <input value={form.country} onChange={set("country")} />
        </label>
        <label>
          City
          <input
            value={form.city}
            onChange={handleCityChange}
            list="city-options"
            placeholder="Start typing a known city…"
          />
        </label>
        <label>
          Latitude *
          <input value={form.latitude} onChange={set("latitude")} />
        </label>
        <label>
          Longitude *
          <input value={form.longitude} onChange={set("longitude")} />
        </label>
        <StatusBanner status={status} />
        <button type="submit" className="login-submit" disabled={saving || primaries.length === 0}>
          {saving ? "Adding…" : "Add customer router"}
        </button>
      </form>
    </div>
  );
}

function AddPeeringForm({ primaries, onFleetChanged }) {
  const [routerAIp, setRouterAIp] = useState("");
  const [routerBIp, setRouterBIp] = useState("");
  const [distanceKm, setDistanceKm] = useState("");
  const [repeaterCount, setRepeaterCount] = useState("");
  const [distanceTouched, setDistanceTouched] = useState(false);
  const [repeaterTouched, setRepeaterTouched] = useState(false);
  const [status, setStatus] = useState(null);
  const [saving, setSaving] = useState(false);

  const routerA = primaries.find((p) => p.mgmt_ip === routerAIp);
  const routerB = primaries.find((p) => p.mgmt_ip === routerBIp);

  useEffect(() => {
    if (!routerA || !routerB) return;
    const km = haversineKm(routerA.latitude, routerA.longitude, routerB.latitude, routerB.longitude);
    if (!distanceTouched) setDistanceKm(km.toFixed(1));
    if (!repeaterTouched) setRepeaterCount(String(Math.floor(km / REPEATER_SPACING_KM)));
    // Only recompute off the router selection, not every keystroke on the
    // (possibly manually-overridden) distance/repeater fields themselves.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routerAIp, routerBIp]);

  const resetForm = () => {
    setRouterAIp("");
    setRouterBIp("");
    setDistanceKm("");
    setRepeaterCount("");
    setDistanceTouched(false);
    setRepeaterTouched(false);
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setStatus(null);
    if (!routerAIp || !routerBIp) {
      setStatus({ type: "error", text: "Select both routers to link." });
      return;
    }
    if (routerAIp === routerBIp) {
      setStatus({ type: "error", text: "Select two different routers." });
      return;
    }
    setSaving(true);
    try {
      await seedBgpPeerings([
        {
          router_a_mgmt_ip: routerAIp,
          router_b_mgmt_ip: routerBIp,
          distance_km: Number(distanceKm) || 0,
          repeater_count: Number(repeaterCount) || 0,
        },
      ]);
      setStatus({ type: "success", text: `Linked ${routerA.hostname} ↔ ${routerB.hostname}.` });
      resetForm();
      await onFleetChanged();
    } catch (err) {
      setStatus({ type: "error", text: `Couldn't create the link: ${err.message}` });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="card">
      <h3>Add BGP link (repeaters)</h3>
      <p className="settings-hint">
        Links two primary routers over simulated fiber. Distance and repeater count (one regenerator
        every {REPEATER_SPACING_KM}km) are auto-filled from the two sites' coordinates — override
        either field if you want a specific value. Linking a pair that's already connected updates
        its distance/repeater count instead of creating a duplicate.
      </p>
      {primaries.length < 2 && <p className="settings-hint">Add at least two primary routers first.</p>}
      <form onSubmit={handleSubmit} className="field-grid">
        <label>
          Router A *
          <select value={routerAIp} onChange={(e) => setRouterAIp(e.target.value)}>
            <option value="">Select a primary…</option>
            {primaries.map((p) => (
              <option key={p.id} value={p.mgmt_ip}>
                {p.hostname} ({p.mgmt_ip})
              </option>
            ))}
          </select>
        </label>
        <label>
          Router B *
          <select value={routerBIp} onChange={(e) => setRouterBIp(e.target.value)}>
            <option value="">Select a primary…</option>
            {primaries
              .filter((p) => p.mgmt_ip !== routerAIp)
              .map((p) => (
                <option key={p.id} value={p.mgmt_ip}>
                  {p.hostname} ({p.mgmt_ip})
                </option>
              ))}
          </select>
        </label>
        <label>
          Distance (km)
          <input
            value={distanceKm}
            onChange={(e) => {
              setDistanceTouched(true);
              setDistanceKm(e.target.value);
            }}
          />
        </label>
        <label>
          Repeater count
          <input
            value={repeaterCount}
            onChange={(e) => {
              setRepeaterTouched(true);
              setRepeaterCount(e.target.value);
            }}
          />
        </label>
        <StatusBanner status={status} />
        <button type="submit" className="login-submit" disabled={saving || primaries.length < 2}>
          {saving ? "Linking…" : "Add link"}
        </button>
      </form>
    </div>
  );
}

function ManualEntry({ routers, primaries, models, onFleetChanged, onModelsChanged }) {
  return (
    <div className="card-grid">
      <AddPrimaryForm routers={routers} models={models} onFleetChanged={onFleetChanged} />
      <AddCustomerForm primaries={primaries} routers={routers} models={models} onFleetChanged={onFleetChanged} />
      <AddPeeringForm primaries={primaries} onFleetChanged={onFleetChanged} />
      <RouterModelsCatalog models={models} onModelsChanged={onModelsChanged} />
    </div>
  );
}

function CSVPreviewTable({ headers, rows }) {
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            {headers.map((h) => (
              <th key={h}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 5).map((row, i) => (
            <tr key={i}>
              {headers.map((h) => (
                <td key={h}>{row[h]}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > 5 && <p className="settings-hint">…and {rows.length - 5} more row(s).</p>}
    </div>
  );
}

function BulkUpload({ routers, onFleetChanged }) {
  const [routerFile, setRouterFile] = useState(null);
  const [routerPreview, setRouterPreview] = useState([]);
  const [peeringFile, setPeeringFile] = useState(null);
  const [peeringPreview, setPeeringPreview] = useState([]);
  const [routerStatus, setRouterStatus] = useState(null);
  const [peeringStatus, setPeeringStatus] = useState(null);
  const [uploadingRouters, setUploadingRouters] = useState(false);
  const [uploadingPeerings, setUploadingPeerings] = useState(false);

  const handleRouterFile = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setRouterPreview(parseCSV(await file.text()));
    setRouterFile(file);
    setRouterStatus(null);
  };

  const handlePeeringFile = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setPeeringPreview(parseCSV(await file.text()));
    setPeeringFile(file);
    setPeeringStatus(null);
  };

  const submitRouters = async () => {
    setUploadingRouters(true);
    setRouterStatus(null);
    try {
      const toPayload = (r) => ({
        hostname: r.hostname,
        mgmt_ip: r.mgmt_ip,
        router_type: (r.router_type || "primary").trim() || "primary",
        parent_mgmt_ip: r.parent_mgmt_ip || null,
        asn: r.asn ? Number(r.asn) : null,
        vendor: r.vendor || "Cisco",
        model: r.model || null,
        site_name: r.site_name || null,
        country: r.country || null,
        city: r.city || null,
        latitude: Number(r.latitude),
        longitude: Number(r.longitude),
      });
      // Primaries first: a customer row's parent_mgmt_ip is resolved to a
      // real router id at seed time, so its parent primary must already be
      // in the database (see backend/app/api/routers.py:seed_routers) - even
      // when both rows come from the same uploaded file.
      const primaryRows = routerPreview.filter((r) => (r.router_type || "primary").trim() === "primary");
      const customerRows = routerPreview.filter((r) => (r.router_type || "").trim() === "customer");
      let count = 0;
      if (primaryRows.length) count += (await seedRouters(primaryRows.map(toPayload))).length;
      if (customerRows.length) count += (await seedRouters(customerRows.map(toPayload))).length;
      setRouterStatus({
        type: "success",
        text: `Seeded ${count} router(s) — ${primaryRows.length} primary, ${customerRows.length} customer.`,
      });
      setRouterPreview([]);
      setRouterFile(null);
      await onFleetChanged();
    } catch (err) {
      setRouterStatus({ type: "error", text: `Upload failed: ${err.message}` });
    } finally {
      setUploadingRouters(false);
    }
  };

  const submitPeerings = async () => {
    setUploadingPeerings(true);
    setPeeringStatus(null);
    try {
      const byIp = Object.fromEntries(routers.map((r) => [r.mgmt_ip, r]));
      const pairs = peeringPreview.map((row) => {
        const a = byIp[row.router_a_mgmt_ip];
        const b = byIp[row.router_b_mgmt_ip];
        let distance = row.distance_km ? Number(row.distance_km) : null;
        let repeaters = row.repeater_count ? Number(row.repeater_count) : null;
        if ((distance === null || repeaters === null) && a && b) {
          const km = haversineKm(a.latitude, a.longitude, b.latitude, b.longitude);
          if (distance === null) distance = Math.round(km * 10) / 10;
          if (repeaters === null) repeaters = Math.floor(km / REPEATER_SPACING_KM);
        }
        return {
          router_a_mgmt_ip: row.router_a_mgmt_ip,
          router_b_mgmt_ip: row.router_b_mgmt_ip,
          distance_km: distance ?? 0,
          repeater_count: repeaters ?? 0,
        };
      });
      const created = await seedBgpPeerings(pairs);
      setPeeringStatus({ type: "success", text: `Seeded ${created.length} BGP link(s).` });
      setPeeringPreview([]);
      setPeeringFile(null);
      await onFleetChanged();
    } catch (err) {
      setPeeringStatus({ type: "error", text: `Upload failed: ${err.message}` });
    } finally {
      setUploadingPeerings(false);
    }
  };

  return (
    <div className="card-grid">
      <div className="card">
        <h3>Bulk upload routers (CSV)</h3>
        <p className="settings-hint">
          Columns: router_type (primary/customer), hostname, mgmt_ip, parent_mgmt_ip (customers
          only), asn, vendor, model, site_name, country, city, latitude, longitude. Primaries are
          always seeded before customers, regardless of row order in the file.
        </p>
        <button
          type="button"
          onClick={() =>
            downloadTextFile("routers_template.csv", toCSV(ROUTER_TEMPLATE_HEADERS, ROUTER_TEMPLATE_ROWS))
          }
        >
          Download template
        </button>
        <div style={{ marginTop: "0.75rem" }}>
          <input type="file" accept=".csv" onChange={handleRouterFile} />
        </div>
        {routerPreview.length > 0 && (
          <>
            <p className="settings-hint">
              {routerPreview.length} row(s) parsed from {routerFile?.name}.
            </p>
            <CSVPreviewTable headers={ROUTER_TEMPLATE_HEADERS} rows={routerPreview} />
            <button
              className="login-submit"
              onClick={submitRouters}
              disabled={uploadingRouters}
              style={{ marginTop: "0.75rem" }}
            >
              {uploadingRouters ? "Uploading…" : `Seed ${routerPreview.length} router(s)`}
            </button>
          </>
        )}
        <StatusBanner status={routerStatus} />
      </div>

      <div className="card">
        <h3>Bulk upload BGP links / repeaters (CSV)</h3>
        <p className="settings-hint">
          Columns: router_a_mgmt_ip, router_b_mgmt_ip, distance_km, repeater_count. Leave the last
          two blank to auto-calculate from the routers' coordinates (one repeater every{" "}
          {REPEATER_SPACING_KM}km). Both routers must already exist — upload/add them first.
        </p>
        <button
          type="button"
          onClick={() =>
            downloadTextFile("bgp_links_template.csv", toCSV(PEERING_TEMPLATE_HEADERS, PEERING_TEMPLATE_ROWS))
          }
        >
          Download template
        </button>
        <div style={{ marginTop: "0.75rem" }}>
          <input type="file" accept=".csv" onChange={handlePeeringFile} />
        </div>
        {peeringPreview.length > 0 && (
          <>
            <p className="settings-hint">
              {peeringPreview.length} row(s) parsed from {peeringFile?.name}.
            </p>
            <CSVPreviewTable headers={PEERING_TEMPLATE_HEADERS} rows={peeringPreview} />
            <button
              className="login-submit"
              onClick={submitPeerings}
              disabled={uploadingPeerings}
              style={{ marginTop: "0.75rem" }}
            >
              {uploadingPeerings ? "Uploading…" : `Seed ${peeringPreview.length} link(s)`}
            </button>
          </>
        )}
        <StatusBanner status={peeringStatus} />
      </div>
    </div>
  );
}

export default function AddFleet({ routers, onFleetChanged }) {
  const [tab, setTab] = useState("manual");
  const [models, setModels] = useState([]);
  const primaries = useMemo(() => routers.filter((r) => r.router_type === "primary"), [routers]);

  const reloadModels = useCallback(async () => {
    setModels(await fetchRouterModels());
  }, []);

  useEffect(() => {
    reloadModels().catch(console.error);
  }, [reloadModels]);

  return (
    <div className="page">
      <CityDatalist />
      <h2>Add fleet</h2>
      <p className="settings-hint">
        Add primary (backbone) routers, last-mile customer routers, and the BGP links between
        primaries (which carry the repeater dots shown on the map) — one at a time or in bulk via
        CSV. New entries appear on the Map and Routers tabs immediately. Typing a known city name
        (see the suggestions as you type) autofills its latitude/longitude.
      </p>
      <div className="settings-tabs">
        <button className={tab === "manual" ? "active" : ""} onClick={() => setTab("manual")}>
          Manual entry
        </button>
        <button className={tab === "bulk" ? "active" : ""} onClick={() => setTab("bulk")}>
          Bulk upload
        </button>
      </div>
      {tab === "manual" && (
        <ManualEntry
          routers={routers}
          primaries={primaries}
          models={models}
          onFleetChanged={onFleetChanged}
          onModelsChanged={reloadModels}
        />
      )}
      {tab === "bulk" && <BulkUpload routers={routers} onFleetChanged={onFleetChanged} />}
    </div>
  );
}
