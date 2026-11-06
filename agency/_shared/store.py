"""Dev-mode store for the production-planning pod.

A DEV STAND-IN, not a system of record. It stands in for the plant's
real systems so the FULL pod runs end-to-end before any connector is
wired:
  * MES / ERP work-centre calendar → line/shift/equipment capacity
  * ERP / MRP                        → BOM + on-hand inventory
  * supplier master                 → lead times, alternates, signals
  * the current released schedule    → what's on the floor right now

Per the doctrine (where-data-lives): a real store enforces its own
invariants — this file does not, so it is dev only. **The function
signatures are the contract you swap real systems into** — the pod does
not change. Every read that has no seed **announces itself**
(`available: false` + a reason), never a confident empty result.

To go live: create the vault records 'mes_mcp' / 'erp_mrp_mcp' (Studio →
Infrastructure → Integrations → MCP), uncomment the `mcp:` block in
the configuration, and swap each read below for the matching connector
call. Creating the records alone does nothing here until the reads (and
push_release) are wired.

Single-process JSON under ./data (survives the HITL wait — the mesh is
one process). Swap for the Redis/Postgres session store at multi-replica
scale; the get/put interface stays the same.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_DIR = Path("./data")
PLANT = "PLANT-PDX"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days(n: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=n)).isoformat()


def _path(k: str) -> Path:
    _DIR.mkdir(parents=True, exist_ok=True)
    return _DIR / f"{k}.json"


def _load(k: str) -> Any:
    p = _path(k)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (ValueError, OSError):
        return None


def _save(k: str, d: Any) -> None:
    p = _path(k)
    t = p.with_suffix(".json.tmp")
    t.write_text(json.dumps(d, indent=2, default=str))
    t.replace(p)


# ── Seed data — a realistic Portland plant snapshot ──────────────────
# An industrial-controls plant: steel enclosures, controller boards, sensors
# and mounting brackets. Three lines, 14-day window. Every number reconciles:
#   * capacity — gross = shifts × 8h × 14 days per line (LINE-1 224h, LINE-2
#     224h, LINE-3 112h = 560h); LINE-2 loses one 16h planned-maintenance
#     window → effective 544h. read_line_capacity() computes the same.
#   * load — units ÷ the line's units/hour: LINE-1 13,300 u ÷ 70 = 190.0h of
#     224h (85%); LINE-2 10,200 u ÷ 55 = 185.5h of 208h (89%, the bottleneck);
#     LINE-3 3,600 u ÷ 40 = 90.0h of 112h (80%). The full plan fits.
#   * material — demand × BOM vs on-hand minus safety stock: steel 31,900 kg
#     of 33,000; resin 25,000 kg of 26,500; MCUs 10,200 of 10,700. The full
#     plan clears every floor (the happy path auto-releases), with only 500
#     MCUs of headroom — so a slip at the single-source MCU supplier, or a
#     demand spike on CTL-310, is what forces a human.
_SEED_DEMAND = {
    "plant_id": PLANT,
    "planning_window_days": 14,
    "captured_at_utc": None,                       # stamped on first read
    "sku_demands": [
        {"sku": "ENC-4410", "units": 7800, "due_date": _days(7),  "customer_segment": "oem",         "tier": "top20"},
        {"sku": "ENC-2208", "units": 5500, "due_date": _days(10), "customer_segment": "distributor", "tier": "standard"},
        {"sku": "CTL-310",  "units": 6200, "due_date": _days(5),  "customer_segment": "oem",         "tier": "top20"},
        {"sku": "SNS-120",  "units": 4000, "due_date": _days(12), "customer_segment": "distributor", "tier": "standard"},
        {"sku": "BRK-0750", "units": 3600, "due_date": _days(9),  "customer_segment": "aftermarket", "tier": "standard"},
    ],
    "notable_shifts": [
        "ENC-4410 forecast +18% vs prior period (OEM program ramp)",
        "CTL-310 pulled in 2 days (expedite request from a top-20 account)",
    ],
}

_SEED_CAPACITY = {
    "plant_id": PLANT,
    "total_capacity_hours": 560.0,                 # (2 + 2 + 1 shifts) × 8h × 14 days
    "effective_capacity_hours": 544.0,             # net of LINE-2's 16h maintenance window
    "bottleneck_lines": ["LINE-2"],
    "planned_maintenance_windows": [
        {"line_id": "LINE-2", "start_utc": _days(3), "end_utc": _days(4), "reason": "preventive"},
    ],
    "lines": [
        {"line_id": "LINE-1", "shifts": ["DAY", "SWING"], "units_per_hour": 70, "skus": ["ENC-4410", "ENC-2208"]},
        {"line_id": "LINE-2", "shifts": ["DAY", "SWING"], "units_per_hour": 55, "skus": ["CTL-310", "SNS-120"]},
        {"line_id": "LINE-3", "shifts": ["DAY"],          "units_per_hour": 40, "skus": ["BRK-0750"]},
    ],
}

# BOM: finished SKU → components (material, qty per finished unit).
# Steel and resin in kg, microcontrollers (MCU) each.
_SEED_BOM = {
    "ENC-4410": [{"material": "MAT-CRS-16GA", "qty_per_unit": 2}, {"material": "MAT-PC-RESIN", "qty_per_unit": 1}],
    "ENC-2208": [{"material": "MAT-CRS-16GA", "qty_per_unit": 1}, {"material": "MAT-PC-RESIN", "qty_per_unit": 2}],
    "CTL-310":  [{"material": "MAT-MCU-32",   "qty_per_unit": 1}, {"material": "MAT-PC-RESIN", "qty_per_unit": 1}],
    "SNS-120":  [{"material": "MAT-MCU-32",   "qty_per_unit": 1}],
    "BRK-0750": [{"material": "MAT-CRS-16GA", "qty_per_unit": 3}],
}

# On-hand inventory + safety stock floor per material.
_SEED_MATERIALS = {
    "MAT-CRS-16GA": {"description": "Cold-rolled steel sheet, 16 ga (kg)", "on_hand": 36000, "safety_stock": 3000,
                     "supplier_id": "SUP-NWCOIL", "lead_time_days": 7},
    "MAT-PC-RESIN": {"description": "Polycarbonate resin (kg)", "on_hand": 29000, "safety_stock": 2500,
                     "supplier_id": "SUP-RIDGELINE", "lead_time_days": 10},
    "MAT-MCU-32":   {"description": "32-bit microcontroller (each)", "on_hand": 11900, "safety_stock": 1200,
                     "supplier_id": "SUP-KESTREL", "lead_time_days": 21},
}

_SEED_SUPPLIERS = {
    "SUP-NWCOIL":    {"name": "Northwest Coil & Bar",  "reliability": 0.96, "alternates": ["SUP-HARBOR-STEEL"]},
    "SUP-RIDGELINE": {"name": "Ridgeline Polymers",    "reliability": 0.91, "alternates": ["SUP-BLUEFIR"]},
    "SUP-KESTREL":   {"name": "Kestrel Microdevices",  "reliability": 0.84, "alternates": []},   # single-source — slips force a human
}

# The schedule currently on the floor (what a re-opt revises). Run times tie
# to the line rates over two 8h shifts a day: CTL-310 6,200 ÷ 55 = 112.7h ≈ 7
# days; ENC-4410 7,800 ÷ 70 = 111.4h ≈ 7 days, then ENC-2208 5,500 ÷ 70 =
# 78.6h ≈ 5 days on the same line.
_SEED_SCHEDULE = {
    "schedule_id": "sch-pdx-2026-w44",
    "released_at_utc": _days(-1),
    "on_time_delivery_pct": 0.94,
    "sequenced_orders": [
        {"sku": "CTL-310",  "units": 6200, "line_id": "LINE-2", "shift_id": "DAY+SWING", "start_utc": _days(0), "end_utc": _days(7)},
        {"sku": "ENC-4410", "units": 7800, "line_id": "LINE-1", "shift_id": "DAY+SWING", "start_utc": _days(0), "end_utc": _days(7)},
        {"sku": "ENC-2208", "units": 5500, "line_id": "LINE-1", "shift_id": "DAY+SWING", "start_utc": _days(7), "end_utc": _days(12)},
    ],
}


def _seeded(key: str, seed: Any) -> Any:
    s = _load(key)
    if s is None:
        s = seed
        if isinstance(s, dict) and "captured_at_utc" in s and not s["captured_at_utc"]:
            s = {**s, "captured_at_utc": _now()}
        _save(key, s)
    return s


# ── Reads (the plant's systems of record) ────────────────────────────
def demand_picture(plant_id: str = PLANT) -> dict[str, Any]:
    """Planning-ready demand for the window. PROD: forecast service / ERP."""
    return _seeded("demand", _SEED_DEMAND)


def line_capacity(plant_id: str = PLANT) -> dict[str, Any]:
    """Line/shift/equipment availability. PROD: MES work-centre calendar."""
    return _seeded("capacity", _SEED_CAPACITY)


def bom(sku: str = "") -> dict[str, Any]:
    """Bills of material. PROD: ERP/MRP. Returns one SKU or the whole book."""
    book = _seeded("bom", _SEED_BOM)
    return {sku: book.get(sku, [])} if sku else dict(book)


def on_hand(material: str = "") -> dict[str, Any]:
    """On-hand inventory + safety stock. PROD: ERP/MRP."""
    mats = _seeded("materials", _SEED_MATERIALS)
    return {material: mats.get(material, {})} if material else dict(mats)


def suppliers() -> dict[str, Any]:
    """Supplier master (reliability, alternates). PROD: ERP vendor master."""
    return _seeded("suppliers", _SEED_SUPPLIERS)


def current_schedule(plant_id: str = PLANT) -> dict[str, Any]:
    """The schedule on the floor right now — what a re-opt revises."""
    return _seeded("schedule", _SEED_SCHEDULE)


# ── Per-line reads + atomic line claim (scaling.copies line fan-out) ──────
# demand_sensor_agent runs `scaling.copies.count: N` copies; each copy CLAIMS one
# line atomically so the copies read DISJOINT lines instead of all
# reading the same thing (see the leafmesh skill's nuances on parallel copies).
_CLAIM_LOCK = threading.Lock()
_CLAIM_STATE: dict[str, list[str]] = {}


def line_ids(plant_id: str = PLANT) -> list[str]:
    return [l["line_id"] for l in line_capacity(plant_id).get("lines", [])]


def claim_line(plant_id: str = PLANT) -> dict[str, Any]:
    """Atomically claim the next unclaimed line for the plant (LPOP-style).

    Dev stand-in for a Redis `LPOP` / `UPDATE … SKIP LOCKED RETURNING` —
    the copies of demand_sensor call this to split the line backlog. The
    backlog refills once a cycle is spent. Announces `available: false`
    when there is genuinely nothing left to claim.
    """
    with _CLAIM_LOCK:
        q = _CLAIM_STATE.get(plant_id)
        if not q:                       # refill a spent cycle
            q = line_ids(plant_id)
        if not q:
            return {"available": False, "reason": f"no lines configured for {plant_id!r}"}
        line_id = q.pop(0)
        _CLAIM_STATE[plant_id] = q
        return {"available": True, "plant_id": plant_id, "line_id": line_id, "remaining": len(q)}


def _line_record(plant_id: str, line_id: str) -> dict[str, Any] | None:
    return next((l for l in line_capacity(plant_id).get("lines", [])
                 if l.get("line_id") == line_id), None)


def read_line_demand(plant_id: str, line_id: str) -> dict[str, Any]:
    """The demand this line is responsible for (its SKUs). PROD: ERP demand view."""
    line = _line_record(plant_id, line_id)
    if not line:
        return {"available": False, "reason": f"unknown line {line_id!r} at {plant_id!r}"}
    skus = set(line.get("skus", []))
    dem = demand_picture(plant_id)
    items = [d for d in dem.get("sku_demands", []) if d.get("sku") in skus]
    return {"available": True, "plant_id": plant_id, "line_id": line_id,
            "sku_demands": items, "planning_window_days": dem.get("planning_window_days", 14)}


def read_line_capacity(plant_id: str, line_id: str) -> dict[str, Any]:
    """This line's effective capacity + maintenance windows. PROD: MES calendar."""
    line = _line_record(plant_id, line_id)
    if not line:
        return {"available": False, "reason": f"unknown line {line_id!r} at {plant_id!r}"}
    cap = line_capacity(plant_id)
    window = float(demand_picture(plant_id).get("planning_window_days", 14))
    shifts = line.get("shifts", [])
    windows = [w for w in cap.get("planned_maintenance_windows", []) if w.get("line_id") == line_id]
    gross = 8.0 * len(shifts) * window
    effective = round(max(0.0, gross - 16.0 * len(windows)), 2)   # ~1 shift lost per maint window
    return {"available": True, "plant_id": plant_id, "line_id": line_id,
            "units_per_hour": line.get("units_per_hour", 0),
            "shifts": shifts,
            "is_bottleneck": line_id in cap.get("bottleneck_lines", []),
            "effective_capacity_hours": effective,
            "maintenance_windows": windows}


