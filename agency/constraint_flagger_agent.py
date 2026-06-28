"""constraint_flagger_agent — THE DETERMINISTIC AUTO-RELEASE GATE.

Never an LLM. The auto-release decision is irreversible plant action, so
it is reproducible arithmetic an LLM may not waive (first principle 6).
This module enforces THREE deterministic floors over the scheduler's
output and the plant's real material/capacity picture:

  1. SAFETY-STOCK / SEVERITY floor — over scheduler `unmet_demand`:
       safety_stock breach → planner, no auto-release
       any conflict severity >= 0.7 → planner
  2. MATERIAL-AVAILABILITY floor — explode the scheduled SKUs × BOM
       against on-hand minus safety stock; any material that would
       breach its safety-stock floor is a safety_stock_breach → planner.
       The plant cannot run a schedule it has no material for.
  3. CAPACITY floor — a schedule whose load exceeds effective capacity
       physically cannot run → high-severity conflict → planner.

@pre_compose  load_plant_state — pull the materials + capacity picture
              the floors reason over (DEV store; PROD ERP/MRP + MES).

The agent body IS the floor: a programmatic agent (no LLM), so the three
floors run as the deterministic body — there is nothing downstream to
"waive" them. It also merges the verdict into the per-session stash so
release_agent can recover the schedule after the human gate (the SDK
resumes HITL chains with only the human meta fields).

AUTO-RELEASE is granted ONLY when no floor fires AND every conflict is
below severity 0.5 — the clean re-opt path straight to MES.
"""
from __future__ import annotations

from typing import Any

from leafmesh import LeafMeshLogger, pre_compose

from agency._shared import store
from agency._shared.session_stash import resolve_session_id, stash_get, stash_put

logger = LeafMeshLogger(__name__)

# Effective hours per unit, line-agnostic dev approximation, used to turn
# a schedule's unit load into hours for the capacity floor. PROD: read
# per-line units_per_hour from the capacity picture (already in store).
_HOURS_PER_UNIT = 1.0 / 55.0   # ~ slowest line's rate; conservative


def _up(input_data: dict[str, Any], key: str, default: Any = None) -> Any:
    """Upstream read: flat input_data, then upstream_yields (flat or
    keyed by agent name, e.g. {"upstream_yields": {"scheduler_agent": {...}}})."""
    if input_data.get(key) not in (None, ""):
        return input_data[key]
    uy = input_data.get("upstream_yields")
    if isinstance(uy, dict):
        if uy.get(key) not in (None, ""):
            return uy[key]
        for v in uy.values():
            if isinstance(v, dict) and v.get(key) not in (None, ""):
                return v[key]
    return default


def _prepared(context: Any) -> dict[str, Any]:
    if isinstance(context, dict) and isinstance(context.get("prepared_data"), dict):
        return context["prepared_data"]
    return {}


def load_plant_state(data: Any, ctx: Any) -> dict[str, Any]:
    """Pull the real material + capacity picture the floors enforce against."""
    plant_id = str((data or {}).get("plant_id") or "") if isinstance(data, dict) else ""
    return {
        "materials_on_hand": store.on_hand(),
        "capacity": store.line_capacity(plant_id or store.PLANT),
    }


