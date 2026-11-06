// The Add Fleet > Optical corridor form's model, and its conversion to and
// from a corridor spec (backend/app/schemas.py:CorridorSpec - the same file
// POST /api/corridors/import takes and GET /api/corridors/{id}/spec returns).
//
// The form asks for what a person knows about a corridor - its sites in
// order, the elements' names and IPs, the spans, the transponders, which
// routers each wavelength connects and on which ports - and fills in the
// rest: the line cards from a platform preset, transponder trunk/client
// port numbers, site codes, L3 links. Loading an existing corridor keeps its
// own cards and ports.

let nextId = 1;
const id = () => nextId++;

export const DEFAULT_WAVELENGTHS = ["1560.61", "1561.42", "1562.23", "1563.05"];

// Cisco ONS 15454 MSTP, as on the Bilbao-Madrid corridor: OSC (TNCS) in
// slots 1/8, booster in 2 and preamp in 3 at a terminal, 40-ch mux/demux in
// 4/6; an in-line amplifier site amplifies A->Z in slot 2 and Z->A in slot 7.
export function terminalCards(isA) {
  const tx = isA ? "AZ" : "ZA";
  const rx = isA ? "ZA" : "AZ";
  return [
    { slot: 1, type: "TNCS", function: "osc", direction: isA ? "Z" : "A" },
    { slot: 2, type: "OPT-BST-E", function: "booster", direction: tx },
    { slot: 3, type: "OPT-AMP-C", function: "preamp", direction: rx },
    { slot: 4, type: "40-WSS-C", function: "mux", direction: tx },
    { slot: 6, type: "40-DMX-C", function: "demux", direction: rx },
    { slot: 8, type: "TNCS", function: "osc", direction: null },
  ];
}

export function ilaCards(ampType = "OPT-AMP-C") {
  return [
    { slot: 1, type: "TNCS", function: "osc", direction: "A" },
    { slot: 2, type: ampType, function: "line_amp", direction: "AZ" },
    { slot: 7, type: ampType, function: "line_amp", direction: "ZA" },
    { slot: 8, type: "TNCS", function: "osc", direction: "Z" },
  ];
}

export function newSite(kind) {
  return {
    id: id(),
    kind, // "pop" | "ila"
    code: "",
    name: "",
    city: "",
    country: "",
    latitude: "",
    longitude: "",
    hostname: "",
    mgmt_ip: "",
    model: "ONS 15454",
    ampType: "OPT-AMP-C",
    cards: null, // null = from the preset; loaded corridors keep their own
  };
}

export function newSpan() {
  return { id: id(), provider: "", a_end: "A", b_end: "B", length_km: "", provider_circuit_id: "" };
}

export function newCircuit() {
  return { id: id(), aPort: "", zPort: "", aClient: "", zClient: "" };
}

export function newWavelength(nm = "") {
  return {
    id: id(),
    nm,
    trunkA: "",
    trunkZ: "",
    routerA: "",
    routerZ: "",
    bundleA: "Bundle-Ether1",
    bundleZ: "Bundle-Ether1",
    circuits: [newCircuit()],
  };
}

export function newRouter(end) {
  return { id: id(), end, hostname: "", mgmt_ip: "", model: "ASR9K" };
}

export function emptyForm() {
  return {
    name: "",
    description: "",
    provider: "",
    sites: [newSite("pop"), newSite("ila"), newSite("pop")],
    spans: [newSpan(), newSpan()],
    txpA: { hostname: "", mgmt_ip: "", model: "NCS1004" },
    txpZ: { hostname: "", mgmt_ip: "", model: "NCS1004" },
    routers: [newRouter("A"), newRouter("Z")],
    wavelengths: [newWavelength(DEFAULT_WAVELENGTHS[0])],
    assumptions: [],
    // Ports of loaded routers/transponders not used by a circuit, kept so
    // re-importing an edited corridor doesn't drop them.
    extraPorts: {},
  };
}

