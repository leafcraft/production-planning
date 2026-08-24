"""Rex — Production Scheduler (Super-Agent) + the cross-check floor.

auto_discover matches `scheduler_agent`. Rex (an llm + super_agent) plans,
executes and verifies a multi-line re-optimisation. The MODEL sequences;
this CODE fails closed and bounds the retry.

@chain enforce_cross_check_floor — the deterministic floor:
  a) CROSS-CHECK — every order must carry a sku, units and a line; if it
     does not tie out, return to Sensa with the specific discrepancy —
     never adjust a number to make it fit.
  b) BOUNDED back-edge — the re-sense loop is counted per session; once the
     bound is spent, reopt_retry_exhausted flips true and the wire routes to
     the Ops Director instead of looping forever (Rule 3).
  c) PRE-GATE STASH — when conflicts route the chain through a human, the
     SDK resumes with only human meta fields; the full schedule is stashed
     per session so the finisher can recover it after the gate. A clean
     schedule also renders a draft to ./out for the planner to look at.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from leafmesh import LeafMeshLogger, chain

try:
    from ._shared.session_stash import resolve_session_id, stash_get, stash_put
except ImportError:  # pragma: no cover
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared.session_stash import resolve_session_id, stash_get, stash_put  # type: ignore

logger = LeafMeshLogger(__name__)

_OUT_DIR = Path("./out")
_MAX_REOPT_ATTEMPTS = 1   # re-sense at most once, then escalate — never loop


def _render_draft(result: dict) -> str:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    schedule_id = str(result.get("schedule_id") or "schedule")
    path = _OUT_DIR / f"{schedule_id}_draft_schedule.md"
    lines = [f"# Draft production schedule — {schedule_id}",
             f"On-time delivery: {result.get('on_time_delivery_pct')}", ""]
    for o in result.get("sequenced_orders") or []:
        if isinstance(o, dict):
            lines.append(f"- {o.get('sku','')}  {o.get('units','')} units  "
                         f"{o.get('line_id','')}/{o.get('shift_id','')}  due {o.get('due_date','')}")
    lines += ["", "_Draft — awaiting the auto-release gate / planner review._"]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def _cross_check(result: dict) -> str:
    """Return a discrepancy string, or '' if the schedule ties out."""
    orders = result.get("sequenced_orders")
    if not isinstance(orders, list) or not orders:
        return "no orders sequenced"
    for o in orders:
        if not isinstance(o, dict):
            return "a sequenced order is malformed"
        if not o.get("sku"):
            return "a sequenced order is missing its sku"
        if o.get("units") in (None, "", 0):
            return f"{o.get('sku')}: sequenced order is missing a unit count"
        if not o.get("line_id"):
            return f"{o.get('sku')}: sequenced order is missing a line assignment"
    return ""


def enforce_cross_check_floor(result: Any, context: dict) -> dict:
    result = result if isinstance(result, dict) else {}

    # Normalise the yields contract (conditions fail closed on absence).
    result.setdefault("sequenced_orders", [])
    result.setdefault("unmet_demand", [])
    result.setdefault("planner_briefing", "")
    result.setdefault("discrepancy", "")
    try:
        result["on_time_delivery_pct"] = round(
            min(1.0, max(0.0, float(result.get("on_time_delivery_pct") or 0))), 4)
    except (TypeError, ValueError):
        result["on_time_delivery_pct"] = 0.0
    try:
        result["confidence"] = round(min(1.0, max(0.0, float(result.get("confidence") or 0))), 3)
    except (TypeError, ValueError):
        result["confidence"] = 0.0

    if not result.get("schedule_id"):
        result["schedule_id"] = f"sch-{uuid.uuid4().hex[:12]}"

    session_id = resolve_session_id(result, context if isinstance(context, dict) else {})
    stashed = stash_get(session_id)

    discrepancy = _cross_check(result)
    if discrepancy:
        attempts = int(stashed.get("reopt_attempts") or 0)
        exhausted = attempts >= _MAX_REOPT_ATTEMPTS
        result["cross_check_ok"] = False
        result["schedule_built"] = False
        result["discrepancy"] = discrepancy
        result["reopt_retry_exhausted"] = exhausted
        if not exhausted:
            stashed["reopt_attempts"] = attempts + 1
        stashed.update({"schedule_id": result["schedule_id"],
                        "sequenced_orders": result["sequenced_orders"],
                        "unmet_demand": result["unmet_demand"]})
        stash_put(session_id, stashed)
        logger.info(f"[rex] cross-check held {result['schedule_id']}: {discrepancy} "
                    f"(exhausted={exhausted})")
        return result

    # Ties out → build the draft, stash the full schedule for HITL recovery.
    result["cross_check_ok"] = True
    result["schedule_built"] = True
    result["discrepancy"] = ""
    result["reopt_retry_exhausted"] = False
    draft_path = _render_draft(result)
    stashed.update({"schedule_id": result["schedule_id"],
                    "sequenced_orders": result["sequenced_orders"],
                    "unmet_demand": result["unmet_demand"],
                    "on_time_delivery_pct": result["on_time_delivery_pct"],
                    "draft_path": draft_path})
    stash_put(session_id, stashed)
    logger.info(f"[rex] {result['schedule_id']} ties out "
                f"({len(result['sequenced_orders'])} orders) -> gate; draft {draft_path}")
    return result


@chain(enforce_cross_check_floor)
async def scheduler_agent(llm_response: Any, input_data: dict, context: dict) -> dict:
    """Return the model's re-optimised schedule; the floor re-checks + bounds it."""
    out = llm_response if isinstance(llm_response, dict) else {}
    out.setdefault("schedule_built", False)
    out.setdefault("cross_check_ok", False)
    out.setdefault("discrepancy", "")
    out.setdefault("reopt_retry_exhausted", False)
    out.setdefault("sequenced_orders", [])
    out.setdefault("on_time_delivery_pct", 0)
    out.setdefault("unmet_demand", [])
    out.setdefault("schedule_id", "")
    out.setdefault("confidence", 0)
    out.setdefault("planner_briefing", "")
    return out
