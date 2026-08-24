# production-planning — Production-Planning Pod — PRD

## 1. Story

The Portland plant runs make-to-stock against a rolling forecast. **Maya**,
the production planner, owns the weekly sequence. The picture changes
constantly — a fresh forecast, a line down for maintenance, a supplier
slip, the daily re-sense clock. Each change used to mean Maya rebuilding
the schedule by hand.

This pod re-optimises automatically on every signal. A coordinator opens a
change case; the demand+capacity picture is sensed line-by-line; a
Super-Agent scheduler re-sequences; a **deterministic** gate decides
whether the result is clean enough to auto-release to the MES or must go to
a human; a fail-closed finisher pushes the approved schedule and records a
hash-chained audit entry. Maya's day shrinks to the exceptions that matter.

## 2. Flow

```
 demand_signal ─┐
 capacity_change├─► plan_coordinator (Nova) ──open_case──► demand_sensor (Sensa)
 schedule_status┘                                          [instances:3, claim_line
                                                            → 3 disjoint lines]
                                                                │ picture_clean
                                                                ▼
 daily_sweep ─────────────────────────────────────────► scheduler (Rex, super_agent)
                                                          @chain cross-check floor
                                          discrepancy ◄── bounded (1) ──┐  │ clean
                                          (re-sense)                    │  ▼
                                          exhausted → ops_director      │ constraint_flagger (Gaia)
                                                                        │ [DETERMINISTIC GATE]
 supplier_signal ─► supplier_signal (Sable)         safety_breach ──────┼─► plant_manager_human
   ├ severe  → planner_human                         other review ──────┼─► planner_human
   └ routine → scheduler                             can_auto_release ──┼─► release_agent (Milo)
                                                                        │   [FINISHER, fail closed]
 planner_reply ─► planner_human (HITL resume)                          │        │
   humans: approved → release_agent ; held_for_review → audit          │        ▼
   (2h / 4h / 8h, default-deny on timeout → audit)                     └► planning_audit_logger
                                                                          [hash-chained WORM, terminal]
```

## 3. Actor inventory (job titles)

| Agent | Job title | Type |
|---|---|---|
| `plan_coordinator_agent` (Nova) | Plan Coordinator | llm (pure YAML) |
| `demand_sensor_agent` (Sensa) | Demand & Capacity Sensor | llm + module (`instances`, `@chain`) |
| `scheduler_agent` (Rex) | Production Scheduler | llm + module (`super_agent`, `@chain`) |
| `constraint_flagger_agent` (Gaia) | Auto-Release Gate | programmatic + module (3 floors) |
| `release_agent` (Milo) | MES Release Finisher | programmatic + module (finisher) |
| `supplier_signal_agent` (Sable) | Supplier-Risk Analyst | llm + module (`@pre_compose` + `@chain`) |
| `planner_human` | Production planner (HITL) | human |
| `plant_manager_human` | Plant manager (HITL) | human |
| `ops_director_human` | Operations director (HITL) | human |
| `planning_audit_logger_agent` | Compliance recorder | external (custom) |

## 4. Modern capabilities used (fitted to the domain)

- **instances (line fan-out)** — `demand_sensor_agent: instances: 3`.
  Each copy calls the atomic `claim_line` tool to claim a DIFFERENT line,
  so N copies sense N disjoint lines with no duplicate work. The `@chain`
  floor re-averages `confidence` across copies (the `last` merge SUMs
  numbers — wrong for a ratio) and sums `lines_claimed` (correct).
- **super_agent (dict form)** — `scheduler_agent: super_agent: {cost_ceiling,
  wall_clock}`. The re-optimisation is genuinely multi-step (plan → execute
  → verify per line/shift). Never the dead flat `super_agent_*` keys.
- **timezone** — `demand_sensor_agent` declares `timezone:
  America/Los_Angeles` next to its `wake_up` (the daily sweep would fire in
  UTC otherwise).
- **playbook (per-intent craft)** — Sensa (`sensing`), Rex (`reoptimize`),
  Sable (`supplier_triage`). Only `description` rides in the prompt;
  `responsibility` loads on demand.
- **mcp (declared, commented)** — the MES/ERP `mcp:` block on Sensa ships
  commented (the SDK rejects an empty-url http mcp at load); the day-0 tool
  surface is the dev-store fallback.

## 5. Controls / trust

- **Deterministic auto-release gate** (Gaia) — Python, never an LLM; three
  floors (safety-stock/severity, material-availability BOM × on-hand,
  capacity). Any breach forces a human.
- **Fail-closed finisher** (Milo) — allow/deny token re-check (unknown →
  denied), invariant re-derivation (recoverable schedule, real orders, no
  double-release via an `actuated` marker), delete-on-block, dev-store
  fallback that announces itself.
- **Bounded back-edge** — Rex's re-sense loop counts per session
  (`_MAX_REOPT_ATTEMPTS = 1`) and routes to the Ops Director once spent.
- **Supplier review floor** — Sable's `@chain` re-derives
  `requires_planner_review` (force-majeure / severity / single-source).
- **HITL default-deny** — planner (2h) → plant manager (4h) → ops director
  (8h); each `fallback_on_timeout: true` → `held_for_review` → the WORM
  sink. No human→human wires (escalation is the Manager's job — avoids the
  dual→dual footgun).
- **Hash-chained WORM** — `planning_audit_logger_agent` on every outcome.

## 6. Where data lives

- **Session / working state** — `agency/_shared/session_stash.py`
  (in-process) bridges the HITL resume gap; swap for the Redis session
  store at multi-replica scale (same interface).
- **System of record** — the customer's MES / ERP / MRP over MCP. The pod
  ships **no** system of record. `agency/_shared/store.py` is a seeded dev
  stand-in (demand, capacity, BOM/on-hand, suppliers, current schedule,
  line backlog) + the floor math (`material_shortfalls`,
  `capacity_overrun`); the getter signatures are the connector contract.
  Going live = swap the function bodies (Supabase / MCP), not the pod.

## 7. Verification

- `python validate_config.py` → structure clean (10 agents, 6 entry points).
- Load on SDK 2.4.131 → boots with **0 dual warnings**.
- `pytest` → the deterministic floors, the finisher fail-closed logic, the
  instances confidence re-average, and the bounded back-edge are unit-tested
  with no LLM / Redis.
- Day-0 dev-store run proves the chain; each connector is then verified at
  the **offered** layer and real cardinality before "live".

## 8. Reflection / decisions

- **Why a deterministic gate, not a prompt?** The auto-release decision is
  irreversible plant action; it must be reproducible arithmetic an LLM
  cannot waive. Controls stay separate from roles.
- **Why a finisher separate from the gate?** The gate DECIDES; the finisher
  ACTS — behind the human, re-checking authorization independently and
  failing closed. Splitting act-from-decide is the one-human/one-agent
  discipline held at the last stage.
- **Why instances on the sensor, not the scheduler?** A daily line sweep is
  a backlog fan-out (claim → read disjoint lines); the scheduler's work is
  multi-step reasoning over the whole plant, which is what `super_agent`
  is for. Stacking `instances × super_agent` would multiply cost for no gain.
- **Why a dev store?** A dev *stub* is explicitly fine — it lets the full
  pod run day-0 and gives the deterministic floors real BOM/on-hand numbers
  to compute against. It is never a system of record; every read announces
  itself when it has no real source.
