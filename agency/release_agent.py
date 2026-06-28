"""release_agent — MES ACTUATOR, kept Python on purpose.

WHY THIS MODULE EXISTS: this is the one side-effecting push to the
plant floor and it must FAIL CLOSED — a refusal path cannot live in
YAML. This agent is usually invoked by a HUMAN approval (planner / plant
manager). The SDK resumes the chain with only the human-response meta
fields, so the schedule payload is recovered from the per-session stash
that scheduler_agent / constraint_flagger_agent wrote. FAIL-CLOSED: if
no schedule can be recovered, nothing is pushed — an anonymous MES job
with no schedule_id is a floor incident, not a success.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from leafmesh import LeafMeshLogger

from agency._shared.session_stash import resolve_session_id, stash_get

logger = LeafMeshLogger(__name__)


def _lookup(key: str, *sources: Any) -> Any:
    """First non-empty value for key across dict sources (flat or one
    level deep, e.g. upstream_yields keyed by agent name)."""
    for src in sources:
        if not isinstance(src, dict):
            continue
        if src.get(key) not in (None, ""):
            return src[key]
        for v in src.values():
            if isinstance(v, dict) and v.get(key) not in (None, ""):
                return v[key]
    return None


async def release_agent(llm_response, input_data, context):
    session_id = resolve_session_id(input_data, context)
    stashed = stash_get(session_id)
    sources = (
        input_data,
        input_data.get("upstream_yields"),
        input_data.get("upstream_results"),
        input_data.get("human_data"),
        context.get("upstream") if isinstance(context, dict) else None,
        stashed,
    )

    schedule_id = str(_lookup("schedule_id", *sources) or "")
    sequenced_orders = list(_lookup("sequenced_orders", *sources) or [])

    if not schedule_id:
        logger.error(
            f"[release] REFUSING to release — no schedule_id recoverable "
            f"(session={session_id or '?'}). Pushing an anonymous job to "
            f"MES would put untraceable work on the floor."
        )
        return {
            "released_at_utc": "",
            "mes_job_id": "",
            "released": False,
            "error": "no schedule payload recoverable after approval — "
                     "scheduler/constraint_flagger stash missing for this session",
        }

    job_id = f"mes-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    logger.info(
        f"[release] {schedule_id} ({len(sequenced_orders)} orders) "
        f"→ MES job {job_id} at {now}"
    )
    return {
        "released_at_utc": now,
        "mes_job_id": job_id,
        "released": True,
        "schedule_id": schedule_id,
    }
