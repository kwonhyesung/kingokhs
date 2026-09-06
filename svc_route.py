"""
svc_route.py  -  BIS Route Engine (자동 이동/길찾기 스레드)
──────────────────────────────────────────────────────────────
Grid 좌표 변환, stuck 감지 및 복구, follow 이동을 담당.
svc_logic.py(LogicSvc)와 상태값(GameState)을 동기화하여 동작.
──────────────────────────────────────────────────────────────

Integration:
    - svc_logic.py: 전투/지원 로직과 상태 동기화
    - bis_core.py: GridManager, GameState, hw(하드웨어 입력)
    - patrol_routes.py: 순찰 경로 포인트 파싱
    - support_runtime_rules.py: follow/stuck 판단 순수 함수
"""

import os
import json
import time
import threading
import random

from bis_core import (
    hw, GameState, TIMING_CONFIG, humanized_sleep, GridManager, discord_notify,
)
from patrol_routes import parse_route_point
import svc_hunt as hunt
from svc_hunt import WallMemory
_MAPS_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps.json")
_CONFIG_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

from support_runtime_rules import (
    adjust_follow_target_by_axis_gap,
    dir_between_coords,
    follow_manhattan_gap,
    is_plausible_map_coord,
    normalize_move_dir,
    portal_cache_key,
    should_allow_follow_navigation,
    should_clear_target_box_after_support_stuck,
    should_defer_stuck_escape_for_support,
    should_hold_follow_position,
    should_retarget_after_support_follow_stuck,
    should_detect_warrior_transition,
    should_prioritize_follow_distance,
    WARRIOR_TRANSITION_JUMP_DISTANCE,
    is_plausible_transition_coord,
    is_implausible_walk_speed,
    fingerprint_hamming_distance,
    MAP_FINGERPRINT_SAME_MAX_DISTANCE,
    classify_map_sync,
)


def _nav_step_from_dir(cx: int, cy: int, direction: str) -> tuple[int, int]:
    if direction == "up":
        return cx, cy - 1
    if direction == "down":
        return cx, cy + 1
    if direction == "left":
        return cx - 1, cy
    if direction == "right":
        return cx + 1, cy
    return cx, cy


def nav_cell_blockers(state: GameState, gx: int, gy: int, include_entities: bool = True) -> list[str]:
    """(gx,gy)는 pos(x,y) 좌표. walls도 entities의 world_pos도 전부 pos 기준이다
    (grid 좌표계는 폐기했다 - entities에는 이제 grid 필드 자체가 없다)."""
    blockers: list[str] = []
    try:
        map_name = getattr(state, "current_map", None)
        map_data = getattr(state, "maps_db", {}).get(map_name) if map_name else None
        if map_data:
            walls = map_data.get("walls", [])
            if [gx, gy] in walls:
                blockers.append("wall")
    except Exception:
        pass

    if include_entities:
        try:
            entities = getattr(state, "entities", {}) or {}
            for kind, label in (("monsters", "monster"), ("users", "user")):
                for ent in entities.get(kind, []):
                    if not isinstance(ent, dict):
                        continue
                    world = ent.get("world_pos")
                    if not world or len(world) != 2:
                        continue
                    if int(world[0]) == gx and int(world[1]) == gy:
                        blockers.append(label)
                        break
        except Exception:
            pass

    return list(dict.fromkeys(blockers))


def nav_pick_step_direction(
    state: GameState,
    current_grid: tuple[int, int],
    dx: int,
    dy: int,
    include_entities: bool = True,
    blocked_cells: set[tuple[int, int]] | None = None,
    prefer_manhattan_reduction: bool = False,
    aggressive_follow: bool = False,
) -> tuple[str | None, tuple[int, int] | None, list[str]]:
    if dx == 0 and dy == 0:
        return None, None, []

    ccx, ccy = current_grid

    if abs(dx) > abs(dy):
        axis_priority = ["x", "y"]
    elif abs(dy) > abs(dx):
        axis_priority = ["y", "x"]
    else:
        axis_priority = random.choice([["x", "y"], ["y", "x"]])

    candidates: list[str] = []

    def add_candidate(direction: str):
        if direction not in candidates:
            candidates.append(direction)

    for axis in axis_priority:
        if axis == "x" and dx != 0:
            add_candidate("right" if dx > 0 else "left")
        elif axis == "y" and dy != 0:
            add_candidate("down" if dy > 0 else "up")

    if aggressive_follow and dx != 0 and dy != 0:
        primary_x = "right" if dx > 0 else "left"
        primary_y = "down" if dy > 0 else "up"
        if abs(dx) >= abs(dy):
            aggressive_order = [primary_x, primary_y, "up" if dy > 0 else "down", "left" if dx > 0 else "right"]
        else:
            aggressive_order = [primary_y, primary_x, "left" if dx > 0 else "right", "up" if dy > 0 else "down"]
        candidates = []
        for direction in aggressive_order:
            add_candidate(direction)

    if abs(dx) >= abs(dy):
        if dy > 0:
            add_candidate("down")
            add_candidate("up")
        elif dy < 0:
            add_candidate("up")
            add_candidate("down")
        else:
            for direction in random.choice([["up", "down"], ["down", "up"]]):
                add_candidate(direction)
    else:
        if dx > 0:
            add_candidate("right")
            add_candidate("left")
        elif dx < 0:
            add_candidate("left")
            add_candidate("right")
        else:
            for direction in random.choice([["left", "right"], ["right", "left"]]):
                add_candidate(direction)

    if candidates:
        opposite = {
            "up": "down",
            "down": "up",
            "left": "right",
            "right": "left",
        }
        add_candidate(opposite[candidates[0]])

    if prefer_manhattan_reduction and candidates:
        step_delta = {
            "up": (0, -1),
            "down": (0, 1),
            "left": (-1, 0),
            "right": (1, 0),
        }
        original_order = {direction: idx for idx, direction in enumerate(candidates)}
        candidates = sorted(
            candidates,
            key=lambda direction: (
                abs(dx - step_delta[direction][0]) + abs(dy - step_delta[direction][1]),
                original_order[direction],
            ),
        )

    blockers: list[str] = []
    blocked_cells = blocked_cells or set()
    for direction in candidates:
        nx, ny = _nav_step_from_dir(ccx, ccy, direction)
        if (nx, ny) in blocked_cells:
            blockers.append("blocked_memory")
            continue
        cell_blockers = nav_cell_blockers(state, nx, ny, include_entities=include_entities)
        if not cell_blockers:
            return direction, (nx, ny), []
        blockers.extend(cell_blockers)

    return None, None, list(dict.fromkeys(blockers))


