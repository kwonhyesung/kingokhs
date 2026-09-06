"""점프(격수)와 졈프(도사)는 글자 하나 차이다 - 서로를 오탐하면 안 된다.

실측 회귀: 화면에 졈프 이름표 하나뿐인 프레임에서 err1=err0=0.10이면
점프_name/점프_name_green이 그 자리를 1곳씩 물어서, 도사가 자기 자신을
격수로 알고 클릭했다([TargetConfirm] character click: name=점프_name).
정작 졈프_name_white는 0곳이었다.

글자 모양(잉크)은 정체성이고 그 뒤 바닥(배경)은 잡음이라, 둘에 같은 오차를
주면 정확히 거꾸로 동작한다.
"""
import io, json, os

import cv2
import numpy as np

from svc_pattern_matcher import PatternMatcher

FRAME = os.path.join("logs", "runtime", "play_area.png")


def demo():
    import inspect
    sig = inspect.signature(PatternMatcher.find_text_scan)
    e1 = sig.parameters["err1"].default
    e0 = sig.parameters["err0"].default
    assert e1 < e0, f"잉크 오차가 배경 오차보다 엄격해야 한다: err1={e1} err0={e0}"
    assert e1 <= 0.05, f"잉크 오차 {e1}로는 점프/졈프를 못 가른다"

    if not os.path.exists(FRAME):
        print(f"ok (오차 규칙만 확인, {FRAME} 없음)")
        return

    from findtext_wrapper import get_findtext
    ft = get_findtext()
    bgr = cv2.imread(FRAME)
    gray = ft._to_gray(bgr)
    party = json.load(io.open("findtext_patterns.json", encoding="utf-8"))["party"]

    def hits(name):
        p = ft._parse_text_pattern(party[name])
        if isinstance(p, list):
            p = p[0]
        return len(ft._template_match_native(bgr, p, e1, e0, True, gray=gray))

    jump = sum(hits(n) for n in party if n.startswith("점프_name"))
    zeom = sum(hits(n) for n in party if n.startswith("졈프_name"))
    # 저장된 프레임은 도사 화면이고 거기 보이는 이름표는 졈프 하나다.
    assert zeom >= 1, "졈프 이름표를 못 찾는다"
    assert jump == 0, f"점프 패턴이 졈프 글자를 {jump}곳 오탐한다"
    print(f"ok (err1={e1} err0={e0}, 졈프 {zeom}곳 / 점프 오탐 {jump}곳)")


if __name__ == "__main__":
    demo()
