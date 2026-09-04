# -*- coding: utf-8 -*-
"""자동사냥 사이클 점검 (게임 없이 상태값만으로 검증).

LogicSvc는 이동을 직접 하지 않는다 - state.item_pickup_target에 목표를
넣으면 RouteSvc가 걸어간다. 여기서는 그 '목표를 제대로 정하는지'와
공격/줍기 키가 언제 나가는지를 본다.
"""

import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import svc_hunt as hunt          # noqa: E402
import svc_logic                 # noqa: E402


CFG = {
    "enabled": True,
    "leash_tiles": 6,
    "attack_key": {"격수": "3"},
    "attack_repeat_sec": 0.35,
    "pickup_key": ",",
    "pickup_retry": 3,
    "target_lost_sec": 8.0,
    "stationary_gap_sec": 0.25,
}


# 테스트가 실제 키를 OS로 내보내지 않도록 하드웨어 입력을 통째로 가로챈다.
MOVES: list = []
svc_logic.hw.hold_move = lambda d, **kw: MOVES.append(d) or True
svc_logic.hw.humanized_press = lambda *a, **kw: None


def make_logic(**state_kwargs):
    """스레드/하드웨어 없이 LogicSvc의 사냥 부분만 세운다."""
    MOVES.clear()
    logic = object.__new__(svc_logic.LogicSvc)
    state = types.SimpleNamespace(
        x=10, y=10,
        role="격수",
        service_active=True,
        my_screen_pos=(500, 400),
        detected_monsters_world=[],
        detected_items_world=[],
        item_pickup_target=None,
        item_pickup_arrived=False,
        inventory_full=False,
        is_user_detected=False,
        nav_current_route_point=(10, 10),
        current_map="흉가2",
        maps_db={},
        last_move_time=0.0,
        target_locked=False,
        target_name="",
    )
    for k, v in state_kwargs.items():
        setattr(state, k, v)
    logic.state = state
    logic._hunt_cfg_cache = (CFG, time.time())
    logic._hunt_sticky = None
    logic._hunt_sticky_name = ""
    logic._hunt_target_since = 0.0
    logic._hunt_giveup_until = {}
    logic._item_job = None
    logic._last_hunt_log_time = 0.0
    logic._warrior_next_attack_at = 0.0

    keys = []
    logic._press_hw_key = lambda key, **kw: keys.append(key) or True
    logic._needs_hp_recovery = lambda: False
    logic._needs_mp_recovery = lambda: False

    def _fake_search_target():
        state.target_locked = True
        return True
    logic._search_target_v3 = _fake_search_target
    return logic, state, keys


def test_approaches_adjacent_cell_not_the_monster_tile():
    logic, state, keys = make_logic(
        detected_monsters_world=[{"name": "달걀", "world": (14, 10)}])
    logic._run_warrior_attack_loot_cycle()
    # 몬스터 칸(14,10)이 아니라 그 옆칸을 목표로 잡아야 한다.
    assert state.item_pickup_target == (13, 10), state.item_pickup_target
    assert keys == []          # 아직 멀어서 공격 안 함


def test_attacks_when_orthogonally_adjacent():
    logic, state, keys = make_logic(
        detected_monsters_world=[{"name": "달걀", "world": (11, 10)}])
    # 1회차: 붙었지만 아직 target_locked가 아니다 - Tab으로 lock부터 하고
    # 이번 사이클엔 공격하지 않는다 (대상선택박스 방지, red_tab 없이 공격
    # 스킬을 쏘면 박스가 뜨고 그 뒤 방향키가 캐릭터 대신 박스를 움직인다).
    logic._run_warrior_attack_loot_cycle()
    assert MOVES == ["right"], MOVES      # 몬스터 쪽으로 회전
    assert keys == [], keys               # 아직 공격 안 함
    assert state.target_locked is True    # lock 됨
    assert state.item_pickup_target is None   # 붙었으니 이동 목표는 해제

    # 2회차: 이미 lock된 상태 - 이제 공격키가 나간다.
    logic._run_warrior_attack_loot_cycle()
    assert keys == ["3"], keys


def test_diagonal_monster_is_not_attacked():
    logic, state, keys = make_logic(
        detected_monsters_world=[{"name": "달걀", "world": (11, 11)}])
    logic._run_warrior_attack_loot_cycle()
    assert keys == []                      # 대각선은 때릴 수 없다
    assert MOVES == []
    assert state.item_pickup_target in {(11, 10), (10, 11)}, state.item_pickup_target


