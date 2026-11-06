#!/usr/bin/env python3
"""Converts a corridor layout sheet (the semicolon CSV export of the
Bilbao-Madrid style diagram) into a corridor spec for
POST /api/corridors/import (see backend/app/schemas.py:CorridorSpec).

The sheet is a drawing, not a table, so this reads it by landmarks:

* the row holding the two "slot" markers - between them are the line
  elements (terminal, ILAs, terminal), outside them each POP's routers and
  transponder;
* the rows below with slot numbers in the left "slot" column - one card per
  line element per slot ("empty" = no card);
* the "POP" header row and the row under it - site names, and each span's
  "A;Fiberprov;B" cells between two line elements;
* per POP, the router name rows split the routers' ports/transponder
  client ports into groups: each group's router ports, in order, plug into
  that group's client ports, in order, which ride the group's trunk
  wavelength.

Anything the sheet doesn't hold (management IPs, coordinates, names already
used in the live fleet) comes from an overrides JSON. Everything the
converter had to assume is written into the spec's "assumptions" list and
printed, so it can be checked against the real network.

Usage (stdlib only):
    ./scripts/convert-corridor-sheet.py seed/corridors/bio1-mad1.sheet.csv \\
        seed/corridors/bio1-mad1.overrides.json > seed/corridors/bio1-mad1.json
"""

import csv
import json
import re
import sys

PORT_RE = re.compile(r"^\d+/\d+/\d+/\d+$")
WAVE_RE = re.compile(r"trunk\s+wave\s*(\d+)[,.](\d+)", re.I)


def cell(rows, r, c):
    if r < 0 or r >= len(rows) or c < 0 or c >= len(rows[r]):
        return ""
    return rows[r][c].strip()


def find_row(rows, predicate):
    for i, row in enumerate(rows):
        if predicate([c.strip() for c in row]):
            return i
    raise SystemExit("sheet layout not recognised")


def card_function(card_type: str, role: str) -> str | None:
    t = card_type.upper()
    if "WSS" in t:
        return "mux"
    if "DMX" in t:
        return "demux"
    if "BST" in t:
        return "booster"
    if "TNCS" in t or "OSC" in t:
        return "osc"
    if "AMP" in t or "EDFA" in t:
        return "preamp" if role == "terminal" else "line_amp"
    return None


