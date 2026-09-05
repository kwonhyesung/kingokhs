# -*- coding: utf-8 -*-
"""최신 세션의 핵심만 한 화면에 뽑는다.

로그가 수천 줄이라 매번 grep 조합을 새로 짜게 되는데, 정작 보는 건 늘 같다:
하드웨어가 붙었나 / 키가 실제로 나갔나 / 캐릭터가 움직였나 / 스캔이 몇 ms인가.

    py logcheck.py            # logs/remote_격수.log
    py logcheck.py 도사1       # logs/remote_도사1.log
    py logcheck.py logs/x.log # 파일 직접 지정
"""
import glob
import os
import re
import sys

# (제목, 정규식) - 순서가 곧 출력 순서다.
SECTIONS = [
    ("실행 정보", r"^\[Net\] (runtime identity|hunt settings)"),
    ("하드웨어",   r"^\[(Input\] backend|Hardware\])"),
    ("하드웨어 검사 '/'", r"^\[HWTest\]"),
    ("모드/맵",    r"^\[(Hotkey|Map)\]"),
    ("이동 진단",  r"^\[(MoveDiag|MoveTest|NavBlock)\]"),
    ("사냥",      r"^\[(Hunt|HuntIdle)\]"),
    ("감지",      r"^\[Sentinel\] (몬스터|파티원)"),
    ("막힘",      r"^\[(Stuck|NavMem|WallMem)\]"),
    ("에러",      r"(전송 실패|error:|Traceback|\[ERROR\])"),
]
POS_RE = re.compile(r"(?:Current: |me=|pos=)\((\d+),\s*(\d+)\)")
CYCLE_RE = re.compile(r"사이클 (\d+)ms")


def latest_session(lines):
    """마지막 [Net] runtime identity 이후만. 없으면 마지막 500줄."""
    for i in range(len(lines) - 1, -1, -1):
        if "runtime identity" in lines[i]:
            return lines[i:]
    return lines[-500:]


def show(title, rows, limit=25):
    print(f"\n=== {title} ({len(rows)}줄) ===")
    if not rows:
        print("  (없음)")
        return
    for r in rows[:limit]:
        print(f"  {r}")
    if len(rows) > limit:
        print(f"  ... {len(rows) - limit}줄 더 (뒤쪽 5줄)")
        for r in rows[-5:]:
            print(f"  {r}")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "격수"
    path = arg if os.path.sep in arg or arg.endswith(".log") else f"logs/remote_{arg}.log"
    if not os.path.exists(path):
        found = glob.glob("logs/remote_*.log")
        sys.exit(f"{path} 없음. 있는 파일: {found}")

    with open(path, encoding="utf-8", errors="replace") as f:
        all_lines = [ln.rstrip("\n") for ln in f]
    lines = latest_session(all_lines)
    print(f"파일: {path}  (전체 {len(all_lines)}줄 / 최신 세션 {len(lines)}줄)")

    for title, pat in SECTIONS:
        rx = re.compile(pat)
        show(title, [ln for ln in lines if rx.search(ln)])

    # 좌표: 움직였는지가 핵심이라 '서로 다른 값이 몇 개인가'만 본다.
    seen, order = set(), []
    for ln in lines:
        for m in POS_RE.finditer(ln):
            p = (int(m.group(1)), int(m.group(2)))
            if p not in seen:
                seen.add(p)
                order.append(p)
    print(f"\n=== 좌표 ({len(order)}종) ===")
    print(f"  {order[:20] if order else '(없음)'}"
          + ("  <- 1종뿐: 전혀 안 움직임" if len(order) == 1 else ""))

    cy = [int(m.group(1)) for ln in lines for m in CYCLE_RE.finditer(ln)]
    if cy:
        cy.sort()
        print(f"\n=== 스캔 사이클 ({len(cy)}회) ===")
        print(f"  최소 {cy[0]}ms  중앙 {cy[len(cy)//2]}ms  최대 {cy[-1]}ms  (목표 33ms)")


if __name__ == "__main__":
    main()
