# production-planning — Production-Planning Pod

A pod of self-reliant digital employees + human plant roles that re-plans
the production schedule every time the picture changes — a fresh forecast,
a line down, a supplier slip, or the daily clock — releases the routine
re-opts itself, and routes only the consequential ones to a human. It
reads like an org chart: a coordinator, specialists, a deterministic gate,
a fail-closed finisher, three human tiers, and a WORM audit sink, watched
by the Manager as supervisor.

---

## 1. Story

It's Tuesday at the Portland plant. **Maya**, the production planner, used
to spend her mornings rebuilding the week's sequence in a spreadsheet. Now
a demand signal lands on the `demand_signal` entry point: the weekly review
pushed the forecast against a tight capacity ceiling.

**Nova** (Plan Coordinator) opens the change case and hands it to **Sensa**
(Demand & Capacity Sensor). Sensa runs three parallel copies — `scaling.copies.count:
3` — and each copy *atomically claims one production line* (`claim_line`)
so the three read three disjoint lines at once, scoring every SKU's demand
with a confidence. A SKU Sensa cannot read with confidence is flagged, not
guessed. Sensa's `@chain` floor re-averages the confidence across the
copies (the copies merge — `handoff: last` — SUMs numbers — wrong for a ratio) and refuses
to mark the picture clean while anything is uncertain.

With a clean picture, **Rex** (Production Scheduler, a Super-Agent) plans,
executes and verifies a re-optimisation across lines and shifts, writes
Maya a briefing that leads with on-time-delivery %, and renders a draft to
`./out`. Its `@chain` cross-check floor refuses to force an order to fit —
a discrepancy returns to Sensa, bounded to one re-sense before it escalates
(never an infinite loop).

**Gaia** — the Auto-Release Gate, **deterministic Python, never an LLM** —
runs three floors over the schedule (safety-stock / severity,
material-availability via BOM × on-hand, capacity vs effective hours). A
clean re-opt flows straight to **Milo** (the MES Release Finisher); a
safety-stock breach goes to the plant manager; any other review goes to
Maya.

**Milo** never trusts that reaching it means "approved." It re-checks
authorization (an allow-token allowlist + a block-token denylist; the
gate's `can_auto_release` is the only other authorized path; anything
unknown fails closed), re-derives invariants a human can't waive
(recoverable schedule, real orders, no double-release), renders the release
pack to `./out`, and **deletes the partial on any tripped floor**. With no
MES connector wired it records a *verifiable* dev outbox job that announces
itself (`released: false`, `status: recorded_dev`) rather than faking an
ack. Every release, approval, and held gate lands in the hash-chained WORM
sink.

Two weeks later Kestrel Microdevices, the single-source supplier of the
microcontroller in the CTL-310 controller, slips five days. **Sable**
(Supplier-Risk Analyst) ingests the notice; because it's a single-source
vendor its `@chain` floor forces a planner review no matter how the model
rated it. Maya sees one decision. If she does not answer within two hours,
her Inbox card closes and the ticket records that no reply came — nothing is
released on silence, and nobody is chased automatically.

## 2. Why it works in an organization

Planners spend loaded hours rebuilding schedules every cycle; most of that
is routine arithmetic that needs no judgment. This pod carries the routine
and reserves the human for the exceptions that actually move on-time
delivery or breach a safety-stock floor.

- **Reclaimed planner hours** — clean re-opts auto-release; Maya only
  touches flagged exceptions.
- **Faster reaction to disruption** — supplier slips and line-down events
  re-plan the moment they arrive, so the floor hears about a shortage
  before material is missing.
- **Auditable control** — the auto-release-vs-human decision is
  reproducible Python, not an LLM vote; every release and intervention
  lands in a hash-chained WORM log.
- **No rip-and-replace** — schedules, BOMs, on-hand inventory and capacity
  stay in *your* MES/ERP, reached over MCP. The pod ships zero
  systems-of-record; a seeded dev store runs the full chain day-0.

## 3. Prerequisites (three tiers)

