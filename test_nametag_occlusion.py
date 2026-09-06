"""이름표가 몬스터에 일부 가려져도 찾아야 한다.

실측: 격수가 몬스터에 둘러싸이면 이름표 위로 스프라이트가 겹쳐서 초록
픽셀 일부가 사라진다. 오차 10%(143px 중 14px)로는 그때마다 놓쳤다
(검출률 26/42). 색상 패턴은 '색 + 모양'을 동시에 요구해 훨씬 선택적이라
오차를 크게 줘도 헛매칭이 안 난다 - 그 오차가 곧 가림 허용치다.
"""
import io, json, os

import cv2
import numpy as np

from findtext_wrapper import get_findtext

FRAME = os.path.join("logs", "runtime", "play_area.png")


def demo():
    ft = get_findtext()
    assert ft.COLOR_ERR1 >= 0.25, f"가림 허용치가 너무 작다: {ft.COLOR_ERR1}"

    if not os.path.exists(FRAME):
        print(f"ok (허용치만 확인 {ft.COLOR_ERR1}, {FRAME} 없음)")
        return

    party = json.load(io.open("findtext_patterns.json", encoding="utf-8"))["party"]
    tags = [n for n in party if n.startswith("점프_name")]
    assert tags, "격수 이름표 패턴이 없다"
    bgr = cv2.imread(FRAME)

    def clusters(img):
        """겹치는 히트는 한 덩어리로 세서 '몇 군데'인지 본다."""
        found = []
        for n in tags:
            for h in ft.find_text(party[n], screenshot=img, find_all=True):
                found.append((h["x"], h["y"]))
        if not found:
            return 0
        mask = np.zeros(img.shape[:2], np.uint8)
        for x, y in found:
            mask[y, x] = 1
        return cv2.connectedComponentsWithStats(mask, 8)[0] - 1

    assert clusters(bgr) == 1, "가리지 않은 화면에서 정확히 한 군데여야 한다"

    # 이름표 오른쪽 30%를 몬스터가 덮은 상황을 흉내낸다
    x, y = [(h["x"], h["y"]) for n in tags
            for h in ft.find_text(party[n], screenshot=bgr, find_all=True)][0]
    p = ft._parse_text_pattern(party[tags[0]])
    ph, pw = p["bitmap"].shape
    covered = bgr.copy()
    covered[y:y + ph, x + int(pw * 0.7):x + pw] = (60, 40, 30)   # 어두운 몬스터색
    assert clusters(covered) == 1, "30% 가려지면 못 찾는다"
    print(f"ok (오차 {ft.COLOR_ERR1}, 가리지 않음/30% 가림 모두 1군데)")


if __name__ == "__main__":
    demo()
