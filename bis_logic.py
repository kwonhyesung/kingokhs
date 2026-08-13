"""
bis_logic.py  -  BIS Logic Engine (자동 사냥 상태 FSM)
──────────────────────────────────────────────────────────────
기본 상태 흐름: Emergency -> Combat -> Moving
일부 구간에서 Navigation 갱신(스레드)
모든 사이클에 humanized_sleep / TIMING_CONFIG 적용
전투 Fail-safe  combat_timeout 초과시 강제로 break + ESC
이동 불가(Stuck) 탐지  반복 점프나 이동 안될 경우 복구 시도
파티전 도중 "모험가가 아님" / 보너스맵 도달시 ESC
──────────────────────────────────────────────────────────────

Purpose:
    - 자동 사냥 상태 FSM(Finite State Machine) 로직 구현
    - Emergency(긴급처치) > Combat(전투) > Moving(이동) 순으로 처리
    - NavigationThread와 상태값 동기화 하여 실제 전투 진행
    - Anti-cheat 탐지를 위한 랜덤화 및 입력 변조 적용

Core Features:
    - 물약/회복 HP/MP 자동 사용
    - 자동 이동 버프/파티 지원
    - 이동 불가 상태 복구 및 전투 재시도
    - 문제 상황 감지 및 이동 제한 차단
    - 버프 자동화

Integration:
    - svc_worker.py: GUI 연동 및 상태 전달 역할
    - svc_kernel.py: 키입력 처리 및 커맨드 실행 기능
    - bis_route.py: 경로 탐색 및 이동 관리
    - spells_config.json: 버프 설정 정보 활용
"""

import time
import threading
import random
import win32gui
import win32con

from bis_core import (
    AppStatus, TargetType, hw, GameState,
    TIMING_CONFIG, humanized_sleep, tc, GridManager, display_game_hotkey
)
from bis_route import RouteManager
from bis_spell import RecoveryManager
from patrol_routes import parse_route_point
from support_runtime_rules import (
    adjust_follow_target_by_axis_gap,
    build_f5_hon_sequence,
    confirm_support_lock_by_hp_gain,
    SELF_HP_EMERGENCY_THRESHOLD,
    SELF_MP_PRIORITY_THRESHOLD,
    ZERO_HP_CONFIRMATIONS_REQUIRED,
    build_warrior_search_sequence,
    dir_between_coords,
    follow_manhattan_gap,
    infer_dir_from_trail,
    is_support_role,
    is_confirmed_zero_hp_state,
    is_plausible_map_coord,
    next_zero_hp_count,
    normalize_move_dir,
    opposite_move_dir,
    offset_coord_by_dir,
    pick_portal_enter_dir,
    resolve_portal_follow_cells,
    should_allow_follow_navigation,
    should_allow_party_support_cast,
    should_attempt_portal_enter,
    should_block_party_heal,
    should_clear_target_box_after_support_stuck,
    should_cast_periodic_heewon,
    classify_map_sync,
    should_continue_self_hp_recovery,
    should_defer_stuck_escape_for_support,
    should_hold_follow_gap,
    should_hold_follow_position,
    should_ignore_monster_combat_for_support_autohunt,
    should_prioritize_self_mp,
    should_prioritize_self_mp_over_party_heal,
    should_retarget_after_support_follow_stuck,
    should_detect_warrior_transition,
    should_prioritize_follow_distance,
    should_trigger_self_hp_emergency,
    speed_up_delay,
    support_retarget_block_duration,
    support_follow_stuck_timeout,
    WARRIOR_TRANSITION_JUMP_DISTANCE,
    is_plausible_transition_coord,
)