// Spans always sit between consecutive sites.
export function syncSpans(form) {
  const n = Math.max(form.sites.length - 1, 0);
  const spans = form.spans.slice(0, n);
  while (spans.length < n) spans.push(newSpan());
  return { ...form, spans };
}

const slug = (s) =>
  (s || "")
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toUpperCase()
    .replace(/[^A-Z0-9]+/g, "");

export function siteCode(form, index) {
  const site = form.sites[index];
  if (site.code.trim()) return site.code.trim();
  const corridor = slug(form.name) || "CORRIDOR";
  if (site.kind === "pop") return (slug(site.city || site.name).slice(0, 5) || (index === 0 ? "POPA" : "POPZ")) + "01";
  return `${corridor}-A${String(index).padStart(4, "0")}`;
}

// Default hostnames, prod-style: t-<pop>-<corridor>, a-000N-<corridor>
// (numbered from the A end), txp1.<pop>.
export function suggestedHostname(form, index) {
  const site = form.sites[index];
  const corridor = (form.name || "corridor").toLowerCase().replace(/[^a-z0-9]+/g, "-");
  if (site.kind === "pop") return `t-${slug(site.city || site.name).toLowerCase().slice(0, 5) || (index === 0 ? "a" : "z")}01-${corridor}`;
  return `a-${String(index).padStart(4, "0")}-${corridor}`;
}

export function trunkPort(form, w, end) {
  const explicit = (end === "A" ? w.trunkA : w.trunkZ).trim();
  return explicit || `0/0/0/${form.wavelengths.indexOf(w)}`;
}

// Client ports numbered after the trunks (NCS1004: trunks 0/0/0/0-1,
// clients 0/0/0/2...), in circuit order, skipping any typed explicitly.
export function clientPorts(form, end) {
  const key = end === "A" ? "aClient" : "zClient";
  const taken = new Set(form.wavelengths.flatMap((w) => w.circuits.map((c) => c[key].trim()).filter(Boolean)));
  const out = new Map();
  let n = form.wavelengths.length;
  for (const w of form.wavelengths) {
    for (const c of w.circuits) {
      if (c[key].trim()) {
        out.set(c.id, c[key].trim());
        continue;
      }
      while (taken.has(`0/0/0/${n}`)) n++;
      out.set(c.id, `0/0/0/${n}`);
      n++;
    }
  }
  return out;
}

const num = (v) => (v === "" || v === null || v === undefined ? null : Number(v));

