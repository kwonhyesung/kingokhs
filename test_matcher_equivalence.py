"""최적화한 매칭이 예전 알고리즘과 '똑같은 결과'를 내는지 대조한다.

속도를 위해 중간 배열을 없애고 두 번째 상관연산을 건너뛰게 고쳤는데,
그건 결과가 1픽셀도 달라지지 않아야만 허용되는 종류의 변경이다.
"""
import json, io, re
import numpy as np
import cv2
from findtext_wrapper import FindText

ft = FindText()


def _reference(screen_bin, pattern_bin, len1, len0, e1_max, e0_max):
    """고치기 전의 알고리즘 (round -> int32 -> 비교)."""
    ph, pw = pattern_bin.shape
    sh, sw = screen_bin.shape
    ok = np.ones((sh - ph + 1, sw - pw + 1), dtype=bool)
    if len1 > 0:
        c1 = cv2.matchTemplate(screen_bin, pattern_bin, cv2.TM_CCORR)
        ok &= ((len1 - np.round(c1).astype(np.int32)) <= e1_max)
    if len0 > 0:
        c0 = cv2.matchTemplate(screen_bin, 1.0 - pattern_bin, cv2.TM_CCORR)
        ok &= (np.round(c0).astype(np.int32) <= e0_max)
    return set(zip(*np.where(ok)))


def _scene(pat_text, rng):
    """패턴 자신을 심어둔 화면 + 잡음. 히트가 실제로 나오는 조건이어야 한다."""
    m = re.match(r"\|<[^>]*>\*(\d+)\$(\d+)\.(.*)", pat_text)
    thr, w, data = int(m.group(1)), int(m.group(2)), m.group(3)
    bits = ft.base64tobit(data)
    h = len(bits) // w
    arr = np.array([int(b) for b in bits[: w * h]], dtype=np.uint8).reshape(h, w)
    img = np.where(arr == 1, 0, 255).astype(np.uint8)
    canvas = rng.integers(0, 256, (h + 60, w + 60), dtype=np.uint8)
    canvas[20 : 20 + h, 20 : 20 + w] = img
    return np.dstack([canvas] * 3), thr


def demo():
    rng = np.random.default_rng(7)
    pats = json.load(io.open("findtext_patterns.json", encoding="utf-8"))
    checked = hits_seen = 0
    for cat in ("monster", "item", "party", "map"):
        for name, text in pats.get(cat, {}).items():
            bgr, _ = _scene(text, rng)
            p = ft._parse_single_pattern(text)
            assert p is not None, name
            gray = ft._to_gray(bgr)
            screen_bin = (gray < ((p["color"] + 1) << 7)).astype(np.float32)
            pb = (p["bitmap"] > 0).astype(np.float32)
            ph, pw = pb.shape
            len1 = int(pb.sum())
            len0 = ph * pw - len1
            e1 = (len1 * int(0.10 * 1024)) >> 10
            e0 = (len0 * int(0.10 * 1024)) >> 10
            if e1 >= len1:
                len1 = 0
            if e0 >= len0:
                len0 = 0
            want = _reference(screen_bin, pb, len1, len0, e1, e0)
            got = {(r["y"], r["x"])
                   for r in ft._template_match_native(bgr, p, 0.10, 0.10, True, gray=gray)}
            assert got == want, (cat, name, len(got), len(want))
            checked += 1
            hits_seen += len(want)
    assert checked >= 20 and hits_seen > 0, (checked, hits_seen)
    print(f"ok ({checked} patterns, {hits_seen} hits, identical)")


if __name__ == "__main__":
    demo()
