"""production-planning — Production Planning (YAML-first).

The mesh is defined in configs/config.yaml. Capacity, MRP and KPI data
come from YOUR systems via the MCP connectors declared there (MES,
ERP/MRP, MES historian). Day-0 (no connectors) the agency modules pull
their context from the seeded dev store (agency/_shared/store.py) so the
FULL chain runs end-to-end. The Python that stays:
  * demand_sensor / scheduler / supplier_signal — @pre_compose self-pull
  * constraint_flagger — the deterministic auto-release gate (3 floors)
  * supplier_signal — @chain review floor (LLM can't under-rate a slip)
  * scheduler — pre-gate stash; release_agent — fail-closed MES actuator
The agency modules auto-bind by filename (auto_discover); only the custom
WORM audit sink needs explicit registration below.
"""
import asyncio
import signal
import sys

from dotenv import load_dotenv
from leafmesh import EventType, LeafMesh, LeafMeshLogger

from agency._shared import audit_logger as _audit_logger

load_dotenv()
logger = LeafMeshLogger(__name__)
leafmesh = LeafMesh.from_yaml("configs/config.yaml")

# Register the WORM audit sink as the intelligence function for the
# custom external planning_audit_logger_agent (SDK 2.4+ API).
_audit_logger.register(leafmesh, "planning_audit_logger_agent")


async def _on_agent_error(event):
    logger.warning(f"[plan-audit] AGENT_ERROR session={event.session_id}")

async def _on_manager_intervention(event):
    logger.warning(f"[plan-ops] MANAGER_INTERVENTION session={event.session_id}")

async def _on_intervention_needed(event):
    logger.error(f"[plan-ops] INTERVENTION_NEEDED session={event.session_id}")

async def _on_human_input_timeout(event):
    logger.warning(f"[plan-audit] HITL TIMEOUT session={event.session_id}")


async def main():
    await leafmesh.start()
    leafmesh.event_bus.subscribe(EventType.AGENT_ERROR,           _on_agent_error)
    leafmesh.event_bus.subscribe(EventType.MANAGER_INTERVENTION,  _on_manager_intervention)
    leafmesh.event_bus.subscribe(EventType.INTERVENTION_NEEDED,   _on_intervention_needed)
    leafmesh.event_bus.subscribe(EventType.HUMAN_INPUT_TIMEOUT,   _on_human_input_timeout)
    try:
        await leafmesh.enable_self_healing()
    except AttributeError:
        pass

    logger.info("production-planning — Production Planning mesh running")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
    else:
        try: await stop.wait()
        except KeyboardInterrupt: pass

    await leafmesh.stop()


if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