def test_monster_outside_leash_is_ignored():
    logic, state, keys = make_logic(
        detected_monsters_world=[{"name": "달걀", "world": (30, 10)}])
    logic._run_warrior_attack_loot_cycle()
    assert state.item_pickup_target is None
    assert keys == []


def test_item_target_only_taken_while_stationary():
    logic, state, keys = make_logic(
        detected_items_world=[{"name": "호박", "world": (12, 10)}],
        last_move_time=time.time())          # 방금 이동키가 나감 = 걷는 중
    logic._run_warrior_attack_loot_cycle()
    assert state.item_pickup_target is None  # 걷는 중엔 좌표를 고정하지 않는다
    assert logic._item_job is None

    state.last_move_time = time.time() - 1.0  # 멈춘 지 1초
    logic._run_warrior_attack_loot_cycle()
    assert state.item_pickup_target == (12, 10)
    assert logic._item_job["pos"] == (12, 10)


def test_inventory_full_skips_pickup_but_not_hunting():
    logic, state, keys = make_logic(
        detected_items_world=[{"name": "호박", "world": (12, 10)}],
        inventory_full=True,
        last_move_time=time.time() - 1.0)
    logic._run_warrior_attack_loot_cycle()
    assert state.item_pickup_target is None
    assert logic._item_job is None


def test_pickup_presses_comma_on_exact_arrival():
    logic, state, keys = make_logic(
        detected_items_world=[{"name": "호박", "world": (12, 10)}],
        last_move_time=time.time() - 1.0)
    logic._run_warrior_attack_loot_cycle()          # 목표 설정
    state.item_pickup_arrived = True
    state.detected_items_world = []                 # 주워져서 사라짐
    logic._run_warrior_attack_loot_cycle()
    assert keys == [","], keys
    assert logic._item_job is None                  # 줍기 완료


def test_pickup_retries_on_neighbour_when_item_still_there():
    logic, state, keys = make_logic(
        detected_items_world=[{"name": "호박", "world": (12, 10)}],
        last_move_time=time.time() - 1.0)
    logic._run_warrior_attack_loot_cycle()
    state.item_pickup_arrived = True
    # ',' 를 눌러도 아이템이 그대로 보임 = 좌표가 한 칸 어긋났다
    state.detected_items_world = [{"name": "호박", "world": (12, 11)}]
    logic._run_warrior_attack_loot_cycle()
    assert keys == [","]
    assert state.item_pickup_target == (12, 11)     # 옆칸으로 재시도
    assert logic._item_job["tries"] == 1


def test_char_pattern_lost_stops_hunt_judgement():
    logic, state, keys = make_logic(
        my_screen_pos=(0, 0),
        detected_monsters_world=[{"name": "달걀", "world": (11, 10)}])
    logic._run_warrior_attack_loot_cycle()
    assert state.item_pickup_target is None
    assert keys == []


def test_known_wall_is_avoided_when_choosing_approach_cell():
    logic, state, keys = make_logic(
        detected_monsters_world=[{"name": "달걀", "world": (14, 10)}],
        maps_db={"흉가2": {"walls": [[13, 10]]}})
    logic._run_warrior_attack_loot_cycle()
    assert state.item_pickup_target != (13, 10)
    assert state.item_pickup_target in {(14, 9), (14, 11), (15, 10)}


def test_target_stays_sticky_while_monster_moves():
    logic, state, keys = make_logic(
        detected_monsters_world=[{"name": "달걀", "world": (14, 10)},
                                 {"name": "불귀신", "world": (12, 10)}])
    logic._run_warrior_attack_loot_cycle()
    assert logic._hunt_sticky == (12, 10)          # 가까운 놈부터
    # 그놈이 한 칸 움직여도 더 가까운 다른 놈으로 갈아타지 않는다
    state.detected_monsters_world = [{"name": "달걀", "world": (11, 10)},
                                     {"name": "불귀신", "world": (13, 10)}]
    logic._run_warrior_attack_loot_cycle()
    assert logic._hunt_sticky == (13, 10)


def test_target_lock_released_when_target_dies():
    logic, state, keys = make_logic(
        detected_monsters_world=[{"name": "달걀", "world": (11, 10)}])
    logic._run_warrior_attack_loot_cycle()   # lock
    assert state.target_locked is True
    state.detected_monsters_world = []       # 죽음/이탈
    logic._run_warrior_attack_loot_cycle()
    assert state.target_locked is False      # 다음 몬스터를 위해 풀려야 한다


def run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"{len(fns)} passed")


if __name__ == "__main__":
    hunt.demo()
    run_all()