# ============================================================
#  ActionThread
# ============================================================
class LogicSvc(threading.Thread):
    """
    논리 처리 / 자동전투 FSM 스레드 (NavigationThread와 독립적으로 동작)

    Purpose:
        - 게임 내 모든 액션 루틴 처리 (전투, 회복, 버프, 이동)
        - 상태 및 이벤트 기반 FSM 전환과 액션 실행
        - 비정상 상황 자동 감지 및 복구 지원
        
    Architecture:
        - FSM(Finite State Machine) 상태기반 구조
        - 메인 스레드에서 NavigationThread와 분리 실행 시작
        - rate-limiting으로 입력 제어 및 반복 최소화
        - fail-safe 메커니즘으로 예외 상황 감지 및 회복
        
    State Flow:
        IDLE → EMERGENCY_HEAL/MANA_REGEN/COMBAT/SCANNING → BUFFING → IDLE
        
    Key Features:
        - 자동전투 핵심 로직 (v3 엔진)
        - 직업군별 자동탐색(OCR 기반)
        - 자동 회복/버프 알고리즘 구현
        - 다양한 예외 및 비정상 이동 처리
    """

    def __init__(self, state: GameState):
        """
        LogicSvc 클래스 초기화
        
        Args:
            state (GameState): 게임 상태 객체
            
        Attributes:
            state: 게임 상태 참조
            current_state: 현재 FSM 상태 (AppStatus Enum)
            last_load_time: 마지막 설정/로드 시간
            task_active: 자동 사냥 동작 활성화 여부 (F2 등으로 제어)
            _last_*_time: 각종 반응 rate-limiting 체크용 시간값
        """
        super().__init__(daemon=True)  # 메인 프로세스 종료 시 자동 종료
        self.state         = state  # 게임 상태 참조
        self.current_state = AppStatus.IDLE  # 초기 상태: 대기
        self.last_load_time = time.time()  # 마지막 설정 변경 시각
        self.task_active   = False  # F2 자동 사냥 동작 여부
        
        # Trigger 반응 rate-limit (회복/탐색/전투, 중복 처리 방지)
        self._last_hp_recover_time = 0.0  # HP 회복 처리 시간 제한
        self._last_mp_recover_time = 0.0  # MP 회복 처리 시간 제한
        self._last_target_search_time = 0.0  # 타겟 탐색 시간 제한
        self._last_monster_seen_time = 0.0  # 몬스터 최근 인지 시간
 
        self.recovery_manager = RecoveryManager(self.state)
        self._support_self_pattern = "jump2"
        self._support_warrior_pattern = "jump"
        self._support_wizard_pattern = "dah"
        self._ntab_repeat_per_direction = 1
        self._ntab_confirm_required_hits = 1
        self._ntab_confirm_timeout = 0.42
        self._warrior_next_attack_at = 0.0
        self._warrior_next_loot_at = 0.0
        self._last_warrior_cycle_log_time = 0.0
        self._red_tab_confirm_required_hits = 2
        self._red_tab_confirm_timeout = 0.45
        self._red_tab_promotion_timeout = 0.95
        self._red_tab_promotion_recent_grace = 0.18
        self._warrior_bomu_interval = 186.0
        self._current_nav_context = None
        self._combat_resume_context = None
        self._advance_waypoint_after_combat = False
        self._self_status_refresh_interval = 4.8
        self._self_status_stale_after = 1.0
        self._self_status_refresh_fail_until = 0.0
        self._last_self_status_missing_log_time = 0.0
        self._last_hw_skip_log_time = 0.0
        self._last_focus_skip_log_time = 0.0
        self._ntab_in_progress = False
        self._service_loop_active_sleep = 0.0035
        self._service_loop_idle_sleep = 0.0065
        self._service_loop_no_target_sleep = 0.0100
        self._self_status_scan_window_sec = 1.30
        self._last_self_redtab_clear_time = 0.0
        self._redtab_clear_cooldown_sec = 1.0
        self._last_party_hp_support_time = 0.0
        self._last_party_hp_value = 0
        self._last_party_hp_check_log_time = 0.0
        self._last_party_direct_heal_log_time = 0.0
        self._last_dosa_f2_watchdog_log_time = 0.0
        self._last_dosa_f2_idle_reason_log_time = 0.0
        self._party_hp_tick_base_interval_min = 0.18
        self._party_hp_tick_base_interval_max = 0.22
        self._party_hp_tick_fast_interval_min = 0.16
        self._party_hp_tick_fast_interval_max = 0.20
        self._party_hp_next_tick_interval = 0.18
        self._last_warrior_bomu_attempt_time = 0.0
        self._warrior_bomu_fail_retry_sec = 6.0
        self._last_redtab_lock_attempt_time = 0.0
        self._redtab_lock_retry_sec = 0.10
        self._redtab_lock_backoff_until = 0.0
        self._last_redtab_lock_fail_log_time = 0.0
        self._last_warrior_redtab_skip_log_time = 0.0
        self._last_local_redtab_seen_time = 0.0
        self._support_lock_search_until = 0.0
        self._warrior_jump_confirm_timeout = 0.24
        self._warrior_jump_settle_delay = 0.05
        self._self_hp_emergency_threshold = SELF_HP_EMERGENCY_THRESHOLD
        self._self_mp_priority_threshold = SELF_MP_PRIORITY_THRESHOLD
        self._zero_hp_confirm_required = ZERO_HP_CONFIRMATIONS_REQUIRED
        self._zero_hp_read_count = 0
        self._death_recovery_active = False
        self._last_self_emergency_hp_time = 0.0
        self._self_emergency_hp_cooldown = 0.80
        self._last_self_mp_priority_time = 0.0
        self._self_mp_priority_interval = 0.65
        self._self_mp_priority_failed_until = 0.0
        self._self_mp_priority_defer_until = 0.0
        self._mp_follow_self_heal_until = 0.0
        self._last_mp_follow_self_heal_tick = 0.0
        self._mp_stationary_self_heal_threshold = 50000
        self._last_party_heal_blocked_log_time = 0.0
        self._death_recovery_heal_attempts = 6
        self._self_gg_retry_cooldown_until = 0.0
        self._self_bm_retry_cooldown_until = 0.0
        self._last_self_support_buff_attempt_time = 0.0
        self._self_support_buff_min_interval = 6.0
        self._party_direct_heal_key = "3"
        self._party_direct_heal_verified = False
        self._party_direct_heal_target_prepared = False
        self._party_heal_blocked_until = 0.0
        self._direct_heal_prepare_requested = False
        self._party_direct_heal_fail_count = 0
        self._warrior_redtab_verified = False
        self._warrior_redtab_verified_hp = 0
        self.state.support_input_blocked_until = 0.0
        self._last_warrior_debuff_time = 0.0
        self._warrior_debuff_interval_min = 10.0
        self._warrior_debuff_interval_max = 16.0
        self._warrior_debuff_next_interval = random.uniform(
            self._warrior_debuff_interval_min,
            self._warrior_debuff_interval_max,
        )
        self._warrior_debuff_active = False
        self._post_debuff_follow_until = 0.0
        self._post_debuff_recover_prepare_pending = False
        self._follow_pause_until = 0.0
        self._self_cooltime_refresh_timeout = 0.22
        self._self_gg_verify_timeout = 0.18
        self._self_gg_retry_cooldown_sec = 0.08
        self._last_box_collision_recover_time = 0.0
        self._support_phase = "idle"
        self._support_phase_until = 0.0
        self._support_party_heal_follow_guard_until = 0.0
        self._support_follow_hold_distance = 1
        self._support_follow_resume_distance = 2
        self._warrior_heewon_interval = 16.0
        self._last_warrior_heewon_time = 0.0
        self._warrior_heewoncheom_interval = 25.0
        self._last_warrior_heewoncheom_time = 0.0
        self._periodic_support_min_mp = 50000
        self._service_last_warrior_coord: tuple[int, int] | None = None
        self._service_last_warrior_dir: str | None = None
        self._service_last_warrior_step_dir: str | None = None
        self._service_last_warrior_map_sig: tuple[str, str, str, str] | None = None
        self._service_warrior_trail: list[tuple[int, int, str | None]] = []
        self._service_last_map_info_change_seq = 0
        self._service_last_coord_transition_seq = 0
        self._service_portal_session_cache: dict[tuple[str, str, str, str], dict[str, object]] = {}
        self._last_portal_immediate_clear_time = 0.0
        self._portal_support_pause_until = 0.0
        self._sulsa_hellfire_interval = 10.5
        self._sulsa_paralyze_interval = 19.0
        self._sulsa_curse_interval = 600.0
        self._sulsa_last_hellfire_time = 0.0
        self._sulsa_last_paralyze_time = 0.0
        self._sulsa_last_curse_time = 0.0
        self._sulsa_last_action_log_time = 0.0

    # ----------------------------------------------------------

    # 지원 및 리커버리 관련: 회복/지원 관련 보조 메서드들
    # ----------------------------------------------------------
    def _execute_v3_heal(self, skill):
        """
        회복 실행: RecoveryManager를 통해 실행
        """
        self.recovery_manager.execute_v3_heal(skill)

    def _press_fast(self, key: str, variance: float = 0.15):
        """
        회복 단축키 입력: SpellCaster를 통해 실행
        """
        self.recovery_manager.caster._press_fast(key, variance)

    def _resolve_recovery_key(self, selected: str, default_key: str):
        """
        GUI에서 선택된 단축키로 복구키 매핑: RecoveryManager를 통해 실행
        """
        return self.recovery_manager.resolve_recovery_key(selected, default_key)

    def _clear_nav_context(self):
        """Pause/Resume 명령 시 LogicSvc 현재 네비게이션 컨텍스트 초기화"""
        self._current_nav_context = None

    def _get_visible_monsters(self) -> list[dict]:
        """
        화면에 보이는 몬스터 리스트 반환
        
        Purpose:
            - 현재 화면에 인식된 몬스터 리스트 얻기
            - 타겟팅/탐색 로직에서 사용
            - 유효한 엔티티만 반환
        
        Returns:
            list[dict]: 몬스터 엔티티 정보 리스트 (없으면 빈 리스트)
        
        Integration:
            - svc_monitor.py: OCR을 통해 인식된 몬스터 정보
            - GameState.entities: 게임 상태에서 몬스터 정보 제공
        """
        monsters = self.state.entities.get("monsters", [])
        return monsters if isinstance(monsters, list) else []

    def _set_support_phase(self, phase: str, duration: float = 0.0):
        self._support_phase = str(phase or "idle")
        self._support_phase_until = time.time() + max(0.0, float(duration or 0.0))

    def _clear_support_phase(self, expected: str | None = None):
        if expected and str(getattr(self, "_support_phase", "idle")) != str(expected):
            return
        self._support_phase = "idle"
        self._support_phase_until = 0.0

    def _is_support_phase_active(self, phase: str | None = None) -> bool:
        current_phase = str(getattr(self, "_support_phase", "idle") or "idle")
        if phase and current_phase != str(phase):
            return False
        if current_phase == "idle":
            return False
        return time.time() < float(getattr(self, "_support_phase_until", 0.0) or 0.0)

    def _mark_support_party_heal_follow_guard(self, duration: float = 0.8):
        self._support_party_heal_follow_guard_until = time.time() + max(0.1, float(duration or 0.0))

    def _is_support_follow_guard_active(self) -> bool:
        if self._is_support_phase_active("self_recover") or self._is_support_phase_active("retarget"):
            return True
        return time.time() < float(getattr(self, "_support_party_heal_follow_guard_until", 0.0) or 0.0)

    def _pause_follow_for_action(self, duration: float = 1.0):
        self._follow_pause_until = max(
            float(getattr(self, "_follow_pause_until", 0.0) or 0.0),
            time.time() + max(0.1, float(duration or 0.0)),
        )
        self.state.nav_follow_enabled = False

    def _restore_follow_after_action(self, previous_follow: bool):
        self._follow_pause_until = 0.0
        if bool(previous_follow) and bool(getattr(self.state, "service_active", False)):
            self.state.nav_follow_enabled = True

    def _is_party_heal_blocked(self) -> bool:
        return should_block_party_heal(
            self._is_support_phase_active("retarget"),
            time.time(),
            float(getattr(self, "_party_heal_blocked_until", 0.0) or 0.0),
        )

    def _should_hold_follow_at_gap(self, gap: int) -> bool:
        hold_distance = 1
        if self._is_support_follow_guard_active():
            hold_distance = int(self._support_follow_hold_distance)
        return should_hold_follow_gap(gap, hold_distance=hold_distance)

    def _find_spell_by_name(self, spell_name: str):
        wanted = str(spell_name or "").strip()
        if not wanted:
            return None
        return next((s for s in self.state.spells if str(getattr(s, "name", "") or "").strip() == wanted), None)

    def _get_good_hp_threshold(self) -> int:
        """
        HP 회복 기준값 반환
        
        Purpose:
            - GUI에서 설정한 HP 회복 기준값 얻기
            - 자기자신/파티 회복 로직에서 사용
            - 예외 처리로 기본값 반환
        
        Returns:
            int: HP 회복 기준값(최소값 100000)
        
        Integration:
            - svc_worker.py: GUI에서 설정한 good_hp 값
            - _needs_hp_recovery(): 기준값 판별에 사용
        """
        try:
            return max(100000, int(getattr(self.state, "good_hp", 100000) or 100000))
        except Exception:
            return 100000

    def _get_good_mp_threshold(self) -> int:
        """
        MP 회복 기준값 반환
        
        Purpose:
            - GUI에서 설정한 MP 회복 기준값 얻기
            - 자기자신/파티 회복 로직에서 사용
            - 예외 처리로 기본값 반환
        
        Returns:
            int: MP 회복 기준값(기본값 0)
        
        Integration:
            - svc_worker.py: GUI에서 설정한 good_mp 값
            - _needs_mp_recovery(): 기준값 판별에 사용
        """
        try:
            return max(0, int(getattr(self.state, "good_mp", 0) or 0))
        except Exception:
            return 0

    def _needs_hp_recovery(self) -> bool:
        """
        자기자신 HP 회복 필요 여부 판별
        
        Purpose:
            - 현재 HP 상태 기반으로 회복 필요 조건 판단
            - GUI 기준값과 트리거 상태 반영
            - rate-limiting 등으로 불필요 회복 제한
        
        Algorithm:
            1. 현재 HP 값 확인(예외처리)
            2. GUI 설정 기준값 판별
            3. HP 트리거 상태 확인
        
        Returns:
            bool: HP 회복 필요하면 True
        
        Integration:
            - _recover_hp(): 실제 회복 로직 실행 전
            - _recover_party_hp(): 파티원 회복 전
        """
        current_hp = max(0, int(getattr(self.state, "hp", 0) or 0))
        good_hp = self._get_good_hp_threshold()
        if good_hp > 0 and current_hp < good_hp:
            return True
        return bool(getattr(self.state, "hp_trig_active", False))

    def _needs_mp_recovery(self) -> bool:
        """
        자기자신 MP 회복 필요 여부 판별
        
        Purpose:
            - 현재 MP 상태 기반으로 회복 필요 조건 판단
            - GUI 기준값과 트리거 상태 반영
            - rate-limiting 등으로 불필요 회복 제한
        
        Algorithm:
            1. 현재 MP 값 확인(예외처리)
            2. 우선 자기자신 MP 우선 회복 조건 판단
            3. GUI 기준값 판별
            4. MP 트리거 상태 확인
        
        Returns:
            bool: MP 회복 필요하면 True
        
        Integration:
            - _recover_mp(): 실제 회복 로직 실행 전
            - _recover_party_mp(): 파티원 회복 전
        """
        current_mp = max(0, int(getattr(self.state, "mp", 0) or 0))
        if should_prioritize_self_mp(current_mp, self._self_mp_priority_threshold):
            return True
        good_mp = self._get_good_mp_threshold()
        if good_mp > 0 and current_mp < good_mp:
            return True
        return bool(getattr(self.state, "mp_trig_active", False))

    def _get_support_target_data(self) -> dict | None:
        """도사가 지원해야 할 대상(아군)의 스냅샷 정보 반환 (격수 데이터 최우선)"""
        # role='격수'로 명시되고 freshness 조건(1.5초 이내)을 만족하는 원격 데이터만 사용
        remote = self.state.get_fresh_remote_data_by_role("격수")
        if not remote:
            return None
        
        # 격수 데이터 수신 여부 확인 및 로깅
        if remote and isinstance(remote, dict):
            hp = remote.get("hp", 0)
            mp = remote.get("mp", 0)
            heal_request = remote.get("heal_request", False)
            mp_request = remote.get("mp_request", False)
            
            # 신호 수신 로깅 (10초에 한 번 출력)
            current_time = time.time()
            last_log_time = getattr(self.state, 'last_signal_log_time', 0)
            if current_time - last_log_time > 10:
                good_hp = remote.get("good_hp", 0)
                good_mp = remote.get("good_mp", 0)
                red_tab_enabled = remote.get("red_tab_enabled", False)
                print(f"[Signal] 격수 데이터 수신 - HP: {hp}, MP: {mp}, 힐요청: {heal_request}, MP요청: {mp_request}")
                print(f"[Signal] HP기준값: {good_hp}, MP기준값: {good_mp}, red_tab: {red_tab_enabled}")
                print(f"[Signal] 요청 계산: good_hp>0={good_hp>0}, hp>0={hp>0}, hp<=good_hp={hp<=good_hp}")
                self.state.last_signal_log_time = current_time
        else:
            current_time = time.time()
            last_log_time = getattr(self.state, 'last_signal_log_time', 0)
            if current_time - last_log_time > 10:
                print("[Signal] 격수 데이터 미수신 - other_pc_data 비어있음")
                self.state.last_signal_log_time = current_time
        
        if not isinstance(remote, dict) or not remote:
            return None

        # 비정상 텔레메트리(좌표/수치 깨짐)는 지원/추적에 사용하지 않는다.
        try:
            rx = int(remote.get("x", remote.get("pos_x", 0)) or 0)
            ry = int(remote.get("y", remote.get("pos_y", 0)) or 0)
            hp = int(remote.get("hp", 0) or 0)
            mp = int(remote.get("mp", 0) or 0)
            if abs(rx) > 300 or abs(ry) > 300 or hp < 0 or hp > 3000000 or mp < 0 or mp > 3000000:
                now = time.time()
                last_bad = float(getattr(self.state, "last_bad_signal_log_time", 0.0) or 0.0)
                if now - last_bad >= 2.0:
                    print(f"[Signal] invalid telemetry ignored: x={rx}, y={ry}, hp={hp}, mp={mp}")
                    self.state.last_bad_signal_log_time = now
                return None
        except Exception:
            return None
        return remote

    def _pace_service_loop(self, mode: str = "idle"):
        if mode == "active":
            humanized_sleep(self._service_loop_active_sleep, variance=0.06)
        elif mode == "no_target":
            humanized_sleep(self._service_loop_no_target_sleep, variance=0.08)
        else:
            humanized_sleep(self._service_loop_idle_sleep, variance=0.08)

    def _set_support_input_block(self, duration: float):
        until = time.time() + max(0.0, float(duration))
        current = float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0)
        if until > current:
            self.state.support_input_blocked_until = until

    def _is_support_input_block_active(self) -> bool:
        return time.time() < float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0)

    def _stop_support_movement_inputs(self):
        try:
            hw.stop_all_inputs(repeat=1, delay=0.02)
        except Exception:
            pass

    def _release_movement_keys_only(self):
        for direction in ("up", "down", "left", "right"):
            try:
                hw.release_key(direction)
            except Exception:
                pass

    def _cast_self_hp_micro_follow_tick(self) -> bool:
        """이동형 자힐 1틱: 방향키만 짧게 끊고 3>home>enter 후 follow를 즉시 재개한다."""
        if not self._is_hw_ready() or not self._is_game_window_active():
            return False
        key, skill = self.recovery_manager.resolve_recovery_key(
            getattr(self.state, "recovery_hp_spell", ""),
            default_key="3",
        )
        if skill is not None and hasattr(skill, "is_ready") and not skill.is_ready():
            return False

        now = time.time()
        try:
            self.recovery_manager._last_hp_recover_time = now
        except Exception:
            pass

        previous_block = float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0)
        self.state.support_input_blocked_until = max(previous_block, time.time() + 0.16)
        self._release_movement_keys_only()
        for press_key in (key, "home", "enter"):
            if not self._press_hw_key(str(press_key), variance=0.06, skip_focus_guard=True):
                return False
            self._sleep_ui_gap(0.018)
        if skill is not None:
            try:
                skill.last_cast_time = now
            except Exception:
                pass
        self.state.support_input_blocked_until = min(
            float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0),
            time.time() + 0.02,
        )
        print(f"[Recovery] self HP micro-follow tick: key={key}")
        return True

    def _mark_self_status_scan_window(self, duration: float | None = None):
        window = float(duration if duration is not None else self._self_status_scan_window_sec)
        until = time.time() + max(0.25, window)
        self.state.self_status_scan_active = True
        self.state.self_status_scan_until = until

    def _update_zero_hp_state(self) -> bool:
        current_hp = int(getattr(self.state, "hp", 0) or 0)
        self._zero_hp_read_count = next_zero_hp_count(self._zero_hp_read_count, current_hp)
        return is_confirmed_zero_hp_state(self._zero_hp_read_count, self._zero_hp_confirm_required)

    def _should_prioritize_self_mp(self) -> bool:
        current_mp = int(getattr(self.state, "mp", 0) or 0)
        return should_prioritize_self_mp(current_mp, self._self_mp_priority_threshold)

    def _handle_zero_hp_recovery(self) -> bool:
        if self._death_recovery_active:
            return True
        if not self._is_hw_ready() or not self._is_game_window_active():
            return False

        print("[Recovery] self zero-HP confirmed. Stop support and run revive sequence.")
        self._death_recovery_active = True
        self._set_support_input_block(2.0)
        self._stop_support_movement_inputs()
        self._set_combat_busy(True)
        self.state.last_self_buff_time = 0.0
        self.state.last_self_gg_cast_time = 0.0

        try:
            for key in ("esc", "5", "home", "enter"):
                if not self._press_hw_key(key, variance=0.10, skip_focus_guard=True):
                    return False
                self._sleep_ui_gap(0.12 if key != "enter" else 0.20)

            for _ in range(self._death_recovery_heal_attempts):
                for key in ("3", "home", "enter"):
                    if not self._press_hw_key(key, variance=0.10, skip_focus_guard=True):
                        return False
                    self._sleep_ui_gap(0.10 if key != "enter" else 0.16)
                if int(getattr(self.state, "hp", 0) or 0) > self._self_hp_emergency_threshold:
                    break

            self._mark_self_status_scan_window()
            if self._press_hw_key("s", variance=0.10, skip_focus_guard=True):
                self.state.last_self_status_open_time = time.time()
                self._sleep_ui_gap(0.16)
                self._wait_for_user_pattern(self._support_self_pattern, timeout=0.80)

            self._refresh_self_cooltime_view("[Recovery] post-revive status refresh.")
            self._execute_self_geumgang_cycle(skip_refresh=True, force_cast=True)
            self._execute_self_bomu_cycle(skip_refresh=True, force_cast=True)
            if self.state.role == "도사" and self.state.service_active:
                self.request_initial_direct_heal_target_prepare()
            self._zero_hp_read_count = 0
            print("[Recovery] self revive sequence complete. Resume follow/service.")
            return True
        finally:
            self._death_recovery_active = False
            self._set_combat_busy(False)

    def _handle_self_hp_emergency(self) -> bool:
        current_hp = int(getattr(self.state, "hp", 0) or 0)
        if not should_trigger_self_hp_emergency(current_hp, self._self_hp_emergency_threshold):
            return False

        now = time.time()
        if (now - self._last_self_emergency_hp_time) < self._self_emergency_hp_cooldown:
            return False

        print(f"[Recovery] self HP emergency: hp={current_hp} <= {self._self_hp_emergency_threshold}")
        self._last_self_emergency_hp_time = now
        self._invalidate_warrior_redtab_verification()
        self._direct_heal_prepare_requested = False
        recovered = self._recover_self_hp_until_good_hp(
            reason_log="[Recovery] self emergency sustain recovery.",
            support_block_duration=2.4,
            max_attempts=8,
        )
        if recovered:
            self._complete_self_hp_recovery_reengage(
                source_log="[Recovery] self emergency recovery complete."
            )
        elif is_support_role(getattr(self.state, "network_role", "") or self.state.role) and self.state.service_active:
            self.request_initial_direct_heal_target_prepare()
        return True

    def _recover_self_hp_until_good_hp(
        self,
        reason_log: str,
        support_block_duration: float = 2.0,
        max_attempts: int = 6,
    ) -> bool:
        self._set_support_phase("self_recover", duration=max(1.2, float(support_block_duration or 0.0) + 0.8))
        self._set_support_input_block(max(0.8, float(support_block_duration or 0.0)))
        self._stop_support_movement_inputs()
        self._set_combat_busy(True)
        try:
            attempts = 0
            while attempts < max(1, int(max_attempts)):
                current_hp = int(getattr(self.state, "hp", 0) or 0)
                good_hp = self._get_good_hp_threshold()
                if not should_continue_self_hp_recovery(
                    current_hp,
                    good_hp,
                    self._self_hp_emergency_threshold,
                ):
                    return current_hp > 0
                self.recovery_manager.execute_self_hp_recovery()
                attempts += 1

            final_hp = int(getattr(self.state, "hp", 0) or 0)
            target_hp = max(
                int(self._self_hp_emergency_threshold) + 7000,
                int(self._get_good_hp_threshold() or 0),
            )
            if final_hp < target_hp:
                print(
                    f"{reason_log} hp={final_hp} target={target_hp} "
                    f"attempts={attempts}"
                )
            return final_hp > 0 and final_hp >= target_hp
        finally:
            self._clear_support_phase(expected="self_recover")
            self._set_combat_busy(False)

    def _cast_emergency_gg_hold(self, hold_duration: float = 1.5) -> bool:
        if not self._is_hw_ready() or not self._is_game_window_active():
            return False
        if not self._clear_red_tab_for_self_cast():
            return False

        base_hold = max(1.35, float(hold_duration or 0.0))
        for attempt in range(2):
            actual_hold = max(1.35, min(1.75, random.uniform(base_hold * 0.97, base_hold * 1.10)))
            print(
                "[Recovery] self emergency GG hold cast: "
                f"attempt={attempt + 1} hold={actual_hold:.2f}s"
            )
            try:
                hw.press_key("0")
                humanized_sleep(actual_hold, variance=0.08)
            finally:
                try:
                    hw.release_key("0")
                except Exception:
                    pass
            self.state.last_self_gg_cast_time = time.time()
            humanized_sleep(0.18, variance=0.10)
        return True

    def _complete_self_hp_recovery_reengage(self, source_log: str) -> bool:
        print(source_log)
        moving_follow = bool(
            getattr(self.state, "nav_follow_enabled", False)
            and getattr(self.state, "service_active", False)
        )
        self._set_support_phase("retarget", duration=2.0 if moving_follow else 3.0)
        self._set_support_input_block(support_retarget_block_duration(moving_follow))
        if not moving_follow:
            self._stop_support_movement_inputs()
        self._invalidate_warrior_redtab_verification()
        self._party_direct_heal_target_prepared = False
        self._party_heal_blocked_until = time.time() + (1.4 if moving_follow else 3.0)
        previous_busy = bool(getattr(self.state, "is_combat_busy", False))
        if not moving_follow:
            self._set_combat_busy(True)
        try:
            self._cast_emergency_gg_hold(hold_duration=1.5)
            humanized_sleep(0.025, variance=0.08)
            if moving_follow:
                self._set_support_input_block(support_retarget_block_duration(True))
                self._release_movement_keys_only()
            red_tab_reacquired = self._reacquire_warrior_red_tab_after_emergency(moving_follow=moving_follow)
            if is_support_role(getattr(self.state, "network_role", "") or self.state.role) and self.state.service_active and not red_tab_reacquired:
                if moving_follow:
                    self._party_heal_blocked_until = time.time() + 0.15
                    print("[Recovery] red_tab 재확인 실패: 추적은 계속하고 격수 회복 입력은 잠시 보류합니다.")
                else:
                    print("[Recovery] red_tab 재확인 실패. 직접 대상 준비를 다시 요청합니다.")
                    self.request_initial_direct_heal_target_prepare()
            return red_tab_reacquired
        finally:
            self._clear_support_phase(expected="retarget")
            if not moving_follow:
                self._set_combat_busy(previous_busy)

    def _reacquire_warrior_red_tab_after_emergency(self, moving_follow: bool = False) -> bool:
        if not self._is_hw_ready() or not self._is_game_window_active():
            return False

        self._invalidate_warrior_redtab_verification()
        self._party_direct_heal_target_prepared = False
        self._set_support_input_block(0.75 if moving_follow else support_retarget_block_duration(False))
        self.state.support_targeting_active = True
        if not moving_follow:
            self._stop_support_movement_inputs()
        humanized_sleep(0.025, variance=0.08)
        for attempt in range(3):
            wait_between_tabs = random.uniform(
                0.075,
                0.105,
            )
            print(
                "[Recovery] reacquire warrior red_tab: "
                f"attempt={attempt + 1} ESC -> TAB ({wait_between_tabs:.2f}s) -> TAB"
            )
            self.state.red_tab_promotion_active = True
            self.state.red_tab_promotion_until = time.time() + 2.0
            try:
                if not self._press_hw_key("esc", variance=0.10, skip_focus_guard=True):
                    continue
                humanized_sleep(0.025, variance=0.08)
                if bool(getattr(self.state, "red_tab_enabled", False)):
                    if not self._press_hw_key("esc", variance=0.10, skip_focus_guard=True):
                        continue
                    humanized_sleep(0.030, variance=0.08)
                if not self._press_hw_key("tab", variance=0.10, skip_focus_guard=True):
                    continue
                humanized_sleep(wait_between_tabs, variance=0.08)
                if not self._press_hw_key("tab", variance=0.10, skip_focus_guard=True):
                    continue
                if self._wait_for_red_tab_lock(timeout=0.28, min_hits=1):
                    self._party_direct_heal_target_prepared = True
                    self._party_heal_blocked_until = time.time() + 0.03
                    self._last_party_hp_support_time = 0.0
                    print("[Recovery] warrior red_tab confirmed after TAB>TAB.")
                    self.state.support_targeting_active = False
                    if moving_follow:
                        self.state.support_input_blocked_until = min(
                            float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0),
                            time.time() + 0.02,
                        )
                    return True
                self._cancel_ntab_selection()
            finally:
                self.state.red_tab_promotion_active = False
            humanized_sleep(speed_up_delay(0.24, factor=3.0), variance=0.10)
        self._party_direct_heal_target_prepared = False
        self.state.support_targeting_active = False
        if moving_follow:
            self.state.support_input_blocked_until = min(
                float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0),
                time.time() + 0.02,
            )
        return False

    def _handle_self_mp_priority(self, support_target: dict | None = None) -> bool:
        trigger_mp = int(getattr(self.state, "mp", 0) or 0)
        if not should_prioritize_self_mp(trigger_mp, self._self_mp_priority_threshold):
            return False

        if time.time() < float(getattr(self, "_self_mp_priority_failed_until", 0.0) or 0.0):
            return False
        if time.time() < float(getattr(self, "_self_mp_priority_defer_until", 0.0) or 0.0):
            return False

        now = time.time()
        if (now - self._last_self_mp_priority_time) < self._self_mp_priority_interval:
            return False

        self._last_self_mp_priority_time = now
        print(
            f"[Recovery] self MP priority: mp={trigger_mp} "
            f"<= {self._self_mp_priority_threshold}"
        )
        self._execute_self_mp_recovery_and_retarget(
            support_target,
            source_log="[Recovery] self MP priority complete.",
            trigger_mp=trigger_mp,
        )
        return True

    def _execute_self_mp_recovery_and_retarget(
        self,
        support_target: dict | None = None,
        source_log: str = "[Recovery] self MP recovery complete.",
        trigger_mp: int | None = None,
    ) -> bool:
        self._set_support_phase("mp_recover", duration=0.45)
        try:
            before_mp = max(0, int(getattr(self.state, "mp", 0) or 0))
            before_hp = max(0, int(getattr(self.state, "hp", 0) or 0))
            casted_mp = self._cast_self_mp_recovery_fast(trigger_mp=trigger_mp)
            humanized_sleep(0.06, variance=0.10)
            after_mp = max(0, int(getattr(self.state, "mp", 0) or 0))
            after_hp = max(0, int(getattr(self.state, "hp", 0) or 0))
            if not casted_mp:
                self._self_mp_priority_failed_until = time.time() + 0.35
                print(
                    f"[Recovery] self MP boost skipped: before={before_mp}, after={after_mp}. "
                    "Allow warrior heal before retry."
                )
            else:
                self._self_mp_priority_defer_until = time.time() + 0.35
                good_hp = self._get_good_hp_threshold()
                print(
                    "[Recovery] self MP boost result: "
                    f"mp={before_mp}->{after_mp}, hp={before_hp}->{after_hp}, good_hp={good_hp}"
                )
                if after_hp > 0 and after_hp <= int(getattr(self, "_mp_stationary_self_heal_threshold", 50000) or 50000):
                    self._mp_follow_self_heal_until = 0.0
                    print(
                        "[Recovery] post-MP critical HP. "
                        f"Stop follow briefly and recover self hp={after_hp}/{good_hp}."
                    )
                    recovered = self._recover_self_hp_until_good_hp(
                        reason_log="[Recovery] post-MP critical self HP recovery.",
                        support_block_duration=2.4,
                        max_attempts=14,
                    )
                    if recovered:
                        self._complete_self_hp_recovery_reengage(
                            source_log="[Recovery] post-MP critical self HP recovery complete."
                        )
                    return True
                if after_hp > 0 and after_hp <= good_hp:
                    self._mp_follow_self_heal_until = time.time() + 0.35
                    self._set_support_input_block(support_retarget_block_duration(True))
                    self._release_movement_keys_only()
                    red_tab_reacquired = self._reacquire_warrior_red_tab_after_emergency(moving_follow=True)
                    if not red_tab_reacquired:
                        self._cast_self_hp_micro_follow_tick()
                        self._party_heal_blocked_until = time.time() + 0.15
                    print("[Recovery] post-MP self HP recovery re-synced. Follow remains active.")
                    return True
            if self.state.role != "도사" or not bool(getattr(self.state, "service_active", False)):
                return True
            if support_target is None:
                support_target = self._get_support_target_data()
            if not support_target:
                print(f"{source_log} no warrior telemetry. Skip retarget in moving MP flow.")
                return True
            # Moving MP flow intentionally skips immediate ESC>TAB>TAB.
            # Retarget only when the next warrior-heal path actually needs it.
            self._party_direct_heal_target_prepared = False
            self._party_direct_heal_verified = False
            print(f"{source_log} moving flow complete. Retarget deferred until warrior heal.")
            return True
        finally:
            self._clear_support_phase(expected="mp_recover")

    def _cast_self_mp_recovery_fast(self, trigger_mp: int | None = None) -> bool:
        current_mp = max(0, int(getattr(self.state, "mp", 0) or 0))
        good_mp = max(0, int(getattr(self.state, "good_mp", 0) or 0))
        effective_mp = current_mp
        if trigger_mp is not None:
            try:
                trigger_mp_int = max(0, int(trigger_mp))
            except Exception:
                trigger_mp_int = current_mp
            if trigger_mp_int <= self._self_mp_priority_threshold:
                effective_mp = trigger_mp_int
        if current_mp < 30 and trigger_mp is None:
            print(f"[Recovery] self MP boost skipped: MP={current_mp} (<30)")
            return False
        if effective_mp > self._self_mp_priority_threshold and (good_mp <= 0 or current_mp >= good_mp):
            return False

        key, skill = self.recovery_manager.resolve_recovery_key(
            getattr(self.state, "recovery_mp_spell", ""),
            default_key="2",
        )
        if skill is not None and hasattr(skill, "is_ready") and not skill.is_ready():
            print(f"[Recovery] self MP boost skipped: cooldown key={key}")
            return False

        now = time.time()
        try:
            self.recovery_manager._last_mp_recover_time = now
        except Exception:
            pass

        self._press_hw_key("esc", variance=0.10, skip_focus_guard=True)
        self._sleep_ui_gap(0.035)
        cast_function = self.recovery_manager.resolve_recovery_cast_function(
            getattr(self.state, "recovery_mp_spell", ""),
            default_func="SpellEnter",
        )
        self.recovery_manager.cast_recovery_sequence(key, cast_function)
        if skill is not None:
            try:
                skill.last_cast_time = now
            except Exception:
                pass
        print(f"[Recovery] self MP boost fast cast: key={key}, mp={current_mp}, trigger_mp={trigger_mp}")
        return True

    def _needs_hp_recovery_for(self, snapshot: dict | None) -> bool:
        if not snapshot:
            return self._needs_hp_recovery()
        # ?먯슂泥??곹깭? 臾닿??섍쾶 吏곸젒 HP ?꾧퀎媛믪쑝濡??먮떒
        current_hp = max(0, int(snapshot.get("hp", 0) or 0))
        snap_good_hp = int(snapshot.get("good_hp", 0) or 0)
        
        # [FIX] ?듭떊 ?ㅻ쪟???ㅼ젙 ?꾨씫 ??寃⑹닔 泥대젰 湲곗? ?섎뱶肄붾뵫 (800000)
        good_hp = snap_good_hp if snap_good_hp > 0 else 800000
        
        # HP媛 good_hp ?댄븯?대㈃ ??긽 ?뚮났 ?ㅽ뻾 (?먯슂泥?臾댁떆)
        if good_hp > 0 and current_hp <= good_hp:
            return True
        # 鍮꾩긽 ?곹솴: HP媛 10000 ?댄븯?대㈃ ??긽 ?뚮났
        return current_hp <= 10000

    def _get_party_good_hp_threshold(self, snapshot: dict | None) -> int:
        if not snapshot:
            return 800000
        try:
            snap_good_hp = int(snapshot.get("good_hp", 0) or 0)
        except Exception:
            snap_good_hp = 0
        return snap_good_hp if snap_good_hp > 0 else 800000

    def _needs_mp_recovery_for(self, snapshot: dict | None) -> bool:
        if not snapshot:
            return self._needs_mp_recovery()
        # MP?붿껌 ?곹깭? 臾닿??섍쾶 吏곸젒 MP ?꾧퀎媛믪쑝濡??먮떒
        current_mp = max(0, int(snapshot.get("mp", 0) or 0))
        snap_good_mp = int(snapshot.get("good_mp", 0) or 0)
        
        # [FIX] ?듭떊 ?ㅻ쪟???ㅼ젙 ?꾨씫 ??寃⑹닔 留덈굹 湲곗? ?섎뱶肄붾뵫 (30000)
        good_mp = snap_good_mp if snap_good_mp > 0 else 30000
        
        # MP媛 good_mp ?댄븯?대㈃ ?뚮났 ?ㅽ뻾
        if good_mp > 0 and current_mp <= good_mp:
            return True
        return current_mp <= 5000

    def _clear_target_state(self, reset_combat_timer: bool = True):
        """
        ?寃??곹깭 珥덇린??
        
        Purpose:
            - ?寃??좉툑 ?댁젣 諛??대쫫 珥덇린??
            - ?꾪닾 ??대㉧ 由ъ뀑 ?듭뀡
            - ?ㅼ쓬 ?寃??먯깋 以鍮?
            
        Args:
            reset_combat_timer (bool): ?꾪닾 ?쒖옉 ?쒓컙 由ъ뀑 ?щ?
            
        Effects:
            - state.target_locked: False濡??ㅼ젙
            - state.target_name: 鍮?臾몄옄?대줈 ?ㅼ젙
            - state.combat_start_time: 0.0?쇰줈 由ъ뀑 (?좏깮??
            
        Integration:
            - _search_target_v3(): ?寃??먯깋 ???몄텧
            - _validate_target(): ?寃?寃利??ㅽ뙣 ???몄텧
        """
        self.state.target_locked = False
        self.state.target_name = ""
        if reset_combat_timer:
            self.state.combat_start_time = 0.0

    def _set_combat_busy(self, active: bool):
        """
        ?꾪닾 ?곹깭 ?ㅼ젙
        
        Purpose:
            - ?꾪닾 以??곹깭 異붿쟻 諛?愿由?
            - NavigationThread????숈옉 ?쒖뼱
            - ?꾪닾 醫낅즺 ????대㉧ ?뺣━
            
        Args:
            active (bool): ?꾪닾 ?곹깭 ?ㅼ젙媛?
            
        Effects:
            - state.is_combat_busy: ?꾪닾 以??щ? ?ㅼ젙
            - ?꾪닾 醫낅즺 ???寃??놁쑝硫???대㉧ 由ъ뀑
            
        Integration:
            - _should_handle_combat(): ?꾪닾 ?꾩슂???먮떒
            - NavigationThread: ?꾪닾 以??대룞 以묒?
        """
        self.state.is_combat_busy = bool(active)
        if not active and not self.state.target_locked:
            self.state.combat_start_time = 0.0

    def _should_handle_combat(self) -> bool:
        """
        ?꾪닾 吏꾩엯 ?꾩슂???먮떒 (?대룞怨??꾪닾 遺꾨━)
        
        Purpose:
            - ?붾㈃ ?곹깭 湲곕컲?쇰줈 ?꾪닾/?대룞 ?곗꽑?쒖쐞 寃곗젙
            - OCR ?묐떟 ?湲??쒓컙 愿由?
            - 遺덊븘?뷀븳 ?꾪닾 ?곹깭 諛⑹?
            
        Algorithm:
            1. ?붾㈃ 紐ъ뒪??議댁옱 ?뺤씤
            2. ?寃??좉툑 ?곹깭 ?뺤씤
            3. OCR ?묐떟 ?湲??쒓컙 ?곸슜 (grace period)
            4. ?좎궗 ?뱀닔 濡쒖쭅 ?곸슜
            
        Returns:
            bool: ?꾪닾 泥섎━ ?꾩슂?섎㈃ True
            
        Integration:
            - _handle_combat(): ?ㅼ젣 ?꾪닾 濡쒖쭅 ?몄텧
            - NavigationThread: ?꾪닾 以??대룞 以묒?
        """
        monsters = self._get_visible_monsters()
        now = time.time()
        if monsters:
            self._last_monster_seen_time = now

        if should_ignore_monster_combat_for_support_autohunt(
            getattr(self.state, "role", ""),
            getattr(self.state, "service_active", False),
            getattr(self.state, "nav_follow_enabled", False),
        ):
            return False

        if self.state.target_locked:
            acquire_grace = max(0.30, float(TIMING_CONFIG.get("ocr_wait", 0.10)) * 3.0)
            if self.state.target_name:
                return True
            if monsters and (now - self._last_target_search_time) <= max(acquire_grace, 0.50):
                return True
            if (now - self._last_target_search_time) <= acquire_grace:
                return True
            self._clear_target_state()
            return False

        if self.state.role == "술사":
            _me_grid, close_infos, _dense_count = self._get_priest_engage_metrics()
            return bool(close_infos)

        return bool(monsters)
        monsters = self._get_visible_monsters()
        now = time.time()
        if monsters:
            self._last_monster_seen_time = now

        if self.state.target_locked:
            acquire_grace = max(0.30, float(TIMING_CONFIG.get("ocr_wait", 0.10)) * 3.0)
            if self.state.target_name:
                return True
            if monsters and (now - self._last_target_search_time) <= max(acquire_grace, 0.50):
                return True
            if (now - self._last_target_search_time) <= acquire_grace:
                return True
            self._clear_target_state()
            return False

        if self.state.role == "술사":
            _me_grid, close_infos, _dense_count = self._get_priest_engage_metrics()
            return bool(close_infos)

        return bool(monsters)

    def _recover_hp(self):
        if not self._needs_hp_recovery():
            return
        self.recovery_manager.execute_self_hp_recovery()

    def _recover_mp(self):
        if not self._needs_mp_recovery():
            return
        self.recovery_manager.execute_self_mp_recovery()

    def _recover_party_hp(self, snapshot: dict | None) -> bool:
        if self._is_portal_support_paused():
            return False
        if not snapshot or not self._needs_hp_recovery_for(snapshot):
            return False
        before_hp = max(0, int(snapshot.get("hp", 0) or 0))
        if self._is_party_heal_blocked():
            return False
        red_tab_confirmed = bool(getattr(self.state, "red_tab_enabled", False))
        if self._party_direct_heal_verified and should_allow_party_support_cast(
            red_tab_confirmed,
            self._party_direct_heal_target_prepared,
        ):
            if not self._cast_party_direct_heal(snapshot):
                return False
            self._last_party_hp_value = before_hp
            return True
        if self._party_direct_heal_verified:
            self._party_direct_heal_verified = False
            self._party_direct_heal_target_prepared = False

        if not self._party_direct_heal_target_prepared and not self._prepare_direct_tab_heal_target():
            now = time.time()
            if (now - float(self._last_warrior_redtab_skip_log_time or 0.0)) >= 0.8:
                print("[Support] HP heal skipped: direct target prepare failed.")
                self._last_warrior_redtab_skip_log_time = now
            return False
        if not self._party_direct_heal_target_prepared:
            print("[Support] direct heal target prepared: esc -> tab -> tab")
            self._party_direct_heal_target_prepared = True

        if not should_allow_party_support_cast(
            bool(getattr(self.state, "red_tab_enabled", False)),
            self._party_direct_heal_target_prepared,
        ):
            self._cancel_ntab_selection()
            self._party_direct_heal_target_prepared = False
            self._party_heal_blocked_until = time.time() + 0.12
            return False

        if not self._cast_party_direct_heal(snapshot):
            return False

        self._party_direct_heal_verified = True
        self._party_direct_heal_target_prepared = True
        self._party_direct_heal_fail_count = 0
        self._last_party_hp_value = before_hp
        return True

    def _roll_party_hp_tick_interval(self, rapid_heal: bool = False) -> float:
        if rapid_heal:
            low = float(self._party_hp_tick_fast_interval_min)
            high = float(self._party_hp_tick_fast_interval_max)
        else:
            low = float(self._party_hp_tick_base_interval_min)
            high = float(self._party_hp_tick_base_interval_max)
        if high < low:
            high = low
        return random.uniform(low, high)

    def _recover_party_mp(self, snapshot: dict | None) -> bool:
        if not snapshot or not self._needs_mp_recovery_for(snapshot):
            return False
        red_locked = bool(getattr(self.state, "red_tab_enabled", False))
        if not red_locked:
            self._invalidate_warrior_redtab_verification()
        if not should_allow_party_support_cast(red_locked):
            red_locked = self._ensure_warrior_red_tab_via_ntab(snapshot)
        if not should_allow_party_support_cast(red_locked):
            now = time.time()
            if (now - float(self._last_warrior_redtab_skip_log_time or 0.0)) >= 0.8:
                print("[Support] MP heal skipped: warrior red_tab not confirmed.")
                self._last_warrior_redtab_skip_log_time = now
            return False
        self.recovery_manager.execute_party_mp_recovery(snapshot, use_red_tab=True)
        return True

    def _invalidate_warrior_redtab_verification(self):
        self._warrior_redtab_verified = False
        self._warrior_redtab_verified_hp = 0
        self._party_direct_heal_verified = False
        self._party_direct_heal_target_prepared = False
        self._party_direct_heal_fail_count = 0

    def request_initial_direct_heal_target_prepare(self):
        self._invalidate_warrior_redtab_verification()
        moving_follow = bool(
            getattr(self.state, "nav_follow_enabled", False)
            and getattr(self.state, "service_active", False)
        )
        self._set_support_input_block(support_retarget_block_duration(moving_follow))
        self._direct_heal_prepare_requested = True

    def _handle_initial_direct_heal_prepare_request(self) -> bool:
        if not bool(getattr(self, "_direct_heal_prepare_requested", False)):
            return False
        current_hp = int(getattr(self.state, "hp", 0) or 0)
        if should_trigger_self_hp_emergency(current_hp, self._self_hp_emergency_threshold):
            return False
        self._direct_heal_prepare_requested = False
        self._invalidate_warrior_redtab_verification()
        moving_follow = bool(
            getattr(self.state, "nav_follow_enabled", False)
            and getattr(self.state, "service_active", False)
        )
        self._set_support_input_block(support_retarget_block_duration(moving_follow))
        if not moving_follow:
            self._stop_support_movement_inputs()
        if not self._prepare_direct_tab_heal_target():
            print("[Support] 자동사냥 초기 direct target prepare failed.")
            return True
        self._party_direct_heal_target_prepared = True
        self._last_party_hp_support_time = 0.0
        print("[Support] 자동사냥 초기 direct target prepared: esc -> tab -> tab")
        return True

    def _handle_box_collision_reprepare_request(self) -> bool:
        if not bool(getattr(self.state, "box_collision_reprepare_requested", False)):
            return False
        self.state.box_collision_reprepare_requested = False
        now = time.time()
        self._last_box_collision_recover_time = now
        self._invalidate_warrior_redtab_verification()
        self._set_support_input_block(0.75)
        self._stop_support_movement_inputs()
        print("[Support] stalled follow recover: ESC -> TAB -> TAB (movement locked).")
        try:
            self._prepare_direct_tab_heal_target()
        finally:
            self.state.support_targeting_active = False
        return True

    def _prepare_direct_tab_heal_target(self) -> bool:
        if not self._is_hw_ready():
            return False
        previous_busy = bool(getattr(self.state, "is_combat_busy", False))
        self._ntab_in_progress = True
        moving_follow = bool(
            getattr(self.state, "nav_follow_enabled", False)
            and getattr(self.state, "service_active", False)
        )
        self._set_support_input_block(0.75 if moving_follow else support_retarget_block_duration(False))
        if not moving_follow:
            self._stop_support_movement_inputs()
        self._support_lock_search_until = time.time() + 0.60
        self.state.red_tab_promotion_active = False
        self.state.red_tab_promotion_until = 0.0
        self.state.support_targeting_active = True
        self._set_combat_busy(True)
        try:
            for key, delay in (("esc", 0.06), ("tab", 0.08), ("tab", 0.08)):
                if not self._press_hw_key(key, variance=0.10):
                    return False
                self._sleep_ui_gap(delay)
            if self._wait_for_red_tab_lock(timeout=0.28, min_hits=1):
                self._party_direct_heal_target_prepared = True
                return True
            self._cancel_ntab_selection()
            return False
        finally:
            self._ntab_in_progress = False
            self.state.support_targeting_active = False
            if moving_follow:
                self.state.support_input_blocked_until = min(
                    float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0),
                    time.time() + 0.02,
                )
            self._support_lock_search_until = time.time() + 0.12
            self._set_combat_busy(previous_busy)

    def _cast_party_direct_heal(self, snapshot: dict | None = None) -> bool:
        if self._is_portal_support_paused():
            return False
        if not self._is_hw_ready():
            return False
        if not should_allow_party_support_cast(
            bool(getattr(self.state, "red_tab_enabled", False)),
            self._party_direct_heal_target_prepared,
        ):
            return False
        hp_val = int((snapshot or {}).get("hp", 0) or 0)
        now = time.time()
        if (now - float(getattr(self, "_last_party_direct_heal_log_time", 0.0) or 0.0)) >= 1.0:
            print(f"[Support] HP heal casting on warrior: key={self._party_direct_heal_key}, hp={hp_val}, direct_verified={self._party_direct_heal_verified}")
            self._last_party_direct_heal_log_time = now
        self.state.support_targeting_active = True
        try:
            if not self._press_hw_key(self._party_direct_heal_key, variance=0.10):
                return False
            self._last_party_hp_support_time = time.time()
            self._sleep_ui_gap(random.uniform(0.012, 0.030))
            return True
        finally:
            self.state.support_targeting_active = False

    def _cast_party_heewon(self, snapshot: dict | None = None) -> bool:
        if self._is_portal_support_paused():
            return False
        if not self._is_hw_ready():
            return False
        if not should_allow_party_support_cast(
            bool(getattr(self.state, "red_tab_enabled", False)),
            self._party_direct_heal_target_prepared,
        ):
            return False
        spell = self._find_spell_by_name("희원")
        if spell is not None and hasattr(spell, "is_ready") and not spell.is_ready():
            return False
        hp_val = int((snapshot or {}).get("hp", 0) or 0)
        print(f"[Support] Heewon casting on warrior: key=1, hp={hp_val}")
        self.state.support_targeting_active = True
        try:
            if not self._press_hw_key("1", variance=0.10):
                return False
            now = time.time()
            self._last_warrior_heewon_time = now
            if spell is not None:
                try:
                    spell.last_cast_time = now
                except Exception:
                    pass
            self._sleep_ui_gap(random.uniform(0.018, 0.036))
            return True
        finally:
            self.state.support_targeting_active = False

    def _cast_party_heewoncheom(self, snapshot: dict | None = None) -> bool:
        if self._is_portal_support_paused():
            return False
        if not self._is_hw_ready():
            return False
        if not should_allow_party_support_cast(
            bool(getattr(self.state, "red_tab_enabled", False)),
            self._party_direct_heal_target_prepared,
        ):
            return False
        spell = self._find_spell_by_name("희원첨")
        if spell is not None and hasattr(spell, "is_ready") and not spell.is_ready():
            return False
        hp_val = int((snapshot or {}).get("hp", 0) or 0)
        print(f"[Support] Heewoncheom casting on warrior: key=4, hp={hp_val}")
        self.state.support_targeting_active = True
        try:
            if not self._press_hw_key("4", variance=0.10):
                return False
            now = time.time()
            self._last_warrior_heewoncheom_time = now
            if spell is not None:
                try:
                    spell.last_cast_time = now
                except Exception:
                    pass
            self._sleep_ui_gap(random.uniform(0.018, 0.036))
            return True
        finally:
            self.state.support_targeting_active = False

    def _cast_due_periodic_party_support(self, snapshot: dict | None, now_support: float | None = None) -> bool:
        if not snapshot:
            return False
        current_mp = int(getattr(self.state, "mp", 0) or 0)
        if current_mp <= self._periodic_support_min_mp:
            return False
        now_support = time.time() if now_support is None else float(now_support)
        hp_val = int(snapshot.get("hp", 0) or 0)

        heewoncheom_elapsed = now_support - float(getattr(self, "_last_warrior_heewoncheom_time", 0.0) or 0.0)
        if should_cast_periodic_heewon(heewoncheom_elapsed, self._warrior_heewoncheom_interval):
            if not self._party_direct_heal_target_prepared and not self._prepare_direct_tab_heal_target():
                return False
            if self._cast_party_heewoncheom(snapshot):
                self._last_party_hp_value = hp_val
                return True

        heewon_elapsed = now_support - float(getattr(self, "_last_warrior_heewon_time", 0.0) or 0.0)
        if should_cast_periodic_heewon(heewon_elapsed, self._warrior_heewon_interval):
            if not self._party_direct_heal_target_prepared and not self._prepare_direct_tab_heal_target():
                return False
            if self._cast_party_heewon(snapshot):
                self._last_party_hp_value = hp_val
                return True

        return False

    def cast_repeat_6_up_enter(self, repeat_count: int = 5) -> bool:
        if not self._is_hw_ready() or not self._is_game_window_active():
            return False
        previous_follow = bool(getattr(self.state, "nav_follow_enabled", False))
        estimated_duration = 0.35 + (max(1, int(repeat_count or 1)) * 0.22)
        self._pause_follow_for_action(duration=max(1.6, estimated_duration + 0.8))
        self._set_support_input_block(1.8)
        self._stop_support_movement_inputs()
        previous_busy = bool(getattr(self.state, "is_combat_busy", False))
        self._set_combat_busy(True)
        try:
            print("[Hon] follow paused. Cast ESC>6>UP>ENTER.")
            tick_interval = 0.20
            sequence = build_f5_hon_sequence(repeat_count=repeat_count)
            for idx in range(0, max(0, len(sequence) - 3), 4):
                tick_started = time.perf_counter()
                for key, delay in zip(sequence[idx:idx + 4], (0.04, 0.04, 0.04, 0.06)):
                    if not self._press_hw_key(key, variance=0.10, skip_focus_guard=True):
                        return False
                    self._sleep_ui_gap(min(delay, 0.03 if key != "enter" else 0.04))
                remaining = tick_interval - (time.perf_counter() - tick_started)
                if remaining > 0:
                    humanized_sleep(remaining, variance=0.05)
            for key, delay in zip(sequence[-3:], (0.07, 0.08, 0.08)):
                if not self._press_hw_key(key, variance=0.10, skip_focus_guard=True):
                    return False
                self._sleep_ui_gap(delay)
            print("[Hon] cast complete. Follow resumed.")
            return True
        finally:
            self._set_combat_busy(previous_busy)
            self._restore_follow_after_action(previous_follow)

    def _get_latest_warrior_snapshot(self) -> dict | None:
        remote = self.state.get_fresh_remote_data_by_role("격수")
        return remote if isinstance(remote, dict) and remote else None

    def _confirm_party_heal_by_hp_gain(self, before_hp: int, timeout: float = 0.65) -> tuple[bool, int]:
        deadline = time.time() + max(0.20, float(timeout))
        latest_hp = max(0, int(before_hp))
        while time.time() < deadline:
            latest = self._get_latest_warrior_snapshot()
            if latest:
                latest_hp = max(0, int(latest.get("hp", 0) or 0))
                if confirm_support_lock_by_hp_gain(before_hp, latest_hp):
                    return True, latest_hp
            humanized_sleep(0.03, variance=0.04)
        return False, latest_hp

    def _is_hw_ready(self) -> bool:
        return hw.is_input_ready()

    def _is_game_window_active(self) -> bool:
        try:
            current_hwnd = win32gui.GetForegroundWindow()
            candidate_hwnds = [current_hwnd]
            try:
                root_hwnd = win32gui.GetAncestor(current_hwnd, 2)
                if root_hwnd and root_hwnd not in candidate_hwnds:
                    candidate_hwnds.append(root_hwnd)
            except Exception:
                pass

            if bool(self.state.hwnd) and self.state.hwnd in candidate_hwnds:
                return True

            for hwnd_candidate in candidate_hwnds:
                try:
                    title = win32gui.GetWindowText(hwnd_candidate) or ""
                except Exception:
                    title = ""
                if any(token.lower() in title.lower() for token in ["ory", "바람", "aion"]):
                    self.state.hwnd = hwnd_candidate
                    return True
            return False
        except Exception:
            return False

    def _is_dosa_f2_follow_service(self) -> bool:
        return (
            self.state.role in ("도사", "도사1")
            and bool(getattr(self.state, "service_active", False))
            and bool(getattr(self.state, "nav_follow_enabled", False))
            and bool(getattr(self.state, "auto_hunt", False))
        )

    def _restore_game_window_focus_for_support(self) -> bool:
        if self._is_game_window_active():
            return True
        if not self._is_dosa_f2_follow_service():
            return False
        hwnd = getattr(self.state, "hwnd", None)
        if not hwnd:
            return False
        try:
            if not win32gui.IsWindow(hwnd):
                return False
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            humanized_sleep(0.025, variance=0.04)
            win32gui.SetForegroundWindow(hwnd)
            humanized_sleep(0.035, variance=0.04)
            return self._is_game_window_active()
        except Exception as exc:
            now = time.time()
            if now - self._last_focus_skip_log_time >= 2.0:
                print(f"[Support] game window focus restore failed: {exc}")
                self._last_focus_skip_log_time = now
            return False

    def _is_self_status_visible(self) -> bool:
        if self._get_current_user_pattern() == self._support_self_pattern:
            return True
        last_seen = float(getattr(self.state, "last_self_userinfo_seen", 0.0) or 0.0)
        return (time.time() - last_seen) <= self._self_status_stale_after

    def _is_support_lock_search_active(self) -> bool:
        return bool(self._ntab_in_progress) or time.time() < float(getattr(self, "_support_lock_search_until", 0.0) or 0.0)

    def _refresh_self_status_if_needed(self, force: bool = False) -> bool:
        if self._is_support_lock_search_active():
            return self._is_self_status_visible()
        if not self._is_hw_ready() or not self._is_game_window_active():
            return self._is_self_status_visible()
        if not force and time.time() < float(getattr(self, "_self_status_refresh_fail_until", 0.0) or 0.0):
            return self._is_self_status_visible()
        if not force and self._is_self_status_visible():
            return True

        now = time.time()
        last_open = float(getattr(self.state, "last_self_status_open_time", 0.0) or 0.0)
        if not force and (now - last_open) < self._self_status_refresh_interval:
            return self._is_self_status_visible()

        now_log = time.time()
        if force or (now_log - float(getattr(self, "_last_self_status_missing_log_time", 0.0) or 0.0)) >= 8.0:
            print("[Buff] self jump2 missing. Press S to refresh status window.")
            self._last_self_status_missing_log_time = now_log
        self._mark_self_status_scan_window()
        self._press_hw_key("s", variance=0.10)
        self.state.last_self_status_open_time = now
        self._sleep_ui_gap(0.12)
        visible = self._wait_for_user_pattern(self._support_self_pattern, timeout=0.80)
        if visible:
            self._self_status_refresh_fail_until = 0.0
        else:
            self._self_status_refresh_fail_until = time.time() + 6.0
        return visible

    def _should_abort_self_support_buff(self) -> bool:
        if self._is_support_lock_search_active():
            return True
        if self.state.role == "도사" and self._is_follow_reposition_needed():
            return True
        if self._needs_hp_recovery():
            return True
        return False

    def _should_attempt_self_support_buffs(self) -> bool:
        if self._should_abort_self_support_buff():
            return False
        now = time.time()
        if (now - float(getattr(self, "_last_self_support_buff_attempt_time", 0.0) or 0.0)) < self._self_support_buff_min_interval:
            return False
        return True

    def _ensure_self_cooltime_visible(self) -> bool:
        if self._is_self_status_visible():
            return True
        return self._refresh_self_status_if_needed(force=False)

    def _wait_for_self_cooltime_scan(self, since: float, timeout: float = 0.65) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            scan_time = float(getattr(self.state, "last_self_cooltime_scan_time", 0.0) or 0.0)
            if scan_time > since:
                return True
            time.sleep(0.03)
        return False

    def _refresh_self_cooltime_view(self, reason_log: str | None = None) -> bool:
        if self._is_support_lock_search_active():
            return bool(getattr(self.state, "self_bm_detected", False)) or bool(getattr(self.state, "self_gg_detected", False))
        if not self._is_hw_ready() or not self._is_game_window_active():
            return False

        if bool(getattr(self.state, "self_bm_detected", False)) or bool(getattr(self.state, "self_gg_detected", False)):
            return True

        now = time.time()
        last_open = float(getattr(self.state, "last_self_status_open_time", 0.0) or 0.0)
        last_scan = float(getattr(self.state, "last_self_cooltime_scan_time", 0.0) or 0.0)
        if last_open > 0.0 and (now - last_open) < self._self_status_refresh_interval:
            if last_scan > last_open:
                return True
            return self._wait_for_self_cooltime_scan(last_open, timeout=0.18)

        if reason_log:
            print(reason_log)

        refresh_start = now
        self.state.self_bm_detected = False
        self.state.self_gg_detected = False
        self._mark_self_status_scan_window()
        if not self._press_hw_key("s", variance=0.10):
            return False
        self.state.last_self_status_open_time = refresh_start
        self._sleep_ui_gap(0.10)
        if not self._wait_for_self_cooltime_scan(refresh_start, timeout=self._self_cooltime_refresh_timeout):
            return False
        print(
            "[Buff] fresh cooltime scan: "
            f"bm={bool(getattr(self.state, 'self_bm_detected', False))} "
            f"gg={bool(getattr(self.state, 'self_gg_detected', False))}"
        )
        return True

    def _clear_red_tab_for_self_cast(self) -> bool:
        if not bool(getattr(self.state, "red_tab_enabled", False)):
            return True
        now = time.time()
        if (now - self._last_self_redtab_clear_time) < self._redtab_clear_cooldown_sec:
            return True
        print("[Buff] red_tab active. Press ESC before self buff cast.")
        if not self._press_hw_key("esc", variance=0.10):
            return False
        self._invalidate_warrior_redtab_verification()
        self._last_self_redtab_clear_time = now
        self._sleep_self_buff_gap()
        if bool(getattr(self.state, "red_tab_enabled", False)):
            print("[Buff] red_tab detector still ON after ESC. Continue self buff cast anyway.")
        return True

    def _execute_self_geumgang_cycle(self, skip_refresh: bool = False, force_cast: bool = False) -> bool:
        current_time = time.time()
        if int(getattr(self.state, "hp", 0) or 0) <= 0:
            self.state.last_self_gg_cast_time = 0.0
            return False
        if current_time < float(getattr(self, "_self_gg_retry_cooldown_until", 0.0) or 0.0):
            return False
        if (not force_cast) and self._should_abort_self_support_buff():
            return False

        if not skip_refresh:
            cooltime_ready = self._refresh_self_cooltime_view("[Buff] self gg missing. Press S once before gg cast.")
            if not cooltime_ready:
                return False
        if bool(getattr(self.state, "self_gg_detected", False)):
            self._self_gg_retry_cooldown_until = 0.0
            return False

        last_cast_time = float(getattr(self.state, "last_self_gg_cast_time", 0.0) or 0.0)
        if current_time - last_cast_time < 2.0:
            return False

        if not self._clear_red_tab_for_self_cast():
            return False

        attempt_count = random.randint(2, 3)
        print(f"[Buff] self gg missing. Cast 0 x{attempt_count}.")
        for attempt in range(attempt_count):
            if self._should_abort_self_support_buff():
                return attempt > 0
            self._press_hw_key("0", variance=0.10)
            self.state.last_self_gg_cast_time = time.time()
            if attempt < attempt_count - 1:
                self._sleep_self_buff_gap()

        cast_refresh_start = time.time()
        self._wait_for_self_cooltime_scan(cast_refresh_start, timeout=self._self_gg_verify_timeout)
        deadline = time.time() + self._self_gg_verify_timeout
        while time.time() < deadline:
            if bool(getattr(self.state, "self_gg_detected", False)):
                print("[Buff] self gg detected after cast burst.")
                self._self_gg_retry_cooldown_until = 0.0
                return True
            time.sleep(0.03)

        print("[Buff] self gg still missing after cast burst.")
        self._self_gg_retry_cooldown_until = time.time() + self._self_gg_retry_cooldown_sec
        return True

    def _execute_self_bomu_cycle(self, skip_refresh: bool = False, force_cast: bool = False) -> bool:
        current_time = time.time()
        if int(getattr(self.state, "hp", 0) or 0) <= 0:
            self.state.last_self_buff_time = 0.0
            return False
        if current_time < float(getattr(self, "_self_bm_retry_cooldown_until", 0.0) or 0.0):
            return False
        if (not force_cast) and self._should_abort_self_support_buff():
            return False

        cooltime_ready = True
        if not skip_refresh:
            cooltime_ready = self._refresh_self_cooltime_view("[Buff] self bm missing. Press S once before buff cast.")
        bm_detected = bool(getattr(self.state, "self_bm_detected", False))
        last_buff_time = float(getattr(self.state, "last_self_buff_time", 0.0) or 0.0)

        if not cooltime_ready:
            return False
        if bm_detected:
            self._self_bm_retry_cooldown_until = 0.0
            return False
        if current_time - last_buff_time < 3.0:
            return False

        if bool(getattr(self.state, "self_bm_detected", False)):
            return False
        if not self._clear_red_tab_for_self_cast():
            return False

        print("[Buff] self bm missing. Cast 8 -> HOME -> ENTER, 9 -> HOME -> ENTER")

        for key in ("8", "9"):
            if self._should_abort_self_support_buff():
                return key != "8"
            self._press_hw_key(key, variance=0.10)
            self._sleep_self_buff_gap()
            self._press_hw_key("home", variance=0.10)
            self._sleep_self_buff_gap()
            self._press_hw_key("enter", variance=0.10)
            if key == "8":
                self._sleep_self_buff_gap()
        self.state.last_self_buff_time = current_time
        self._self_bm_retry_cooldown_until = time.time() + 8.0
        return True

    def _execute_self_support_buffs_cycle(self) -> bool:
        current_time = time.time()
        if int(getattr(self.state, "hp", 0) or 0) <= 0:
            self.state.last_self_buff_time = 0.0
            self.state.last_self_gg_cast_time = 0.0
            return False
        if not self._should_attempt_self_support_buffs():
            return False
        self._last_self_support_buff_attempt_time = current_time

        if not self._refresh_self_status_if_needed(force=False):
            self._self_status_refresh_fail_until = time.time() + 6.0
            return False

        if not self._refresh_self_cooltime_view("[Buff] self status check. Press S once for cooltime scan."):
            return False

        if not self._clear_red_tab_for_self_cast():
            return False

        acted = False
        gg_ready_for_cycle = bool(getattr(self.state, "self_gg_detected", False))
        if current_time < float(getattr(self, "_self_gg_retry_cooldown_until", 0.0) or 0.0):
            gg_ready_for_cycle = True
        if not gg_ready_for_cycle:
            attempt_count = random.randint(2, 3)
            print(f"[Buff] self gg missing. Cast 0 x{attempt_count}.")
            for attempt in range(attempt_count):
                if self._should_abort_self_support_buff():
                    return acted
                self._press_hw_key("0", variance=0.10, skip_focus_guard=True)
                self.state.last_self_gg_cast_time = time.time()
                if attempt < attempt_count - 1:
                    self._sleep_geumgang_gap_fast()
            acted = True

            cast_refresh_start = time.time()
            self._wait_for_self_cooltime_scan(cast_refresh_start, timeout=0.15)
            deadline = time.time() + 0.15
            while time.time() < deadline:
                if bool(getattr(self.state, "self_gg_detected", False)):
                    print("[Buff] self gg detected after cast burst.")
                    self._self_gg_retry_cooldown_until = 0.0
                    gg_ready_for_cycle = True
                    break
                time.sleep(0.015)

            if not gg_ready_for_cycle:
                print("[Buff] self gg still missing after cast burst. Retry deferred.")
                self._self_gg_retry_cooldown_until = time.time() + self._self_gg_retry_cooldown_sec
                return acted

        last_buff_time = float(getattr(self.state, "last_self_buff_time", 0.0) or 0.0)
        bm_retry_locked = current_time < float(getattr(self, "_self_bm_retry_cooldown_until", 0.0) or 0.0)
        if gg_ready_for_cycle and not bm_retry_locked and not bool(getattr(self.state, "self_bm_detected", False)) and (time.time() - last_buff_time) >= 3.0:
            print("[Buff] self bm missing. Cast 8 -> HOME -> ENTER, 9 -> HOME -> ENTER")
            for key in ("8", "9"):
                if self._should_abort_self_support_buff():
                    return acted
                self._press_hw_key(key, variance=0.10, skip_focus_guard=True)
                self._sleep_self_buff_gap_fast()
                self._press_hw_key("home", variance=0.10, skip_focus_guard=True)
                self._sleep_self_buff_gap_fast()
                self._press_hw_key("enter", variance=0.10, skip_focus_guard=True)
                if key == "8":
                    self._sleep_self_buff_gap_fast()
            self.state.last_self_buff_time = time.time()
            self._self_bm_retry_cooldown_until = time.time() + 8.0
            acted = True

        return acted

    def _execute_warrior_bomu_cycle(self, snapshot: dict | None) -> bool:
        if not snapshot:
            return False

        warrior_hp = int(snapshot.get("hp", 0) or 0)
        if warrior_hp <= 0:
            self.state.last_warrior_bomu_time = 0.0
            self._last_warrior_bomu_attempt_time = 0.0
            return False

        current_time = time.time()
        last_buff_time = float(getattr(self.state, "last_warrior_bomu_time", 0.0) or 0.0)
        if current_time - last_buff_time < self._warrior_bomu_interval:
            return False
        if (current_time - float(self._last_warrior_bomu_attempt_time or 0.0)) < self._warrior_bomu_fail_retry_sec:
            return False

        # 실패 시 즉시 재탐색을 막아 입력 폭주를 줄인다.
        self._last_warrior_bomu_attempt_time = current_time

        # jump/N-tab 경로는 현재 불안정하므로 보무는 로컬 red_tab이 이미 잡힌 경우에만 시전한다.
        if not bool(getattr(self.state, "red_tab_enabled", False)):
            if (current_time - float(self._last_warrior_redtab_skip_log_time or 0.0)) >= 2.0:
                print("[Support] warrior bomu skipped: local red_tab not ready.")
                self._last_warrior_redtab_skip_log_time = current_time
            return False

        print("[Support] warrior 보무 시전: 8 -> 9 (red_tab direct)")
        for key in ("8", "9"):
            self._press_hw_key(key, variance=0.10)
            humanized_sleep(TIMING_CONFIG["spell_cast_gap"], variance=0.10)
            if key == "8":
                humanized_sleep(TIMING_CONFIG["bomu_spell_gap"], variance=0.10)
        self.state.last_warrior_bomu_time = current_time
        return True

    def _execute_warrior_debuff_cycle(self, snapshot: dict | None) -> bool:
        # 혼(디버프)은 현재 불안정해서 F2 루프에서 임시 제외한다.
        return False
        if not snapshot:
            return False
        # 혼(디버프)은 F2(FOLLOW+SERVICE) 루프 내부에서만 실행한다.
        if not bool(getattr(self.state, "f2_service_debuff_enabled", False)):
            return False
        if self._is_support_lock_search_active() or self._is_support_input_block_active():
            return False

        debuff_spell = str(getattr(self.state, "recovery_debuff_spell", "") or "").strip()
        if not debuff_spell or debuff_spell == "사용 안 함":
            return False

        # 조건 1) 격수와 맨해튼 거리 5 이내
        remote_x = int(snapshot.get("x", snapshot.get("pos_x", 0)) or 0)
        remote_y = int(snapshot.get("y", snapshot.get("pos_y", 0)) or 0)
        local_x = int(getattr(self.state, "x", 0) or 0)
        local_y = int(getattr(self.state, "y", 0) or 0)
        if follow_manhattan_gap(remote_x, remote_y, local_x, local_y) > 5:
            return False

        # 조건 2) 격수 HP 50000 이하일 때 혼 금지
        warrior_hp = int(snapshot.get("hp", 0) or 0)
        if warrior_hp <= 50000:
            return False

        now = time.time()
        if (now - float(getattr(self, "_last_warrior_debuff_time", 0.0) or 0.0)) < float(self._warrior_debuff_next_interval):
            return False

        skill = next((s for s in self.state.spells if getattr(s, "name", "") == debuff_spell), None)
        if skill is not None and hasattr(skill, "is_ready") and not skill.is_ready():
            return False

        # 조건 3) 혼 키는 6번 고정
        key = "6"
        loop_count = random.randint(8, 14)
        previous_busy = bool(getattr(self.state, "is_combat_busy", False))
        previous_follow = bool(getattr(self.state, "nav_follow_enabled", False))
        self._warrior_debuff_active = True
        estimated_duration = 0.5 + (loop_count * 0.22)
        self._pause_follow_for_action(duration=max(2.2, estimated_duration + 0.8))
        self._set_support_input_block(max(2.2, estimated_duration + 0.6))
        self._stop_support_movement_inputs()
        self._set_combat_busy(True)
        try:
            print("[Debuff] follow paused before warrior hon cast.")
            for _ in range(loop_count):
                if not self._press_hw_key("esc", variance=0.10):
                    return False
                self._sleep_ui_gap(random.uniform(0.05, 0.09))
                if not self._press_hw_key(key, variance=0.10):
                    return False
                self._sleep_ui_gap(random.uniform(0.05, 0.09))
                if not self._press_hw_key("up", variance=0.10):
                    return False
                self._sleep_ui_gap(random.uniform(0.05, 0.09))
                if not self._press_hw_key("enter", variance=0.10):
                    return False

            self._last_warrior_debuff_time = time.time()
            self._warrior_debuff_next_interval = random.uniform(
                self._warrior_debuff_interval_min,
                self._warrior_debuff_interval_max,
            )
            print(
                f"[Debuff] warrior debuff cast: {debuff_spell} key={key} loops={loop_count} "
                f"next={self._warrior_debuff_next_interval:.1f}s"
            )

            # 혼 종료 후 순서: gg -> follow -> warrior heal
            self._refresh_self_cooltime_view("[Debuff] post-debuff status refresh.")
            self._execute_self_geumgang_cycle(skip_refresh=True, force_cast=True)
            self._post_debuff_follow_until = time.time() + 0.70
            self._post_debuff_recover_prepare_pending = True
            return True
        finally:
            self._warrior_debuff_active = False
            self._set_combat_busy(previous_busy)
            self._restore_follow_after_action(previous_follow)

    def _press_hw_key(self, key: str, variance: float = 0.10, skip_focus_guard: bool = False):
        if not self._is_hw_ready():
            now = time.time()
            if now - self._last_hw_skip_log_time >= 2.0:
                print(f"[Support] hardware not ready. Skip key: {key}")
                self._last_hw_skip_log_time = now
            return False
        force_support_input = bool(skip_focus_guard or self._is_dosa_f2_follow_service())
        if not force_support_input and not self._restore_game_window_focus_for_support():
            now = time.time()
            if now - self._last_focus_skip_log_time >= 2.0:
                print(f"[Support] game window inactive. Skip key: {key}")
                self._last_focus_skip_log_time = now
            return False
        if force_support_input:
            actual_key = display_game_hotkey(key)
            hw.send_force(f"D:{actual_key}")
            humanized_sleep(TIMING_CONFIG["key_down_hold"], variance)
            hw.send_force(f"U:{actual_key}")
        else:
            hw.fast_press(key, variance=variance)
        return True

    def _sleep_ntab_gap(self):
        # N-tab 반응성을 높이되 선택 안정성은 유지하는 구간.
        humanized_sleep(random.uniform(0.10, 0.16), variance=0.10)

    def _sleep_warrior_search_gap(self):
        # 격수 jump 탐색은 실제 입력 템포에 맞춰 더 짧게 확인한다.
        humanized_sleep(random.uniform(0.05, 0.09), variance=0.08)

    def _sleep_ui_gap(self, base: float = 0.14):
        humanized_sleep(base, variance=0.10)

    def _sleep_self_buff_gap(self):
        time.sleep(random.uniform(0.20, 0.50))

    def _sleep_self_buff_gap_fast(self):
        time.sleep(random.uniform(0.10, 0.22))

    def _sleep_geumgang_gap_fast(self):
        time.sleep(random.uniform(0.08, 0.15))

    def _get_current_user_pattern(self) -> str:
        current = str(getattr(self.state, "user_info_text", "") or getattr(self.state, "user_name", "") or "").strip()
        if not current:
            return ""
        allowed = set(getattr(self.state, "party_userinfo_patterns", []) or [])
        return current if current in allowed else ""

    def _wait_for_user_pattern(self, expected_pattern: str, timeout: float = 0.30) -> bool:
        deadline = time.time() + max(0.05, float(timeout or 0.0))
        while time.time() < deadline:
            if self._get_current_user_pattern() == expected_pattern:
                return True
            time.sleep(0.03)
        return self._get_current_user_pattern() == expected_pattern

    def _is_user_pattern_confident(self, expected_pattern: str | None = None, min_score: float = 0.90) -> bool:
        current = self._get_current_user_pattern()
        if expected_pattern and current != expected_pattern:
            return False
        if not current:
            return False
        if bool(getattr(self.state, "user_info_ambiguous", False)):
            return False

        source = str(getattr(self.state, "user_info_source", "") or "")
        score = float(getattr(self.state, "user_score", 0.0) or 0.0)
        if self._ntab_in_progress and current == self._support_warrior_pattern:
            if source == "findtext-stable":
                return True
            if source == "findtext":
                return score >= max(0.80, min_score - 0.10)
            if source == "bitwise":
                return score >= max(0.74, min_score - 0.16)
        if source == "findtext-stable":
            return True
        if source == "findtext":
            return score >= min_score
        if source == "bitwise":
            return score >= max(0.84, min_score - 0.06)
        return False

    def _wait_for_confident_user_pattern(
        self,
        expected_pattern: str,
        timeout: float | None = None,
        min_hits: int | None = None,
    ) -> bool:
        timeout = float(timeout if timeout is not None else self._ntab_confirm_timeout)
        min_hits = int(min_hits if min_hits is not None else self._ntab_confirm_required_hits)
        deadline = time.time() + max(0.10, timeout)
        streak = 0
        sample_sleep = 0.02 if self._ntab_in_progress else 0.03
        while time.time() < deadline:
            if self._is_user_pattern_confident(expected_pattern):
                streak += 1
                if streak >= max(1, min_hits):
                    return True
            else:
                streak = 0
            time.sleep(sample_sleep)
        return False

    def _wait_for_red_tab_lock(self, timeout: float | None = None, min_hits: int | None = None) -> bool:
        timeout = float(timeout if timeout is not None else self._red_tab_confirm_timeout)
        min_hits = int(min_hits if min_hits is not None else self._red_tab_confirm_required_hits)
        deadline = time.time() + max(0.10, timeout)
        streak = 0
        sample_sleep = 0.015 if self._ntab_in_progress else 0.03
        while time.time() < deadline:
            if bool(getattr(self.state, "red_tab_enabled", False)):
                self._last_local_redtab_seen_time = time.time()
                streak += 1
                if streak >= max(1, min_hits):
                    return True
            else:
                streak = 0
            time.sleep(sample_sleep)
        recent_seen = time.time() - float(getattr(self, "_last_local_redtab_seen_time", 0.0) or 0.0)
        return recent_seen <= self._red_tab_promotion_recent_grace

    def _wait_for_red_tab_promotion_lock(self, timeout: float | None = None) -> bool:
        timeout = float(timeout if timeout is not None else self._red_tab_promotion_timeout)
        deadline = time.time() + max(0.20, timeout)
        while time.time() < deadline:
            if self._wait_for_red_tab_lock(timeout=0.16, min_hits=1):
                return True
            time.sleep(0.02)
        recent_seen = time.time() - float(getattr(self, "_last_local_redtab_seen_time", 0.0) or 0.0)
        return recent_seen <= self._red_tab_promotion_recent_grace

    def _is_remote_warrior_redtab_ready(self, snapshot: dict | None = None) -> bool:
        if not isinstance(snapshot, dict):
            return False
        if not bool(snapshot.get("red_tab_enabled", False)):
            return False
        return self._is_user_pattern_confident(self._support_warrior_pattern, min_score=0.84)

    def _prime_self_user_info(self) -> bool:
        if self._refresh_self_status_if_needed(force=True) or self._wait_for_user_pattern(self._support_self_pattern, timeout=0.30):
            print("[Support] self user_info primed: jump2")
            return True
        last_seen = float(getattr(self.state, "last_self_userinfo_seen", 0.0) or 0.0)
        if (time.time() - last_seen) <= 1.20:
            print("[Support] self user_info primed by recent jump2 snapshot.")
            return True
        print("[Support] failed to prime self user_info via s key.")
        return False

    def _prime_self_user_info_after_red_lock(self) -> bool:
        prev_ntab_active = bool(getattr(self.state, "ntab_active", False))
        prev_ntab_since = float(getattr(self.state, "ntab_active_since", 0.0) or 0.0)
        prev_in_progress = bool(getattr(self, "_ntab_in_progress", False))
        try:
            # red_tab lock 이후에는 self status 확인(S/jump2)을 우선 수행한다.
            self.state.ntab_active = False
            self.state.ntab_active_since = 0.0
            self._ntab_in_progress = False
            return self._prime_self_user_info()
        finally:
            self._ntab_in_progress = prev_in_progress
            self.state.ntab_active = prev_ntab_active
            self.state.ntab_active_since = prev_ntab_since

    def _cancel_ntab_selection(self):
        self._press_hw_key("esc")
        self._invalidate_warrior_redtab_verification()
        self._sleep_ui_gap(0.12)
        try:
            hw.stop_all_inputs(repeat=1, delay=0.02)
        except Exception:
            pass

    def _reenter_ntab_selection(self):
        self._press_hw_key("tab")
        self._sleep_ntab_gap()

    def _get_ntab_direction_order(self, snapshot: dict | None = None) -> list[str]:
        current_x = int(getattr(self.state, "x", 0) or 0)
        current_y = int(getattr(self.state, "y", 0) or 0)
        target_x = int((snapshot or {}).get("x", (snapshot or {}).get("pos_x", current_x)) or current_x)
        target_y = int((snapshot or {}).get("y", (snapshot or {}).get("pos_y", current_y)) or current_y)
        dx = target_x - current_x
        dy = target_y - current_y
        order = []

        def add(direction: str | None):
            if direction and direction not in order:
                order.append(direction)

        horiz = "right" if dx > 0 else "left" if dx < 0 else None
        vert = "down" if dy > 0 else "up" if dy < 0 else None
        opposite = {"up": "down", "down": "up", "left": "right", "right": "left"}

        if abs(dx) >= abs(dy):
            add(horiz)
            add(vert)
            add(opposite.get(vert) if vert else None)
            add(opposite.get(horiz) if horiz else None)
        else:
            add(vert)
            add(horiz)
            add(opposite.get(horiz) if horiz else None)
            add(opposite.get(vert) if vert else None)

        for direction in ["up", "down", "left", "right"]:
            add(direction)

        last_dir = getattr(self, "_last_ntab_success_direction", None)
        if last_dir in order:
            order.remove(last_dir)
            order.insert(0, last_dir)
        return order

    def _is_monster_target_selected(self, baseline_target_text: str = "", baseline_target_kind: str = "") -> bool:
        current_kind = str(getattr(self.state, "target_kind", "") or "")
        current_text = str(getattr(self.state, "target_info_text", "") or "")
        if current_kind == "MONSTER" and current_text:
            if current_kind != baseline_target_kind or current_text != baseline_target_text:
                return True
        monster_names = getattr(self.state, "target_monster_names", []) or []
        return any(name and name in current_text for name in monster_names)

    def _attempt_ntab_confirm(
        self,
        direction: str,
        expected_pattern: str,
        confirm_timeout: float | None = None,
    ) -> str:
        baseline_user = self._get_current_user_pattern()
        baseline_target_text = str(getattr(self.state, "target_info_text", "") or "")
        baseline_target_kind = str(getattr(self.state, "target_kind", "") or "")

        effective_timeout = float(confirm_timeout if confirm_timeout is not None else self._warrior_jump_confirm_timeout)
        if self._ntab_in_progress and expected_pattern == self._support_warrior_pattern:
            self._sleep_ui_gap(self._warrior_jump_settle_delay)

        if self._wait_for_confident_user_pattern(expected_pattern, timeout=effective_timeout, min_hits=1):
            return "user"

        deadline = time.time() + max(0.08, effective_timeout)
        soft_user_streak = 0
        while time.time() < deadline:
            current_user = self._get_current_user_pattern()
            if current_user == expected_pattern:
                if self._is_user_pattern_confident(expected_pattern, min_score=0.84):
                    return "user"
                # N-tab 중에는 bitwise도 허용해 락 반응속도를 높인다.
                source = str(getattr(self.state, "user_info_source", "") or "")
                score = float(getattr(self.state, "user_score", 0.0) or 0.0)
                if source == "bitwise" and score >= 0.80 and not bool(getattr(self.state, "user_info_ambiguous", False)):
                    return "user"
                if self._ntab_in_progress and not bool(getattr(self.state, "user_info_ambiguous", False)):
                    if source == "findtext" and score >= 0.76:
                        soft_user_streak += 1
                    elif source == "bitwise" and score >= 0.72:
                        soft_user_streak += 1
                    else:
                        soft_user_streak = 0
                    if soft_user_streak >= 2:
                        return "user"
            if current_user and current_user != baseline_user and self._is_user_pattern_confident(current_user):
                return "other_user"
            if self._is_monster_target_selected(baseline_target_text, baseline_target_kind):
                return "monster"
            time.sleep(0.02 if self._ntab_in_progress else 0.03)
        return "no_match"

    def _promote_ntab_to_red_tab(self) -> bool:
        self.state.red_tab_promotion_active = True
        self.state.red_tab_promotion_until = time.time() + max(0.60, float(self._red_tab_promotion_timeout) + 0.20)
        for _ in range(3):
            self._press_hw_key("tab")
            self._sleep_ui_gap(0.08)
            self._press_hw_key("tab")
            if self._wait_for_red_tab_promotion_lock(timeout=self._red_tab_promotion_timeout):
                self.state.red_tab_promotion_active = False
                return True
            self._sleep_ui_gap(0.10)
        self.state.red_tab_promotion_active = False
        return self._wait_for_red_tab_lock(timeout=0.12, min_hits=1)

    def _ensure_warrior_red_tab_via_ntab(self, snapshot: dict | None = None) -> bool:
        # 격수 힐은 방향 탐색 없이 tab -> tab 으로 red_tab 을 직접 승격시킨다.
        if bool(getattr(self.state, "red_tab_enabled", False)):
            return True
        self._invalidate_warrior_redtab_verification()
        if self._wait_for_red_tab_lock(timeout=0.12, min_hits=1):
            return True
        if not self._is_hw_ready() or not self._is_game_window_active():
            return False

        now = time.time()
        if now < float(self._redtab_lock_backoff_until or 0.0):
            return False
        if (now - float(self._last_redtab_lock_attempt_time or 0.0)) < self._redtab_lock_retry_sec:
            return False
        self._last_redtab_lock_attempt_time = now

        previous_busy = bool(getattr(self.state, "is_combat_busy", False))
        self._ntab_in_progress = True
        self._support_lock_search_until = time.time() + 0.90
        self.state.red_tab_promotion_active = True
        self.state.red_tab_promotion_until = time.time() + 1.20
        self.state.support_targeting_active = True
        self._set_support_input_block(0.75)
        self.state.ntab_active = False
        self.state.ntab_active_since = 0.0
        self._set_combat_busy(True)
        try:
            try:
                hw.stop_all_inputs(repeat=1, delay=0.02)
            except Exception:
                pass

            print("[Support] direct red_tab promotion: tab -> tab")
            if self._promote_ntab_to_red_tab():
                print("[Support] warrior red_tab activated by direct tab-tab.")
                self._redtab_lock_backoff_until = 0.0
                self._prime_self_user_info_after_red_lock()
                return True

            self._redtab_lock_backoff_until = time.time() + 0.35
            self._invalidate_warrior_redtab_verification()
            if (time.time() - float(self._last_redtab_lock_fail_log_time or 0.0)) >= 0.8:
                print("[Support] warrior direct red_tab lock failed. (red_tab OCR miss)")
                self._last_redtab_lock_fail_log_time = time.time()
            return False
        finally:
            try:
                hw.stop_all_inputs(repeat=1, delay=0.02)
            except Exception:
                pass
            self._ntab_in_progress = False
            self._support_lock_search_until = time.time() + 0.25
            self.state.red_tab_promotion_active = False
            self.state.ntab_active = False
            self.state.ntab_active_since = 0.0
            self.state.support_targeting_active = False
            self.state.support_input_blocked_until = min(
                float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0),
                time.time() + 0.02,
            )
            self._set_combat_busy(previous_busy)
    # ----------------------------------------------------------
    # ?? 蹂대Т(Buff) ?먮룞 愿由??????????????????????????????????
    # ----------------------------------------------------------
    def _execute_bomu_buff_v3(self):
        """
        蹂대Т 踰꾪봽 ?쒖쟾 (蹂댄샇+臾댁옣)
        
        Purpose:
            - 185珥?二쇨린濡?蹂댄샇/臾댁옣 踰꾪봽 ?먮룞 ?곸슜
            - 踰꾪봽 吏?띿떆媛?愿由?
            - humanized ?낅젰?쇰줈 ?먯뿰?ㅻ윭???쒖쟾
            
        Algorithm:
            1. 8踰?蹂댄샇) ?ㅽ궗 ?쒖쟾: ??-> Home -> Enter
            2. bomu_spell_gap(200ms) ?湲?
            3. 9踰?臾댁옣) ?ㅽ궗 ?쒖쟾: ??-> Home -> Enter
            4. 踰꾪봽 ?쒓컙 媛깆떊
            
        Integration:
            - TIMING_CONFIG: 吏?곗떆媛?李몄“
            - state.last_bomu_time: 踰꾪봽 二쇨린 愿由?
            - hw.humanized_press: ?섎뱶?⑥뼱 ?낅젰
        """
        print("[Buff] 蹂대Т 踰꾪봽 ?쒖쟾 以?(8 -> 9 ?쒖감)...")
        for key in ("8", "9"):
            hw.humanized_press(key)
            humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
            hw.humanized_press("home")
            humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
            hw.humanized_press("enter")
            if key == "8":
                humanized_sleep(TIMING_CONFIG["bomu_spell_gap"])
        self.state.last_bomu_time = time.time()
        print("[OK] 蹂대Т 踰꾪봽 ?꾨즺.")

    # ----------------------------------------------------------
    # ?? 吏?ν삎 ?붾쾭???ㅼ틪 ????????????????????????????????????
    # ----------------------------------------------------------
    def _execute_debuff_scan_v3(self):
        """
        ?붾쾭???ㅼ틪 (10~20???쒕뜡)
        
        Purpose:
            - ?곸뿉寃??붾쾭???ㅽ궗 ?먮룞 ?곸슜
            - '嫄몃━吏 ?딆뒿?덈떎' ?⑦꽩?쇰줈 ?ㅽ뙣 媛먯?
            - fail-safe濡?臾댄븳 猷⑦봽 諛⑹?
            - ?쒕뜡 ?쒕룄濡??먯뿰?ㅻ윭? 援ы쁽
            
        Algorithm:
            1. ?ㅽ궗 ?ㅼ젙 ?뺤씤 諛?以鍮?
            2. 10~20???쒕뜡 ?쒕룄
            3. 媛??쒕룄: ?二쇳궎 -> Up -> Enter -> OCR ?뺤씤
            4. ?ㅽ뙣 5???곗냽 ??議곌린 醫낅즺
            5. combat_timeout 珥덇낵 ??媛뺤젣 醫낅즺
            
        Fail-safe:
            - ?寃?臾댄슚 5?? 議곌린 醫낅즺
            - ??꾩븘?? panic_escape() ?몄텧
            
        Integration:
            - TIMING_CONFIG: 紐⑤뱺 吏?곗떆媛?李몄“
            - state.target_name: OCR 寃곌낵 ?뺤씤
            - hw.panic_escape: 鍮꾩긽 ?덉텧
        """
        debuff_spell = self.state.recovery_debuff_spell
        if not debuff_spell or debuff_spell == "사용 안 함":
            return

        skill = next((s for s in self.state.spells if s.name == debuff_spell), None)
        if not skill: return

        print(f"[Debuff] ?붾쾭???ㅼ틪 ?쒖옉: {debuff_spell}")
        previous_follow = bool(getattr(self.state, "nav_follow_enabled", False))
        self._pause_follow_for_action(duration=float(TIMING_CONFIG["combat_timeout"]) + 1.0)
        self._stop_support_movement_inputs()
        num_cycles  = random.randint(10, 20)
        fail_count  = 0
        start_time  = time.time()
        timeout     = TIMING_CONFIG["combat_timeout"]

        try:
            for _ in range(num_cycles):
                # ?? Fail-safe ???????????????????????????????????
                if time.time() - start_time > timeout * random.uniform(0.9, 1.1):
                    print("[Failsafe] [FAIL-SAFE] ?붾쾭???ㅼ틪 ??꾩븘??-> 猷⑦봽 媛뺤젣 以묐떒.")
                    hw.panic_escape()
                    break

                # 1. ?二???
                hw.humanized_press(skill.hotkey)
                humanized_sleep(TIMING_CONFIG["debuff_key_wait"])
                # 2. Up (?ㅼ쓬 ?寃?
                hw.humanized_press("up")
                humanized_sleep(TIMING_CONFIG["debuff_up_wait"])
                # 3. Enter (?寃??뺤젙)
                hw.humanized_press("enter")
                humanized_sleep(TIMING_CONFIG["debuff_ocr_wait"])

                # 4. ?ㅽ뙣 寃利?
                if "嫄몃━吏 ?딆뒿?덈떎" in self.state.target_name:
                    fail_count += 1
                    print(f"[Warn] ?寃?臾댄슚 ({fail_count}/5)")
                    if fail_count >= 5:
                        print("[Stop] ?좏슚 ?寃?遺??-> ?붾쾭???ㅼ틪 議곌린 醫낅즺.")
                        hw.panic_escape(2)
                        break
                else:
                    fail_count = 0
        finally:
            self._restore_follow_after_action(previous_follow)

        self.state.last_debuff_x = self.state.x
        self.state.last_debuff_y = self.state.y
        print("[OK] ?붾쾭???ㅼ틪 ?꾨즺.")

    # ----------------------------------------------------------
    # ?? ?寃??먯깋 ?????????????????????????????????????????????
    # ----------------------------------------------------------
    def _search_target_v3(self):
        """
        ?寃??먯깋 (Tab ??Up ??Enter)
        
        Purpose:
            - Tab ?ㅻ줈 紐ъ뒪??紐⑸줉 ?닿린
            - Up ?ㅻ줈 泥?踰덉㎏ 紐ъ뒪???좏깮
            - Enter ?ㅻ줈 ?寃??뺤젙
            - rate-limiting?쇰줈 怨쇰룄???먯깋 諛⑹?
            
        Algorithm:
            1. ?먯깋 媛꾧꺽 ?쒗븳 ?뺤씤 (理쒖냼 0.15珥?
            2. Tab ???낅젰 (紐ъ뒪??紐⑸줉)
            3. tab_wait(40ms) ?湲?
            4. Up ???낅젰 (泥??寃?
            5. up_wait(40ms) ?湲?
            6. Enter ???낅젰 (?寃??뺤젙)
            7. ocr_wait(100ms) ?湲?(OCR ?묐떟)
            8. ?寃??곹깭 ?ㅼ젙
            
        Integration:
            - TIMING_CONFIG: 紐⑤뱺 吏?곗떆媛?李몄“
            - state.target_locked: ?寃??좉툑 ?곹깭 愿由?
            - _validate_target(): ?寃??좏슚??寃利?
        """
        now = time.time()
        # rate-limiting ?쒓굅: 利됱떆 ?ㅽ뻾 蹂댁옣
        self._last_target_search_time = now
        hw.humanized_press("tab")
        humanized_sleep(TIMING_CONFIG["tab_wait"])
        hw.humanized_press("up")
        humanized_sleep(TIMING_CONFIG["up_wait"])
        hw.humanized_press("enter")
        humanized_sleep(TIMING_CONFIG["ocr_wait"])
        self.state.target_locked = True
        self._last_target_search_time = time.time()
        return True

    # ----------------------------------------------------------
    # ?? ?ъ쟾 寃利??????????????????????????????????????????????
    # ----------------------------------------------------------
    def _validate_target(self) -> bool:
        """
        ?寃??좏슚??寃利?
        
        Purpose:
            - ?寃잛씠 怨듦꺽 媛?ν븳 紐ъ뒪?곗씤吏 ?뺤씤
            - '嫄몃━吏 ?딆뒿?덈떎' ?⑦꽩 利됱떆 ?寃??댁젣
            - OCR ?묐떟 ?湲??쒓컙 愿由?(grace period)
            
        Algorithm:
            1. ?寃??대쫫 議댁옱 ?щ? ?뺤씤
            2. '嫄몃━吏 ?딆뒿?덈떎' ?⑦꽩 寃利?
            3. grace period ???ы깘??諛⑹?
            4. ?좏슚?섏? ?딆쑝硫?ESC ?낅젰 諛??곹깭 ?뺣━
            
        Returns:
            bool: ?寃잛씠 ?좏슚?섎㈃ True
            
        Integration:
            - _search_target_v3(): ?먯깋 ???몄텧
            - _handle_combat(): ?꾪닾 濡쒖쭅?먯꽌 李몄“
            - _clear_target_state(): ?寃??뺣━
        """
        name = self.state.target_name
        if not name:
            acquire_grace = max(0.30, float(TIMING_CONFIG.get("ocr_wait", 0.10)) * 3.0)
            if time.time() - self._last_target_search_time > acquire_grace:
                self._clear_target_state()
            return False

        # ??'嫄몃━吏 ?딆쓬' 利됱떆 痍⑥냼
        if "嫄몃━吏 ?딆뒿?덈떎" in name:
            hw.humanized_press("esc")
            self._clear_target_state()
            return False

        # ??紐ъ뒪???붿씠?몃━?ㅽ듃 寃利?
        if any(m in name for m in self.state.target_monster_names):
            return True

        # ??洹???(?좎?, NPC ?? ??ESC
        print(f"[Misc] 鍮꾨が?ㅽ꽣 ?寃? '{name}' -> ESC.")
        hw.humanized_press("esc")
        self._clear_target_state()
        return False

    # ----------------------------------------------------------
    # ?? ?좎? ?먯? ???????????????????????????????????????????
    # ----------------------------------------------------------
    def _handle_unidentified_user(self):
        import winsound
        if self.state.user_alarm_enabled:
            winsound.MessageBeep()                   # ?덈룄??湲곕낯 寃쎄퀬??
        if self.state.user_next_enabled:
            hw.humanized_press("esc")
            self.state.target_locked = False
            self.state.user_name     = ""
            self.state.is_user_detected = False
        if self.state.user_stop_enabled:
            print("[Stop] 誘명솗???좎? -> ?щ깷 以묐떒.")
            self.state.auto_hunt = False

    # ----------------------------------------------------------
    # ?? ?뷀떚??湲곕컲 ?곗꽑?쒖쐞 ?됰룞 ?쒖뼱 ???????????????????????
    # ----------------------------------------------------------
    def _handle_entity_priorities(self) -> bool:
        """
        entities ?뺣낫瑜?湲곕컲?쇰줈 ?곗꽑?쒖쐞???곕씪 ?됰룞??寃곗젙?쒕떎.

        ?곗꽑?쒖쐞:
            Priority 1 (理쒖슦??: HP/MP ?꾧툒 ??蹂꾨룄 硫붿꽌?쒖뿉??泥섎━ (?ш린?쒕뒗 ?ㅽ궢)
            Priority 2: USER 媛먯? (?붿씠?몃━?ㅽ듃 ?? ??ESC + ?щ깷 以묐떒
            Priority 3: MONSTER 媛먯? ???щ깷 猷⑦떞?쇰줈 吏꾩엯
            Priority 4: ITEM 媛먯? ??猷⑦똿 (is_in_combat==False ???뚮쭔)

        Returns:
            True  ????硫붿꽌?쒖뿉???됰룞??泥섎━?덉쑝誘濡?硫붿씤 猷⑦봽??嫄대꼫?
            False ??泥섎━ ?놁쓬, 硫붿씤 猷⑦봽 怨꾩냽
        """
        # --- Priority 2: ?곷????좎? ---
        hostile_users = [
            u for u in self.state.entities.get("users", [])
            if not u.get("is_whitelisted", True)
        ]
        if hostile_users or (
            getattr(self.state, "is_user_detected", False)
            and self.state.user_name
            and self.state.user_name not in self.state.whitelist_names
            and self.state.user_name not in (getattr(self.state, "party_userinfo_patterns", None) or [])
        ):
            user_name = (hostile_users[0]["name"] if hostile_users
                         else self.state.user_name)
            print(f"[Priority2] ?곷????좎? 媛먯? ??ESC ??? {user_name}")
            self._handle_unidentified_user()
            if not self.state.auto_hunt:
                return True
            return True

        # --- Priority 3: 紐ъ뒪??媛먯? ???щ깷 ---
        monsters = self.state.entities.get("monsters", [])
        if monsters:
            # 媛??媛源뚯슫 紐ъ뒪???뺣낫 異쒕젰 (0踰덉㎏媛 媛??癒쇱? 異붿쟻??紐?
            m = monsters[0]
            if not self.state.target_locked:
                print(f"[Priority3] MONSTER 媛먯? (ID={m.get('id','?')}) "
                      f"{m.get('name','')} @ {m.get('grid','?')} ???щ깷 猷⑦떞")
                self.state.combat_start_time = 0.0
            return False   # ?щ깷? 硫붿씤 猷⑦봽?먯꽌 怨꾩냽 泥섎━

        # --- Priority 4: ?꾩씠????猷⑦똿 (鍮꾩쟾??以묒뿉留? ---
        items = self.state.entities.get("items", [])
        if items and not self._is_in_combat():
            item = items[0]
            grid = item.get("grid")
            if grid and not self.state.detected_item_grid:
                print(f"[Priority4] ITEM 媛먯? ??猷⑦똿: {item.get('name','')} @ {grid}")
                self.state.detected_item_grid = grid
                self.state.detected_item_name = item.get("name", "")
            return False

        return False



    # ----------------------------------------------------------
    # ?? ?꾪닾 怨듦꺽 猷⑦봽 (Fail-safe ?댁옣) ??????????????????????
    # ----------------------------------------------------------
    def _execute_combat(self):
        """
        ?꾩옱 ?寃잛뿉 怨듦꺽 留덈쾿 1???쒖쟾.
        combat_timeout 珥덇낵 ??猷⑦봽 利됱떆 break ??ESC ??IDLE 蹂듦?.
        """
        if not self._validate_target():
            return

        # ?꾪닾 ?쒖옉 ??대㉧ 珥덇린??
        if self.state.combat_start_time == 0.0:
            self.state.combat_start_time = time.time()

        # ?? Fail-safe 寃????????????????????????????????????
        elapsed = time.time() - self.state.combat_start_time
        timeout = TIMING_CONFIG["combat_timeout"] * random.uniform(0.90, 1.10)
        if elapsed > timeout:
            print(f"[Failsafe] [FAIL-SAFE] ?꾪닾 ??꾩븘??{elapsed:.1f}s) -> 媛뺤젣 IDLE 蹂듦?.")
            # ?듭떖: 媛??癒쇱? ?꾩옱 怨듦꺽 猷⑦봽瑜?以묐떒?섍퀬 ESC
            hw.panic_escape()
            self.state.target_locked      = False
            self.state.target_name        = ""
            self.state.combat_start_time  = 0.0
            self.current_state            = AppStatus.IDLE
            return

        # ?? 怨듦꺽 留덈쾿 ?쒖쟾 ??????????????????????????????????
        for s in self.state.spells:
            if s.category == "怨듦꺽" and s.is_ready():
                hw.humanized_press(s.hotkey)
                humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
                s.last_cast_time = time.time()
                break

    # ----------------------------------------------------------
    # ?? 硫붿씤 FSM 猷⑦봽 (?곗꽑?쒖쐞 泥닿퀎) ?????????????????????????
    # ----------------------------------------------------------
    def run(self):
        print(f"[Logic] BIS Logic Engine started (role: {self.state.role})")
        print("[Logic] Priority: Emergency -> Combat -> Moving")
        while self.state.running:
            if getattr(self.state, "automation_paused", False):
                self._clear_nav_context()
                self._was_in_combat = False
                self._advance_waypoint_after_combat = False
                self._combat_resume_context = None
                humanized_sleep(0.02)
                continue

            if not getattr(self.state, "service_active", False) and not self._restore_dosa_service_if_follow_autohunt():
                if self.state.is_combat_busy:
                    self._set_combat_busy(False)
                self.state.last_bomu_time = 0.0
                self.state.combat_start_time = 0.0
                humanized_sleep(0.02)
                continue

            # Hot-Reload
            if self.state.last_update_time > self.last_load_time:
                self.last_load_time = time.time()
                print("[Sync] [Logic] Reloading shared settings...")

            # ?대룞 以??먮룞?щ깷? Sentinel 湲곕컲 媛먯?媛 ?꾩젣??
            if self.state.auto_hunt and not self.state.sentinel_enabled:
                self.state.sentinel_enabled = True

            # ?? 0?쒖쐞: Emergency (鍮꾩긽 ?곹솴) ?????????????????????
            is_dosa_role = is_support_role(
                getattr(self.state, "network_role", "") or getattr(self.state, "role", "")
            )
            support_target = self._get_support_target_data() if is_dosa_role else None
            if is_dosa_role and self._run_dosa_service_cycle(support_target):
                continue
            if self.state.role == "술사" and self._run_sulsa_service_cycle():
                continue
            if self._check_emergency(support_target):
                self._handle_emergency(support_target)
                continue

            # ?? 1?쒖쐞: Combat (?꾪닾) ???????????????????????????
            combat_needed = self._should_handle_combat()
            self._set_combat_busy(combat_needed)
            if combat_needed:
                self._handle_combat()
                continue

            # ?? 2?쒖쐞: Moving (?대룞/?ㅻ퉬寃뚯씠?? ??????????????????
            if self.state.auto_hunt or getattr(self.state, "service_active", False):
                self._handle_moving()
                continue

            # ?? ?湲??곹깭 (理쒖냼?뷀븯??鍮좊Ⅸ 諛섏쓳) ?????????????????????????????????????
            humanized_sleep(0.001)  # 1ms留??湲고븯??鍮좊Ⅸ 諛섏쓳 ?띾룄 ?뺜퓷

    def _check_emergency(self, snapshot: dict | None = None) -> bool:
        """鍮꾩긽 ?곹솴 ?뺤씤 (HP/MP ?꾧퀎媛? ?좎? 媛먯?, 梨꾪똿)"""
        if self._needs_hp_recovery_for(snapshot) or self._needs_mp_recovery_for(snapshot):
            return True
        # 誘명솗???좎? 媛먯?
        if snapshot is None and self.state.is_user_detected:
            return True
        # 梨꾪똿 ?쒖꽦??
        if snapshot is None and self.state.is_chat_active:
            return True
        return False

    def _handle_emergency(self, snapshot: dict | None = None):
        """鍮꾩긽 ?곹솴 泥섎━"""
        if snapshot is not None:
            if self._needs_hp_recovery_for(snapshot):
                self._recover_party_hp(snapshot)
            if self._needs_mp_recovery_for(snapshot):
                self._recover_party_mp(snapshot)
            return

        if self._needs_hp_recovery():
            self._recover_hp()
        if self._needs_mp_recovery():
            self._recover_mp()
        
        # ?좎? 媛먯? ???
        if self.state.is_user_detected:
            self._handle_unidentified_user()
        
        # 梨꾪똿 ?쒖꽦?????꾪닾 以묐떒
        if self.state.is_chat_active:
            if self.state.target_locked:
                hw.humanized_press("esc")
                self.state.target_locked = False

    def _is_follow_reposition_needed(self) -> bool:
        if not bool(getattr(self.state, "nav_follow_enabled", False)):
            return False

        remote_data = self.state.get_fresh_remote_data_by_role("격수")
        if not isinstance(remote_data, dict):
            return False

        target_x = int(remote_data.get("x", remote_data.get("pos_x", 0)) or 0)
        target_y = int(remote_data.get("y", remote_data.get("pos_y", 0)) or 0)
        if abs(target_x) > 300 or abs(target_y) > 300:
            return False

        follow_x = target_x
        follow_y = target_y
        current_x = int(getattr(self.state, "x", 0) or 0)
        current_y = int(getattr(self.state, "y", 0) or 0)
        follow_x, follow_y = adjust_follow_target_by_axis_gap(
            follow_x,
            follow_y,
            current_x,
            current_y,
            target_x,
            target_y,
            min_axis_gap=1,
        )
        return not should_hold_follow_position(follow_x, follow_y, current_x, current_y)

    def _repair_dosa_f2_runtime_state(self):
        if not is_support_role(getattr(self.state, "network_role", "") or self.state.role) or not bool(getattr(self.state, "service_active", False)):
            return

        changed = []
        follow_pause_active = time.time() < float(getattr(self, "_follow_pause_until", 0.0) or 0.0)
        if (not follow_pause_active) and not bool(getattr(self.state, "nav_follow_enabled", False)):
            self.state.nav_follow_enabled = True
            changed.append("follow")
        if not bool(getattr(self.state, "auto_hunt", False)):
            self.state.auto_hunt = True
            changed.append("auto_hunt")
        if not bool(getattr(self.state, "sentinel_enabled", False)):
            self.state.sentinel_enabled = True
            changed.append("sentinel")

        busy_is_stale = (
            bool(getattr(self.state, "is_combat_busy", False))
            and not self._is_support_phase_active()
            and not bool(getattr(self, "_warrior_debuff_active", False))
            and not bool(getattr(self, "_death_recovery_active", False))
            and time.time() >= float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0)
        )
        if busy_is_stale:
            self._set_combat_busy(False)
            changed.append("combat_busy")

        if changed:
            now = time.time()
            if now - float(getattr(self, "_last_dosa_f2_watchdog_log_time", 0.0) or 0.0) >= 1.0:
                print(f"[F2Watchdog] restored: {','.join(changed)}")
                self._last_dosa_f2_watchdog_log_time = now

    def _restore_dosa_service_if_follow_autohunt(self) -> bool:
        if not is_support_role(getattr(self.state, "network_role", "") or self.state.role):
            return False
        if bool(getattr(self.state, "service_active", False)):
            return True
        if not (
            bool(getattr(self.state, "auto_hunt", False))
            and bool(getattr(self.state, "nav_follow_enabled", False))
        ):
            return False
        self.state.service_active = True
        self.state.control_mode = "FOLLOW+SERVICE"
        self.state.sentinel_enabled = True
        now = time.time()
        if now - float(getattr(self, "_last_dosa_f2_watchdog_log_time", 0.0) or 0.0) >= 1.0:
            print("[F2Watchdog] restored: service_active")
            self._last_dosa_f2_watchdog_log_time = now
        return True

    def _log_dosa_f2_idle_reason(self, reason: str, interval: float = 1.0):
        now = time.time()
        if now - float(getattr(self, "_last_dosa_f2_idle_reason_log_time", 0.0) or 0.0) < max(0.2, float(interval)):
            return
        self._last_dosa_f2_idle_reason_log_time = now
        print(
            f"[F2Idle] {reason} | "
            f"service={bool(getattr(self.state, 'service_active', False))} "
            f"follow={bool(getattr(self.state, 'nav_follow_enabled', False))} "
            f"auto_hunt={bool(getattr(self.state, 'auto_hunt', False))} "
            f"busy={bool(getattr(self.state, 'is_combat_busy', False))} "
            f"block_left={max(0.0, float(getattr(self.state, 'support_input_blocked_until', 0.0) or 0.0) - time.time()):.2f}s"
        )

    def _is_portal_support_paused(self) -> bool:
        return (
            bool(getattr(self.state, "portal_follow_active", False))
            or time.time() < float(getattr(self, "_portal_support_pause_until", 0.0) or 0.0)
        )

    def _force_portal_esc_clear(self) -> None:
        try:
            hw.send_force("RELEASE_ALL")
        except Exception:
            pass
        for _ in range(2):
            hw.send_force("D:esc")
            humanized_sleep(0.008, variance=0.02)
            hw.send_force("U:esc")
            humanized_sleep(0.014, variance=0.03)

    def _handle_warrior_transition_immediate_clear(self, support_target: dict | None) -> bool:
        if not isinstance(support_target, dict):
            return False
        try:
            cur_x = int(support_target.get("x", support_target.get("pos_x", 0)) or 0)
            cur_y = int(support_target.get("y", support_target.get("pos_y", 0)) or 0)
        except Exception:
            return False

        prev = self._service_last_warrior_coord
        prev_dir = normalize_move_dir(self._service_last_warrior_dir)
        prev_step = normalize_move_dir(self._service_last_warrior_step_dir)
        dps_dir = normalize_move_dir(support_target.get("last_move_dir", ""))
        map_sig = (
            str(support_target.get("map_name", "") or ""),
            str(support_target.get("map_floor", "") or ""),
            str(support_target.get("current_map", "") or ""),
            str(support_target.get("current_floor", "") or ""),
        )
        map_info_change_seq = int(support_target.get("map_info_change_seq", 0) or 0)
        map_info_changed = map_info_change_seq > int(self._service_last_map_info_change_seq or 0)
        if map_info_changed:
            self._service_last_map_info_change_seq = map_info_change_seq
        prev_map_sig = self._service_last_warrior_map_sig
        cur_ok = is_plausible_map_coord(cur_x, cur_y)
        event_prev = None
        event_dir = None
        event_seq = 0
        event_age = None
        event_data = support_target.get("coord_transition")
        if isinstance(event_data, dict):
            try:
                event_seq = int(event_data.get("seq", 0) or support_target.get("coord_transition_seq", 0) or 0)
                event_ts = float(event_data.get("ts", 0.0) or 0.0)
                if event_ts > 0.0:
                    event_age = max(0.0, time.time() - event_ts)
                event_from = event_data.get("from") or []
                event_x = int(event_from[0])
                event_y = int(event_from[1])
                if (
                    event_seq > int(self._service_last_coord_transition_seq or 0)
                    and is_plausible_transition_coord(event_x, event_y)
                    and (event_age is None or event_age <= 0.75)
                ):
                    event_prev = (event_x, event_y)
                    event_dir = normalize_move_dir(event_data.get("dir") or event_data.get("input_dir"))
                    self._service_last_coord_transition_seq = event_seq
            except Exception:
                event_prev = None
                event_dir = None
                event_age = None

        # Already following a portal: keep tracking only, never overwrite target mid-run.
        if bool(getattr(self.state, "portal_follow_active", False)):
            if cur_ok:
                if prev and (prev[0], prev[1]) != (cur_x, cur_y):
                    step = dir_between_coords(prev[0], prev[1], cur_x, cur_y)
                    if follow_manhattan_gap(prev[0], prev[1], cur_x, cur_y) == 1 and step:
                        self._service_last_warrior_step_dir = step
                    self._service_warrior_trail.append((prev[0], prev[1], prev_step or prev_dir))
                    if len(self._service_warrior_trail) > 12:
                        self._service_warrior_trail = self._service_warrior_trail[-12:]
                self._service_last_warrior_coord = (cur_x, cur_y)
                self._service_last_warrior_map_sig = map_sig
                if dps_dir:
                    self._service_last_warrior_dir = dps_dir
            return False

        map_changed = bool(prev and prev_map_sig and map_sig != prev_map_sig and any(map_sig))
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
        transition_prev = event_prev or prev
        transition = bool(event_prev) or (
            bool(prev)
            and is_plausible_map_coord(prev[0], prev[1])
            and (coord_jumped or map_changed or map_info_changed)
        )

        if prev and cur_ok and (prev[0], prev[1]) != (cur_x, cur_y):
            step = dir_between_coords(prev[0], prev[1], cur_x, cur_y)
            if follow_manhattan_gap(prev[0], prev[1], cur_x, cur_y) == 1 and step:
                self._service_last_warrior_step_dir = step
            self._service_warrior_trail.append((prev[0], prev[1], prev_step or prev_dir))
            if len(self._service_warrior_trail) > 12:
                self._service_warrior_trail = self._service_warrior_trail[-12:]

        if cur_ok:
            self._service_last_warrior_coord = (cur_x, cur_y)
            self._service_last_warrior_map_sig = map_sig
            if dps_dir:
                self._service_last_warrior_dir = dps_dir
        elif map_changed and prev:
            # Map changed but new coords are junk — still allow transition using prev.
            self._service_last_warrior_map_sig = map_sig

        if not transition:
            return False

        now = time.time()
        if (now - float(getattr(self, "_last_portal_immediate_clear_time", 0.0) or 0.0)) < 0.8:
            return False
        self._last_portal_immediate_clear_time = now
        self._portal_support_pause_until = now + 3.0

        # Do NOT block movement keys — only pause party heal via portal_follow_active.
        source_map_sig = prev_map_sig if prev_map_sig and any(prev_map_sig) else map_sig
        cached_hint = {}
        if isinstance(getattr(self.state, "portal_session_cache", None), dict):
            cached_hint = dict(self.state.portal_session_cache.get(source_map_sig, {}) or {})
        cached_dir = normalize_move_dir(cached_hint.get("enter_dir"))
        enter_dir = event_dir or prev_step or getattr(self, "_service_last_warrior_step_dir", None) or cached_dir
        if not enter_dir:
            if cur_ok:
                self._service_last_warrior_coord = (cur_x, cur_y)
                self._service_last_warrior_map_sig = map_sig
            print(
                f"[PortalFollow] transition waiting for stable step dir: "
                f"now=({cur_x}, {cur_y}) event_age={event_age if event_age is not None else '-'} "
                f"map={prev_map_sig}->{map_sig}"
            )
            return False
        warrior_last = (int(transition_prev[0]), int(transition_prev[1]))
        portal_xy = offset_coord_by_dir(warrior_last[0], warrior_last[1], enter_dir)
        self.state.portal_follow_active = True
        self.state.portal_follow_retarget_requested = True
        self.state.portal_follow_started_at = time.time()
        self.state.portal_follow_finished_at = 0.0
        self.state.portal_follow_coord = warrior_last
        self.state.portal_follow_approach = warrior_last
        self.state.portal_follow_dir = enter_dir
        try:
            self.state.red_tab_enabled = False
        except Exception:
            pass
        self._party_direct_heal_target_prepared = False
        self._party_direct_heal_verified = False
        self._warrior_redtab_verified = False

        self._force_portal_esc_clear()
        print(
            "[PortalFollow] immediate clear on warrior transition: "
            f"warrior_last={warrior_last}, portal={portal_xy}, now=({cur_x}, {cur_y}), "
            f"dir={enter_dir or '-'}, step_dir={prev_step or '-'}, "
            f"coord_jump={coord_jumped}, map_changed={map_changed}, "
            f"event_seq={event_seq or '-'}, map={prev_map_sig}->{map_sig}"
        )
        return True

    def _run_dosa_service_cycle(self, support_target: dict | None) -> bool:
        """
        ?? ??? ?? ??.
        ?? ??/?? ??? ?? ????, ?? ?? red_tab ?? ???? ????.
        """
        if not self._restore_dosa_service_if_follow_autohunt():
            return False

        self._repair_dosa_f2_runtime_state()

        if not self._is_hw_ready():
            try:
                hw.auto_reconnect()
            except Exception:
                pass
            now = time.time()
            if now - self._last_hw_skip_log_time >= 2.0:
                print("[Support] hardware not ready. Skip service loop.")
                self._last_hw_skip_log_time = now
            self._log_dosa_f2_idle_reason("hardware_not_ready")
            self._pace_service_loop("idle")
            return True

        if self._handle_warrior_transition_immediate_clear(support_target):
            self._pace_service_loop("active")
            return True

        if bool(getattr(self.state, "portal_follow_active", False)):
            self._portal_support_pause_until = max(
                float(getattr(self, "_portal_support_pause_until", 0.0) or 0.0),
                time.time() + 0.4,
            )
            self._invalidate_warrior_redtab_verification()
            self._log_dosa_f2_idle_reason("portal_follow_active stop_party_heal", interval=0.4)
            self._pace_service_loop("active")
            return True

        if isinstance(support_target, dict):
            map_sync_status = classify_map_sync(self.state.get_all(), support_target, now=time.time())
            self.state.map_sync_status = map_sync_status
            if map_sync_status != "same":
                self._invalidate_warrior_redtab_verification()
                self._log_dosa_f2_idle_reason(
                    f"map_sync_{map_sync_status}: defer_party_heal",
                    interval=0.5,
                )
                self._pace_service_loop("active")
                return True

        if not self._restore_game_window_focus_for_support():
            now = time.time()
            if now - self._last_focus_skip_log_time >= 2.0:
                print("[Support] game window inactive. Continue F2 with force hardware input.")
                self._last_focus_skip_log_time = now
            self._log_dosa_f2_idle_reason("game_window_inactive_force_input")

        if self._handle_initial_direct_heal_prepare_request():
            self._pace_service_loop("active")
            return True

        if self._handle_box_collision_reprepare_request():
            self._pace_service_loop("active")
            return True

        if bool(getattr(self, "_warrior_debuff_active", False)):
            self._pace_service_loop("active")
            return True

        if time.time() < float(getattr(self, "_post_debuff_follow_until", 0.0) or 0.0):
            self._pace_service_loop("active")
            return True
        if bool(getattr(self, "_post_debuff_recover_prepare_pending", False)):
            self._post_debuff_recover_prepare_pending = False
            self.request_initial_direct_heal_target_prepare()
            self._pace_service_loop("active")
            return True

        if self._update_zero_hp_state():
            self._handle_zero_hp_recovery()
            self._pace_service_loop("active")
            return True

        if int(getattr(self.state, "hp", 0) or 0) <= 0:
            self.state.last_self_buff_time = 0.0
            self.state.last_self_gg_cast_time = 0.0

        # 1) ?먭? ?앹〈? ??긽 ?곗꽑
        good_hp = self._get_good_hp_threshold()
        current_self_hp = int(getattr(self.state, "hp", 0) or 0)
        if current_self_hp >= good_hp or current_self_hp <= int(getattr(self, "_mp_stationary_self_heal_threshold", 50000) or 50000):
            self._mp_follow_self_heal_until = 0.0
        party_hp_needed_before_self_recover = bool(
            support_target and self._needs_hp_recovery_for(support_target)
        )
        if support_target and should_prioritize_self_mp_over_party_heal(
            int(getattr(self.state, "mp", 0) or 0),
            int(support_target.get("hp", 0) or 0),
            self._self_mp_priority_threshold,
            warrior_critical_hp=30000,
        ):
            if self._handle_self_mp_priority(support_target):
                self._pace_service_loop("active")
                return True

        if (
            current_self_hp > 0
            and current_self_hp < good_hp
            and current_self_hp > int(getattr(self, "_mp_stationary_self_heal_threshold", 50000) or 50000)
            and time.time() < float(getattr(self, "_mp_follow_self_heal_until", 0.0) or 0.0)
        ):
            now_micro_heal = time.time()
            if (now_micro_heal - float(getattr(self, "_last_mp_follow_self_heal_tick", 0.0) or 0.0)) >= 0.22:
                self._last_mp_follow_self_heal_tick = now_micro_heal
                self._cast_self_hp_micro_follow_tick()
            self._log_dosa_f2_idle_reason(
                f"post_mp_micro_self_heal self_hp={current_self_hp}/{good_hp}",
                interval=0.5,
            )
            self._pace_service_loop("active")
            return True

        if (
            party_hp_needed_before_self_recover
            and current_self_hp > self._self_hp_emergency_threshold
            and not self._is_party_heal_blocked()
        ):
            self._cast_due_periodic_party_support(support_target)
            if self._recover_party_hp(support_target):
                self._party_hp_next_tick_interval = self._roll_party_hp_tick_interval(True)
                self._log_dosa_f2_idle_reason(
                    f"party_heal_prioritized_over_self_recover self_hp={current_self_hp}/{good_hp}",
                    interval=1.0,
                )
                self._pace_service_loop("active")
                return True

        if current_self_hp > 0 and current_self_hp < good_hp:
            if party_hp_needed_before_self_recover and current_self_hp > self._self_hp_emergency_threshold:
                self._log_dosa_f2_idle_reason(
                    f"self_recover_deferred_for_party_heal self_hp={current_self_hp}/{good_hp}",
                    interval=1.0,
                )
                self._pace_service_loop("active")
                return True
            recovered = self._recover_self_hp_until_good_hp(
                reason_log="[Recovery] self low HP sustain recovery.",
                support_block_duration=1.8,
                max_attempts=6,
            )
            if recovered:
                self._complete_self_hp_recovery_reengage(
                    source_log="[Recovery] self low-HP recovery complete."
                )
            elif self.state.role == "도사" and self.state.service_active:
                self.request_initial_direct_heal_target_prepare()
            self._pace_service_loop("active")
            return True

        # 2) 寃⑹닔 ?곗씠?곌? ?놁쑝硫?吏??猷⑦봽 ????湲?(遺덊븘?뷀븳 gg/bm ?고? 諛⑹?)
        if not support_target:
            self._log_dosa_f2_idle_reason("no_warrior_telemetry")
            if self._handle_self_mp_priority(None):
                self._pace_service_loop("active")
                return True
            if self._needs_mp_recovery():
                self._execute_self_mp_recovery_and_retarget(
                    None,
                    source_log="[Recovery] self MP recovery without warrior telemetry complete.",
                )
                self._pace_service_loop("active")
                return True
            self._pace_service_loop("no_target")
            return True

        # 3) 寃⑹닔 吏???곗꽑
        if int(support_target.get("hp", 0) or 0) <= 0:
            self.state.last_warrior_bomu_time = 0.0

        now_dbg = time.time()
        last_dbg = getattr(self, '_last_cycle_debug_time', 0)
        if now_dbg - last_dbg > 3.0:
            self._last_cycle_debug_time = now_dbg
            needs_hp_dbg = self._needs_hp_recovery_for(support_target)
            hp_dbg = int(support_target.get('hp', 0) or 0)
            good_hp_dbg = self._get_party_good_hp_threshold(support_target)
            follow_dbg = bool(getattr(self.state, "nav_follow_enabled", False))
            busy_dbg = bool(getattr(self.state, "is_combat_busy", False))
            print(
                f"[CycleDBG] 서비스루프=True, 따라가기={follow_dbg}, busy={busy_dbg}, "
                f"힐필요={needs_hp_dbg}, HP={hp_dbg}/{good_hp_dbg}"
            )

        needs_hp = self._needs_hp_recovery_for(support_target)
        needs_mp = self._needs_mp_recovery_for(support_target)
        warrior_hp_for_mp_priority = int(support_target.get("hp", 0) or 0)
        try:
            warrior_x = int(support_target.get("x", support_target.get("pos_x", 0)) or 0)
            warrior_y = int(support_target.get("y", support_target.get("pos_y", 0)) or 0)
            warrior_distance = follow_manhattan_gap(
                warrior_x,
                warrior_y,
                int(getattr(self.state, "x", 0) or 0),
                int(getattr(self.state, "y", 0) or 0),
            )
        except Exception:
            warrior_distance = 0
        follow_distance_risk = (
            bool(getattr(self.state, "nav_follow_enabled", False))
            and should_prioritize_follow_distance(warrior_distance, risk_distance=7)
        )
        portal_follow_active = bool(getattr(self.state, "portal_follow_active", False))
        portal_retarget_requested = bool(getattr(self.state, "portal_follow_retarget_requested", False))

        if portal_follow_active:
            self._invalidate_warrior_redtab_verification()
            self._log_dosa_f2_idle_reason(
                f"portal_follow_active defer_warrior_support distance={warrior_distance}",
                interval=0.5,
            )
            self._pace_service_loop("active")
            return True

        if portal_retarget_requested:
            if warrior_distance > 4:
                self._invalidate_warrior_redtab_verification()
                self._log_dosa_f2_idle_reason(
                    f"portal_retarget_wait distance={warrior_distance}",
                    interval=0.5,
                )
                self._pace_service_loop("active")
                return True
            self._invalidate_warrior_redtab_verification()
            if not self._prepare_direct_tab_heal_target():
                self._log_dosa_f2_idle_reason("portal_retarget_prepare_failed", interval=0.6)
                self._pace_service_loop("active")
                return True
            self._party_direct_heal_target_prepared = True
            self._last_party_hp_support_time = 0.0
            self.state.portal_follow_retarget_requested = False
            print(f"[PortalFollow] warrior red_tab restored after portal: distance={warrior_distance}")

        if needs_hp:
            if follow_distance_risk and int(support_target.get("hp", 0) or 0) > 30000:
                self._invalidate_warrior_redtab_verification()
                self._log_dosa_f2_idle_reason(
                    f"follow_distance_priority distance={warrior_distance} defer_warrior_heal",
                    interval=0.7,
                )
                self._pace_service_loop("active")
                return True
            if self._is_party_heal_blocked():
                now_blocked = time.time()
                if now_blocked - float(getattr(self, "_last_party_heal_blocked_log_time", 0.0) or 0.0) >= 1.0:
                    self._last_party_heal_blocked_log_time = now_blocked
                    print(
                        "[Support] warrior HP heal blocked: "
                        f"phase={getattr(self, '_support_phase', 'idle')} "
                        f"party_block_left={max(0.0, float(getattr(self, '_party_heal_blocked_until', 0.0) or 0.0) - now_blocked):.2f}s"
                    )
                self._pace_service_loop("active")
                return True
            hp_val = int(support_target.get('hp', 0) or 0)
            good_hp_remote = self._get_party_good_hp_threshold(support_target)
            remote_heal_request = bool(support_target.get("heal_request", False))
            now_support = time.time()
            self._mark_support_party_heal_follow_guard(duration=0.90)
            rapid_heal = remote_heal_request or (good_hp_remote > 0 and hp_val <= int(good_hp_remote * 0.55))
            hp_tick_interval = float(getattr(self, "_party_hp_next_tick_interval", 0.24) or 0.24)
            if rapid_heal:
                hp_tick_interval = min(hp_tick_interval, float(self._party_hp_tick_fast_interval_max))
            if (now_support - self._last_party_hp_support_time) < hp_tick_interval:
                self._pace_service_loop("active")
                return True
            if (now_support - self._last_party_hp_check_log_time) >= 1.0:
                self._last_party_hp_check_log_time = now_support
                cast_mode = "direct 3 verified" if self._party_direct_heal_verified else "esc>tab>tab acquire"
                print(f"[Support] HP check: {hp_val} / {good_hp_remote} -> {cast_mode}")
            self._cast_due_periodic_party_support(support_target, now_support=now_support)
            casted_hp = self._recover_party_hp(support_target)
            if casted_hp:
                self._party_hp_next_tick_interval = self._roll_party_hp_tick_interval(rapid_heal)
                self._last_party_hp_value = hp_val
            if (not rapid_heal) and self._handle_self_mp_priority(support_target):
                self._pace_service_loop("active")
                return True
            self._pace_service_loop("active")
            return True

        if needs_mp:
            self._recover_party_mp(support_target)
            self._pace_service_loop("active")
            return True

        # 4) 吏???ъ쑀 援ш컙?먯꽌留??먭? ?곹깭/踰꾪봽 ?먭?
        remote_hp = int(support_target.get("hp", 0) or 0)
        remote_good_hp = self._get_party_good_hp_threshold(support_target)
        support_safe = remote_hp > max(1, remote_good_hp + 30000)
        follow_repositioning = self._is_follow_reposition_needed()
        self._log_dosa_f2_idle_reason(
            f"warrior_heal_not_needed hp={remote_hp}/{remote_good_hp} follow_repositioning={follow_repositioning}",
            interval=2.0,
        )

        if not follow_repositioning and self._handle_self_mp_priority(support_target):
            self._pace_service_loop("active")
            return True

        if support_safe and not follow_repositioning:
            if self._execute_warrior_debuff_cycle(support_target):
                self._pace_service_loop("active")
                return True
            self._refresh_self_status_if_needed()
            if self._execute_self_support_buffs_cycle():
                self._pace_service_loop("active")
                return True
            if self._needs_mp_recovery():
                self._execute_self_mp_recovery_and_retarget(
                    support_target,
                    source_log="[Recovery] self MP recovery during safe support complete.",
                )
                self._pace_service_loop("active")
                return True

        # 5) 蹂대Т 二쇨린 ?좎?
        if self._execute_warrior_bomu_cycle(support_target):
            self._pace_service_loop("active")
            return True

        self._log_dosa_f2_idle_reason(
            f"safe_idle hp={remote_hp}/{remote_good_hp} follow_needed={follow_repositioning}",
            interval=1.5,
        )
        self._pace_service_loop("idle")
        return True

    def _self_buff_cycle(self):
        """?? ??? ???: ?? ?? ???."""
        return self._execute_self_support_buffs_cycle()


    
    def _cast_spell_by_name(self, spell_name: str):
        """스킬 이름으로 스킬 캐스팅."""
        print(f"[DEBUG] _cast_spell_by_name ?몄텧: {spell_name}")
        try:
            from bis_spell import spell_caster, target_mapper
            import json
            
            # spells_config.json?먯꽌 ?ㅽ궗 ?뺣낫 李얘린
            with open('spells_config.json', 'r', encoding='utf-8') as f:
                spells = json.load(f)
            
            for spell in spells:
                if spell.get('name') == spell_name and spell.get('use'):
                    spell_char = spell.get('spell_char', '')
                    cast_function = spell.get('cast_function', 'SpellEnter')
                    
                    print(f"[DEBUG] ?ㅽ궗 ?뺣낫 李얠쓬: {spell_name}, char='{spell_char}', func={cast_function}")
                    
                    if spell_char:
                        print(f"[DEBUG] ?ㅽ궗 ?쒖쟾 ?쒖옉: {spell_name}")
                        caster = spell_caster
                        # 罹먯뒪???⑥닔 ?몄텧
                        if cast_function == 'SpellEnter':
                            caster.spell_enter(spell_char)
                        elif cast_function == 'SpellHomeEnter':
                            caster.spell_home_enter(spell_char)
                        elif cast_function == 'SpellArrowEnter':
                            caster.spell_arrow_enter(spell_char)
                        elif cast_function == 'SpellClickEnter':
                            caster.spell_click_enter(spell_char)
                        print(f"[Logic] Casted spell: {spell_name} ({cast_function})")
                    else:
                        print(f"[ERROR] ?ㅽ궗 臾몄옄 ?놁쓬: {spell_name}")
                    break
            else:
                print(f"[ERROR] ?ㅽ궗 李얠? 紐삵븿 ?먮뒗 use=false: {spell_name}")
        except Exception as e:
            print(f"[ERROR] Failed to cast spell {spell_name}: {e}")

    def _handle_combat(self):
        """?꾪닾 泥섎━ (?곗꽑?쒖쐞 1)"""
        # ?꾪닾 以묒뿉??is_combat_busy ?뚮옒洹??ㅼ젙
        self._set_combat_busy(True)

        role_name = str(getattr(self.state, "role", "") or "").strip()
        network_role = str(getattr(self.state, "network_role", "") or "").strip()
        is_warrior = (
            role_name in {"격수", "Warrior", "寃⑹닔"}
            or network_role in {"격수", "Warrior", "寃⑹닔"}
        )

        # ??븷蹂??꾪닾 濡쒖쭅
        if is_warrior:
            self._run_dps_server_mode()
        elif self.state.role in ("도사", "도사1"):
            self._run_support_mode()
        elif self.state.role == "술사":
            self._run_priest_mode()
        else:
            self._run_default_mode()

        if not self._should_handle_combat():
            self._set_combat_busy(False)

    def _handle_moving(self):
        """?대룞/?ㅻ퉬寃뚯씠??泥섎━ (?곗꽑?쒖쐞 2)"""
        # ?꾪닾 以묒씠 ?꾨땺 ?뚮쭔 ?대룞
        if not self.state.is_combat_busy:
            # Route mode on F2 should always keep warrior attack/loot loop alive.
            if getattr(self.state, "service_active", False) and getattr(self.state, "nav_route_enabled", False):
                self._run_warrior_attack_loot_cycle()
                return
            # ?대룞 濡쒖쭅? RouteSvc?먯꽌 泥섎━
            role_name = str(getattr(self.state, "role", "") or "").strip()
            network_role = str(getattr(self.state, "network_role", "") or "").strip()
            if (
                role_name in {"격수", "Warrior", "寃⑹닔"}
                or network_role in {"격수", "Warrior", "寃⑹닔"}
            ):
                self._run_warrior_attack_loot_cycle()

    # ----------------------------------------------------------
    # ?? 寃⑹닔 紐⑤뱶: ?곗씠??怨듭쑀 ?쒕쾭 ??븷留??????????????????????
    # ----------------------------------------------------------
    def _run_dps_server_mode(self):
        """격수 모드: 데이터 송신 + 이동 중 3/0 교대 자동사냥."""
        # Keep warrior loop alive while F2 service mode is active.
        if not bool(getattr(self.state, "service_active", False)):
            self.state.last_bomu_time    = 0
            self.state.combat_start_time = 0.0
            humanized_sleep(TIMING_CONFIG["idle_sleep"])
            return

        # Keep network/status sharing and add warrior-specific attack/loot cycle.
        
        # ?앹〈 理쒖슦??(HP/MP ?뚮났) - 寃⑹닔???먭? ?뚮났 ?꾩슂
        if self._needs_hp_recovery():
            self._recover_hp()
            self.state.combat_start_time = 0.0

        if self._needs_mp_recovery():
            self._recover_mp()
            if self._needs_hp_recovery():
                self._recover_hp()

        # 蹂대Т 踰꾪봽 ?좎? (185s 二쇨린)
        if time.time() - self.state.last_bomu_time > TIMING_CONFIG["bomu_interval"]:
            self._execute_bomu_buff_v3()

        # ?붾쾭???ㅼ틪 (寃⑹닔???꾩슂)
        dist_change = (abs(self.state.x - self.state.last_debuff_x) +
                       abs(self.state.y - self.state.last_debuff_y))
        if self.state.auto_debuff_enabled and dist_change > 15:
            self._execute_debuff_scan_v3()

        self._run_warrior_attack_loot_cycle()
        humanized_sleep(TIMING_CONFIG["action_loop"])

    def _run_warrior_attack_loot_cycle(self):
        """격수 전용: 3은 짧게 반복, 0은 긴 랜덤 쿨타임으로 독립 관리."""
        # Route thread may keep moving even if auto_hunt flips false transiently.
        # For warrior F2 mode, run loop based on service_active.
        if not bool(getattr(self.state, "service_active", False)):
            return
        now = time.time()
        attack_due = now >= float(getattr(self, "_warrior_next_attack_at", 0.0) or 0.0)
        loot_due = now >= float(getattr(self, "_warrior_next_loot_at", 0.0) or 0.0)
        if not attack_due and not loot_due:
            return

        # Loot has lower frequency but should not starve behind the attack loop.
        if loot_due:
            if not self._is_hw_ready():
                self._warrior_next_loot_at = now + 0.10
                return
            try:
                hw.force_press("0", variance=0.08)
                pressed = True
            except Exception:
                pressed = False
            if not pressed:
                self._warrior_next_loot_at = now + 0.10
                return
            self._warrior_next_loot_at = now + random.uniform(2.0, 14.0)
            if now - self._last_warrior_cycle_log_time >= 1.0:
                print(f"[WarriorLoop] cast=0 next_loot={max(0.0, self._warrior_next_loot_at - now):.2f}s")
                self._last_warrior_cycle_log_time = now
            return

        pressed = self._press_hw_key("3", variance=0.08, skip_focus_guard=True)
        if not pressed:
            self._warrior_next_attack_at = now + 0.10
            return
        self._warrior_next_attack_at = now + random.uniform(0.3, 0.5)
        if now - self._last_warrior_cycle_log_time >= 1.0:
            print(f"[WarriorLoop] cast=3 next_attack={max(0.0, self._warrior_next_attack_at - now):.2f}s")
            self._last_warrior_cycle_log_time = now

    def _is_sulsa_role(self) -> bool:
        return str(getattr(self.state, "role", "") or "").strip() == "술사"

    def _set_sulsa_input_busy(self, duration: float = 0.35) -> bool:
        if not self._is_hw_ready():
            now = time.time()
            if now - self._last_hw_skip_log_time >= 2.0:
                print("[Sulsa] hardware not ready.")
                self._last_hw_skip_log_time = now
            return False
        self._set_combat_busy(True)
        self._set_support_input_block(duration)
        self._release_movement_keys_only()
        return True

    def _cast_sulsa_target_spell(self, key: str, label: str) -> bool:
        if not self._set_sulsa_input_busy(0.42):
            return False
        try:
            for press_key, delay in ((key, 0.035), ("up", 0.035), ("enter", 0.045)):
                if not self._press_hw_key(press_key, variance=0.08, skip_focus_guard=True):
                    return False
                humanized_sleep(delay, variance=0.08)
            print(f"[Sulsa] cast {label}: {key}>up>enter")
            return True
        finally:
            self._set_combat_busy(False)

    def _cast_sulsa_power_boost_once(self) -> bool:
        if not self._set_sulsa_input_busy(0.18):
            return False
        try:
            ok = self._press_hw_key("2", variance=0.08, skip_focus_guard=True)
            if ok:
                print("[Sulsa] cast 공력증강: 2")
            return ok
        finally:
            self._set_combat_busy(False)

    def _cast_sulsa_self_heal_once(self) -> bool:
        if not self._set_sulsa_input_busy(0.42):
            return False
        try:
            for key, delay in (("3", 0.035), ("home", 0.035), ("enter", 0.045)):
                if not self._press_hw_key(key, variance=0.08, skip_focus_guard=True):
                    return False
                humanized_sleep(delay, variance=0.08)
            print("[Sulsa] self heal: 3>home>enter")
            return True
        finally:
            self._set_combat_busy(False)

    def _use_sulsa_mp_item(self) -> bool:
        slots = list(getattr(self.state, "sulsa_item_slots", []) or [])
        if not slots:
            return False
        index = max(0, int(getattr(self.state, "sulsa_item_slot_index", 0) or 0))
        if index >= len(slots):
            print("[Sulsa] MP item slots exhausted.")
            return False
        slot = str(slots[index] or "").strip().lower()
        if not slot:
            return False
        if not self._set_sulsa_input_busy(0.24):
            return False
        try:
            for key, delay in (("u", 0.025), (slot, 0.040)):
                if not self._press_hw_key(key, variance=0.08, skip_focus_guard=True):
                    return False
                humanized_sleep(delay, variance=0.08)
            uses = int(getattr(self.state, "sulsa_item_slot_uses", 0) or 0) + 1
            max_uses = max(1, int(getattr(self.state, "sulsa_item_uses_per_slot", 100) or 100))
            if uses >= max_uses:
                self.state.sulsa_item_slot_index = index + 1
                self.state.sulsa_item_slot_uses = 0
                print(f"[Sulsa] MP item used: u>{slot} ({uses}/{max_uses}), next slot index={index + 1}")
            else:
                self.state.sulsa_item_slot_uses = uses
                print(f"[Sulsa] MP item used: u>{slot} ({uses}/{max_uses})")
            return True
        finally:
            self._set_combat_busy(False)

    def _run_sulsa_debuff_once_if_due(self, force: bool = False) -> bool:
        now = time.time()
        acted = False
        if force or (now - float(self._sulsa_last_paralyze_time or 0.0)) >= self._sulsa_paralyze_interval:
            if self._cast_sulsa_target_spell("5", "마비"):
                self._sulsa_last_paralyze_time = time.time()
                acted = True
        now = time.time()
        if force or (now - float(self._sulsa_last_curse_time or 0.0)) >= self._sulsa_curse_interval:
            if self._cast_sulsa_target_spell("6", "저주"):
                self._sulsa_last_curse_time = time.time()
                acted = True
        return acted

    def _run_sulsa_hellfire_cycle_if_due(self) -> bool:
        now = time.time()
        if (now - float(self._sulsa_last_hellfire_time or 0.0)) < self._sulsa_hellfire_interval:
            return False
        if not bool(getattr(self.state, "sulsa_attack_enabled", False)):
            return False
        if not self._cast_sulsa_target_spell("1", "헬파이어"):
            return False
        self._sulsa_last_hellfire_time = time.time()
        humanized_sleep(0.05, variance=0.08)
        self._use_sulsa_mp_item()
        humanized_sleep(0.05, variance=0.08)
        self._cast_sulsa_power_boost_once()
        if int(getattr(self.state, "hp", 0) or 0) < self._get_good_hp_threshold():
            humanized_sleep(0.05, variance=0.08)
            self._cast_sulsa_self_heal_once()
        return True

    def _run_sulsa_self_sustain(self) -> bool:
        current_hp = int(getattr(self.state, "hp", 0) or 0)
        current_mp = int(getattr(self.state, "mp", 0) or 0)
        if 0 < current_hp < self._get_good_hp_threshold():
            return self._cast_sulsa_self_heal_once()
        if 0 < current_mp <= self._self_mp_priority_threshold:
            if self._cast_sulsa_power_boost_once():
                if int(getattr(self.state, "hp", 0) or 0) < self._get_good_hp_threshold():
                    self._cast_sulsa_self_heal_once()
                return True
        return False

    def _run_sulsa_service_cycle(self) -> bool:
        if not self._is_sulsa_role():
            return False
        active = bool(getattr(self.state, "service_active", False)) or bool(getattr(self.state, "sulsa_debuff_standalone", False))
        if not active:
            return False
        if self._run_sulsa_self_sustain():
            self._pace_service_loop("active")
            return True
        if bool(getattr(self.state, "sulsa_debuff_enabled", False)):
            if self._run_sulsa_debuff_once_if_due(force=False):
                self._pace_service_loop("active")
                return True
        if bool(getattr(self.state, "auto_hunt", False)):
            if self._run_sulsa_hellfire_cycle_if_due():
                self._pace_service_loop("active")
                return True
        now = time.time()
        if now - float(getattr(self, "_sulsa_last_action_log_time", 0.0) or 0.0) >= 2.0:
            print(
                "[Sulsa] idle "
                f"attack={bool(getattr(self.state, 'sulsa_attack_enabled', False))} "
                f"debuff={bool(getattr(self.state, 'sulsa_debuff_enabled', False))} "
                f"follow={bool(getattr(self.state, 'nav_follow_enabled', False))}"
            )
            self._sulsa_last_action_log_time = now
        self._pace_service_loop("idle")
        return True

    # ----------------------------------------------------------
    # ?? 吏??紐⑤뱶: ?꾩궗 - 寃⑹닔 ?곗씠???섏떊 諛?吏??????????????????
    # ----------------------------------------------------------
    def _run_support_mode(self):
        """?꾩궗 紐⑤뱶: 寃⑹닔???곗씠???섏떊諛쏆븘 吏??濡쒖쭅 ?섑뻾"""
        # ?? ?먮룞?щ깷 鍮꾪솢???????????????????????????????
        if not self.state.auto_hunt:
            self.state.last_bomu_time    = 0
            self.state.combat_start_time = 0.0
            humanized_sleep(TIMING_CONFIG["idle_sleep"])
            return

        # ?? 吏??濡쒖쭅 ?섑뻾 ???????????????????????????????
        # ?꾩궗??寃⑹닔???寃잛쓣 ?곕씪媛硫?吏??
        
        # ?앹〈 理쒖슦??(HP/MP ?뚮났)
        if self._needs_hp_recovery():
            self._recover_hp()
            self.state.combat_start_time = 0.0

        if self._needs_mp_recovery():
            self._recover_mp()
            if self._needs_hp_recovery():
                self._recover_hp()

        # 蹂대Т 踰꾪봽 ?좎? (185s 二쇨린)
        if time.time() - self.state.last_bomu_time > TIMING_CONFIG["bomu_interval"]:
            self._execute_bomu_buff_v3()

        # ?붾쾭???ㅼ틪
        dist_change = (abs(self.state.x - self.state.last_debuff_x) +
                       abs(self.state.y - self.state.last_debuff_y))
        if self.state.auto_debuff_enabled and dist_change > 15:
            self._execute_debuff_scan_v3()

        # ?좎? ?먯? ???+ ?뷀떚???곗꽑?쒖쐞 ?됰룞 ?쒖뼱
        if self._handle_entity_priorities():
            humanized_sleep(TIMING_CONFIG["action_loop"])
            return

        # SentinelThread 寃곌낵 蹂닿퀬: 紐ъ뒪??議댁옱 ???쒗룷???ㅽ궗 ?쒖쟾
        if self.state.monster_on_screen:
            # 寃⑹닔媛 ?寃잛쓣 ?↔퀬 ?덉쑝硫?吏???ㅽ궗 ?쒖쟾
            self._execute_support_skills()

        humanized_sleep(TIMING_CONFIG["action_loop"])

    def _get_priest_monsters(self) -> list[dict]:
        monsters = self.state.entities.get("monsters", [])
        return monsters if isinstance(monsters, list) else []

    def _calc_monster_metrics(self):
        me_pos = (int(getattr(self.state, "x", 0)), int(getattr(self.state, "y", 0)))
        if me_pos[0] <= 0 and me_pos[1] <= 0:
            return None, [], 0

        valid_monsters = []
        dense_count = 0
        for m in self._get_priest_monsters():
            if not isinstance(m, dict):
                continue
            monster_pos = m.get("world_pos") or m.get("pos")
            if not monster_pos or len(monster_pos) != 2:
                grid = m.get("grid")
                if not grid or len(grid) != 2:
                    continue
                dx = int(grid[0]) - int(self.state.char_grid[0])
                dy = int(grid[1]) - int(self.state.char_grid[1])
            else:
                dx = int(monster_pos[0]) - me_pos[0]
                dy = int(monster_pos[1]) - me_pos[1]
            dist = (dx * dx + dy * dy) ** 0.5
            valid_monsters.append({"monster": m, "dist": dist, "dx": dx, "dy": dy})
            if max(abs(dx), abs(dy)) <= 1:
                dense_count += 1
        return me_pos, valid_monsters, dense_count

    def _get_priest_engage_metrics(self, engage_range: int | None = None):
        """
        ?좎궗 泥⑥궗?μ? 媛源뚯슫 紐ъ뒪?곕쭔 ?꾪닾 吏꾩엯 ??곸쑝濡?蹂몃떎.
        engage_range??pos x/y 湲곗? 泥대퉬?쇳봽 嫄곕━??
        """
        if engage_range is None:
            engage_range = max(0, int(getattr(self.state, "priest_engage_range", 2)))
        me_grid, monster_infos, _dense_count = self._calc_monster_metrics()
        if me_grid is None:
            return None, [], 0

        close_infos = [
            info for info in monster_infos
            if max(abs(info["dx"]), abs(info["dy"])) <= engage_range
        ]
        close_dense_count = sum(
            1 for info in close_infos
            if max(abs(info["dx"]), abs(info["dy"])) <= 1
        )
        return me_grid, close_infos, close_dense_count

    def _find_ready_attack_skill(self, aoe_mode: bool):
        attack_skills = [s for s in self.state.spells if s.category == "怨듦꺽" and s.is_ready()]
        if not attack_skills:
            return None

        if aoe_mode:
            for s in attack_skills:
                if "천" in s.name:
                    return s
            return attack_skills[0]

        for s in attack_skills:
            if "격" in s.name or "단일" in s.name:
                return s
        for s in attack_skills:
            if "천" not in s.name:
                return s
        return attack_skills[0]

    def _cast_attack_skill(self, skill):
        hw.humanized_press(skill.hotkey)
        humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
        skill.last_cast_time = time.time()

    # ----------------------------------------------------------
    # ?? ?좎궗 紐⑤뱶: 泥⑥궗??濡쒖쭅 ????????????????????????????????
    # ----------------------------------------------------------
    def _run_priest_mode(self):
        """?좎궗 紐⑤뱶: 泥⑥궗??濡쒖쭅 ?섑뻾"""
        # ?? ?먮룞?щ깷 鍮꾪솢???????????????????????????????
        if not self.state.auto_hunt:
            self.state.last_bomu_time    = 0
            self.state.combat_start_time = 0.0
            humanized_sleep(TIMING_CONFIG["idle_sleep"])
            return

        # ?? 泥⑥궗??濡쒖쭅 ?섑뻾 ????????????????????????????
        # ?좎궗???낆옄?곸쑝濡??щ깷 ?섑뻾
        
        # 蹂대Т 踰꾪봽 ?좎? (185s 二쇨린)
        if time.time() - self.state.last_bomu_time > TIMING_CONFIG["bomu_interval"]:
            self._execute_bomu_buff_v3()

        # ?앹〈 理쒖슦??(HP/MP ?뚮났)
        if self._needs_hp_recovery():
            self._recover_hp()
            self.state.combat_start_time = 0.0

        if self._needs_mp_recovery():
            self._recover_mp()
            if self._needs_hp_recovery():
                self._recover_hp()

        # ?좎? ?먯? ???+ ?뷀떚???곗꽑?쒖쐞 ?됰룞 ?쒖뼱
        if self._handle_entity_priorities():
            humanized_sleep(TIMING_CONFIG["action_loop"])
            return

        # ?꾩씠???띾뱷 (?좏쑕 ?곹깭???뚮쭔)
        if not self.state.target_locked and self.state.detected_item_grid:
            gx, gy = self.state.detected_item_grid
            print(f"[Item] ?꾩씠???띾뱷 ?쒕룄: {self.state.detected_item_name} @ Grid({gx}, {gy})")

            while self.state.running and self.state.auto_hunt and self.state.detected_item_grid:
                if self.state.target_locked or self._needs_hp_recovery() or self._needs_mp_recovery():
                    print("[Action] ?띾뱷 以??꾪닾/?꾧툒 ?곹솴 諛쒖깮 -> 猷⑦똿 以묐떒.")
                    break

                # ?? ?뚰뵾 ?대룞 (Stuck 媛먯?) ?듯빀 ??
                if self._check_stuck():
                    self._escape_stuck(rewind_waypoint=False)
                    humanized_sleep(TIMING_CONFIG["move_hold"])
                    continue

                arrived = self._move_toward_grid(gx, gy)
                if arrived:
                    print(f"[Point] ?꾩씠?????꾩갑. ?띾뱷 ?쒕룄 (',')")
                    hw.humanized_press(",") 
                    humanized_sleep(TIMING_CONFIG["enter_wait"])
                    self.state.detected_item_grid = None
                    print("[OK] ?꾩씠???띾뱷 ?꾨즺.")
                    break
                
                humanized_sleep(TIMING_CONFIG["nav_loop"])

        # ?붾쾭???ㅼ틪
        dist_change = (abs(self.state.x - self.state.last_debuff_x) +
                       abs(self.state.y - self.state.last_debuff_y))
        if self.state.auto_debuff_enabled and dist_change > 15:
            self._execute_debuff_scan_v3()

        me_grid, monster_infos, dense_count = self._get_priest_engage_metrics()
        has_monsters = len(monster_infos) > 0
        if has_monsters:
            aoe_mode = dense_count >= 2
            # ?⑥씪 ?寃??ㅽ궗? ?寃??쎌씠 ?꾩슂?섎?濡?湲곗〈 ?먯깋 ?쒗???좎?
            if (not aoe_mode) and (not self.state.target_locked):
                self.state.combat_start_time = 0.0
                if self._search_target_v3():
                    humanized_sleep(TIMING_CONFIG["ocr_fast"])
                return

            skill = self._find_ready_attack_skill(aoe_mode=aoe_mode)
            if skill:
                mode_label = "愿묒뿭(泥?" if aoe_mode else "?⑥씪"
                print(f"[Priest] 怨듦꺽 遺꾧린: {mode_label} | 洹쇱젒諛吏?{dense_count} | ?덉씠?붾す={len(monster_infos)}")
                self._cast_attack_skill(skill)
            else:
                self._execute_combat()
        elif self.state.target_locked:
            humanized_sleep(TIMING_CONFIG["ocr_fast"])
            self._execute_combat()
        else:
            self._clear_target_state()
            humanized_sleep(TIMING_CONFIG["nav_loop"])
            return

        humanized_sleep(TIMING_CONFIG["action_loop"])

    # ----------------------------------------------------------
    # ?? 湲곕낯 紐⑤뱶: 湲곗〈 濡쒖쭅 ?????????????????????????????????
    # ----------------------------------------------------------
    def _run_default_mode(self):
        """湲곕낯 紐⑤뱶: 湲곗〈 濡쒖쭅 ?섑뻾"""
        # ?? ?먮룞?щ깷 鍮꾪솢???????????????????????????????
        if not self.state.auto_hunt:
            self.state.last_bomu_time    = 0
            self.state.combat_start_time = 0.0
            humanized_sleep(TIMING_CONFIG["idle_sleep"])
            return

        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        # 0. 蹂대Т 踰꾪봽 ?좎? (185s 二쇨린)
        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        if time.time() - self.state.last_bomu_time > TIMING_CONFIG["bomu_interval"]:
            self._execute_bomu_buff_v3()

        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        # 1. ?앹〈 理쒖슦??(HP / MP ?뚮났)
        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        if self._needs_hp_recovery():
            self._recover_hp()
            self.state.combat_start_time = 0.0

        if self._needs_mp_recovery():
            self._recover_mp()
            if self._needs_hp_recovery():
                self._recover_hp()

        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        # 2. ?좎? ?먯? ???+ ?뷀떚???곗꽑?쒖쐞 ?됰룞 ?쒖뼱
        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        if self._handle_entity_priorities():
            humanized_sleep(TIMING_CONFIG["action_loop"])
            return

        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        # 3. ?꾩씠???띾뱷 (?좏쑕 ?곹깭???뚮쭔)
        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        if not self.state.target_locked and self.state.detected_item_grid:
            gx, gy = self.state.detected_item_grid
            print(f"[Item] ?꾩씠???띾뱷 ?쒕룄: {self.state.detected_item_name} @ Grid({gx}, {gy})")

            while self.state.running and self.state.auto_hunt and self.state.detected_item_grid:
                if self.state.target_locked or self._needs_hp_recovery() or self._needs_mp_recovery():
                    print("[Action] ?띾뱷 以??꾪닾/?꾧툒 ?곹솴 諛쒖깮 -> 猷⑦똿 以묐떒.")
                    break

                arrived = self._move_toward_grid(gx, gy)
                if arrived:
                    print(f"[Point] ?꾩씠?????꾩갑. ?띾뱷 ?쒕룄 (',')")
                    hw.humanized_press(",") 
                    humanized_sleep(TIMING_CONFIG["enter_wait"])
                    self.state.detected_item_grid = None
                    print("[OK] ?꾩씠???띾뱷 ?꾨즺.")
                    break
                
                humanized_sleep(TIMING_CONFIG["nav_loop"])

        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        # 4. 醫뚰몴 湲곕컲 ?먮룞 ?붾쾭???ㅼ틪
        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        dist_change = (abs(self.state.x - self.state.last_debuff_x) +
                       abs(self.state.y - self.state.last_debuff_y))
        if self.state.auto_debuff_enabled and dist_change > 15:
            self._execute_debuff_scan_v3()

        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        # 5. ?寃??먯깋 & ?꾪닾
        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        has_monsters = bool(self._get_visible_monsters())
        if not self.state.target_locked:
            if not has_monsters:
                humanized_sleep(TIMING_CONFIG["nav_loop"])
                return
            self.state.combat_start_time = 0.0
            if self._search_target_v3():
                humanized_sleep(TIMING_CONFIG["ocr_fast"])
            return

        humanized_sleep(TIMING_CONFIG["ocr_fast"])
        self._execute_combat()

        humanized_sleep(TIMING_CONFIG["action_loop"])

    # ----------------------------------------------------------
    # ?? 吏???ㅽ궗 ?쒖쟾 (?꾩궗/?좎궗) ???????????????????????????
    # ----------------------------------------------------------
    def _execute_support_skills(self):
        """?꾩궗/?좎궗: 寃⑹닔???寃잛뿉 吏???ㅽ궗 ?쒖쟾"""
        # ?꾩궗/?좎궗 ?꾩슜 吏???ㅽ궗 ?쒖쟾 濡쒖쭅
        # ?? 寃⑹닔???寃잛뿉 踰꾪봽 ?ㅽ궗 ?쒖쟾, ???ㅽ궗 ?쒖쟾 ??
        # ??遺遺꾩? 媛???븷蹂??ㅽ궗 ?ㅼ젙???곕씪 ?숈옉
        pass


