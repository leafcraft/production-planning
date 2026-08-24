"""Gaia — Auto-Release Gate. THE DETERMINISTIC gate, never an LLM.

auto_discover matches `constraint_flagger_agent`. The auto-release
decision is irreversible plant action, so it is reproducible arithmetic a
model may not waive (doctrine: controls stay separate, code decides who
acts). This module enforces THREE deterministic floors over the
scheduler's output and the plant's real material/capacity picture:

  1. SAFETY-STOCK / SEVERITY — over scheduler `unmet_demand`:
       reason contains "safety_stock" → severity 1.0 + breach;
       "shortage"/"supplier" → 0.8; else 0.6.
  2. MATERIAL-AVAILABILITY — explode scheduled SKUs × BOM against on-hand
       minus safety stock; any material that would breach its safety-stock
       floor is a safety_stock_breach. The plant cannot run a schedule it
       has no material for.
  3. CAPACITY — a schedule whose load exceeds effective capacity physically
       cannot run → high-severity conflict.

AUTO-RELEASE only when no floor fires AND every conflict is below severity
0.5. It also merges the verdict into the per-session stash so the finisher
recovers the schedule after the human gate.
"""
from __future__ import annotations

from typing import Any

from leafmesh import LeafMeshLogger

try:
    from ._shared import store
    from ._shared.session_stash import resolve_session_id, stash_get, stash_put
except ImportError:  # pragma: no cover
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import store  # type: ignore
    from _shared.session_stash import resolve_session_id, stash_get, stash_put  # type: ignore

logger = LeafMeshLogger(__name__)

# Effective hours per unit — line-agnostic dev approximation to turn a unit
# load into hours for the capacity floor. PROD: per-line units_per_hour.
_HOURS_PER_UNIT = 1.0 / 55.0   # ~ slowest line's rate; conservative


def _up(input_data: dict[str, Any], key: str, default: Any = None) -> Any:
    """Upstream read: flat input_data, then upstream_yields (flat or keyed
    by agent name, e.g. {"upstream_yields": {"scheduler_agent": {...}}})."""
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


def _scheduled_skus(input_data: dict[str, Any], stashed: dict[str, Any]) -> list[dict[str, Any]]:
    """Demand the schedule commits to — drives the material + capacity floors."""
    orders = _up(input_data, "sequenced_orders", None)
    if not orders:
        orders = stashed.get("sequenced_orders") or []
    out: list[dict[str, Any]] = []
    for o in orders or []:
        if isinstance(o, dict) and o.get("sku"):
            out.append({"sku": str(o["sku"]), "units": float(o.get("units") or 0)})
    return out


def compute_gate(input_data: dict[str, Any], stashed: dict[str, Any] | None = None) -> dict[str, Any]:
    """The three deterministic floors → the gate verdict. Pure; testable."""
    stashed = stashed or {}
    conflicts: list[dict[str, Any]] = []
    safety_breach = False

    # ── Floor 1: safety-stock / severity over scheduler unmet_demand ──
    unmet = _up(input_data, "unmet_demand", None)
    if not unmet:
        unmet = stashed.get("unmet_demand") or []
    for u in unmet or []:
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

    scheduled = _scheduled_skus(input_data, stashed)

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


async def constraint_flagger_agent(llm_response: Any, input_data: Any, context: Any) -> dict:
    input_data = input_data if isinstance(input_data, dict) else {}
    session_id = resolve_session_id(input_data, context if isinstance(context, dict) else {})
    stashed = stash_get(session_id)

    verdict = compute_gate(input_data, stashed)

    schedule_id = str(_up(input_data, "schedule_id", "") or stashed.get("schedule_id") or "")

    # Merge the verdict into the stash so the finisher recovers the full
    # schedule + the gate decision after the human gate.
    if schedule_id:
        stashed["schedule_id"] = schedule_id
    stashed.update({
        "conflicts": verdict["conflicts"],
        "safety_stock_breach": verdict["safety_stock_breach"],
        "requires_planner_review": verdict["requires_planner_review"],
        "can_auto_release": verdict["can_auto_release"],
    })
    stash_put(session_id, stashed)

    logger.info(
        f"[gaia] {schedule_id or '?'} conflicts={len(verdict['conflicts'])} "
        f"safety={verdict['safety_stock_breach']} planner={verdict['requires_planner_review']} "
        f"auto_release={verdict['can_auto_release']}"
    )
    return {**verdict, "schedule_id": schedule_id}
