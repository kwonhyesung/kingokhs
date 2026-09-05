# -*- coding: utf-8 -*-
"""맵 이름 흔들림이 맵 변경으로 새지 않는지 본다.

실측: 캐릭터가 가만히 있는데 '흉가1' <-> '천안궁성흉가입구'가 다섯 번
왕복했다. 그 흔들림이 그대로 맵 변경이 되어 사냥이 '사냥터를 벗어남'으로
계속 멈췄다.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _apply(reads, need=3):
    """svc_monitor의 확정 규칙을 그대로 흉내낸다: 연속 need회 같아야 바꾼다."""
    current, cand, hits, changes = "", "", 0, []
    for text in reads:
        if text != current:
            hits = hits + 1 if cand == text else 1
            cand = text
            if hits >= need:
                current = text
                changes.append(text)
                cand, hits = "", 0
        else:
            cand, hits = "", 0
    return changes


def test_flapping_reads_do_not_change_the_map():
    flap = ["흉가1", "천안궁성흉가입구"] * 5
    assert _apply(flap) == [], f"흔들림이 맵 변경으로 샜다: {_apply(flap)}"


def test_a_real_move_still_registers():
    reads = ["흉가1"] * 3 + ["천안궁성흉가입구"] * 4
    assert _apply(reads) == ["흉가1", "천안궁성흉가입구"]


def test_one_stray_read_in_the_middle_is_ignored():
    reads = ["흉가1"] * 3 + ["도삭산_1"] + ["흉가1"] * 3
    assert _apply(reads) == ["흉가1"]


def test_monitor_actually_debounces():
    """위 규칙이 svc_monitor에 실제로 들어있는지 확인(흉내와 어긋나지 않게)."""
    src = open(os.path.join(HERE, "svc_monitor.py"), encoding="utf-8").read()
    block = src.split('print(f"[Map] 맵 변경됨')[0][-1200:]
    assert "_map_candidate" in block, "맵 변경에 디바운스가 없다"
    assert re.search(r"hits\s*>=\s*3", block), "확정 조건(3회)이 안 보인다"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("test_map_debounce OK")
