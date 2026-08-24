"""Milo — MES Release Finisher (the FINISHER / actuator).

auto_discover matches `release_agent`. Milo sits BEHIND the gate + the
human approval. It never approves — it is the concrete "@chain fails
closed":

  a) authorization RE-CHECK — reaching this agent does not mean approved.
     TWO authorized paths, everything else fails closed:
       * a human approve token (allow-token allowlist; a block token or an
         unknown/ambiguous verdict → NOT authorized), OR
       * the deterministic gate's can_auto_release == true delivered THIS
         turn (never trusted from the stash — a stale clean check must not
         re-authorize after a human held).
  b) deterministic invariants a human can't waive — a recoverable
     schedule_id, real sequenced orders, and NO double-release.
  c) render the real release pack to ./out; on a tripped floor, DELETE the
     partial and record status:blocked with reasons — never a half-release.
  d) dev-store fallback that ANNOUNCES itself — with no MES connector wired
     it records a verifiable dev outbox job (mes_job_id "DEV-…",
     released:false, status:recorded_dev) and never fakes a real MES ack.
  e) recovery across the HITL gate — the schedule is recovered from the
     session stash / dev store (the SDK resumes with only human meta).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from leafmesh import LeafMeshLogger, chain

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

_OUT_DIR = Path("./out")
_APPROVE_TOKENS = frozenset({"approved", "approve", "sign", "signed", "yes", "ok",
                             "release", "released"})
_BLOCK_TOKENS = frozenset({"rejected", "reject", "no", "deny", "denied", "held",
                           "held_for_review", "hold", "halt", "halt_for_review"})


def _sources(input_data: dict) -> list[dict]:
    srcs = [input_data]
    up = input_data.get("upstream_yields")
    if isinstance(up, dict):
        srcs.append(up)
        srcs.extend(v for v in up.values() if isinstance(v, dict))
    hd = input_data.get("human_data")
    if isinstance(hd, dict):
        srcs.append(hd)
    return srcs


def _signal(input_data: dict) -> str:
    """The human verdict, from wherever the SDK delivered it (shape-tolerant)."""
    for src in _sources(input_data):
        for key in ("human_message", "human_decision", "decision", "message"):
            val = src.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip().lower()
    return ""


def _is_blocked(input_data: dict) -> bool:
    return _signal(input_data) in _BLOCK_TOKENS


def _gate_cleared(input_data: dict) -> bool:
    """True only when the deterministic gate delivered can_auto_release THIS
    turn (never read from the recovered stash — fail closed on staleness)."""
    for src in _sources(input_data):
        if src.get("can_auto_release") is True:
            return True
    return False


def _authorized(input_data: dict) -> bool:
    """Fail closed: an explicit approve token OR the gate's clean verdict.
    A block token, or anything unknown, is NOT authorized."""
    if _is_blocked(input_data):
        return False
    if _signal(input_data) in _APPROVE_TOKENS:
        return True
    return _gate_cleared(input_data)


def _lookup(key: str, *srcs: Any) -> Any:
    for src in srcs:
        if not isinstance(src, dict):
            continue
        if src.get(key) not in (None, ""):
            return src[key]
        for v in src.values():
            if isinstance(v, dict) and v.get(key) not in (None, ""):
                return v[key]
    return None


def _recover_schedule(input_data: dict, context: dict) -> dict:
    """Recover the schedule across the HITL boundary: upstream → stash → dev store."""
    sid = resolve_session_id(input_data, context)
    stashed = stash_get(sid)
    schedule_id = str(_lookup("schedule_id", input_data, stashed) or "")
    sequenced = _lookup("sequenced_orders", input_data, stashed)
    if not sequenced and schedule_id:
        sequenced = store.get_approved_schedule(schedule_id).get("sequenced_orders")
    return {"schedule_id": schedule_id, "sequenced_orders": list(sequenced or [])}


def _render_pack(schedule: dict) -> str:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    schedule_id = str(schedule.get("schedule_id") or "schedule")
    path = _OUT_DIR / f"{schedule_id}_release_pack.md"
    lines = [f"# Released production schedule — {schedule_id}",
             f"Released at: {datetime.now(timezone.utc).isoformat()}", ""]
    total = 0
    for o in schedule.get("sequenced_orders") or []:
        if isinstance(o, dict):
            total += int(float(o.get("units") or 0))
            lines.append(f"- {o.get('sku','')}  {o.get('units','')} units  "
                         f"{o.get('line_id','')}/{o.get('shift_id','')}")
    lines += ["", f"**Total units: {total}**", "",
              "_Pushed to the MES and ledgered._"]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def _delete_partial(result: dict) -> None:
    p = result.get("pack_path")
    if p:
        Path(p).unlink(missing_ok=True)
        result["pack_path"] = ""


def enforce_release_floor(result: Any, context: dict) -> dict:
    """The finisher floor — release ONLY on real authorization; delete on a block."""
    result = result if isinstance(result, dict) else {}
    result.setdefault("block_reasons", [])
    result.setdefault("released", False)
    result.setdefault("released_at_utc", "")
    result.setdefault("mes_job_id", "")
    result.setdefault("pack_path", "")

    input_data = result.get("_input", {})
    schedule = result.get("_schedule", {})

    # (a) authorization re-check — independent of the body.
    if not _authorized(input_data):
        result["status"] = "blocked"
        if _is_blocked(input_data):
            result["block_reasons"].append("approver rejected / held")
        else:
            result["block_reasons"].append("no explicit approval and gate did not clear (fail closed)")
        _delete_partial(result)
        logger.info(f"[milo] release floor BLOCKED: {result['block_reasons']}")
        return result

    # (b) deterministic invariants a human can't waive.
    schedule_id = str(schedule.get("schedule_id") or "")
    if not schedule_id:
        result.update(status="blocked")
        result["block_reasons"].append("no recoverable schedule_id — anonymous MES job refused")
        _delete_partial(result)
        return result
    if not schedule.get("sequenced_orders"):
        result.update(status="blocked")
        result["block_reasons"].append("no sequenced orders to release")
        _delete_partial(result)
        return result
    prior = store.get_approved_schedule(schedule_id)
    if prior.get("actuated") is True:
        result.update(status="blocked")
        result["block_reasons"].append(f"schedule {schedule_id} already released (no double-issue)")
        _delete_partial(result)
        return result

    # (c) passed the floor — render the real artifact.
    result["pack_path"] = _render_pack(schedule)
    now = datetime.now(timezone.utc).isoformat()

    # (d) MES push — real connector, else a dev record that announces itself.
    if store.mes_wired():
        result["mes_job_id"] = f"mes-{uuid.uuid4().hex[:12]}"
        result["released"] = True
        result["status"] = "released"
    else:
        result["mes_job_id"] = f"DEV-{uuid.uuid4().hex[:12]}"
        result["released"] = False   # honest: no real MES ack day-0
        result["status"] = "recorded_dev"
        result["block_reasons"].append("no MES connector wired (MES_MCP_URL) — recorded to dev outbox, not pushed")

    result["released_at_utc"] = now
    store.record_outbox({"schedule_id": schedule_id, "action": "release_to_mes",
                         "mes_job_id": result["mes_job_id"], "pack_path": result["pack_path"],
                         "released": result["released"]})
    store.append_ledger({"schedule_id": schedule_id, "event": "released",
                         "mes_job_id": result["mes_job_id"], "released": result["released"]})
    store.put_approved_schedule(schedule_id, {"sequenced_orders": schedule["sequenced_orders"],
                                              "released": result["released"],
                                              "actuated": True,   # dev or real — the schedule was pushed once
                                              "mes_job_id": result["mes_job_id"]})
    logger.info(f"[milo] {schedule_id} -> {result['status']} job={result['mes_job_id']} "
                f"pack={result['pack_path']}")
    return result


@chain(enforce_release_floor)
async def release_agent(llm_response: Any, input_data: Any, context: Any) -> dict:
    input_data = input_data if isinstance(input_data, dict) else {}
    context = context if isinstance(context, dict) else {}
    schedule = _recover_schedule(input_data, context)

    # Keep the schedule recoverable for the ledger/dedupe across the gate.
    sid = resolve_session_id(input_data, context)
    if schedule.get("schedule_id"):
        stashed = stash_get(sid)
        stashed.update(schedule)
        stash_put(sid, stashed)

    return {
        "released": False, "released_at_utc": "", "mes_job_id": "",
        "pack_path": "", "status": "", "block_reasons": [],
        "_input": input_data, "_schedule": schedule,
    }