# ============================================================
#  Grid 醫뚰몴 蹂???⑥닔 (GridManager ?ъ슜)
# ============================================================
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
        self._last_support_quick_escape_time = 0.0
        self._follow_blocked_memory_count = 0
        self._last_support_quick_escape_dir = None
        self._last_warrior_coord: tuple[int, int] | None = None
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
        self._portal_enter_fail_streak = 0
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
        last_escape_dir = str(getattr(self, "_last_support_quick_escape_dir", "") or "")
        if len(candidate_dirs) > 1 and last_escape_dir in candidate_dirs:
            candidate_dirs = [direction for direction in candidate_dirs if direction != last_escape_dir]

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
        self._last_support_quick_escape_dir = escape_dir
        print(f"[Recover] support follow quick escape: {escape_dir} (no retarget).")
        hw.hold_move(escape_dir, "stuck_side_hold", duration=0.085)
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
                return False

        now = time.time()
        current_pos = (self.state.x, self.state.y)

        if self._nav_current_pos != current_pos:
            self.last_pos = self._nav_current_pos
            self._nav_current_pos = current_pos
            self._nav_attempt_pos = None
            self._nav_attempt_started_at = 0.0
            self.stuck_count = 0
            self._support_follow_soft_stuck_count = 0
            return False

        if self._nav_attempt_pos != current_pos:
            return False

        stuck_time_limit = float(TIMING_CONFIG.get("stuck_time", 4.0))
        if follow_navigation_active and follow_target:
            support_follow_active = should_defer_stuck_escape_for_support(
                f"{getattr(self.state, 'role', '')} {getattr(self.state, 'network_role', '')}",
                bool(getattr(self.state, "service_active", False)) or bool(getattr(self.state, "auto_hunt", False)),
                True,
            )
            if support_follow_active:
                stuck_time_limit = support_follow_stuck_timeout(True)
            else:
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
        warrior_xy, portal_xy = resolve_portal_follow_cells(warrior_xy[0], warrior_xy[1], dir_norm)
        started = float(started_at if started_at is not None else time.time())
        self._portal_follow_active = True
        self._portal_follow_approach = warrior_xy
        self._portal_follow_coord = portal_xy
        self._portal_follow_dir = dir_norm
        self._portal_follow_source_map_sig = tuple(source_map_sig) if source_map_sig else None
        self._portal_follow_started_at = started
        self._portal_enter_fail_streak = 0
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
        self.state.portal_follow_coord = portal_xy
        self.state.portal_follow_approach = warrior_xy
        self.state.portal_follow_dir = dir_norm
        try:
            self.state.red_tab_enabled = False
        except Exception:
            pass
        if clear_target:
            self._clear_target_for_portal_follow()
        print(
            f"[PortalFollow] {log_prefix}: warrior_last={warrior_xy}, "
            f"portal={portal_xy}, enter_dir={dir_norm or '-'}"
        )

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
                    self._last_coord_transition_seq = event_seq
            except Exception:
                event_prev = None
                event_dir = None
                event_age = None

        if self._portal_follow_active:
            if cur_ok:
                if prev and (prev[0], prev[1]) != (cur_x, cur_y):
                    if follow_manhattan_gap(prev[0], prev[1], cur_x, cur_y) == 1:
                        step = dir_between_coords(prev[0], prev[1], cur_x, cur_y)
                        if step:
                            self._last_warrior_step_dir = step
                self._last_warrior_coord = (cur_x, cur_y)
                self._last_warrior_map_sig = map_sig
                if dps_dir:
                    self._last_warrior_dir = dps_dir
            return

        map_changed = bool(prev and prev_map_sig and map_sig != prev_map_sig and any(map_sig))
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
            if isinstance(getattr(self.state, "portal_session_cache", None), dict):
                cached_hint = dict(self.state.portal_session_cache.get(source_map_sig, {}) or {})
            cached_dir = normalize_move_dir(cached_hint.get("enter_dir"))
            enter_dir = event_dir or prev_step or self._last_warrior_step_dir or cached_dir
            if not enter_dir:
                if cur_ok:
                    self._last_warrior_coord = (cur_x, cur_y)
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
        self._portal_enter_fail_streak = 0
        self.state.portal_follow_active = False
        self.state.portal_follow_finished_at = time.time()
        self.state.portal_follow_coord = None
        self.state.portal_follow_approach = None
        self.state.portal_follow_dir = None
        self._nav_attempt_pos = None
        self._nav_attempt_started_at = 0.0
        self.stuck_count = 0
        self._support_follow_soft_stuck_count = 0
        self.state.last_move_dir = ""
        print(f"[PortalFollow] finished: {reason}")

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
        target = self._get_portal_follow_target()
        if not target:
            return False
        current_pos = (int(self.state.x), int(self.state.y))
        warrior_last = self._portal_follow_approach or target
        enter_dir = normalize_move_dir(self._portal_follow_dir)
        _, portal_xy = resolve_portal_follow_cells(warrior_last[0], warrior_last[1], enter_dir)
        if not should_attempt_portal_enter(
            current_pos[0],
            current_pos[1],
            warrior_last[0],
            warrior_last[1],
            enter_dir=enter_dir,
            portal_x=portal_xy[0],
            portal_y=portal_xy[1],
        ):
            return False

        before_pos = current_pos
        print(
            f"[PortalFollow] enter zone: current={current_pos}, warrior_last={warrior_last}, "
            f"enter_dir={enter_dir or '-'}"
        )
        if not enter_dir:
            print("[PortalFollow] enter_dir missing; waiting on warrior last tile")
            return True

        # Force-hold the entry key on the exact warrior last tile (ignore support input block).
        try:
            self.state.support_input_blocked_until = 0.0
        except Exception:
            pass
        hold_sec = max(1.0, float(TIMING_CONFIG.get("entry_hold", 1.0) or 1.0))
        warped = False
        for attempt in range(3):
            if current_pos != warrior_last and current_pos != portal_xy:
                approach_dir = dir_between_coords(current_pos[0], current_pos[1], warrior_last[0], warrior_last[1])
                if approach_dir:
                    print(
                        f"[PortalFollow] re-approach warrior_last={warrior_last} "
                        f"from {current_pos} via {approach_dir}"
                    )
                    hw.hold_move(approach_dir, "move_hold", duration=0.18, force=True)
                    humanized_sleep(0.08, variance=0.02)
                    current_pos = (
                        int(getattr(self.state, "x", 0) or 0),
                        int(getattr(self.state, "y", 0) or 0),
                    )
                    if current_pos != warrior_last:
                        break

            before_pos = current_pos
            hw.hold_move(
                enter_dir,
                "entry_hold",
                duration=random.uniform(hold_sec, hold_sec + 0.35),
                force=True,
            )
            self.state.last_move_dir = enter_dir
            humanized_sleep(0.12, variance=0.04)
            after_pos = (
                int(getattr(self.state, "x", 0) or 0),
                int(getattr(self.state, "y", 0) or 0),
            )
            if should_detect_warrior_transition(
                before_pos[0],
                before_pos[1],
                after_pos[0],
                after_pos[1],
                jump_distance=WARRIOR_TRANSITION_JUMP_DISTANCE,
            ):
                warped = True
                cache_key = self._portal_follow_source_map_sig
                if cache_key is None:
                    cache_key = (
                        str(getattr(self.state, "map_name", "") or ""),
                        str(getattr(self.state, "map_floor", "") or ""),
                        str(getattr(self.state, "current_map", "") or ""),
                        str(getattr(self.state, "current_floor", "") or ""),
                    )
                cache = getattr(self.state, "portal_session_cache", None)
                if isinstance(cache, dict):
                    cache[cache_key] = {
                        "enter_dir": enter_dir,
                        "approach": [int(warrior_last[0]), int(warrior_last[1])],
                        "portal": [int(portal_xy[0]), int(portal_xy[1])],
                        "confirmed_at": time.time(),
                        "source": "runtime_success",
                    }
                self._finish_portal_follow(
                    f"entered self_transition before={before_pos}, after={after_pos}, attempt={attempt + 1}"
                )
                break
            current_pos = after_pos
            if current_pos != warrior_last:
                print(
                    f"[PortalFollow] left warrior_last tile while holding {enter_dir}: "
                    f"{before_pos} -> {current_pos}"
                )

        if warped:
            return True

        self._portal_enter_fail_streak = int(getattr(self, "_portal_enter_fail_streak", 0) or 0) + 1
        self._last_self_portal_coord = current_pos
        print(
            f"[PortalFollow] portal enter pending: current={current_pos}, "
            f"warrior_last={warrior_last}, fail_streak={self._portal_enter_fail_streak}"
        )
        # Stay in portal mode so we keep retrying instead of normal follow.
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

        remote_data = self.state.get_fresh_remote_data_by_role("격수")
        if not isinstance(remote_data, dict):
            return False

        target_x = int(remote_data.get("x", remote_data.get("pos_x", 0)) or 0)
        target_y = int(remote_data.get("y", remote_data.get("pos_y", 0)) or 0)
        if abs(target_x) > 300 or abs(target_y) > 300:
            return False

        follow_x = target_x
        follow_y = target_y
        current_x = int(getattr(self.state, "x", 0) or 0)
        current_y = int(getattr(self.state, "y", 0) or 0)
        follow_x, follow_y = adjust_follow_target_by_axis_gap(
            follow_x,
            follow_y,
            current_x,
            current_y,
            target_x,
            target_y,
            min_axis_gap=1,
        )
        return not should_hold_follow_position(follow_x, follow_y, current_x, current_y)

    def _repair_dosa_f2_runtime_state(self):
        if not is_support_role(getattr(self.state, "network_role", "") or self.state.role) or not bool(getattr(self.state, "service_active", False)):
            return

        changed = []
        follow_pause_active = time.time() < float(getattr(self, "_follow_pause_until", 0.0) or 0.0)
        if (not follow_pause_active) and not bool(getattr(self.state, "nav_follow_enabled", False)):
            self.state.nav_follow_enabled = True
            changed.append("follow")
        if not bool(getattr(self.state, "auto_hunt", False)):
            self.state.auto_hunt = True
            changed.append("auto_hunt")
        if not bool(getattr(self.state, "sentinel_enabled", False)):
            self.state.sentinel_enabled = True
            changed.append("sentinel")

        busy_is_stale = (
            bool(getattr(self.state, "is_combat_busy", False))
            and not self._is_support_phase_active()
            and not bool(getattr(self, "_warrior_debuff_active", False))
            and not bool(getattr(self, "_death_recovery_active", False))
            and time.time() >= float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0)
        )
        if busy_is_stale:
            self._set_combat_busy(False)
            changed.append("combat_busy")

        if changed:
            now = time.time()
            if now - float(getattr(self, "_last_dosa_f2_watchdog_log_time", 0.0) or 0.0) >= 1.0:
                print(f"[F2Watchdog] restored: {','.join(changed)}")
                self._last_dosa_f2_watchdog_log_time = now

    def _restore_dosa_service_if_follow_autohunt(self) -> bool:
        if not is_support_role(getattr(self.state, "network_role", "") or self.state.role):
            return False
        if bool(getattr(self.state, "service_active", False)):
            return True
        if not (
            bool(getattr(self.state, "auto_hunt", False))
            and bool(getattr(self.state, "nav_follow_enabled", False))
        ):
            return False
        self.state.service_active = True
        self.state.control_mode = "FOLLOW+SERVICE"
        self.state.sentinel_enabled = True
        now = time.time()
        if now - float(getattr(self, "_last_dosa_f2_watchdog_log_time", 0.0) or 0.0) >= 1.0:
            print("[F2Watchdog] restored: service_active")
            self._last_dosa_f2_watchdog_log_time = now
        return True

    def _log_dosa_f2_idle_reason(self, reason: str, interval: float = 1.0):
        now = time.time()
        if now - float(getattr(self, "_last_dosa_f2_idle_reason_log_time", 0.0) or 0.0) < max(0.2, float(interval)):
            return
        self._last_dosa_f2_idle_reason_log_time = now
        print(
            f"[F2Idle] {reason} | "
            f"service={bool(getattr(self.state, 'service_active', False))} "
            f"follow={bool(getattr(self.state, 'nav_follow_enabled', False))} "
            f"auto_hunt={bool(getattr(self.state, 'auto_hunt', False))} "
            f"busy={bool(getattr(self.state, 'is_combat_busy', False))} "
            f"block_left={max(0.0, float(getattr(self.state, 'support_input_blocked_until', 0.0) or 0.0) - time.time()):.2f}s"
        )

    def _is_portal_support_paused(self) -> bool:
        return (
            bool(getattr(self.state, "portal_follow_active", False))
            or time.time() < float(getattr(self, "_portal_support_pause_until", 0.0) or 0.0)
        )

    def _force_portal_esc_clear(self) -> None:
        try:
            hw.send_force("RELEASE_ALL")
        except Exception:
            pass
        for _ in range(2):
            hw.send_force("D:esc")
            humanized_sleep(0.008, variance=0.02)
            hw.send_force("U:esc")
            humanized_sleep(0.014, variance=0.03)

    def _handle_warrior_transition_immediate_clear(self, support_target: dict | None) -> bool:
        if not isinstance(support_target, dict):
            return False
        try:
            cur_x = int(support_target.get("x", support_target.get("pos_x", 0)) or 0)
            cur_y = int(support_target.get("y", support_target.get("pos_y", 0)) or 0)
        except Exception:
            return False

        prev = self._service_last_warrior_coord
        prev_dir = normalize_move_dir(self._service_last_warrior_dir)
        prev_step = normalize_move_dir(self._service_last_warrior_step_dir)
        dps_dir = normalize_move_dir(support_target.get("last_move_dir", ""))
        map_sig = (
            str(support_target.get("map_name", "") or ""),
            str(support_target.get("map_floor", "") or ""),
            str(support_target.get("current_map", "") or ""),
            str(support_target.get("current_floor", "") or ""),
        )
        map_info_change_seq = int(support_target.get("map_info_change_seq", 0) or 0)
        map_info_changed = map_info_change_seq > int(self._service_last_map_info_change_seq or 0)
        if map_info_changed:
            self._service_last_map_info_change_seq = map_info_change_seq
        prev_map_sig = self._service_last_warrior_map_sig
        cur_ok = is_plausible_map_coord(cur_x, cur_y)
        event_prev = None
        event_dir = None
        event_seq = 0
        event_age = None
        event_data = support_target.get("coord_transition")
        if isinstance(event_data, dict):
            try:
                event_seq = int(event_data.get("seq", 0) or support_target.get("coord_transition_seq", 0) or 0)
                event_ts = float(event_data.get("ts", 0.0) or 0.0)
                if event_ts > 0.0:
                    event_age = max(0.0, time.time() - event_ts)
                event_from = event_data.get("from") or []
                event_x = int(event_from[0])
                event_y = int(event_from[1])
                if (
                    event_seq > int(self._service_last_coord_transition_seq or 0)
                    and is_plausible_transition_coord(event_x, event_y)
                    and (event_age is None or event_age <= 0.75)
                ):
                    event_prev = (event_x, event_y)
                    event_dir = normalize_move_dir(event_data.get("dir") or event_data.get("input_dir"))
                    self._service_last_coord_transition_seq = event_seq
            except Exception:
                event_prev = None
                event_dir = None
                event_age = None

        # Already following a portal: keep tracking only, never overwrite target mid-run.
        if bool(getattr(self.state, "portal_follow_active", False)):
            if cur_ok:
                if prev and (prev[0], prev[1]) != (cur_x, cur_y):
                    step = dir_between_coords(prev[0], prev[1], cur_x, cur_y)
                    if follow_manhattan_gap(prev[0], prev[1], cur_x, cur_y) == 1 and step:
                        self._service_last_warrior_step_dir = step
                    self._service_warrior_trail.append((prev[0], prev[1], prev_step or prev_dir))
                    if len(self._service_warrior_trail) > 12:
                        self._service_warrior_trail = self._service_warrior_trail[-12:]
                self._service_last_warrior_coord = (cur_x, cur_y)
                self._service_last_warrior_map_sig = map_sig
                if dps_dir:
                    self._service_last_warrior_dir = dps_dir
            return False

        map_changed = bool(prev and prev_map_sig and map_sig != prev_map_sig and any(map_sig))
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
        transition_prev = event_prev or prev
        transition = bool(event_prev) or (
            bool(prev)
            and is_plausible_map_coord(prev[0], prev[1])
            and (coord_jumped or map_changed or map_info_changed)
        )

        if prev and cur_ok and (prev[0], prev[1]) != (cur_x, cur_y):
            step = dir_between_coords(prev[0], prev[1], cur_x, cur_y)
            if follow_manhattan_gap(prev[0], prev[1], cur_x, cur_y) == 1 and step:
                self._service_last_warrior_step_dir = step
            self._service_warrior_trail.append((prev[0], prev[1], prev_step or prev_dir))
            if len(self._service_warrior_trail) > 12:
                self._service_warrior_trail = self._service_warrior_trail[-12:]

        if cur_ok:
            self._service_last_warrior_coord = (cur_x, cur_y)
            self._service_last_warrior_map_sig = map_sig
            if dps_dir:
                self._service_last_warrior_dir = dps_dir
        elif map_changed and prev:
            # Map changed but new coords are junk — still allow transition using prev.
            self._service_last_warrior_map_sig = map_sig

        if not transition:
            return False

        now = time.time()
        if (now - float(getattr(self, "_last_portal_immediate_clear_time", 0.0) or 0.0)) < 0.8:
            return False
        self._last_portal_immediate_clear_time = now
        self._portal_support_pause_until = now + 3.0

        # Do NOT block movement keys — only pause party heal via portal_follow_active.
        source_map_sig = prev_map_sig if prev_map_sig and any(prev_map_sig) else map_sig
        cached_hint = {}
        if isinstance(getattr(self.state, "portal_session_cache", None), dict):
            cached_hint = dict(self.state.portal_session_cache.get(source_map_sig, {}) or {})
        cached_dir = normalize_move_dir(cached_hint.get("enter_dir"))
        enter_dir = event_dir or prev_step or getattr(self, "_service_last_warrior_step_dir", None) or cached_dir
        if not enter_dir:
            if cur_ok:
                self._service_last_warrior_coord = (cur_x, cur_y)
                self._service_last_warrior_map_sig = map_sig
            print(
                f"[PortalFollow] transition waiting for stable step dir: "
                f"now=({cur_x}, {cur_y}) event_age={event_age if event_age is not None else '-'} "
                f"map={prev_map_sig}->{map_sig}"
            )
            return False
        warrior_last = (int(transition_prev[0]), int(transition_prev[1]))
        portal_xy = offset_coord_by_dir(warrior_last[0], warrior_last[1], enter_dir)
        self.state.portal_follow_active = True
        self.state.portal_follow_retarget_requested = True
        self.state.portal_follow_started_at = time.time()
        self.state.portal_follow_finished_at = 0.0
        self.state.portal_follow_coord = warrior_last
        self.state.portal_follow_approach = warrior_last
        self.state.portal_follow_dir = enter_dir
        try:
            self.state.red_tab_enabled = False
        except Exception:
            pass
        self._party_direct_heal_target_prepared = False
        self._party_direct_heal_verified = False
        self._warrior_redtab_verified = False

        self._force_portal_esc_clear()
        print(
            "[PortalFollow] immediate clear on warrior transition: "
            f"warrior_last={warrior_last}, portal={portal_xy}, now=({cur_x}, {cur_y}), "
            f"dir={enter_dir or '-'}, step_dir={prev_step or '-'}, "
            f"coord_jump={coord_jumped}, map_changed={map_changed}, "
            f"event_seq={event_seq or '-'}, map={prev_map_sig}->{map_sig}"
        )
        return True

    def _run_dosa_service_cycle(self, support_target: dict | None) -> bool:
        """
        ?? ??? ?? ??.
        ?? ??/?? ??? ?? ????, ?? ?? red_tab ?? ???? ????.
        """
        if not self._restore_dosa_service_if_follow_autohunt():
            return False

        self._repair_dosa_f2_runtime_state()

        if not self._is_hw_ready():
            try:
                hw.auto_reconnect()
            except Exception:
                pass
            now = time.time()
            if now - self._last_hw_skip_log_time >= 2.0:
                print("[Support] hardware not ready. Skip service loop.")
                self._last_hw_skip_log_time = now
            self._log_dosa_f2_idle_reason("hardware_not_ready")
            self._pace_service_loop("idle")
            return True

        if self._handle_warrior_transition_immediate_clear(support_target):
            self._pace_service_loop("active")
            return True

        if bool(getattr(self.state, "portal_follow_active", False)):
            self._portal_support_pause_until = max(
                float(getattr(self, "_portal_support_pause_until", 0.0) or 0.0),
                time.time() + 0.4,
            )
            self._invalidate_warrior_redtab_verification()
            self._log_dosa_f2_idle_reason("portal_follow_active stop_party_heal", interval=0.4)
            self._pace_service_loop("active")
            return True

        if not self._restore_game_window_focus_for_support():
            now = time.time()
            if now - self._last_focus_skip_log_time >= 2.0:
                print("[Support] game window inactive. Continue F2 with force hardware input.")
                self._last_focus_skip_log_time = now
            self._log_dosa_f2_idle_reason("game_window_inactive_force_input")

        if self._handle_initial_direct_heal_prepare_request():
            self._pace_service_loop("active")
            return True

        if self._handle_box_collision_reprepare_request():
            self._pace_service_loop("active")
            return True

        if bool(getattr(self, "_warrior_debuff_active", False)):
            self._pace_service_loop("active")
            return True

        if time.time() < float(getattr(self, "_post_debuff_follow_until", 0.0) or 0.0):
            self._pace_service_loop("active")
            return True
        if bool(getattr(self, "_post_debuff_recover_prepare_pending", False)):
            self._post_debuff_recover_prepare_pending = False
            self.request_initial_direct_heal_target_prepare()
            self._pace_service_loop("active")
            return True

        if self._update_zero_hp_state():
            self._handle_zero_hp_recovery()
            self._pace_service_loop("active")
            return True

        if int(getattr(self.state, "hp", 0) or 0) <= 0:
            self.state.last_self_buff_time = 0.0
            self.state.last_self_gg_cast_time = 0.0

        # 1) ?먭? ?앹〈? ??긽 ?곗꽑
        good_hp = self._get_good_hp_threshold()
        current_self_hp = int(getattr(self.state, "hp", 0) or 0)
        if current_self_hp >= good_hp or current_self_hp <= int(getattr(self, "_mp_stationary_self_heal_threshold", 50000) or 50000):
            self._mp_follow_self_heal_until = 0.0
        party_hp_needed_before_self_recover = bool(
            support_target and self._needs_hp_recovery_for(support_target)
        )
        if support_target and should_prioritize_self_mp_over_party_heal(
            int(getattr(self.state, "mp", 0) or 0),
            int(support_target.get("hp", 0) or 0),
            self._self_mp_priority_threshold,
            warrior_critical_hp=30000,
        ):
            if self._handle_self_mp_priority(support_target):
                self._pace_service_loop("active")
                return True

        if (
            current_self_hp > 0
            and current_self_hp < good_hp
            and current_self_hp > int(getattr(self, "_mp_stationary_self_heal_threshold", 50000) or 50000)
            and time.time() < float(getattr(self, "_mp_follow_self_heal_until", 0.0) or 0.0)
        ):
            self._cast_self_hp_micro_follow_tick()
            self._log_dosa_f2_idle_reason(
                f"post_mp_micro_self_heal self_hp={current_self_hp}/{good_hp}",
                interval=0.5,
            )
            self._pace_service_loop("active")
            return True

        if (
            party_hp_needed_before_self_recover
            and current_self_hp > self._self_hp_emergency_threshold
            and not self._is_party_heal_blocked()
        ):
            self._cast_due_periodic_party_support(support_target)
            if self._recover_party_hp(support_target):
                self._party_hp_next_tick_interval = self._roll_party_hp_tick_interval(True)
                self._log_dosa_f2_idle_reason(
                    f"party_heal_prioritized_over_self_recover self_hp={current_self_hp}/{good_hp}",
                    interval=1.0,
                )
                self._pace_service_loop("active")
                return True

        if current_self_hp > 0 and current_self_hp < good_hp:
            if party_hp_needed_before_self_recover and current_self_hp > self._self_hp_emergency_threshold:
                self._log_dosa_f2_idle_reason(
                    f"self_recover_deferred_for_party_heal self_hp={current_self_hp}/{good_hp}",
                    interval=1.0,
                )
                self._pace_service_loop("active")
                return True
            recovered = self._recover_self_hp_until_good_hp(
                reason_log="[Recovery] self low HP sustain recovery.",
                support_block_duration=1.8,
                max_attempts=6,
            )
            if recovered:
                self._complete_self_hp_recovery_reengage(
                    source_log="[Recovery] self low-HP recovery complete."
                )
            elif self.state.role == "도사" and self.state.service_active:
                self.request_initial_direct_heal_target_prepare()
            self._pace_service_loop("active")
            return True

        # 2) 寃⑹닔 ?곗씠?곌? ?놁쑝硫?吏??猷⑦봽 ????湲?(遺덊븘?뷀븳 gg/bm ?고? 諛⑹?)
        if not support_target:
            self._log_dosa_f2_idle_reason("no_warrior_telemetry")
            if self._handle_self_mp_priority(None):
                self._pace_service_loop("active")
                return True
            if self._needs_mp_recovery():
                self._execute_self_mp_recovery_and_retarget(
                    None,
                    source_log="[Recovery] self MP recovery without warrior telemetry complete.",
                )
                self._pace_service_loop("active")
                return True
            self._pace_service_loop("no_target")
            return True

        # 3) 寃⑹닔 吏???곗꽑
        if int(support_target.get("hp", 0) or 0) <= 0:
            self.state.last_warrior_bomu_time = 0.0

        now_dbg = time.time()
        last_dbg = getattr(self, '_last_cycle_debug_time', 0)
        if now_dbg - last_dbg > 3.0:
            self._last_cycle_debug_time = now_dbg
            needs_hp_dbg = self._needs_hp_recovery_for(support_target)
            hp_dbg = int(support_target.get('hp', 0) or 0)
            good_hp_dbg = self._get_party_good_hp_threshold(support_target)
            follow_dbg = bool(getattr(self.state, "nav_follow_enabled", False))
            busy_dbg = bool(getattr(self.state, "is_combat_busy", False))
            print(
                f"[CycleDBG] 서비스루프=True, 따라가기={follow_dbg}, busy={busy_dbg}, "
                f"힐필요={needs_hp_dbg}, HP={hp_dbg}/{good_hp_dbg}"
            )

        needs_hp = self._needs_hp_recovery_for(support_target)
        needs_mp = self._needs_mp_recovery_for(support_target)
        warrior_hp_for_mp_priority = int(support_target.get("hp", 0) or 0)
        try:
            warrior_x = int(support_target.get("x", support_target.get("pos_x", 0)) or 0)
            warrior_y = int(support_target.get("y", support_target.get("pos_y", 0)) or 0)
            warrior_distance = follow_manhattan_gap(
                warrior_x,
                warrior_y,
                int(getattr(self.state, "x", 0) or 0),
                int(getattr(self.state, "y", 0) or 0),
            )
        except Exception:
            warrior_distance = 0
        follow_distance_risk = (
            bool(getattr(self.state, "nav_follow_enabled", False))
            and should_prioritize_follow_distance(warrior_distance, risk_distance=7)
        )
        portal_follow_active = bool(getattr(self.state, "portal_follow_active", False))
        portal_retarget_requested = bool(getattr(self.state, "portal_follow_retarget_requested", False))

        if portal_follow_active:
            self._invalidate_warrior_redtab_verification()
            self._log_dosa_f2_idle_reason(
                f"portal_follow_active defer_warrior_support distance={warrior_distance}",
                interval=0.5,
            )
            self._pace_service_loop("active")
            return True

        if portal_retarget_requested:
            if warrior_distance > 4:
                self._invalidate_warrior_redtab_verification()
                self._log_dosa_f2_idle_reason(
                    f"portal_retarget_wait distance={warrior_distance}",
                    interval=0.5,
                )
                self._pace_service_loop("active")
                return True
            self._invalidate_warrior_redtab_verification()
            if not self._prepare_direct_tab_heal_target():
                self._log_dosa_f2_idle_reason("portal_retarget_prepare_failed", interval=0.6)
                self._pace_service_loop("active")
                return True
            self._party_direct_heal_target_prepared = True
            self._last_party_hp_support_time = 0.0
            self.state.portal_follow_retarget_requested = False
            print(f"[PortalFollow] warrior red_tab restored after portal: distance={warrior_distance}")

        if needs_hp:
            if follow_distance_risk and int(support_target.get("hp", 0) or 0) > 30000:
                self._invalidate_warrior_redtab_verification()
                self._log_dosa_f2_idle_reason(
                    f"follow_distance_priority distance={warrior_distance} defer_warrior_heal",
                    interval=0.7,
                )
                self._pace_service_loop("active")
                return True
            if self._is_party_heal_blocked():
                now_blocked = time.time()
                if now_blocked - float(getattr(self, "_last_party_heal_blocked_log_time", 0.0) or 0.0) >= 1.0:
                    self._last_party_heal_blocked_log_time = now_blocked
                    print(
                        "[Support] warrior HP heal blocked: "
                        f"phase={getattr(self, '_support_phase', 'idle')} "
                        f"party_block_left={max(0.0, float(getattr(self, '_party_heal_blocked_until', 0.0) or 0.0) - now_blocked):.2f}s"
                    )
                self._pace_service_loop("active")
                return True
            hp_val = int(support_target.get('hp', 0) or 0)
            good_hp_remote = self._get_party_good_hp_threshold(support_target)
            remote_heal_request = bool(support_target.get("heal_request", False))
            now_support = time.time()
            self._mark_support_party_heal_follow_guard(duration=0.90)
            rapid_heal = remote_heal_request or (good_hp_remote > 0 and hp_val <= int(good_hp_remote * 0.55))
            hp_tick_interval = float(getattr(self, "_party_hp_next_tick_interval", 0.24) or 0.24)
            if rapid_heal:
                hp_tick_interval = min(hp_tick_interval, float(self._party_hp_tick_fast_interval_max))
            if (now_support - self._last_party_hp_support_time) < hp_tick_interval:
                self._pace_service_loop("active")
                return True
            if (now_support - self._last_party_hp_check_log_time) >= 1.0:
                self._last_party_hp_check_log_time = now_support
                cast_mode = "direct 3 verified" if self._party_direct_heal_verified else "esc>tab>tab acquire"
                print(f"[Support] HP check: {hp_val} / {good_hp_remote} -> {cast_mode}")
            self._cast_due_periodic_party_support(support_target, now_support=now_support)
            casted_hp = self._recover_party_hp(support_target)
            if casted_hp:
                self._party_hp_next_tick_interval = self._roll_party_hp_tick_interval(rapid_heal)
                self._last_party_hp_value = hp_val
            if (not rapid_heal) and self._handle_self_mp_priority(support_target):
                self._pace_service_loop("active")
                return True
            self._pace_service_loop("active")
            return True

        if needs_mp:
            self._recover_party_mp(support_target)
            self._pace_service_loop("active")
            return True

        # 4) 吏???ъ쑀 援ш컙?먯꽌留??먭? ?곹깭/踰꾪봽 ?먭?
        remote_hp = int(support_target.get("hp", 0) or 0)
        remote_good_hp = self._get_party_good_hp_threshold(support_target)
        support_safe = remote_hp > max(1, remote_good_hp + 30000)
        follow_repositioning = self._is_follow_reposition_needed()
        self._log_dosa_f2_idle_reason(
            f"warrior_heal_not_needed hp={remote_hp}/{remote_good_hp} follow_repositioning={follow_repositioning}",
            interval=2.0,
        )

        if not follow_repositioning and self._handle_self_mp_priority(support_target):
            self._pace_service_loop("active")
            return True

        if support_safe and not follow_repositioning:
            if self._execute_warrior_debuff_cycle(support_target):
                self._pace_service_loop("active")
                return True
            self._refresh_self_status_if_needed()
            if self._execute_self_support_buffs_cycle():
                self._pace_service_loop("active")
                return True
            if self._needs_mp_recovery():
                self._execute_self_mp_recovery_and_retarget(
                    support_target,
                    source_log="[Recovery] self MP recovery during safe support complete.",
                )
                self._pace_service_loop("active")
                return True

        # 5) 蹂대Т 二쇨린 ?좎?
        if self._execute_warrior_bomu_cycle(support_target):
            self._pace_service_loop("active")
            return True

        self._log_dosa_f2_idle_reason(
            f"safe_idle hp={remote_hp}/{remote_good_hp} follow_needed={follow_repositioning}",
            interval=1.5,
        )
        self._pace_service_loop("idle")
        return True

    def _self_buff_cycle(self):
        """?? ??? ???: ?? ?? ???."""
        return self._execute_self_support_buffs_cycle()


    
    def _cast_spell_by_name(self, spell_name: str):
        """스킬 이름으로 스킬 캐스팅."""
        print(f"[DEBUG] _cast_spell_by_name ?몄텧: {spell_name}")
        try:
            from bis_spell import spell_caster, target_mapper
            import json
            
            # spells_config.json?먯꽌 ?ㅽ궗 ?뺣낫 李얘린
            with open('spells_config.json', 'r', encoding='utf-8') as f:
                spells = json.load(f)
            
            for spell in spells:
                if spell.get('name') == spell_name and spell.get('use'):
                    spell_char = spell.get('spell_char', '')
                    cast_function = spell.get('cast_function', 'SpellEnter')
                    
                    print(f"[DEBUG] ?ㅽ궗 ?뺣낫 李얠쓬: {spell_name}, char='{spell_char}', func={cast_function}")
                    
                    if spell_char:
                        print(f"[DEBUG] ?ㅽ궗 ?쒖쟾 ?쒖옉: {spell_name}")
                        caster = spell_caster
                        # 罹먯뒪???⑥닔 ?몄텧
                        if cast_function == 'SpellEnter':
                            caster.spell_enter(spell_char)
                        elif cast_function == 'SpellHomeEnter':
                            caster.spell_home_enter(spell_char)
                        elif cast_function == 'SpellArrowEnter':
                            caster.spell_arrow_enter(spell_char)
                        elif cast_function == 'SpellClickEnter':
                            caster.spell_click_enter(spell_char)
                        print(f"[Logic] Casted spell: {spell_name} ({cast_function})")
                    else:
                        print(f"[ERROR] ?ㅽ궗 臾몄옄 ?놁쓬: {spell_name}")
                    break
            else:
                print(f"[ERROR] ?ㅽ궗 李얠? 紐삵븿 ?먮뒗 use=false: {spell_name}")
        except Exception as e:
            print(f"[ERROR] Failed to cast spell {spell_name}: {e}")

    def _handle_combat(self):
        """?꾪닾 泥섎━ (?곗꽑?쒖쐞 1)"""
        # ?꾪닾 以묒뿉??is_combat_busy ?뚮옒洹??ㅼ젙
        self._set_combat_busy(True)

        role_name = str(getattr(self.state, "role", "") or "").strip()
        network_role = str(getattr(self.state, "network_role", "") or "").strip()
        is_warrior = (
            role_name in {"격수", "Warrior", "寃⑹닔"}
            or network_role in {"격수", "Warrior", "寃⑹닔"}
        )

        # ??븷蹂??꾪닾 濡쒖쭅
        if is_warrior:
            self._run_dps_server_mode()
        elif self.state.role == "도사":
            self._run_support_mode()
        elif self.state.role == "술사":
            self._run_priest_mode()
        else:
            self._run_default_mode()

        if not self._should_handle_combat():
            self._set_combat_busy(False)

    def _handle_moving(self):
        """?대룞/?ㅻ퉬寃뚯씠??泥섎━ (?곗꽑?쒖쐞 2)"""
        # ?꾪닾 以묒씠 ?꾨땺 ?뚮쭔 ?대룞
        if not self.state.is_combat_busy:
            # Route mode on F2 should always keep warrior attack/loot loop alive.
            if getattr(self.state, "service_active", False) and getattr(self.state, "nav_route_enabled", False):
                self._run_warrior_attack_loot_cycle()
                return
            # ?대룞 濡쒖쭅? RouteSvc?먯꽌 泥섎━
            role_name = str(getattr(self.state, "role", "") or "").strip()
            network_role = str(getattr(self.state, "network_role", "") or "").strip()
            if (
                role_name in {"격수", "Warrior", "寃⑹닔"}
                or network_role in {"격수", "Warrior", "寃⑹닔"}
            ):
                self._run_warrior_attack_loot_cycle()

    # ----------------------------------------------------------
    # ?? 寃⑹닔 紐⑤뱶: ?곗씠??怨듭쑀 ?쒕쾭 ??븷留??????????????????????
    # ----------------------------------------------------------
    def _run_dps_server_mode(self):
        """격수 모드: 데이터 송신 + 이동 중 3/0 교대 자동사냥."""
        # Keep warrior loop alive while F2 service mode is active.
        if not bool(getattr(self.state, "service_active", False)):
            self.state.last_bomu_time    = 0
            self.state.combat_start_time = 0.0
            humanized_sleep(TIMING_CONFIG["idle_sleep"])
            return

        # Keep network/status sharing and add warrior-specific attack/loot cycle.
        
        # ?앹〈 理쒖슦??(HP/MP ?뚮났) - 寃⑹닔???먭? ?뚮났 ?꾩슂
        if self._needs_hp_recovery():
            self._recover_hp()
            self.state.combat_start_time = 0.0

        if self._needs_mp_recovery():
            self._recover_mp()
            if self._needs_hp_recovery():
                self._recover_hp()

        # 蹂대Т 踰꾪봽 ?좎? (185s 二쇨린)
        if time.time() - self.state.last_bomu_time > TIMING_CONFIG["bomu_interval"]:
            self._execute_bomu_buff_v3()

        # ?붾쾭???ㅼ틪 (寃⑹닔???꾩슂)
        dist_change = (abs(self.state.x - self.state.last_debuff_x) +
                       abs(self.state.y - self.state.last_debuff_y))
        if self.state.auto_debuff_enabled and dist_change > 15:
            self._execute_debuff_scan_v3()

        self._run_warrior_attack_loot_cycle()
        humanized_sleep(TIMING_CONFIG["action_loop"])

    def _run_warrior_attack_loot_cycle(self):
        """격수 전용: 3은 짧게 반복, 0은 긴 랜덤 쿨타임으로 독립 관리."""
        # Route thread may keep moving even if auto_hunt flips false transiently.
        # For warrior F2 mode, run loop based on service_active.
        if not bool(getattr(self.state, "service_active", False)):
            return
        now = time.time()
        attack_due = now >= float(getattr(self, "_warrior_next_attack_at", 0.0) or 0.0)
        loot_due = now >= float(getattr(self, "_warrior_next_loot_at", 0.0) or 0.0)
        if not attack_due and not loot_due:
            return

        # Loot has lower frequency but should not starve behind the attack loop.
        if loot_due:
            if not self._is_hw_ready():
                self._warrior_next_loot_at = now + 0.10
                return
            try:
                hw.force_press("0", variance=0.08)
                pressed = True
            except Exception:
                pressed = False
            if not pressed:
                self._warrior_next_loot_at = now + 0.10
                return
            self._warrior_next_loot_at = now + random.uniform(2.0, 14.0)
            if now - self._last_warrior_cycle_log_time >= 1.0:
                print(f"[WarriorLoop] cast=0 next_loot={max(0.0, self._warrior_next_loot_at - now):.2f}s")
                self._last_warrior_cycle_log_time = now
            return

        pressed = self._press_hw_key("3", variance=0.08, skip_focus_guard=True)
        if not pressed:
            self._warrior_next_attack_at = now + 0.10
            return
        self._warrior_next_attack_at = now + random.uniform(0.3, 0.5)
        if now - self._last_warrior_cycle_log_time >= 1.0:
            print(f"[WarriorLoop] cast=3 next_attack={max(0.0, self._warrior_next_attack_at - now):.2f}s")
            self._last_warrior_cycle_log_time = now

    def _is_sulsa_role(self) -> bool:
        return str(getattr(self.state, "role", "") or "").strip() == "술사"

    def _set_sulsa_input_busy(self, duration: float = 0.35) -> bool:
        if not self._is_hw_ready():
            now = time.time()
            if now - self._last_hw_skip_log_time >= 2.0:
                print("[Sulsa] hardware not ready.")
                self._last_hw_skip_log_time = now
            return False
        self._set_combat_busy(True)
        self._set_support_input_block(duration)
        self._release_movement_keys_only()
        return True

    def _cast_sulsa_target_spell(self, key: str, label: str) -> bool:
        if not self._set_sulsa_input_busy(0.42):
            return False
        try:
            for press_key, delay in ((key, 0.035), ("up", 0.035), ("enter", 0.045)):
                if not self._press_hw_key(press_key, variance=0.08, skip_focus_guard=True):
                    return False
                humanized_sleep(delay, variance=0.08)
            print(f"[Sulsa] cast {label}: {key}>up>enter")
            return True
        finally:
            self._set_combat_busy(False)

    def _cast_sulsa_power_boost_once(self) -> bool:
        if not self._set_sulsa_input_busy(0.18):
            return False
        try:
            ok = self._press_hw_key("2", variance=0.08, skip_focus_guard=True)
            if ok:
                print("[Sulsa] cast 공력증강: 2")
            return ok
        finally:
            self._set_combat_busy(False)

    def _cast_sulsa_self_heal_once(self) -> bool:
        if not self._set_sulsa_input_busy(0.42):
            return False
        try:
            for key, delay in (("3", 0.035), ("home", 0.035), ("enter", 0.045)):
                if not self._press_hw_key(key, variance=0.08, skip_focus_guard=True):
                    return False
                humanized_sleep(delay, variance=0.08)
            print("[Sulsa] self heal: 3>home>enter")
            return True
        finally:
            self._set_combat_busy(False)

    def _use_sulsa_mp_item(self) -> bool:
        slots = list(getattr(self.state, "sulsa_item_slots", []) or [])
        if not slots:
            return False
        index = max(0, int(getattr(self.state, "sulsa_item_slot_index", 0) or 0))
        if index >= len(slots):
            print("[Sulsa] MP item slots exhausted.")
            return False
        slot = str(slots[index] or "").strip().lower()
        if not slot:
            return False
        if not self._set_sulsa_input_busy(0.24):
            return False
        try:
            for key, delay in (("u", 0.025), (slot, 0.040)):
                if not self._press_hw_key(key, variance=0.08, skip_focus_guard=True):
                    return False
                humanized_sleep(delay, variance=0.08)
            uses = int(getattr(self.state, "sulsa_item_slot_uses", 0) or 0) + 1
            max_uses = max(1, int(getattr(self.state, "sulsa_item_uses_per_slot", 100) or 100))
            if uses >= max_uses:
                self.state.sulsa_item_slot_index = index + 1
                self.state.sulsa_item_slot_uses = 0
                print(f"[Sulsa] MP item used: u>{slot} ({uses}/{max_uses}), next slot index={index + 1}")
            else:
                self.state.sulsa_item_slot_uses = uses
                print(f"[Sulsa] MP item used: u>{slot} ({uses}/{max_uses})")
            return True
        finally:
            self._set_combat_busy(False)

    def _run_sulsa_debuff_once_if_due(self, force: bool = False) -> bool:
        now = time.time()
        acted = False
        if force or (now - float(self._sulsa_last_paralyze_time or 0.0)) >= self._sulsa_paralyze_interval:
            if self._cast_sulsa_target_spell("5", "마비"):
                self._sulsa_last_paralyze_time = time.time()
                acted = True
        now = time.time()
        if force or (now - float(self._sulsa_last_curse_time or 0.0)) >= self._sulsa_curse_interval:
            if self._cast_sulsa_target_spell("6", "저주"):
                self._sulsa_last_curse_time = time.time()
                acted = True
        return acted

    def _run_sulsa_hellfire_cycle_if_due(self) -> bool:
        now = time.time()
        if (now - float(self._sulsa_last_hellfire_time or 0.0)) < self._sulsa_hellfire_interval:
            return False
        if not bool(getattr(self.state, "sulsa_attack_enabled", False)):
            return False
        if not self._cast_sulsa_target_spell("1", "헬파이어"):
            return False
        self._sulsa_last_hellfire_time = time.time()
        humanized_sleep(0.05, variance=0.08)
        self._use_sulsa_mp_item()
        humanized_sleep(0.05, variance=0.08)
        self._cast_sulsa_power_boost_once()
        if int(getattr(self.state, "hp", 0) or 0) < self._get_good_hp_threshold():
            humanized_sleep(0.05, variance=0.08)
            self._cast_sulsa_self_heal_once()
        return True

    def _run_sulsa_self_sustain(self) -> bool:
        current_hp = int(getattr(self.state, "hp", 0) or 0)
        current_mp = int(getattr(self.state, "mp", 0) or 0)
        if 0 < current_hp < self._get_good_hp_threshold():
            return self._cast_sulsa_self_heal_once()
        if 0 < current_mp <= self._self_mp_priority_threshold:
            if self._cast_sulsa_power_boost_once():
                if int(getattr(self.state, "hp", 0) or 0) < self._get_good_hp_threshold():
                    self._cast_sulsa_self_heal_once()
                return True
        return False

    def _run_sulsa_service_cycle(self) -> bool:
        if not self._is_sulsa_role():
            return False
        active = bool(getattr(self.state, "service_active", False)) or bool(getattr(self.state, "sulsa_debuff_standalone", False))
        if not active:
            return False
        if self._run_sulsa_self_sustain():
            self._pace_service_loop("active")
            return True
        if bool(getattr(self.state, "sulsa_debuff_enabled", False)):
            if self._run_sulsa_debuff_once_if_due(force=False):
                self._pace_service_loop("active")
                return True
        if bool(getattr(self.state, "auto_hunt", False)):
            if self._run_sulsa_hellfire_cycle_if_due():
                self._pace_service_loop("active")
                return True
        now = time.time()
        if now - float(getattr(self, "_sulsa_last_action_log_time", 0.0) or 0.0) >= 2.0:
            print(
                "[Sulsa] idle "
                f"attack={bool(getattr(self.state, 'sulsa_attack_enabled', False))} "
                f"debuff={bool(getattr(self.state, 'sulsa_debuff_enabled', False))} "
                f"follow={bool(getattr(self.state, 'nav_follow_enabled', False))}"
            )
            self._sulsa_last_action_log_time = now
        self._pace_service_loop("idle")
        return True

    # ----------------------------------------------------------
    # ?? 吏??紐⑤뱶: ?꾩궗 - 寃⑹닔 ?곗씠???섏떊 諛?吏??????????????????
    # ----------------------------------------------------------
    def _run_support_mode(self):
        """?꾩궗 紐⑤뱶: 寃⑹닔???곗씠???섏떊諛쏆븘 吏??濡쒖쭅 ?섑뻾"""
        # ?? ?먮룞?щ깷 鍮꾪솢???????????????????????????????
        if not self.state.auto_hunt:
            self.state.last_bomu_time    = 0
            self.state.combat_start_time = 0.0
            humanized_sleep(TIMING_CONFIG["idle_sleep"])
            return

        # ?? 吏??濡쒖쭅 ?섑뻾 ???????????????????????????????
        # ?꾩궗??寃⑹닔???寃잛쓣 ?곕씪媛硫?吏??
        
        # ?앹〈 理쒖슦??(HP/MP ?뚮났)
        if self._needs_hp_recovery():
            self._recover_hp()
            self.state.combat_start_time = 0.0

        if self._needs_mp_recovery():
            self._recover_mp()
            if self._needs_hp_recovery():
                self._recover_hp()

        # 蹂대Т 踰꾪봽 ?좎? (185s 二쇨린)
        if time.time() - self.state.last_bomu_time > TIMING_CONFIG["bomu_interval"]:
            self._execute_bomu_buff_v3()

        # ?붾쾭???ㅼ틪
        dist_change = (abs(self.state.x - self.state.last_debuff_x) +
                       abs(self.state.y - self.state.last_debuff_y))
        if self.state.auto_debuff_enabled and dist_change > 15:
            self._execute_debuff_scan_v3()

        # ?좎? ?먯? ???+ ?뷀떚???곗꽑?쒖쐞 ?됰룞 ?쒖뼱
        if self._handle_entity_priorities():
            humanized_sleep(TIMING_CONFIG["action_loop"])
            return

        # SentinelThread 寃곌낵 蹂닿퀬: 紐ъ뒪??議댁옱 ???쒗룷???ㅽ궗 ?쒖쟾
        if self.state.monster_on_screen:
            # 寃⑹닔媛 ?寃잛쓣 ?↔퀬 ?덉쑝硫?吏???ㅽ궗 ?쒖쟾
            self._execute_support_skills()

        humanized_sleep(TIMING_CONFIG["action_loop"])

    def _get_priest_monsters(self) -> list[dict]:
        monsters = self.state.entities.get("monsters", [])
        return monsters if isinstance(monsters, list) else []

    def _calc_monster_metrics(self):
        me_pos = (int(getattr(self.state, "x", 0)), int(getattr(self.state, "y", 0)))
        if me_pos[0] <= 0 and me_pos[1] <= 0:
            return None, [], 0

        valid_monsters = []
        dense_count = 0
        for m in self._get_priest_monsters():
            if not isinstance(m, dict):
                continue
            monster_pos = m.get("world_pos") or m.get("pos")
            if not monster_pos or len(monster_pos) != 2:
                grid = m.get("grid")
                if not grid or len(grid) != 2:
                    continue
                dx = int(grid[0]) - int(self.state.char_grid[0])
                dy = int(grid[1]) - int(self.state.char_grid[1])
            else:
                dx = int(monster_pos[0]) - me_pos[0]
                dy = int(monster_pos[1]) - me_pos[1]
            dist = (dx * dx + dy * dy) ** 0.5
            valid_monsters.append({"monster": m, "dist": dist, "dx": dx, "dy": dy})
            if max(abs(dx), abs(dy)) <= 1:
                dense_count += 1
        return me_pos, valid_monsters, dense_count

    def _get_priest_engage_metrics(self, engage_range: int | None = None):
        """
        ?좎궗 泥⑥궗?μ? 媛源뚯슫 紐ъ뒪?곕쭔 ?꾪닾 吏꾩엯 ??곸쑝濡?蹂몃떎.
        engage_range??pos x/y 湲곗? 泥대퉬?쇳봽 嫄곕━??
        """
        if engage_range is None:
            engage_range = max(0, int(getattr(self.state, "priest_engage_range", 2)))
        me_grid, monster_infos, _dense_count = self._calc_monster_metrics()
        if me_grid is None:
            return None, [], 0

        close_infos = [
            info for info in monster_infos
            if max(abs(info["dx"]), abs(info["dy"])) <= engage_range
        ]
        close_dense_count = sum(
            1 for info in close_infos
            if max(abs(info["dx"]), abs(info["dy"])) <= 1
        )
        return me_grid, close_infos, close_dense_count

    def _find_ready_attack_skill(self, aoe_mode: bool):
        attack_skills = [s for s in self.state.spells if s.category == "怨듦꺽" and s.is_ready()]
        if not attack_skills:
            return None

        if aoe_mode:
            for s in attack_skills:
                if "천" in s.name:
                    return s
            return attack_skills[0]

        for s in attack_skills:
            if "격" in s.name or "단일" in s.name:
                return s
        for s in attack_skills:
            if "천" not in s.name:
                return s
        return attack_skills[0]

    def _cast_attack_skill(self, skill):
        hw.humanized_press(skill.hotkey)
        humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
        skill.last_cast_time = time.time()

    # ----------------------------------------------------------
    # ?? ?좎궗 紐⑤뱶: 泥⑥궗??濡쒖쭅 ????????????????????????????????
    # ----------------------------------------------------------
    def _run_priest_mode(self):
        """?좎궗 紐⑤뱶: 泥⑥궗??濡쒖쭅 ?섑뻾"""
        # ?? ?먮룞?щ깷 鍮꾪솢???????????????????????????????
        if not self.state.auto_hunt:
            self.state.last_bomu_time    = 0
            self.state.combat_start_time = 0.0
            humanized_sleep(TIMING_CONFIG["idle_sleep"])
            return

        # ?? 泥⑥궗??濡쒖쭅 ?섑뻾 ????????????????????????????
        # ?좎궗???낆옄?곸쑝濡??щ깷 ?섑뻾
        
        # 蹂대Т 踰꾪봽 ?좎? (185s 二쇨린)
        if time.time() - self.state.last_bomu_time > TIMING_CONFIG["bomu_interval"]:
            self._execute_bomu_buff_v3()

        # ?앹〈 理쒖슦??(HP/MP ?뚮났)
        if self._needs_hp_recovery():
            self._recover_hp()
            self.state.combat_start_time = 0.0

        if self._needs_mp_recovery():
            self._recover_mp()
            if self._needs_hp_recovery():
                self._recover_hp()

        # ?좎? ?먯? ???+ ?뷀떚???곗꽑?쒖쐞 ?됰룞 ?쒖뼱
        if self._handle_entity_priorities():
            humanized_sleep(TIMING_CONFIG["action_loop"])
            return

        # ?꾩씠???띾뱷 (?좏쑕 ?곹깭???뚮쭔)
        if not self.state.target_locked and self.state.detected_item_grid:
            gx, gy = self.state.detected_item_grid
            print(f"[Item] ?꾩씠???띾뱷 ?쒕룄: {self.state.detected_item_name} @ Grid({gx}, {gy})")

            while self.state.running and self.state.auto_hunt and self.state.detected_item_grid:
                if self.state.target_locked or self._needs_hp_recovery() or self._needs_mp_recovery():
                    print("[Action] ?띾뱷 以??꾪닾/?꾧툒 ?곹솴 諛쒖깮 -> 猷⑦똿 以묐떒.")
                    break

                # ?? ?뚰뵾 ?대룞 (Stuck 媛먯?) ?듯빀 ??
                if self._check_stuck():
                    self._escape_stuck(rewind_waypoint=False)
                    humanized_sleep(TIMING_CONFIG["move_hold"])
                    continue

                arrived = self._move_toward_grid(gx, gy)
                if arrived:
                    print(f"[Point] ?꾩씠?????꾩갑. ?띾뱷 ?쒕룄 (',')")
                    hw.humanized_press(",") 
                    humanized_sleep(TIMING_CONFIG["enter_wait"])
                    self.state.detected_item_grid = None
                    print("[OK] ?꾩씠???띾뱷 ?꾨즺.")
                    break
                
                humanized_sleep(TIMING_CONFIG["nav_loop"])

        # ?붾쾭???ㅼ틪
        dist_change = (abs(self.state.x - self.state.last_debuff_x) +
                       abs(self.state.y - self.state.last_debuff_y))
        if self.state.auto_debuff_enabled and dist_change > 15:
            self._execute_debuff_scan_v3()

        me_grid, monster_infos, dense_count = self._get_priest_engage_metrics()
        has_monsters = len(monster_infos) > 0
        if has_monsters:
            aoe_mode = dense_count >= 2
            # ?⑥씪 ?寃??ㅽ궗? ?寃??쎌씠 ?꾩슂?섎?濡?湲곗〈 ?먯깋 ?쒗???좎?
            if (not aoe_mode) and (not self.state.target_locked):
                self.state.combat_start_time = 0.0
                if self._search_target_v3():
                    humanized_sleep(TIMING_CONFIG["ocr_fast"])
                return

            skill = self._find_ready_attack_skill(aoe_mode=aoe_mode)
            if skill:
                mode_label = "愿묒뿭(泥?" if aoe_mode else "?⑥씪"
                print(f"[Priest] 怨듦꺽 遺꾧린: {mode_label} | 洹쇱젒諛吏?{dense_count} | ?덉씠?붾す={len(monster_infos)}")
                self._cast_attack_skill(skill)
            else:
                self._execute_combat()
        elif self.state.target_locked:
            humanized_sleep(TIMING_CONFIG["ocr_fast"])
            self._execute_combat()
        else:
            self._clear_target_state()
            humanized_sleep(TIMING_CONFIG["nav_loop"])
            return

        humanized_sleep(TIMING_CONFIG["action_loop"])

    # ----------------------------------------------------------
    # ?? 湲곕낯 紐⑤뱶: 湲곗〈 濡쒖쭅 ?????????????????????????????????
    # ----------------------------------------------------------
    def _run_default_mode(self):
        """湲곕낯 紐⑤뱶: 湲곗〈 濡쒖쭅 ?섑뻾"""
        # ?? ?먮룞?щ깷 鍮꾪솢???????????????????????????????
        if not self.state.auto_hunt:
            self.state.last_bomu_time    = 0
            self.state.combat_start_time = 0.0
            humanized_sleep(TIMING_CONFIG["idle_sleep"])
            return

        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        # 0. 蹂대Т 踰꾪봽 ?좎? (185s 二쇨린)
        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        if time.time() - self.state.last_bomu_time > TIMING_CONFIG["bomu_interval"]:
            self._execute_bomu_buff_v3()

        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        # 1. ?앹〈 理쒖슦??(HP / MP ?뚮났)
        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        if self._needs_hp_recovery():
            self._recover_hp()
            self.state.combat_start_time = 0.0

        if self._needs_mp_recovery():
            self._recover_mp()
            if self._needs_hp_recovery():
                self._recover_hp()

        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        # 2. ?좎? ?먯? ???+ ?뷀떚???곗꽑?쒖쐞 ?됰룞 ?쒖뼱
        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        if self._handle_entity_priorities():
            humanized_sleep(TIMING_CONFIG["action_loop"])
            return

        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        # 3. ?꾩씠???띾뱷 (?좏쑕 ?곹깭???뚮쭔)
        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        if not self.state.target_locked and self.state.detected_item_grid:
            gx, gy = self.state.detected_item_grid
            print(f"[Item] ?꾩씠???띾뱷 ?쒕룄: {self.state.detected_item_name} @ Grid({gx}, {gy})")

            while self.state.running and self.state.auto_hunt and self.state.detected_item_grid:
                if self.state.target_locked or self._needs_hp_recovery() or self._needs_mp_recovery():
                    print("[Action] ?띾뱷 以??꾪닾/?꾧툒 ?곹솴 諛쒖깮 -> 猷⑦똿 以묐떒.")
                    break

                arrived = self._move_toward_grid(gx, gy)
                if arrived:
                    print(f"[Point] ?꾩씠?????꾩갑. ?띾뱷 ?쒕룄 (',')")
                    hw.humanized_press(",") 
                    humanized_sleep(TIMING_CONFIG["enter_wait"])
                    self.state.detected_item_grid = None
                    print("[OK] ?꾩씠???띾뱷 ?꾨즺.")
                    break
                
                humanized_sleep(TIMING_CONFIG["nav_loop"])

        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        # 4. 醫뚰몴 湲곕컲 ?먮룞 ?붾쾭???ㅼ틪
        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        dist_change = (abs(self.state.x - self.state.last_debuff_x) +
                       abs(self.state.y - self.state.last_debuff_y))
        if self.state.auto_debuff_enabled and dist_change > 15:
            self._execute_debuff_scan_v3()

        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        # 5. ?寃??먯깋 & ?꾪닾
        # ?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧?먥븧
        has_monsters = bool(self._get_visible_monsters())
        if not self.state.target_locked:
            if not has_monsters:
                humanized_sleep(TIMING_CONFIG["nav_loop"])
                return
            self.state.combat_start_time = 0.0
            if self._search_target_v3():
                humanized_sleep(TIMING_CONFIG["ocr_fast"])
            return

        humanized_sleep(TIMING_CONFIG["ocr_fast"])
        self._execute_combat()

        humanized_sleep(TIMING_CONFIG["action_loop"])

    # ----------------------------------------------------------
    # ?? 吏???ㅽ궗 ?쒖쟾 (?꾩궗/?좎궗) ???????????????????????????
    # ----------------------------------------------------------
    def _execute_support_skills(self):
        """?꾩궗/?좎궗: 寃⑹닔???寃잛뿉 吏???ㅽ궗 ?쒖쟾"""
        # ?꾩궗/?좎궗 ?꾩슜 吏???ㅽ궗 ?쒖쟾 濡쒖쭅
        # ?? 寃⑹닔???寃잛뿉 踰꾪봽 ?ㅽ궗 ?쒖쟾, ???ㅽ궗 ?쒖쟾 ??
        # ??遺遺꾩? 媛???븷蹂??ㅽ궗 ?ㅼ젙???곕씪 ?숈옉
        pass