### Tier 1 — Boot (required to start at all)
| Var | Why |
|---|---|
| `LEAFMESH_LICENSE_KEY` | required by the SDK at `start()` |
| `OPENAI_API_KEY` | Nova, Sensa, Sable (gpt-4o-mini) + the Manager |
| `ANTHROPIC_API_KEY` | Rex (claude-sonnet-4-6, the Super-Agent scheduler) |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` | session + persistence (defaults `localhost:6379`, no password) |

`LEAFMESH_ENV_TOKEN` defaults to `development`. Day-0 the seeded dev store
(`agency/_shared/store.py`) stands in for every system of record, so the
**full pod runs with no connectors wired.**

### Tier 2 — Your systems of record (wire when going live)
The pod runs without these; they replace the dev store's reads. Each
system's address + token is entered **only in the vault** (Studio →
Infrastructure → Integrations → MCP) — the SDK refuses a `url:` / `token:` written in
the config, even as `${ENV_VAR}`. The `mcp:` block on `demand_sensor_agent`
ships **commented**, already picking `connection: mes_mcp` /
`connection: erp_mrp_mcp`: create the connections, then uncomment it (in
`configs/config.yaml` before the first start, in Studio's YAML editor after it —
see *Changing the configuration after the first start*).

| Your system | Reached by | Vault connection | Replaces (dev-store fn) |
|---|---|---|---|
| MES work-centre calendar | Sensa (`read_line_capacity` / mcp) | `mes_mcp` | `store.read_line_capacity` |
| ERP / MRP (BOM + on-hand) | Sensa + Gaia (`read_line_demand` / mcp) | `erp_mrp_mcp` | `store.read_line_demand`, `store.material_shortfalls` |
| MES release (push a job) | Milo (the finisher) | `mes_mcp` | `store.push_release` (returns nothing day-0) |

Nothing goes in `.env` for these: the tools and the finisher check the vault
records themselves. Until `store.push_release()` is written against your MES
and returns the MES's own job id, Milo records a dev outbox job that
announces itself (`released: false`, `status: recorded_dev`, and a reason
naming `mes_mcp`) — it never fakes a real MES ack. Swap the function bodies in
`store.py` for connector calls; the signatures are the contract, the pod does
not change.

### Tier 3 — HITL + escalation egress (optional)
| Var | Used by | Note |
|---|---|---|
| *(no setting)* | the three human gates | answer in Studio's Inbox; to notify an external tool instead, create a vault connection (an "Add custom" record on the Infrastructure page, service type 'Outbound webhook') and set `webhook_config: {connection: <name>}` on the seat |
| *(vault connection `escalation_webhook`)* / `SLACK_PLANT_CHANNEL` | Manager escalation (commented targets) | webhook: create the vault connection (the target already picks `connection: escalation_webhook`); Slack: connect Slack on Infrastructure → Messaging → Slack (vault record, e.g. `slack_plant`), give `ops_director_human` `channels: {slack: {connection: slack_plant}}`, set the var (the target cannot post without that adapter); then uncomment the target in `manager.escalation` (in `configs/config.yaml` before the first start, in Studio's YAML editor after it) |

## 4. Agents

| Agent | Human job title / role | Responsibilities | Type & wiring |
|---|---|---|---|
| `plan_coordinator_agent` (**Nova**) | Plan Coordinator | Opens each change case, orchestrates, keeps live status, single point of contact. Never senses, schedules, or approves. | llm (gpt-4o-mini), pure YAML, chain → `demand_sensor` (open) \| `ops_director_human` (escalate) |
| `demand_sensor_agent` (**Sensa**) | Demand & Capacity Sensor | Claims a line, reads its demand + capacity, scores confidence, refuses to guess. `scaling.copies.count: 3` fan-out; `@chain` re-averages confidence + confidence floor. | llm (gpt-4o-mini), `agency/demand_sensor_agent.py`, tools `claim_line`/`read_line_*`, playbook `sensing`, wake_up `0 6 * * *` (America/Los_Angeles) → `scheduler` (clean) \| `ops_director_human` (unclean) |
| `scheduler_agent` (**Rex**) | Production Scheduler | Re-optimises across lines/shifts, cross-checks, briefs the planner, renders a draft. `super_agent` (plan/execute/verify); `@chain` cross-check floor + bounded re-sense back-edge + pre-gate stash. | llm (claude-sonnet-4-6), `agency/scheduler_agent.py`, playbook `reoptimize` → `demand_sensor` (bounded retry) \| `ops_director_human` (exhausted) \| `constraint_flagger` (clean) |
| `constraint_flagger_agent` (**Gaia**) | Auto-Release Gate | THREE deterministic floors (safety-stock/severity, material-availability, capacity) decide auto-release vs a human. Reproducible math, never an LLM. | programmatic, `agency/constraint_flagger_agent.py` → `plant_manager_human` (safety breach) \| `planner_human` (review) \| `release_agent` (clean) |
| `release_agent` (**Milo**) | MES Release Finisher | Re-checks authorization (fail closed on unknown), re-derives invariants, renders the release pack, deletes on block, ledgers; dev-store fallback announces itself. | programmatic FINISHER, `agency/release_agent.py` → `planning_audit_logger_agent` |
| `supplier_signal_agent` (**Sable**) | Supplier-Risk Analyst | Ingests supplier signals, proposes alternates; `@chain` floor forces a human on a slip it can't safely absorb (force-majeure / severity / single-source). | llm (gpt-4o-mini), `agency/supplier_signal_agent.py`, playbook `supplier_triage` → `planner_human` (severe) \| `scheduler` (re-plan) |
| `planner_human` | Production planner | Decides flagged schedules + supplier reviews; approves release. Answers within 2h (same shift). | human, Studio Inbox, dual → `scheduler` (supplier approved) \| `release_agent` (schedule approved) \| audit (rejected / held_for_review) |
| `plant_manager_human` | Plant manager | The safety/quality call on a safety-stock breach. Answers within 4h. | human, Studio Inbox, dual → `release_agent` (approved) \| audit (rejected / held_for_review) |
| `ops_director_human` | Operations director | Accountable owner; reviews exceptions (unclear demand picture, exhausted re-sense) + the Manager's escalations. Answers within 8h (a business day). | human, Studio Inbox, dual → `release_agent` (approved) \| audit (rejected / held_for_review) |
| `planning_audit_logger_agent` | Compliance recorder | Hash-chained WORM audit of every release, approval, hold, intervention. Terminus. | external (`framework: custom`), `agency/_shared/audit_logger.py`, terminal |

The **Manager** (`gpt-4o-mini`, static routing) is the supervisor, not a
router in the middle of the team. It escalates to the Ops Director
(`manager.escalation`) on a non-retryable error (auth, a bad request, credit,
bad config), when 5 different agents fail in one case, or when restarts run
out. A malformed hand-off only raises an alert.

**If nobody answers.** Each human request waits for the time in the table
above. If nobody answers within it, the request waits no longer: its Inbox
card closes and the ticket records that no reply came. Nothing is released
on silence, and nobody is chased automatically. (Each seat's
`fallback_response` — `held_for_review` → audit — states the intended
default-deny; leafmesh 2.4.486 does not route it for these seats yet.)

## 5. Trust mechanisms

- **Deterministic auto-release gate** — Gaia is Python, never an LLM. Three
  floors the model can't waive: safety-stock/severity, material-availability
  (BOM × on-hand minus safety stock), and capacity per line (units ÷ the
  line's rate vs that line's effective hours).
- **Fail-closed finisher** — Milo re-checks authorization (allow/deny
  tokens, unknown → denied), re-derives invariants, deletes the partial
  artifact on any tripped floor, and never fakes a real MES ack.
- **Bounded back-edge** — Rex's re-sense loop counts per session and routes
  to the Ops Director once spent (Rule 3), never an infinite loop.
- **Supplier review floor** — Sable's `@chain` forces a human on
  force-majeure, severity floors, or a single-source vendor.
- **Nothing released on silence** — planner (2h), plant manager (4h), ops
  director (8h). An unanswered request closes its Inbox card and the ticket
  records that no reply came; every answer (approved / rejected /
  held_for_review) has a route.
- **WORM audit** — hash-chained, append-only; insertion/deletion detectable.
- **Stub announces itself** — every dev-store read returns `available:
  false` when it has no real source, never a confident empty result.

## Quick start

```bash
cp env .env       # set LEAFMESH_LICENSE_KEY (+ OPENAI/ANTHROPIC keys)
pip install -r requirements.txt
docker run -d -p 6379:6379 redis:7-alpine
python validate_config.py     # → "all checks pass"
python main.py
```

The connectors (vault connections `mes_mcp` / `erp_mrp_mcp`) are optional
day-0 — the seeded dev store stands in so the full pod runs out of the box.

### Drive it — a demand signal end-to-end

```bash
curl -X POST http://127.0.0.1:18820/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{"entry_point":"demand_signal",
       "data":{"plant_id":"PLANT-PDX","message":"weekly forecast refresh"}}'