export function formToSpec(form) {
  const sites = form.sites;
  const codes = sites.map((_, i) => siteCode(form, i));
  const codeA = codes[0];
  const codeZ = codes[codes.length - 1];
  const hostname = (i) => sites[i].hostname.trim() || suggestedHostname(form, i);
  const clientsA = clientPorts(form, "A");
  const clientsZ = clientPorts(form, "Z");

  const siteSpecs = sites.map((s, i) => ({
    code: codes[i],
    name: s.name.trim() || s.city.trim() || codes[i],
    kind: s.kind,
    city: s.city.trim() || null,
    country: s.country.trim() || null,
    latitude: num(s.latitude),
    longitude: num(s.longitude),
  }));

  const devices = sites.map((s, i) => ({
    hostname: hostname(i),
    mgmt_ip: s.mgmt_ip.trim(),
    role: s.kind === "pop" ? "terminal" : "ila",
    vendor: "Cisco",
    model: s.model || "ONS 15454",
    site: codes[i],
    cards: s.cards || (s.kind === "pop" ? terminalCards(i === 0) : ilaCards(s.ampType)),
    ports: [],
  }));

  for (const [end, txp, clients] of [
    ["A", form.txpA, clientsA],
    ["Z", form.txpZ, clientsZ],
  ]) {
    const ports = [];
    for (const w of form.wavelengths) {
      ports.push({ name: trunkPort(form, w, end), kind: "trunk", wavelength_nm: num(w.nm) });
      for (const c of w.circuits) ports.push({ name: clients.get(c.id), kind: "client", wavelength_nm: null });
    }
    for (const p of form.extraPorts[txp.hostname.trim()] || []) {
      if (!ports.some((q) => q.name === p.name)) ports.push(p);
    }
    devices.push({
      hostname: txp.hostname.trim(),
      mgmt_ip: txp.mgmt_ip.trim(),
      role: "transponder",
      vendor: "Cisco",
      model: txp.model || "NCS1004",
      site: end === "A" ? codeA : codeZ,
      cards: [],
      ports,
    });
  }

  for (const r of form.routers) {
    const name = r.hostname.trim();
    if (!name) continue;
    const ports = [];
    for (const w of form.wavelengths) {
      for (const c of w.circuits) {
        if (w.routerA === name && c.aPort.trim()) ports.push({ name: c.aPort.trim(), kind: "router_eth", wavelength_nm: null });
        if (w.routerZ === name && c.zPort.trim()) ports.push({ name: c.zPort.trim(), kind: "router_eth", wavelength_nm: null });
      }
    }
    for (const p of form.extraPorts[name] || []) {
      if (!ports.some((q) => q.name === p.name)) ports.push(p);
    }
    devices.push({
      hostname: name,
      mgmt_ip: r.mgmt_ip.trim(),
      role: "router",
      vendor: "Cisco",
      model: r.model || "ASR9K",
      site: r.end === "A" ? codeA : codeZ,
      cards: [],
      ports,
    });
  }

  const spans = form.spans.map((s, i) => ({
    seq: i + 1,
    from_device: hostname(i),
    to_device: hostname(i + 1),
    provider: s.provider.trim() || form.provider.trim() || null,
    a_end: s.a_end.trim() || null,
    b_end: s.b_end.trim() || null,
    length_km: num(s.length_km),
    provider_circuit_id: s.provider_circuit_id.trim() || null,
  }));

  const circuits = form.wavelengths.flatMap((w) =>
    w.circuits.map((c) => ({
      wavelength_nm: num(w.nm),
      a_router: w.routerA,
      a_router_port: c.aPort.trim(),
      a_transponder: form.txpA.hostname.trim(),
      a_client_port: clientsA.get(c.id),
      z_transponder: form.txpZ.hostname.trim(),
      z_client_port: clientsZ.get(c.id),
      z_router: w.routerZ,
      z_router_port: c.zPort.trim(),
    }))
  );

  const l3 = new Map();
  for (const w of form.wavelengths) {
    if (!w.routerA || !w.routerZ) continue;
    const key = `${w.routerA}|${w.routerZ}`;
    if (!l3.has(key)) {
      l3.set(key, {
        router_a: w.routerA,
        router_z: w.routerZ,
        bundle_a: w.bundleA.trim() || "Bundle-Ether1",
        bundle_z: w.bundleZ.trim() || "Bundle-Ether1",
        member_bandwidth_mbps: 100000,
      });
    }
  }

  return {
    name: form.name.trim(),
    description: form.description.trim() || null,
    assumptions: form.assumptions,
    sites: siteSpecs.filter((s, i) => codes.indexOf(s.code) === i),
    devices,
    spans,
    circuits,
    l3_links: [...l3.values()],
  };
}

