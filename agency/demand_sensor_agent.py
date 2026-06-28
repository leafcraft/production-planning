"""demand_sensor_agent — SELF-RELIANT demand planner.

@pre_compose  load_demand — pull the planning-ready demand picture
              (SKU units, due dates, segments, notable shifts) from the
              plant's demand systems BEFORE the LLM runs, so it reasons
              over the real forecast instead of being spoon-fed. DEV: the
              seeded store; PROD: the forecast service / ERP demand view.

No @chain floor here — demand sensing is judgment over text, and the
deterministic floors live downstream at the auto-release gate. The
module exists purely to make this agent pull its own context.
"""
from __future__ import annotations

from typing import Any

from leafmesh import LeafMeshLogger, pre_compose

from agency._shared import store

logger = LeafMeshLogger(__name__)


def load_demand(data: Any, ctx: Any) -> dict[str, Any]:
    plant_id = ""
    if isinstance(data, dict):
        plant_id = str(data.get("plant_id") or "")
        payload = data.get("payload")
        if not plant_id and isinstance(payload, dict):
            plant_id = str(payload.get("plant_id") or "")
    picture = store.demand_picture(plant_id or store.PLANT)
    logger.info(
        f"[demand_sensor] pulled {len(picture.get('sku_demands') or [])} SKU demands "
        f"for plant={plant_id or store.PLANT}"
    )
    return {
        "demand_picture": picture,
        "sku_demands": picture.get("sku_demands", []),
        "planning_window_days": picture.get("planning_window_days", 14),
        "notable_shifts": picture.get("notable_shifts", []),
    }


@pre_compose(context_processor=load_demand)
async def demand_sensor_agent(llm_response: Any, input_data: Any, context: Any) -> Any:
    # Pure pass-through of the LLM's structured demand picture. The
    # OUTPUT CONTRACT + enforce_yields in YAML shape the result; the
    # only job this module owns is the pre_compose context pull above.
    return llm_response