```

Nova opens the case, Sensa senses three lines in parallel, Rex re-sequences,
and Gaia runs the three floors. On the seed the full 14-day plan fits (LINE-2,
the bottleneck, runs at 89%; the microcontroller has 500 units of headroom
above safety stock), so a clean re-opt renders a release pack to `./out` and
lands a hash-chained audit entry; a material / safety-stock / capacity breach
routes to a human instead — answer it in **Studio → Inbox**.

```bash
# A single-source supplier slip — Sable's @chain floor forces a planner review
curl -X POST http://127.0.0.1:18820/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{"entry_point":"supplier_signal",
       "data":{"supplier_signal":{"supplier_id":"SUP-KESTREL",
                                  "signal_type":"delay","delay_days":5}}}'
```

## Changing the configuration after the first start

`configs/config.yaml` is only the first-start seed. On the first start (an empty store) the agency imports it as version 1 and from then on keeps its configuration in the store. After that the file is not read again: an edit to it is **not** applied, even after a restart (the boot log says "that edit was NOT applied").

To change a running agency:

- **Studio** — its settings pages, or the raw YAML editor.
- **API** — `PUT /api/yaml/raw` with an `X-API-Key` header and the body `{"yaml_content": "<the whole YAML>", "save_to_disk": true}`, or `PATCH /api/yaml/config` with `{"updates": {...}, "save_to_disk": true}`.

Each save is a new version and applies live on every pod — no restart. (A few settings, such as brokers, make the agency reload itself in place within seconds.) So wherever this README or a comment says "uncomment X in `configs/config.yaml`", do it in the file before the first start, or in Studio's YAML editor after it.

## Changing a running project (Docker)

What reaches a project that is already running (leafmesh 2.4.497). The
image holds the Python code and the first-start `configs/config.yaml`;
`.env` is passed in when the container is created; the store (Redis) keeps
its data on its own volume.

| You change | When it applies |
|---|---|
| An agent in Studio (prompt, model, tools, routes, human seats, schedules, …) | At once, no restart |
| A human seat's `channels` (Slack, email, …) | The agency reloads itself in place within seconds |
| Add or delete an agent in Studio | At once, including routes and schedules |
| A connection in the vault (model keys, MCP, webhooks, connectors, Slack, email) | At once. An agent switched off for a missing connection or address switches back on by itself |
| Anything in `.env` (licence, agency name, store, ports) | After `docker compose up -d` (the container is recreated) |
| Python code (`agency/*.py`: programmatic agents, tools, `@pre_compose`) | After a rebuild: `docker compose up -d --build` |
| `configs/config.yaml` | Never after the first start. It is read only when the store is empty |
| Several app containers on one store | A Studio save reaches every one within about 15 seconds |

- Keep model keys in the vault. A key there wins over the same key in
  `.env` (the log warns), and a change applies at once. From 2.4.497 that
  holds for every value the vault holds (endpoints, organisation / project
  ids, cloud identities); the `.env` value returns when the record goes.
- Code the image does not have: an agent marked pure Python
  (`integration: python`) is switched off until its function is in the image;
  any other programmatic agent stays on, and each call to code it does not
  have fails. Rebuild the image with the code first.
- A new Redis volume is an empty store, so `configs/config.yaml` is imported
  again as version 1.
- **If the agency refuses to start after an upgrade** (a new leafmesh pin,
  then a rebuild: "Configuration has N error(s) in the stored
  configuration"), repair the stored configuration with the agency stopped:

  ```bash
  docker compose run --rm production-planning leafmesh config check      # each problem, with its line
  docker compose run --rm production-planning leafmesh config versions   # kept versions: which pass, which is in use
  docker compose run --rm production-planning leafmesh config rollback   # back to the newest version that passes (saved as a new version)
  ```

  Or `leafmesh config export > config.yaml`, fix it by hand, then
  `leafmesh config import config.yaml` (it is checked before it is saved).

## Where the data store is set

From leafmesh 2.4.482 the store (Redis or Postgres) is set **only in `.env`** — never in `configs/config.yaml`. A `persistence:` block in the YAML is ignored with a warning on leafmesh 2.4.482–2.4.485; from 2.4.486 the agency refuses to start if that block differs from `.env`, and names each `.env` line to add. The project's `.env` sets the store explicitly (`LEAFMESH_STORE`, `LEAFMESH_AGENCY_NAME`, `REDIS_*`).

- Redis (default): `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `REDIS_DB`; optional `REDIS_DEFAULT_TTL`, `REDIS_SESSION_TTL`, `REDIS_SSL*`, `REDIS_CLUSTER_NODES`.
- Postgres instead: `LEAFMESH_STORE=postgres` plus `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` (optional `POSTGRES_SSLMODE`).
- Or the whole server in one value: `REDIS_URL=redis://user:pw@host:6379/0` (`rediss://` turns TLS on) or `DATABASE_URL=postgresql://user:pw@host:5432/db`. The single names fill in what the URL leaves out.
- With Docker Compose the app reaches Redis as `redis` (the service name); compose sets that for you.
- Check it in Studio → **Infrastructure → Datastores**: it shows whether the store is reachable (never the address or password).

## This mesh's sign-in (leafmesh 2.4.497)

From leafmesh 2.4.497 each mesh signs in to LeafCraft with its own key, not
a key shared by every install.

- **First start.** The mesh makes a key pair and registers it with
  `LEAFMESH_LICENSE_KEY` and `LEAFMESH_ENV_TOKEN` from `.env` (copy the
  environment token from Studio — a placeholder such as `development` is
  refused). With both valid it works at once. If your organisation turned on
  "require approval", the first start stops and says so: approve it in
  Studio → **Keys → SDK access**, then start again. Organisation admins get an
  email for every new registration.
- **Where the key lives.** In the vault, in the store, encrypted with
  `LEAFMESH_VAULT_KEY`: it survives rebuilds and every copy of the app shares
  it. A new Redis volume, or a new vault key, makes a new key pair that
  registers again — revoke the old one in Studio.
- **Check or change it** (in Docker: `docker compose run --rm production-planning leafmesh auth status`):
  - `leafmesh auth status` — credential id, key fingerprint, registration and
    mode, never a key; `--check` signs in now and reports the tokens.
  - `leafmesh auth register` — register now.
  - `leafmesh auth rotate` — a new key pair: once it is active the mesh
    switches to it and revokes the old one. A revoked or expired key is never
    revived; rotate instead.
- **Lost the environment token?** From 5 Dec 2026 Studio shows the licence
  key and environment token only once, when they are made (then only a hint).
  Rotate the environment token in Studio and put the new one in `.env`.
  Rotating it changes where the agency's data is kept in the store, so do it
  before the agency holds data.
- **An existing project whose `.env` says `LEAFMESH_ENV_TOKEN=development`.**
  2.4.497 needs the real token. The token also names where the agency's data
  sits in the store, so with the real token the agency starts on an empty
  store (vault, sessions, board) — the old data stays under the old name. Set
  the real token before you put data in the agency, or contact us to move it.

## Your vault key — back it up

`create-leafmesh` put a `LEAFMESH_VAULT_KEY` in your `.env` when it made this project. It is the master key that encrypts every connection and credential you store in the vault (in Redis / Postgres).

- **Back it up like a master password.** Lose it and every stored connection has to be entered again.
- **Never change or regenerate it** for an existing project — a new key locks the existing vault. To change it, use `leafmesh vault rotate-key`.
- Every copy of the app (every replica) must get the **same** value. Docker Compose passes it in from `.env` as an environment variable.
- Never commit `.env` (it is already in `.gitignore`).
