# -*- coding: utf-8 -*-
"""저장된 패턴의 임계값을 실제 화면에 맞춰 다시 잡는다 (명령줄판).

봇을 켜둔 상태라면 게임 안에서 '/' 키를 누르는 쪽이 편하다 - 지금 보고
있는 화면으로 바로 맞추고 돌고 있는 스캐너에도 즉시 반영한다.
이 스크립트는 봇이 남긴 logs/runtime/play_area.png를 대신 쓴다.

    python tools/retune_pattern.py                  # 전부 점검
    python tools/retune_pattern.py 불귀신_right       # 하나만
    python tools/retune_pattern.py 불귀신_right --apply
"""
import argparse, os, sys

import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pattern_tuner  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="비우면 전부")
    ap.add_argument("--frame", default=pattern_tuner.FRAME_FILE)
    ap.add_argument("--apply", action="store_true", help="찾은 값으로 저장")
    a = ap.parse_args()

    frame = cv2.imread(a.frame)
    if frame is None:
        raise SystemExit(f"프레임을 못 읽음: {a.frame} "
                         "(봇을 켜두면 10초마다 갱신된다)")
    known = {n for t in pattern_tuner.load_patterns().values() for n in t}
    for n in a.names:
        if n not in known:
            raise SystemExit(f"'{n}' 패턴이 없다. 있는 것: {', '.join(sorted(known))}")

    print(f"프레임: {a.frame}")
    for line in pattern_tuner.retune(frame, names=set(a.names) or None, apply=a.apply):
        print(line)
    if not a.apply:
        print("\n적용하려면 --apply (또는 게임 안에서 '/' 키)")


if __name__ == "__main__":
    main()
