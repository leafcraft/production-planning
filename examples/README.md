# production-planning — example scenarios

Four realistic scenarios that exercise the major paths through the pod.

## How to run

```bash
# from the project root (so .env and agency/ resolve)
create-leafmesh --load .   # once: loads the team into the Redis in .env (or use create-leafmesh --start .)
python examples/run_examples.py                 # all scenarios
python examples/run_examples.py 03_             # one scenario (substring match)
```

## Scenario map

- **01_demand_signal_normal** — `demand_signal` — a weekly forecast refresh:
  Nova opens the case, Sensa senses three lines in parallel (`scaling.copies.count: 3`
  + `claim_line`), Rex re-sequences, and Gaia's deterministic gate runs the
  three floors — a clean re-opt renders a release pack to `./out` and lands
  a WORM entry.
- **02_supplier_delay** — `supplier_signal` — Ridgeline Polymers slips 7 days but has
  an alternate, so Sable absorbs it with an automatic re-plan (routine path,
  no human).
- **03_single_source_slip** — `supplier_signal` — Kestrel Microdevices is
  single-source; Sable's `@chain` floor FORCES a planner review no matter
  how the model rated the slip (the control the LLM can't waive).
- **04_capacity_change** — `capacity_change` — LINE-2 (the bottleneck) drops
  for unplanned maintenance; Nova opens a case and the pod re-plans. Day 0 the
  seeded MES calendar only knows LINE-2's planned 16h window, so the re-plan
  reads that; to see the reduced ceiling, add the outage to
  `planned_maintenance_windows` in `agency/_shared/store.py` or wire the MES.

## Day-0 vs wired

Everything runs day-0 against the seeded dev store
(`agency/_shared/store.py`) — no connectors needed. To go live, create the
vault connections `mes_mcp` / `erp_mrp_mcp` (Studio → Infrastructure →
Integrations → MCP — the addresses and tokens live only there), uncomment the `mcp:`
block on `demand_sensor_agent` (in Studio's raw YAML editor — no restart). Nothing goes in `.env`: the
tools and the finisher check the vault records themselves. Until
`store.push_release()` is written against your MES, the finisher records a
dev outbox job that announces itself (`released: false`, `status:
recorded_dev`) — it never fakes a real MES ack.

## Human gates

Scenarios 03 (and any flagged re-opt) route to a human gate. The request lands
in **Studio → Inbox** — answer it there (approved / rejected /
held_for_review). Unanswered, it waits until `human_timeout_seconds`; then its
card closes and the ticket records that no reply came. Nothing is released
on silence.

## Audit trail

Every scenario appends hash-chained entries to
`logs/planning_audit.jsonl`. Tail it during a run to watch the ledger build.
