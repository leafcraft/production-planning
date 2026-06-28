"""scheduler_agent — SELF-RELIANT production scheduler + pre-gate stash.

@pre_compose  load_planning_context — pull what the scheduler needs to
              re-optimise: line/shift capacity, the current schedule on
              the floor, and the BOM/materials picture. The fan-in
              (demand AND capacity) is wired in YAML; this gives the LLM
              the *full* operating picture instead of being spoon-fed.

@chain        stash_schedule — DETERMINISTIC. When conflicts route the
              chain through planner_human / plant_manager_human, the SDK
              resumes with ONLY the human-response meta fields — the
              schedule flowing through the chain is NOT re-delivered. This
              stashes the full schedule keyed by session so release_agent
              can recover it after the human gate, and mints a schedule_id
              if the model omitted one.

Everything else (briefing synthesis, clamping, JSON glue) lives in the
YAML prompt + lenient yields enforcement — not here.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from leafmesh import LeafMeshLogger, chain, pre_compose

from agency._shared import store
from agency._shared.session_stash import resolve_session_id, stash_put

logger = LeafMeshLogger(__name__)


def load_planning_context(data: Any, ctx: Any) -> dict[str, Any]:
    plant_id = ""
    if isinstance(data, dict):
        plant_id = str(data.get("plant_id") or "")
    plant_id = plant_id or store.PLANT
    capacity = store.line_capacity(plant_id)
    current = store.current_schedule(plant_id)
    logger.info(
        f"[scheduler] pulled capacity (eff={capacity.get('effective_capacity_hours')}h, "
        f"bottlenecks={capacity.get('bottleneck_lines')}) + current schedule "
        f"{current.get('schedule_id')} for plant={plant_id}"
    )
    return {
        "capacity": capacity,
        "current_schedule": current,
        "bom": store.bom(),
        "materials_on_hand": store.on_hand(),
    }


async def stash_schedule(result: Any, ctx: Any) -> Any:
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            result = {}
    if not isinstance(result, dict):
        result = {}

    schedule = dict(result)
    if not schedule.get("schedule_id"):
        schedule["schedule_id"] = f"sch-{uuid.uuid4().hex[:12]}"

    session_id = resolve_session_id(schedule, ctx if isinstance(ctx, dict) else {})
    stash_put(session_id, schedule)
    logger.info(
        f"[scheduler] stashed {schedule['schedule_id']} (session={session_id or '?'})"
    )
    return schedule


@pre_compose(context_processor=load_planning_context)
@chain(stash_schedule)
async def scheduler_agent(llm_response: Any, input_data: Any, context: Any) -> Any:
    # The pre_compose pull + the chain stash are the two jobs Python owns.
    # The LLM's structured schedule passes through to the chain, which
    # mints the id and stashes it for HITL recovery.
    return llm_response
