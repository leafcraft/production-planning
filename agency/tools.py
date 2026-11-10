"""Global tools for the production-planning pod.

These are demand_sensor_agent's day-0 tool surface — the tools it calls,
in the turn, to CLAIM a line and PULL that line's demand + capacity. Each
reads the dev store (seed plant snapshot) so the pod runs with no
connector wired, and **each announces itself**: when there is no real
source and no seed it returns `available: false` and says why — never a
synthetic success that looks like a real read.

`claim_line` is the ATOMIC claim that makes `scaling.copies.count: N` fan out across
the plant's lines without duplicating work: each copy of demand_sensor
claims one line, so N copies read N disjoint lines. To go live, create the
vault records 'mes_mcp' / 'erp_mrp_mcp' and wire the MES/ERP `mcp:` block in
Studio's raw YAML editor (each named
action becomes its own tool); these dev tools are the fallback contract. They
check those vault records themselves; nothing is read from .env.
"""
import os

from leafmesh import global_tool, LeafMeshLogger

try:
    from ._shared import store
    from ._shared.wiring import vault_record_exists
except ImportError:  # pragma: no cover — direct-run fallback
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import store  # type: ignore
    from _shared.wiring import vault_record_exists  # type: ignore

logger = LeafMeshLogger(__name__)


async def _connector_wired() -> bool:
    """True once the vault record 'mes_mcp' or 'erp_mrp_mcp' exists
    (Infrastructure → Integrations → MCP)."""
    return await vault_record_exists("mes_mcp") or await vault_record_exists("erp_mrp_mcp")


@global_tool(category="planning")
def claim_line(plant_id: str = "") -> dict:
    """Atomically claim the next unplanned production line for a plant.

    Call this ONCE at the start of your turn. It hands each parallel copy
    of the demand sensor a DIFFERENT line so the copies split the plant's
    lines instead of all reading the same one. Returns the claimed
    `line_id`, or `available: false` when no lines remain to claim.

    Args:
        plant_id: The plant to claim a line from (e.g. "PLANT-PDX").
    """
    pid = str(plant_id).strip() or store.PLANT
    result = store.claim_line(pid)
    logger.info(f"claim_line plant={pid} -> {result.get('line_id') or result.get('reason')}")
    return result


@global_tool(category="planning")
async def read_line_demand(plant_id: str, line_id: str) -> dict:
    """Read the demand (SKU units, due dates, segments) a line is
    responsible for over the planning window.

    Args:
        plant_id: The plant (e.g. "PLANT-PDX").
        line_id: The line you claimed (e.g. "LINE-1").
    """
    pid = str(plant_id).strip() or store.PLANT
    result = store.read_line_demand(pid, str(line_id).strip())
    if not result.get("available") and await _connector_wired():
        return {"available": False,
                "reason": "MES/ERP vault record exists, but this dev tool reads only the seed; "
                          "use the mcp tools for this line"}
    return result


@global_tool(category="planning")
async def read_line_capacity(plant_id: str, line_id: str) -> dict:
    """Read a line's effective capacity hours, bottleneck flag and
    maintenance windows over the planning window.

    Args:
        plant_id: The plant (e.g. "PLANT-PDX").
        line_id: The line you claimed (e.g. "LINE-1").
    """
    pid = str(plant_id).strip() or store.PLANT
    result = store.read_line_capacity(pid, str(line_id).strip())
    if not result.get("available") and await _connector_wired():
        return {"available": False,
                "reason": "MES/ERP vault record exists, but this dev tool reads only the seed; "
                          "use the mcp tools for this line"}
    return result


@global_tool(category="planning")
def list_prior_schedules(plant_id: str = "") -> dict:
    """List prior schedule ids for a plant, to spot a duplicate or a
    regression against what recently ran.

    Args:
        plant_id: The plant (e.g. "PLANT-PDX").
    """
    pid = str(plant_id).strip() or store.PLANT
    return store.list_prior_schedules(pid)
