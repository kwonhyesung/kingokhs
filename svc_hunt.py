"""svc_hunt.py - 자동사냥 판단 로직 (순수 함수 + 벽 학습 메모리).

좌표계는 pos(x,y) 하나만 쓴다. grid 좌표(char_grid / 'B12' 같은 격자 이름)는
쓰지 않는다.

몬스터/아이템 pos는 SentinelThread가 계산해 state에 넣어준 값을 그대로 쓴다:
    내 pos + (패턴 픽셀 - 캐릭터 패턴 픽셀) / grid_size, 반올림

격수는 "바라보는 방향 1칸 앞"만 때릴 수 있어서 대각선 공격이 불가능하다.
그래서 접근 목표는 몬스터 좌표가 아니라 몬스터의 상하좌우 4칸 중 하나다.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque

_DIRS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


def chebyshev(a, b) -> int:
    """대각선을 1칸으로 세는 거리. 이 저장소의 기존 근접 판정과 같은 기준."""
    return max(abs(int(a[0]) - int(b[0])), abs(int(a[1]) - int(b[1])))


def attack_dir(my_pos, target_pos) -> str | None:
    """상하좌우 정확히 1칸이면 그 방향 이름, 아니면 None (대각선은 공격 불가)."""
    d = (int(target_pos[0]) - int(my_pos[0]), int(target_pos[1]) - int(my_pos[1]))
    for name, delta in _DIRS.items():
        if d == delta:
            return name
    return None


def approach_cell(my_pos, target_pos, walls=()):
    """몬스터의 상하좌우 4칸 중, 벽이 아니면서 내게 가장 가까운 칸.

    몬스터 좌표 자체를 목표로 주면 몬스터가 그 칸을 막고 있어서 '도착'이
    영원히 오지 않고, 벽 학습이 그 칸을 벽으로 잘못 기억한다.
    """
    walls = {(int(x), int(y)) for x, y in walls}
    tx, ty = int(target_pos[0]), int(target_pos[1])
    cands = [(tx + dx, ty + dy) for dx, dy in _DIRS.values()]
    cands = [c for c in cands if c not in walls]
    if not cands:
        return None
    return min(cands, key=lambda c: (chebyshev(my_pos, c),
                                     abs(c[0] - my_pos[0]) + abs(c[1] - my_pos[1])))


_STEPS = (("up", 0, -1), ("down", 0, 1), ("left", -1, 0), ("right", 1, 0))


def path_step(start, goal, blocked, radius: int = 12) -> str | None:
    """start에서 goal로 가는 첫 걸음의 방향. 길이 없으면 None.

    목표 쪽으로 한 칸씩 가는 방식(greedy)은 벽을 못 돈다 - 실측에서 격수가
    y=14에 갇혔다. 아래로 내려가는 통로가 x축으로 몇 칸 떨어져 있는데, 매
    사이클 "아래로" 를 고르니 원리적으로 영원히 못 간다. 폭이 좁은 격자라
    BFS면 충분하고 A*까지 갈 이유가 없다.

    radius: start 기준 이 칸수 밖은 탐색하지 않는다(맵 경계를 모르므로
            무한히 퍼지는 것만 막는다).
    """
    start = (int(start[0]), int(start[1]))
    goal = (int(goal[0]), int(goal[1]))
    if start == goal:
        return None
    blocked = {(int(c[0]), int(c[1])) for c in (blocked or ())}
    if goal in blocked:
        return None

    # 각 칸에 '거기 도달하려면 처음에 어느 쪽으로 갔어야 하나'를 같이 들고 다닌다.
    seen = {start}
    q = deque()
    for name, dx, dy in _STEPS:
        nxt = (start[0] + dx, start[1] + dy)
        if nxt in blocked or nxt[0] < 0 or nxt[1] < 0:
            continue
        if nxt == goal:
            return name
        seen.add(nxt)
        q.append((nxt, name))

    while q:
        (cx, cy), first = q.popleft()
        for _, dx, dy in _STEPS:
            nxt = (cx + dx, cy + dy)
            if nxt in seen or nxt in blocked or nxt[0] < 0 or nxt[1] < 0:
                continue
            if abs(nxt[0] - start[0]) + abs(nxt[1] - start[1]) > radius:
                continue
            if nxt == goal:
                return first
            seen.add(nxt)
            q.append((nxt, first))
    return None


def pick_target(monsters, my_pos, anchor, leash, sticky=None, sticky_name=None,
                sticky_slack=2):
    """쫓을 몬스터 하나를 고른다.

    monsters: [{"name":..., "world":(x,y)}, ...]  (state.detected_monsters_world)
    anchor  : 현재 순찰 포인트. 여기서 leash칸 밖은 쫓지 않는다.
    sticky  : 직전에 쫓던 대상의 pos(+이름). 아직 근처에 있으면 그놈을 유지한다
              (안 그러면 비슷한 거리의 두 마리 사이에서 타겟이 매 사이클
              바뀌며 제자리에서 떨린다).
    """
    live = [m for m in monsters
            if m.get("world") and chebyshev(anchor, m["world"]) <= leash]
    if not live:
        return None
    if sticky:
        near = [m for m in live if chebyshev(sticky, m["world"]) <= sticky_slack]
        if near:
            # 이름이 같은 놈을 먼저 본다. 위치만으로 고르면 두 마리가 같은
            # 거리에 있을 때 매 사이클 다른 놈이 뽑혀서 제자리에서 떨린다.
            return min(near, key=lambda m: (m.get("name") != sticky_name,
                                            chebyshev(sticky, m["world"])))
    return min(live, key=lambda m: chebyshev(my_pos, m["world"]))


class WallMemory:
    """벽 학습 메모리 (maps.json에 맵 이름별로 저장).

    규칙 세 가지가 다 있어야 망가지지 않는다:
      1. 막힌 칸에 몬스터가 감지 중이면 세지 않는다 - 선공 몬스터에 잠깐
         막힌 것뿐인데 통로를 영구히 벽으로 기억해버린다.
      2. 시간대가 다른 실패가 3번 쌓여야 벽으로 확정한다.
      3. 나중에 그 칸을 실제로 밟으면 벽 기록을 지운다 (반증 우선).
    맵 이름을 모르면 아무것도 기록하지 않는다 - 틀린 이름으로 저장하면
    다른 맵의 벽이 섞여서 영구히 오염된다.
    """

    CONFIRM_HITS = 3
    MIN_GAP = 2.0      # 같은 칸의 실패를 다른 '시간대'로 칠 최소 간격(초)
    FAIL_TTL = 120.0   # 오래된 미확정 실패는 잊는다
    WALKED_CAP = 5000  # 맵당 밟은 좌표 저장 상한
    SAVE_GAP = 10.0

    def __init__(self, path: str, confirm_hits: int | None = None):
        self.path = path
        self.CONFIRM_HITS = int(confirm_hits or self.CONFIRM_HITS)
        self._db = self._load()
        self._fails: dict[tuple, list] = {}   # (map, x, y) -> [hits, last_time]
        self._dirty = False
        self._last_save = 0.0

    # ---------- 저장소 ----------
    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _map_entry(self, map_name: str) -> dict:
        entry = self._db.get(map_name)
        if not isinstance(entry, dict):
            entry = {}
            self._db[map_name] = entry
        return entry

    def save(self, force: bool = False) -> bool:
        now = time.time()
        if not self._dirty:
            return False
        if not force and (now - self._last_save) < self.SAVE_GAP:
            return False
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._db, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception as exc:
            print(f"[WallMem] save failed: {exc}")
            return False
        self._dirty = False
        self._last_save = now
        return True

    # ---------- 조회 ----------
    def walls(self, map_name: str) -> set:
        if not map_name:
            return set()
        raw = self._map_entry(map_name).get("walls") or []
        out = set()
        for cell in raw:
            try:
                out.add((int(cell[0]), int(cell[1])))
            except Exception:
                continue
        return out

    def is_wall(self, map_name: str, cell) -> bool:
        return (int(cell[0]), int(cell[1])) in self.walls(map_name)

    # ---------- 학습 ----------
    def record_block(self, map_name: str, cell, monster_cells=(), now: float | None = None) -> bool:
        """이동 실패 1회 기록. 새로 벽으로 확정되면 True."""
        if not map_name:
            return False
        cell = (int(cell[0]), int(cell[1]))
        if cell in {(int(c[0]), int(c[1])) for c in monster_cells}:
            return False          # 규칙 1: 몬스터가 막은 것이므로 학습하지 않음
        now = time.time() if now is None else now
        self._prune_fails(now)
        key = (map_name, cell[0], cell[1])
        hits, last = self._fails.get(key, [0, 0.0])
        if now - last < self.MIN_GAP:
            return False          # 규칙 2: 같은 시간대의 연속 실패는 1회로 침
        hits += 1
        self._fails[key] = [hits, now]
        if hits < self.CONFIRM_HITS:
            return False
        entry = self._map_entry(map_name)
        walls = entry.get("walls")
        if not isinstance(walls, list):
            walls = []
            entry["walls"] = walls
        if [cell[0], cell[1]] in walls:
            return False
        walls.append([cell[0], cell[1]])
        self._fails.pop(key, None)
        self._dirty = True
        print(f"[WallMem] wall confirmed: {map_name} {cell}")
        return True

    def record_walked(self, map_name: str, cell) -> bool:
        """실제로 밟은 칸. 벽 기록이 있었으면 지우고 True."""
        if not map_name:
            return False
        cell = (int(cell[0]), int(cell[1]))
        self._fails.pop((map_name, cell[0], cell[1]), None)
        entry = self._map_entry(map_name)
        erased = False
        walls = entry.get("walls")
        if isinstance(walls, list) and [cell[0], cell[1]] in walls:
            walls.remove([cell[0], cell[1]])
            erased = True
            self._dirty = True
            print(f"[WallMem] wall erased (walked): {map_name} {cell}")
        walked = entry.get("walked")
        if not isinstance(walked, list):
            walked = []
            entry["walked"] = walked
        if [cell[0], cell[1]] not in walked:
            if len(walked) < self.WALKED_CAP:
                walked.append([cell[0], cell[1]])
                self._dirty = True
        return erased

    def _prune_fails(self, now: float):
        stale = [k for k, v in self._fails.items() if now - v[1] > self.FAIL_TTL]
        for k in stale:
            self._fails.pop(k, None)


def demo():
    """게임 없이 돌려보는 자체 점검. `python svc_hunt.py`로 실행."""
    import tempfile

    # 공격 방향: 상하좌우 1칸만, 대각선은 불가
    assert attack_dir((5, 5), (6, 5)) == "right"
    assert attack_dir((5, 5), (5, 4)) == "up"
    assert attack_dir((5, 5), (6, 6)) is None      # 대각선
    assert attack_dir((5, 5), (7, 5)) is None      # 2칸
    assert attack_dir((5, 5), (5, 5)) is None      # 같은 칸

    # 접근 칸: 몬스터 자신이 아니라 그 옆칸, 벽은 제외
    assert approach_cell((0, 5), (5, 5)) == (4, 5)
    assert approach_cell((0, 5), (5, 5), walls=[(4, 5)]) in {(5, 4), (5, 6)}
    assert approach_cell((0, 5), (5, 5),
                         walls=[(4, 5), (5, 4), (5, 6), (6, 5)]) is None

    # 경로: 벽을 돌아서 간다 (greedy로는 못 푸는 배치)
    #   . . . .        (0,0)에서 (0,2)로 갈 때 (0,1)이 벽이면
    #   # # . .        오른쪽으로 돌아가야 한다 - "아래로"만 고르면 영원히 못 간다.
    #   . . . .
    assert path_step((0, 0), (0, 2), blocked=[(0, 1), (1, 1)]) == "right"
    assert path_step((0, 0), (2, 0), blocked=[]) == "right"
    assert path_step((0, 0), (0, 0), blocked=[]) is None
    assert path_step((0, 0), (5, 5), blocked=[(5, 5)]) is None      # 목표가 벽
    # 사방이 막히면 길 없음
    assert path_step((3, 3), (9, 9),
                     blocked=[(2, 3), (4, 3), (3, 2), (3, 4)]) is None
    # 벽이 탐색 반경 전체를 가로막으면 길 없음 (radius 밖은 안 본다)
    wall = [(x, 1) for x in range(0, 20)]
    assert path_step((5, 0), (5, 2), blocked=wall) is None
    # 같은 벽이라도 끝이 뚫려 있으면 돌아서 간다
    gap = [(x, 1) for x in range(0, 20) if x != 9]
    assert path_step((5, 0), (5, 2), blocked=gap) == "right"

    # 타겟 선택: 가까운 놈, leash 밖은 무시, sticky는 유지
    mons = [{"name": "달걀", "world": (3, 0)}, {"name": "불귀신", "world": (1, 0)}]
    assert pick_target(mons, (0, 0), (0, 0), 6)["world"] == (1, 0)
    assert pick_target(mons, (0, 0), (0, 0), 2)["world"] == (1, 0)
    assert pick_target([{"name": "x", "world": (9, 9)}], (0, 0), (0, 0), 6) is None
    # 쫓던 놈(3,0)이 한 칸 움직여도 그놈을 유지 - 더 가까운 놈이 있어도 안 바꿈
    moved = [{"name": "달걀", "world": (4, 0)}, {"name": "불귀신", "world": (1, 0)}]
    assert pick_target(moved, (0, 0), (0, 0), 6, sticky=(3, 0))["world"] == (4, 0)
    # 같은 거리에 두 마리가 있으면 이름이 같은 쪽을 유지한다 (타겟 떨림 방지)
    tie = [{"name": "달걀", "world": (2, 0)}, {"name": "불귀신", "world": (4, 0)}]
    assert pick_target(tie, (0, 0), (0, 0), 6, sticky=(3, 0),
                       sticky_name="불귀신")["name"] == "불귀신"

    # 벽 학습
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "maps.json")
        wm = WallMemory(path)
        t = 1000.0
        # 몬스터가 막은 칸은 몇 번을 실패해도 벽이 아니다
        for i in range(5):
            assert wm.record_block("흉가2", (3, 3), monster_cells=[(3, 3)], now=t + i * 5) is False
        assert wm.is_wall("흉가2", (3, 3)) is False
        # 같은 순간의 연속 실패는 1회로만 센다
        assert wm.record_block("흉가2", (7, 7), now=t) is False
        assert wm.record_block("흉가2", (7, 7), now=t + 0.5) is False
        assert wm.record_block("흉가2", (7, 7), now=t + 5) is False
        assert wm.record_block("흉가2", (7, 7), now=t + 10) is True   # 3번째
        assert wm.is_wall("흉가2", (7, 7)) is True
        # 맵이 다르면 섞이지 않는다
        assert wm.is_wall("흉가1", (7, 7)) is False
        # 실제로 밟으면 벽 기록이 지워진다
        assert wm.record_walked("흉가2", (7, 7)) is True
        assert wm.is_wall("흉가2", (7, 7)) is False
        # 맵 이름을 모르면 아무것도 기록하지 않는다
        assert wm.record_block("", (1, 1), now=t) is False
        assert wm.save(force=True) is True
        assert WallMemory(path).walls("흉가2") == set()

    print("svc_hunt demo OK")


if __name__ == "__main__":
    demo()
