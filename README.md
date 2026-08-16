# Network Control Room

A monitoring application for a telco's fleet of (Cisco) routers: it
receives real SNMP traps, classifies them into incident types (link
flapping, cold start, auth failures, BGP state changes, etc.), stores
everything in a database, and shows the fleet on a world map with live
status updates. The fleet is two-tiered — 400 telco-owned **primary**
(backbone) routers, each BGP-peered with its ~3 geographically closest
primaries for redundancy, plus 4000 last-mile **customer** CPE routers
(10 per primary, single-homed to it) — and both the BGP mesh and the
customer uplinks render as lines on the map. For actionable incidents it
also runs an auto-heal playbook — backing up the router's configuration
first, then attempting (or explicitly declining) a fix — and shows
exactly what it did.

No LLM is involved — classification and auto-heal are both deterministic,
rule-based engines (trap OID → incident type; incident type → playbook).

## Architecture

```
┌─────────────┐   SNMP traps (UDP)   ┌─────────────┐
│  simulator   │ ───────────────────▶│   backend    │
│ 400 primary  │                      │  FastAPI +   │
│ + 4000       │   REST (seed)        │  trap        │
│ customer     │ ───────────────────▶│  listener +  │
│ routers      │                      │  SQLAlchemy  │
└─────────────┘                      └──────┬──────┘
                                              │ REST + WebSocket
                                       ┌──────▼──────┐
                                       │  frontend    │
                                       │ React+Leaflet│
                                       │  (nginx)     │
                                       └─────────────┘
```

