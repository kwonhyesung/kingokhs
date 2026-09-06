"""이름표는 색으로 찾고, 일부 가려져도 찾아야 한다.

두 가지를 못 박는다:
  1) 격수 이름표 패턴은 색상 패턴이어야 한다. 밝기 패턴은 넓이의 60~70%가
     '글자 주변 바닥'이라(글자가 밝고 잉크는 어두운 쪽) 격수가 불 이팩트
     위로 들어가면 진짜 위치에서도 25~50% 어긋나 실패했다.
  2) 몬스터가 이름표 일부를 덮어도 찾아야 한다. 오차 10%로는 그때마다
     놓쳤다(실측 검출률 26/42).

장면은 패턴에서 직접 합성한다 - 봇이 덮어쓰는 logs/runtime/play_area.png에
기대면 그 순간 격수가 화면에 없을 때 테스트가 깨진다.
"""
import io, json

import cv2
import numpy as np

from findtext_wrapper import get_findtext

ft = get_findtext()


def _scene(pattern_text, occlude=0.0):
    """패턴의 잉크 픽셀을 그 색으로 그린 화면. occlude 비율만큼 오른쪽을 덮는다."""
    p = ft._parse_text_pattern(pattern_text)
    if isinstance(p, list):
        p = p[0]
    bm = p["bitmap"] > 0
    ph, pw = bm.shape
    r, g, b, _ = p["colors"][0]
    rng = np.random.default_rng(3)
    canvas = rng.integers(40, 110, (ph + 80, pw + 80, 3), dtype=np.uint8)  # 어두운 바닥
    ys, xs = np.where(bm)
    canvas[ys + 40, xs + 40] = (b, g, r)                                   # BGR로 그린다
    if occlude:
        cut = 40 + int(pw * (1 - occlude))
        canvas[40:40 + ph, cut:40 + pw] = (60, 40, 30)                     # 몬스터색으로 덮기
    return canvas, (40, 40)


def _near(got, want, tol=1):
    return abs(got[0] - want[0]) <= tol and abs(got[1] - want[1]) <= tol


def _clusters(text, img):
    hits = ft.find_text(text, screenshot=img, find_all=True)
    if not hits:
        return 0, []
    return len(hits), [(h["x"], h["y"]) for h in hits]


def demo():
    party = json.load(io.open("findtext_patterns.json", encoding="utf-8"))["party"]
    from svc_logic import LogicSvc
    tags = [n for n in party if n.startswith(LogicSvc._WARRIOR_NAME_PREFIX)]
    assert tags, "격수 이름표 패턴이 없다"

    for n in tags:
        assert "@" in party[n].split("$")[0], (
            f"{n}은 밝기 패턴이다 - 배경(바닥/불꽃)이 바뀌면 못 찾는다")
        img, at = _scene(party[n])
        cnt, pos = _clusters(party[n], img)
        # 비트맵 가장자리에 빈 줄이 있으면 최적 위치가 1px 밀린다 - 그건 무해하다
        assert cnt == 1 and _near(pos[0], at), f"{n}: 가리지 않은 화면 {cnt}곳 {pos}"

        img, at = _scene(party[n], occlude=0.30)
        cnt, pos = _clusters(party[n], img)
        assert cnt == 1 and _near(pos[0], at), f"{n}: 30% 가림 {cnt}곳 {pos}"

    assert ft.COLOR_ERR1 >= 0.25, f"가림 허용치가 너무 작다: {ft.COLOR_ERR1}"
    print(f"ok ({tags}, 오차 {ft.COLOR_ERR1}, 가림 0%/30% 모두 1군데)")


if __name__ == "__main__":
    demo()
