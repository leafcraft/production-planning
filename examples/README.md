# production-planning — example scenarios

Four realistic scenarios that exercise the major paths through this mesh.

## How to run

```bash
python examples/run_examples.py                 # all scenarios
python examples/run_examples.py 01_             # one scenario (substring match)
```

## Scenario map

- **01_demand_signal_normal** — `manual_action` — Weekly demand sense — scheduler builds the 7-day plan, no constraints fire.
- **02_supplier_delay** — `supplier_signal` — Tier-1 supplier announces 7-day delay on resin — supplier_signal_agent re-plans against constrained supply.
- **03_emergency_constraint_alert** — `shortage_alert` — Material shortage — constraint_flagger picks up, ops_director_human escalation path.
- **04_supplier_capacity_change** — `supplier_capacity_change` — Supplier announces capacity reduction — re-plan against new ceiling.

## Connectors

This template is YAML-first: `capacity_model_agent`,
`material_requirements_agent` and `planning_analytics_agent` read from
YOUR MES / ERP-MRP / historian via the MCP connectors in
`configs/config.yaml`. Set `MES_MCP_URL` (and friends) in `.env`
before running scenarios that traverse them — scenario 01 fans in
demand + capacity, so the scheduler waits on your MES connector.

## Audit trail

Every scenario appends one hash-chained entry to the template's
`logs/<audit>.jsonl` file. Tail it during a run to see the
ledger build up across scenarios.