# ============================================================
#  Grid 醫뚰몴 蹂???⑥닔 (GridManager ?ъ슜)
# ============================================================
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
        self._last_support_quick_escape_time = 0.0
        self._follow_blocked_memory_count = 0
        self._last_support_quick_escape_dir = None
        self._last_warrior_coord: tuple[int, int] | None = None
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
        self._portal_enter_fail_streak = 0
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
        last_escape_dir = str(getattr(self, "_last_support_quick_escape_dir", "") or "")
        if len(candidate_dirs) > 1 and last_escape_dir in candidate_dirs:
            candidate_dirs = [direction for direction in candidate_dirs if direction != last_escape_dir]

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
        self._last_support_quick_escape_dir = escape_dir
        print(f"[Recover] support follow quick escape: {escape_dir} (no retarget).")
        hw.hold_move(escape_dir, "stuck_side_hold", duration=0.085)
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
                return False

        now = time.time()
        current_pos = (self.state.x, self.state.y)

        if self._nav_current_pos != current_pos:
            self.last_pos = self._nav_current_pos
            self._nav_current_pos = current_pos
            self._nav_attempt_pos = None
            self._nav_attempt_started_at = 0.0
            self.stuck_count = 0
            self._support_follow_soft_stuck_count = 0
            return False

        if self._nav_attempt_pos != current_pos:
            return False

        stuck_time_limit = float(TIMING_CONFIG.get("stuck_time", 4.0))
        if follow_navigation_active and follow_target:
            support_follow_active = should_defer_stuck_escape_for_support(
                f"{getattr(self.state, 'role', '')} {getattr(self.state, 'network_role', '')}",
                bool(getattr(self.state, "service_active", False)) or bool(getattr(self.state, "auto_hunt", False)),
                True,
            )
            if support_follow_active:
                stuck_time_limit = support_follow_stuck_timeout(True)
            else:
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
        warrior_xy, portal_xy = resolve_portal_follow_cells(warrior_xy[0], warrior_xy[1], dir_norm)
        started = float(started_at if started_at is not None else time.time())
        self._portal_follow_active = True
        self._portal_follow_approach = warrior_xy
        self._portal_follow_coord = portal_xy
        self._portal_follow_dir = dir_norm
        self._portal_follow_source_map_sig = tuple(source_map_sig) if source_map_sig else None
        self._portal_follow_started_at = started
        self._portal_enter_fail_streak = 0
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
        self.state.portal_follow_coord = portal_xy
        self.state.portal_follow_approach = warrior_xy
        self.state.portal_follow_dir = dir_norm
        try:
            self.state.red_tab_enabled = False
        except Exception:
            pass
        if clear_target:
            self._clear_target_for_portal_follow()
        print(
            f"[PortalFollow] {log_prefix}: warrior_last={warrior_xy}, "
            f"portal={portal_xy}, enter_dir={dir_norm or '-'}"
        )

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
                    self._last_coord_transition_seq = event_seq
            except Exception:
                event_prev = None
                event_dir = None
                event_age = None

        if self._portal_follow_active:
            if cur_ok:
                if prev and (prev[0], prev[1]) != (cur_x, cur_y):
                    if follow_manhattan_gap(prev[0], prev[1], cur_x, cur_y) == 1:
                        step = dir_between_coords(prev[0], prev[1], cur_x, cur_y)
                        if step:
                            self._last_warrior_step_dir = step
                self._last_warrior_coord = (cur_x, cur_y)
                self._last_warrior_map_sig = map_sig
                if dps_dir:
                    self._last_warrior_dir = dps_dir
            return

        map_changed = bool(prev and prev_map_sig and map_sig != prev_map_sig and any(map_sig))
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
            if isinstance(getattr(self.state, "portal_session_cache", None), dict):
                cached_hint = dict(self.state.portal_session_cache.get(source_map_sig, {}) or {})
            cached_dir = normalize_move_dir(cached_hint.get("enter_dir"))
            enter_dir = event_dir or prev_step or self._last_warrior_step_dir or cached_dir
            if not enter_dir:
                if cur_ok:
                    self._last_warrior_coord = (cur_x, cur_y)
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
        self._portal_enter_fail_streak = 0
        self.state.portal_follow_active = False
        self.state.portal_follow_finished_at = time.time()
        self.state.portal_follow_coord = None
        self.state.portal_follow_approach = None
        self.state.portal_follow_dir = None
        self._nav_attempt_pos = None
        self._nav_attempt_started_at = 0.0
        self.stuck_count = 0
        self._support_follow_soft_stuck_count = 0
        self.state.last_move_dir = ""
        print(f"[PortalFollow] finished: {reason}")

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
        target = self._get_portal_follow_target()
        if not target:
            return False
        current_pos = (int(self.state.x), int(self.state.y))
        warrior_last = self._portal_follow_approach or target
        enter_dir = normalize_move_dir(self._portal_follow_dir)
        _, portal_xy = resolve_portal_follow_cells(warrior_last[0], warrior_last[1], enter_dir)
        if not should_attempt_portal_enter(
            current_pos[0],
            current_pos[1],
            warrior_last[0],
            warrior_last[1],
            enter_dir=enter_dir,
            portal_x=portal_xy[0],
            portal_y=portal_xy[1],
        ):
            return False

        before_pos = current_pos
        print(
            f"[PortalFollow] enter zone: current={current_pos}, warrior_last={warrior_last}, "
            f"enter_dir={enter_dir or '-'}"
        )
        if not enter_dir:
            print("[PortalFollow] enter_dir missing; waiting on warrior last tile")
            return True

        # Force-hold the entry key on the exact warrior last tile (ignore support input block).
        try:
            self.state.support_input_blocked_until = 0.0
        except Exception:
            pass
        hold_sec = max(1.0, float(TIMING_CONFIG.get("entry_hold", 1.0) or 1.0))
        warped = False
        for attempt in range(3):
            if current_pos != warrior_last and current_pos != portal_xy:
                approach_dir = dir_between_coords(current_pos[0], current_pos[1], warrior_last[0], warrior_last[1])
                if approach_dir:
                    print(
                        f"[PortalFollow] re-approach warrior_last={warrior_last} "
                        f"from {current_pos} via {approach_dir}"
                    )
                    hw.hold_move(approach_dir, "move_hold", duration=0.18, force=True)
                    humanized_sleep(0.08, variance=0.02)
                    current_pos = (
                        int(getattr(self.state, "x", 0) or 0),
                        int(getattr(self.state, "y", 0) or 0),
                    )
                    if current_pos != warrior_last:
                        break

            before_pos = current_pos
            hw.hold_move(
                enter_dir,
                "entry_hold",
                duration=random.uniform(hold_sec, hold_sec + 0.35),
                force=True,
            )
            self.state.last_move_dir = enter_dir
            humanized_sleep(0.12, variance=0.04)
            after_pos = (
                int(getattr(self.state, "x", 0) or 0),
                int(getattr(self.state, "y", 0) or 0),
            )
            if should_detect_warrior_transition(
                before_pos[0],
                before_pos[1],
                after_pos[0],
                after_pos[1],
                jump_distance=WARRIOR_TRANSITION_JUMP_DISTANCE,
            ):
                warped = True
                cache_key = self._portal_follow_source_map_sig
                if cache_key is None:
                    cache_key = (
                        str(getattr(self.state, "map_name", "") or ""),
                        str(getattr(self.state, "map_floor", "") or ""),
                        str(getattr(self.state, "current_map", "") or ""),
                        str(getattr(self.state, "current_floor", "") or ""),
                    )
                cache = getattr(self.state, "portal_session_cache", None)
                if isinstance(cache, dict):
                    cache[cache_key] = {
                        "enter_dir": enter_dir,
                        "approach": [int(warrior_last[0]), int(warrior_last[1])],
                        "portal": [int(portal_xy[0]), int(portal_xy[1])],
                        "confirmed_at": time.time(),
                        "source": "runtime_success",
                    }
                self._finish_portal_follow(
                    f"entered self_transition before={before_pos}, after={after_pos}, attempt={attempt + 1}"
                )
                break
            current_pos = after_pos
            if current_pos != warrior_last:
                print(
                    f"[PortalFollow] left warrior_last tile while holding {enter_dir}: "
                    f"{before_pos} -> {current_pos}"
                )

        if warped:
            return True

        self._portal_enter_fail_streak = int(getattr(self, "_portal_enter_fail_streak", 0) or 0) + 1
        self._last_self_portal_coord = current_pos
        print(
            f"[PortalFollow] portal enter pending: current={current_pos}, "
            f"warrior_last={warrior_last}, fail_streak={self._portal_enter_fail_streak}"
        )
        # Stay in portal mode so we keep retrying instead of normal follow.
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
            # char_grid와 Grid 기반 장애물 판정은 다른 좌표계이므로 추적 경로에서 제외한다.
            if abs(dx) >= abs(dy):
                step_dir = "right" if dx > 0 else "left"
            else:
                step_dir = "down" if dy > 0 else "up"
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
        if follow_mode:
            # 2D/鍮꾨?媛??대룞 + ??2移??듬줈 湲곗?: follow?먯꽌 異??대룞???뺤떎??諛잙룄濡?hold瑜?媛蹂 ?뺤옣.
            if gap >= 4:
                hold_time = max(hold_time, 0.145)
            elif gap >= 3:
                hold_time = max(hold_time, 0.130)
            elif gap >= 2:
                hold_time = max(hold_time, 0.115)
            else:
                hold_time = max(hold_time, 0.090)

        hold_time = max(0.050, min(0.180, random.gauss(hold_time, 0.007)))
        if self._is_in_combat() or time.time() < float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0):
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
                    if should_hold_follow_position(tx, ty, cx, cy):
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