// An existing corridor (GET /api/corridors/{id}/spec) as a form, keeping its
// cards and any ports the form doesn't otherwise know about.
export function specToForm(spec) {
  const byHost = Object.fromEntries(spec.devices.map((d) => [d.hostname, d]));
  const siteByCode = Object.fromEntries(spec.sites.map((s) => [s.code, s]));
  const spans = [...spec.spans].sort((a, b) => a.seq - b.seq);
  const chain = spans.length ? [spans[0].from_device, ...spans.map((s) => s.to_device)] : [];
  const codeA = byHost[chain[0]]?.site;
  const codeZ = byHost[chain[chain.length - 1]]?.site;

  const sites = chain.map((h, i) => {
    const d = byHost[h];
    const s = siteByCode[d.site] || {};
    const amp = d.cards.find((c) => c.function === "line_amp");
    return {
      ...newSite(i === 0 || i === chain.length - 1 ? "pop" : "ila"),
      code: s.code || "",
      name: s.name || "",
      city: s.city || "",
      country: s.country || "",
      latitude: s.latitude ?? "",
      longitude: s.longitude ?? "",
      hostname: d.hostname,
      mgmt_ip: d.mgmt_ip,
      model: d.model || "ONS 15454",
      ampType: amp ? amp.type : "OPT-AMP-C",
      cards: d.cards,
    };
  });

  const txps = spec.devices.filter((d) => d.role === "transponder");
  const txpOf = (code) => txps.find((d) => d.site === code);
  const toTxp = (d) => (d ? { hostname: d.hostname, mgmt_ip: d.mgmt_ip, model: d.model || "NCS1004" } : { hostname: "", mgmt_ip: "", model: "NCS1004" });
  const txpA = txpOf(codeA);
  const txpZ = txpOf(codeZ);

  const routers = spec.devices
    .filter((d) => d.role === "router")
    .map((d) => ({ ...newRouter(d.site === codeZ ? "Z" : "A"), hostname: d.hostname, mgmt_ip: d.mgmt_ip, model: d.model || "ASR9K" }));

  const trunkFor = (txp, nm) => (txp?.ports || []).find((p) => p.kind === "trunk" && p.wavelength_nm === nm)?.name || "";
  const wls = [...new Set(spec.circuits.map((c) => c.wavelength_nm))].sort((a, b) => a - b);
  const wavelengths = wls.map((nm) => {
    const cs = spec.circuits.filter((c) => c.wavelength_nm === nm);
    const link = spec.l3_links.find((l) => l.router_a === cs[0].a_router && l.router_z === cs[0].z_router);
    return {
      ...newWavelength(String(nm)),
      trunkA: trunkFor(txpA, nm),
      trunkZ: trunkFor(txpZ, nm),
      routerA: cs[0].a_router,
      routerZ: cs[0].z_router,
      bundleA: link?.bundle_a || "Bundle-Ether1",
      bundleZ: link?.bundle_z || "Bundle-Ether1",
      circuits: cs.map((c) => ({
        ...newCircuit(),
        aPort: c.a_router_port,
        zPort: c.z_router_port,
        aClient: c.a_client_port,
        zClient: c.z_client_port,
      })),
    };
  });

  const providers = spans.map((s) => s.provider).filter(Boolean);
  const common = providers.sort((a, b) => providers.filter((p) => p === b).length - providers.filter((p) => p === a).length)[0] || "";

  const used = new Set(spec.circuits.flatMap((c) => [`${c.a_router}|${c.a_router_port}`, `${c.z_router}|${c.z_router_port}`, `${c.a_transponder}|${c.a_client_port}`, `${c.z_transponder}|${c.z_client_port}`]));
  const extraPorts = {};
  for (const d of spec.devices) {
    if (d.role !== "router" && d.role !== "transponder") continue;
    const extra = d.ports.filter((p) => p.kind !== "trunk" && !used.has(`${d.hostname}|${p.name}`));
    if (extra.length) extraPorts[d.hostname] = extra;
  }

  return {
    name: spec.name,
    description: spec.description || "",
    provider: common,
    sites,
    spans: spans.map((s) => ({
      ...newSpan(),
      provider: s.provider && s.provider !== common ? s.provider : "",
      a_end: s.a_end || "",
      b_end: s.b_end || "",
      length_km: s.length_km ?? "",
      provider_circuit_id: s.provider_circuit_id || "",
    })),
    txpA: toTxp(txpA),
    txpZ: toTxp(txpZ),
    routers,
    wavelengths,
    assumptions: spec.assumptions || [],
    extraPorts,
  };
}