def _scheduled_skus(input_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Demand the schedule commits to producing — drives the material +
    capacity floors. Reads scheduler sequenced_orders (sku, units)."""
    orders = _up(input_data, "sequenced_orders", []) or []
    out: list[dict[str, Any]] = []
    for o in orders:
        if isinstance(o, dict) and o.get("sku"):
            out.append({"sku": str(o["sku"]), "units": float(o.get("units") or 0)})
    return out


def compute_gate(input_data: dict[str, Any], prepared: dict[str, Any]) -> dict[str, Any]:
    """The three deterministic floors → the gate verdict. Pure; testable
    directly. `prepared` may be empty (falls back to the store)."""
    conflicts: list[dict[str, Any]] = []
    safety_breach = False

    # ── Floor 1: safety-stock / severity over scheduler unmet_demand ──
    for u in _up(input_data, "unmet_demand", []) or []:
        if not isinstance(u, dict):
            continue
        reason = str(u.get("reason") or "").lower()
        severity = 0.6
        if "safety_stock" in reason:
            severity = 1.0
            safety_breach = True
        elif "shortage" in reason or "supplier" in reason:
            severity = 0.8
        conflicts.append({
            "type": "unmet_demand", "severity": severity,
            "summary": f"{u.get('sku', '?')}: short {u.get('units_short', 0)} ({reason or 'unspecified'})",
        })

    scheduled = _scheduled_skus(input_data)

    # ── Floor 2: material-availability (explode SKUs × BOM vs on-hand) ──
    shortfalls = store.material_shortfalls([{"sku": s["sku"], "units": s["units"]} for s in scheduled])
    for sf in shortfalls:
        safety_breach = True
        conflicts.append({
            "type": "material_shortfall", "severity": 1.0,
            "summary": f"{sf['material']}: short {sf['shortfall']} "
                       f"(would breach safety stock; supplier {sf.get('supplier_id', '?')})",
        })

    # ── Floor 3: capacity (load vs effective capacity) ──
    cap = prepared.get("capacity") if isinstance(prepared.get("capacity"), dict) else None
    if cap is None:
        cap = store.line_capacity()
    scheduled_units = sum(s["units"] for s in scheduled)
    scheduled_hours = scheduled_units * _HOURS_PER_UNIT
    overrun = store.capacity_overrun(scheduled_hours) if scheduled_units else {"exceeds_capacity": False, "overrun_hours": 0}
    if overrun.get("exceeds_capacity"):
        conflicts.append({
            "type": "capacity_overrun", "severity": 0.8,
            "summary": f"schedule load {overrun.get('scheduled_hours')}h exceeds "
                       f"effective capacity {overrun.get('effective_capacity_hours')}h "
                       f"by {overrun.get('overrun_hours')}h",
        })

    # ── Verdict — floors the LLM can't waive ──
    requires_planner = bool(safety_breach) or any(c["severity"] >= 0.7 for c in conflicts)
    can_auto_release = (not requires_planner) and (not conflicts or all(c["severity"] < 0.5 for c in conflicts))

    return {
        "conflicts": conflicts,
        "safety_stock_breach": safety_breach,
        "requires_planner_review": requires_planner,
        "can_auto_release": can_auto_release,
    }


def persist_verdict(input_data: dict[str, Any], context: Any, verdict: dict[str, Any],
                    schedule_id: str) -> None:
    """Merge the gate verdict into the per-session stash over whatever the
    scheduler already stashed, so release_agent recovers the full schedule
    after the human gate. Called from the body (which has input_data, hence
    the session_id); the SDK does not re-deliver input_data to a @chain fn."""
    session_id = resolve_session_id(input_data, context if isinstance(context, dict) else {})
    stashed = stash_get(session_id)
    if schedule_id:
        stashed["schedule_id"] = schedule_id
    stashed.update({
        "conflicts": verdict.get("conflicts", []),
        "safety_stock_breach": verdict.get("safety_stock_breach", False),
        "requires_planner_review": verdict.get("requires_planner_review", False),
        "can_auto_release": verdict.get("can_auto_release", False),
    })
    stash_put(session_id, stashed)


@pre_compose(context_processor=load_plant_state)
async def constraint_flagger_agent(llm_response, input_data, context):
    input_data = input_data if isinstance(input_data, dict) else {}
    verdict = compute_gate(input_data, _prepared(context))

    # Identity passthrough — the planner inbox renders schedule_id so the
    # human sees WHICH schedule the conflicts belong to. Recover from the
    # stash if the upstream didn't carry it.
    schedule_id = str(_up(input_data, "schedule_id", "") or "")
    if not schedule_id:
        session_id = resolve_session_id(input_data, context if isinstance(context, dict) else {})
        schedule_id = str(stash_get(session_id).get("schedule_id") or "")

    persist_verdict(input_data, context, verdict, schedule_id)

    logger.info(
        f"[constraint] {schedule_id or '?'} conflicts={len(verdict['conflicts'])} "
        f"safety={verdict['safety_stock_breach']} planner={verdict['requires_planner_review']} "
        f"auto_release={verdict['can_auto_release']}"
    )
    return {**verdict, "schedule_id": schedule_id}
