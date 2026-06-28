# production-planning — Production Planning & Scheduling — PRD

## 1. Story

The Portland plant runs make-to-stock against a rolling forecast. **Maya**,
the production planner, owns the weekly sequence. The picture changes
constantly — a fresh forecast, a line going down for maintenance, a
supplier slipping a delivery, the 4-hourly re-opt clock. Each change used
to mean Maya rebuilding the schedule by hand.

This agency re-optimises automatically on every signal. A demand signal,
capacity change, supplier signal, scheduled tick or manual action enters
the mesh; demand and capacity are gathered; the scheduler re-sequences; a
**deterministic** gate decides whether the result is clean enough to
auto-release to the MES or must go to Maya. Clean re-opts release
themselves and land an audit entry; consequential ones become a single
human decision. Every release and intervention is recorded in a
hash-chained WORM log. Maya's day shrinks to the exceptions that matter.

## 2. Flow

```
                 ┌──────────────── entry points ────────────────┐
 demand_signal → demand_sensor_agent ─┐
 capacity_change → capacity_model_agent│ (also called by demand_sensor)
                                       ▼
            scheduler_agent  ◄── wait_for: demand_sensor AND capacity_model
 scheduled_reopt ─┘  ▲  manual_action ─┘        │ (stashes full schedule)
 (cron heartbeat:    │                          ▼
  rolling_reopt)     │              constraint_flagger_agent  [DETERMINISTIC GATE]
                     │                  │                       │
 supplier_signal →   │   requires_planner_review==true ─────────┘   can_auto_release==true
 supplier_signal_agent┤                ▼                              ▼
   ├─ review → planner_human ◄─────────┘                         release_agent ──┐
   └─ replan → scheduler_agent                                        ▲          │
                          planner_human (2h, default-deny)            │          │
                            ├─ approve → release_agent ───────────────┘          │
                            └─ escalate → plant_manager_human (4h, default-deny)  │
                                            ├─ approve → release_agent ───────────┤
                                            └─ escalate → ops_director_human (8h) │
                                                            └─ halt → audit       │
 mrp_compute → material_requirements_agent (ERP/MRP) ─ terminal                   ▼
 shift_kpi / kpi_report / backtest → planning_analytics_agent ─ planner_human   planning_audit_logger_agent
 capacity_what_if_run → capacity_what_if_agent ─ planner_human                   [hash-chained WORM, terminal]
 shortage_alert → constraint_flagger_agent
```

## 3. Actor inventory (job titles)

| Agent | Job title | Type |
|---|---|---|
| `demand_sensor_agent` | Demand planner | llm + module (`@pre_compose`) |
| `capacity_model_agent` | Capacity analyst | programmatic (mcp) |
| `scheduler_agent` | Production scheduler | llm + module (`@pre_compose` + `@chain`) |
| `constraint_flagger_agent` | Plant-policy gatekeeper | programmatic + module (`@pre_compose`, 3 floors) |
| `release_agent` | MES release operator | programmatic + module |
| `supplier_signal_agent` | Supplier-risk analyst | llm + module (`@pre_compose` + `@chain` floor) |
| `material_requirements_agent` | Materials planner (MRP) | programmatic (mcp) |
| `planning_analytics_agent` | Planning analyst | programmatic (mcp) |
| `capacity_what_if_agent` | Capacity strategist | llm |
| `rolling_reopt_orchestrator_agent` | Scheduling cadence keeper | programmatic (cron) |
| `planner_human` | Production planner (HITL) | human |
| `plant_manager_human` | Plant manager (HITL) | human |
| `ops_director_human` | Operations director (HITL) | human |
| `planning_audit_logger_agent` | Compliance recorder | external (custom) |

## 4. Data artifacts

- **`PlanEvent`** (declared `data_structures`) — the ingress envelope:
  `schedule_id`, `plant_id`, `line_id`, `signal_source`
  (demand|capacity|scheduled|manual), `planning_window_days`,
  `received_at_utc`, `raw_payload`. Required: `plant_id`, `signal_source`.
- **Demand picture** — `demand_sensor_agent` yields: `sku_demands`,
  `confidence`, `notable_shifts`.
- **Capacity** — `capacity_model_agent` yields (MES contract):
  `total_capacity_hours`, `bottleneck_lines`,
  `planned_maintenance_windows`, `effective_capacity_hours`.
- **Schedule** — `scheduler_agent` yields: `sequenced_orders`,
  `on_time_delivery_pct`, `unmet_demand`, `confidence`, `schedule_id`,
  `planner_briefing`.
- **Gate verdict** — `constraint_flagger_agent` yields: `conflicts`,
  `safety_stock_breach`, `requires_planner_review`, `can_auto_release`,
  `schedule_id`.
- **Release** — `release_agent` yields: `released_at_utc`, `mes_job_id`,
  `released`.
- **Audit entry** — `planning_audit_logger_agent` yields:
  `audit_event_id`, `written_at_utc`, `hash_chain_prev`,
  `hash_chain_current`. Sink: `./logs/planning_audit.jsonl`.
- **Session stash** (`agency/_shared/session_stash.py`) — in-process,
  per-session schedule payload that bridges the HITL resume gap (the SDK
  resumes a human-gated chain with only human meta fields).
