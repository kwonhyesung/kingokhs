# -*- coding: utf-8 -*-
"""저장된 패턴의 임계값을 실제 화면에 맞춰 다시 잡는다.

패턴 문자열의 *숫자(임계값)는 캡처할 때 드래그한 ROI 밝기로 자동 결정된다.
그 값이 실전 화면과 어긋나면 모양이 멀쩡해도 화면 이진화가 통째로 어긋나
영영 안 잡힌다. 실측: 졈프_name이 228로 저장돼 0곳이었는데 180으로 내리니
1곳에서 잡혔다. 불귀신(175~188)/달걀(169)이 같은 대역이다.

찾은 값을 적용하는 조건은 하나다 - '지금은 못 찾는데 그 값이면 정확히
찾는' 경우만. 이미 잘 찾는 패턴은 건드리지 않고, 아무 값으로도 못 찾으면
(그 대상이 화면에 없으면) 그냥 넘어간다.
"""
import io
import json
import os

import cv2
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PATTERNS_FILE = os.path.join(SCRIPT_DIR, "findtext_patterns.json")
FRAME_FILE = os.path.join(SCRIPT_DIR, "logs", "runtime", "play_area.png")

# 이보다 많이 맞으면 '찾았다'가 아니라 헛매칭이다(개체 몇 마리 수준을 넘는다).
MAX_SANE_HITS = 8
THRESHOLD_STEPS = range(40, 245, 5)


def load_patterns() -> dict:
    return json.load(io.open(PATTERNS_FILE, encoding="utf-8"))


def save_patterns(pats: dict) -> None:
    io.open(PATTERNS_FILE, "w", encoding="utf-8", newline="").write(
        json.dumps(pats, ensure_ascii=False, indent=2))


def _hits_at(ft, gray, bitmap, threshold, err=0.10):
    pb = (bitmap > 0).astype(np.float32)
    ph, pw = pb.shape
    len1 = int(pb.sum())
    len0 = ph * pw - len1
    e1 = (len1 * int(err * 1024)) >> 10
    e0 = (len0 * int(err * 1024)) >> 10
    sb = (gray < ((threshold + 1) << 7)).astype(np.float32)
    c1 = cv2.matchTemplate(sb, pb, cv2.TM_CCORR)
    ok = c1 >= (len1 - e1 - 0.5)
    if not ok.any():
        return 0
    c0 = cv2.matchTemplate(sb, 1.0 - pb, cv2.TM_CCORR)
    ok &= c0 <= (e0 + 0.5)
    if not ok.any():
        return 0
    n, _, _, _ = cv2.connectedComponentsWithStats(
        np.ascontiguousarray(ok).view(np.uint8), 8)
    return n - 1


def retune(frame_bgr, names=None, categories=("monster", "item", "party"),
           apply=False) -> list:
    """(보고 줄 목록). apply=True면 개선되는 것만 파일에 저장한다."""
    from findtext_wrapper import get_findtext

    ft = get_findtext()
    gray = ft._to_gray(frame_bgr)
    pats = load_patterns()
    lines = []
    changed = 0

    for cat in categories:
        for name, text in sorted(pats.get(cat, {}).items()):
            if names and name not in names:
                continue
            p = ft._parse_text_pattern(text)
            if isinstance(p, list):
                p = p[0]
            if p is None or p.get("mode") == "color":
                continue
            cur = int(p["color"])
            now = _hits_at(ft, gray, p["bitmap"], cur)
            if 1 <= now <= MAX_SANE_HITS:
                lines.append(f"  {name:16} 임계 {cur:3} -> 이미 {now}곳, 그대로 둠")
                continue
            best = None
            for t in THRESHOLD_STEPS:
                n = _hits_at(ft, gray, p["bitmap"], t)
                if 1 <= n <= MAX_SANE_HITS:
                    score = (n, abs(t - cur))
                    if best is None or score < best[0]:
                        best = (score, t, n)
            if best is None:
                lines.append(f"  {name:16} 임계 {cur:3} -> 어떤 값으로도 못 찾음 "
                             f"(화면에 없거나 모양이 다름)")
                continue
            _, t, n = best
            lines.append(f"  {name:16} 임계 {cur:3} -> {t:3} 이면 {n}곳  "
                         f"{'[적용]' if apply else '[미적용]'}")
            if apply:
                pats[cat][name] = text.replace(f"*{cur}$", f"*{t}$", 1)
                changed += 1

    if apply and changed:
        save_patterns(pats)
        lines.append(f"  -> {changed}개 저장 완료 (findtext_patterns.json)")
    elif apply:
        lines.append("  -> 바꿀 것 없음")
    return lines
