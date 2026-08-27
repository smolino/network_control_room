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

Alarms are layered **L1 (physical/optical) vs. L3 (control-plane)**, and a
fiber cut on the physical layer is correlated to the router-side symptoms
it causes — the downstream interface flaps are suppressed as
*symptomatic* instead of being treated as independent problems, and never
get their own auto-heal attempt. That correlation runs through a real
event pipeline: mediation publishes to **Kafka**, a normalization service
maps vendor OIDs to a common alarm model, and a correlation service
traverses a **Neo4j** topology graph (which physical fiber span a router
interface is actually carried over) to find the root cause before writing
the final incident state to Postgres. See
["Alarm pipeline: Kafka + Neo4j"](#alarm-pipeline-kafka--neo4j) and
["L1/L3 layering and root-cause correlation"](#l1l3-layering-and-root-cause-correlation)
below.

No LLM is involved anywhere in this pipeline — normalization, correlation,
and auto-heal are all deterministic, rule-based engines (trap OID →
incident type; topology graph → root cause; incident type → playbook).

## Architecture

```
┌─────────────┐  SNMP traps (UDP)  ┌───────────────────────────┐
│  simulator   │───────────────────▶│  backend (mediation)      │
│ 400 primary  │                    │  trap listener +          │
│ + 4000       │  REST (seed)       │  fiber-fault generator    │
│ customer     │───────────────────▶│  — pure Kafka producers   │
│ routers      │                    └────────────┬──────────────┘
└─────────────┘                                  │ produce
                                                  ▼
                                     Kafka topic: raw-alarms
                                                  │ consume
                                                  ▼
                                     ┌──────────────────────┐
                                     │  normalizer            │  OID → Common
                                     │  (own container)       │  Alarm Model
                                     └────────────┬────────────┘
                                                  │ produce
                                                  ▼
                                     Kafka topic: norm-alarms
                                                  │ consume
                                                  ▼
                       Neo4j ◀── read ── ┌──────────────────────┐
                  (topology graph:       │  correlator            │  enrichment +
                   Router/Interface/     │  (own container)       │  root-cause
                   FiberSpan,             └────────────┬────────────┘  correlation
                   SUPPORTED_BY)                       │ write            │ produce
                                                  ┌─────▼─────┐            ▼
                                                  │ Postgres   │  Kafka topic:
                                                  │ alarm store│  incident-events
                                                  │ (+PostGIS) │            │ consume
                                                  └─────┬─────┘            ▼
                                                        │ REST   ┌──────────────────┐
                                                        │        │ backend (ws relay)│
                                                        │        └─────────┬─────────┘
                                                        │                  │ WebSocket
                                                        ▼                  ▼
                                                 ┌──────────────────────────┐
                                                 │  frontend (React+Leaflet, │
                                                 │  served by nginx)         │
                                                 └──────────────────────────┘
```

- **backend/** — FastAPI app. Runs an asyncio SNMP trap listener on UDP
  1162 (mapped to the standard trap port 162 on the host) and the
  fiber-fault generator; both are pure Kafka *producers* now — they
  publish to `raw-alarms` and never touch the database directly (see
  "Alarm pipeline" below). The same process also runs a background
  thread that relays the pipeline's final `incident-events` topic onto
  the `/ws/events` WebSocket, and serves the REST API. Database access
  goes through SQLAlchemy, so switching from Postgres to SQLite/MySQL is
  a one-line `DATABASE_URL` change — no code changes, though it drops the
  PostGIS-backed nearest-router lookup (see "Switching the database").
- **normalizer/** and **correlator/** (`backend/app/streaming/`, run as
  their own containers off the same backend image) — the rest of the
  alarm pipeline: OID → Common Alarm Model normalization, then
  enrichment + Neo4j-backed root-cause correlation + the actual Postgres
  write. See "Alarm pipeline: Kafka + Neo4j" below.
- **kafka** / **neo4j** — the message bus between mediation and the two
  services above, and the static topology graph the correlator traverses
  for root-cause lookups, respectively. Neither holds mutable alarm
  state — that's Postgres's job throughout.
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
- **frontend/** — React + Vite app with a Leaflet world map on an
  OpenStreetMap basemap (routers colored by status: green=up, red=down,
  yellow=flapping — see [Color reference](#color-reference); primaries
  as larger dots, customers as small dots near their primary), an L1/L3
  layer toggle, a dashboard scoped to the backbone's health, and
  router/incident list views with a primary/customer filter. Updates live
  over the WebSocket. Served by nginx, which also proxies `/api` and
  `/ws` to the backend.

## Running it

```bash
docker compose up --build
```

Then open:
- **http://localhost** — the web UI (map, dashboard, routers, incidents)
- **http://localhost:8000/docs** — the backend's interactive API docs
- **http://localhost:7474** — the Neo4j browser, useful for poking at the
  topology graph directly (e.g. `MATCH (s:FiberSpan) RETURN count(s)`);
  login is `neo4j` / the value of `NEO4J_PASSWORD` (`ncrpassword` by
  default — see "Configuration reference")

`backend` now waits on `kafka`, `neo4j`, and `postgres` all being healthy
before it starts, so first boot takes a bit longer than before — Kafka
and Neo4j both need a few seconds to finish their own internal startup.
It takes a few seconds after that for the simulator to seed the 4400
routers (400 primaries + 4000 customers) and start sending traps — the
map fills in and incidents start appearing shortly after
`docker compose up` finishes. If you ever see `backend` (or
`normalizer`/`correlator`) briefly log Kafka connection retries on a cold
start, that's expected — they retry with backoff until the broker's ready
rather than crash-looping.

## Deploying to Kubernetes

`k8s/` has a plain-manifest (no Helm) deployment of the same stack, split
across the same services as `docker-compose.yml` — Postgres, Kafka, Neo4j,
`backend`, `normalizer`, `correlator`, `simulator`, `frontend` — into a
dedicated `ncr` namespace. It expects prebuilt images already published to a
registry rather than building from source in-cluster:

```bash
./scripts/build-and-push.sh          # builds + pushes backend/frontend/simulator
                                      # to docker.io/mescalo/network-control-room-*:latest
kubectl apply -f k8s/
```

`build-and-push.sh` requires `docker login docker.io` first, and always
targets `linux/AMD64` regardless of the host architecture (`frontend/
Dockerfile`'s build stage is `--platform=$BUILDPLATFORM` so it still cross-
builds correctly from an Apple Silicon host); override the tag with
`TAG=v1.2.3 ./scripts/build-and-push.sh`. If `docker.io/mescalo` isn't a
public repo you can pull from, create an `imagePullSecrets` credential first
— see the comment in `k8s/01-secret.yaml`.

A few things differ from the compose setup:
- **Config** is split the same way as the env vars documented below:
  secrets (DB/Neo4j credentials) in `k8s/01-secret.yaml`, everything else in
  `k8s/02-configmap.yaml` — both use the same default values as
  `docker-compose.yml`/`.env.example`, so change them there before applying
  outside a throwaway/demo cluster.
- **Frontend TLS**: the `frontend` image's baked-in nginx config is HTTPS-
  only and hardcoded to `controlroom.point2point.org.uk` with Let's Encrypt
  certs from the `certbot` compose service, neither of which exists
  in-cluster. `k8s/10-frontend.yaml` mounts a ConfigMap that overrides
  `/etc/nginx/conf.d/default.conf` at runtime with a plain-HTTP config
  (same `/api`, `/ws` proxy routes) instead of building a separate k8s-only
  image — add TLS back later via an Ingress + cert-manager without touching
  the image.
- **Access**: `frontend`'s Service is `NodePort` (`kubectl get svc -n ncr
  frontend` to see which port); there's no Ingress in these manifests.

## Trap classification

`backend/app/snmp/oid_map.py` maps trap OIDs to incident types, and
`app/models.py:INCIDENT_LAYER` maps incident types to a Common-Alarm-
Model-style **layer** (`L1` physical/optical vs. `L3` control-plane) —
everything defaults to `L3` except the genuinely physical-layer types:

| Trap | Incident type | Layer |
|---|---|---|
| `linkDown` | `LINK_DOWN` (or `LINK_FLAP` if the interface has flapped ≥4 times in the last 10 minutes) | L3 |
| `linkUp` | `LINK_UP` (resolves any open `LINK_DOWN`/`LINK_FLAP` on that interface) | L3 |
| `coldStart` / `warmStart` | `COLD_START` / `WARM_START` | L3 |
| `authenticationFailure` | `AUTH_FAILURE` | L3 |
| BGP established/backward-transition | `BGP_STATE_CHANGE` | L3 |
| IS-IS adjacency down/up (on a bundle member) | `ISIS_NBR_DOWN` / `ISIS_NBR_UP` | L3 |
| BFD session down | `BFD_SESSION_DOWN` | L3 |
| Cisco CPU threshold | `HIGH_CPU` | L3 |
| Cisco memory-pool-low | `HIGH_MEMORY` | L3 |
| Cisco config-change | `CONFIG_CHANGE` | L3 |
| Cisco environment/temperature | `ENV_ALARM` | L3 |
| Cisco fan / redundant-supply failure | `FAN_FAILURE` / `PSU_FAILURE` | **L1** |
| Cisco optical Rx-power threshold, or the fiber-fault generator's `opticalLossOfSignal`/`opticalSignalRestored` (`FIBER_CUT_OID`/`FIBER_CUT_CLEAR_OID`, synthetic — see below) | `OPTICAL_ALARM` | **L1** |
| anything else | `UNKNOWN` | L3 |

Flap detection (`backend/app/snmp/flapping.py`) keeps a rolling window
per `(router, interface)`; once enough linkUp/linkDown transitions occur
in that window, the router is marked `flapping` on the map instead of
just `down`/`up`. Window size and threshold are configurable via the
`FLAP_WINDOW_SECONDS` / `FLAP_TRANSITION_THRESHOLD` env vars.

Since the simulator runs as a single container standing in for 4400
different routers, every trap carries the simulated router's management
IP and interface name as extra varbinds (private OIDs under
`1.3.6.1.4.1.9.9.9999.*`) so the pipeline knows which router "sent" it.
Real routers, each with their own IP, are identified by UDP source
address instead — no simulator-specific varbinds required.

## Alarm pipeline: Kafka + Neo4j

Every trap — real or synthetic — flows through the same four-stage
pipeline rather than being handled in one function call in the request
path:

1. **Mediation** (`backend/app/snmp/trap_listener.py`,
   `backend/app/fiber_faults.py`) decodes/generates an event and
   publishes it to the `raw-alarms` Kafka topic (`backend/app/streaming/
   producer.py`). This is the *only* thing either module does now —
   neither touches the database at all.
2. **Normalization** (`backend/app/streaming/normalizer.py`, its own
   container) consumes `raw-alarms`, maps the trap OID to Common-Alarm-
   Model fields (`trap_name`/`incident_type`/`severity`/`layer`, via the
   same `oid_map.py`/`INCIDENT_LAYER` tables above) with **no database or
   graph access at all**, and republishes to `norm-alarms`. Onboarding a
   new vendor's MIB is purely "add OID mappings here" — never a
   schema/infra change.
3. **Enrichment + root-cause correlation** (`backend/app/streaming/
   correlator.py`, its own container) consumes `norm-alarms` and hands
   each message to `app/snmp/classifier.py:classify_and_store` — the same
   function that used to run synchronously in the trap listener's request
   path before this pipeline existed. It looks up the router, persists
   the `TrapEvent`/`Incident` rows (Postgres is the **alarm store**
   throughout — Neo4j never holds mutable state), runs flap/bundle/BGP-
   peering updates, and calls `app/correlation.py` for root-cause
   correlation (below). The final WS-ready payload is published to
   `incident-events`.
4. **Delivery**: a background thread inside `backend`
   (`app/streaming/ws_relay.py`) consumes `incident-events` and calls the
   same `manager.broadcast()` the WebSocket already used — the frontend
   needed zero changes for any of this.

**Why a real message bus matters here**, not just as architecture
cosplay: if `correlator` is down (a deploy, a crash), `raw-alarms`/
`norm-alarms` keep buffering in Kafka instead of alarms being silently
dropped, and it drains the backlog from its last committed offset the
moment it's back — no gap. Try it: `docker compose stop correlator`,
wait through a fiber-fault cycle (see below), `docker compose start
correlator`, and watch it catch up.

**The topology graph** (Neo4j, `backend/app/topology_graph.py`) is the
piece a purely-relational version of this app never had: an explicit
`(Interface)-[:SUPPORTED_BY]->(FiberSpan)` edge for "this L3 interface is
physically carried over this fiber span," plus `(Router)-[:PEERS_WITH]-
(Router)`. It's written once, in `api/bgp.py:seed_peerings`, right after
the same topology is written to Postgres — and it only ever holds
*structure*. `app/correlation.py` queries it to find which peering(s) a
specific interface (or, when an incident carries no specific interface,
the whole router) depends on, then queries **Postgres** for whether any
of those peerings actually has an open L1 incident right now. Structure
lives in the graph; state lives in the alarm store — never duplicated
into both.

**Deliberately not Flink.** The design this pipeline follows lists "Kafka
Streams, Flink, or even a Python consumer with a sliding window" as
equivalent options for the stream-processing step — `normalizer` and
`correlator` are the latter: plain blocking Kafka consumers, not a Flink
job on a JobManager/TaskManager cluster. At this app's alarm volume, a
Flink cluster would add real operational weight (job submission,
checkpointing, cluster topology) without a throughput problem to justify
it; the same windowed-correlation logic runs identically either way.

## L1/L3 layering and root-cause correlation

Every incident carries a `layer` (`L1` or `L3`, see the trap
classification table above), and `Incident.root_cause_incident_id` links
an incident to whichever open **L1** incident on the same fiber link
caused it — this is what turns a fiber cut into a single visible root
cause instead of a wall of unrelated-looking router alarms.

**The fiber-cut scenario** (`backend/app/fiber_faults.py`) is the
concrete, always-running demonstration: every 15–40s it picks an
established BGP peering with at least 2 SMF repeaters and knocks out one
interior span for 10 seconds. Unlike the map-only "faulty segment"
overlay this started as, it now raises a *real* chain of events through
the pipeline above:

1. An `OPTICAL_ALARM` incident opens, tied to the peering via
   `Incident.peering_id` (not a router — a fiber cut isn't any one
   router's problem). It goes through the normal auto-heal pipeline like
   any other real optical alarm (`NOTIFY_ONLY` — a fiber cut can't be
   fixed by a config push).
2. One bundle member interface on **each side** of that peering loses IS-
   IS adjacency (`ISIS_NBR_DOWN`) — a real, partial degradation, not the
   whole bundle, since real fiber typically carries one wavelength/member
   per physical strand.
3. `app/correlation.py:try_link_root_cause` runs for each of those two new
   `ISIS_NBR_DOWN` incidents: it asks Neo4j which peering(s) that specific
   interface's fiber path is `SUPPORTED_BY`, then checks Postgres for an
   open L1 incident on that peering within the last 60s
   (`CORRELATION_WINDOW_SECONDS`). Finding the `OPTICAL_ALARM` from step
   1, it sets `root_cause_incident_id` — marking both as **symptomatic**.
4. Symptomatic incidents **never get their own auto-heal attempt**
   (`classifier.py` skips `maybe_remediate` whenever
   `root_cause_incident_id` is set) — bouncing a downstream interface
   wouldn't fix an upstream fiber cut, and it avoids a redundant config
   backup for every member a single fault happens to touch.
5. After 10s, the reverse sequence resolves everything: both members'
   `ISIS_NBR_UP`, then the `OPTICAL_ALARM` itself (via a synthetic
   `FIBER_CUT_CLEAR_OID` event, resolved by peering rather than by
   router+interface — see `flapping.py:resolve_incident_for_peering`).

**In the UI:**
- **Map** — an "L1 physical" / "L3 logical" layer toggle shows/hides the
  fiber-span repeater dots + fault-segment line (L1) independently of
  router markers + BGP/customer/reroute lines (L3). A peering with a
  currently-open L1 incident gets a pulsing fault-segment line (reusing
  the same blink timer as `needs_attention`), and the two routers on
  either end get a dashed amber ring (`#fb923c`) distinct from the solid
  orange `needs_attention` ring — "symptomatic of an upstream fault," not
  "this router's own remediation failed."
- **Incidents tab** — a Layer column, an "All/L1/L3" filter, and a "Hide
  symptomatic" checkbox (checked by default) that filters out anything
  with `root_cause_incident_id` set — matching the doc this app follows:
  root cause stays visible, symptomatic alarms are suppressed from the
  top-level view but never deleted. Unchecking it reveals a
  "↳ symptomatic of #N (OPTICAL_ALARM)" note under each one.
- **Drill-down**: `GET /api/incidents/{id}/tree` returns
  `{root_cause, symptomatic: [...]}` — walking up to the true root first
  if the given incident is itself a symptom, then listing everything
  currently linked to that root.

**Deliberately simplified vs. a real multi-tier optical network**: there's
one L1 incident type (`OPTICAL_ALARM`) and one L3 tier, so causality
ranking is just "is there an open L1 incident on this peering" rather
than a multi-hop chain (fiber span > amplifier > transponder > router
interface) — a real deployment would need the fuller topology
(transponders, ROADMs, individual amplifiers as their own graph nodes)
that the design doc this app follows calls out as the hardest, most-
often-underinvested-in part of a real system.

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
   | `BFD_SESSION_DOWN` | `NOTIFY_ONLY` (skipped) | fast-failure-detection event — carrier-side/physical, needs NOC/carrier engagement |
   | `ISIS_NBR_DOWN` | `NOTIFY_ONLY` (skipped) | likely a transient physical issue on one bundle member; the peering itself is unaffected unless every member goes down (see "Interface bundles" below) |
   | `OPTICAL_ALARM` (incl. the fiber-cut scenario) | `NOTIFY_ONLY` (skipped) | hardware/fiber issue — can't be fixed by a config change; see "L1/L3 layering" above |
   | `FAN_FAILURE` / `PSU_FAILURE` | `NOTIFY_ONLY` (skipped) | chassis hardware failure — NOC/dispatch notified |
   | `HIGH_MEMORY` | `NOTIFY_ONLY` (skipped) | memory-pool exhaustion — flagged for human review rather than an automated process restart |

   `LINK_UP`, `COLD_START`/`WARM_START`, `ISIS_NBR_UP`, and `UNKNOWN` are
   "good news" or uninterpretable events and never enter the auto-heal
   pipeline at all. Whatever the type, an incident that's been linked as
   **symptomatic** of an open L1 root cause (see "L1/L3 layering" above)
   also skips auto-heal entirely, even if its type would otherwise be
   actionable.

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

**Manual resolution.** Every `NOTIFY_ONLY` incident type in the table
above stays **open** - nothing auto-remediates them, so they sit there
until a human closes them (or, for `ISIS_NBR_DOWN`/`OPTICAL_ALARM`
specifically, until the matching "up"/"cleared" event resolves it - see
"L1/L3 layering" above). The Incidents tab's
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
| Basemap | OpenStreetMap standard tiles (`tile.openstreetmap.org`) | — switched from CARTO's `dark_all` raster tiles, which now require a paid-tier API key and render an "API key required" watermark without one; the customer-uplink/BGP-line palette below was already designed to stay legible against tile-map greens/tans for exactly this reason |
| Healthy customer uplink line | slate | `#64748b` |
| BGP peering — established | blue | `#3b82f6` |
| BGP peering — down | red | `#ef4444` (same as router "down") |
| Active reroute path (animated dashes) | purple | `#a855f7` — deliberately distinct from both established-blue and down-red, so "traffic detouring" reads differently from either steady state |
| Marker outline — normal | near-black | `#0b0f1a` |
| Marker outline — needs attention (blinking, solid ring) | orange | `#f97316` |
| Marker outline — symptomatic of an open L1 fault (dashed ring) | amber | `#fb923c` |
| Fiber-fault segment line — cosmetic overlay only | orange | `#f97316` (same as needs-attention; pulses when the underlying incident is real - see "L1/L3 layering") |

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

SQLite specifically has one new wrinkle since `correlator` became its own
container (see "Alarm pipeline" above): SQLite is a local file, and only
`backend`'s container has the `backend-data` volume mounted, so
`correlator` would end up writing to its own separate, empty SQLite file
instead of sharing one. SQLite here is really meant for running the
backend directly on your machine outside docker-compose (see its comment
in `.env.example`); under docker-compose, use Postgres or MySQL, both
real network services every container reaches the same way.

Neither Kafka nor Neo4j are affected by this choice at all — they're
independent of `DATABASE_URL` and stay exactly as configured regardless
of which SQL database the alarm store uses.

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
| `DATABASE_URL` | backend, correlator | `postgresql+psycopg2://ncr:ncr@postgres:5432/ncr` | SQLAlchemy connection string (the alarm store) |
| `KAFKA_BOOTSTRAP_SERVERS` | backend, normalizer, correlator | `kafka:9092` | Kafka broker address for the alarm pipeline (see "Alarm pipeline" above) |
| `NEO4J_URI` | backend, correlator | `bolt://neo4j:7687` | Topology graph connection - written by `backend` (on BGP seed), read by `correlator` (root-cause lookups) |
| `NEO4J_USER` / `NEO4J_PASSWORD` | backend, correlator | `neo4j` / `ncrpassword` | Neo4j credentials the app connects with. `NEO4J_PASSWORD` also drives the `neo4j` service's own `NEO4J_AUTH` in `docker-compose.yml` (`${NEO4J_PASSWORD:-ncrpassword}`), so changing it in `.env` updates both sides from one value; `NEO4J_USER` only changes what the app sends - the server's admin username is always `neo4j` (the image's own default), so leave it as-is unless you also change that server-side |
| `TRAP_PORT` | backend | `1162` | UDP port the trap listener binds |
| `FLAP_WINDOW_SECONDS` | backend | `600` | Sliding window for flap detection |
| `FLAP_TRANSITION_THRESHOLD` | backend | `4` | Transitions within the window to call it "flapping" |
| `FLAPPING_ROUTER_COUNT` / `DOWN_ROUTER_COUNT` | simulator | `5` / `3` | How many primaries misbehave |
| `FLAPPING_BUNDLE_COUNT` | simulator | `3` | How many BGP-peering bundles get a chronically-flapping member (redundancy always absorbs it) |
| `TICK_MIN_SECONDS` / `TICK_MAX_SECONDS` | simulator | `3` / `8` | Pace of the primary event stream |
| `CUSTOMER_TICK_MIN_SECONDS` / `CUSTOMER_TICK_MAX_SECONDS` | simulator | `20` / `45` | Pace of the (much quieter) customer event stream |
| `BGP_INCIDENT_INTERVAL_SECONDS` | simulator | `600` | Cadence of a guaranteed logical BGP flap (session-level, bundle unaffected) |
| `BUNDLE_FAILURE_INTERVAL_SECONDS` | simulator | `900` | Cadence of a guaranteed full bundle failure (every member down — redundancy exhausted, peering actually drops) |
