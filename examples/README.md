# production-planning — example scenarios

Four realistic scenarios that exercise the major paths through the pod.

## How to run

```bash
# from the template's project root (so configs/ and agency/ resolve)
python examples/run_examples.py                 # all scenarios
python examples/run_examples.py 03_             # one scenario (substring match)
```

## Scenario map

- **01_demand_signal_normal** — `demand_signal` — a weekly forecast refresh:
  Nova opens the case, Sensa senses three lines in parallel (`instances: 3`
  + `claim_line`), Rex re-sequences, and Gaia's deterministic gate runs the
  three floors — a clean re-opt renders a release pack to `./out` and lands
  a WORM entry.
- **02_supplier_delay** — `supplier_signal` — PolyChem slips 7 days but has
  an alternate, so Sable absorbs it with an automatic re-plan (routine path,
  no human).
- **03_single_source_slip** — `supplier_signal` — Silicon Ltd is
  single-source; Sable's `@chain` floor FORCES a planner review no matter
  how the model rated the slip (the control the LLM can't waive).
- **04_capacity_change** — `capacity_change` — LINE-2 (the bottleneck) drops
  for unplanned maintenance; the pod re-plans against the reduced ceiling.

## Day-0 vs wired

Everything runs day-0 against the seeded dev store
(`agency/_shared/store.py`) — no connectors needed. To go live, set
`MES_MCP_URL` / `ERP_MRP_MCP_URL` in `.env` and uncomment the `mcp:` block
on `demand_sensor_agent` in `configs/config.yaml`. Until `MES_MCP_URL` is
set, the finisher records a dev outbox job that announces itself
(`released: false`, `status: recorded_dev`) — it never fakes a real MES ack.

## Human gates

Scenarios 03 (and any flagged re-opt) route to a human gate. With no HITL
receiver running they will wait until `human_timeout_seconds` and then
**default-deny** (`held_for_review` → the WORM sink). Point
`HITL_OUTBOUND_URL` at a receiver, or drive an approval via the
`planner_reply` entry point / the `/webhook/planner_reply` endpoint.

## Audit trail

Every scenario appends hash-chained entries to
`logs/planning_audit.jsonl`. Tail it during a run to watch the ledger build.