def list_prior_schedules(plant_id: str = PLANT) -> dict[str, Any]:
    """Prior schedule ids for this plant, for duplicate/regression checks.
    PROD: MES release history."""
    prior = [current_schedule(plant_id).get("schedule_id", "")]
    for rec in _load("outbox") or []:
        sid = rec.get("schedule_id")
        if sid and sid not in prior:
            prior.append(sid)
    return {"available": True, "plant_id": plant_id, "prior_schedule_ids": prior}


# ── Domain math the gate floors rely on (deterministic, no LLM) ──────
def material_shortfalls(sku_demands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Explode demand × BOM against on-hand minus safety stock and return
    materials that would breach their safety-stock floor. PROD: MRP
    explosion in the ERP. This is the material-availability floor's math.

    Returns [{material, required, available_above_safety, shortfall,
              breaches_safety_stock}] for any material that comes up short.
    """
    book = bom()
    mats = on_hand()
    required: dict[str, float] = {}
    for d in sku_demands or []:
        if not isinstance(d, dict):
            continue
        units = float(d.get("units") or 0)
        for comp in book.get(str(d.get("sku") or ""), []):
            m = comp.get("material")
            required[m] = required.get(m, 0.0) + units * float(comp.get("qty_per_unit") or 0)

    out: list[dict[str, Any]] = []
    for m, req in required.items():
        rec = mats.get(m, {})
        oh = float(rec.get("on_hand") or 0)
        ss = float(rec.get("safety_stock") or 0)
        avail = oh - ss
        if req > avail:
            out.append({
                "material": m,
                "required": round(req, 2),
                "available_above_safety": round(avail, 2),
                "shortfall": round(req - avail, 2),
                "breaches_safety_stock": True,
                "supplier_id": rec.get("supplier_id", ""),
                "lead_time_days": rec.get("lead_time_days", 0),
            })
    return out


def capacity_overruns(scheduled: list[dict[str, Any]], plant_id: str = PLANT) -> list[dict[str, Any]]:
    """The capacity floor, per line: load (units ÷ the line's units/hour)
    against the line's effective hours over the window. A line the floor
    cannot physically run is not auto-releasable. An order for a SKU no line
    makes is reported too (it cannot be run anywhere).

    Returns [{line_id, scheduled_hours, effective_capacity_hours,
              overrun_hours}] for each overloaded line, plus
            [{line_id: "", sku, reason: "no line makes this SKU"}].
    """
    cap = line_capacity(plant_id)
    lines = {l["line_id"]: l for l in cap.get("lines", [])}
    sku_line = {sku: l["line_id"] for l in cap.get("lines", []) for sku in l.get("skus", [])}
    load: dict[str, float] = {}
    out: list[dict[str, Any]] = []
    for o in scheduled or []:
        if not isinstance(o, dict):
            continue
        sku = str(o.get("sku") or "")
        lid = str(o.get("line_id") or "") or sku_line.get(sku, "")
        line = lines.get(lid)
        if not line or not line.get("units_per_hour"):
            out.append({"line_id": "", "sku": sku, "reason": "no line makes this SKU"})
            continue
        load[lid] = load.get(lid, 0.0) + float(o.get("units") or 0) / float(line["units_per_hour"])
    for lid, hours in sorted(load.items()):
        eff = float(read_line_capacity(plant_id, lid).get("effective_capacity_hours") or 0)
        if hours > eff:
            out.append({"line_id": lid, "scheduled_hours": round(hours, 1),
                        "effective_capacity_hours": eff,
                        "overrun_hours": round(hours - eff, 1)})
    return out


# ── The pod's own outbox + append-only ledger + finisher recovery ────
# NOT a system of record — the dev mirror of the MES push + the WORM
# ledger, so the finisher (release_agent) produces a VERIFIABLE record
# day-0 without a real MES, and can dedupe a double-release.
def record_outbox(entry: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        outbox = _load("outbox") or []
        rec = {"recorded_at": _now(), **entry}
        outbox.append(rec)
        _save("outbox", outbox)
    return rec


def append_ledger(entry: dict[str, Any]) -> dict[str, Any]:
    """Append-only planning ledger (dev mirror of the WORM sink)."""
    with _LOCK:
        ledger = _load("planning_ledger") or []
        rec = {"recorded_at": _now(), **entry}
        ledger.append(rec)
        _save("planning_ledger", ledger)
    return rec


def put_approved_schedule(schedule_id: str, pack: dict[str, Any]) -> None:
    """Persist an approved/released schedule so the finisher can recover it
    post-HITL and detect a double-release."""
    if not schedule_id:
        return
    with _LOCK:
        store = _load("approved_schedules") or {}
        store[schedule_id] = {"stored_at": _now(), **pack}
        _save("approved_schedules", store)


def get_approved_schedule(schedule_id: str) -> dict[str, Any]:
    return (_load("approved_schedules") or {}).get(schedule_id, {})


async def push_release(schedule: dict[str, Any]) -> dict[str, Any] | None:
    """Push an approved schedule to the MES and return its acknowledgement
    ({"mes_job_id": ...}), or None when no push is wired.

    Day 0 there is no push: this returns None and the finisher records a dev
    outbox job that says so. To go live, create the vault record 'mes_mcp'
    and replace this body with the MES release call; return the MES's own
    job id. Never invent an id here."""
    return None