- **backend/** — FastAPI app. Runs an asyncio SNMP trap listener on UDP
  1162 (mapped to the standard trap port 162 on the host), classifies
  each trap, persists it, runs flap detection, and serves a REST API +
  a `/ws/events` WebSocket for live updates. Database access goes
  through SQLAlchemy, so switching from Postgres to SQLite/MySQL is a
  one-line `DATABASE_URL` change — no code changes, though it drops the
  PostGIS-backed nearest-router lookup (see "Switching the database").
- **simulator/** — generates the 400 primary routers spread across
  real-world cities (`generate_routers.py` / `routers_seed.json`) plus
  4000 customer CPE routers clustered near them
  (`generate_customer_routers.py` / `customer_routers_seed.json`) and a
  ~3-peer, proximity-based BGP mesh among the primaries
  (`generate_bgp_topology.py` / `bgp_topology.json`), seeds all of it
  into the backend, then
  continuously sends real SNMP traps: boot-time `coldStart`s, a rich
  event mix on primaries (link flaps, BGP/CPU/temperature/config/auth
  events), and a much quieter trickle of occasional link blips on
  customer routers.
- **frontend/** — React + Vite app with a Leaflet world map on a dark
  basemap (routers colored by status: green=up, red=down,
  yellow=flapping — see [Color reference](#color-reference); primaries
  as larger dots, customers as small dots near their primary), a
  dashboard scoped to the backbone's health, and router/incident list
  views with a primary/customer filter. Updates live over the
  WebSocket. Served by nginx, which also proxies `/api` and `/ws` to
  the backend.

## Running it

```bash
docker compose up --build
```

Then open:
- **http://localhost** — the web UI (map, dashboard, routers, incidents)
- **http://localhost:8000/docs** — the backend's interactive API docs

It takes a few seconds after startup for the simulator to seed the 4400
routers (400 primaries + 4000 customers) and start sending traps — the
map fills in and incidents start appearing shortly after
`docker compose up` finishes.

## Trap classification

`backend/app/snmp/oid_map.py` maps trap OIDs to incident types:

| Trap | Incident type |
|---|---|
| `linkDown` | `LINK_DOWN` (or `LINK_FLAP` if the interface has flapped ≥4 times in the last 10 minutes) |
| `linkUp` | `LINK_UP` (resolves any open `LINK_DOWN`/`LINK_FLAP` on that interface) |
| `coldStart` / `warmStart` | `COLD_START` / `WARM_START` |
| `authenticationFailure` | `AUTH_FAILURE` |
| BGP established/backward-transition | `BGP_STATE_CHANGE` |
| IS-IS adjacency down/up (on a bundle member) | `ISIS_NBR_DOWN` / `ISIS_NBR_UP` |
| Cisco CPU threshold | `HIGH_CPU` |
| Cisco environment/temperature | `ENV_ALARM` |
| Cisco config-change | `CONFIG_CHANGE` |
| anything else | `UNKNOWN` |

Flap detection (`backend/app/snmp/flapping.py`) keeps a rolling window
per `(router, interface)`; once enough linkUp/linkDown transitions occur
in that window, the router is marked `flapping` on the map instead of
just `down`/`up`. Window size and threshold are configurable via the
`FLAP_WINDOW_SECONDS` / `FLAP_TRANSITION_THRESHOLD` env vars.

Since the simulator runs as a single container standing in for 4400
different routers, every trap carries the simulated router's management
IP and interface name as extra varbinds (private OIDs under
`1.3.6.1.4.1.9.9.9999.*`) so the listener knows which router "sent" it.
Real routers, each with their own IP, are identified by UDP source
address instead — no simulator-specific varbinds required.

## Auto-heal

`backend/app/remediation/engine.py` runs a rule-based playbook the first
time an actionable incident opens (repeat traps against an already-open
incident don't re-trigger it):

1. **Always back up first** (`backup_router_config`) — snapshots the
   router's running-config *before* anything else happens, so any change
   can be diffed or rolled back later. Every remediation action is linked
   to the backup that preceded it.
2. **Then act, or explicitly decline to act**, per incident type:

   | Incident type | Action | Reasoning |
   |---|---|---|
   | `LINK_DOWN` | `INTERFACE_BOUNCE` | shutdown/no-shutdown the interface |
   | `LINK_FLAP` | `CLEAR_COUNTERS` | clear counters + dampen the flapping interface |
   | `HIGH_CPU` | `PROCESS_RESTART` | restart the offending process |
   | `BGP_STATE_CHANGE` | `BGP_NEIGHBOR_RESET` | soft-reset the BGP session |
   | `AUTH_FAILURE` | `NOTIFY_ONLY` (skipped) | possible security event — flagged for a human, never auto-remediated |
   | `ENV_ALARM` | `NOTIFY_ONLY` (skipped) | hardware/environmental — can't be fixed by a config change |
   | `CONFIG_CHANGE` | `NOTIFY_ONLY` (skipped) | config was already changed out-of-band; backup kept for diffing |

   `LINK_UP`, `COLD_START`/`WARM_START`, and `UNKNOWN` are "good news" or
   uninterpretable events and never enter the auto-heal pipeline at all.

Every backup and every action (success, failure, or skip, with a log) is
persisted and shown in the UI: the **Incidents** tab has an "Auto-heal"
column (click a badge for the full log), and each router's detail panel
has "Auto-heal history" and "Configuration backups" sections. The same
data is available over the API: `GET /api/incidents/{id}/remediation`,
`GET /api/routers/{id}/remediation`, `GET /api/routers/{id}/backups`,
`GET /api/backups/{id}`.

These are simulated routers with no real management plane, so the backup
and the remediation "action" are synthetic — a real deployment would
replace `_simulated_config_snapshot` and the action execution in
`engine.py` with actual SSH/NETCONF/RESTCONF calls against the device,
while keeping the same backup-then-act-then-record flow.

**Manual resolution.** `AUTH_FAILURE`, `ENV_ALARM`, and `CONFIG_CHANGE`
incidents get `NOTIFY_ONLY` and stay **open** - nothing auto-remediates
them, so they sit there until a human closes them. The Incidents tab's
"⚠ Needs manual review" filter pulls up exactly these; every open
incident has a "Resolve" link, and a "Resolve N open (shown)" button
above the table bulk-resolves whatever's currently open in the filtered
view (with a confirmation prompt). Resolving sets `resolved_manually`
so the UI can distinguish "a human closed this" from "a linkUp/
bgpEstablished trap closed this" - both look identical otherwise
(`status: resolved`). Backed by `POST /api/incidents/{id}/resolve` and
`POST /api/incidents/resolve` (`{"incident_ids": [...]}`), both broadcast
over the WebSocket so every connected tab updates immediately, not just
the one that clicked.

**When auto-heal doesn't fix it, the map tells you.** If a remediation
action comes back `failed` and the incident is still open, that router
is "needs attention" (`RouterOut.needs_attention`,
`app/remediation/engine.py:needs_attention_router_ids`) and its marker
blinks on the map — alternating between a bright orange ring at full
size/opacity and a dim, smaller one, about twice a second — with a
"⚠ auto-heal failed — needs attention" line in its popup. This needs no
separate "resolved" bookkeeping: `needs_attention` is recomputed fresh on
every trap from current DB state (does this router have an *open*
incident whose remediation failed?), so the instant the router actually
recovers (a real `linkUp` resolves the incident, independent of whether
the earlier auto-heal attempt succeeded) it drops out of the query and
the blink stops on its own.

## Two-tier topology: primaries, customers, and BGP

- **Primary routers** (`Router.router_type = "primary"`) are the 400
  telco-owned backbone routers. Each is BGP-peered with exactly 3 others
  (600 peerings total) — a redundant mesh, not a full mesh: enough that
  no primary is single-homed, without every router talking to every
  other one. Peers are chosen by geographic proximity, not at random:
  `simulator/generate_bgp_topology.py` computes haversine distance
  between every pair of primaries and greedily peers each with its
  nearest available neighbors (median peer distance ~340km — e.g.
  Amsterdam↔Rotterdam, Vienna↔Budapest), then runs a 2-opt cleanup pass to
  fix the handful of "leftover" pairings a pure greedy match can strand
  far apart. A few genuinely long links remain where a primary simply
  has no close neighbors to exhaust its degree-3 requirement with (e.g.
  Reykjavik, whose nearest primaries are all 1300km+ away in either
  direction, ends up peering across the Atlantic with Halifax — an
  actual transatlantic cable landing point in real life) — that's a
  structural property of an exact-degree mesh over unevenly distributed
  cities, not an algorithm bug. Generated once into
  `simulator/bgp_topology.json`, seeded via `POST /api/bgp/seed`.
- **Customer routers** (`router_type = "customer"`) are 4000 last-mile
  CPE routers, 10 per primary, scattered 30-250km from their primary
  (uniformly by *area*, via `random_offset` sampling angle + radius
  rather than independent lat/lon jitter) so they trace out the region
  the primary actually serves instead of clustering on top of it, and
  single-homed to it via `parent_router_id` — they are *not* part of the
  BGP mesh. Generated by `simulator/generate_customer_routers.py` into
  `customer_routers_seed.json`; seeded after the primaries (via the same
  `POST /api/routers/seed`, since a customer's `parent_mgmt_ip` must
  resolve to an already-existing primary).
- The **Map** tab draws both link types: BGP peerings as solid lines
  between primaries (blue = established, red = down), and customer
  uplinks as dashed lines from each customer to its primary — neutral
  slate-gray while healthy, turning vivid red/amber the moment that
  customer has an actual down/flapping incident (deliberately a
  different palette from the router markers themselves, since a plain
  "up"-green line all but disappears against green/tan map tiles). Two
  checkboxes ("Show BGP links (600)", "Show customer routers (4000)")
  let you declutter either layer. Primaries render as larger dots with a
  dark outline ring, customers as smaller (but still clearly visible,
  fully-opaque, outlined) dots near them.
- BGP traps sent by the simulator carry the real neighbor's management
  IP as an extra varbind (`BGP_PEER_OID`, `1.3.6.1.4.1.9.9.9999.1.3`), so
  `bgpEstablished`/`bgpBackwardTransition` traps update that *specific*
  peering's status (`GET /api/bgp/peerings`) instead of just logging a
  generic incident — the line flips color live over the WebSocket, same
  as router status. `BGP_STATE_CHANGE` incidents still go through the
  same classification and auto-heal pipeline as any other incident type
  (backup, then `BGP_NEIGHBOR_RESET`) - only primaries generate these,
  since customer CPE doesn't run BGP.
- Customer routers get a much quieter trap mix than primaries (occasional
  link blips, rare auth failures - no BGP/CPU/env/config traps), and the
  dashboard/`GET /api/stats/summary` are scoped to primaries only, so
  4000 customer routers' worth of last-mile noise doesn't drown out
  backbone health. The **Routers** tab defaults to a "Primary routers"
  filter for the same reason, with "Customer routers" / "All" available.
- **Every primary has one dedicated connection per real link** — an 80Gb
  bundle (`Port-channel{N}`, 2x40G `FortyGigE0/0/{2N,2N+1}` members) per
  BGP peer, plus a 1Gb `GigabitEthernet0/N` per customer router (17
  interfaces for a typical 3-peer, 10-customer primary), and this is what
  actually shows up everywhere: traps name the specific bundle/member/
  customer interface for whichever connection is affected
  (`simulator/trap_simulator.py:build_bgp_bundles` /
  `build_customer_facing_interfaces`), and each config backup
  (`backend/app/remediation/engine.py:_simulated_config_snapshot`) lists
  those same interfaces (with `channel-group` bundling for BGP peers) with
  a description naming the real peer/customer hostname — built fresh from
  the live topology at backup time, not a static template. Both sides agree
  on the interface numbering (BGP peers numbered in mgmt-IP-sort order,
  customer interfaces numbered from the customer's own IP octet) purely by
  using the same deterministic derivation independently
  (`backend/app/bundles.py` on the backend side) — no interface-assignment
  table needed. Customer CPEs, being single-homed, just have the one
  `GigabitEthernet0/0` uplink.
- **Every BGP peering link runs over real Single Mode Fiber (SMF)**, and
  `simulator/generate_bgp_topology.py` records each link's actual
  great-circle distance (`distance_km`) plus how many regenerators an
  unamplified long-haul SMF run that length would need at ~80km spacing
  (`repeater_count`) — both computed once from real coordinates, not
  simulated. `POST /api/bgp/seed` persists them onto `BgpPeering`
  (`backend/app/models.py`), `GET /api/bgp/peerings` exposes them, the
  Router detail panel's BGP peers table shows them per peer, and each
  primary's simulated config backup notes them on the corresponding
  Port-channel's description line (`backend/app/remediation/engine.py:_simulated_config_snapshot`).
  The ~80km repeater spacing only applies to terrestrial spans, though:
  `generate_bgp_topology.py:is_oceanic` samples 19 interior points along a
  link's straight-line path (the same path the map actually draws) against
  the same land/sea raster `generate_routers.py` uses to place routers, and
  a link that's over open water for most of its length (a real transoceanic
  cable, e.g. Reykjavik↔Halifax) gets `repeater_count = 0` instead - a
  submarine cable's repeaters are pressure-housed units spliced into the
  cable itself, not the roadside regenerator huts this model simulates for
  terrestrial fiber. Both endpoints are always real city sites and always
  on land, so only interior samples are checked; because it follows the
  same straight-line path the map draws rather than an actual cable route,
  a handful of short coastal city pairs whose straight line happens to cut
  across a bay (e.g. Los Angeles↔San Diego) get swept in too - a
  consequence of the same straight-line-not-geodesic simplification the map
  itself already documents, not a separate bug.
- **Interface bundles (Port-channel/LACP-style) provide true redundancy**
  on the BGP mesh: IS-IS — the IGP that manages physical adjacency
  fleet-wide — tracks each bundle member's adjacency independently
  (`Interface.isis_adjacency_up`, `backend/app/bundles.py`), and a bundle
  (`InterfaceBundle`) only goes down once *every* member has lost
  adjacency; a single member flapping is a low-impact `ISIS_NBR_DOWN`/
  `ISIS_NBR_UP` incident that never touches router status or the BGP
  peering. When a bundle *does* fully fail, that cascades into the same
  `BgpPeering.status`/reroute-path machinery a direct BGP flap already
  uses (`backend/app/snmp/classifier.py`) — deliberately without a second
  `BGP_STATE_CHANGE` incident or a `BGP_NEIGHBOR_RESET` attempt, since a
  soft reset can't fix a transport that's actually gone. The simulator
  demonstrates three distinct IS-IS link-failure scenarios:
  `random_event_loop` flips one random member per peer as one-off routine
  noise across the whole mesh; `FLAPPING_BUNDLE_COUNT` (default 3) specific
  bundles get a chronically-flapping member instead
  (`bundle_flap_loop`, same burst/quiet cadence as the router-level
  `flap_loop`) - a persistently-demoable "degraded but redundant" bundle;
  and `scheduled_bundle_failure_loop` takes every member of a random bundle
  down together at a bounded cadence (`BUNDLE_FAILURE_INTERVAL_SECONDS`,
  default 900s) for the "redundancy exhausted" case. All three leave
  router status and the peering itself alone unless every member is
  actually down. Bundle status, member counts, and live aggregate
  bandwidth show up in each primary's BGP peers panel
  (`GET /api/bgp/peerings`).
- **IS-IS itself runs only on the bundles** (plus each primary's Loopback0,
  so it's reachable): every primary's simulated config carries a single
  level-2-only `router isis BACKBONE` process with a per-router NET
  (`backend/app/bundles.py:isis_net`, also shown in the UI's router detail
  panel), and each Port-channel bundle interface (not its individual
  members, which are pure L2 channel-group members) gets
  `ip router isis BACKBONE` / `isis network point-to-point` / a metric.
  This is IS-IS's actual job here: providing the underlying reachability
  that the `router bgp`/`neighbor ... remote-as` eBGP-over-loopback
  sessions ride on top of — consistent with the per-bundle-member adjacency
  tracking above. Customer-facing ports and CPE don't run it.

Links are drawn as straight lines between lat/lon, not curved
geodesics, so a few long intercontinental peerings will visually cut
across the map rather than following a greatcircle path — a
deliberate simplification, not a bug.

## Color reference

The UI shares one status vocabulary — green/red/yellow/gray — applied
consistently across map markers, map lines, and table badges, plus a
small set of colors reserved for things that aren't a health status.

**Status colors** (routers, incidents, badges):

| Meaning | Color | Hex | Where it shows up |
|---|---|---|---|
| Up / resolved | green (blue for healthy customer CPE) | `#22c55e` marker · `#4ade80` badge text on `#1b3d2b`; healthy customer routers use `#3b82f6` instead, to set the two router tiers apart on the map | router markers; `badge.up` / `badge.resolved` |
| Down / open | red | `#ef4444` marker/line · `#f87171` badge text on `#3d1b1b` | router markers, BGP/customer lines; `badge.down` / `badge.open` |
| Flapping | yellow/amber | `#facc15` marker/badge on `#3d341b` · `#f59e0b` for customer uplink lines | router markers; `badge.flapping` |
| Unknown / no auto-heal action | slate gray | `#9aa4bf` marker/badge on `#26314d` | router markers; `badge.unknown` |

**Map-only colors** (`frontend/src/components/MapView.jsx`):

| Element | Color | Hex |
|---|---|---|
| Basemap | CartoDB "Dark Matter" dark tiles | — (no green/tan land colors to clash with the line/marker palette) |
| Healthy customer uplink line | slate | `#64748b` |
| BGP peering — established | blue | `#3b82f6` |
| BGP peering — down | red | `#ef4444` (same as router "down") |
| Active reroute path (animated dashes) | purple | `#a855f7` — deliberately distinct from both established-blue and down-red, so "traffic detouring" reads differently from either steady state |
| Marker outline — normal | near-black | `#0b0f1a` |
| Marker outline — needs attention (blinking) | orange | `#f97316` |

The customer-uplink and BGP-line palettes are intentionally *not* the
same as the marker fill colors: a plain "up"-green line would nearly
disappear against tile-map greens/tans, so healthy links use neutral
slate instead and only turn a loud color when something's actually
wrong.

**General UI theme** (`frontend/src/index.css`):

| Element | Hex |
|---|---|
| Page background | `#0f1420` |
| Panel / header / card background | `#161c2c` |
| Borders | `#262f45` |
| Primary text | `#e6e9f0` |
| Secondary/muted text | `#9aa4bf` |
| Links & accents | `#60a5fa` |
| Dashboard bar-chart fill | gradient `#60a5fa` → `#818cf8` |
| Destructive action ("Reset ALL open incidents") | `#7c2d12` fill / `#9a3412` border |

## HTTPS / Let's Encrypt

`frontend/nginx.conf` serves the app over HTTPS on port 443 (redirecting
plain HTTP to it) using a Let's Encrypt certificate for
`controlroom.point2point.org.uk`. This needs two things before it'll work:

1. A DNS **A record** for that domain pointing at the server's public IP.
2. Ports **80** and **443** reachable from the internet (Let's Encrypt's
   HTTP-01 challenge, which `scripts/init-letsencrypt.sh` uses, connects to
   port 80).

To get the first certificate, run once from the repo root, **before** (or
instead of) `docker compose up`:

```bash
./scripts/init-letsencrypt.sh
```

This brings up `backend` + `frontend` itself (nginx has to be running to
answer the ACME challenge), so nothing else needs to be started first. It's
also safe to re-run — if a real certificate already exists it just makes
sure the stack is up and exits. Set `STAGING=1` (in `.env` or the
environment) to use Let's Encrypt's staging server while testing, which has
no rate limits but issues untrusted certs.

Domain and contact email come from `DOMAIN` / `LETSENCRYPT_EMAIL` in `.env`
(see `.env.example`); changing the domain also means updating the two
`server_name`/`ssl_certificate*` lines in `frontend/nginx.conf` to match.

**Renewal is automatic** as long as the stack is running:
- The `certbot` service (docker-compose.yml) loops `certbot renew` every
  12h, which only actually renews within ~30 days of expiry.
- `frontend/start.sh` reloads nginx every 6h, so a renewed certificate gets
  picked up without needing a container restart.

## Switching the database

Default is Postgres with the [PostGIS](https://postgis.net/) extension
(`postgis/postgis` image, backed by the `postgres-data` Docker volume) —
`docker compose up --build` brings it up automatically, nothing else to
configure. It backs one spatial feature: `Router.location`, a `geography`
point kept in sync with each router's `latitude`/`longitude` (see
`app/models.py`), used by `GET /api/routers/{id}/nearest` to find the
geographically closest other primaries via PostGIS `ST_Distance` — a
candidate list of backup/reroute sites for an operator looking at a
degraded router, independent of the router's actual BGP peerings.

To use SQLite or MySQL instead (no code changes, but `/nearest` then
returns `501` and `Router.location` doesn't exist):

1. Copy `.env.example` to `.env` and set `DATABASE_URL` to the SQLite or
   MySQL connection string shown there (commented out by default).
2. For MySQL, also uncomment the `mysql` service block (and its volume)
   in `docker-compose.yml`, and add `depends_on: [mysql]` under the
   `backend` service; for SQLite, no service is needed. Either way you
   can remove the `postgres` service's `depends_on` under `backend`.
3. `docker compose up --build` — the backend's SQLAlchemy models and API
   are otherwise unchanged; only the connection string differs. Drivers
   for both (`psycopg2-binary`, `pymysql`) are already in
   `backend/requirements.txt`.

## Regenerating the fleet

`generate_routers.py` and `generate_customer_routers.py` both place their
points with a land/sea check (`land.py`, backed by `global-land-mask`) so
routers never end up sitting in the ocean after their random jitter, and
`generate_bgp_topology.py` uses the same check to tell submarine links
apart from terrestrial ones (see `is_oceanic` above) - that needs one
extra dev-only dependency, not part of the runtime image:

```bash
cd simulator
pip install -r requirements-dev.txt    # global-land-mask + numpy, generator-only
```

Then, in this order - each step reads the previous step's output file:

```bash
python3 generate_routers.py            # rewrites routers_seed.json (400 primaries)
python3 generate_bgp_topology.py       # rewrites bgp_topology.json (~3 peers/primary, reads routers_seed.json)
python3 generate_customer_routers.py   # rewrites customer_routers_seed.json (10 customers/primary, reads routers_seed.json)
```

## Configuration reference

| Env var | Where | Default | Purpose |
|---|---|---|---|
| `DATABASE_URL` | backend | `postgresql+psycopg2://ncr:ncr@postgres:5432/ncr` | SQLAlchemy connection string |
| `TRAP_PORT` | backend | `1162` | UDP port the trap listener binds |
| `FLAP_WINDOW_SECONDS` | backend | `600` | Sliding window for flap detection |
| `FLAP_TRANSITION_THRESHOLD` | backend | `4` | Transitions within the window to call it "flapping" |
| `FLAPPING_ROUTER_COUNT` / `DOWN_ROUTER_COUNT` | simulator | `5` / `3` | How many primaries misbehave |
| `FLAPPING_BUNDLE_COUNT` | simulator | `3` | How many BGP-peering bundles get a chronically-flapping member (redundancy always absorbs it) |
| `TICK_MIN_SECONDS` / `TICK_MAX_SECONDS` | simulator | `3` / `8` | Pace of the primary event stream |
| `CUSTOMER_TICK_MIN_SECONDS` / `CUSTOMER_TICK_MAX_SECONDS` | simulator | `20` / `45` | Pace of the (much quieter) customer event stream |
| `BGP_INCIDENT_INTERVAL_SECONDS` | simulator | `600` | Cadence of a guaranteed logical BGP flap (session-level, bundle unaffected) |
| `BUNDLE_FAILURE_INTERVAL_SECONDS` | simulator | `900` | Cadence of a guaranteed full bundle failure (every member down — redundancy exhausted, peering actually drops) |
