"""Sensa — Demand & Capacity Sensor.

auto_discover matches `demand_sensor_agent`. Sensa runs `scaling.copies.count: 3`
(set in the configuration): three parallel copies, each claims one production line via
the atomic claim_line tool and reads THAT line's demand + capacity. The
MODEL claims, reads and scores; this CODE fails closed and reshapes the
merge.

@chain enforce_picture_floor — the deterministic floor a prompt can't
waive AND the copies-merge fix:
  * confidence is a RATIO — the `copies.handoff: last` merge SUMS numbers, so a
    summed confidence is wrong. Re-average it from the per-copy pool
    (`result._instances`).
  * a SKU flagged low-confidence, or a picture with no SKUs, can never be
    marked picture_clean — a guessed demand number never reaches Rex.
"""
from __future__ import annotations

from typing import Any

from leafmesh import LeafMeshLogger, chain

logger = LeafMeshLogger(__name__)

_CONFIDENCE_MIN = 0.85


def _instance_confidences(result: dict) -> list[float]:
    """Per-copy confidence values from the copies pool (`_instances`, if any)."""
    inst = result.get("_instances")
    if not isinstance(inst, dict):
        return []
    per = inst.get("results")
    if not isinstance(per, dict):
        return []
    out: list[float] = []
    for v in per.values():
        if isinstance(v, dict) and isinstance(v.get("confidence"), (int, float)):
            out.append(float(v["confidence"]))
    return out


def enforce_picture_floor(result: Any, context: dict) -> dict:
    """Fail closed + fix the ratio-summing footgun from the copies merge."""
    result = result if isinstance(result, dict) else {}

    # ── Re-average confidence across instance copies (SUM would be wrong) ──
    confs = _instance_confidences(result)
    if confs:
        result["confidence"] = round(sum(confs) / len(confs), 3)
    else:
        try:
            result["confidence"] = round(min(1.0, max(0.0, float(result.get("confidence") or 0))), 3)
        except (TypeError, ValueError):
            result["confidence"] = 0.0

    sku_demands = result.get("sku_demands") if isinstance(result.get("sku_demands"), list) else []
    low_conf = list(result.get("low_confidence_skus") or [])

    # Deterministic re-check the model cannot talk its way past: a SKU with
    # no unit count is not a readable demand.
    for d in sku_demands:
        if not isinstance(d, dict):
            continue
        if d.get("units") in (None, "", 0):
            low_conf.append(f"{d.get('sku', 'sku')}:units")
        score = d.get("confidence")
        if isinstance(score, (int, float)) and score < _CONFIDENCE_MIN:
            low_conf.append(f"{d.get('sku', 'sku')}:confidence")

    result["low_confidence_skus"] = sorted(set(str(x) for x in low_conf))

    # picture_clean ONLY when there is something and nothing uncertain.
    result["picture_clean"] = bool(sku_demands) and not result["low_confidence_skus"]
    if not result["picture_clean"] and not result.get("notes"):
        result["notes"] = ("picture un-clean — low-confidence SKUs: "
                           + ", ".join(result["low_confidence_skus"])) if result["low_confidence_skus"] \
                          else "picture un-clean — no readable demand for the claimed line(s)"

    logger.info(f"[sensa] picture_clean={result['picture_clean']} "
                f"lines_claimed={result.get('lines_claimed')} conf={result['confidence']} "
                f"low_conf={result['low_confidence_skus']}")
    return result


@chain(enforce_picture_floor)
async def demand_sensor_agent(llm_response: Any, input_data: dict, context: dict) -> dict:
    """Return the model's sensed picture; the floor re-checks + re-averages it."""
    out = llm_response if isinstance(llm_response, dict) else {}
    input_data = input_data if isinstance(input_data, dict) else {}

    # Normalise to the yields contract so downstream conditions never read a
    # missing field (conditions fail closed on absence).
    out.setdefault("picture_clean", False)
    out.setdefault("plant_id", str(input_data.get("plant_id") or ""))
    out.setdefault("schedule_id", str(input_data.get("schedule_id") or ""))
    out.setdefault("sku_demands", [])
    out.setdefault("lines_read", [])
    out.setdefault("low_confidence_skus", [])
    out.setdefault("lines_claimed", 0)
    out.setdefault("confidence", 0)
    out.setdefault("notes", "")
    return out
