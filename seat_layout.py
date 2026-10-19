"""Normalize public school seat coordinates; uncertain seats are never selectable."""
from __future__ import annotations

import math
import time

CAMPUS = "江浦校区"
BUILDINGS = ("逸夫图书馆", "浦江图书馆")


def jiangpu_libraries(data):
    groups = {str(g.get("id")): g.get("group_name") for g in data.get("libGroups", [])}
    rooms = data.get("libs") or []
    if isinstance(rooms, dict):
        rooms = [rooms]
    result = []
    for room in rooms:
        building = groups.get(str(room.get("lib_group_id")))
        if building not in BUILDINGS or not room.get("lib_id"):
            continue
        result.append({"id": int(room["lib_id"]), "name": str(room.get("lib_name") or room["lib_id"]),
                       "campus": CAMPUS, "building": building, "floor": str(room.get("lib_floor") or ""),
                       "is_open": room.get("is_open") is True})
    return sorted(result, key=lambda r: (BUILDINGS.index(r["building"]), r["floor"], r["id"]))


def coordinate(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and 0 <= value <= 10000 else None
    except (TypeError, ValueError):
        return None


def normalize_layout(room, info, observed_at=None):
    layout = room.get("lib_layout") or {}
    raw = layout.get("seats") or []
    if isinstance(raw, dict):
        raw = list(raw.values())
    opened = room.get("is_open") is True
    seats, seen = [], set()
    for item in raw:
        if not isinstance(item, dict) or not item.get("key") or not item.get("name"):
            continue
        key = str(item["key"])
        if key in seen:
            continue
        seen.add(key)
        # Observed school replies: 1/false = free, 2..4/true = unavailable.
        # Missing, contradictory or new statuses remain unknown.
        code, status = item.get("seat_status"), item.get("status")
        if not opened:
            state = "unavailable"
        elif type(code) is int and code == 1 and status is False:
            state = "available"
        elif type(code) is int and code in (2, 3, 4) and status is True:
            state = "occupied"
        else:
            state = "unknown"
        seats.append({"key": key, "name": str(item["name"]), "state": state,
                      "available": state == "available", "x": coordinate(item.get("x")),
                      "y": coordinate(item.get("y")), "status": {
                          "available": "空闲", "occupied": "已占用", "unknown": "状态未知",
                          "unavailable": "馆室未开放"}[state]})
    return {**info, "lib": int(room["lib_id"]), "is_open": opened, "source": "school",
            "observed_at": time.time() if observed_at is None else observed_at,
            "max_x": coordinate(layout.get("max_x")), "max_y": coordinate(layout.get("max_y")),
            "seats": seats, "total_count": len(seats),
            "available_count": sum(s["available"] for s in seats)}
