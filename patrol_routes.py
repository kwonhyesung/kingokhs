import csv
from pathlib import Path
from typing import Any


def _to_int(value: Any) -> int | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any, default: bool = True) -> bool:
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "t", "yes", "y", "on"}


def load_patrol_points_csv(csv_path: str | Path) -> list[dict[str, Any]]:
    path = Path(csv_path)
    if not path.exists():
        return []

    points: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for index, row in enumerate(reader, start=1):
            x = _to_int(row.get("x", ""))
            y = _to_int(row.get("y", ""))
            if x is None or y is None:
                continue

            point_id = str(row.get("point_id", "")).strip() or f"p{index:02d}"
            radius = _to_int(row.get("radius", "")) or 1
            combat_allowed = _to_bool(row.get("combat_allowed", ""), default=True)
            note = str(row.get("note", "")).strip()
            points.append(
                {
                    "id": point_id,
                    "x": x,
                    "y": y,
                    "radius": radius,
                    "combat_allowed": combat_allowed,
                    "note": note,
                }
            )
    return points


def parse_route_point(point: Any) -> dict[str, Any] | None:
    if isinstance(point, dict):
        x = _to_int(point.get("x", ""))
        y = _to_int(point.get("y", ""))
        if x is not None and y is not None:
            return {
                "mode": "absolute",
                "x": x,
                "y": y,
                "radius": _to_int(point.get("radius", "")) or 1,
            }
        grid_name = str(point.get("grid", "")).strip()
        if grid_name:
            return {"mode": "grid", "grid": grid_name}
        return None

    if isinstance(point, (list, tuple)) and len(point) >= 2:
        x = _to_int(point[0])
        y = _to_int(point[1])
        if x is not None and y is not None:
            return {"mode": "absolute", "x": x, "y": y, "radius": 1}
        return None

    text = str(point or "").strip()
    if not text:
        return None
    if "," in text:
        left, right = text.split(",", 1)
        x = _to_int(left)
        y = _to_int(right)
        if x is not None and y is not None:
            return {"mode": "absolute", "x": x, "y": y, "radius": 1}
    return {"mode": "grid", "grid": text}


def inject_patrol_points(
    loaded_db: dict[str, Any],
    map_name: str,
    floor: str,
    points: list[dict[str, Any]],
) -> dict[str, Any]:
    if not points:
        return loaded_db

    if map_name not in loaded_db:
        loaded_db[map_name] = {}
    if floor not in loaded_db[map_name]:
        loaded_db[map_name][floor] = {}

    loaded_db[map_name][floor]["point_mode"] = "absolute"
    loaded_db[map_name][floor]["points"] = list(points)
    return loaded_db
