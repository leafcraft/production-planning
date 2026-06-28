# production-planning — Production Planning & Scheduling

A department of self-reliant agents that re-optimises the production
schedule every time the picture changes — a new demand forecast, a line
going down, a supplier slipping, or the 4-hourly clock — and routes only
the consequential changes to a human planner. Realises the **prod × goods**
ROI cell: pool ~14% of revenue, ~70% routine.

---

## 1. Story

It's Tuesday at the Portland plant. **Maya**, the production planner, used
to spend her mornings rebuilding the week's sequence in a spreadsheet. Now
a demand signal lands on the `demand_signal` entry point: the weekly
review pushed 12,400 units of forecast against 13,200 of capacity.

`demand_sensor_agent` (LLM) turns the raw forecast + actuals into a
planning-ready demand picture — SKU-level units, due dates, segments,
a confidence score, the notable shifts vs last period. It hands to
`capacity_model_agent`, which calls Maya's **MES work-centre calendar**
over MCP and returns the real line/shift/equipment availability and any
maintenance windows. With both in hand, `scheduler_agent` (Claude) fans
in (`wait_for: demand_sensor AND capacity_model`), re-optimises the
sequence, and writes a planner briefing leading with on-time-delivery %.

`constraint_flagger_agent` — **deterministic Python, never an LLM** — runs
the auto-release gate over the schedule's unmet demand. A clean re-opt
(no safety-stock breach, no severity-≥0.7 conflict) flows straight to
`release_agent`, which pushes the job to the MES and lands a hash-chained
WORM audit entry. Maya never sees it; that's the point.

Two weeks later a supplier slips. `supplier_signal_agent` ingests the
delay notice, finds no alternate supplier above its severity floor, and —
because the hard rule fires — routes to `planner_human`. Maya now sees a
single decision: *which* schedule, *which* SKUs, *what* the impact is.
She approves; `release_agent` recovers the stashed schedule and pushes.
If she'd gone quiet for two hours, the gate fails closed and escalates to
the plant manager, then the ops director, then halts — never a silent
auto-release.

## 2. Why it works in an organization

Planners spend loaded hours rebuilding schedules every cycle; most of that
work is routine arithmetic that doesn't need judgment. This agency carries
the routine (clean re-opts auto-release) and reserves the human for the
exceptions that actually move on-time delivery or breach a safety-stock
floor. The wins:

- **Reclaimed planner hours** — the ~70% routine re-opts run agent-side;
  Maya only touches flagged exceptions.
- **Faster reaction to disruption** — supplier slips, line-down events and
  demand spikes re-plan the moment they arrive, not at the next manual
  cycle, so the floor hears about a shortage before material is missing.
- **Auditable control** — every release and every human intervention lands
  in a hash-chained WORM log; the gate that decides auto-release-vs-human
  is reproducible Python, not an LLM vote.
- **No rip-and-replace** — schedules, BOMs, on-hand inventory and OEE
  history stay in *your* MES/ERP/historian, reached over MCP. The template
  ships zero systems-of-record.

## 3. Configuration (three tiers)

