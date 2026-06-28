"""supplier_signal_agent — SELF-RELIANT supplier-risk analyst.

@pre_compose  load_supplier_context — pull the supplier master (reliability,
              alternates) + the current schedule the slip would hit, so the
              LLM reasons over real lead times and alternates instead of
              guessing. DEV store; PROD: ERP vendor master + MES schedule.

@chain        enforce_review_floor — DETERMINISTIC floor the LLM can't
              waive. The hard rules were narrated in the prompt before; an
              LLM that under-rates a single-source slip would silently
              auto-replan. This re-derives requires_planner_review from the
              shaped result + the supplier master and FORCES it on:
                (a) force_majeure,
                (b) severity >= 0.5,
                (c) severity >= 0.3 with no alternate supplier options,
                (d) a single-source supplier (no alternates in the master),
              regardless of what the model returned.
"""
from __future__ import annotations

import json
import re
from typing import Any

from leafmesh import LeafMeshLogger, chain, pre_compose

from agency._shared import store

logger = LeafMeshLogger(__name__)


def _input(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    sig = data.get("supplier_signal")
    if isinstance(sig, dict):
        return sig
    return data


def load_supplier_context(data: Any, ctx: Any) -> dict[str, Any]:
    sig = _input(data)
    supplier_id = str(sig.get("supplier_id") or "")
    master = store.suppliers()
    rec = master.get(supplier_id, {})
    logger.info(
        f"[supplier_signal] pulled master for {supplier_id or '?'} "
        f"(alternates={rec.get('alternates')})"
    )
    return {
        "supplier_master": master,
        "supplier_record": rec,
        "alternates": rec.get("alternates", []),
        "current_schedule": store.current_schedule(),
        "materials_on_hand": store.on_hand(),
    }


def _parse(resp: Any) -> dict[str, Any]:
    if isinstance(resp, dict):
        return resp
    if isinstance(resp, str):
        try:
            return json.loads(resp)
        except ValueError:
            m = re.search(r"\{.*\}", resp, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except ValueError:
                    pass
    return {}


def apply_review_floor(result: dict[str, Any], supplier_master: dict[str, Any]) -> dict[str, Any]:
    """Deterministic hard rules → requires_planner_review. Pure; testable."""
    result = dict(result) if isinstance(result, dict) else {}
    try:
        severity = float(result.get("severity") or 0)
    except (TypeError, ValueError):
        severity = 0.0
    signal_type = str(result.get("signal_type") or "").lower()
    alternates_in_result = list(result.get("alternate_supplier_options") or [])

    supplier_id = str(result.get("supplier_id") or "")
    master_rec = (supplier_master or {}).get(supplier_id, {})
    single_source = not (master_rec.get("alternates") or [])

    forced = (
        signal_type == "force_majeure"
        or severity >= 0.5
        or (severity >= 0.3 and not alternates_in_result)
        or (single_source and severity > 0)
    )
    # Floor only RAISES the gate — never lowers a model that already escalated.
    result["requires_planner_review"] = bool(result.get("requires_planner_review")) or forced
    return result


async def enforce_review_floor(result: Any, ctx: Any) -> Any:
    if not isinstance(result, dict):
        result = _parse(result)
    floored = apply_review_floor(result, store.suppliers())
    if floored.get("requires_planner_review") and not (result.get("requires_planner_review")):
        logger.warning(
            f"[supplier_signal] FLOOR forced planner review for "
            f"{floored.get('supplier_id', '?')} (severity={floored.get('severity')}, "
            f"type={floored.get('signal_type')})"
        )
    return floored


@pre_compose(context_processor=load_supplier_context)
@chain(enforce_review_floor)
async def supplier_signal_agent(llm_response: Any, input_data: Any, context: Any) -> Any:
    # The LLM produces the shaped supplier assessment (OUTPUT CONTRACT in
    # YAML); the @chain re-derives requires_planner_review deterministically.
    return _parse(llm_response)
