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
(Demand & Capacity Sensor). Sensa runs three parallel copies — `instances:
3` — and each copy *atomically claims one production line* (`claim_line`)
so the three read three disjoint lines at once, scoring every SKU's demand
with a confidence. A SKU Sensa cannot read with confidence is flagged, not
guessed. Sensa's `@chain` floor re-averages the confidence across the
copies (the instances merge SUMs numbers — wrong for a ratio) and refuses
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

Two weeks later a supplier slips. **Sable** (Supplier-Risk Analyst) ingests
the notice; because it's a single-source vendor its `@chain` floor forces a
planner review no matter how the model rated it. Maya sees one decision. If
she goes quiet, the gate fails closed and the Manager escalates to the ops
director — never a silent auto-release.

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
The pod runs without these; they replace the dev store's reads. The
`mcp:` block on `demand_sensor_agent` ships **commented** (the SDK rejects
an empty-url http mcp at load) — fill the URL, then uncomment.

| Your system | Reached by | Env var | Replaces (dev-store fn) |
|---|---|---|---|
| MES work-centre calendar | Sensa (`read_line_capacity` / mcp) | `MES_MCP_URL` | `store.read_line_capacity` |
| ERP / MRP (BOM + on-hand) | Sensa + Gaia (`read_line_demand` / mcp) | `ERP_MRP_MCP_URL` | `store.read_line_demand`, `store.material_shortfalls` |
| MES release (push a job) | Milo (the finisher) | `MES_MCP_URL` | `store.record_outbox` (dev outbox) |

Until `MES_MCP_URL` is set, Milo records a dev outbox job that announces
itself (`released: false`, `status: recorded_dev`) — it never fakes a real
MES ack. Swap the function bodies in `store.py` for connector calls; the
signatures are the contract, the pod does not change.

### Tier 3 — HITL + escalation egress (optional)
| Var | Used by | Note |
|---|---|---|
| `HITL_OUTBOUND_URL` | the three human gates' `webhook_config.outbound_url` | defaults to the local HITL stub `http://127.0.0.1:9999/human-notify` |
| `PAGER_WEBHOOK` / `SLACK_PLANT_CHANNEL` | Manager escalation (commented targets) | uncomment the target in `manager.escalation` to use |

## 4. Agents

| Agent | Human job title / role | Responsibilities | Type & wiring |
|---|---|---|---|
| `plan_coordinator_agent` (**Nova**) | Plan Coordinator | Opens each change case, orchestrates, keeps live status, single point of contact. Never senses, schedules, or approves. | llm (gpt-4o-mini), pure YAML, chain → `demand_sensor` (open) \| `ops_director_human` (escalate) |
| `demand_sensor_agent` (**Sensa**) | Demand & Capacity Sensor | Claims a line, reads its demand + capacity, scores confidence, refuses to guess. `instances: 3` fan-out; `@chain` re-averages confidence + confidence floor. | llm (gpt-4o-mini), `agency/demand_sensor_agent.py`, tools `claim_line`/`read_line_*`, playbook `sensing`, wake_up `0 6 * * *` (America/Los_Angeles) → `scheduler` (clean) \| `ops_director_human` (unclean) |
| `scheduler_agent` (**Rex**) | Production Scheduler | Re-optimises across lines/shifts, cross-checks, briefs the planner, renders a draft. `super_agent` (plan/execute/verify); `@chain` cross-check floor + bounded re-sense back-edge + pre-gate stash. | llm (claude-sonnet-4-6), `agency/scheduler_agent.py`, playbook `reoptimize` → `demand_sensor` (bounded retry) \| `ops_director_human` (exhausted) \| `constraint_flagger` (clean) |
| `constraint_flagger_agent` (**Gaia**) | Auto-Release Gate | THREE deterministic floors (safety-stock/severity, material-availability, capacity) decide auto-release vs a human. Reproducible math, never an LLM. | programmatic, `agency/constraint_flagger_agent.py` → `plant_manager_human` (safety breach) \| `planner_human` (review) \| `release_agent` (clean) |
| `release_agent` (**Milo**) | MES Release Finisher | Re-checks authorization (fail closed on unknown), re-derives invariants, renders the release pack, deletes on block, ledgers; dev-store fallback announces itself. | programmatic FINISHER, `agency/release_agent.py` → `planning_audit_logger_agent` |
| `supplier_signal_agent` (**Sable**) | Supplier-Risk Analyst | Ingests supplier signals, proposes alternates; `@chain` floor forces a human on a slip it can't safely absorb (force-majeure / severity / single-source). | llm (gpt-4o-mini), `agency/supplier_signal_agent.py`, playbook `supplier_triage` → `planner_human` (severe) \| `scheduler` (re-plan) |
| `planner_human` | Production planner | Decides flagged schedules + supplier reviews; approves release. 2h timeout, default-deny → audit. | human, webhook, dual → `scheduler` (supplier OK) \| `release_agent` (schedule OK) \| audit (held) |
| `plant_manager_human` | Plant manager | The safety/quality call on a safety-stock breach. 4h timeout, default-deny → audit. | human, webhook, dual → `release_agent` (OK) \| audit (held) |
| `ops_director_human` | Operations director | Accountable owner; reviews exceptions + the Manager's escalations. 8h timeout, default-deny → audit. | human, webhook, dual → `release_agent` (OK) \| audit (held) |
| `planning_audit_logger_agent` | Compliance recorder | Hash-chained WORM audit of every release, approval, hold, intervention. Terminus. | external (`framework: custom`), `agency/_shared/audit_logger.py`, terminal |

The **Manager** (`gpt-4o-mini`, static routing) is the supervisor — it
watches every hand-off and escalates stalls, contract violations, and
repeated errors to the Ops Director (`manager.escalation`). It is not a
router in the middle of the team.

## 5. Trust mechanisms

- **Deterministic auto-release gate** — Gaia is Python, never an LLM. Three
  floors the model can't waive: safety-stock/severity, material-availability
  (BOM × on-hand minus safety stock), capacity vs effective hours.
- **Fail-closed finisher** — Milo re-checks authorization (allow/deny
  tokens, unknown → denied), re-derives invariants, deletes the partial
  artifact on any tripped floor, and never fakes a real MES ack.
- **Bounded back-edge** — Rex's re-sense loop counts per session and routes
  to the Ops Director once spent (Rule 3), never an infinite loop.
- **Supplier review floor** — Sable's `@chain` forces a human on
  force-majeure, severity floors, or a single-source vendor.
- **Default-deny HITL** — planner (2h) → plant manager (4h) → ops director
  (8h), each `fallback_on_timeout` → `held_for_review` → the WORM sink;
  silence never advances a release.
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

The connector env vars (`MES_MCP_URL` / `ERP_MRP_MCP_URL`) are optional
day-0 — the seeded dev store stands in so the full pod runs out of the box.

### Drive it — a demand signal end-to-end

```bash
curl -X POST http://127.0.0.1:18820/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{"entry_point":"demand_signal",
       "data":{"plant_id":"PLANT-PDX","message":"weekly forecast refresh"}}'
```

Nova opens the case, Sensa senses three lines in parallel, Rex re-sequences,
and Gaia runs the three floors: a clean re-opt renders a release pack to
`./out` and lands a hash-chained audit entry; a material / safety-stock /
capacity breach routes to a human instead.

```bash
# A single-source supplier slip — Sable's @chain floor forces a planner review
curl -X POST http://127.0.0.1:18820/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{"entry_point":"supplier_signal",
       "data":{"supplier_signal":{"supplier_id":"SUP-SILICON",
                                  "signal_type":"delay","delay_days":5}}}'
```
