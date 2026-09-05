# -*- coding: utf-8 -*-
"""스캔 최적화 점검 (게임 없이 순수 계산만 검증).

두 가지를 본다:
  1. gray를 프레임당 한 번만 계산해서 넘겨도 결과가 예전과 100% 같은가.
     (같지 않으면 '빨라졌는데 가끔 인식이 이상하다'가 되므로 이게 핵심이다)
  2. 맵 이름으로 몬스터 패턴을 골라내는 규칙이 의도대로 동작하는가.
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from findtext_wrapper import FindText              # noqa: E402
from svc_sentinel import SentinelThread           # noqa: E402


def _make_pattern(ft, bitmap, threshold):
    return {"bitmap": np.array(bitmap, dtype=np.uint8),
            "width": len(bitmap[0]), "height": len(bitmap),
            "color": threshold, "comment": "t", "mode": "gray"}


def test_precomputed_gray_gives_identical_results():
    """A 최적화: gray를 밖에서 만들어 넘긴 결과 == 안에서 만든 결과."""
    rng = np.random.default_rng(0)
    ft = FindText.__new__(FindText)     # __init__ 없이 매칭만 씀
    screen = rng.integers(0, 256, (120, 160, 3), dtype=np.uint8)
    gray = FindText._to_gray(screen)

    # 임계값을 여러 개 쓴다 - 실제 패턴 26개도 임계값이 전부 다르다.
    for threshold in (24, 99, 133, 207):
        pat = _make_pattern(ft, (rng.random((6, 8)) > 0.5).astype(np.uint8), threshold)
        old = ft._template_match_native(screen, pat, 0.1, 0.1, True)          # 내부 계산
        new = ft._template_match_native(screen, pat, 0.1, 0.1, True, gray=gray)  # 재사용
        assert old == new, f"threshold={threshold}에서 결과가 달라짐"

    # 실제로 뭔가 찾긴 하는지도 본다 (둘 다 빈 리스트면 위 비교가 무의미하다).
    solid = np.zeros((40, 40, 3), dtype=np.uint8)
    pat = _make_pattern(ft, np.ones((4, 4), dtype=np.uint8), 200)
    hits = ft._template_match_native(solid, pat, 0.1, 0.1, True,
                                     gray=FindText._to_gray(solid))
    assert hits, "검은 화면에서 검은 패턴은 찾아져야 한다"


def test_monsters_for_map_matches_by_prefix():
    """맵 이름 앞부분으로 목록을 고른다 - 흉가1/흉가2가 '흉가' 한 줄로 걸린다."""
    s = SentinelThread.__new__(SentinelThread)
    s._maps_cfg = lambda: {}          # hunt_maps.json 없음 -> config.json 쪽을 본다
    cfg = {"hunt": {"monsters_by_map": {"흉가": ["처녀귀신", "달걀", "불귀신"],
                                        "도삭산": ["갈산신"]}}}
    assert s._monsters_for_map(cfg, "흉가1") == {"처녀귀신", "달걀", "불귀신"}
    assert s._monsters_for_map(cfg, "흉가2") == {"처녀귀신", "달걀", "불귀신"}
    assert s._monsters_for_map(cfg, "도삭산551") == {"갈산신"}
    # 목록에 없는 맵 / 아직 맵 이름을 못 읽은 상태 -> 스캔 안 함
    assert s._monsters_for_map(cfg, "복건성") == set()
    assert s._monsters_for_map(cfg, "기본맵") == set()
    assert s._monsters_for_map(cfg, "") == set()
    # 설정 자체가 없으면(아직 config를 안 고친 PC) None = 예전처럼 전부 스캔.
    # 여기서 빈 set을 돌려주면 그 PC는 모든 맵에서 몬스터가 통째로 안 잡힌다.
    assert s._monsters_for_map({}, "흉가1") is None
    assert s._monsters_for_map({"hunt": {}}, "흉가1") is None


def test_hunt_maps_json_wins_over_config_json():
    """공용 설정은 hunt_maps.json에 둔다 - config.json은 PC마다 달라서 저장소가
    건드리면 다른 PC의 git pull이 깨진다."""
    s = SentinelThread.__new__(SentinelThread)
    s._maps_cfg = lambda: {"monsters_by_map": {"흉가": ["달걀"]}}
    stale = {"hunt": {"monsters_by_map": {"흉가": ["처녀귀신", "달걀", "불귀신"]}}}
    assert s._monsters_for_map(stale, "흉가1") == {"달걀"}

    # 저장소에 실제로 들어있는 hunt_maps.json이 읽히고 흉가가 들어있는지도 본다
    real = SentinelThread.__new__(SentinelThread)
    real._maps_cfg_cache = (None, 0.0)
    assert real._monsters_for_map({}, "흉가1") == {"처녀귀신", "달걀", "불귀신"}
    assert real._monsters_for_map({}, "도삭산551") == {"갈산신"}


def test_only_names_filters_direction_suffixes():
    """패턴 이름은 '처녀귀신_left'인데 목록엔 '처녀귀신'만 적는다."""
    from svc_pattern_matcher import PatternMatcher

    m = PatternMatcher.__new__(PatternMatcher)
    m.findtext_patterns = {"monster": {
        "처녀귀신_left": "|<처녀귀신_left>*118$1.0",
        "처녀귀신_top": "|<처녀귀신_top>*129$1.0",
        "갈산신_left": "|<갈산신_left>*30$1.0",
    }}

    captured = {}

    class _FT:
        def find_text(self, combined, **kw):
            captured["combined"] = combined
            return []

    m._ft = _FT()
    m.find_text_scan(np.zeros((10, 10, 3), np.uint8), "monster", "MONSTER",
                     only_names={"처녀귀신"})
    assert "처녀귀신_left" in captured["combined"]
    assert "처녀귀신_top" in captured["combined"]
    assert "갈산신" not in captured["combined"], "이 맵에 없는 몬스터는 스캔하면 안 된다"

    # 필터를 안 주면 예전처럼 전부 스캔한다
    captured.clear()
    m.find_text_scan(np.zeros((10, 10, 3), np.uint8), "monster", "MONSTER")
    assert "갈산신" in captured["combined"]


def test_known_walls_file_covers_the_blocked_row():
    """흉가1의 y=15는 x=1~10이 막혀 있다 - 이걸 미리 알아야 첫 실행부터 돈다."""
    import json
    from svc_hunt import path_step

    cfg = json.load(open("hunt_maps.json", encoding="utf-8"))
    walls = {(c[0], c[1]) for c in cfg["known_walls"]["흉가1"]}
    assert walls == {(x, 15) for x in range(1, 11)}, walls
    # 그 벽을 알면 (5,14)에서 (9,17)로 가는 길을 찾는다
    assert path_step((5, 14), (9, 17), walls, radius=24) == "right"
    # 모르면 벽으로 곧장 들어간다(= 지금까지의 동작)
    assert path_step((5, 14), (9, 17), set(), radius=24) == "down"


if __name__ == "__main__":
    test_precomputed_gray_gives_identical_results()
    test_monsters_for_map_matches_by_prefix()
    test_hunt_maps_json_wins_over_config_json()
    test_only_names_filters_direction_suffixes()
    test_known_walls_file_covers_the_blocked_row()
    print("test_scan_map_filter OK")