def parse(rows, overrides):
    assumptions = list(overrides.get("assumptions", []))

    # Line elements: between the two "slot" markers.
    host_row = find_row(rows, lambda r: r.count("slot") >= 2)
    slot_cols = [c for c, v in enumerate(rows[host_row]) if v.strip() == "slot"]
    slot_a, slot_z = slot_cols[0], slot_cols[-1]
    line_cols = [c for c in range(slot_a + 1, slot_z) if cell(rows, host_row, c)]
    line_names = [cell(rows, host_row, c) for c in line_cols]

    card_rows = [r for r in range(host_row + 1, len(rows)) if cell(rows, r, slot_a).isdigit()]
    elements = []
    for i, (col, name) in enumerate(zip(line_cols, line_names)):
        role = "terminal" if i in (0, len(line_cols) - 1) else "ila"
        cards = []
        for r in card_rows:
            t = cell(rows, r, col)
            if t and t.lower() != "empty":
                cards.append({"slot": int(cell(rows, r, slot_a)), "type": t.upper()})
        elements.append({"col": col, "name": name, "role": role, "cards": cards})

    # Card function/direction. Terminal A transmits AZ, terminal Z ZA.
    assumptions.append(
        "ILA amplifier in the lower slot (2) amplifies A->Z (Bilbao->Madrid), the higher slot (7) Z->A"
    )
    assumptions.append(
        "OSC (TNCS): at an ILA the lower slot faces A and the higher slot faces Z; at a terminal the"
        " lower slot faces the line and the higher one is standby"
    )
    for i, el in enumerate(elements):
        if el["role"] == "terminal":
            tx = "AZ" if i == 0 else "ZA"
            rx = "ZA" if tx == "AZ" else "AZ"
            line_side = "Z" if i == 0 else "A"
            osc = sorted(c["slot"] for c in el["cards"] if card_function(c["type"], "terminal") == "osc")
            for c in el["cards"]:
                f = card_function(c["type"], "terminal")
                c["function"] = f
                c["direction"] = {"mux": tx, "booster": tx, "preamp": rx, "demux": rx}.get(f)
                if f == "osc":
                    c["direction"] = line_side if c["slot"] == osc[0] else None
            # Only the line-facing TNCS is on the path; a terminal amp that
            # isn't the preamp (e.g. a spare) is left off it.
        else:
            amps = sorted(c["slot"] for c in el["cards"] if card_function(c["type"], "ila") == "line_amp")
            osc = sorted(c["slot"] for c in el["cards"] if card_function(c["type"], "ila") == "osc")
            for c in el["cards"]:
                f = card_function(c["type"], "ila")
                c["function"] = f
                if f == "line_amp":
                    c["direction"] = "AZ" if c["slot"] == amps[0] else "ZA"
                elif f == "osc":
                    c["direction"] = "A" if c["slot"] == osc[0] else "Z" if c["slot"] == osc[-1] else None
            if len(amps) == 2:
                types = {c["slot"]: c["type"] for c in el["cards"]}
                if types[amps[0]] != types[amps[1]]:
                    assumptions.append(
                        f"{el['name']}: the two directions use different amplifiers"
                        f" ({types[amps[0]]} / {types[amps[1]]}) - kept as in the sheet"
                    )

    # Sites and spans: the row under the "POP" header.
    pop_row = find_row(rows, lambda r: r and r[0] == "POP")
    site_row = pop_row + 1
    pop_cols = [c for c, v in enumerate(rows[pop_row]) if v.strip() == "POP"]
    pop_a, pop_z = cell(rows, site_row, pop_cols[0]), cell(rows, site_row, pop_cols[-1])
    spans = []
    for c, v in enumerate(rows[site_row]):
        if cell(rows, pop_row, c).lower() == "fiber provider" and v.strip():
            left = max(e for e in line_cols if e < c)
            right = min(e for e in line_cols if e > c)
            a_end, b_end = cell(rows, site_row, c - 1), cell(rows, site_row, c + 1)
            spans.append(
                {
                    "from_col": left,
                    "to_col": right,
                    "provider": v.strip(),
                    "a_end": a_end,
                    "b_end": b_end,
                }
            )
    for i, s in enumerate(spans, 1):
        s["seq"] = i
        if s["a_end"] == s["b_end"]:
            assumptions.append(f"span {i}: sheet labels both ends {s['a_end']!r} - B end assumed to be 'B'")
            s["b_end"] = "B"
    site_names = {}
    for el in elements[1:-1]:
        site_names[el["name"]] = cell(rows, site_row, el["col"])
    seen = {}
    for name, site in site_names.items():
        seen.setdefault(site, []).append(name)
    for site, names in seen.items():
        if len(names) > 1:
            assumptions.append(f"site name {site!r} appears for {len(names)} ILAs ({', '.join(names)}) - check the sheet")

    # POP equipment: bands left of the first and right of the last "slot".
    def pop_side(cols):
        names = {}
        for r in range(host_row, card_rows[-1] + 1):
            for c in cols:
                v = cell(rows, r, c)
                if v and not PORT_RE.match(v) and "." in v and not v.lower().startswith(("trunk", "client")):
                    names[(r, c)] = v
        txp = [(rc, v) for rc, v in names.items() if v.lower().startswith("txp")]
        routers = sorted(((rc, v) for rc, v in names.items() if not v.lower().startswith("txp")), key=lambda x: x[0][0])
        if len(txp) != 1:
            raise SystemExit(f"expected one transponder per POP, found {[v for _, v in txp]}")
        (txp_r, txp_c), txp_name = txp[0]
        groups = []
        for gi, ((rr, rc), rname) in enumerate(routers):
            end = routers[gi + 1][0][0] if gi + 1 < len(routers) else card_rows[-1] + 1
            router_ports, clients, trunk = [], [], None
            for r in range(rr, end):
                label = cell(rows, r, txp_c)
                wave = WAVE_RE.search(label)
                for c in cols:
                    v = cell(rows, r, c)
                    if not PORT_RE.match(v):
                        continue
                    if abs(c - txp_c) == 1:
                        if wave:
                            trunk = (v, float(f"{wave.group(1)}.{wave.group(2)}"))
                        elif label.lower() == "client":
                            clients.append((r, v))
                    else:
                        router_ports.append((r, v))
            if trunk is None:
                raise SystemExit(f"{rname}: no trunk wavelength in its group of rows")
            if len(router_ports) != len(clients):
                raise SystemExit(f"{rname}: {len(router_ports)} router ports but {len(clients)} client ports")
            offsets = {cr - rr2 for (rr2, _), (cr, _) in zip(router_ports, clients)}
            if offsets != {0}:
                assumptions.append(
                    f"{rname}: router ports and transponder client ports are on different rows in the sheet"
                    f" - paired in order ({', '.join(p for _, p in router_ports)} -> {', '.join(p for _, p in clients)})"
                )
            groups.append({"router": rname, "router_ports": [p for _, p in router_ports], "clients": [p for _, p in clients], "trunk": trunk})
        return txp_name, groups

    txp_a, groups_a = pop_side(range(0, slot_a))
    txp_z, groups_z = pop_side(range(slot_z + 1, max(len(r) for r in rows)))
    if [g["trunk"][1] for g in groups_a] != [g["trunk"][1] for g in groups_z]:
        raise SystemExit("the two POPs' router groups don't use the same wavelengths in the same order")
    assumptions.append(
        "router-to-router pairing follows the sheet's row groups: "
        + "; ".join(f"{ga['router']} <-> {gz['router']} on {ga['trunk'][1]:.2f} nm" for ga, gz in zip(groups_a, groups_z))
    )

    # --- assemble the spec, applying overrides -----------------------------
    ov_dev = overrides.get("devices", {})
    ov_site = overrides.get("sites", {})
    port_prefix = overrides.get("router_port_prefix", "")

    def dev(name):
        return ov_dev.get(name, {})

    def hostname(name):
        return dev(name).get("hostname", name)

    sites, devices = [], []

    def add_site(key, default_name, kind):
        o = ov_site.get(key)
        if o is None:
            raise SystemExit(f"overrides.sites is missing {key!r} (needs code, latitude, longitude)")
        sites.append({"code": o["code"], "name": o.get("name", default_name), "kind": kind, "city": o.get("city", default_name),
                      "country": o.get("country"), "latitude": o["latitude"], "longitude": o["longitude"]})
        return o["code"]

    code_a = add_site(pop_a, pop_a, "pop")
    ila_codes = {}
    for el in elements[1:-1]:
        ila_codes[el["name"]] = add_site(el["name"], site_names[el["name"]], "ila")
    code_z = add_site(pop_z, pop_z, "pop")  # sites now run A, ILAs in order, Z

    def mgmt_ip(name):
        ip = dev(name).get("mgmt_ip")
        if not ip:
            raise SystemExit(f"overrides.devices[{name!r}] needs a mgmt_ip")
        return ip

    model = overrides.get("models", {})
    for side_code, txp, groups in ((code_a, txp_a, groups_a), (code_z, txp_z, groups_z)):
        for g in groups:
            devices.append({
                "hostname": hostname(g["router"]), "mgmt_ip": mgmt_ip(g["router"]), "role": "router",
                "model": dev(g["router"]).get("model", model.get("router")), "site": side_code,
                "ports": [{"name": port_prefix + p, "kind": "router_eth"} for p in g["router_ports"]],
            })
        ports = []
        for g in groups:
            ports.append({"name": g["trunk"][0], "kind": "trunk", "wavelength_nm": g["trunk"][1]})
            ports += [{"name": p, "kind": "client"} for p in g["clients"]]
        devices.append({
            "hostname": hostname(txp), "mgmt_ip": mgmt_ip(txp), "role": "transponder",
            "model": dev(txp).get("model", model.get("transponder")), "site": side_code, "ports": ports,
        })
    for i, el in enumerate(elements):
        site = code_a if i == 0 else code_z if i == len(elements) - 1 else ila_codes[el["name"]]
        devices.append({
            "hostname": hostname(el["name"]), "mgmt_ip": mgmt_ip(el["name"]), "role": el["role"],
            "model": dev(el["name"]).get("model", model.get("line")), "site": site,
            "cards": [{k: c[k] for k in ("slot", "type", "function", "direction")} for c in el["cards"]],
        })

    by_col = {el["col"]: el["name"] for el in elements}
    span_specs = []
    for s in spans:
        o = overrides.get("spans", {}).get(str(s["seq"]), {})
        span_specs.append({
            "seq": s["seq"], "from_device": hostname(by_col[s["from_col"]]), "to_device": hostname(by_col[s["to_col"]]),
            "provider": s["provider"], "a_end": s["a_end"], "b_end": s["b_end"],
            "length_km": o.get("length_km"), "provider_circuit_id": o.get("provider_circuit_id"),
        })
    if not any(o.get("length_km") for o in overrides.get("spans", {}).values()):
        assumptions.append("span lengths aren't in the sheet - the importer uses straight-line distance between sites")

    circuits, l3_links = [], []
    for ga, gz in zip(groups_a, groups_z):
        wl = ga["trunk"][1]
        for rp_a, cl_a, cl_z, rp_z in zip(ga["router_ports"], ga["clients"], gz["clients"], gz["router_ports"]):
            circuits.append({
                "wavelength_nm": wl,
                "a_router": hostname(ga["router"]), "a_router_port": port_prefix + rp_a,
                "a_transponder": hostname(txp_a), "a_client_port": cl_a,
                "z_transponder": hostname(txp_z), "z_client_port": cl_z,
                "z_router": hostname(gz["router"]), "z_router_port": port_prefix + rp_z,
            })
        l3_links.append({"router_a": hostname(ga["router"]), "router_z": hostname(gz["router"]),
                         **overrides.get("l3_link_defaults", {})})

    return {
        "name": overrides["name"],
        "description": overrides.get("description"),
        "assumptions": assumptions,
        "sites": sites,
        "devices": devices,
        "spans": span_specs,
        "circuits": circuits,
        "l3_links": l3_links,
    }


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    with open(sys.argv[1], newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f, delimiter=";"))
    with open(sys.argv[2]) as f:
        overrides = json.load(f)
    spec = parse(rows, overrides)
    json.dump(spec, sys.stdout, indent=2)
    sys.stdout.write("\n")
    print(f"{len(spec['devices'])} devices, {len(spec['spans'])} spans, {len(spec['circuits'])} circuits", file=sys.stderr)
    for a in spec["assumptions"]:
        print(f"ASSUMPTION: {a}", file=sys.stderr)


if __name__ == "__main__":
    main()
