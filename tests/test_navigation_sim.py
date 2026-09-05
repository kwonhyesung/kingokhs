# -*- coding: utf-8 -*-
"""게임 없이 '실제로 목적지까지 걸어가는가'를 돌려본다.

이게 없어서 지금까지 수정 한 번에 게임 실행 한 번이 필요했다. 그런데 그동안
잡은 문제(BFS가 호출조차 안 됨 / 벽 기억이 6초 만에 만료 / 사방이 막힘으로
기억되어 갇힘 / 포탈을 밟고 맵 이탈)는 전부 화면 없이 재현되는 로직 버그였다.
여기서 걸리면 게임을 켤 필요가 없다.

게임이 여전히 필요한 것은 두 가지뿐이다: 하드웨어 신호(K, vs D:/U:)와
맵의 벽/포탈 좌표. 후자는 hunt_maps.json에 적어두면 이 시뮬레이터가 쓴다.

    py tests/test_navigation_sim.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from svc_hunt import path_step, chebyshev   # noqa: E402

STEPS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


class World:
    """격자 맵. 벽은 못 지나가고, 포탈을 밟으면 맵을 벗어난다."""

    def __init__(self, walls=(), portals=()):
        self.walls = {tuple(c) for c in walls}
        self.portals = {tuple(c) for c in portals}

    def move(self, pos, direction):
        dx, dy = STEPS[direction]
        nxt = (pos[0] + dx, pos[1] + dy)
        if nxt in self.walls or nxt[0] < 0 or nxt[1] < 0:
            return pos, False          # 벽 - 제자리
        return nxt, nxt in self.portals


def walk(world, start, goal, known=(), max_steps=300, learn=True):
    """실제 path_step으로 goal까지 걸어본다.

    known: 출발 시점에 이미 아는 막힌 칸(hunt_maps.json에 적어둔 것).
    learn: 벽에 부딪히면 그 칸을 기억한다(봇의 no_move 학습과 같은 동작).

    반환: (도착했나, 걸음수, 맵을벗어났나)
    """
    pos = tuple(start)
    blocked = {tuple(c) for c in known}
    for n in range(max_steps):
        if pos == tuple(goal):
            return True, n, False
        direction = path_step(pos, goal, blocked, radius=24)
        if direction is None:
            return False, n, False     # 길이 없다고 판단
        nxt, left_map = world.move(pos, direction)
        if left_map:
            return False, n, True
        if nxt == pos and learn:
            dx, dy = STEPS[direction]
            blocked.add((pos[0] + dx, pos[1] + dy))
        pos = nxt
    return False, max_steps, False     # 걸음 수 초과 = 맴돌았다


def hyunga1():
    """hunt_maps.json에 적어둔 흉가1 실제 지도."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = json.load(open(os.path.join(here, "hunt_maps.json"), encoding="utf-8"))
    walls = cfg["known_walls"]["흉가1"]
    portals = cfg["avoid_cells"]["흉가1"]
    return World(walls, portals), walls, portals


def test_reaches_monster_across_the_wall_when_map_is_known():
    """설정에 적어둔 벽/포탈을 알면 첫 시도에 몬스터 옆까지 간다."""
    world, walls, portals = hyunga1()
    arrived, steps, left = walk(world, (5, 14), (9, 17), known=walls + portals)
    assert arrived and not left, f"도착={arrived} 이탈={left} 걸음={steps}"
    assert steps < 30, f"너무 돌아간다: {steps}걸음"


def test_learns_the_wall_and_still_arrives_when_map_is_unknown():
    """새 맵이라 아무것도 모르는 상태 - 부딪히며 배워서 결국 도착해야 한다.
    벽 기억이 곧 만료되던 시절엔 여기서 영원히 맴돌았다."""
    world, _, portals = hyunga1()
    arrived, steps, left = walk(world, (5, 14), (9, 17), known=portals)
    assert arrived, f"학습으로도 도착 못함 (걸음={steps}, 이탈={left})"


def test_walks_into_the_portal_when_it_is_not_configured():
    """포탈을 안 적어두면 맵을 벗어난다 - 실제로 두 번 그랬다.
    이 테스트가 실패하면 avoid_cells가 무의미해진 것이다."""
    world, walls, _ = hyunga1()
    _, _, left = walk(world, (0, 14), (0, 10), known=walls)
    assert left, "포탈을 모르는데도 안 밟았다면 이 시나리오가 낡은 것"


def test_never_enters_a_portal_when_configured():
    """포탈을 알면 어디서 어디로 가든 밟지 않는다."""
    world, walls, portals = hyunga1()
    known = walls + portals
    for start in [(0, 14), (2, 14), (5, 14), (0, 16)]:
        for goal in [(0, 10), (9, 17), (3, 11)]:
            _, _, left = walk(world, start, goal, known=known)
            assert not left, f"{start} -> {goal} 에서 포탈을 밟았다"


def test_gives_up_instead_of_looping_when_sealed():
    """정말 길이 없으면 맴돌지 말고 없다고 답해야 한다."""
    sealed = World(walls=[(x, 15) for x in range(0, 40)])
    arrived, steps, left = walk(sealed, (5, 14), (9, 17), max_steps=80)
    assert not arrived and not left
    assert steps < 80, "길이 없는데 걸음 수를 다 쓸 때까지 맴돌았다"


def test_every_movement_path_sees_the_configured_walls():
    """설정에 적어둔 벽/포탈은 '모든' 이동 판단에 들어가야 한다.

    실측 사고: avoid_cells에 포탈을 적어뒀는데도 열 번 왕복했다. 원인은
    _get_blocked_cells()가 학습분만 돌려주고 설정분은 BFS 호출부에서만
    따로 합쳐진 것 - 평소 경로는 포탈을 몰랐다. 경로 계산이 여러 갈래여도
    막힌 칸 집합은 한 곳에서 나와야 한다.
    """
    import svc_route

    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "svc_route.py"), encoding="utf-8").read()
    body = src.split("def _get_blocked_cells")[1].split("def ")[0]
    assert "_known_wall_cells()" in body,         "_get_blocked_cells()가 설정 벽/포탈을 포함하지 않는다"

    # 그리고 그 함수 밖에서 따로 합치는 곳이 남아 있으면 안 된다(빠뜨리기 쉽다)
    others = src.count("_known_wall_cells()")
    assert others == 1, f"_known_wall_cells()를 {others}곳에서 쓴다 - 한 곳으로 모을 것"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("test_navigation_sim OK")