# ============================================================
#  NavigationThread  ?  吏?ν삎 ?대룞 + Stuck ?덉텧
# ============================================================
class RouteSvc(threading.Thread):
    """?⑥씠?ъ씤???대룞 + 洹몃９??異붿쟻 + 吏?ν삎 Stuck ?덉텧."""

    # 諛⑺뼢 ?뺤쓽
    _ALL_DIRS  = ["up", "down", "left", "right"]
    _HORIZ     = ["left", "right"]
    _VERT      = ["up", "down"]

    def __init__(self, state: GameState):
        super().__init__(daemon=True)
        self.state          = state
        self.wp_idx         = 0
        self.last_load_time = time.time()
        self._last_coord_check_time = time.time()
        self._last_checked_pos      = (0, 0)
        # 吏?ν삎 ?뚰뵾 濡쒖쭅??蹂??
        self.last_pos = None
        self.stuck_timer = 0.0
        self.stuck_count = 0
        # ?쒗??湲곕컲 ?대룞??蹂??
        self.seq_phase = "entry"  # entry, points, exit
        self.seq_point_idx = 0
        self.seq_last_action_time = 0
        self._was_in_combat = False
        self._current_nav_context = None
        self._combat_resume_context = None
        self._advance_waypoint_after_combat = False
        self._nav_current_pos = None
        self._nav_attempt_pos = None
        self._nav_attempt_started_at = 0.0
        self._last_follow_close_log_time = 0.0
        self._last_nav_trace_time = 0.0
        self._last_nav_trace_signature = None
        self._last_nav_trace_target = None
        self._last_nav_trace_pos = None
        self._last_nav_trace_gap = None
        self._blocked_cells_until: dict[tuple[int, int], float] = {}
        # 벽 학습: 이동 실패가 같은 칸에서 시간대를 달리해 3번 쌓이면 벽으로
        # 확정하고 maps.json에 남긴다. 몬스터가 막은 칸은 세지 않고, 나중에
        # 실제로 밟으면 지운다 (svc_hunt.WallMemory).
        self._walls = WallMemory(_MAPS_JSON_PATH)
        self._last_walked_cell = None
        self._last_pickup_blocked_by_follow_log_time = 0.0
        self._last_combat_busy_block_log_time = 0.0
        self._last_move_facing_log_time = 0.0
        # 방향별로 '키를 보냈고 실제로 좌표가 바뀌었나'를 센다.
        # 한 방향만 0/N이면 그 쪽이 벽이거나 그 키가 안 먹는 것이고,
        # 둘 다 로그에선 똑같이 '보냈는데 안 움직임'으로 보인다.
        self._move_result_stats = {}   # dir -> [시도, 좌표변화]
        self._pending_move = None      # (dir, 보낼 때 좌표, 보낸 시각)
        self._move_fail_streak = {}    # (좌표, 방향) -> 연속 실패 횟수
        self._last_move_stats_log = 0.0
        # 순찰 포인트 도달 실패 감시
        self._patrol_point_idx_seen = None
        self._patrol_point_started_at = 0.0
        self._patrol_point_skips = {}
        try:
            with open(_CONFIG_JSON_PATH, "r", encoding="utf-8") as _f:
                self._patrol_point_timeout = float(json.load(_f).get("hunt", {}).get("patrol_point_timeout_sec", 15.0))
        except Exception:
            self._patrol_point_timeout = 15.0
        self._blocked_cell_hits: dict[tuple[int, int], int] = {}
        # _forget_neighbours로 '잘못된 벽'이라 판단해 지운 칸은 잠깐 재학습을
        # 막는다 - 안 그러면 다음 stuck 사이클이 곧바로 같은 칸을 다시
        # 막힘으로 찍어 mark/clear를 무한 반복한다(로그의 '4칸 해제' 도배).
        self._navmem_forgiven_until: dict[tuple[int, int], float] = {}
        self._navmem_forgive_sec = 2.5
        self._last_follow_detour_log = 0.0
        self._blocked_cell_base_ttl = 6.0
        self._blocked_cell_max_ttl = 18.0
        self._blocked_cell_wall_ttl = 300.0   # 벽은 안 움직인다
        self._blocked_cells_map = None        # 이 기억이 어느 맵의 것인지
        self._last_box_collision_recover_request_time = 0.0
        self._support_follow_hold_distance = 1
        self._support_follow_soft_stuck_count = 0
        self._support_follow_visited_cells: set[tuple[int, int]] = set()
        self._support_follow_escape_attempts = 0
        self._support_follow_tried_escape_dirs: set[str] = set()
        self._last_support_quick_escape_time = 0.0
        self._follow_blocked_memory_count = 0
        self._last_warrior_coord: tuple[int, int] | None = None
        self._last_warrior_coord_ts: float = 0.0
        self._last_warrior_fingerprint: str = ""
        self._last_warrior_dir: str | None = None
        self._last_warrior_step_dir: str | None = None
        self._last_warrior_map_sig: tuple[str, str, str, str] | None = None
        self._warrior_trail: list[tuple[int, int, str | None]] = []
        self._last_follow_target_key: tuple | None = None
        self._last_follow_target: tuple[int, int] | None = None
        self._last_follow_target_ts = 0.0
        self._last_map_info_change_seq = 0
        self._last_coord_transition_seq = 0
        self._portal_session_cache: dict[tuple[str, str, str, str], dict[str, object]] = {}
        self._portal_follow_active = False
        self._portal_follow_coord: tuple[int, int] | None = None
        self._portal_follow_approach: tuple[int, int] | None = None
        self._portal_follow_dir: str | None = None
        self._portal_follow_source_map_sig: tuple[str, str, str, str] | None = None
        self._portal_follow_started_at = 0.0
        self._portal_follow_timeout = 25.0
        self._last_portal_follow_log_time = 0.0
        self._last_self_portal_coord: tuple[int, int] | None = None
        self._portal_enter_attempted = False
        self._portal_enter_nudge_used = False
        self.state.portal_follow_active = False
        self.state.portal_follow_retarget_requested = False
        self.state.portal_follow_coord = None
        self.state.portal_follow_approach = None
        self.state.portal_follow_dir = None

    # ----------------------------------------------------------
    def _get_nav_grid_pos(self) -> tuple[int, int]:
        """이동/차단 판정에 쓰는 현재 좌표. 좌표계는 pos(x,y) 하나뿐이다."""
        return (int(getattr(self.state, "x", 0) or 0),
                int(getattr(self.state, "y", 0) or 0))

    def _quick_support_follow_escape(self, follow_target: tuple[int, int] | None = None) -> bool:
        """F2 follow stuck 1회차는 red_tab 재준비 대신 짧은 우회 이동만 수행한다."""
        if bool(getattr(self, "_portal_follow_active", False)):
            return False

        now = time.time()
        if (now - float(getattr(self, "_last_support_quick_escape_time", 0.0) or 0.0)) < 0.18:
            return False
        self._last_support_quick_escape_time = now

        try:
            hw.stop_all_inputs(repeat=1, delay=0.005)
        except Exception:
            pass

        ccx, ccy = self._get_nav_grid_pos()
        last_dir = str(getattr(self.state, "last_move_dir", "") or "")
        if last_dir in ("left", "right"):
            candidate_dirs = ["up", "down"]
        elif last_dir in ("up", "down"):
            candidate_dirs = ["left", "right"]
        else:
            candidate_dirs = ["left", "right", "up", "down"]

        blocked_cells = self._get_blocked_cells()
        candidate_dirs = [
            direction for direction in candidate_dirs
            if _nav_step_from_dir(int(ccx), int(ccy), direction) not in blocked_cells
        ] or ["left", "right", "up", "down"]
        # 이번 stuck 에피소드에서 이미 시도해서 실패한 방향은 다시 고르지 않는다.
        # last_move_dir 기반 후보 선정은 정상 follow 이동이 끼어들면 신뢰할 수
        # 없어서(방향이 계속 덮어써짐), 같은 방향을 몇 번이고 재시도하는 문제가
        # 있었다 - 시도한 방향 자체를 직접 기록해서 확실하게 배제한다.
        tried_dirs = self._support_follow_tried_escape_dirs
        untried_dirs = [direction for direction in candidate_dirs if direction not in tried_dirs]
        if untried_dirs:
            candidate_dirs = untried_dirs

        if follow_target:
            tx, ty = follow_target
            current_gap = follow_manhattan_gap(int(tx), int(ty), int(self.state.x), int(self.state.y))
            candidate_dirs = sorted(
                candidate_dirs,
                key=lambda direction: follow_manhattan_gap(
                    int(tx),
                    int(ty),
                    *_nav_step_from_dir(int(ccx), int(ccy), direction),
                ),
            )
            if bool(getattr(self, "_portal_follow_active", False)):
                closer_dirs = [
                    direction for direction in candidate_dirs
                    if follow_manhattan_gap(
                        int(tx),
                        int(ty),
                        *_nav_step_from_dir(int(ccx), int(ccy), direction),
                    ) <= current_gap
                ]
                if closer_dirs:
                    candidate_dirs = closer_dirs

        escape_dir = candidate_dirs[0]
        tried_dirs.add(escape_dir)
        attempts = int(getattr(self, "_support_follow_escape_attempts", 0) or 0)
        self._support_follow_escape_attempts = attempts + 1
        # 같은 자리에서 반복 실패할수록(진전 없음) 더 멀리, 더 빠르게 밀어붙여서
        # 장애물 폭을 실제로 벗어나게 한다. 1회차 0.22s -> 3회차부터 0.45s 상한.
        # 예전 0.13s 시작값은 게임의 한 칸 이동 최소 홀드 시간보다 짧아서
        # 첫 twitch가 캐릭터를 아예 안 움직여 stuck 루프를 스스로 유지했다.
        # ponytail: 상수 튜닝. 실측 이동 홀드 시간에 맞춰 조정.
        escape_duration = min(0.22 + 0.08 * attempts, 0.45)
        if attempts >= 1:
            # 회피가 2회 연속 진전 없이 실패한 지점 자체(주저앉은 칸)를 몇 초간
            # 막힌 칸으로 기억해서, 나중에 다시 여기로 되돌아오려는 경로를 피한다.
            self._remember_blocked_cell(int(ccx), int(ccy), reason="repeated_escape_fail")
        print(
            f"[Recover] support follow quick escape: {escape_dir} "
            f"(no retarget, attempt={attempts + 1}, dur={escape_duration:.3f}s)."
        )
        hw.hold_move(escape_dir, "stuck_side_hold", duration=escape_duration)
        self.state.last_move_dir = escape_dir
        self._nav_attempt_pos = (self.state.x, self.state.y)
        self._nav_attempt_started_at = time.time()
        return True

    # ----------------------------------------------------------
    def _forget_neighbours(self, cx: int, cy: int):
        """지금 칸의 상하좌우 '막힘' 기억을 지운다(맵 학습 벽은 안 건드린다)."""
        cleared = []
        now = time.time()
        for direction in ("up", "down", "left", "right"):
            cell = _nav_step_from_dir(cx, cy, direction)
            # 지웠든 아니든, 이 4칸은 잠깐 재학습을 막는다(mark/clear 무한반복 차단).
            self._navmem_forgiven_until[cell] = now + self._navmem_forgive_sec
            if self._blocked_cells_until.pop(cell, None) is not None:
                self._blocked_cell_hits.pop(cell, None)
                cleared.append(cell)
        if cleared:
            print(f"[NavMem] ({cx},{cy}) 사방이 막힘으로 기억됨 - 잘못된 기억으로 보고 "
                  f"{len(cleared)}칸 해제: {cleared} (재학습 {self._navmem_forgive_sec}s 억제)")

    def _forget_blocked_cells_on_map_change(self):
        """맵이 바뀌면 벽 기억을 버린다. 좌표계가 맵마다 달라서, 안 버리면
        새 맵의 멀쩡한 칸이 이전 맵의 벽 때문에 막힌 것으로 취급된다."""
        now_map = self._learning_map_name()
        if not now_map:
            return
        if self._blocked_cells_map != now_map:
            if self._blocked_cells_map is not None and self._blocked_cells_until:
                print(f"[NavMem] 맵 변경({self._blocked_cells_map} -> {now_map}) - 벽 기억 초기화")
            self._blocked_cells_map = now_map
            self._blocked_cells_until.clear()
            self._blocked_cell_hits.clear()

    def _prune_blocked_cells(self):
        now = time.time()
        expired = [cell for cell, until in self._blocked_cells_until.items() if until <= now]
        for cell in expired:
            self._blocked_cells_until.pop(cell, None)
            self._blocked_cell_hits.pop(cell, None)
        for cell in [c for c, until in self._navmem_forgiven_until.items() if until <= now]:
            self._navmem_forgiven_until.pop(cell, None)

    def _get_blocked_cells(self) -> set[tuple[int, int]]:
        """지나갈 수 없는 칸 전부. 학습한 것 + 설정에 적어둔 것(벽/포탈).

        예전엔 학습분만 돌려줬고, 설정에 적어둔 벽/포탈은 BFS를 부를 때만
        따로 합쳤다. 그래서 평소 이동 경로는 포탈을 몰랐고, 실측에서 흉가1과
        천안궁성흉가입구 사이를 열 번 왕복했다 - avoid_cells에 적어두고도."""
        self._prune_blocked_cells()
        return set(self._blocked_cells_until.keys()) | self._known_wall_cells()

    def _learning_map_name(self) -> str:
        """벽 학습에 쓸 맵 이름. '방금 확실히 읽은' 경우에만 돌려준다.
        current_map은 한 번 정해지면 계속 남아있어서 그것만으론 지금
        제대로 읽고 있는지 알 수 없다."""
        seen_at = float(getattr(self.state, "map_seen_at", 0.0) or 0.0)
        if time.time() - seen_at > 3.0:
            return ""
        return str(getattr(self.state, "current_map", "") or "").strip()

    def _detected_monster_cells(self) -> list:
        cells = []
        for m in (getattr(self.state, "detected_monsters_world", []) or []):
            world = m.get("world") if isinstance(m, dict) else None
            if world:
                cells.append((int(world[0]), int(world[1])))
        return cells

    def _learn_wall(self, cell):
        """이동 실패 1회를 벽 학습에 넘긴다 (확정되면 maps_db에도 즉시 반영).

        사냥/줍기 이동(item_pickup_target, nav context 없음)에서만 배운다.
        follow/순찰/포탈 이동은 자체 stuck-recovery가 이미 있고, red_tab
        타겟팅 지연처럼 벽이 아닌 이유로도 잠깐 멈춘다 - 그걸 배우면
        도사가 격수 따라가다 잠깐 막힌 자리가 그대로 영구 벽이 된다
        (실측: 흉가1 (4,13)이 follow 중 멈칫 3번으로 벽 확정됨)."""
        if self._current_nav_context is not None:
            return
        map_name = self._learning_map_name()
        if not map_name:
            return
        if not self._walls.record_block(map_name, cell, monster_cells=self._detected_monster_cells()):
            return
        try:
            entry = self.state.maps_db.setdefault(map_name, {})
            walls = entry.setdefault("walls", [])
            if [int(cell[0]), int(cell[1])] not in walls:
                walls.append([int(cell[0]), int(cell[1])])
        except Exception:
            pass
        self._walls.save()

    def _record_walked_cell(self):
        """실제로 밟은 칸을 기록한다. 벽으로 잘못 새긴 칸이면 지워진다(반증 우선)."""
        pos = (int(getattr(self.state, "x", 0) or 0), int(getattr(self.state, "y", 0) or 0))
        if pos == self._last_walked_cell or pos == (0, 0):
            return
        self._last_walked_cell = pos
        map_name = self._learning_map_name()
        if not map_name:
            return
        if self._walls.record_walked(map_name, pos):
            try:
                walls = (self.state.maps_db.get(map_name) or {}).get("walls")
                if isinstance(walls, list) and [pos[0], pos[1]] in walls:
                    walls.remove([pos[0], pos[1]])
            except Exception:
                pass
        self._walls.save()

    def _known_wall_cells(self) -> set:
        """이 맵의 벽. BFS가 우회로를 찾을 때 쓴다.

        두 곳을 합친다: maps.json(봇이 부딪혀 배운 것)과 hunt_maps.json의
        known_walls(사람이 확실히 아는 것을 미리 적어둔 것). 후자가 있으면
        첫 실행부터 우회한다 - 벽 한 칸을 배우는 데 3초 넘게 걸려서, 열 칸짜리
        벽면은 배우는 동안 계속 헤맨다."""
        cells = set()
        map_name = self._learning_map_name()
        try:
            walls = (self.state.maps_db.get(map_name) or {}).get("walls") or []
            cells |= {(int(c[0]), int(c[1])) for c in walls}
        except Exception:
            pass
        cfg = self._hunt_maps_cfg()
        for section in ("known_walls", "avoid_cells"):
            try:
                preset = (cfg.get(section) or {}).get(map_name) or []
                cells |= {(int(c[0]), int(c[1])) for c in preset}
            except Exception:
                pass
        return cells

    def _hunt_maps_cfg(self) -> dict:
        """hunt_maps.json (60초 캐시). 없으면 빈 dict."""
        now = time.time()
        cached, at = getattr(self, "_hunt_maps_cache", (None, 0.0))
        if cached is not None and now - at < 60.0:
            return cached
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hunt_maps.json")
            with open(path, encoding="utf-8") as f:
                cached = json.load(f)
            if not isinstance(cached, dict):
                cached = {}
        except Exception:
            cached = {}
        self._hunt_maps_cache = (cached, now)
        return cached

    def _coord_fresh_since(self, t0: float) -> bool:
        """t0 이후에 '캡처된' 화면으로 좌표를 다시 읽었는가.

        좌표(state.x/y)는 화면 OCR이라 키를 보낸 뒤에도 한동안 이동 전 값이
        남는다. 그 값으로 '안 움직였다'를 판정하면, 실제로 밟고 지나간 칸까지
        벽으로 기억해서 스스로 길을 막는다. 실측 로그:
          (10,11)에서 right 전송 -> 0.55초 만에 stuck 판정, (11,11)을 90초
          차단 + 이웃 2칸까지 cluster 차단 -> 엉뚱한 방향(up)으로 탈출.
          그런데 바로 다음 줄의 위치는 (12,11) - (11,11)을 지나간 것이다.
        follow 중엔 stuck 한계가 0.35초까지 내려가는데(격수가 멀수록 짧아짐)
        좌표 갱신 주기가 그보다 길면 100% 오판이 된다.

        numeric_last_update_time = 숫자 OCR이 마지막으로 한 프레임을 처리한
        시각, capture_age_ms = 그 프레임이 찍힌 뒤 흐른 시간. 둘을 빼면 그
        좌표가 '언제 찍힌 화면'인지가 나온다."""
        if t0 <= 0.0:
            return True
        last_read = float(getattr(self.state, "numeric_last_update_time", 0.0) or 0.0)
        if last_read <= 0.0:
            return True   # 이 신호가 없는 환경에서는 예전처럼 동작
        age_sec = float(getattr(self.state, "capture_age_ms", 0.0) or 0.0) / 1000.0
        return (last_read - age_sec) > t0

    def _remember_blocked_cell(self, gx: int, gy: int, reason: str = "stuck"):
        self._prune_blocked_cells()
        self._forget_blocked_cells_on_map_change()
        cell = (int(gx), int(gy))
        forgiven_until = float(self._navmem_forgiven_until.get(cell, 0.0) or 0.0)
        if forgiven_until > time.time():
            # 방금 '잘못된 벽'으로 지운 칸 - 억제창 동안은 다시 안 찍는다.
            return
        if forgiven_until:
            self._navmem_forgiven_until.pop(cell, None)
        hits = int(self._blocked_cell_hits.get(cell, 0)) + 1
        self._blocked_cell_hits[cell] = hits
        # 몬스터가 서 있어서 막힌 칸은 곧 비워지므로 짧게 잡는다. 진짜 벽은
        # 세션 내내 그 자리이므로 오래 기억해야 한다 - 6초로는 벽 하나를
        # 배우는 데 걸리는 시간(2회 실패 = 약 3.4초)을 감안하면 열 칸짜리
        # 벽면을 다 그리기도 전에 앞쪽을 잊어서, 지도가 영영 완성되지 않는다
        # (실측: 흉가1의 y=15가 x=1~10까지 막혀 있는데 계속 다시 부딪힘).
        if cell in self._detected_monster_cells():
            ttl = self._blocked_cell_base_ttl
        else:
            ttl = min(self._blocked_cell_wall_ttl,
                      60.0 + (hits - 1) * 30.0)
        until = time.time() + ttl
        prev_until = float(self._blocked_cells_until.get(cell, 0.0) or 0.0)
        self._blocked_cells_until[cell] = max(prev_until, until)
        if hits <= 2:
            print(f"[NavMem] blocked cell remember: {cell} ttl={ttl:.1f}s reason={reason}")
        self._learn_wall(cell)

    def _remember_last_move_blocked_cell(self, reason: str = "stuck"):
        try:
            direction = str(getattr(self.state, "last_move_dir", "") or "")
            if direction not in self._ALL_DIRS:
                return
            ccx, ccy = self._get_nav_grid_pos()
            nx, ny = _nav_step_from_dir(int(ccx), int(ccy), direction)
            self._remember_blocked_cell(nx, ny, reason=reason)
            if int(self._blocked_cell_hits.get((nx, ny), 0) or 0) >= 2:
                if direction in ("left", "right"):
                    shoulders = ((nx, ny - 1), (nx, ny + 1))
                else:
                    shoulders = ((nx - 1, ny), (nx + 1, ny))
                for sx, sy in shoulders:
                    self._remember_blocked_cell(sx, sy, reason=f"{reason}_cluster")
        except Exception:
            return

    def _check_stuck(self) -> bool:
        """Return True when a move key was sent and coordinates did not change for 1 second."""
        if bool(getattr(self.state, "ntab_active", False)):
            self._nav_attempt_pos = None
            self._nav_attempt_started_at = 0.0
            self.stuck_count = 0
            return False
        if bool(getattr(self.state, "support_targeting_active", False)) or bool(
            getattr(self.state, "red_tab_promotion_active", False)
        ):
            self._nav_attempt_pos = None
            self._nav_attempt_started_at = 0.0
            self.stuck_count = 0
            return False

        # Follow 紐⑤뱶?먯꽌??洹쇱젒 踰붿쐞 吏꾩엯 ??stuck ??대㉧瑜?利됱떆 ?댁젣?쒕떎.
        # (猷⑦봽 ?쒖꽌??check_stuck媛 follow 洹쇱젒 ?먯젙蹂대떎 癒쇱? ?ㅽ뻾?섍린 ?뚮Ц)
        follow_target = self._calc_follow_target() if bool(getattr(self.state, "nav_follow_enabled", False)) else None
        follow_navigation_active = should_allow_follow_navigation(
            getattr(self.state, "nav_follow_enabled", False),
            follow_target is not None,
            getattr(self.state, "is_connected", False),
        )
        if follow_navigation_active and follow_target:
            tx, ty = follow_target
            if should_hold_follow_position(int(tx), int(ty), int(self.state.x), int(self.state.y)):
                self._nav_attempt_pos = None
                self._nav_attempt_started_at = 0.0
                self.stuck_count = 0
                self._support_follow_soft_stuck_count = 0
                self._support_follow_visited_cells = set()
                self._support_follow_escape_attempts = 0
                self._support_follow_tried_escape_dirs = set()
                return False

        now = time.time()
        current_pos = (self.state.x, self.state.y)

        if self._nav_current_pos != current_pos:
            self.last_pos = self._nav_current_pos
            self._nav_current_pos = current_pos
            self._nav_attempt_pos = None
            self._nav_attempt_started_at = 0.0
            self.stuck_count = 0
            # 좌표가 바뀌어도 이번 stuck 에피소드에서 이미 지나간 칸으로
            # 되돌아온 것이면 진전이 아니다 - follow_target(격수)이 움직이면
            # 거리(gap)만으로는 진전 여부를 판단할 수 없어서, 실제로 처음
            # 밟는 칸인지로 판정한다. 안 그러면 막다른 구석에서 격수가
            # 조금만 움직여도 카운트가 지워져 재타겟팅까지 도달 못 한다.
            if follow_navigation_active and follow_target:
                if current_pos in self._support_follow_visited_cells:
                    pass
                else:
                    self._support_follow_visited_cells.add(current_pos)
                    if len(self._support_follow_visited_cells) > 12:
                        self._support_follow_visited_cells = {current_pos}
                    self._support_follow_soft_stuck_count = 0
                    self._support_follow_escape_attempts = 0
                    self._support_follow_tried_escape_dirs = set()
            else:
                self._support_follow_soft_stuck_count = 0
                self._support_follow_visited_cells = set()
                self._support_follow_escape_attempts = 0
                self._support_follow_tried_escape_dirs = set()
            return False

        if self._nav_attempt_pos != current_pos:
            return False

        # 좌표가 아직 이동 전 값이면 '안 움직였다'를 판정할 수 없다.
        if not self._coord_fresh_since(self._nav_attempt_started_at):
            return False

        stuck_time_limit = float(TIMING_CONFIG.get("stuck_time", 4.0))
        if follow_navigation_active and follow_target:
            tx, ty = follow_target
            follow_gap = follow_manhattan_gap(int(tx), int(ty), int(self.state.x), int(self.state.y))
            # 예전값 0.35/0.55/1.00s는 '키 입력 -> 한 칸 이동 -> 화면 캡처 ->
            # 좌표 OCR'이 실제로 완료되는 시간(약 1s 이상)보다 짧아서, 아직
            # 움직이는 중인데 '멈췄다'로 오판했다. 그 오판이 escape twitch를
            # 남발하고(각 twitch가 또 진짜 이동을 방해) 없는 벽을 학습해
            # (WallMem confirmed -> 나중에 walked로 취소) 길을 스스로 막았다.
            # 격수가 멀수록(gap 큼) 더 빨리 우회 판단을 해야 하니 한계는
            # 짧게 두되, 한 스텝이 등록될 시간은 확보한다.
            # ponytail: 상수 튜닝. 근본은 인식 주기(2.6s)라 그게 빨라지면 더 내려도 됨.
            if follow_gap >= 4:
                stuck_time_limit = min(stuck_time_limit, 1.2)
            elif follow_gap >= 2:
                stuck_time_limit = min(stuck_time_limit, 1.6)
            else:
                stuck_time_limit = min(stuck_time_limit, 2.2)
        if self._nav_attempt_started_at > 0.0 and (now - self._nav_attempt_started_at) >= stuck_time_limit:
            if bool(getattr(self, "_portal_follow_active", False)):
                if (now - float(getattr(self, "_last_portal_follow_log_time", 0.0) or 0.0)) >= 0.8:
                    print(f"[PortalFollow] stuck wait: keep exact portal path pos={current_pos}")
                    self._last_portal_follow_log_time = now
                # Remember the exact cell the last attempt walked into
                # instead of wiping blocked-cell memory - this was silently
                # discarding the one signal that could tell "up" apart from
                # "left" here, so the same blocked direction got retried
                # forever. 2D game, no diagonals, so the failed step is
                # always exactly one of the four axis-neighbor cells.
                # _remember_blocked_cell's hit-count TTL already covers
                # both cases this can mean: a monster in the way clears
                # after its short TTL and gets retried; an actual wall
                # keeps getting hit and its TTL escalates instead of
                # thrashing on the same blocked step.
                last_dir = normalize_move_dir(getattr(self.state, "last_move_dir", ""))
                if last_dir:
                    bx, by = _nav_step_from_dir(current_pos[0], current_pos[1], last_dir)
                    self._remember_blocked_cell(bx, by, reason="portal_follow_stuck")
                self._nav_attempt_pos = None
                self._nav_attempt_started_at = 0.0
                self.stuck_count = 0
                self._support_follow_soft_stuck_count = 0
                self._support_follow_visited_cells = set()
                self._support_follow_escape_attempts = 0
                self._support_follow_tried_escape_dirs = set()
                return False

            self.stuck_count += 1
            self._remember_last_move_blocked_cell(reason="stuck_timeout")
            print(f"[Stuck] no coord change for {stuck_time_limit}s ({self.stuck_count} consecutive) pos={current_pos}")
            if should_defer_stuck_escape_for_support(
                f"{getattr(self.state, 'role', '')} {getattr(self.state, 'network_role', '')}",
                bool(getattr(self.state, "service_active", False)) or bool(getattr(self.state, "auto_hunt", False)),
                getattr(self.state, "nav_follow_enabled", False),
            ):
                if not should_retarget_after_support_follow_stuck(
                    int(getattr(self, "_support_follow_soft_stuck_count", 0) or 0),
                    max_soft_stucks_before_retarget=2,
                ):
                    self._support_follow_soft_stuck_count += 1
                    if should_clear_target_box_after_support_stuck(
                        True,
                        bool(getattr(self.state, "red_tab_enabled", False)),
                        False,
                    ):
                        self._trigger_box_collision_recover(force_retarget=True)
                    else:
                        self._quick_support_follow_escape(follow_target)
                    self._nav_attempt_pos = None
                    self._nav_attempt_started_at = 0.0
                    return False
                self._support_follow_soft_stuck_count = 0
                self._support_follow_visited_cells = set()
                self._support_follow_tried_escape_dirs = set()
                self._trigger_box_collision_recover(force_retarget=True)
                self._nav_attempt_pos = None
                self._nav_attempt_started_at = 0.0
                self.stuck_count = 0
                return False
            self._trigger_box_collision_recover()
            return True
        return False

    def _trigger_box_collision_recover(self, force_retarget: bool = False):
        now = time.time()
        if (not force_retarget) and (now - float(self._last_box_collision_recover_request_time or 0.0)) < 1.2:
            return
        self._last_box_collision_recover_request_time = now
        if should_defer_stuck_escape_for_support(
            f"{getattr(self.state, 'role', '')} {getattr(self.state, 'network_role', '')}",
            bool(getattr(self.state, "service_active", False)) or bool(getattr(self.state, "auto_hunt", False)),
            getattr(self.state, "nav_follow_enabled", False),
        ):
            if bool(getattr(self.state, "support_targeting_active", False)) or bool(
                getattr(self.state, "red_tab_promotion_active", False)
            ):
                print("[Recover] support target preparation active: defer target-box clear.")
                return
            if should_clear_target_box_after_support_stuck(
                True,
                bool(getattr(self.state, "red_tab_enabled", False)),
                False,
            ):
                try:
                    hw.stop_all_inputs(repeat=1, delay=0.005)
                    self.state.support_input_blocked_until = max(
                        float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0),
                        time.time() + 0.75,
                    )
                    self.state.support_targeting_active = True
                    self.state.box_collision_reprepare_requested = True
                    print("[Recover] support follow stalled: request ESC -> TAB -> TAB with movement locked.")
                except Exception as error:
                    self.state.support_targeting_active = False
                    print(f"[Recover] support red_tab reset request failed: {error}")
                return
            self._quick_support_follow_escape(self._calc_follow_target())
            print("[Recover] support follow stalled with confirmed red_tab: use detour, keep target.")
            return
        # 도사/따라가기 상황이 아니면(예: 격수 본인, 또는 follow 시작 전) red_tab
        # 타겟박스 자체가 없으므로 "박스 충돌"로 볼 근거가 없다 - ESC+재타겟
        # 요청은 여기선 아무 의미가 없고, 실제 벽/장애물이었을 때 오히려 도움이
        # 안 된다. 다른 stuck 상황에서 이미 쓰는 일반 회피 이동으로 처리한다.
        self._escape_stuck(rewind_waypoint=False)

    def _mark_nav_attempt(self):
        current_pos = (self.state.x, self.state.y)
        if self._nav_attempt_pos != current_pos:
            self._nav_attempt_pos = current_pos
            self._nav_attempt_started_at = time.time()

    # ----------------------------------------------------------
    def _escape_stuck(self, rewind_waypoint: bool = False):
        """
        Intelligent escape movement.
        1) step back toward the previous position when available
        2) random side-step
        3) repeat a few times
        4) reset target state
        """
        print("[Warn] [STUCK] blocked movement detected, escaping...")
        self.state.is_stuck = True

        for _ in range(random.randint(2, 3)):
            if isinstance(self.last_pos, tuple) and len(self.last_pos) == 2:
                cx, cy = self.state.x, self.state.y
                lx, ly = self.last_pos
                dx, dy = lx - cx, ly - cy
                if abs(dx) > abs(dy):
                    move_dir = "right" if dx > 0 else "left"
                else:
                    move_dir = "down" if dy > 0 else "up"
                hw.hold_move(move_dir, "stuck_back_hold")
                humanized_sleep(TIMING_CONFIG["key_gap"])

            ccx, ccy = self._get_nav_grid_pos()
            blocked_cells = self._get_blocked_cells()
            escape_dirs = []
            for d in ["left", "right", "up", "down"]:
                nx, ny = _nav_step_from_dir(int(ccx), int(ccy), d)
                if (nx, ny) not in blocked_cells:
                    escape_dirs.append(d)
            ranked_dirs = escape_dirs or ["left", "right", "up", "down"]
            if isinstance(self._current_nav_context, dict) and self._current_nav_context.get("kind") == "follow":
                follow_target = self._calc_follow_target()
                if follow_target:
                    tx, ty = follow_target
                    ranked_dirs = sorted(
                        ranked_dirs,
                        key=lambda d: follow_manhattan_gap(
                            int(tx),
                            int(ty),
                            *_nav_step_from_dir(int(ccx), int(ccy), d),
                        ),
                    )
            side_dir = ranked_dirs[0] if ranked_dirs else random.choice(["left", "right", "up", "down"])
            hw.hold_move(side_dir, "stuck_side_hold")
            humanized_sleep(TIMING_CONFIG["key_gap"])

        self.state.target_locked = False
        self.state.target_name = ""
        if rewind_waypoint and self.wp_idx > 0:
            self.wp_idx -= 1
        self.state.is_stuck = False
        self.stuck_count = 0
        self.stuck_timer = 0.0
        self._nav_attempt_pos = None
        self._nav_attempt_started_at = 0.0
        print("[OK] [STUCK] escape complete, continue navigation.")

    def _set_nav_context(self, kind: str, **kwargs):
        self._current_nav_context = {"kind": kind, **kwargs}

    def _clear_nav_context(self):
        self._current_nav_context = None

    def _mark_combat_pause(self):
        ctx = self._current_nav_context
        if not isinstance(ctx, dict):
            return
        if ctx.get("kind") not in ("seq_point", "list_wp"):
            return
        self._combat_resume_context = dict(ctx)
        self._advance_waypoint_after_combat = True

    def _advance_waypoint_after_interrupt(self):
        if not self._advance_waypoint_after_combat:
            return
        ctx = self._combat_resume_context
        if not isinstance(ctx, dict):
            self._advance_waypoint_after_combat = False
            return

        kind = ctx.get("kind")
        if kind == "seq_point":
            idx = int(ctx.get("seq_point_idx", -1))
            advance_delta = int(ctx.get("advance_delta", 1))
            if self.seq_phase == "points" and self.seq_point_idx == idx:
                self.seq_point_idx += advance_delta
                print(f"[Nav] ?꾪닾 醫낅즺 ???ㅼ쓬 ?щ깷?먯쑝濡?嫄대꼫?: index {self.seq_point_idx}")
        elif kind == "list_wp":
            idx = int(ctx.get("wp_idx", -1))
            advance_delta = int(ctx.get("advance_delta", 1))
            if self.wp_idx == idx:
                self.wp_idx += advance_delta
                print(f"[Nav] ?꾪닾 醫낅즺 ???ㅼ쓬 ?⑥씠?ъ씤?몃줈 嫄대꼫?: index {self.wp_idx}")

        self._advance_waypoint_after_combat = False
        self._combat_resume_context = None

    # ----------------------------------------------------------
    def _is_in_combat(self) -> bool:
        """?꾪닾 以??곹깭 ?먮퀎: target_locked == True ?먮뒗 combat_start_time?쇰줈遺??5珥??대궡."""
        if self.state.target_locked:
            return True
        if self.state.combat_start_time > 0:
            elapsed = time.time() - self.state.combat_start_time
            if elapsed < 5.0:
                return True
        return False

    # ----------------------------------------------------------
    def _is_wall(self, gx: int, gy: int) -> bool:
        """?꾩옱 留듭쓽 maps_db瑜?議고쉶?섏뿬 ?대떦 Grid媛 踰쎌씤吏 ?먮퀎."""
        m = self.state.current_map
        map_data = self.state.maps_db.get(m)
        if not map_data:
            return False
        walls = map_data.get("walls", [])
        return [gx, gy] in walls

    def _get_remote_warrior_snapshot(self) -> dict | None:
        remote = self.state.get_fresh_remote_data_by_role("격수")
        return remote if isinstance(remote, dict) and remote else None

    def _clear_target_for_portal_follow(self) -> bool:
        """굴이동 감지 직후 대상선택box/red_tab을 하드웨어 강제 ESC 2회로 해제한다."""
        try:
            hw.send_force("RELEASE_ALL")
        except Exception:
            for direction in self._ALL_DIRS:
                try:
                    hw.release_key(direction)
                except Exception:
                    pass

        current_block = float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0)
        self.state.support_input_blocked_until = max(current_block, time.time() + 0.80)

        try:
            for _ in range(2):
                hw.send_force("D:esc")
                humanized_sleep(0.008, variance=0.02)
                hw.send_force("U:esc")
                humanized_sleep(0.014, variance=0.03)
            print("[PortalFollow] clear target before portal follow: force D/U esc x2")
            return True
        except Exception as e:
            print(f"[PortalFollow] force esc x2 failed: {e}")
            try:
                for _ in range(2):
                    hw.fast_press("esc", variance=0.04)
                    humanized_sleep(0.055, variance=0.06)
                print("[PortalFollow] clear target before portal follow: fallback fast esc x2")
                return True
            except Exception as fallback_error:
                print(f"[PortalFollow] fallback esc x2 failed: {fallback_error}")
                return False

    def _arm_portal_follow(
        self,
        warrior_last: tuple[int, int],
        enter_dir: str | None,
        *,
        started_at: float | None = None,
        clear_target: bool = False,
        source_map_sig: tuple[str, str, str, str] | None = None,
        log_prefix: str = "armed",
    ) -> None:
        warrior_xy = (int(warrior_last[0]), int(warrior_last[1]))
        dir_norm = normalize_move_dir(enter_dir)
        started = float(started_at if started_at is not None else time.time())
        self._portal_follow_active = True
        # Nav target is the warrior's own last coordinate itself (the
        # portal tile), not a tile past it - walk exactly there, then
        # _complete_portal_follow_if_arrived taps enter_dir once from that
        # standstill. (Previously this targeted one tile past the door so
        # ordinary walking would carry the dosa through it, since an
        # earlier standstill-tap attempt never triggered doors live - but
        # that attempt used force_press(), a single instant packet;
        # _complete_portal_follow_if_arrived was already fixed to use a
        # real held hold_move() instead, matching how ordinary walking
        # crosses doors, but the caller that would have exercised the
        # fixed version was never reconnected.)
        self._portal_follow_approach = warrior_xy
        self._portal_follow_coord = warrior_xy
        self._portal_follow_dir = dir_norm
        self._portal_follow_source_map_sig = tuple(source_map_sig) if source_map_sig else None
        self._portal_follow_started_at = started
        self._portal_enter_attempted = False
        self._portal_enter_nudge_used = False
        self._last_self_portal_coord = (int(self.state.x), int(self.state.y))
        self._blocked_cells_until.clear()
        self._blocked_cell_hits.clear()
        # Portal follow must be able to press movement keys.
        try:
            self.state.support_input_blocked_until = 0.0
            self.state.is_combat_busy = False
        except Exception:
            pass
        self.state.portal_follow_active = True
        self.state.portal_follow_retarget_requested = True
        self.state.portal_follow_started_at = started
        self.state.portal_follow_finished_at = 0.0
        self.state.portal_follow_coord = warrior_xy
        self.state.portal_follow_approach = warrior_xy
        self.state.portal_follow_dir = dir_norm
        try:
            self.state.red_tab_enabled = False
        except Exception:
            pass
        if clear_target:
            self._clear_target_for_portal_follow()
        portal_arm_line = (
            f"[PortalFollow] {log_prefix}: warrior_last={warrior_xy}, "
            f"enter_dir={dir_norm or '-'}"
        )
        print(portal_arm_line)
        discord_notify(portal_arm_line)

    def _remember_warrior_coord(self, cur_x: int, cur_y: int, map_sig, dps_dir, remote_data: dict) -> None:
        """Record where we last saw the warrior. Every exit point of
        _update_portal_follow_state must call this - skipping it leaves
        _last_warrior_coord frozen, which makes the next call's distance
        checks compare against a stale point instead of the real last-seen
        position (this is what caused the coord-jump re-arm loop)."""
        self._last_warrior_coord = (cur_x, cur_y)
        self._last_warrior_coord_ts = float(remote_data.get("_received_at") or 0.0) or time.time()
        self._last_warrior_map_sig = map_sig
        if dps_dir:
            self._last_warrior_dir = dps_dir
        cur_fp = str(remote_data.get("map_info_fingerprint", "") or "")
        if cur_fp:
            self._last_warrior_fingerprint = cur_fp

    def _update_portal_follow_state(self, remote_data: dict | None) -> None:
        if not isinstance(remote_data, dict):
            return
        try:
            cur_x = int(remote_data.get("x", remote_data.get("pos_x", 0)) or 0)
            cur_y = int(remote_data.get("y", remote_data.get("pos_y", 0)) or 0)
        except Exception:
            return

        prev = self._last_warrior_coord
        prev_dir = normalize_move_dir(self._last_warrior_dir)
        prev_step = normalize_move_dir(self._last_warrior_step_dir)
        dps_dir = normalize_move_dir(remote_data.get("last_move_dir", ""))
        map_sig = (
            str(remote_data.get("map_name", "") or ""),
            str(remote_data.get("map_floor", "") or ""),
            str(remote_data.get("current_map", "") or ""),
            str(remote_data.get("current_floor", "") or ""),
        )
        map_info_change_seq = int(remote_data.get("map_info_change_seq", 0) or 0)
        map_info_changed = map_info_change_seq > int(getattr(self, "_last_map_info_change_seq", 0) or 0)
        if map_info_changed:
            self._last_map_info_change_seq = map_info_change_seq
        prev_map_sig = self._last_warrior_map_sig
        cur_ok = is_plausible_map_coord(cur_x, cur_y)
        event_prev = None
        event_dir = None
        event_seq = 0
        event_age = None
        event_from_confidence = "low"
        event_data = remote_data.get("coord_transition")
        if isinstance(event_data, dict):
            try:
                event_seq = int(event_data.get("seq", 0) or remote_data.get("coord_transition_seq", 0) or 0)
                event_ts = float(event_data.get("ts", 0.0) or 0.0)
                if event_ts > 0.0:
                    event_age = max(0.0, time.time() - event_ts)
                event_from = event_data.get("from") or []
                event_x = int(event_from[0])
                event_y = int(event_from[1])
                if (
                    event_seq > int(self._last_coord_transition_seq or 0)
                    and is_plausible_transition_coord(event_x, event_y)
                    and (event_age is None or event_age <= 0.75)
                ):
                    self._last_coord_transition_seq = event_seq
                    # 격수 PC의 x/y OCR은 매 사이클 갱신되지만 map_info OCR은
                    # 별도 스레드/캡처라 완전히 같은 순간을 보장 못한다 - 실제로
                    # 포탈을 넘는 그 사이클에 x/y는 이미 새 맵 좌표인데 map_sig는
                    # 아직 옛 맵으로 찍히는 경우가 있었다. 그러면 "from"이 사실
                    # 새 맵 좌표라 도사가 옛 맵에서 걸어갈 수 없는 곳을 목표로
                    # 삼게 된다(실측: 몇 칸 이내에서 붙어 추적 중이다가 갑자기
                    # 30칸+ 떨어진 좌표로 점프). from_map_sig(그 좌표를 찍은
                    # 순간의 격수 맵)가 도사가 알고 있는 격수의 직전 맵과 다르면
                    # - 이미 새 맵 좌표라는 뜻이니 위치 정보로 신뢰하지 않는다.
                    event_from_map_sig = event_data.get("from_map_sig")
                    from_map_mismatch = bool(
                        event_from_map_sig
                        and prev_map_sig
                        and any(prev_map_sig[:3])
                        and tuple(str(v) for v in event_from_map_sig) != tuple(str(v) for v in prev_map_sig[:3])
                    )
                    if from_map_mismatch:
                        print(
                            f"[PortalFollow] ignoring event_seq={event_seq}: from={[event_x, event_y]} was "
                            f"captured on map={event_from_map_sig}, not the warrior's last known map="
                            f"{prev_map_sig[:3]} - likely already the post-crossing coordinate"
                        )
                    else:
                        event_prev = (event_x, event_y)
                        event_dir = normalize_move_dir(event_data.get("dir") or event_data.get("input_dir"))
                        event_from_confidence = str(event_data.get("from_confidence") or "low")
            except Exception:
                event_prev = None
                event_dir = None
                event_age = None

        if self._portal_follow_active:
            if event_prev is not None:
                # event_seq increments on every ordinary footstep the warrior
                # takes, not just real door crossings - confirmed live: a
                # plain 1-tile walk (23,1)->(23,0) fired a coord_transition
                # event while already portal-following, which this branch
                # used to trust unconditionally and re-arm to. The very next
                # ordinary step then re-armed again to one tile further
                # (23,-1) - extending the nav target past the door in the
                # same direction every single step instead of only on an
                # actual crossing, walking the dosa backward through the
                # door it was already lined up to use correctly. Require the
                # same portal-sized jump distance the no-event branch below
                # already demands before trusting this as a real second
                # crossing.
                event_jump_distance = (
                    follow_manhattan_gap(event_prev[0], event_prev[1], cur_x, cur_y) if cur_ok else None
                )
                is_real_crossing = event_jump_distance is None or should_detect_warrior_transition(
                    event_prev[0], event_prev[1], cur_x, cur_y, jump_distance=WARRIOR_TRANSITION_JUMP_DISTANCE
                )
                if not is_real_crossing:
                    print(
                        f"[PortalFollow] ignoring event_seq={event_seq} while still following "
                        f"previous one - looks like an ordinary step (jump={event_jump_distance}), "
                        "not a new crossing"
                    )
                else:
                    # Warrior sent another explicit transition while we were still en
                    # route to the last one (e.g. two portals back-to-back). Confirmed
                    # live: staying locked onto the stale target left the dosa walking
                    # toward a portal the warrior was no longer anywhere near. Re-arm
                    # to the new one instead of silently tracking a target we've given
                    # up reaching.
                    print(
                        f"[PortalFollow] new transition while still following previous one "
                        f"(event_seq={event_seq}): re-arming to warrior_last={event_prev}"
                    )
                    self._finish_portal_follow(f"superseded_by_event_seq_{event_seq}")
                    enter_dir = event_dir or normalize_move_dir(remote_data.get("last_move_dir", ""))
                    if enter_dir:
                        source_map_sig = prev_map_sig if prev_map_sig and any(prev_map_sig) else map_sig
                        self._arm_portal_follow(
                            event_prev,
                            enter_dir,
                            started_at=time.time(),
                            clear_target=True,
                            source_map_sig=source_map_sig,
                            log_prefix=(
                                f"warrior transition detected now=({cur_x}, {cur_y}) "
                                f"(superseding stale portal-follow) event_seq={event_seq or '-'}"
                            ),
                        )
                        # _last_warrior_coord must move up to (cur_x, cur_y) here too -
                        # leaving it stale (as before) made every later call keep
                        # comparing against this same pre-transition point, so an
                        # unrelated coordinate-jump check further down kept firing
                        # on distance that was really just "how far we drifted since
                        # we stopped updating this", not a new jump.
                        self._remember_warrior_coord(cur_x, cur_y, map_sig, dps_dir, remote_data)
                        return
            if (
                cur_ok
                and prev
                and is_plausible_map_coord(prev[0], prev[1])
                and should_detect_warrior_transition(prev[0], prev[1], cur_x, cur_y, jump_distance=WARRIOR_TRANSITION_JUMP_DISTANCE)
            ):
                # No explicit event this time, but the raw coordinate jumped a lot
                # from where we last tracked the warrior while already
                # portal-following - same "moved on without us" case as above,
                # just without a coord_transition packet to key off. Gate on
                # implied speed like the normal detector so an ordinary telemetry
                # gap doesn't churn a false re-arm.
                jump_distance = follow_manhattan_gap(prev[0], prev[1], cur_x, cur_y)
                cur_received_at = float(remote_data.get("_received_at") or 0.0)
                prev_ts = float(getattr(self, "_last_warrior_coord_ts", 0.0) or 0.0)
                elapsed = cur_received_at - prev_ts if (cur_received_at > 0.0 and prev_ts > 0.0) else 0.0
                if is_implausible_walk_speed(jump_distance, elapsed):
                    enter_dir = dps_dir or normalize_move_dir(remote_data.get("last_move_dir", ""))
                    print(
                        f"[PortalFollow] warrior coordinate jumped {jump_distance} tiles "
                        f"while still following a previous transition: re-arming to ({cur_x}, {cur_y})"
                    )
                    self._finish_portal_follow(f"superseded_by_coord_jump_{jump_distance}")
                    if enter_dir:
                        source_map_sig = prev_map_sig if prev_map_sig and any(prev_map_sig) else map_sig
                        self._arm_portal_follow(
                            (cur_x, cur_y),
                            enter_dir,
                            started_at=time.time(),
                            clear_target=True,
                            source_map_sig=source_map_sig,
                            log_prefix="warrior coord jump superseding stale portal-follow",
                        )
                        # Same fix as the event_seq branch above: without this,
                        # _last_warrior_coord stayed frozen at the pre-jump spot,
                        # so the very next signal recomputed the same huge
                        # "jump_distance" from that same stale point and re-armed
                        # to the same (cur_x, cur_y) again - live logs showed this
                        # looping several times in a row on an unchanged target.
                        self._remember_warrior_coord(cur_x, cur_y, map_sig, dps_dir, remote_data)
                        return
            if cur_ok:
                if prev and (prev[0], prev[1]) != (cur_x, cur_y):
                    if follow_manhattan_gap(prev[0], prev[1], cur_x, cur_y) == 1:
                        step = dir_between_coords(prev[0], prev[1], cur_x, cur_y)
                        if step:
                            self._last_warrior_step_dir = step
                self._remember_warrior_coord(cur_x, cur_y, map_sig, dps_dir, remote_data)
            return

        # Same fallback as LogicSvc's _handle_warrior_transition_immediate_clear
        # (kept in sync manually - both copies must move together):
        # map text goes stale when OCR fails, so also trust the fingerprint,
        # which is recomputed every cycle regardless of OCR success.
        cur_fingerprint = str(remote_data.get("map_info_fingerprint", "") or "")
        prev_fingerprint = self._last_warrior_fingerprint
        fingerprint_changed = bool(
            prev_fingerprint
            and cur_fingerprint
            and fingerprint_hamming_distance(prev_fingerprint, cur_fingerprint) > MAP_FINGERPRINT_SAME_MAX_DISTANCE
        )
        if cur_fingerprint:
            self._last_warrior_fingerprint = cur_fingerprint

        map_changed = bool(
            (prev and prev_map_sig and map_sig != prev_map_sig and any(map_sig))
            or fingerprint_changed
        )
        cur_ok = (
            is_plausible_map_coord(cur_x, cur_y)
            or (event_prev is not None and is_plausible_transition_coord(cur_x, cur_y))
            or (map_changed and is_plausible_transition_coord(cur_x, cur_y))
        )
        coord_jumped = bool(
            prev
            and is_plausible_map_coord(prev[0], prev[1])
            and cur_ok
            and should_detect_warrior_transition(prev[0], prev[1], cur_x, cur_y, jump_distance=WARRIOR_TRANSITION_JUMP_DISTANCE)
        )
        if coord_jumped and not map_changed and event_prev is None:
            # map_info_changed is a raw pixel-diff of the map-name crop (see
            # svc_monitor.py's map_signature) - it flips on any visual noise
            # in that region (text flicker, an overlay passing over it), not
            # just real map changes. map_changed (recognized map name/
            # fingerprint) is the trustworthy version of "the map changed";
            # map_info_changed alone must not skip the speed check below, or
            # ordinary fast walking + a stray pixel flicker gets misread as
            # a portal crossing.
            cur_received_at = float(remote_data.get("_received_at") or 0.0)
            prev_ts = float(getattr(self, "_last_warrior_coord_ts", 0.0) or 0.0)
            elapsed = cur_received_at - prev_ts if (cur_received_at > 0.0 and prev_ts > 0.0) else 0.0
            jump_distance = follow_manhattan_gap(prev[0], prev[1], cur_x, cur_y)
            coord_jumped = is_implausible_walk_speed(jump_distance, elapsed)
            cur_received_at = float(remote_data.get("_received_at") or 0.0)
            prev_ts = float(getattr(self, "_last_warrior_coord_ts", 0.0) or 0.0)
            elapsed = cur_received_at - prev_ts if (cur_received_at > 0.0 and prev_ts > 0.0) else 0.0
            jump_distance = follow_manhattan_gap(prev[0], prev[1], cur_x, cur_y)
            coord_jumped = is_implausible_walk_speed(jump_distance, elapsed)

        if prev and cur_ok and (prev[0], prev[1]) != (cur_x, cur_y):
            if follow_manhattan_gap(prev[0], prev[1], cur_x, cur_y) == 1:
                step = dir_between_coords(prev[0], prev[1], cur_x, cur_y)
                if step:
                    self._last_warrior_step_dir = step
                    prev_step = step
            self._warrior_trail.append((prev[0], prev[1], prev_step or prev_dir))
            if len(self._warrior_trail) > 12:
                self._warrior_trail = self._warrior_trail[-12:]

        transition_prev = event_prev or prev
        # Right after finishing a real crossing, the warrior PC's own
        # map-name OCR can still be settling for a moment - a bare
        # map_changed/map_info_changed reading (no coord jump, no explicit
        # transition event) in that window is more likely leftover noise
        # than a second door. An explicit event or an actual distance jump
        # is still trusted immediately regardless of this cooldown.
        # The fixed 1.0s window alone isn't enough: red_tab retarget after a
        # crossing can silently retry up to 4 times (each a full esc->tab->tab
        # cycle) and take longer than that, so the timestamp can expire
        # mid-retry and let a stale map-only signal back in right as the busy
        # retarget work finally finishes. portal_follow_retarget_requested
        # stays True for that whole retry span, so hold the cooldown open
        # for as long as that's set, not just the fixed window.
        in_portal_cooldown = time.time() < float(
            getattr(self, "_portal_follow_cooldown_until", 0.0) or 0.0
        ) or bool(getattr(self.state, "portal_follow_retarget_requested", False))
        map_only_signal = bool((map_changed or map_info_changed) and not coord_jumped and event_prev is None)
        if map_only_signal and in_portal_cooldown and prev and is_plausible_map_coord(prev[0], prev[1]):
            print(
                f"[PortalFollow] suppressing map-only transition signal during post-crossing "
                f"cooldown: map_changed={map_changed} map_info_changed={map_info_changed} "
                f"now=({cur_x}, {cur_y})"
            )
        if event_prev or (
            prev
            and is_plausible_map_coord(prev[0], prev[1])
            # map_info_changed dropped from this OR: it's raw crop-pixel
            # noise (see comment above) and shouldn't be able to arm a
            # portal-follow by itself with zero coordinate or map-name
            # evidence backing it up.
            and (coord_jumped or map_changed)
            and not (map_only_signal and in_portal_cooldown)
        ):
            source_map_sig = prev_map_sig if prev_map_sig and any(prev_map_sig) else map_sig
            cached_hint = {}
            if isinstance(getattr(self.state, "portal_session_cache", None), dict) and transition_prev:
                cache_key = portal_cache_key(source_map_sig, transition_prev[0], transition_prev[1])
                cached_hint = dict(self.state.portal_session_cache.get(cache_key, {}) or {})
            cached_dir = normalize_move_dir(cached_hint.get("enter_dir"))
            if event_prev is not None and not cached_dir and event_from_confidence == "low":
                # A jump this big is still trusted as a real portal, but
                # without a fresh key-press timestamp corroborating it, the
                # captured "from" tile itself might already be a step or two
                # past the actual door (a capture hiccup right at the
                # crossing instant). Standing at the wrong tile and tapping
                # a direction does nothing useful - just keep following the
                # warrior's live position instead of arming a door-tap.
                if cur_ok:
                    self._last_warrior_coord = (cur_x, cur_y)
                    self._last_warrior_coord_ts = float(remote_data.get("_received_at") or 0.0) or time.time()
                    self._last_warrior_map_sig = map_sig
                print(
                    f"[PortalFollow] transition from={transition_prev} low confidence "
                    f"(no fresh key press near capture); following warrior directly instead of arming door tap"
                )
                return False
            # A direction confirmed at this exact portal beats a guess from
            # noisy/stale telemetry (warrior's self-reported last_move_dir can
            # lag the actual key held at the moment of transition).
            enter_dir = cached_dir or event_dir or prev_step or self._last_warrior_step_dir
            if not enter_dir:
                if cur_ok:
                    self._last_warrior_coord = (cur_x, cur_y)
                    self._last_warrior_coord_ts = float(remote_data.get("_received_at") or 0.0) or time.time()
                    self._last_warrior_map_sig = map_sig
                print(
                    f"[PortalFollow] transition waiting for stable step dir: "
                    f"now=({cur_x}, {cur_y}) event_age={event_age if event_age is not None else '-'} "
                    f"map={prev_map_sig}->{map_sig}"
                )
                return False
            self._arm_portal_follow(
                transition_prev,
                enter_dir,
                started_at=time.time(),
                clear_target=True,
                source_map_sig=source_map_sig,
                log_prefix=(
                    f"warrior transition detected now=({cur_x}, {cur_y}) "
                    f"coord_jump={coord_jumped} map_changed={map_changed} "
                    f"map_info_changed={map_info_changed} event_prev={'yes' if event_prev else 'no'} "
                    f"event_seq={event_seq or '-'} step_dir={prev_step or '-'}"
                ),
            )

        if cur_ok:
            self._last_warrior_coord = (cur_x, cur_y)
            self._last_warrior_coord_ts = float(remote_data.get("_received_at") or 0.0) or time.time()
            self._last_warrior_map_sig = map_sig
            if dps_dir:
                self._last_warrior_dir = dps_dir
        elif map_changed:
            self._last_warrior_map_sig = map_sig

    def _finish_portal_follow(self, reason: str) -> None:
        # The warrior PC's own map-name OCR can take a beat to settle right
        # after a real crossing - live log showed a second transition arm
        # immediately after a successful one purely on map_changed=True
        # (no event_prev, no coord_jump), chasing a door that was never
        # actually there and never resolving. Give map-name-only signals a
        # short grace window to stop spurious back-to-back arms; an explicit
        # coord_transition event is still trusted immediately regardless.
        self._portal_follow_cooldown_until = time.time() + 1.0
        self._portal_follow_active = False
        self._portal_follow_coord = None
        self._portal_follow_approach = None
        self._portal_follow_dir = None
        self._portal_follow_source_map_sig = None
        self._last_self_portal_coord = None
        self._portal_enter_attempted = False
        self.state.portal_follow_active = False
        self.state.portal_follow_finished_at = time.time()
        self.state.portal_follow_coord = None
        self.state.portal_follow_approach = None
        self.state.portal_follow_dir = None
        self._nav_attempt_pos = None
        self._nav_attempt_started_at = 0.0
        self.stuck_count = 0
        self._support_follow_soft_stuck_count = 0
        self._support_follow_visited_cells = set()
        self._support_follow_escape_attempts = 0
        self._support_follow_tried_escape_dirs = set()
        self.state.last_move_dir = ""
        finish_line = f"[PortalFollow] finished: {reason}"
        print(finish_line)
        discord_notify(finish_line)

    def _complete_portal_follow_on_self_transition(self) -> bool:
        if not self._portal_follow_active:
            return False
        current = (int(getattr(self.state, "x", 0) or 0), int(getattr(self.state, "y", 0) or 0))
        prev = self._last_self_portal_coord
        self._last_self_portal_coord = current
        if not prev:
            return False
        if should_detect_warrior_transition(prev[0], prev[1], current[0], current[1], jump_distance=WARRIOR_TRANSITION_JUMP_DISTANCE):
            self._finish_portal_follow(f"self_transition prev={prev}, now={current}")
            return True
        return False

    def _get_portal_follow_target(self) -> tuple[int, int] | None:
        if (
            not self._portal_follow_active
            and bool(getattr(self.state, "portal_follow_active", False))
            and getattr(self.state, "portal_follow_coord", None)
        ):
            try:
                requested = getattr(self.state, "portal_follow_coord", None)
                requested_approach = getattr(self.state, "portal_follow_approach", None)
                enter_dir = normalize_move_dir(getattr(self.state, "portal_follow_dir", None))
                if requested_approach:
                    warrior_last = (int(requested_approach[0]), int(requested_approach[1]))
                else:
                    warrior_last = (int(requested[0]), int(requested[1]))
                self._arm_portal_follow(
                    warrior_last,
                    enter_dir,
                    started_at=float(getattr(self.state, "portal_follow_started_at", 0.0) or time.time()),
                    clear_target=False,
                    log_prefix="adopted immediate service request",
                )
            except Exception:
                self._portal_follow_active = False
                self._portal_follow_coord = None
                self._portal_follow_approach = None
        if not self._portal_follow_active or not self._portal_follow_coord:
            return None
        if self._complete_portal_follow_on_self_transition():
            return None
        if time.time() - float(self._portal_follow_started_at or 0.0) > self._portal_follow_timeout:
            print("[PortalFollow] timeout. Resume normal follow.")
            self._finish_portal_follow("timeout")
            return None
        return self._portal_follow_coord

    def _complete_portal_follow_if_arrived(self) -> bool:
        """Walk to the warrior's own pre-jump tile, then tap the single
        direction key they used - not a different tile per guessed
        direction. Taps once and polls for the map change instead of a
        fixed sleep-then-check, and does NOT re-tap the same key at the
        same tile on a no-change read: on doors where the same key also
        triggers the return trip on the other side, a blind second tap
        there can undo a transition that actually succeeded but was
        detected late - live logs showed exactly this (tap 1 and tap 2
        both read "no change" even though the dosa was standing right on
        the door). If the tap produced zero position change at all
        (capture/network lag likely left warrior_last one tile short of
        the real door), it instead walks one tile further and retries the
        tap from there - once."""
        target = self._get_portal_follow_target()
        if not target:
            return False
        current_pos = (int(self.state.x), int(self.state.y))
        warrior_last = self._portal_follow_approach or target
        enter_dir = normalize_move_dir(self._portal_follow_dir)
        if not enter_dir:
            return False
        # Arrival must be an exact tile match, not "close enough" - the
        # approach move below is a short single-tile tap (base move_hold,
        # never the gap-scaled longer hold - see the portal_follow
        # exemption further down in _move_toward), so landing exactly on
        # warrior_last is what that short tap is for.
        if current_pos != warrior_last:
            return False
        if getattr(self, "_portal_enter_attempted", False):
            # Already tapped this key twice for this arm - don't keep
            # re-tapping every cycle. That just walks the dosa off the tile
            # and back (looks like aimless left-right jitter) and keeps
            # heal/red_tab blocked the whole time for no new information.
            # Wait for a fresh transition (re-arms with new data) or the
            # overall timeout instead.
            return True
        self._portal_enter_attempted = True

        print(
            f"[PortalFollow] enter zone: current={current_pos}, warrior_last={warrior_last}, "
            f"enter_dir={enter_dir}"
        )
        try:
            self.state.support_input_blocked_until = 0.0
        except Exception:
            pass
        # Own map identity right before tapping the entry key. A raw
        # coordinate jump is NOT a reliable "did we warp" signal here -
        # compare actual map identity instead (same fingerprint tolerance
        # classify_map_sync already uses elsewhere).
        before_own_map_sig = (
            str(getattr(self.state, "map_name", "") or ""),
            str(getattr(self.state, "map_floor", "") or ""),
            str(getattr(self.state, "current_map", "") or ""),
        )
        before_own_fp = str(getattr(self.state, "map_info_fingerprint", "") or "")
        # Baseline for the remote_confirmed cross-check below: if local and
        # remote map text/fingerprint already read "same" before we even tap
        # (e.g. both stuck on a degenerate short OCR read like "도삭산2" that
        # never changes - confirmed live across multiple real crossings this
        # session), then "same" after proves nothing - it was already true.
        # Only trust it as confirmation of a genuine crossing if it transitions
        # from not-same into same.
        before_local_map = {
            "map_info_text": getattr(self.state, "map_info_text", "") or before_own_map_sig[2],
            "current_map": before_own_map_sig[2],
            "map_info_fingerprint": before_own_fp,
        }
        before_remote_snapshot = self._get_remote_warrior_snapshot()
        before_map_sync = (
            classify_map_sync(before_local_map, before_remote_snapshot, now=time.time())
            if before_remote_snapshot else "pending"
        )

        # force_press() sends a single instant "K,<key>" hardware packet.
        # Live logs show this door (and every other one this session) never
        # once triggers from it - not even a delayed transition, and one
        # attempt reported the dosa's own position actually moving BACKWARD
        # after the tap. But normal following crosses the exact same door
        # fine via hold_move() (down, held, up) - confirmed live via
        # "self_transition" completions. Use the same real held keypress
        # here instead of a synthetic tap, since that's what this game
        # actually recognizes as a step.
        # force=True: skip hold_move's own support_targeting_active /
        # red_tab_promotion_active / is_combat_busy busy-check - without it,
        # hold_move can silently send no keypress at all (returns False) if
        # a heal/targeting cycle is running at this exact instant, and the
        # code below has no way to tell that apart from "door didn't
        # respond" (confirmed live: enter attempt landed mid heal-cast,
        # moved=False, gave up on a door that was never actually tapped).
        hw.hold_move(enter_dir, "move_hold", duration=TIMING_CONFIG.get("move_hold", 0.09), force=True)
        self.state.last_move_dir = enter_dir

        entered = False
        map_sig_changed = False
        fp_changed = False
        remote_confirmed = False
        deadline = time.time() + 0.70
        while time.time() < deadline:
            after_own_map_sig = (
                str(getattr(self.state, "map_name", "") or ""),
                str(getattr(self.state, "map_floor", "") or ""),
                str(getattr(self.state, "current_map", "") or ""),
            )
            after_own_fp = str(getattr(self.state, "map_info_fingerprint", "") or "")
            map_sig_changed = bool(after_own_map_sig != before_own_map_sig and any(after_own_map_sig))
            fp_changed = bool(
                before_own_fp
                and after_own_fp
                and fingerprint_hamming_distance(before_own_fp, after_own_fp) > MAP_FINGERPRINT_SAME_MAX_DISTANCE
            )
            if not (map_sig_changed or fp_changed):
                # Own map OCR/fingerprint can lag the actual warp - cross-check
                # against the warrior's own post-crossing map (they already
                # settled there before we tapped) as an independent signal.
                # They were on a different map than us when this crossing was
                # armed, so classify_map_sync reading "same" now can only mean
                # we actually landed there, even if our own before/after diff
                # hasn't caught up yet.
                local_now = {
                    "map_info_text": getattr(self.state, "map_info_text", "") or getattr(self.state, "current_map", ""),
                    "current_map": getattr(self.state, "current_map", ""),
                    "map_info_fingerprint": getattr(self.state, "map_info_fingerprint", ""),
                }
                remote_now = self._get_remote_warrior_snapshot()
                if (
                    remote_now
                    and before_map_sync != "same"
                    and classify_map_sync(local_now, remote_now, now=time.time()) == "same"
                ):
                    remote_confirmed = True
            if map_sig_changed or fp_changed or remote_confirmed:
                entered = True
                break
            humanized_sleep(0.06, variance=0.10)

        if entered:
            # Cross-check against the warrior's own post-portal coordinate
            # (their telemetry already reflects wherever they landed) -
            # confirms we warped to the SAME place, not just some map.
            after_pos = (
                int(getattr(self.state, "x", 0) or 0),
                int(getattr(self.state, "y", 0) or 0),
            )
            warrior_snapshot = self._get_remote_warrior_snapshot()
            warrior_now = None
            warrior_gap = None
            if warrior_snapshot:
                try:
                    warrior_now = (
                        int(warrior_snapshot.get("x", warrior_snapshot.get("pos_x", 0)) or 0),
                        int(warrior_snapshot.get("y", warrior_snapshot.get("pos_y", 0)) or 0),
                    )
                    warrior_gap = follow_manhattan_gap(
                        after_pos[0], after_pos[1], warrior_now[0], warrior_now[1]
                    )
                except Exception:
                    warrior_now = None
                    warrior_gap = None
            print(
                f"[PortalFollow] entry confirmed: dosa_now={after_pos}, "
                f"warrior_now={warrior_now}, distance={warrior_gap if warrior_gap is not None else '-'}"
            )
            cache_key = portal_cache_key(
                self._portal_follow_source_map_sig or before_own_map_sig,
                warrior_last[0],
                warrior_last[1],
            )
            cache = getattr(self.state, "portal_session_cache", None)
            if isinstance(cache, dict):
                cache[cache_key] = {
                    "enter_dir": enter_dir,
                    "approach": [int(warrior_last[0]), int(warrior_last[1])],
                    "confirmed_at": time.time(),
                    "source": "runtime_success",
                }
            self._finish_portal_follow(
                f"entered map_changed={map_sig_changed} fp_changed={fp_changed} "
                f"remote_confirmed={remote_confirmed} enter_dir={enter_dir}"
            )
            return True

        # No change detected within the poll window - nothing changes by
        # sitting here longer, and continuing to wait just keeps heal/red_tab
        # blocked for no reason. Give up on this door now instead of tapping
        # the same key again (that can undo an already-successful transition
        # on doors where the same key works both ways); normal follow picks
        # the warrior back up, and a fresh transition re-arms.
        after_pos_on_fail = (
            int(getattr(self.state, "x", 0) or 0),
            int(getattr(self.state, "y", 0) or 0),
        )
        moved = after_pos_on_fail != current_pos
        # Same door/direction has now failed identically across several live
        # sessions - this distinguishes two very different root causes for
        # next time: if pos_after == pos_before, the keypress plausibly never
        # reached the game at all (focus/hardware); if it moved but no map
        # change followed, enter_dir or the approach tile is wrong for this
        # specific door.
        print(
            f"[PortalFollow] portal enter failed: current={current_pos}, "
            f"warrior_last={warrior_last}, enter_dir={enter_dir}, "
            f"pos_after_tap={after_pos_on_fail} moved={moved}"
        )
        # moved=False no longer points at a swallowed keypress (force=True
        # above already guarantees hold_move sends it) - it more likely means
        # capture/network lag left warrior_last one tile short of the real
        # door tile, and the held key just walked into a wall next to the
        # door instead of through it. Walk one more real tile in enter_dir
        # and retry the tap from there, once. A door that's genuinely wrong
        # (moved=True but no map change) still gives up immediately - a
        # position that DID change means the key reached the game fine, so
        # nudging further would just walk past a door that was never here.
        if not moved and not self._portal_enter_nudge_used:
            self._portal_enter_nudge_used = True
            nudged = _nav_step_from_dir(warrior_last[0], warrior_last[1], enter_dir)
            print(f"[PortalFollow] no movement on tap - retrying one tile further {enter_dir} at {nudged}")
            self._portal_follow_coord = nudged
            self._portal_follow_approach = nudged
            self.state.portal_follow_coord = nudged
            self.state.portal_follow_approach = nudged
            self._portal_enter_attempted = False
            return False
        self._finish_portal_follow(f"enter_failed enter_dir={enter_dir}")
        return True

    # ----------------------------------------------------------
    def _calc_follow_target(self) -> tuple[int, int] | None:
        """
        ??? last_move_dir? ???? ??? ?? ??? ??.
        ?? ???? ??? ??? ? ?? ??? ???? ??? ?? ??? ?? ????.
        """
        remote_data = self._get_remote_warrior_snapshot()
        if not remote_data:
            return None

        dps_x = int(remote_data.get("x", remote_data.get("pos_x", 0)) or 0)
        dps_y = int(remote_data.get("y", remote_data.get("pos_y", 0)) or 0)
        if abs(dps_x) > 300 or abs(dps_y) > 300:
            return None
        map_sig = (
            str(remote_data.get("map_name", "") or ""),
            str(remote_data.get("map_floor", "") or ""),
            str(remote_data.get("current_map", "") or ""),
            str(remote_data.get("current_floor", "") or ""),
        )
        self._update_portal_follow_state(remote_data)
        portal_target = self._get_portal_follow_target()
        if portal_target:
            self._last_follow_target_key = ("portal", *map_sig, dps_x, dps_y)
            self._last_follow_target = (int(portal_target[0]), int(portal_target[1]))
            self._last_follow_target_ts = time.time()
            now = time.time()
            if now - float(self._last_portal_follow_log_time or 0.0) >= 1.0:
                print(
                    f"[PortalFollow] target warrior_last={portal_target}, "
                    f"enter_dir={getattr(self, '_portal_follow_dir', None) or '-'}, "
                    f"warrior_now=({dps_x}, {dps_y})"
                )
                self._last_portal_follow_log_time = now
            return portal_target
        # portal_follow가 끝났는데도(예: enter_failed로 포기) 격수가 이미 다른 맵으로
        # 넘어간 상태라면, 격수의 raw x,y는 도사가 있는 맵과 무관한 좌표다 - 같은 맵인
        # 것처럼 그 좌표를 목표로 삼으면 엉뚱한 방향으로 걷는다. "different"로 확실할
        # 때만 걸러낸다("pending"까지 막으면 map_info OCR이 애매한 정상 상황에서도
        # 추적이 자주 끊긴다).
        local_map_info = {
            "map_info_text": getattr(self.state, "map_info_text", "") or getattr(self.state, "current_map", ""),
            "current_map": getattr(self.state, "current_map", ""),
            "map_info_fingerprint": getattr(self.state, "map_info_fingerprint", ""),
        }
        if classify_map_sync(local_map_info, remote_data, now=time.time()) == "different":
            self._last_follow_target = None
            self._last_follow_target_key = None
            return None

        # 일반 추적은 마지막 방향값에 의존하지 않고 최신 격수 좌표를 직접 목표로 삼는다.
        # 방향값은 포탈 진입 판정에서만 사용해야 지연/오래된 방향으로 선회하지 않는다.
        follow_key = ("normal", *map_sig, dps_x, dps_y)
        cached_target = getattr(self, "_last_follow_target", None)
        cached_key = getattr(self, "_last_follow_target_key", None)
        cached_age = time.time() - float(getattr(self, "_last_follow_target_ts", 0.0) or 0.0)
        if cached_target and cached_key == follow_key and cached_age <= 0.75:
            return cached_target

        target_x = dps_x
        target_y = dps_y

        current_x = int(getattr(self.state, "x", 0) or 0)
        current_y = int(getattr(self.state, "y", 0) or 0)
        follow_target = adjust_follow_target_by_axis_gap(
            target_x,
            target_y,
            current_x,
            current_y,
            dps_x,
            dps_y,
            min_axis_gap=1,
        )
        self._last_follow_target_key = follow_key
        self._last_follow_target = (int(follow_target[0]), int(follow_target[1]))
        self._last_follow_target_ts = time.time()
        return follow_target

    def _log_portal_move_block(self, reason: str) -> None:
        # Only the caller-facing gate at the bottom of _move_toward used to
        # explain why a portal-follow step was withheld - the three early
        # returns above it (combat, support_input_blocked_until,
        # portal_move_settle_until) were completely silent, so a stall
        # caused by one of those looked identical in the log to "not moving
        # for no reason". Route all of them through the same 1s-throttled
        # line so a live stall is diagnosable from the log alone.
        #
        # 예전엔 portal_follow_active일 때만 찍었다. 그래서 사냥 이동이 이
        # 게이트(특히 target_locked로 인한 is_in_combat)에 막히면 로그에
        # 단서가 전혀 안 남았고, 그 하나 때문에 원인 추적이 세 커밋을
        # 잡아먹었다(64e5fbb 커밋 메시지 참고). 어느 상황이든 찍는다.
        now = time.time()
        if now - float(getattr(self, "_last_portal_move_block_log_time", 0.0) or 0.0) < 1.0:
            return
        self._last_portal_move_block_log_time = now
        portal = bool(getattr(self, "_portal_follow_active", False))
        where = "portal-follow" if portal else "movement"
        extra = ""
        if reason == "is_in_combat":
            extra = (f" (target_locked={bool(getattr(self.state, 'target_locked', False))}"
                     f" combat_start_time={float(getattr(self.state, 'combat_start_time', 0.0) or 0.0):.0f})")
        print(f"[NavBlock] {where} withheld: {reason}{extra}")

    def _move_toward(self, tx: int, ty: int) -> bool:
        """Move one step toward a world target, avoiding walls / monsters / users."""
        if self._is_in_combat():
            self._log_portal_move_block("is_in_combat")
            return False
        if time.time() < float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0):
            self._log_portal_move_block("support_input_blocked_until")
            return False
        if bool(getattr(self, "_portal_follow_active", False)) and time.time() < float(
            getattr(self, "_portal_move_settle_until", 0.0) or 0.0
        ):
            # A portal warp doesn't update state.x/y (OCR-based position
            # tracking) instantly. Confirmed live: dosa warped
            # 23,1 -up-> 23,0 -> (new map) 18,25, and before that landing
            # was detected, ANOTHER "up" fired using the still-stale
            # pre-warp target and walked it to 18,24 - which happened to be
            # this door's own return trigger, sending it straight back to
            # 23,1. Give position tracking a short window to catch up after
            # every portal-follow step before allowing the next one.
            self._log_portal_move_block("portal_move_settle_until")
            return False
        if not bool(getattr(self, "_portal_follow_active", False)) and time.time() < float(
            getattr(self, "_portal_follow_cooldown_until", 0.0) or 0.0
        ):
            # _finish_portal_follow() already gives new transition DETECTION
            # a grace window via this same timer, but ordinary follow
            # movement wasn't respecting it - confirmed live: right after
            # "arrived_no_transition" gave up on a crossing that may have
            # actually landed, self.state.x/y and current_map alternated
            # between the pre- and post-crossing values every single cycle
            # (12,0/도삭산804 <-> 26,28/도삭산805), and normal follow kept
            # issuing moves off whichever reading it happened to catch,
            # sending the character back and forth through the door
            # instead of just letting position/map settle first.
            now = time.time()
            if now - float(getattr(self, "_last_portal_cooldown_move_log_time", 0.0) or 0.0) >= 1.0:
                self._last_portal_cooldown_move_log_time = now
                print("[NavBlock] follow movement withheld: portal_follow_cooldown_until (letting position settle)")
            return False

        cx, cy = self.state.x, self.state.y
        dx, dy = tx - cx, ty - cy
        if dx == 0 and dy == 0:
            return True
        gap = max(abs(dx), abs(dy))
        trace_sig = (cx, cy)
        now = time.time()
        target_changed = False
        if isinstance(self._last_nav_trace_target, tuple) and len(self._last_nav_trace_target) == 2:
            lx, ly = self._last_nav_trace_target
            target_changed = (abs(int(tx) - int(lx)) + abs(int(ty) - int(ly))) >= 2
        else:
            target_changed = True

        should_log = False
        if trace_sig != self._last_nav_trace_signature:
            should_log = True
        elif (now - self._last_nav_trace_time) >= 1.25 and target_changed:
            should_log = True
        elif (now - self._last_nav_trace_time) >= 2.0:
            should_log = True

        if should_log:
            print(f"[FollowROI] Target: ({tx}, {ty}) | Current: ({cx}, {cy}) | dx={dx} dy={dy}")
            self._last_nav_trace_signature = trace_sig
            self._last_nav_trace_target = (tx, ty)
            self._last_nav_trace_pos = (cx, cy)
            self._last_nav_trace_gap = gap
            self._last_nav_trace_time = now

        follow_mode = should_allow_follow_navigation(
            getattr(self.state, "nav_follow_enabled", False),
            self._calc_follow_target() is not None,
            getattr(self.state, "is_connected", False),
        )
        aggressive_follow = bool(follow_mode and gap >= 4)
        portal_follow = bool(getattr(self, "_portal_follow_active", False))
        if follow_mode:
            # F2 추적은 격수/도사 양쪽의 pos ROI x,y만 사용한다.
            # char_grid와 Grid 기반 장애물 판정(nav_cell_blockers)은 다른 좌표계이므로
            # 추적 경로에서 제외한다. 다만 stuck-recovery가 같은 ROI 좌표계로 기록해둔
            # blocked_cells 메모리는 참조 가능하므로, 방금 막혀서 회피했던 칸으로
            # 곧바로 다시 걸어 들어가는 것만은 피한다.
            if abs(dx) >= abs(dy):
                primary_dir = "right" if dx > 0 else "left"
                secondary_dir = ("down" if dy > 0 else "up") if dy != 0 else None
            else:
                primary_dir = "down" if dy > 0 else "up"
                secondary_dir = ("right" if dx > 0 else "left") if dx != 0 else None
            step_dir = primary_dir
            blocked_cells_roi = self._get_blocked_cells()
            picked_unblocked = False
            # Toward-target axes first (primary, then secondary), but if
            # both of those are walled off, try the other two directions
            # too before giving up - this used to stop at 2 candidates, so
            # a cell blocked on both toward-target sides had no escape and
            # just kept re-issuing the same known-blocked primary move
            # every cycle (confirmed live: dosa stuck 8 tiles from a portal
            # target for several seconds with only one blocked cell ever
            # recorded, going nowhere until an unrelated coordinate jump
            # ended portal-follow early).
            fallback_dirs = [d for d in ("up", "down", "left", "right") if d not in (primary_dir, secondary_dir)]
            for candidate in (primary_dir, secondary_dir, *fallback_dirs):
                if candidate is None:
                    continue
                if _nav_step_from_dir(int(cx), int(cy), candidate) not in blocked_cells_roi:
                    step_dir = candidate
                    picked_unblocked = True
                    break
            if not picked_unblocked:
                # 사방이 다 막혔다고 기억하고 있다면 그 기억이 틀렸다 - 지금
                # 서 있는 칸까지 걸어온 길이 반드시 있다. 실측에서 벽 기억을
                # 60초로 늘린 뒤 오판(좌표 OCR 지연으로 성공한 이동을 실패로
                # 셈)까지 오래 남아 이 상태로 갇혔다. 주변 기억만 지우고
                # 다시 배우게 한다(진짜 벽이면 곧 다시 채워진다).
                self._forget_neighbours(int(cx), int(cy))
                blocked_cells_roi = self._get_blocked_cells()
                for candidate in (primary_dir, secondary_dir, *fallback_dirs):
                    if candidate is None:
                        continue
                    if _nav_step_from_dir(int(cx), int(cy), candidate) not in blocked_cells_roi:
                        step_dir = candidate
                        picked_unblocked = True
                        break
            if not picked_unblocked:
                # All 4 neighbor cells are in blocked_cells_roi memory -
                # step_dir falls back to primary_dir above and this branch
                # still issues that (possibly-blocked) move below (unlike
                # the nav_pick_step_direction path, this one never returns
                # None), so a genuinely stuck cell here looks like ordinary
                # movement in the log with no explanation for why position
                # isn't advancing.
                self._log_portal_move_block(
                    f"all 4 directions in blocked_cells_roi: primary={primary_dir} secondary={secondary_dir}"
                )
            # 여기까지의 step_dir은 '메모리상 안 막힌 아무 한 칸'일 뿐, 벽
            # 뒤에 있는 격수로 이어지는 경로가 아니다. 목표 쪽 1차 방향이
            # 막혀 있으면(=우회가 필요한 상황) 학습한 벽을 장애물로 삼아
            # BFS로 우회로의 첫 스텝을 구해 그걸 쓴다. greedy + 수직 twitch
            # 만으로는 벽을 절대 못 돌아간다(실측: 격수가 벽 2칸 뒤에서 죽어
            # 있는데 도사가 같은 벽에 14번 연속 헤딩).
            # ponytail: follow에서도 hunt.path_step 재사용. A*는 불필요(격자 좁음).
            primary_cell = _nav_step_from_dir(int(cx), int(cy), primary_dir)
            if (not picked_unblocked) or (primary_cell in blocked_cells_roi):
                detour_blocked = {c for c in blocked_cells_roi if c != (int(tx), int(ty))}
                detour = hunt.path_step(
                    (int(cx), int(cy)), (int(tx), int(ty)), detour_blocked, radius=24
                )
                if detour:
                    now_fd = time.time()
                    if now_fd - float(getattr(self, "_last_follow_detour_log", 0.0) or 0.0) >= 2.0:
                        self._last_follow_detour_log = now_fd
                        print(f"[Nav] follow 우회 경로: {detour} "
                              f"(({cx},{cy}) -> ({tx},{ty}), greedy={step_dir}, "
                              f"막힌칸 {len(detour_blocked)}개)")
                    step_dir = detour
            next_grid = (int(cx), int(cy))
            blockers = []
        else:
            ccx, ccy = self._get_nav_grid_pos()
            blocked_cells = set() if portal_follow else self._get_blocked_cells()
            step_dir, next_grid, blockers = nav_pick_step_direction(
                self.state,
                (ccx, ccy),
                dx,
                dy,
                include_entities=True,
                blocked_cells=blocked_cells,
                prefer_manhattan_reduction=follow_mode,
                aggressive_follow=aggressive_follow,
            )
        if step_dir is None and portal_follow:
            retry_step_dir, retry_next_grid, retry_blockers = nav_pick_step_direction(
                self.state,
                (ccx, ccy),
                dx,
                dy,
                include_entities=False,
                blocked_cells=set(),
                prefer_manhattan_reduction=True,
                aggressive_follow=True,
            )
            if retry_step_dir is not None:
                print(f"[PortalFollow] direct path retry without entities: dir={retry_step_dir}")
                step_dir, next_grid, blockers = retry_step_dir, retry_next_grid, retry_blockers

        if step_dir is None and follow_mode and "blocked_memory" in blockers:
            self._follow_blocked_memory_count += 1
            if self._follow_blocked_memory_count >= 3:
                retry_step_dir, retry_next_grid, retry_blockers = nav_pick_step_direction(
                    self.state,
                    (ccx, ccy),
                    dx,
                    dy,
                    include_entities=True,
                    blocked_cells=set(),
                    prefer_manhattan_reduction=True,
                    aggressive_follow=aggressive_follow,
                )
                if retry_step_dir is not None:
                    print(
                        "[Nav] blocked_memory bypass: "
                        f"count={self._follow_blocked_memory_count}, dir={retry_step_dir}"
                    )
                    step_dir, next_grid, blockers = retry_step_dir, retry_next_grid, retry_blockers
                    self._follow_blocked_memory_count = 0
        elif step_dir is not None:
            self._follow_blocked_memory_count = 0

        # 막힌 칸을 이미 알고 있다면 BFS가 greedy보다 낫다. greedy는 한 칸
        # 앞만 보므로, 목표 쪽이 벽이고 통로가 옆으로 몇 칸 떨어진 배치를
        # 못 푼다 (실측: down 355번 시도해서 13번 성공, y=15로 끝내 못 감).
        # 4방향이 전부 막혀야만 step_dir이 None이 되는데 좌우는 늘 열려
        # 있어서, 우회로 계산이 호출조차 되지 않았다.
        # follow_mode는 nav_follow_enabled + is_connected 만으로도 켜져서,
        # 따라갈 대상이 없는 격수까지 follow 분기로 들어간다. 그 분기는 목표
        # 쪽 방향만 고르고 우회로를 계산하지 않아서 BFS가 영영 호출되지 않았다
        # (실측: [Nav] 우회 경로 0줄, 대신 all-4-blocked 만 반복). 실제로
        # 따라갈 상대가 있을 때만 제외한다 - 도사 동작은 그대로다.
        chasing_someone = self._calc_follow_target() is not None
        if not chasing_someone and not portal_follow:
            known_blocked = self._get_blocked_cells()
            if len(known_blocked) >= 3:
                detour = hunt.path_step((cx, cy), (tx, ty), known_blocked, radius=24)
                if detour and detour != step_dir:
                    now_d = time.time()
                    if now_d - getattr(self, "_last_detour_log", 0.0) >= 2.0:
                        self._last_detour_log = now_d
                        print(f"[Nav] 우회 경로: {detour} (({cx},{cy}) -> ({tx},{ty}), "
                              f"greedy={step_dir}, 막힌칸 {len(known_blocked)}개)")
                    step_dir = detour
                elif detour:
                    step_dir = detour

        if step_dir is None:
            # greedy도 BFS도 길을 못 찾았다.
            detour = hunt.path_step((cx, cy), (tx, ty),
                                    self._get_blocked_cells(), radius=24)
            if detour:
                print(f"[Nav] 우회 경로: {detour} (({cx},{cy}) -> ({tx},{ty}))")
                step_dir = detour
        if step_dir is None:
            if blockers:
                print(f"[Nav] blocked: {','.join(blockers)}")
            if portal_follow:
                self._nav_attempt_pos = None
                self._nav_attempt_started_at = 0.0
                return False
            self._remember_last_move_blocked_cell(reason="no_step")
            if follow_mode and should_defer_stuck_escape_for_support(
                f"{getattr(self.state, 'role', '')} {getattr(self.state, 'network_role', '')}",
                bool(getattr(self.state, "service_active", False)) or bool(getattr(self.state, "auto_hunt", False)),
                getattr(self.state, "nav_follow_enabled", False),
            ):
                self._quick_support_follow_escape((tx, ty))
                return False
            self._escape_stuck(rewind_waypoint=False)
            return False

        base_hold = TIMING_CONFIG["move_hold"]
        hold_time = base_hold
        if follow_mode and not portal_follow:
            # follow gap based hold-time scaling (move faster when far behind).
            # Portal-follow must NOT use this: at gap>=4 hold stretches to 0.145s,
            # long enough that stepping onto a door tile carries residual motion
            # into the destination map for the rest of that hold - reported as
            # "only 1 tile should move but it moved several". Portal-follow always
            # uses the minimum base hold instead, below.
            if gap >= 4:
                hold_time = max(hold_time, 0.145)
            elif gap >= 3:
                hold_time = max(hold_time, 0.130)
            elif gap >= 2:
                hold_time = max(hold_time, 0.115)
            else:
                hold_time = max(hold_time, 0.090)

        hold_time = max(0.050, min(0.180, random.gauss(hold_time, 0.007)))
        in_combat = self._is_in_combat()
        input_blocked = time.time() < float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0)
        # hw.hold_move() itself (bis_core.py) also silently no-ops on
        # support_targeting_active / red_tab_promotion_active / is_combat_busy
        # - the original diagnostic only checked two of these four gates, so
        # a freeze caused by an in-progress red_tab retarget (e.g. the
        # "heal anyway" critical-HP override that can fire mid portal-follow)
        # would sail through this check and call hold_move anyway, which
        # would then silently do nothing with no explanation in the log.
        support_targeting = bool(getattr(self.state, "support_targeting_active", False)) or bool(
            getattr(self.state, "red_tab_promotion_active", False)
        )
        # 대상선택박스(tab으로 열림)가 켜져 있으면 방향키가 캐릭터가 아니라
        # 박스를 조작한다 - 그 상태로 hold_move를 보내면 "따라가는데 안 움직임".
        # ntab_active_since가 2s 넘게 묵었으면 닫혔는데 플래그만 남은 것으로
        # 보고 무시한다(영구 이동 정지 방지).
        ntab_since = float(getattr(self.state, "ntab_active_since", 0.0) or 0.0)
        ntab_box_open = bool(getattr(self.state, "ntab_active", False)) and (
            ntab_since <= 0.0 or (time.time() - ntab_since) < 2.0
        )
        hw_combat_busy = bool(getattr(self.state, "is_combat_busy", False))
        if in_combat or input_blocked or support_targeting or hw_combat_busy or ntab_box_open:
            if portal_follow and (time.time() - float(getattr(self, "_last_portal_move_block_log_time", 0.0) or 0.0)) >= 1.0:
                print(
                    f"[NavBlock] portal-follow movement withheld: in_combat={in_combat} "
                    f"target_locked={bool(getattr(self.state, 'target_locked', False))} "
                    f"input_blocked={input_blocked} support_targeting={support_targeting} "
                    f"is_combat_busy={hw_combat_busy}"
                )
                self._last_portal_move_block_log_time = time.time()
            elif follow_mode and not portal_follow and (time.time() - float(getattr(self, "_last_follow_move_block_log_time", 0.0) or 0.0)) >= 0.5:
                # Same gate as the portal-follow branch above, but this is the
                # path that actually fires during ordinary following - a heal
                # retarget (esc>tab>tab) sets support_targeting/is_combat_busy
                # and can hold support_input_blocked_until up to 0.75s, and
                # every step call in that window returns False here with no
                # move ever attempted. This was previously invisible in logs.
                now_block = time.time()
                block_left = max(0.0, float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0) - now_block)
                print(
                    f"[NavBlock] {time.strftime('%H:%M:%S', time.localtime(now_block))}.{int(now_block % 1 * 1000):03d} "
                    f"follow movement withheld: in_combat={in_combat} input_blocked={input_blocked} "
                    f"block_left={block_left:.2f}s support_targeting={support_targeting} "
                    f"is_combat_busy={hw_combat_busy} ntab_box_open={ntab_box_open}"
                )
                self._last_follow_move_block_log_time = now_block
            return False
        moved = hw.hold_move(step_dir, "move_hold", duration=hold_time)
        if not moved:
            # hold_move can still silently no-op here (its own internal
            # checks run after simulate_pause's sleep, so state can flip
            # true in that window even though the checks above just
            # passed). Don't arm mark_nav_attempt/settle_until on a
            # keypress that never went out - that was turning every
            # missed race into a self-renewing 0.2s block loop with no
            # actual move ever attempted again.
            self._log_portal_move_block("hold_move_no_op")
            return False
        # 키는 실제로 나갔다. 그런데도 좌표가 안 변하는 경우가 있어서, 그때
        # 캐릭터가 어느 쪽을 보고 있는지를 같이 남긴다(1초 제한):
        #   facing이 step_dir을 따라 바뀌는데 좌표만 그대로 -> 키는 게임에
        #     닿았고 캐릭터는 '제자리에서 방향만 트는' 중이다(또는 진짜 벽).
        #   facing이 전혀 안 바뀐다 -> 키가 게임까지 안 갔다.
        # 이 둘은 지금까지 로그에서 완전히 똑같이 보였다.
        now_mv = time.time()
        # 직전에 보낸 이동이 좌표를 실제로 바꿨는지 채점한다.
        if self._pending_move and not self._coord_fresh_since(self._pending_move[2]):
            # 좌표가 아직 그 이동보다 오래된 화면에서 읽은 값이다. 채점하면
            # 실제로 지나간 칸까지 벽으로 기억한다 - 갱신될 때까지 미룬다.
            pass
        else:
            if self._pending_move:
                prev_dir, prev_pos, _sent_at = self._pending_move
                st = self._move_result_stats.setdefault(prev_dir, [0, 0])
                st[0] += 1
                if (cx, cy) != prev_pos:
                    st[1] += 1
                    self._move_fail_streak.clear()   # 움직였으면 실패 기록은 무효
                else:
                    # 키는 나갔는데 좌표가 그대로다. 다만 한 번으로 벽이라고
                    # 단정하면 안 된다 - 좌표는 화면 OCR로 읽어서 이동보다 한
                    # 박자 늦게 갱신되고, 실측에서 캐릭터가 실제로 밟고 지나간
                    # 칸((8,13),(8,14))까지 막힘으로 기록해 스스로 길을 막았다.
                    # 같은 자리에서 같은 방향이 두 번 연속 실패해야 벽으로 본다.
                    key = (prev_pos, prev_dir)
                    fails = self._move_fail_streak.get(key, 0) + 1
                    self._move_fail_streak[key] = fails
                    if fails >= 2:
                        bx, by = _nav_step_from_dir(prev_pos[0], prev_pos[1], prev_dir)
                        self._remember_blocked_cell(bx, by, reason="no_move")
            self._pending_move = (step_dir, (cx, cy), now_mv)
        if now_mv - self._last_move_stats_log >= 10.0 and self._move_result_stats:
            self._last_move_stats_log = now_mv
            parts = " ".join(f"{d}={v[1]}/{v[0]}"
                             for d, v in sorted(self._move_result_stats.items()))
            print(f"[MoveStats] 방향별 이동성공 {parts}  (0/N인 방향 = 벽이거나 그 키가 안 먹음)")
        if now_mv - self._last_move_facing_log_time >= 1.0:
            self._last_move_facing_log_time = now_mv
            print(f"[MoveDiag] {step_dir} 키 전송됨 | pos=({cx},{cy}) "
                  f"facing={getattr(self.state, 'my_facing', '') or '?'}")
        self.state.last_move_dir = step_dir
        self._mark_nav_attempt()
        if portal_follow:
            self._portal_move_settle_until = time.time() + 0.20

        arrived = abs(dx) <= 1 and abs(dy) <= 1
        if arrived:
            print(f"[Nav] arrived: waypoint {self.wp_idx}")
        return arrived

    # [V5] ?꾩씠???띾뱷??寃⑹옄 湲곕컲 ?대룞
    def _move_toward_route_point(self, point) -> bool:
        parsed_point = parse_route_point(point)
        if not parsed_point:
            return False

        if parsed_point.get("mode") == "absolute":
            tx = int(parsed_point["x"])
            ty = int(parsed_point["y"])
            # 사냥 이탈 제한(leash)의 기준점 = 지금 향하는 순찰 포인트
            self.state.nav_current_route_point = (tx, ty)
            radius = max(0, int(parsed_point.get("radius", 1) or 1))
            if abs(tx - self.state.x) <= radius and abs(ty - self.state.y) <= radius:
                return True
            return self._move_toward(tx, ty)

        # grid 이름 방식('B12' 등)은 폐기했다 - 좌표계는 pos(x,y) 하나뿐이다.
        print(f"[Seq] grid 방식 포인트는 더 이상 지원하지 않는다: {point}")
        return True

    def _traverse_points_only(self, seq_data, is_reverse: bool) -> None:
        points = seq_data.get("points", [])
        if not points:
            return

        if self.seq_phase != "points":
            self.seq_phase = "points"
            self.seq_point_idx = len(points) - 1 if is_reverse else 0

        if self.seq_point_idx < 0 or self.seq_point_idx >= len(points):
            self.seq_point_idx = len(points) - 1 if is_reverse else 0

        point = points[self.seq_point_idx]
        arrived = self._move_toward_route_point(point) or self._patrol_point_timed_out(self.seq_point_idx)
        if arrived:
            print(f"[Seq] patrol point arrived: {self.seq_point_idx}")
            self.seq_point_idx += -1 if is_reverse else 1
            if self.seq_point_idx < 0:
                self.seq_point_idx = len(points) - 1
            elif self.seq_point_idx >= len(points):
                self.seq_point_idx = 0

    def _patrol_point_timed_out(self, idx: int) -> bool:
        """순찰 포인트에 제한 시간 안에 못 닿으면 건너뛴다.
        벽 학습으로도 못 뚫는 지점이 있어서, 없으면 거기서 영원히 멈춘다.
        건너뛴 횟수를 남겨서 잘못 찍은 포인트를 나중에 알아볼 수 있게 한다."""
        now = time.time()
        if getattr(self, "_patrol_point_idx_seen", None) != idx:
            self._patrol_point_idx_seen = idx
            self._patrol_point_started_at = now
            return False
        limit = float(getattr(self, "_patrol_point_timeout", 15.0) or 15.0)
        if now - float(getattr(self, "_patrol_point_started_at", now)) < limit:
            return False
        self._patrol_point_skips[idx] = int(self._patrol_point_skips.get(idx, 0)) + 1
        print(f"[Seq] patrol point {idx} 도달 실패 {limit:.0f}s -> 건너뜀 "
              f"(누적 {self._patrol_point_skips[idx]}회)")
        self._patrol_point_started_at = now
        return True

    def _get_reverse_direction(self, direction, reverse_action):
        """??갑??諛⑺뼢??寃곗젙: reverse_action ?곗꽑, ?놁쑝硫??먮룞 怨꾩궛"""
        if reverse_action:
            return reverse_action
        # ?먮룞 怨꾩궛
        reverse_map = {"up": "down", "down": "up", "left": "right", "right": "left"}
        return reverse_map.get(direction, direction)

    def _traverse_sequence(self, seq_data):
        """?쒗??湲곕컲 ?대룞: entry ??points ??exit (?쇰컲) / exit ??points(??닚) ??entry (??갑??"""
        now = time.time()
        is_reverse = self.state.reverse_mode
        if "entry" not in seq_data and "exit" not in seq_data and seq_data.get("points"):
            self._traverse_points_only(seq_data, is_reverse)
            return
        
        # ??갑??紐⑤뱶: Exit ??Points(??닚) ??Entry
        if is_reverse:
            # Exit ?④퀎 (??갑???쒖옉??
            if self.seq_phase == "entry":  # ??갑?μ뿉?쒕뒗 entry瑜?exit濡??ъ슜
                self._set_nav_context("seq_entry")
                exit_data = seq_data.get("exit", {"x": 0, "y": 0, "direction": "down", "reverse_action": ""})
                tx, ty = exit_data["x"], exit_data["y"]
                
                # 異쒓뎄 醫뚰몴濡??대룞
                arrived = self._move_toward(tx, ty)
                if arrived:
                    # ??갑??諛⑺뼢??寃곗젙
                    rev_dir = self._get_reverse_direction(exit_data["direction"], exit_data.get("reverse_action", ""))
                    print(f"[Seq-Rev] 異쒓뎄 ?꾨떖: ({tx}, {ty}) ??諛⑺뼢??{rev_dir} 1珥덇컙 ?꾨쫫")
                    self.state.last_move_dir = rev_dir
                    hw.hold_move(rev_dir, "exit_hold")
                    time.sleep(1.0)
                    self.seq_phase = "points"
                    self.seq_point_idx = len(seq_data.get("points", [])) - 1  # 留덉?留??щ깷?먮???
            
            # Points ?④퀎 (??닚)
            elif self.seq_phase == "points":
                points = seq_data.get("points", [])
                if self.seq_point_idx >= 0:
                    point = points[self.seq_point_idx]
                    point_label = point.get("id", "") if isinstance(point, dict) else str(point)
                    self._set_nav_context(
                        "seq_point",
                        seq_point_idx=self.seq_point_idx,
                        grid_name=point_label,
                        advance_delta=-1,
                    )
                    
                    if self._move_toward_route_point(point):
                        print(f"[Seq-Rev] ?щ깷???꾨떖: {point_label}")
                        self.seq_point_idx -= 1
                else:
                    self.seq_phase = "exit"  # ??갑?μ뿉?쒕뒗 exit瑜?entry濡??ъ슜
            
            # Entry ?④퀎 (??갑??醫낅즺??
            elif self.seq_phase == "exit":
                self._set_nav_context("seq_exit")
                entry = seq_data.get("entry", {"x": 0, "y": 0, "direction": "right", "reverse_action": ""})
                tx, ty = entry["x"], entry["y"]
                
                # ?낃뎄 醫뚰몴濡??대룞
                arrived = self._move_toward(tx, ty)
                if arrived:
                    # ??갑??諛⑺뼢??寃곗젙
                    rev_dir = self._get_reverse_direction(entry["direction"], entry.get("reverse_action", ""))
                    print(f"[Seq-Rev] ?낃뎄 ?꾨떖: ({tx}, {ty}) ??諛⑺뼢??{rev_dir} 1珥덇컙 ?꾨쫫")
                    self.state.last_move_dir = rev_dir
                    hw.hold_move(rev_dir, "entry_hold")
                    time.sleep(1.0)
                    # ?쒗???꾨즺, ?ㅼ떆 泥섏쓬遺??
                    self.seq_phase = "entry"
                    self.seq_point_idx = 0
        
        # ?쇰컲 紐⑤뱶: Entry ??Points ??Exit
        else:
            # Entry ?④퀎
            if self.seq_phase == "entry":
                self._set_nav_context("seq_entry")
                entry = seq_data.get("entry", {"x": 0, "y": 0, "direction": "right", "reverse_action": ""})
                tx, ty = entry["x"], entry["y"]
                
                # ?낃뎄 醫뚰몴濡??대룞
                arrived = self._move_toward(tx, ty)
                if arrived:
                    print(f"[Seq] ?낃뎄 ?꾨떖: ({tx}, {ty}) ??諛⑺뼢??{entry['direction']} 1珥덇컙 ?꾨쫫")
                    self.state.last_move_dir = entry["direction"]
                    hw.hold_move(entry["direction"], "entry_hold")
                    time.sleep(1.0)  # 1珥덇컙 hold
                    self.seq_phase = "points"
                    self.seq_point_idx = 0
            
            # Points ?④퀎
            elif self.seq_phase == "points":
                points = seq_data.get("points", [])
                if self.seq_point_idx < len(points):
                    point = points[self.seq_point_idx]
                    point_label = point.get("id", "") if isinstance(point, dict) else str(point)
                    self._set_nav_context(
                        "seq_point",
                        seq_point_idx=self.seq_point_idx,
                        grid_name=point_label,
                        advance_delta=1,
                    )
                    
                    if self._move_toward_route_point(point):
                        print(f"[Seq] ?щ깷???꾨떖: {point_label}")
                        self.seq_point_idx += 1
                else:
                    self.seq_phase = "exit"
            
            # Exit ?④퀎
            elif self.seq_phase == "exit":
                self._set_nav_context("seq_exit")
                exit_data = seq_data.get("exit", {"x": 0, "y": 0, "direction": "down", "reverse_action": ""})
                tx, ty = exit_data["x"], exit_data["y"]
                
                # 異쒓뎄 醫뚰몴濡??대룞
                arrived = self._move_toward(tx, ty)
                if arrived:
                    print(f"[Seq] 異쒓뎄 ?꾨떖: ({tx}, {ty}) ??諛⑺뼢??{exit_data['direction']} 1珥덇컙 ?꾨쫫")
                    self.state.last_move_dir = exit_data["direction"]
                    hw.hold_move(exit_data["direction"], "exit_hold")
                    time.sleep(1.0)  # 1珥덇컙 hold
                    # ?쒗???꾨즺, ?ㅼ떆 泥섏쓬遺??
                    self.seq_phase = "entry"
                    self.seq_point_idx = 0

    # ----------------------------------------------------------
    def run(self):
        print("[Nav] V5 Navigation Thread started...")
        while self.state.running:

            if getattr(self.state, "automation_paused", False):
                self._clear_nav_context()
                self._was_in_combat = False
                self._advance_waypoint_after_combat = False
                self._combat_resume_context = None
                humanized_sleep(TIMING_CONFIG["idle_sleep"])
                continue

            self._record_walked_cell()

            # Hot-Reload
            if self.state.last_update_time > self.last_load_time:
                self.last_load_time = time.time()
                print("[Sync] [Nav] Reloading route settings...")

            nav_requested = self.state.nav_route_enabled or self.state.nav_follow_enabled or self.state.nav_avoid_enabled
            if not self.state.auto_hunt and not nav_requested:
                self._clear_nav_context()
                self._was_in_combat = False
                self._advance_waypoint_after_combat = False
                self._combat_resume_context = None
                humanized_sleep(TIMING_CONFIG["idle_sleep"] * 2)
                continue

            if (self.state.nav_route_enabled or self.state.nav_follow_enabled or self.state.nav_avoid_enabled) and not self.state.sentinel_enabled:
                self.state.sentinel_enabled = True
                print("[Nav] Sentinel auto-enabled for navigation")

            in_combat = bool(self.state.is_combat_busy)
            support_input_blocked = time.time() < float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0)
            if in_combat and not self._was_in_combat:
                self._mark_combat_pause()
            elif (not in_combat) and self._was_in_combat:
                self._advance_waypoint_after_interrupt()
            self._was_in_combat = in_combat

            # ?? 0?쒖쐞: ?꾪닾 以??대룞 ?뺤? (is_combat_busy ?뚮옒洹?媛먯떆) ??
            if in_combat or support_input_blocked:
                # 이 continue는 아래 사냥 이동(item_pickup_target)까지 통째로
                # 건너뛰는데 지금까지 로그가 한 줄도 안 나갔다 - 격수가 안
                # 움직일 때 여기서 막힌 건지조차 알 수가 없었다(1초 제한).
                if getattr(self.state, "item_pickup_target", None):
                    now_blk = time.time()
                    if now_blk - self._last_combat_busy_block_log_time >= 1.0:
                        self._last_combat_busy_block_log_time = now_blk
                        print(f"[NavBlock] 사냥 이동 목표 "
                              f"{self.state.item_pickup_target} 보류: "
                              f"is_combat_busy={in_combat} "
                              f"support_input_blocked={support_input_blocked}")
                humanized_sleep(TIMING_CONFIG["nav_loop"])
                continue

            self._clear_nav_context()

            # ?? 1?쒖쐞: Stuck Escape (留됲옒 媛먯? ???ㅻⅨ 濡쒖쭅 ?쇱떆 以묐떒) ??
            if self._check_stuck():
                self._escape_stuck(rewind_waypoint=False)
                humanized_sleep(TIMING_CONFIG["move_hold"])
                continue

            # ?? ?좎궗 ?꾩슜: 紐?諛吏?以묒떖?쇰줈 ?대룞 ???湲????????????????
            current_follow_target = self._calc_follow_target() if self.state.nav_follow_enabled else None
            follow_navigation_active = should_allow_follow_navigation(
                self.state.nav_follow_enabled,
                current_follow_target is not None,
                self.state.is_connected,
            )

            # 아이템 픽업: LogicSvc가 item_pickup_target(world x,y)을 세팅하면
            # 여기서 실제로 걸어간다 (LogicSvc는 RouteSvc 전용 이동 메서드에
            # 접근할 수 없는 별도 스레드라 상태값으로만 요청을 넘긴다).
            # is_connected 하나만으로 follow_navigation_active가 True가 될 수
            # 있는데(그룹 follow 자격 체크일 뿐, 실제 갈 곳이 있다는 뜻이
            # 아니다) 이걸로 사냥 이동을 막으면 목표가 없어도 영원히 막힌다.
            # 실제로 경쟁하는 목적지가 있을 때만(current_follow_target) 막는다.
            follow_target_blocking = current_follow_target is not None
            item_pickup_target = getattr(self.state, "item_pickup_target", None)
            if item_pickup_target and follow_target_blocking:
                # 실전에서 격수 이동이 전혀 안 되는 사례가 나왔다(사냥 타겟은
                # 계속 세팅되는데 좌표가 한 번도 안 바뀜) - follow 모드가
                # 격수한테도 켜져 있으면(F2가 모든 역할에 nav_follow_enabled를
                # 켠다) 이 사냥 이동 채널이 통째로 막힌다. 원인을 바로 보이게
                # 한다(1초 제한).
                now_blk = time.time()
                if now_blk - self._last_pickup_blocked_by_follow_log_time >= 1.0:
                    self._last_pickup_blocked_by_follow_log_time = now_blk
                    print(f"[Hunt] 이동 목표 {item_pickup_target}가 follow 모드에 막힘 "
                          f"(follow_target={current_follow_target})")
            # 순찰(nav_route_enabled) 중에도 이 목표가 우선이다 - 사냥 접근과
            # 아이템 줍기가 둘 다 이 채널을 쓴다 (LogicSvc가 목표만 세팅).
            if item_pickup_target and not follow_target_blocking:
                tx, ty = item_pickup_target
                arrived = self._move_toward(tx, ty)
                if arrived:
                    self.state.item_pickup_arrived = True
                    humanized_sleep(TIMING_CONFIG["idle_sleep"])
                else:
                    humanized_sleep(TIMING_CONFIG["nav_loop"])
                continue

            # ?? 2?쒖쐞: Group Follow Mode (is_connected && nav_follow_enabled) ??
            role_name = str(getattr(self.state, "role", "") or "").strip()
            network_role = str(getattr(self.state, "network_role", "") or "").strip()
            warrior_route_priority = bool(
                self.state.nav_route_enabled
                and (
                    role_name in {"격수", "Warrior", "寃⑹닔"}
                    or network_role in {"격수", "Warrior", "寃⑹닔"}
                )
            )
            if follow_navigation_active and not warrior_route_priority:
                follow_target = current_follow_target or self._calc_follow_target()
                if follow_target:
                    self._set_nav_context("follow")
                    tx, ty = follow_target
                    if bool(getattr(self, "_portal_follow_active", False)) and self._complete_portal_follow_if_arrived():
                        # Arrived exactly on the portal tile and tapped
                        # enter_dir (entered/failed - either way
                        # _finish_portal_follow already ran), or already
                        # tapped this arm and still waiting. Nothing more
                        # to do this cycle.
                        self._nav_attempt_pos = None
                        self._nav_attempt_started_at = 0.0
                        continue
                    if bool(getattr(self, "_portal_follow_active", False)):
                        # _complete_portal_follow_if_arrived() can nudge
                        # _portal_follow_coord one tile further (moved=False
                        # retry) and return False so this cycle walks toward
                        # it - but tx,ty above was snapshotted BEFORE that
                        # call, so it's still the old (now-arrived-at) target.
                        # Re-fetch fresh or the gap==0 safety net below reads
                        # "arrived but nothing happened" off the stale target
                        # and gives up before the nudge ever gets to walk
                        # (confirmed live: nudge printed, then
                        # arrived_no_transition fired the very same cycle).
                        refreshed_target = self._get_portal_follow_target()
                        if refreshed_target:
                            tx, ty = refreshed_target
                    cx, cy = self.state.x, self.state.y
                    gap_x = abs(tx - cx)
                    gap_y = abs(ty - cy)
                    follow_gap = follow_manhattan_gap(tx, ty, cx, cy)
                    hold_follow_gap = 0 if bool(getattr(self, "_portal_follow_active", False)) else int(getattr(self, "_support_follow_hold_distance", 1) or 1)
                    if should_hold_follow_position(tx, ty, cx, cy, max_gap=hold_follow_gap):
                        if bool(getattr(self, "_portal_follow_active", False)):
                            # Fallback safety net only - the normal path is
                            # _complete_portal_follow_if_arrived() above,
                            # which already handles arrival+tap and always
                            # finishes portal-follow one way or another.
                            # Reaching here while still portal_follow_active
                            # means that call returned False despite gap=0
                            # (e.g. enter_dir missing) - give up rather than
                            # sit here forever.
                            self._finish_portal_follow("arrived_no_transition")
                        # [FIX] Follow range ?덉뿉???湲???Stuck ??대㉧ 由ъ뀑 (?대룞 ???대룄 Stuck???꾨떂)
                        self._nav_attempt_pos = None
                        self._nav_attempt_started_at = 0.0
                        now = time.time()
                        if now - self._last_follow_close_log_time >= 1.5:
                            print(f"[Follow] Within follow range (pos gap: dx={gap_x}, dy={gap_y})")
                            self._last_follow_close_log_time = now
                        humanized_sleep(max(0.0015, float(TIMING_CONFIG["nav_loop"]) * 0.70))
                        continue

                    self._move_toward(tx, ty)
                    if should_prioritize_follow_distance(follow_gap, risk_distance=7):
                        humanized_sleep(max(0.001, float(TIMING_CONFIG["nav_loop"]) * 0.15))
                        continue
                    if follow_gap >= 4:
                        humanized_sleep(max(0.001, float(TIMING_CONFIG["nav_loop"]) * 0.25))
                        continue

            # ?? 3?쒖쐞: Waypoint Traversal (follow false/no target && is_in_combat false) ??
            elif self.state.nav_route_enabled and not self._is_in_combat():
                m   = self.state.current_map
                map_data = self.state.waypoints_db.get(m, {})
                
                # 留?痢?怨꾩링 援ъ“ 吏??
                if isinstance(map_data, dict):
                    # ?꾩옱 痢?寃곗젙 (湲곕낯媛? "1")
                    current_floor = "1"
                    for floor_key in map_data.keys():
                        if floor_key.isdigit():
                            current_floor = floor_key
                            break
                    
                    seq_data = map_data.get(current_floor, {})
                    
                    # ?덈줈???쒗??援ъ“ 吏??(entry, points, exit)
                    if isinstance(seq_data, dict) and seq_data.get("points"):
                        self._traverse_sequence(seq_data)
                
                # 湲곗〈 ?⑥씪 留?援ъ“ 吏??(醫뚰몴 由ъ뒪??
                elif isinstance(map_data, list):
                    wps = map_data
                    if wps:
                        if self.wp_idx >= len(wps):
                            self.wp_idx = 0
                        self._set_nav_context("list_wp", wp_idx=self.wp_idx, advance_delta=1)
                        arrived = self._move_toward(*wps[self.wp_idx])
                        if arrived:
                            print(f"[Point] ?꾨떖: {wps[self.wp_idx]}")
                            self.wp_idx += 1

            loop_sleep = float(TIMING_CONFIG["nav_loop"])
            if follow_navigation_active:
                loop_sleep = max(0.0015, loop_sleep * 0.70)
            humanized_sleep(loop_sleep)
