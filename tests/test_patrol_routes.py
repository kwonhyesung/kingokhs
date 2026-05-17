import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from patrol_routes import (
    inject_patrol_points,
    load_patrol_points_csv,
    parse_route_point,
)


def test_load_patrol_points_csv_reads_absolute_points(tmp_path):
    csv_path = tmp_path / "patrol_points.csv"
    csv_path.write_text(
        "\n".join(
            [
                "point_id,x,y,radius,combat_allowed,note",
                "p01,3,21,1,true,",
                "p02,16,10,2,false,boss",
                "p03,,,1,true,skip-me",
            ]
        ),
        encoding="utf-8",
    )

    points = load_patrol_points_csv(csv_path)

    assert points == [
        {"id": "p01", "x": 3, "y": 21, "radius": 1, "combat_allowed": True, "note": ""},
        {"id": "p02", "x": 16, "y": 10, "radius": 2, "combat_allowed": False, "note": "boss"},
    ]


def test_parse_route_point_supports_absolute_and_grid_formats():
    assert parse_route_point({"x": 3, "y": 21, "radius": 1}) == {
        "mode": "absolute",
        "x": 3,
        "y": 21,
        "radius": 1,
    }
    assert parse_route_point("3,21") == {
        "mode": "absolute",
        "x": 3,
        "y": 21,
        "radius": 1,
    }
    assert parse_route_point("B12") == {
        "mode": "grid",
        "grid": "B12",
    }


def test_inject_patrol_points_builds_points_only_sequence():
    loaded_db = {
        "기존맵": {
            "1": {
                "entry": {"x": 1, "y": 1, "direction": "right", "reverse_action": ""},
                "points": ["A1"],
                "exit": {"x": 9, "y": 9, "direction": "down", "reverse_action": ""},
            }
        }
    }

    inject_patrol_points(
        loaded_db,
        map_name="천안궁흉가5",
        floor="1",
        points=[
            {"id": "p01", "x": 3, "y": 21, "radius": 1, "combat_allowed": True, "note": ""},
            {"id": "p02", "x": 16, "y": 10, "radius": 1, "combat_allowed": True, "note": ""},
        ],
    )

    assert loaded_db["기존맵"]["1"]["points"] == ["A1"]
    assert loaded_db["천안궁흉가5"]["1"]["point_mode"] == "absolute"
    assert loaded_db["천안궁흉가5"]["1"]["points"] == [
        {"id": "p01", "x": 3, "y": 21, "radius": 1, "combat_allowed": True, "note": ""},
        {"id": "p02", "x": 16, "y": 10, "radius": 1, "combat_allowed": True, "note": ""},
    ]