- **Dev store** (`agency/_shared/store.py`) — seeded plant snapshot
  (demand, capacity, BOM/on-hand, supplier master, current schedule) +
  the floor math (`material_shortfalls`, `capacity_overrun`). The agency
  modules read it through the getter signatures a connector would satisfy,
  so the full chain runs day-0 with no connectors. Inert until the env
  vars are set AND the reads are swapped for connector calls.

## 5. Per-agent spec

See the agent table in `README.md` §4 (role, responsibilities,
type/model/knowledge/connector, routing). Key contracts:

- **demand_sensor_agent / scheduler_agent / supplier_signal_agent** are
  self-reliant — each `@pre_compose` pulls its own context from the dev
  store (PROD: the matching system of record) before the LLM runs:
  demand picture; capacity + current schedule + BOM/materials; supplier
  master + affected schedule.
- **scheduler_agent** fans in `demand_sensor_agent AND capacity_model_agent`
  (`wait_for_timeout: 60`). Its `@chain` stashes the full schedule keyed by
  session so `release_agent` can recover it post-HITL.
- **constraint_flagger_agent** enforces THREE deterministic floors:
  (1) severity from `unmet_demand` reason strings (`safety_stock`→1.0,
  `shortage`/`supplier`→0.8, else 0.6); (2) **material-availability** —
  scheduled SKUs exploded × BOM against on-hand minus safety stock
  (`store.material_shortfalls`); (3) **capacity** — schedule load vs
  effective capacity (`store.capacity_overrun`). `requires_planner_review`
  on any safety breach or any severity≥0.7; `can_auto_release` only when no
  floor fires and every conflict <0.5.
- **supplier_signal_agent** has a `@chain` review floor: `requires_planner_review`
  is re-derived (force-majeure, severity≥0.5, severity≥0.3 with no
  alternates, single-source supplier) so the LLM cannot under-rate a slip.
- **The three connector agents** (`capacity_model`,
  `material_requirements`, `planning_analytics`) are pure-YAML MCP shells.
  The `yields:` block is the contract the customer's connector must return.
- **release_agent** is fail-closed: no recoverable `schedule_id` → no push,
  returns `released: false` with an error.

## 6. Entry points

`demand_signal`, `capacity_change`, `scheduled_reopt`, `manual_action`,
`mrp_compute`, `shift_kpi_ingest`, `production_kpi_report`,
`supplier_signal`, `supplier_capacity_change`, `raw_material_shortage`,
`shortage_alert`, plus direct-call entry points
(`direct_call_release`, `direct_call_rolling_reopt_orchestrator`) and the
Lyzr-inspired `capacity_what_if_run` and `demand_forecast_backtest`
(planning_analytics backtest mode). Every non-human, non-terminal agent is
API-addressable.

## 7. Controls / trust

- **Deterministic auto-release gate** — `constraint_flagger_agent` is
  Python, never an LLM. THREE floors the LLM can't waive: safety-stock /
  severity, material-availability (BOM × on-hand), and capacity. Any
  breach forces a human.
- **Supplier review floor** — `supplier_signal_agent`'s `@chain`
  re-derives `requires_planner_review` (force-majeure / severity / no
  alternates / single-source) over the LLM's self-report.
- **HITL chain, default-deny** — planner (2h) → plant manager (4h) → ops
  director (8h); each `fallback_on_timeout: true` routing to the next tier;
  ops-director timeout **halts** rather than releasing.
- **Hash-chained WORM** — `planning_audit_logger_agent` on every release +
  intervention; SHA-256 chain makes insertion/deletion detectable.
- **Fail-closed actuator** — `release_agent` refuses anonymous pushes.
- **yields↔prompt OUTPUT CONTRACT** on every LLM agent.
- **No placeholder operator_ids** — HITL gates route to the shared inbox
  unless real operator emails are set.

## 8. Config pointer

`configs/config.yaml` IS the application. State lives in the customer's
systems (MES / ERP-MRP / MES historian) over MCP; the template ships no
SQLite system of record. See `README.md` §3 for the three-tier config.

## 9. Reflection / decisions

- **Why a module for the gate and not a prompt?** The auto-release decision
  is irreversible plant action; it must be reproducible arithmetic an LLM
  cannot waive (first principle 6, trust by construction).
- **Why a session stash instead of relying on the chain?** The SDK resumes
  a human-gated chain with only human meta fields; the schedule must
  survive the gate, so it is stashed per session (in-process dev stub;
  swap for the Redis session store at multi-replica scale).
- **Why keep the connector agents pure YAML?** Schedules/BOMs/OEE belong in
  the customer's systems (first principle 7); a template SQLite system of
  record would be a smell. The `yields:` block is the integration contract.
- **Why a dev store then?** A dev *stub* is explicitly fine (first
  principle 7) — `agency/_shared/store.py` is a seeded snapshot, not a
  system of record, that lets the full chain run day-0. The getter
  signatures are the connector contract; swap the reads in production. The
  deterministic floors (material-availability, capacity) also need real
  BOM/on-hand/capacity numbers to compute against, and the store provides
  them until the ERP/MES connectors are wired.
- **Why the cron heartbeat is a no-op?** `scheduled_reopt` targets the
  scheduler directly; `rolling_reopt_orchestrator_agent` exists only to
  keep the 4-hourly cadence observable.
- **Slack tokens are inert** — `SLACK_BOT_TOKEN`/`SLACK_SIGNING_SECRET`
  belong to a `channels:` block this config does not define; documented as
  stubs, not wired (avoids phantom config).
