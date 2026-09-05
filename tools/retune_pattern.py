# -*- coding: utf-8 -*-
"""저장된 패턴의 임계값이 실제 화면과 안 맞을 때 맞는 값을 찾아준다.

패턴 문자열의 *숫자(임계값)는 캡처할 때 드래그한 ROI 밝기로 자동 결정되는데,
그 값이 실전 화면과 어긋나면 모양이 멀쩡해도 영원히 안 잡힌다. 실측:
  졈프_name  저장 228 -> 배경오차 26/11 실패,  임계 180/200이면 매칭
  불귀신 175~188 / 달걀 169 = 같은 대역,  잘 잡히는 처녀귀신은 116~154

쓰는 법 (봇이 logs/runtime/play_area.png를 10초마다 남긴다):
    찾고 싶은 몬스터가 화면에 있을 때 그 PNG가 갱신되길 기다린 뒤
    python tools/retune_pattern.py 불귀신_right
    python tools/retune_pattern.py 불귀신_right --apply   (찾은 값으로 저장)
"""
import argparse, io, json, os, sys

import cv2
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPT_DIR)
from findtext_wrapper import get_findtext  # noqa: E402

PATTERNS = os.path.join(SCRIPT_DIR, "findtext_patterns.json")
FRAME = os.path.join(SCRIPT_DIR, "logs", "runtime", "play_area.png")


def find_pattern(pats, name):
    for cat, table in pats.items():
        if name in table:
            return cat, table[name]
    raise SystemExit(f"'{name}' 패턴이 없다. 있는 것: "
                     + ", ".join(sorted(k for t in pats.values() for k in t)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--frame", default=FRAME)
    ap.add_argument("--apply", action="store_true", help="찾은 임계값으로 저장")
    a = ap.parse_args()

    bgr = cv2.imread(a.frame)
    if bgr is None:
        raise SystemExit(f"프레임을 못 읽음: {a.frame} (봇을 켜두면 10초마다 갱신된다)")

    ft = get_findtext()
    pats = json.load(io.open(PATTERNS, encoding="utf-8"))
    cat, text = find_pattern(pats, a.name)
    p = ft._parse_text_pattern(text)
    gray = ft._to_gray(bgr)
    pb = (p["bitmap"] > 0).astype(np.float32)
    ph, pw = pb.shape
    len1 = int(pb.sum())
    len0 = ph * pw - len1
    e1, e0 = (len1 * 102) >> 10, (len0 * 102) >> 10

    print(f"{a.name} [{cat}] {pw}x{ph}, 저장된 임계값 = {p['color']}")
    print(f"  {'임계':>4} {'잉크오차':>9} {'배경오차':>10}  매칭")
    good = []
    for t in range(40, 245, 5):
        sb = (gray < ((t + 1) << 7)).astype(np.float32)
        c1 = cv2.matchTemplate(sb, pb, cv2.TM_CCORR)
        c0 = cv2.matchTemplate(sb, 1.0 - pb, cv2.TM_CCORR)
        m1, m0 = len1 - c1, c0
        hit = int(((m1 <= e1 + 0.5) & (m0 <= e0 + 0.5)).sum())
        i = int(np.argmin(m1 + m0))
        y, x = divmod(i, m1.shape[1])
        if hit or t == p["color"]:
            print(f"  {t:>4} {int(m1[y, x]):>6}/{e1:<3} {int(m0[y, x]):>7}/{e0:<4}  "
                  f"{hit}곳" + ("   <- 지금 저장된 값" if t == p["color"] else ""))
        if hit:
            good.append((hit, t))

    if not good:
        print("\n어떤 임계값으로도 안 잡힌다 - 그 대상이 이 프레임에 없거나, "
              "패턴 모양 자체가 화면과 다르다(ft.py로 다시 캡처할 것).")
        return
    # 헛매칭이 가장 적은(=가장 또렷한) 임계값을 고른다
    best = min(good, key=lambda g: (g[0], abs(g[1] - p["color"])))[1]
    print(f"\n권장 임계값 = {best} (지금 {p['color']})")
    if a.apply:
        pats[cat][a.name] = text.replace(f"*{p['color']}$", f"*{best}$", 1)
        io.open(PATTERNS, "w", encoding="utf-8", newline="").write(
            json.dumps(pats, ensure_ascii=False, indent=2))
        print("findtext_patterns.json 저장 완료")
    else:
        print("적용하려면 --apply")


if __name__ == "__main__":
    main()
