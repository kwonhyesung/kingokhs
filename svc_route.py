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

import time
import threading
import random

from bis_core import (
    hw, GameState, TIMING_CONFIG, humanized_sleep, GridManager, discord_notify,
)
from patrol_routes import parse_route_point
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
    should_hold_follow_gap,
    should_hold_follow_position,
    should_retarget_after_support_follow_stuck,
    should_detect_warrior_transition,
    should_prioritize_follow_distance,
    WARRIOR_TRANSITION_JUMP_DISTANCE,
    is_plausible_transition_coord,
    is_implausible_walk_speed,
    fingerprint_hamming_distance,
    MAP_FINGERPRINT_SAME_MAX_DISTANCE,
)


def grid_name_to_coords(grid_name):
    """Grid 이름(S3, A10)을 (col, row) 좌표로 변환."""
    # GridManager??name_to_grid 硫붿꽌???ъ슜
    gm = GridManager(None)
    return gm.name_to_grid(grid_name)


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
                    grid = ent.get("grid")
                    if not grid or len(grid) != 2:
                        continue
                    if int(grid[0]) == gx and int(grid[1]) == gy:
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
        self._last_ntab_success_direction = None
        self._blocked_cells_until: dict[tuple[int, int], float] = {}
        self._blocked_cell_hits: dict[tuple[int, int], int] = {}
        self._blocked_cell_base_ttl = 6.0
        self._blocked_cell_max_ttl = 18.0
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
        self.state.portal_follow_active = False
        self.state.portal_follow_retarget_requested = False
        self.state.portal_follow_coord = None
        self.state.portal_follow_approach = None
        self.state.portal_follow_dir = None

    # ----------------------------------------------------------
    def _should_hold_follow_at_gap(self, gap: int) -> bool:
        return should_hold_follow_gap(
            gap,
            hold_distance=int(getattr(self, "_support_follow_hold_distance", 1) or 1),
        )

    def _get_nav_grid_pos(self) -> tuple[int, int]:
        """char_grid가 초기값이면 실제 좌표를 blocked-memory 기준으로 사용한다."""
        try:
            gx, gy = tuple(getattr(self.state, "char_grid", (0, 0)) or (0, 0))
            gx, gy = int(gx), int(gy)
        except Exception:
            gx, gy = 0, 0
        sx = int(getattr(self.state, "x", 0) or 0)
        sy = int(getattr(self.state, "y", 0) or 0)
        if (gx, gy) == (0, 0) and (sx, sy) != (0, 0):
            return sx, sy
        return gx, gy

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
        # 장애물 폭을 실제로 벗어나게 한다. 1회차 0.13s -> 3회차부터 0.35s 상한.
        escape_duration = min(0.13 + 0.08 * attempts, 0.35)
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
    def _prune_blocked_cells(self):
        now = time.time()
        expired = [cell for cell, until in self._blocked_cells_until.items() if until <= now]
        for cell in expired:
            self._blocked_cells_until.pop(cell, None)
            self._blocked_cell_hits.pop(cell, None)

    def _get_blocked_cells(self) -> set[tuple[int, int]]:
        self._prune_blocked_cells()
        return set(self._blocked_cells_until.keys())

    def _remember_blocked_cell(self, gx: int, gy: int, reason: str = "stuck"):
        self._prune_blocked_cells()
        cell = (int(gx), int(gy))
        hits = int(self._blocked_cell_hits.get(cell, 0)) + 1
        self._blocked_cell_hits[cell] = hits
        ttl = min(self._blocked_cell_max_ttl, self._blocked_cell_base_ttl + (hits - 1) * 2.0)
        until = time.time() + ttl
        prev_until = float(self._blocked_cells_until.get(cell, 0.0) or 0.0)
        self._blocked_cells_until[cell] = max(prev_until, until)
        if hits <= 2:
            print(f"[NavMem] blocked cell remember: {cell} ttl={ttl:.1f}s reason={reason}")

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

        stuck_time_limit = float(TIMING_CONFIG.get("stuck_time", 4.0))
        if follow_navigation_active and follow_target:
            tx, ty = follow_target
            follow_gap = follow_manhattan_gap(int(tx), int(ty), int(self.state.x), int(self.state.y))
            if follow_gap >= 4:
                stuck_time_limit = min(stuck_time_limit, 0.35)
            elif follow_gap >= 2:
                stuck_time_limit = min(stuck_time_limit, 0.55)
            else:
                stuck_time_limit = min(stuck_time_limit, 1.00)
        if self._nav_attempt_started_at > 0.0 and (now - self._nav_attempt_started_at) >= stuck_time_limit:
            if bool(getattr(self, "_portal_follow_active", False)):
                if (now - float(getattr(self, "_last_portal_follow_log_time", 0.0) or 0.0)) >= 0.8:
                    print(f"[PortalFollow] stuck wait: keep exact portal path pos={current_pos}")
                    self._last_portal_follow_log_time = now
                self._blocked_cells_until.clear()
                self._blocked_cell_hits.clear()
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
        # 이동 키 입력 후 좌표 미변경 = box 충돌로 간주: ESC 1회 + F2 재준비 요청
        try:
            hw.humanized_press("esc")
            humanized_sleep(0.05)
        except Exception:
            pass
        self.state.box_collision_reprepare_requested = True
        print("[Recover] box collision suspected: esc once + request F2 reprepare.")

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
        # Nav target is the warrior's own last tile, not an offset guess one
        # tile past it - walk there, then tap the same key they used.
        self._portal_follow_approach = warrior_xy
        self._portal_follow_coord = warrior_xy
        self._portal_follow_dir = dir_norm
        self._portal_follow_source_map_sig = tuple(source_map_sig) if source_map_sig else None
        self._portal_follow_started_at = started
        self._portal_enter_attempted = False
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
                    event_prev = (event_x, event_y)
                    event_dir = normalize_move_dir(event_data.get("dir") or event_data.get("input_dir"))
                    event_from_confidence = str(event_data.get("from_confidence") or "low")
                    self._last_coord_transition_seq = event_seq
            except Exception:
                event_prev = None
                event_dir = None
                event_age = None

        if self._portal_follow_active:
            if event_prev is not None:
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
        if coord_jumped and not map_changed and not map_info_changed and event_prev is None:
            # No corroborating signal - only trust a bare distance jump as a real
            # portal if the implied speed is faster than normal walking can produce.
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
        if event_prev or (
            prev
            and is_plausible_map_coord(prev[0], prev[1])
            and (coord_jumped or map_changed or map_info_changed)
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
        fixed sleep-then-check, and does NOT auto-retry the same key on a
        no-change read: on doors where the same key also triggers the
        return trip on the other side, a blind second tap can undo a
        transition that actually succeeded but was detected late - live
        logs showed exactly this (tap 1 and tap 2 both read "no change"
        even though the dosa was standing right on the door)."""
        target = self._get_portal_follow_target()
        if not target:
            return False
        current_pos = (int(self.state.x), int(self.state.y))
        warrior_last = self._portal_follow_approach or target
        enter_dir = normalize_move_dir(self._portal_follow_dir)
        if not enter_dir:
            return False
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

        hw.force_press(enter_dir)
        self.state.last_move_dir = enter_dir

        entered = False
        map_sig_changed = False
        fp_changed = False
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
            if map_sig_changed or fp_changed:
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
                f"entered map_changed={map_sig_changed} fp_changed={fp_changed} enter_dir={enter_dir}"
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
        # Same door/direction has now failed identically across several live
        # sessions - this distinguishes two very different root causes for
        # next time: if pos_after == pos_before, the keypress plausibly never
        # reached the game at all (focus/hardware); if it moved but no map
        # change followed, enter_dir or the approach tile is wrong for this
        # specific door.
        print(
            f"[PortalFollow] portal enter failed: current={current_pos}, "
            f"warrior_last={warrior_last}, enter_dir={enter_dir}, "
            f"pos_after_tap={after_pos_on_fail} moved={after_pos_on_fail != current_pos}"
        )
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

    def _is_follow_reposition_needed(self) -> bool:
        if not bool(getattr(self.state, "nav_follow_enabled", False)):
            return False
        follow_target = self._calc_follow_target()
        if not follow_target:
            return False
        tx, ty = follow_target
        cx, cy = int(getattr(self.state, "x", 0) or 0), int(getattr(self.state, "y", 0) or 0)
        return not should_hold_follow_position(tx, ty, cx, cy)

    def _move_toward(self, tx: int, ty: int) -> bool:
        """Move one step toward a world target, avoiding walls / monsters / users."""
        if self._is_in_combat():
            return False
        if time.time() < float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0):
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
            for candidate in (primary_dir, secondary_dir):
                if candidate is None:
                    continue
                if _nav_step_from_dir(int(cx), int(cy), candidate) not in blocked_cells_roi:
                    step_dir = candidate
                    break
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
        if in_combat or input_blocked:
            # Live logs showed the dosa freeze completely for many seconds
            # while portal_follow_active with no further clue why - this
            # pins down which of the two gates (ordinary combat targeting a
            # monster vs. a lingering support_input_blocked_until) is
            # actually responsible next time it happens, instead of guessing.
            if portal_follow and (time.time() - float(getattr(self, "_last_portal_move_block_log_time", 0.0) or 0.0)) >= 1.0:
                print(
                    f"[NavBlock] portal-follow movement withheld: in_combat={in_combat} "
                    f"target_locked={bool(getattr(self.state, 'target_locked', False))} "
                    f"input_blocked={input_blocked}"
                )
                self._last_portal_move_block_log_time = time.time()
            return False
        hw.hold_move(step_dir, "move_hold", duration=hold_time)
        self.state.last_move_dir = step_dir
        self._mark_nav_attempt()

        arrived = abs(dx) <= 1 and abs(dy) <= 1
        if arrived:
            print(f"[Nav] arrived: waypoint {self.wp_idx}")
        return arrived

    # [V5] ?꾩씠???띾뱷??寃⑹옄 湲곕컲 ?대룞
    def _move_toward_grid(self, gx: int, gy: int) -> bool:
        """Move one step toward a grid target, avoiding walls / monsters / users."""
        if self._is_in_combat() or time.time() < float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0):
            return False
        ccx, ccy = self._get_nav_grid_pos()
        dgx, dgy = gx - ccx, gy - ccy

        arrived = (abs(dgx) == 0 and abs(dgy) == 0)
        if arrived:
            return True

        step_dir, next_grid, blockers = nav_pick_step_direction(
            self.state,
            (ccx, ccy),
            dgx,
            dgy,
            include_entities=True,
            blocked_cells=self._get_blocked_cells(),
        )

        if step_dir:
            if self._is_in_combat() or time.time() < float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0):
                return False
            hw.hold_move(step_dir, "move_hold")
            self.state.last_move_dir = step_dir
            self._mark_nav_attempt()
        elif blockers:
            print(f"[Nav] blocked: {','.join(blockers)}")
            self._remember_last_move_blocked_cell(reason="no_step_grid")
            self._escape_stuck(rewind_waypoint=False)

        return False

    def _move_toward_route_point(self, point) -> bool:
        parsed_point = parse_route_point(point)
        if not parsed_point:
            return False

        if parsed_point.get("mode") == "absolute":
            tx = int(parsed_point["x"])
            ty = int(parsed_point["y"])
            radius = max(0, int(parsed_point.get("radius", 1) or 1))
            if abs(tx - self.state.x) <= radius and abs(ty - self.state.y) <= radius:
                return True
            return self._move_toward(tx, ty)

        col, row = grid_name_to_coords(parsed_point["grid"])
        if col is None or row is None:
            return False
        return self._move_toward_grid(col, row)

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
        arrived = self._move_toward_route_point(point)
        if arrived:
            print(f"[Seq] patrol point arrived: {self.seq_point_idx}")
            self.seq_point_idx += -1 if is_reverse else 1
            if self.seq_point_idx < 0:
                self.seq_point_idx = len(points) - 1
            elif self.seq_point_idx >= len(points):
                self.seq_point_idx = 0

    def _find_cluster_center(self) -> tuple[int, int] | None:
        """紐ъ뒪?곌? 2留덈━ ?댁긽 萸됱튇 洹몃━?쒖쓽 以묒떖?먯쓣 諛섑솚."""
        monsters = self.state.entities.get("monsters", [])
        if not isinstance(monsters, list) or len(monsters) < 2:
            return None

        grids = []
        for m in monsters:
            if not isinstance(m, dict):
                continue
            g = m.get("grid")
            if g and len(g) == 2:
                grids.append((int(g[0]), int(g[1])))
        if len(grids) < 2:
            return None

        best_group = []
        for cx, cy in grids:
            group = [(gx, gy) for gx, gy in grids if max(abs(gx - cx), abs(gy - cy)) <= 1]
            if len(group) > len(best_group):
                best_group = group

        if len(best_group) < 2:
            return None

        avg_x = round(sum(gx for gx, _ in best_group) / len(best_group))
        avg_y = round(sum(gy for _, gy in best_group) / len(best_group))
        return (int(avg_x), int(avg_y))

    # [?쒗??湲곕컲 ?대룞]
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

            if (
                self.state.role == "술사"
                and not self._is_in_combat()
                and not self.state.nav_route_enabled
                and not follow_navigation_active
            ):
                cluster_center = self._find_cluster_center()
                if cluster_center:
                    gx, gy = cluster_center
                    arrived = self._move_toward_grid(gx, gy)
                    if arrived:
                        print(f"[MobTrain] 몹 밀집 추적 완료: Grid({gx}, {gy}) -> 대기")
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
                    if self._complete_portal_follow_if_arrived():
                        humanized_sleep(max(0.001, float(TIMING_CONFIG["nav_loop"]) * 0.25))
                        continue
                    self._set_nav_context("follow")
                    tx, ty = follow_target
                    cx, cy = self.state.x, self.state.y
                    gap_x = abs(tx - cx)
                    gap_y = abs(ty - cy)
                    follow_gap = follow_manhattan_gap(tx, ty, cx, cy)
                    hold_follow_gap = 0 if bool(getattr(self, "_portal_follow_active", False)) else int(getattr(self, "_support_follow_hold_distance", 1) or 1)
                    if should_hold_follow_position(tx, ty, cx, cy, max_gap=hold_follow_gap):
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