### Tier 1 — Boot (required to start at all)
| Var | Why |
|---|---|
| `LEAFMESH_LICENSE_KEY` | required by the SDK at `start()` |
| `OPENAI_API_KEY` | `demand_sensor`, `supplier_signal` (gpt-4o-mini), manager |
| `ANTHROPIC_API_KEY` | `scheduler`, `capacity_what_if` (claude-sonnet-4-6) |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` | session + auto-storage (defaults `localhost:6379`, no password) |

`LEAFMESH_ENV_TOKEN` defaults to `development`.

### Tier 2 — Host prereqs + adapters (your systems of record)
The mesh runs without these but the connector agents return nothing until
wired. Each is `integration: mcp` (http transport) — swap to
`n8n`/`zapier`/`composio` by changing `integration` + `connector_config`.
The `yields:` block under each agent is the contract your connector must
return.

| Your system | Agent | Env var | MCP tool |
|---|---|---|---|
| MES / ERP work-centre calendar | `capacity_model_agent` | `MES_MCP_URL` | `get_line_capacity` |
| ERP / MRP (SAP, Oracle, NetSuite, Odoo…) | `material_requirements_agent` | `ERP_MRP_MCP_URL` | `run_mrp_explosion` |
| MES historian (OEE + forecast accuracy) | `planning_analytics_agent` | `MES_HISTORIAN_MCP_URL` | `get_planning_analytics` |

`planning_analytics_agent`'s weekly backtest cron ships commented out —
enable it after wiring the historian.

### Tier 3 — Connectors (optional escalation egress)
| Var | Used by | Note |
|---|---|---|
| `PAGER_WEBHOOK` | manager escalation `webhook` target | defaults to the PagerDuty enqueue URL |
| `SLACK_PLANT_CHANNEL` | manager escalation `channel` target (`channel_id`) | optional |
| `SLACK_BOT_TOKEN` / `SLACK_SIGNING_SECRET` | **not** read by the escalation target — these belong to a `channels: {slack: {...}}` block (ChannelConfig), which this config does not define. Inert stubs until you add that block or a Slack channel adapter. |

The third escalation target is the `ops_director_human` (always present).

## 4. Agents

| Agent | Role | Responsibilities | Can perform |
|---|---|---|---|
| `demand_sensor_agent` | Demand planner | Turn forecast + actuals into a planning-ready SKU demand picture with confidence + notable shifts; pulls its own demand picture (`@pre_compose`) | LLM (gpt-4o-mini), module `demand_sensor_agent.py`, knowledge `demand_signals` → `capacity_model_agent` |
| `capacity_model_agent` | Capacity analyst | Read line/shift/equipment availability + maintenance windows from the MES/ERP work-centre calendar | programmatic, `integration: mcp` (`get_line_capacity`) → `scheduler_agent` |
| `scheduler_agent` | Production scheduler | Self-pull (`@pre_compose`) capacity + current schedule + BOM/materials, fan-in demand+capacity, re-optimise, compute on-time-delivery %, write briefing; `@chain` stashes full schedule for HITL recovery | LLM (claude-sonnet-4-6), knowledge `scheduling_model`, module `scheduler_agent.py` → `constraint_flagger_agent` |
| `constraint_flagger_agent` | Plant-policy gatekeeper | Deterministic auto-release gate — THREE floors (safety-stock/severity, material-availability via BOM×on-hand, capacity vs effective hours). Breach → planner; else auto-release. Reproducible math, never an LLM | programmatic, module `constraint_flagger_agent.py` (`@pre_compose` pulls materials/capacity) → `planner_human` (review) \| `release_agent` (clean) |
| `release_agent` | MES release operator | Fail-closed push of the approved schedule to MES; refuses if no schedule_id is recoverable | programmatic, module `release_agent.py` → `planning_audit_logger_agent` |
| `supplier_signal_agent` | Supplier-risk analyst | Self-pull (`@pre_compose`) supplier master + affected schedule, ingest delay/quality/capacity/force-majeure signals, propose alternates; `@chain` floor re-derives the planner-review decision so an LLM can't under-rate a slip | LLM (gpt-4o-mini), module `supplier_signal_agent.py`, knowledge `supplier_master` → `planner_human` (review) \| `scheduler_agent` (re-plan) |
| `material_requirements_agent` | Materials planner (MRP) | Run the MRP explosion (demand × BOM → requirements + shortages) in the ERP/MRP system | programmatic, `integration: mcp` (`run_mrp_explosion`), terminal answer |
| `planning_analytics_agent` | Planning analyst | Per-shift OEE/availability/quality rollups + forecast backtest (MAPE/bias/tracking-signal → model recs), answered by the MES historian | programmatic, `integration: mcp` (`get_planning_analytics`) → `planner_human` (review) |
| `capacity_what_if_agent` | Capacity strategist | Scenario simulator — add/drop shift, line down, demand spike, supplier outage → throughput/cost/schedule-fit + recommendation | LLM (claude-sonnet-4-6), knowledge `capacity_model_params` → `planner_human` (review) |
| `rolling_reopt_orchestrator_agent` | Scheduling cadence keeper | 4-hourly heartbeat tick that keeps the periodic re-opt cadence observable (no-op; scheduled_reopt entry targets the scheduler directly) | programmatic, cron `0 */4 * * *`, terminal |
| `planner_human` | Production planner | Decide on flagged schedule conflicts: approve → release, revise, or escalate | human gate, 2h timeout (default-deny → plant manager) → `release_agent` \| `plant_manager_human` |
| `plant_manager_human` | Plant manager | Tier-2 operational + safety/quality call | human gate, 4h timeout (default-deny → ops director) → `release_agent` \| `ops_director_human` |
| `ops_director_human` | Operations director | Strategic escalation; final tier | human gate, 8h timeout (default-deny → halt) → `planning_audit_logger_agent` |
| `planning_audit_logger_agent` | Compliance recorder | Hash-chained WORM audit of every release + human intervention | external (`framework: custom`), intelligence sink `agency/_shared/audit_logger.py`, terminal |

Manager (`gpt-4o-mini`, learning routing, knowledge `planning_playbook`)
oversees the mesh and escalates on the conditions in its prompt
(unresolvable shortage, top-20 SKU on-time risk, recurring conflict
pattern, safety-stock floor) via three targets: `ops_director_human`,
the PagerDuty webhook, and the Slack plant channel.

### Why the Python modules stay
- **`demand_sensor_agent.py`** — `@pre_compose` self-pulls the demand
  picture (DEV store / PROD forecast service) before the LLM runs.
- **`scheduler_agent.py`** — `@pre_compose` pulls the full operating
  picture (capacity, current schedule, BOM/materials); `@chain` is the
  pre-gate stash writer (the HITL resume delivers only human meta fields,
  so the full schedule is stashed per session for `release_agent`).
- **`constraint_flagger_agent.py`** — the auto-release gate, reproducible
  arithmetic never an LLM. THREE deterministic floors: safety-stock /
  severity over unmet demand, **material-availability** (scheduled SKUs ×
  BOM vs on-hand minus safety stock), and **capacity** (load vs effective
  capacity). Merges the verdict into the session stash.
- **`supplier_signal_agent.py`** — `@pre_compose` pulls the supplier
  master + affected schedule; `@chain` re-derives
  `requires_planner_review` deterministically so an LLM can't under-rate a
  slip (force-majeure, severity≥0.5, severity≥0.3 with no alternates, or a
  single-source supplier → always a human).
- **`release_agent.py`** — the MES actuator. Fail-closed: no recoverable
  `schedule_id`, no MES push.

### The dev store (day-0, no connectors)
`agency/_shared/store.py` seeds a realistic Portland-plant snapshot —
demand (5 SKUs), three-line/two-shift capacity, BOM + on-hand inventory,
a supplier master (one single-source vendor), and the schedule currently
on the floor. The agency modules read it through the same getter
signatures a connector would satisfy, so the full chain runs end-to-end
before anything is wired. Set `MES_MCP_URL` / `ERP_MRP_MCP_URL` /
`MES_HISTORIAN_MCP_URL` and swap the reads for connector calls in
production — the function signatures are the contract.

## 5. Trust mechanisms
- **safety_stock_breach ALWAYS requires planner review** — no auto-release.
- **Severity ≥ 0.7 conflict → planner review.**
- **Material-availability floor** — scheduled SKUs exploded × BOM against
  on-hand minus safety stock; a material breach can never auto-release.
- **Capacity floor** — a schedule exceeding effective capacity is flagged
  and cannot auto-release.
- **Supplier review floor** — `supplier_signal_agent`'s `@chain` forces a
  planner review on force-majeure, severity≥0.5, severity≥0.3 with no
  alternates, or a single-source supplier — the LLM can't waive it.
- **HITL chain** (planner → plant manager → ops director) with default-deny
  on every timeout; the ops-director timeout halts rather than releases.
- **Hash-chained WORM** on every release + intervention.
- **Fail-closed release** — no recoverable `schedule_id`, no MES push.

## Quick start

```bash
cp env .env       # set LEAFMESH_LICENSE_KEY (+ OPENAI/ANTHROPIC keys)
pip install -r requirements.txt
docker run -d -p 6379:6379 redis:7-alpine
python validate_config.py
python main.py
```

The connector env vars (`MES_MCP_URL` / `ERP_MRP_MCP_URL` /
`MES_HISTORIAN_MCP_URL`) are optional day-0 — the seeded dev store stands
in for them so the full chain runs out of the box.

### Drive it — a demand signal end-to-end

```bash
curl -X POST http://127.0.0.1:18820/api/mesh/call \
  -H "Content-Type: application/json" \
  -d '{"entry_point":"demand_signal",
       "input_data":{"plant_id":"PLANT-PDX","signal_source":"demand",
                     "planning_window_days":14}}'
```

`demand_sensor` pulls the seeded demand picture, `capacity_model` returns
line/shift availability, `scheduler` re-sequences against the real
capacity, and `constraint_flagger` runs the three floors: a clean re-opt
auto-releases to MES and lands a hash-chained audit entry; a
material/safety-stock/capacity breach routes to `planner_human` instead.

```bash
# A single-source supplier slip — the @chain floor forces a planner review
curl -X POST http://127.0.0.1:18820/api/mesh/call \
  -H "Content-Type: application/json" \
  -d '{"entry_point":"supplier_signal",
       "input_data":{"supplier_signal":{"supplier_id":"SUP-SILICON",
                     "signal_type":"delay","delay_days":5}}}'
```
