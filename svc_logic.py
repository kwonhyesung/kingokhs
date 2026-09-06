"""
svc_logic.py  -  BIS Logic Engine (자동 사냥 상태 FSM)
──────────────────────────────────────────────────────────────
기본 상태 흐름: Emergency -> Combat -> Moving
모든 사이클에 humanized_sleep / TIMING_CONFIG 적용
전투 Fail-safe  combat_timeout 초과시 강제로 break + ESC
이동 불가(Stuck) 탐지  반복 점프나 이동 안될 경우 복구 시도
파티전 도중 "모험가가 아님" / 보너스맵 도달시 ESC
──────────────────────────────────────────────────────────────

Purpose:
    - 자동 사냥 상태 FSM(Finite State Machine) 로직 구현
    - Emergency(긴급처치) > Combat(전투) > Moving(이동) 순으로 처리
    - RouteSvc(svc_route.py)와 상태값 동기화 하여 실제 전투 진행
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
    - svc_route.py: 경로 탐색 및 이동 관리(스레드)
    - spells_config.json: 버프 설정 정보 활용
"""

import time
import os
import json
import threading
import random
import win32gui
import win32con

from bis_core import (
    AppStatus, hw, GameState,
    TIMING_CONFIG, humanized_sleep, tc, display_game_hotkey, discord_notify,
    is_game_window_active,
)
from bis_spell import RecoveryManager
import svc_hunt as hunt
from svc_monitor_common import click_offset_y

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
from support_runtime_rules import (
    adjust_follow_target_by_axis_gap,
    build_f5_hon_sequence,
    confirm_support_lock_by_hp_gain,
    SELF_HP_EMERGENCY_THRESHOLD,
    SELF_MP_PRIORITY_THRESHOLD,
    ZERO_HP_CONFIRMATIONS_REQUIRED,
    follow_manhattan_gap,
    is_support_role,
    is_confirmed_zero_hp_state,
    next_zero_hp_count,
    should_allow_party_support_cast,
    should_block_party_heal,
    should_cast_periodic_heewon,
    should_defer_heal_for_follow_distance,
    classify_map_sync,
    should_continue_self_hp_recovery,
    should_hold_follow_position,
    should_ignore_monster_combat_for_support_autohunt,
    should_prioritize_self_mp,
    should_prioritize_self_mp_over_party_heal,
    should_prioritize_follow_distance,
    should_trigger_self_hp_emergency,
    speed_up_delay,
    support_retarget_block_duration,
)

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


RESTORE_GATE_DIAG_INTERVAL_SEC = 3.0


def _ts() -> str:
    """HH:MM:SS.mmm prefix for diagnostic prints - plain print() has no
    timestamp otherwise, making tick-rate/freeze-duration issues (like
    follow stalling during heal retarget) impossible to measure from logs."""
    now = time.time()
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"


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
        self._support_warrior_pattern = "점프_user"
        self._support_wizard_pattern = "졈프_user"
        # 격수 락 시퀀스에서 click 뒤 enter로 선택 확정 + User_info(점프_user)
        # 신원 확인 단계를 넣을지. enter가 이 게임에서 채팅창을 열어버리면
        # 뒤따르는 tab/0/3 키가 채팅으로 들어가므로, 그럴 땐 이 값을 False로.
        self._warrior_lock_use_enter_confirm = True
        self._ntab_confirm_required_hits = 1
        self._ntab_confirm_timeout = 0.42
        self._warrior_next_attack_at = 0.0
        self._last_warrior_cycle_log_time = 0.0
        # 자동사냥 상태 (svc_hunt 참고)
        self._hunt_cfg_cache = ({}, 0.0)
        self._hunt_sticky = None          # 쫓던 몬스터 pos (타겟 유지용)
        self._hunt_sticky_name = ""
        self._hunt_sticky_last_seen = 0.0  # 마지막으로 실제 본 시각 (소멸 유예용)
        self._hunt_target_since = 0.0
        self._hunt_giveup_until = {}      # pos -> 이 시각까지 무시
        self._item_giveup_until = {}      # 줍기 실패한 아이템 좌표 -> 무시 만료
        self._hunt_started_map = None     # F2를 켠 시점의 맵
        self._item_job = None             # {"pos":(x,y), "tries":int, "name":str}
        self._last_hunt_log_time = 0.0
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
        self._redtab_reacquire_pending = False
        self._redtab_clear_cooldown_sec = 1.0
        self._last_party_hp_support_time = 0.0
        self._last_party_hp_value = 0
        self._self_hp_before_warrior_cast = 0
        self._self_hp_before_warrior_cast_time = 0.0
        self._self_redtab_misfire_threshold = 20000
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
        self._last_warrior_revive_time = 0.0
        self._warrior_revive_cooldown = 3.0
        self._party_direct_heal_verified = False
        self._party_direct_heal_target_prepared = False
        self._party_heal_blocked_until = 0.0
        self._direct_heal_prepare_requested = False
        self._party_direct_heal_fail_count = 0
        self._party_direct_heal_fail_cap = 2
        # redtab 재확인은 "힐 후 0.5초 동안 격수 HP 순증가가 3만 미만"일 때만 한다.
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
        self._warrior_heewon_interval = 10.0
        self._last_warrior_heewon_time = 0.0
        self._warrior_heewoncheom_interval = 25.0
        self._last_warrior_heewoncheom_time = 0.0
        self._periodic_support_min_mp = 50000
        self._last_seen_portal_follow_started_at = 0.0
        self._portal_retarget_attempt_count = 0
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

    def _require_map_sync_for_heal(self) -> bool:
        """config.json의 require_map_sync_for_heal (기본 true). 격수 PC가
        아직 이 저장소랑 다른 맵 이름 체계를 쓰고 있어서 map_sync_status가
        항상 "different"로 잘못 판정되는 임시 상황 대응용 - false면 맵이
        달라 보여도 힐을 보류하지 않는다. 격수 PC를 pull해서 맞추면
        true로 되돌려 안전장치를 복원할 것."""
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return bool(json.load(f).get("require_map_sync_for_heal", True))
        except Exception:
            return True

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
        if self._is_warrior_role():
            return False  # 격수는 자힐 스킬 없음 - 기본키'3'(공격키) 오발동 방지
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
        if self._is_warrior_role():
            return False  # 격수는 자힐 스킬 없음
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

        # 진단용: 이 함수가 실제로 호출되는지 + remote를 찾았는지를 짧은
        # 주기(0.5초)로 무조건 찍는다. 기존 [Signal]/[RemoteLookup]은 각각
        # 10초/2초 스로틀이라 세션이 짧으면 한 번도 안 찍힐 수 있어서 추가.
        now_diag = time.time()
        if now_diag - float(getattr(self, "_last_support_target_diag_time", 0.0) or 0.0) >= 0.5:
            self._last_support_target_diag_time = now_diag
            if isinstance(remote, dict) and remote:
                print(f"[SupportTargetDiag] called, remote 찾음: hp={remote.get('hp')} good_hp={remote.get('good_hp')}")
            else:
                print(f"[SupportTargetDiag] called, remote 못 찾음 (get_fresh_remote_data_by_role 결과: {remote!r})")

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
                map_info_text = str(remote.get("map_info_text", "") or "")
                fingerprint = str(remote.get("map_info_fingerprint", "") or "")
                received_at = float(remote.get("_received_at", 0.0) or 0.0)
                age = (current_time - received_at) if received_at > 0 else -1.0
                signal_map_line = (
                    f"[Signal] 격수 map - name={remote.get('map_name', '')}, "
                    f"current_map={remote.get('current_map', '')}, "
                    f"text={map_info_text[:20]!r}, fingerprint_len={len(fingerprint)}, age={age:.2f}s"
                )
                print(signal_map_line)
                discord_notify(signal_map_line)
                local_state = self.state.get_all()
                local_fp = str(local_state.get("map_info_fingerprint", "") or "")
                local_text = str(local_state.get("map_info_text", "") or "")
                local_line = (
                    f"[Signal] 도사(local) map - current_map={local_state.get('current_map', '')}, "
                    f"text={local_text[:20]!r}, fingerprint_len={len(local_fp)}, "
                    f"map_sync_status={getattr(self.state, 'map_sync_status', '')}"
                )
                print(local_line)
                discord_notify(local_line)
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
        """이동형 자힐 1틱: 3>home>enter 후 이동을 즉시 재개한다.

        3키는 기존에 잠긴 타겟이 없으면(예: 이동 중 red_tab이 풀린 상태) 힐 대신
        대상선택 박스를 띄운다. 그 박스가 열려있는 동안 방향키는 캐릭터가 아니라
        박스를 움직이므로, 이동키를 누른 채로 두면 캐릭터가 그대로 멈춰버린다.
        그래서 3>home>enter가 끝날 때까지는 이동키를 반드시 놓아야 한다."""
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
        self.state.support_input_blocked_until = max(previous_block, time.time() + 0.13)
        self._release_movement_keys_only()
        for press_key in (key, "home", "enter"):
            if not self._press_hw_key(str(press_key), variance=0.06, skip_focus_guard=True):
                return False
            self._sleep_ui_gap(0.012)
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

            if self._is_dosa_f2_follow_service():
                self._mark_self_status_scan_window()
                if self._press_hw_key("s", variance=0.10, skip_focus_guard=True):
                    self.state.last_self_status_open_time = time.time()
                    self._sleep_ui_gap(0.16)
                    self._wait_for_user_pattern(self._support_self_pattern, timeout=0.80)

            self._refresh_self_cooltime_view("[Recovery] post-revive status refresh.")
            self._execute_self_geumgang_cycle(skip_refresh=True, force_cast=True)
            self._execute_self_bomu_cycle(skip_refresh=True, force_cast=True)
            if self.state.role in ("도사", "도사1", "도사2") and self.state.service_active:
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
        # 자힐 시전 전에는 따라가기/격수 힐을 명시적으로 끄고, HP가 good_hp
        # 이상이 됐을 때만 다시 켠다 - 기존 input_block/combat_busy는 시간
        # 기반이라 재시도가 오래 걸리면 그 사이에 follow/heal이 먼저 풀려서
        # 자힐과 겹칠 수 있었다. pause_duration은 최악의 경우(모든 attempt
        # 소진)까지 커버하도록 여유 있게 잡는다 - 그래도 못 끝나면
        # _repair_dosa_f2_runtime_state 워치독이 알아서 follow를 복구한다.
        previous_follow = bool(getattr(self.state, "nav_follow_enabled", False))
        pause_duration = max(2.0, float(support_block_duration or 0.0) * max(1, int(max_attempts)))
        self._pause_follow_for_action(duration=pause_duration)
        self._party_heal_blocked_until = time.time() + pause_duration
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
                # 첫 시전만 ESC로 대상선택을 새로 열고, 이후 연타는 이미 Home으로
                # 고정된 자기 자신 타겟을 그대로 재사용 - ESC 왕복(딜레이 포함)을
                # 없애서 초당 최대 5틱인 시전 속도에 최대한 붙인다.
                self.recovery_manager.execute_self_hp_recovery(skip_esc=attempts > 0)
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
            current_hp_final = int(getattr(self.state, "hp", 0) or 0)
            good_hp_final = self._get_good_hp_threshold()
            if current_hp_final >= good_hp_final:
                print(f"[Recovery] self hp {current_hp_final} reached good_hp {good_hp_final} - resuming follow/heal.")
                self._restore_follow_after_action(previous_follow)
                self._party_heal_blocked_until = 0.0

    def _is_geumgang_bulche_detected(self) -> bool:
        """magic_info ROI에서 금강불체 버프 아이콘/텍스트가 떴는지 확인한다
        (실측 캡처된 FindText 패턴 기반 - _execute_self_geumgang_cycle의
        주기 사이클이 사용한다). magic_info ROI나 findtext_patterns.json의
        magic 카테고리가 아직 캘리브레이션/캡처 안 됐으면 조용히 False."""
        matcher = getattr(self.state, "pattern_matcher", None)
        frame = getattr(self.state, "last_frame", None)
        if matcher is None or frame is None or getattr(frame, "size", 0) == 0:
            return False
        crop_result = self._get_config_roi_crop(frame, "magic_info")
        if crop_result is None:
            # magic_info ROI가 아직 캘리브레이션 안 됨(sx/sy/dx/dy 전부 0).
            # 예전엔 여기서 무조건 False -> 0키를 영구히 맹목 발사했다
            # (로그의 magic_info_roi=missing/zero). cooltime_area 스킬바에서
            # 읽은 self_gg_detected로 폴백해서 최소한의 확인은 하게 한다.
            # ponytail: 폴백. magic_info ROI만 잡으면 이 분기는 무의미해진다.
            return bool(getattr(self.state, "self_gg_detected", False))
        crop, _, _ = crop_result
        hits = matcher.find_text_scan(crop, "magic", "STATUS")
        return any(str(h.get("name", "")) == "금강불체" for h in hits)

    def _maintain_geumgang_buff(self) -> bool:
        """GG(금강) 유지: 11초에 한 번씩 magic_info에서 금강불체가 있는지
        확인해서, 있으면 아무것도 안 하고 없으면 0을 누른다 - 그게 전부다.
        jump2 자기상태창 확인이나 cooltime_area 스캔 같은 다른 게이트는
        안 거친다(요청에 따라 제거)."""
        if int(getattr(self.state, "hp", 0) or 0) <= 0:
            return False
        if not self._is_hw_ready() or not self._is_game_window_active():
            return False
        now = time.time()
        if now - float(getattr(self, "_last_gg_check_time", 0.0) or 0.0) < 11.0:
            return False
        self._last_gg_check_time = now
        matcher = getattr(self.state, "pattern_matcher", None)
        frame = getattr(self.state, "last_frame", None)
        crop_result = self._get_config_roi_crop(frame, "magic_info") if (matcher is not None and frame is not None) else None
        # magic_info ROI가 없으면 _is_geumgang_bulche_detected가 cooltime_area의
        # self_gg_detected로 폴백하는데, 그 값은 S를 눌러 쿨타임뷰를 갱신한
        # 직후에만 신선하다 - 폴백일 때만 여기서 한 번 갱신해준다(ROI를
        # 잡고 나면 이 S 입력도 안 나간다).
        if crop_result is None:
            self._refresh_self_cooltime_view("[Buff] self gg check (magic_info ROI 없음). Press S for cooltime.")
        detected = self._is_geumgang_bulche_detected()
        if now - float(getattr(self, "_last_gg_maintain_log_time", 0.0) or 0.0) >= 2.0:
            self._last_gg_maintain_log_time = now
            print(
                f"[GGProbe] {_ts()} detected={detected} matcher={'set' if matcher is not None else 'None'} "
                f"frame={'set' if frame is not None else 'None'} magic_info_roi={'ok' if crop_result is not None else 'missing/zero'}"
            )
        if detected:
            return False
        if now - float(getattr(self, "_last_gg_maintain_press_time", 0.0) or 0.0) < 0.3:
            return False
        self._last_gg_maintain_press_time = now
        self._press_hw_key("0", variance=0.10, skip_focus_guard=True)
        self.state.last_self_gg_cast_time = now
        src = "magic_info" if crop_result is not None else "cooltime-fallback"
        print(f"[Buff] self gg missing ({src}). Cast 0.")
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
            # GG(금강)는 이제 _execute_self_geumgang_cycle의 독립적인 주기
            # 사이클로만 관리한다 - 자힐/MP회복 재동기화에 얹혀서 같이
            # 도는 게 아니다.
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

    def _should_defer_tab_targeting_for_distance(self, log_prefix: str) -> bool:
        """tab은 화면에 보이는(근접한) 대상만 잡힌다 - 격수가 같은 맵인데 멀리
        떨어져 있으면 ESC>TAB>TAB을 몇 번 눌러도 못 잡는다. 게다가 그 시도가
        support_targeting_active를 거는 동안 hold_move()가 통째로 막혀서
        (bis_core.py의 busy-gate) 정작 거리를 좁혀줄 추적 이동까지 멈춘다 -
        멀어서 실패하고, 그 실패가 추적을 막아서 계속 멀고, 그래서 또 실패하는
        악순환(실측: no_match 7연속 재시도가 도는 동안 도사가 제자리). 멀면
        타겟팅을 접고 추적으로 거리부터 좁히게 둔다. 다른 맵이면 거리 비교가
        의미 없으니 관여하지 않는다."""
        support_target = self.state.get_fresh_remote_data_by_role("격수")
        if not isinstance(support_target, dict) or not support_target:
            return False
        if classify_map_sync(self.state.get_all(), support_target, now=time.time()) != "same":
            return False
        try:
            warrior_distance = follow_manhattan_gap(
                int(support_target.get("x", support_target.get("pos_x", 0)) or 0),
                int(support_target.get("y", support_target.get("pos_y", 0)) or 0),
                int(getattr(self.state, "x", 0) or 0),
                int(getattr(self.state, "y", 0) or 0),
            )
        except Exception:
            return False
        if not should_prioritize_follow_distance(warrior_distance, risk_distance=7):
            return False
        now = time.time()
        if now - float(getattr(self, "_last_tab_defer_log_time", 0.0) or 0.0) >= 1.0:
            self._last_tab_defer_log_time = now
            print(f"{log_prefix} tab targeting deferred: distance={warrior_distance} - closing the gap first.")
        return True

    def _reacquire_warrior_red_tab_after_emergency(self, moving_follow: bool = False) -> bool:
        if not self._is_hw_ready() or not self._is_game_window_active():
            return False

        if self._should_defer_tab_targeting_for_distance("[Recovery] red_tab reacquire:"):
            return False

        self._invalidate_warrior_redtab_verification()
        self._party_direct_heal_target_prepared = False
        self._set_support_input_block(0.75 if moving_follow else support_retarget_block_duration(False))
        self.state.support_targeting_active = True
        if not moving_follow:
            self._stop_support_movement_inputs()
        else:
            # See _prepare_direct_tab_heal_target for why this is needed even
            # while following: support_input_blocked_until only stops future
            # movement, not a key already physically held from a step
            # RouteSvc issued just before this function started.
            self._release_movement_keys_only()
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
            # casted_mp만으로는 "성공"을 보장 못 한다 - 그건 그냥 "가드에
            # 안 걸리고 키를 눌렀다"는 뜻이지, 스킬 자체가 게임 안에서
            # 성공했는지는 별개다(캐스트 실패 확률이 있음). MP가 실제로
            # 올랐는지로 진짜 성공 여부를 확인한다.
            mp_actually_rose = casted_mp and after_mp > before_mp
            if not casted_mp:
                self._self_mp_priority_failed_until = time.time() + 0.35
                print(
                    f"[Recovery] self MP boost skipped: before={before_mp}, after={after_mp}. "
                    "Allow warrior heal before retry."
                )
            elif not mp_actually_rose:
                # 캐스트는 나갔는데 MP가 안 올랐다 - 게임 쪽에서 실패한 것으로
                # 본다. "성공" defer(0.35s, 다음 판단을 늦춤)를 걸지 않고
                # 실패와 같은 짧은 재시도 창을 줘서 곧바로 다시 시도하게 한다.
                self._self_mp_priority_failed_until = time.time() + 0.35
                print(
                    f"[Recovery] self MP boost cast fired but MP didn't rise "
                    f"(mp={before_mp}->{after_mp}) - treating as failed cast. Retrying soon."
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
            if self.state.role not in ("도사", "도사1", "도사2") or not bool(getattr(self.state, "service_active", False)):
                return True
            if not mp_actually_rose:
                print(f"{source_log} MP cast failed. Keep warrior red_tab state unchanged.")
                return True
            if mp_actually_rose:
                red_tab_reacquired = self._reacquire_warrior_red_tab_after_emergency(moving_follow=True)
                if not red_tab_reacquired:
                    self._party_heal_blocked_until = time.time() + 0.15
                    print(f"{source_log} red_tab reacquire failed after MP cast. Follow remains active.")
                else:
                    print(f"{source_log} red_tab reacquired after MP cast. Follow remains active.")
                    if support_target is None:
                        support_target = self._get_support_target_data()
                    if support_target:
                        self._recover_party_hp(support_target)
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

        # esc 없이 시도해봤는데 실제로 문제가 있었다: 힐이 초당 5틱으로
        # 계속 도는 동안은 힐 대상박스가 계속 열려있는 상태라, 그걸 안 닫고
        # 2키를 누르면 박스가 남아있는 채로 진행된다. 다시 복원 - esc로
        # 열려있는 박스를 전부 비활성화하고 시작한다(격수 red_tab이 그
        # 과정에서 풀리는 건 알고 있고, 그래서 그 다음 재탐색이 필요한 것도
        # 정상이다).
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

        if self._is_warrior_role():
            # 격수 자동사냥은 접근-회전-공격을 직접 한다. 여기서 True를
            # 돌려주면 is_combat_busy가 켜지고 RouteSvc가 이동을 통째로
            # 멈춰서 몬스터 옆칸까지 걸어갈 수가 없다.
            return False

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

        if not self._party_direct_heal_verified:
            if not self._party_direct_heal_target_prepared and not self._prepare_direct_tab_heal_target():
                now = time.time()
                if (now - float(self._last_warrior_redtab_skip_log_time or 0.0)) >= 0.8:
                    print(f"[Support] {_ts()} HP heal skipped: direct target prepare failed.")
                    self._last_warrior_redtab_skip_log_time = now
                self._register_direct_heal_prepare_failure()
                return False
            if not self._party_direct_heal_target_prepared:
                print(f"[Support] {_ts()} direct heal target prepared: esc -> tab -> tab")
                self._party_direct_heal_target_prepared = True

        if not self._cast_party_direct_heal(snapshot):
            self._register_direct_heal_prepare_failure()
            return False

        if self._is_monster_target_selected():
            # HP정체 스트릭(최대 5틱)까지 기다릴 필요 없이, 대상이 몬스터로
            # 바뀐 건 확실한 신호라 즉시 재확인으로 넘어간다 - 몬스터한테
            # 계속 3키를 날리며 격수는 안 낫는 낭비를 막는다.
            print(f"[Support] {_ts()} target drifted to a monster mid-heal. Retargeting.")
            self._party_direct_heal_verified = False
            self._party_direct_heal_target_prepared = False
            self._register_direct_heal_prepare_failure()
            return False

        current_self_hp = int(getattr(self.state, "hp", 0) or 0)
        self_hp_rise = current_self_hp - self._self_hp_before_warrior_cast
        if self_hp_rise >= self._self_redtab_misfire_threshold:
            # 격수 대상 직접 힐(전용 자힐 스킬과 다른 키)을 눌렀는데 도사 본인
            # HP가 올랐다 - red_tab이 격수가 아니라 도사 자신에게 걸려있다는
            # 확실한 증거다. 격수 원격 HP 정체를 기다리는 스태그넌트 캡(최대
            # ~3초)보다 훨씬 빠르게(캐스트 1번 만에) 잡아서 즉시 재확인한다.
            print(
                f"[Support] {_ts()} self HP rose by {self_hp_rise} after warrior-targeted "
                f"heal cast - red_tab is on self, not warrior. Forcing retarget."
            )
            self._party_direct_heal_verified = False
            self._party_direct_heal_target_prepared = False
            self._register_direct_heal_prepare_failure()
            return False

        self._last_party_hp_value = before_hp

        self._party_direct_heal_verified = True
        self._party_direct_heal_fail_count = 0

        # '격수 HP가 0.5초 안에 30000만큼 안 올랐으면 red_tab이 틀린 것'이라는
        # 추정으로 재타겟을 걸던 블록을 지웠다. 실측 로그에서 34번 발동했는데
        # (자기 HP 상승이라는 확증 경로는 같은 기간 1번), 격수가 맞는 만큼
        # 맞으면 HP는 원래 안 오른다 - 힐이 제대로 들어가는 중에도 계속
        # 재타겟을 걸어서 힐이 끊겼다. red_tab이 틀렸다는 '증거'는 아래 두
        # 가지로 충분하다: 대상이 몬스터로 바뀜 / 도사 자기 HP가 오름.
        self._party_direct_heal_target_prepared = True
        return True

    def _register_direct_heal_prepare_failure(self):
        """esc->tab->tab 타겟 준비가 계속 실패하면(주로 red_tab OCR 순간 flicker로
        prepare 직후 재확인에서 걸림) fail_count만 쌓이고 실제로 재시도를 막는 데
        쓰이지 않아서, 준비 시도마다 support_input_blocked_until(0.75s)이 계속
        재무장되어 이동(추적/포탈 따라가기)이 영원히 멈추는 문제가 있었다.
        cap을 넘기면 잠깐 물러나 이동이 진행될 틈을 준다."""
        self._party_direct_heal_fail_count += 1
        if self._party_direct_heal_fail_count < self._party_direct_heal_fail_cap:
            return
        print(
            "[Support] direct heal target prepare gave up after "
            f"{self._party_direct_heal_fail_count} attempts. Backing off to let follow move."
        )
        self._party_direct_heal_fail_count = 0
        self._party_direct_heal_target_prepared = False
        self._party_heal_blocked_until = time.time() + 0.8
        self.state.support_input_blocked_until = min(
            float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0),
            time.time() + 0.02,
        )

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
        if bool(getattr(self.state, "portal_follow_active", False)):
            # Keep the request pending (don't clear the flag) so it retries
            # once the portal crossing finishes, instead of the request
            # getting silently dropped by _prepare_direct_tab_heal_target's
            # own portal_follow_active guard below.
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
        print("[Support] 자동사냥 초기 direct target prepared.")
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
        if bool(getattr(self.state, "portal_follow_active", False)):
            # esc>tab>tab opens a target-select box and eats movement keys
            # (see _release_movement_keys_only below) - doing that before
            # the warrior's portal crossing is confirmed complete can steal
            # the follow's own keys mid-step and break the crossing. The
            # warrior-critical override further up lets healing proceed
            # during portal-follow, but re-acquiring the target this way
            # must still wait for portal_follow_active to clear first.
            return False
        if self._should_defer_tab_targeting_for_distance("[Support] direct target prepare:"):
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
        else:
            # support_input_blocked_until only stops FUTURE hold_move() calls
            # from starting - it does nothing about a direction key that's
            # already physically down mid-hold from a step RouteSvc issued
            # just before this ran (hold_move sleeps through its full
            # duration in its own thread regardless of what this thread does
            # concurrently). If that overlaps with esc/tab opening the
            # target-select box below, the still-held key drives the box
            # instead of the character and the red_tab lock never completes
            # cleanly. Release just the arrow keys (not esc/enter/etc, unlike
            # the full stop_all_inputs used in the non-follow branch above)
            # so normal following isn't otherwise disrupted.
            self._release_movement_keys_only()
        self._support_lock_search_until = time.time() + 0.60
        self.state.red_tab_promotion_active = False
        self.state.red_tab_promotion_until = 0.0
        self.state.support_targeting_active = True
        self._set_combat_busy(True)
        try:
            # 예전엔 tab -> tab만 누르고 red_tab_enabled(뭔가 선택됐다는 신호)만
            # 봤다 - 몬스터/다른 유저/자기 자신을 잘못 잡아도 구분이 안 됐다.
            # _confirm_and_lock_warrior_target()가 대상을 Enter로 확정하고
            # User_info ROI(findtext_patterns.json의 점프_user)로 신원까지
            # 확인한 뒤에만 최종 lock(esc->tab->tab)을 건다.
            if self._confirm_and_lock_warrior_target():
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

    def _revive_warrior_if_dead(self, snapshot: dict | None) -> bool:
        """격수 HP가 0(사망)이면 일반 힐키(3)로는 못 살린다 - red_tab이 격수에
        걸린 상태에서 부활키 '5' -> 게임 내 확정 핫키 'e' 순으로 눌러
        부활시키고 곧바로 힐을 이어서 시전한다. red_tab이 아직 안 잡혀
        있으면(_party_direct_heal_verified=False, 재확인 중) 부활도 소용없으니
        정상 재확인 경로로 넘긴다."""
        if not snapshot:
            return False
        if int(snapshot.get("hp", 0) or 0) > 0:
            return False
        if not self._party_direct_heal_verified:
            return False
        if self._is_portal_support_paused():
            return False
        now = time.time()
        if now - float(getattr(self, "_last_warrior_revive_time", 0.0) or 0.0) < self._warrior_revive_cooldown:
            return False
        self._last_warrior_revive_time = now
        print(f"[Support] {_ts()} warrior HP=0. Casting revive: key=5 -> e, then heal.")
        if not self._press_hw_key("5", variance=0.10):
            return True
        self._sleep_ui_gap(random.uniform(0.10, 0.16))
        if not self._press_hw_key("e", variance=0.10):
            return True
        self._sleep_ui_gap(random.uniform(0.10, 0.16))
        self._cast_party_direct_heal(snapshot)
        return True

    def _cast_party_direct_heal(self, snapshot: dict | None = None) -> bool:
        if self._is_portal_support_paused():
            return False
        if not self._is_hw_ready():
            return False
        # 아직 HP상승으로 검증된 적이 없으면(첫 캐스팅) red_tab OCR로
        # 한 번은 확인한다. 이미 검증됐으면(_party_direct_heal_verified)
        # OCR 순간 flicker에 막히지 않고 그냥 쏜다 - 실제로 타겟을 잃었는지는
        # 호출부(_recover_party_hp)가 캐스팅 후 HP상승 여부로 판단한다.
        if not self._party_direct_heal_verified and not should_allow_party_support_cast(
            bool(getattr(self.state, "red_tab_enabled", False)),
            self._party_direct_heal_target_prepared,
        ):
            return False
        hp_val = int((snapshot or {}).get("hp", 0) or 0)
        now = time.time()
        # Unthrottled (was 1s-throttled) - a 1s gate hides the very thing
        # we need this line for: the actual gap between individual casts.
        # 지금 '누가' 선택돼 있는지 같이 찍는다. red_tab_enabled는 '무언가
        # 선택됨'일 뿐이라, 힐이 계속 나가는데 격수 HP가 안 오르는 상황
        # (실측: 37초 동안 299,676 -> 299,047로 오히려 감소)에서 대상이
        # 격수인지 도사 자신인지 구분할 방법이 로그에 없었다.
        # user_info ROI가 읽은 이름이 '점프_user'가 아니면 힐은 딴 데로 간다.
        print(f"[Support] {_ts()} HP heal casting on warrior: "
              f"key={self._party_direct_heal_key}, hp={hp_val}, "
              f"direct_verified={self._party_direct_heal_verified}, "
              f"선택대상={self._get_current_user_pattern() or '?'}"
              f"(raw={str(getattr(self.state, 'user_info_text', '') or '')!r} "
              f"score={float(getattr(self.state, 'user_score', 0.0) or 0.0):.2f})")
        self._last_party_direct_heal_log_time = now
        # 이미 red_tab이 잠긴 대상에게 핫키 한 번 탭하는 것뿐이라 이동을 막을
        # 필요가 없다 (자힐 수정과 동일한 이유 - 방향키와 스킬키는 서로 다른
        # 채널이라 동시에 눌러도 충돌하지 않는다). support_targeting_active는
        # 실제 ESC>TAB>TAB 재타겟팅에만 사용한다.
        # red_tab 대상이 격수가 아니라 실수로 도사 자신에게 걸려 있으면, 이
        # 키(격수 대상 직접 힐)가 자기 자신을 낫게 만든다 - 격수 원격 HP가
        # 안 오르는 건 스태그넌트 캡(최대 15캐스트/~3초)이 걸려야 잡히지만,
        # 자기 HP가 오르는 건 이 캐스트 하나만으로 바로 알 수 있는 훨씬 빠른
        # 신호다. 전용 자힐 스킬과는 다른 키(격수 red_tab 직접 힐)이므로
        # 자기 HP가 오르면 안 되는 게 정상 - 오르면 잘못된 타겟이 확실하다.
        self._self_hp_before_warrior_cast = int(getattr(self.state, "hp", 0) or 0)
        self._self_hp_before_warrior_cast_time = time.time()
        if not self._press_hw_key(self._party_direct_heal_key, variance=0.10):
            return False
        self._last_party_hp_support_time = time.time()
        self._sleep_ui_gap(random.uniform(0.012, 0.030))
        return True

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
        # tick_interval below was 0.20s/rep with only ~0.13s of actual key
        # delay - the rest was pure padding. Tightened to 0.14s (still above
        # the raw key-press floor as a safety margin); these durations scale
        # with it so follow-pause/input-block don't stay slower than the
        # cast itself.
        estimated_duration = 0.30 + (max(1, int(repeat_count or 1)) * 0.16)
        self._pause_follow_for_action(duration=max(1.1, estimated_duration + 0.5))
        self._set_support_input_block(1.3)
        self._stop_support_movement_inputs()
        previous_busy = bool(getattr(self.state, "is_combat_busy", False))
        self._set_combat_busy(True)
        try:
            print("[Hon] follow paused. Cast ESC>6>UP>ENTER.")
            tick_interval = 0.14
            sequence = build_f5_hon_sequence(repeat_count=repeat_count)
            for idx in range(0, len(sequence), 4):
                tick_started = time.perf_counter()
                for key, delay in zip(sequence[idx:idx + 4], (0.04, 0.04, 0.04, 0.06)):
                    if not self._press_hw_key(key, variance=0.10, skip_focus_guard=True):
                        return False
                    self._sleep_ui_gap(min(delay, 0.03 if key != "enter" else 0.04))
                remaining = tick_interval - (time.perf_counter() - tick_started)
                if remaining > 0:
                    humanized_sleep(remaining, variance=0.05)
            print("[Hon] cast sequence complete. Reacquiring warrior red_tab.")
            retargeted = self._reacquire_warrior_red_tab_after_emergency(moving_follow=False)
            if not retargeted:
                print("[Hon] warrior red_tab reacquire failed after cast.")
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
        return is_game_window_active(self.state)

    def _is_dosa_f2_follow_service(self) -> bool:
        return (
            self.state.role in ("도사", "도사1", "도사2")
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
        if not self._is_dosa_f2_follow_service():
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
        if self.state.role in ("도사", "도사1", "도사2") and self._is_follow_reposition_needed():
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
        if not self._is_dosa_f2_follow_service():
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
        # ESC로 격수 red_tab을 끊었으니, 이 자가시전이 끝나면 반드시 다시 잡아야 한다
        # - 호출자(geumgang/bomu 자버프 사이클)가 완료 시점에 소비한다.
        self._redtab_reacquire_pending = True
        self._sleep_self_buff_gap()
        if bool(getattr(self.state, "red_tab_enabled", False)):
            print("[Buff] red_tab detector still ON after ESC. Continue self buff cast anyway.")
        return True

    def _reacquire_red_tab_if_pending(self) -> None:
        if not bool(getattr(self, "_redtab_reacquire_pending", False)):
            return
        self._redtab_reacquire_pending = False
        self._reacquire_warrior_red_tab_after_emergency(moving_follow=True)

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
        # magic_info의 금강불체 FindText 패턴으로 확인한다 - cooltime_area의
        # 옛 gg 비트와이즈 패턴(self_gg_detected)보다 정확하다(실측 캡처된
        # 패턴 기반).
        if self._is_geumgang_bulche_detected():
            self._self_gg_retry_cooldown_until = 0.0
            return False

        last_cast_time = float(getattr(self.state, "last_self_gg_cast_time", 0.0) or 0.0)
        if current_time - last_cast_time < 2.0:
            return False

        if not self._clear_red_tab_for_self_cast():
            return False

        try:
            attempt_count = random.randint(2, 3)
            print(f"[Buff] self gg missing. Cast 0 x{attempt_count}.")
            for attempt in range(attempt_count):
                if self._should_abort_self_support_buff():
                    return attempt > 0
                self._press_hw_key("0", variance=0.10)
                self.state.last_self_gg_cast_time = time.time()
                if attempt < attempt_count - 1:
                    self._sleep_self_buff_gap()

            deadline = time.time() + self._self_gg_verify_timeout
            while time.time() < deadline:
                if self._is_geumgang_bulche_detected():
                    print("[Buff] self gg detected after cast burst.")
                    self._self_gg_retry_cooldown_until = 0.0
                    return True
                time.sleep(0.03)

            print("[Buff] self gg still missing after cast burst.")
            self._self_gg_retry_cooldown_until = time.time() + self._self_gg_retry_cooldown_sec
            return True
        finally:
            self._reacquire_red_tab_if_pending()

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

        try:
            print("[Buff] self bm missing. Cast 8 -> HOME -> ENTER, 9 -> HOME -> ENTER")
            self._release_movement_keys_only()
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
        finally:
            self._reacquire_red_tab_if_pending()

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

        try:
            self._release_movement_keys_only()
            acted = False
            # GG(금강)는 이제 이 사이클(jump2/cooltime_view 게이트 뒤)이 아니라
            # _maintain_geumgang_buff()가 magic_info의 금강불체 패턴만으로
            # 독립적으로 판단한다 - 여기 남는 건 BM(보무)뿐이다.
            last_buff_time = float(getattr(self.state, "last_self_buff_time", 0.0) or 0.0)
            bm_retry_locked = current_time < float(getattr(self, "_self_bm_retry_cooldown_until", 0.0) or 0.0)
            if not bm_retry_locked and not bool(getattr(self.state, "self_bm_detected", False)) and (time.time() - last_buff_time) >= 3.0:
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
        finally:
            self._reacquire_red_tab_if_pending()

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
            # spell_cast_gap/bomu_spell_gap (30ms/200ms) was tuned for the
            # self-cast path's own separate home->enter confirm step, not for
            # a bare keypress landing directly on a red_tab-locked target -
            # too tight here and the cast lands unreliably. _sleep_self_buff_gap
            # (0.20~0.50s) is what the self-cast bomu cycle already uses
            # successfully for the same 8/9 keys; reuse it instead of
            # re-tuning blind.
            self._sleep_self_buff_gap()
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
            # press_once가 이 PC의 ESP32가 인식하는 형식(K, 또는 D:/U:)을 고른다.
            # 눌림이 실제로 나갔는지를 그대로 돌려준다 - 예전엔 결과를 버리고
            # 무조건 True를 돌려줘서, 호출한 쪽이 "키를 눌렀다"고 로그를 찍고
            # 쿨다운까지 걸어놓는데 실제로는 아무것도 안 나간 경우가 있었다
            # (실측: 공격 로그는 찍히는데 게임에선 공격이 일어나지 않음).
            return bool(hw.press_once(display_game_hotkey(key), variance=variance))
        return bool(hw.fast_press(key, variance=variance))

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

    def _is_monster_target_selected(self, baseline_target_text: str = "", baseline_target_kind: str = "") -> bool:
        current_kind = str(getattr(self.state, "target_kind", "") or "")
        current_text = str(getattr(self.state, "target_info_text", "") or "")
        if current_kind == "MONSTER" and current_text:
            if current_kind != baseline_target_kind or current_text != baseline_target_text:
                return True
        monster_names = getattr(self.state, "target_monster_names", []) or []
        return any(name and name in current_text for name in monster_names)

    # 이름표(격수 캐릭터 머리 위, 항상 떠 있음). 스프라이트(점프_back/top/
    # left/right)로 찾다가 이름표로 바꿨다 - 이유 둘:
    #   1. 스프라이트는 방향별로 크기가 제각각(폭 12/15/29/36px)이라 '중심에서
    #      캐릭터까지'의 오프셋이 방향마다 달라진다. 이름표는 방향이 없어서
    #      숫자 하나로 끝난다.
    #   2. 몬스터에 둘러싸이면 스프라이트는 이팩트에 묻히는데 이름표는 UI라
    #      안 묻힌다.
    # 이름이 곧 신원이라, 파티원 이름표가 옆에 붙어 있어도 구분이 필요 없다.
    # 격수 이름표 패턴들. 게임이 같은 이름을 상태에 따라 다른 색으로 그린다
    # (실측: 한 프레임에선 흰색 밝기 251, 13분 뒤 프레임에선 초록 밝기 197).
    # 밝기 이진화 기반 패턴 하나로는 두 색을 다 잡을 수 없어서 색깔별로
    # 따로 캡처하고, 여기서는 접두사로 전부 받는다 - 색이 하나 더 생겨도
    # 패턴만 추가하면 코드는 그대로다.
    _WARRIOR_NAME_PREFIX = "점프_name"
    # 센티넬이 본 이름표 위치를 이 시간 안이면 쓴다. 격수는 실측에서 연속
    # 관측 사이에 최대 3칸(144px) 움직였으므로 넉넉히 잡으면 안 된다.
    _NAMETAG_REUSE_SEC = 0.6
    # 그만큼 새 관측이 나올 때까지 기다려 보는 시간(센티넬 한 사이클 남짓).
    _NAMETAG_WAIT_SEC = 0.4
    # red_tab을 잡는 동안 따라가기를 멈춰두는 시간.
    _REDTAB_HOLD_SEC = 0.25

    def _get_config_roi_crop(self, frame, roi_name: str):
        """전체 프레임 대신 config.json의 roi_name 영역만 잘라 반환한다
        (crop, offset_x, offset_y). ROI가 없거나(아직 캘리브레이션 안 됨)
        0 크기면 None을 돌려준다 - 호출부가 "이 기능은 아직 설정 안 됨"으로
        처리해야 한다(전체 프레임 폴백은 느리고, 엉뚱한 위치를 스캔해서
        오탐만 만든다)."""
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                roi = json.load(f).get(roi_name, {})
            sx, sy, dx, dy = int(roi["sx"]), int(roi["sy"]), int(roi["dx"]), int(roi["dy"])
            fh, fw = frame.shape[:2]
            sx, sy = max(0, sx), max(0, sy)
            dx, dy = min(fw, dx), min(fh, dy)
            if dx <= sx or dy <= sy:
                return None
            return frame[sy:dy, sx:dx], sx, sy
        except Exception:
            return None

    def _get_play_area_crop(self, frame):
        """전체 프레임 대신 config.json의 play_area 영역만 잘라 반환한다
        (crop, offset_x, offset_y). 실패하면 프레임 전체를 offset 0으로
        돌려준다(안전한 폴백 - 느리지만 예전처럼은 동작한다)."""
        result = self._get_config_roi_crop(frame, "play_area")
        return result if result is not None else (frame, 0, 0)

    def _click_warrior_marker_and_lock(self) -> bool | None:
        """마우스 직접 클릭 방식(S/W, win32api - 아두이노 하드웨어 신호 아님):
        esc(다른 대상에게 걸려있을 red_tab 비활성화) -> tab(대상박스 활성화)
        -> 화면에서 격수 이름표(점프_name*)를 find_text_scan으로 찾아 그
        아래 click_offset_y 설정값을 클릭 -> enter(선택 확정) -> User_info
        ROI에서 점프_user(격수 신원) 확인 -> tab -> tab 으로 red_tab을 최종
        확정한다. 신원이 확인 안 돼도 힐을 막지는 않고(점프_user 패턴
        신뢰도 검증 전) 경고만 남긴 뒤 red_tab 폴백으로 진행한다.
        이름표 위치를 그대로 클릭하면 캐릭터가 안 잡힌다 - 실측으로 26px
        아래여야 잡히고, 더 내려가면 한 칸 아래 대상이 잡힌다. 현재값은 config.json의 play_area.click_offset_y다.
        클릭이 실제로 이 PC의 커서를 옮기는지는 hw.click_pixel() 안에서
        GetCursorPos로 되읽어 [Click] 로그로 남긴다.
        반환값: True/False = 캐릭터를 찾아서 클릭까지 시도함(성공/실패),
        None = 화면에서 격수를 못 찾음(몬스터/장애물에 가려짐, 화면 밖 등) -
        호출부가 기존 방향키 순차 탐색으로 폴백해야 한다."""
        matcher = getattr(self.state, "pattern_matcher", None)
        frame = getattr(self.state, "last_frame", None)
        if matcher is None or frame is None or getattr(frame, "size", 0) == 0:
            return None
        # esc가 없으면 다른 대상(도사 자신 등)에게 이미 걸려있던 red_tab이
        # 안 풀린 채로 진행돼서 클릭이 새 대상을 잡아도 예전 lock과 꼬일 수
        # 있다 - 먼저 비활성화하고 시작해야 한다.
        if not self._press_hw_key("esc", variance=0.10):
            return None
        self._sleep_ui_gap(0.03)
        if not self._press_hw_key("tab", variance=0.10):
            return None
        self._sleep_ui_gap(0.03)

        # 이전 시도(클릭)로 커서가 격수 캐릭터 위에 그대로 남아있으면, 커서
        # 아이콘이 캐릭터/패턴을 가려서 스캔이 계속 실패하는 원인이 된다 -
        # 스캔 직전에 화면 구석(0,0)으로 미리 빼두고, 캡처가 그 상태를
        # 반영할 시간을 준 뒤 프레임을 다시 읽는다.
        hw.move_cursor(0, 0)
        self._sleep_ui_gap(0.01)
        refreshed_frame = getattr(self.state, "last_frame", None)
        if refreshed_frame is not None:
            frame = refreshed_frame

        # 전체 프레임(1920x1080)을 통째로 스캔하면 느리다(tab 이후 체감
        # 딜레이의 실제 원인). 격수는 play_area 안에서만 보이므로 그 영역만
        # 잘라서 스캔한다 - 훨씬 작은 이미지라 훨씬 빠르다. 매치 좌표는
        # crop 기준이라 클릭 전에 play_area의 화면 offset을 다시 더해야 한다.
        scan_crop, offset_x, offset_y = self._get_play_area_crop(frame)
        hits = [
            h for h in matcher.find_text_scan(scan_crop, "party", "USER")
            if str(h.get("name", "")).startswith(self._WARRIOR_NAME_PREFIX)
        ]
        if hits:
            # 후보가 둘 이상 나오는 경우는 실측 로그(37회 검출) 전체에서 한
            # 번도 없었다 - 점프_name*은 격수만 가진 이름이라 정상 상황에서
            # 하나뿐이다. 첫 번째를 그대로 쓴다.
            best = hits[0]
            px = int(best.get("cx", 0)) + offset_x
            py = int(best.get("cy", 0)) + offset_y + click_offset_y()
            source = str(best.get("name"))
        else:
            # 여기서는 esc/tab을 누른 뒤 화면을 딱 한 번 본다. 하필 그 순간
            # 이름표가 몬스터/이팩트에 가려지면 실패한다 - 실측 로그에서
            # 센티넬이 17번 찾는 동안 이 경로는 9번 못 찾았다. 센티넬은 매
            # 사이클 보고 있으니 방금 본 위치를 그대로 쓴다. 격수는 한 사이클
            # 안에 한 칸 이상 움직이지 않고, 클릭 뒤 red_tab 확인이 또 있다.
            # 오래된 좌표는 쓰면 안 된다. 실측: 연속 관측 사이에 격수가 2칸
            # 이상 움직인 경우가 8회, 최대 3칸(=144px)이었다. 그래서 '방금 본
            # 것'만 쓰고, 없으면 센티넬이 다시 볼 때까지 잠깐 기다린다.
            # 따라가기는 위에서 멈춰놨으므로 기다리는 동안 화면이 흔들리지 않는다.
            fresh = []
            wait_until = time.time() + self._NAMETAG_WAIT_SEC
            while True:
                now_tag = time.time()
                fresh = [t for t in (getattr(self.state, "party_nametag_hits", []) or [])
                         if str(t.get("name", "")).startswith(self._WARRIOR_NAME_PREFIX)
                         and now_tag - float(t.get("at", 0.0) or 0.0) <= self._NAMETAG_REUSE_SEC]
                if fresh or now_tag >= wait_until:
                    break
                time.sleep(0.05)
            if not fresh:
                # 이름표가 아예 안 보이는 상태. 게임에서 이름 상시 표시가 꺼져
                # 있으면 이 경로 전체가 죽으므로, 조용히 넘기지 않는다.
                print(f"[TargetConfirm] 격수 이름표('{self._WARRIOR_NAME_PREFIX}*')를 못 찾음 "
                      f"- 이름표가 가려졌거나 화면 밖입니다")
                return None
            tag = max(fresh, key=lambda t: float(t.get("at", 0.0) or 0.0))
            px, py = int(tag["x"]), int(tag["y"])
            source = f"{tag['name']}(센티넬 {now_tag - float(tag['at']):.1f}초 전)"
        print(f"[TargetConfirm] character click: name={source} pos=({px},{py})")
        hw.click_pixel(px, py)
        self._sleep_ui_gap(0.03)
        # _ntab_in_progress를 켜면 _wait_for_red_tab_lock의 폴링 간격이
        # 0.03s->0.015s로 빨라진다(키보드 경로도 이미 이렇게 쓰고 있음).
        prev_ntab_in_progress = self._ntab_in_progress
        self._ntab_in_progress = True
        prev_ntab_active = bool(getattr(self.state, "ntab_active", False))
        prev_ntab_since = float(getattr(self.state, "ntab_active_since", 0.0) or 0.0)
        # click 다음 enter로 선택을 '확정'하면 우상단 User_info 이름표 박스에
        # 선택 대상의 이름이 뜬다. ntab_active를 켜야 svc_monitor가 그 ROI를
        # party 카테고리(*_user)로 스캔한다. 여기서 점프_user가 확인돼야
        # 진짜 격수를 잡은 것 - 안 뜨면 몬스터/딴 사람/자기 자신일 수 있다.
        # 다만 점프_user FindText 패턴 신뢰도가 아직 검증 전이라, 미확인이어도
        # 힐 자체를 막지는 않고 로그로만 남긴 뒤 red_tab 폴백으로 진행한다.
        # ponytail: 미확인 시 경고 후 진행. 패턴 안정화되면 여기서 하드 리턴.
        self.state.ntab_active = True
        self.state.ntab_active_since = time.time()
        identity_ok = False
        try:
            if self._warrior_lock_use_enter_confirm and self._press_hw_key("enter", variance=0.10):
                self._sleep_ui_gap(0.03)
                identity_ok = self._wait_for_user_pattern(
                    self._support_warrior_pattern, timeout=0.45
                )
            if identity_ok:
                print(f"[TargetConfirm] 신원 확인 OK: {self._support_warrior_pattern}")
            elif self._warrior_lock_use_enter_confirm:
                seen = self._get_current_user_pattern() or (
                    str(getattr(self.state, "user_info_text", "") or "") or "(빈값)"
                )
                print(f"[TargetConfirm] 신원 미확인 (User_info='{seen}') - red_tab만으로 진행")
            # tab -> tab 으로 red_tab 최종 승격 (요청대로 2회).
            if not self._press_hw_key("tab", variance=0.10):
                return False
            self._sleep_ui_gap(0.05)
            self._press_hw_key("tab", variance=0.10)
            self._sleep_ui_gap(0.03)
            if not self._wait_for_red_tab_lock(timeout=0.20, min_hits=1):
                return False
            # red_tab_enabled는 '무언가 선택됨'이라는 뜻일 뿐 '격수가 선택됨'이
            # 아니다. 몬스터가 이미 잡혀 있으면 격수를 클릭하지도 않고 성공으로
            # 보고돼서 재시도조차 안 했다 - 실측 로그는 "confirmed red_tab"인데
            # 화면의 선택 상자는 바닥의 몬스터에 있었다("단 한 번도 격수에
            # 안 걸렸다"의 정체).
            if self._is_monster_target_selected():
                print("[TargetConfirm] 클릭 후에도 대상이 몬스터다 - 격수 락 실패로 처리")
                return False
            return True
        finally:
            self._ntab_in_progress = prev_ntab_in_progress
            self.state.ntab_active = prev_ntab_active
            self.state.ntab_active_since = prev_ntab_since

    def _confirm_and_lock_warrior_target(self) -> bool:
        """격수 이름표(점프_name)를 마우스로 클릭해 red_tab을 세운다.

        예전엔 클릭이 실패하면 esc -> tab -> 위 -> enter를 최대 20번 돌려
        후보를 하나씩 넘겨보며 점프_user를 찾는 폴백이 있었다. 지웠다:
          - 후보 순서가 매번 무작위라 격수가 걸릴 때까지 게임 입력을 수십 번
            난사한다(로그의 "[TargetConfirm] attempt=N sequence=..." 반복).
          - enter가 대상을 '확정'하는 키라, 걸러내기 전에 엉뚱한 대상을
            잡아버리는 사고가 구조적으로 남는다.
        클릭이 실패하면 그냥 실패로 두고 다음 주기에 다시 시도하는 편이 싸다.
        """
        # 잡는 동안은 제자리에 선다. 클릭 좌표는 화면 좌표라, 도사가 한 걸음만
        # 움직여도 카메라가 따라 움직여서 그 좌표가 통째로 어긋난다 - 이름표를
        # 제대로 찾고도 엉뚱한 곳을 클릭하게 된다.
        previous_follow = bool(getattr(self.state, "nav_follow_enabled", False))
        self._pause_follow_for_action(duration=self._REDTAB_HOLD_SEC)
        try:
            for mouse_attempt in range(1, 4):
                marker_result = self._click_warrior_marker_and_lock()
                if marker_result is True:
                    return True
                print(f"[TargetConfirm] mouse attempt={mouse_attempt}/3 result={marker_result}")
            # tab으로 열린 대상박스가 남아있을 수 있으니 닫고 끝낸다.
            self._press_hw_key("esc", variance=0.10)
            return False
        finally:
            self._restore_follow_after_action(previous_follow)

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
        # 격수는 8/9 버프를 안 쓴다. 그냥 낭비가 아니라 해가 된다 - 이 시전은
        # home으로 자기를 타겟팅해서 red_tab을 덮고, 끝나면 red_tab 재확보가
        # 돌면서 support_input_blocked_until을 세워 사냥 이동을 막는다
        # (실측: 버프 직후 [NavBlock] support_input_blocked=True).
        if self._is_warrior_role():
            self.state.last_bomu_time = time.time()   # 186초 뒤 또 시도하지 않게
            return

        print("[Buff] 蹂대Т 踰꾪봽 ?쒖쟾 以?(8 -> 9 ?쒖감)...")
        # home으로 자기 자신을 타겟팅하므로, 격수에게 걸려있던 red_tab을 덮어쓴다.
        # 이동키가 눌려있으면 대상선택 박스가 캐릭터 대신 움직여 멈추므로 먼저 놓고,
        # 끝나면 바로 격수 red_tab을 재확보해서 끊긴 시간을 최소화한다.
        self._release_movement_keys_only()
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
        self._reacquire_warrior_red_tab_after_emergency(moving_follow=True)

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
            world = item.get("world_pos")
            if world and not self.state.detected_item_world:
                print(f"[Priority4] ITEM 감지 -> 루팅: {item.get('name','')} @ {world}")
                self.state.detected_item_name = item.get("name", "")
                self.state.detected_item_world = world
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
            # 격수 자동사냥도 Sentinel 감지가 전제다 (F2로 service_active만
            # 켜고 auto_hunt가 꺼져 있으면 몬스터/아이템이 하나도 안 잡힌다).
            if (not self.state.sentinel_enabled
                    and self._is_warrior_role()
                    and getattr(self.state, "service_active", False)):
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
                if self._is_warrior_role():
                    # 값을 같이 찍는다. '긴급 처리 중'만으로는 HP가 정말 낮은
                    # 건지, OCR이 0을 읽고 있는 건지, 임계값이 잘못된 건지
                    # 구분할 수 없다 - 이 셋은 대응이 전혀 다르다.
                    self._hunt_idle_reason(
                        "긴급(HP/MP) 처리 중 - 사냥 사이클까지 못 감 | "
                        f"hp={getattr(self.state, 'hp', 0)}/{self._get_good_hp_threshold()} "
                        f"mp={getattr(self.state, 'mp', 0)}/{self._get_good_mp_threshold()} "
                        f"hp_trig={getattr(self.state, 'hp_trig_active', False)} "
                        f"mp_trig={getattr(self.state, 'mp_trig_active', False)} "
                        f"user={getattr(self.state, 'is_user_detected', False)} "
                        f"chat={getattr(self.state, 'is_chat_active', False)}")
                self._handle_emergency(support_target)
                continue

            # ?? 1?쒖쐞: Combat (?꾪닾) ???????????????????????????
            combat_needed = self._should_handle_combat()
            self._set_combat_busy(combat_needed)
            if combat_needed:
                if self._is_warrior_role():
                    # 우선순위 1이라 여기서 continue하면 사냥 사이클(우선순위 2)이
                    # 한 번도 안 돈다. 격수가 계속 '전투 중'으로 판정되면 몬스터가
                    # 보여도 [Hunt]가 한 줄도 안 나오는데, 지금까지 이게 조용했다.
                    self._hunt_idle_reason(
                        f"전투 처리 중(우선순위 1) - 사냥 사이클 미실행 | "
                        f"target_locked={getattr(self.state, 'target_locked', None)} "
                        f"combat_start={getattr(self.state, 'combat_start_time', 0):.0f}")
                self._handle_combat()
                continue

            # ?? 2?쒖쐞: Moving (?대룞/?ㅻ퉬寃뚯씠?? ??????????????????
            if self.state.auto_hunt or getattr(self.state, "service_active", False):
                self._handle_moving()
                continue
            if self._is_warrior_role():
                self._hunt_idle_reason("auto_hunt=False, service_active=False (F2가 안 켜짐)")

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

        # 포탈을 넘는 중이면(RouteSvc가 state.portal_follow_active로 공유해둔
        # 값) 좌표 거리 계산과 무관하게 재이동이 우선이다 - 안 그러면 격수가
        # 막 포탈을 넘어 좌표가 크게 튀는 순간에도 그냥 "거리 문제"로만 보고
        # 자버프/MP회복 우선순위 판단을 계속 진행해버린다. RouteSvc가 도는
        # 별도 스레드라 그쪽의 상세 포탈 추적 상태(_calc_follow_target 등)에는
        # 직접 접근할 수 없지만, 이 플래그는 이미 공유 state에 공개돼 있다.
        if bool(getattr(self.state, "portal_follow_active", False)):
            return True

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
        # 일시정지 중엔 아무것도 되살리지 않는다. 이 워치독은 F3가 끈
        # follow/auto_hunt/sentinel을 그대로 다시 켜버린다 - 실측 로그에서
        # "[Hotkey] Pause ON" 뒤에 "[F2Watchdog] restored: follow,auto_hunt".
        if bool(getattr(self.state, "automation_paused", False)):
            return
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
        if bool(getattr(self.state, "automation_paused", False)):
            return False  # 일시정지 중엔 service_active를 되살리지 않는다
        if not is_support_role(getattr(self.state, "network_role", "") or self.state.role):
            now_diag = time.time()
            if now_diag - float(getattr(self, "_last_restore_gate_diag_time", 0.0) or 0.0) >= RESTORE_GATE_DIAG_INTERVAL_SEC:
                self._last_restore_gate_diag_time = now_diag
                print(f"[RestoreGateDiag] blocked: not support role (network_role={getattr(self.state, 'network_role', '')!r} role={self.state.role!r})")
            return False
        if bool(getattr(self.state, "service_active", False)):
            return True
        if not self._is_game_window_active():
            # svc_monitor가 게임창 비포커스를 감지하면 안전을 위해
            # service_active를 끈다 - 여기서 창 포커스 확인 없이 바로
            # 되살리면 그 안전장치와 계속 밀당하며 켜짐/꺼짐이 반복된다
            # (로그에 [F2Watchdog] restored가 수백 번 찍히는 원인).
            now_diag = time.time()
            if now_diag - float(getattr(self, "_last_restore_gate_diag_time", 0.0) or 0.0) >= RESTORE_GATE_DIAG_INTERVAL_SEC:
                self._last_restore_gate_diag_time = now_diag
                print("[RestoreGateDiag] blocked: game window not active")
            return False
        if not (
            bool(getattr(self.state, "auto_hunt", False))
            and bool(getattr(self.state, "nav_follow_enabled", False))
        ):
            now_diag = time.time()
            if now_diag - float(getattr(self, "_last_restore_gate_diag_time", 0.0) or 0.0) >= 0.5:
                self._last_restore_gate_diag_time = now_diag
                print(f"[RestoreGateDiag] blocked: auto_hunt={getattr(self.state, 'auto_hunt', False)} nav_follow_enabled={getattr(self.state, 'nav_follow_enabled', False)}")
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
            f"[F2Idle] {_ts()} {reason} | "
            f"service={bool(getattr(self.state, 'service_active', False))} "
            f"follow={bool(getattr(self.state, 'nav_follow_enabled', False))} "
            f"auto_hunt={bool(getattr(self.state, 'auto_hunt', False))} "
            f"busy={bool(getattr(self.state, 'is_combat_busy', False))} "
            f"block_left={max(0.0, float(getattr(self.state, 'support_input_blocked_until', 0.0) or 0.0) - time.time()):.2f}s"
        )

    def _is_portal_support_paused(self) -> bool:
        # No critical-HP override here, on purpose: the warrior's red_tab
        # target is gone the instant they cross a portal (they're on a
        # different map), so pressing the heal key with no valid target
        # doesn't just whiff - it pops open the game's own target-select
        # box. That box then eats the next inputs (only ENTER-self or ESC
        # clears it) and red_tab has to be fully re-acquired via
        # esc>tab>tab afterward. An earlier version let critical HP bypass
        # this to avoid leaving the warrior unhealed through a long portal
        # crossing, but that's the wrong fix - the right one is for the
        # follow-through-portal move (svc_route.py's arrival handling) to
        # finish quickly so this pause is short, not to press heal into a
        # gone target and risk that broken state.
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

    def _handle_warrior_transition_immediate_clear(self, support_target: dict | None = None) -> bool:
        """RouteSvc detects warrior map transitions and arms portal_follow_* on
        self.state (it owns the movement side, so it must be the one deciding).
        This used to run a second, independent copy of that same detection -
        the two drifted apart on every fix (see git history). Now it just reacts
        once per new transition, keyed off portal_follow_started_at, to reset
        LogicSvc's own heal-targeting state."""
        started_at = float(getattr(self.state, "portal_follow_started_at", 0.0) or 0.0)
        if started_at <= self._last_seen_portal_follow_started_at:
            return False
        self._last_seen_portal_follow_started_at = started_at
        self._portal_support_pause_until = time.time() + 3.0
        self._force_portal_esc_clear()
        self._party_direct_heal_target_prepared = False
        self._party_direct_heal_verified = False
        self._warrior_redtab_verified = False
        print(f"[PartyHealGateDiag] blocked this cycle: warrior transition immediate clear (started_at={started_at:.2f})")
        return True

    def _run_dosa_service_cycle(self, support_target: dict | None) -> bool:
        """
        ?? ??? ?? ??.
        ?? ??/?? ??? ?? ????, ?? ?? red_tab ?? ???? ????.
        """
        now_entry_diag = time.time()
        if now_entry_diag - float(getattr(self, "_last_cycle_entry_diag_time", 0.0) or 0.0) >= 0.5:
            self._last_cycle_entry_diag_time = now_entry_diag
            print(
                f"[CycleEntryDiag] _run_dosa_service_cycle 진입, support_target={'있음' if support_target else '없음'} "
                f"service_active={getattr(self.state, 'service_active', None)}"
            )

        if not self._restore_dosa_service_if_follow_autohunt():
            if now_entry_diag - float(getattr(self, "_last_cycle_entry_fail_diag_time", 0.0) or 0.0) >= 0.5:
                self._last_cycle_entry_fail_diag_time = now_entry_diag
                print("[CycleEntryDiag] restore_dosa_service_if_follow_autohunt() 실패 -> return False")
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

        # GG(금강)는 매 사이클 무조건 확인한다 - 힐/따라가기 등 다른 로직과
        # 무관하게 magic_info에서 금강불체가 없으면 0을 누른다.
        self._maintain_geumgang_buff()

        # No critical-HP bypass here (see _is_portal_support_paused for why):
        # the warrior's red_tab target is gone the moment they cross a
        # portal, so pressing heal mid-crossing pops the game's
        # target-select box instead of actually healing. Keeping this pause
        # short is a follow-through-portal speed problem, not something to
        # route around by casting into a target that isn't there.
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
            # config.json의 require_map_sync_for_heal=false면 이 게이트를 건너뛴다.
            # 격수 PC가 이 저장소와 다른(구버전) 맵 이름 체계를 쓰고 있어서
            # map_sync_status가 항상 "different"로 잘못 판정되는 임시 상황 대응용 -
            # 격수 PC를 pull해서 맵 이름 체계를 맞추면 다시 true로 켜서 안전장치를 복원할 것.
            if map_sync_status != "same" and self._require_map_sync_for_heal():
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
            now_wd_diag = time.time()
            if now_wd_diag - float(getattr(self, "_last_warrior_debuff_gate_diag_time", 0.0) or 0.0) >= 0.5:
                self._last_warrior_debuff_gate_diag_time = now_wd_diag
                print("[PartyHealGateDiag] blocked: _warrior_debuff_active=True (stuck?)")
            self._pace_service_loop("active")
            return True

        post_debuff_remaining = float(getattr(self, "_post_debuff_follow_until", 0.0) or 0.0) - time.time()
        if post_debuff_remaining > 0:
            now_pd_diag = time.time()
            if now_pd_diag - float(getattr(self, "_last_post_debuff_gate_diag_time", 0.0) or 0.0) >= 0.5:
                self._last_post_debuff_gate_diag_time = now_pd_diag
                print(f"[PartyHealGateDiag] blocked: _post_debuff_follow_until remaining={post_debuff_remaining:.2f}s (stuck?)")
            self._pace_service_loop("active")
            return True
        if bool(getattr(self, "_post_debuff_recover_prepare_pending", False)):
            self._post_debuff_recover_prepare_pending = False
            self.request_initial_direct_heal_target_prepare()
            self._pace_service_loop("active")
            return True

        if self._update_zero_hp_state():
            now_zhp_diag = time.time()
            if now_zhp_diag - float(getattr(self, "_last_zero_hp_gate_diag_time", 0.0) or 0.0) >= 0.5:
                self._last_zero_hp_gate_diag_time = now_zhp_diag
                print(
                    f"[PartyHealGateDiag] blocked: zero_hp_state confirmed (state.hp={getattr(self.state, 'hp', None)!r}, "
                    f"death_recovery_active={self._death_recovery_active}, hw_ready={self._is_hw_ready()}, "
                    f"game_window_active={self._is_game_window_active()})"
                )
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

        # 진단용: 파티힐 직전 3개 게이트 값을 무조건(0.5초 스로틀) 찍는다.
        # 여기까지 왔다면 hw_ready/portal/map_sync/초기화 요청/혼캐스팅 게이트는
        # 전부 통과했다는 뜻 - 이 셋 중 뭐가 막고 있는지 바로 보인다.
        now_gate_diag = time.time()
        if now_gate_diag - float(getattr(self, "_last_party_heal_gate_diag_time", 0.0) or 0.0) >= 0.5:
            self._last_party_heal_gate_diag_time = now_gate_diag
            print(
                f"[PartyHealGateDiag] party_hp_needed={party_hp_needed_before_self_recover} "
                f"self_hp={current_self_hp} emergency_threshold={self._self_hp_emergency_threshold} "
                f"self_hp>threshold={current_self_hp > self._self_hp_emergency_threshold} "
                f"party_heal_blocked={self._is_party_heal_blocked()}"
            )

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
                max_attempts=8,
            )
            if recovered:
                self._complete_self_hp_recovery_reengage(
                    source_log="[Recovery] self low-HP recovery complete."
                )
            elif self.state.role in ("도사", "도사1", "도사2") and self.state.service_active:
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
            # Second, independent catch of the same rule as the early gate
            # above - state.portal_follow_active is set by RouteSvc's own
            # thread, so kept here in case it flips true in the gap between
            # that check and this one. No critical-HP bypass, same reason.
            self._invalidate_warrior_redtab_verification()
            self._portal_retarget_attempt_count = 0
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
            attempt_count = int(getattr(self, "_portal_retarget_attempt_count", 0) or 0)
            if attempt_count >= 4:
                # Cap red_tab retarget attempts after a portal move - after
                # that, drop it and let the normal heal cycle retarget on
                # its own instead of looping the esc->tab->tab sequence
                # indefinitely. Was capped at 2, which live logs show
                # exhausting (leaving red_tab off entirely) when the
                # warrior keeps moving through a chain of portals right
                # after landing - by the time attempt 2's esc->tab->tab
                # finishes (~0.5s), the distance<=4 window that let it
                # start can already be gone. Raised to 4 to give a moving
                # warrior more chances to be caught during a stable window.
                print(
                    f"[PortalFollow] red_tab retarget gave up after {attempt_count} "
                    f"attempts (distance={warrior_distance})"
                )
                self.state.portal_follow_retarget_requested = False
                self._party_direct_heal_target_prepared = False
                self._pace_service_loop("active")
                return True
            self._portal_retarget_attempt_count = attempt_count + 1
            if not self._prepare_direct_tab_heal_target():
                self._log_dosa_f2_idle_reason("portal_retarget_prepare_failed", interval=0.6)
                self._pace_service_loop("active")
                return True
            red_tab_now = bool(getattr(self.state, "red_tab_enabled", False))
            print(
                f"[PortalFollow] warrior red_tab restored after portal: "
                f"distance={warrior_distance}, red_tab={red_tab_now}, "
                f"attempt={self._portal_retarget_attempt_count}"
            )
            # _prepare_direct_tab_heal_target() already waited for a lock hit
            # before returning True, but state can flicker between then and
            # here - re-verify before trusting it, and keep the retarget flag
            # set so the next cycle retries (up to the cap above) instead of
            # silently moving on without a real tab lock.
            if not red_tab_now:
                self._log_dosa_f2_idle_reason("portal_retarget_redtab_not_active_yet", interval=0.5)
                self._pace_service_loop("active")
                return True
            self._last_party_hp_support_time = 0.0
            self.state.portal_follow_retarget_requested = False
            self._party_direct_heal_target_prepared = True
            self._portal_retarget_attempt_count = 0

        if needs_hp:
            if self._revive_warrior_if_dead(support_target):
                self._pace_service_loop("active")
                return True
            if should_defer_heal_for_follow_distance(
                follow_distance_risk,
                self._party_direct_heal_verified,
            ):
                # Only defer when a heal cast would need a fresh esc>tab>tab
                # (which releases movement keys and fights the catch-up
                # chase). Once the target is already verified, pressing the
                # heal hotkey is a free tap on a separate input channel from
                # movement - skipping it here bought nothing but threw away
                # the verified state, forcing a full re-verify (and another
                # stretch of zero heal ticks) the moment distance dropped
                # back under risk_distance. That's what capped the real
                # heal rate well under its ~5-tick/s design during follow.
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
                # "esc>tab>tab acquire" kept printing for a long stretch with
                # no heal ever landing and no prepare-success/give-up message
                # either - this exposes exactly which of the 4 pieces
                # (already-prepared flag, verified flag, fail-count, live
                # red_tab OCR reading) disagrees each cycle instead of
                # guessing at another fix blind.
                print(
                    f"[Support] HP debug: prepared={self._party_direct_heal_target_prepared} "
                    f"verified={self._party_direct_heal_verified} "
                    f"fail_count={self._party_direct_heal_fail_count} "
                    f"red_tab_enabled={bool(getattr(self.state, 'red_tab_enabled', False))} "
                    f"portal_follow_active={bool(getattr(self.state, 'portal_follow_active', False))}"
                )
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
        """이동/내비게이션 처리 (우선순위 2)"""
        # 전투 중이 아닐 때만 이동
        if not self.state.is_combat_busy:
            # 실제 걸음은 RouteSvc가 걷는다. 격수는 여기서 회복/버프 유지와
            # 사냥 사이클(접근 목표 세팅 + 회전/공격)을 함께 돌린다.
            if self._is_warrior_role():
                self._run_dps_server_mode()
            else:
                self._hunt_idle_reason(
                    f"격수 역할이 아니라고 판단됨 (role='{getattr(self.state, 'role', '')}')")
        elif self._is_warrior_role():
            self._hunt_idle_reason("is_combat_busy=True - 사냥 사이클 건너뜀")

    # ----------------------------------------------------------
    #  격수 모드: 데이터 공유 서버 역할 + 자동사냥
    # ----------------------------------------------------------
    def _run_dps_server_mode(self):
        """격수 모드: 데이터 송신 + 자동사냥(회복/버프 유지 + 사냥 사이클)."""
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

    # ----------------------------------------------------------
    #  자동사냥 (격수): 순찰 -> 몬스터 접근/공격 -> 드랍 줍기
    # ----------------------------------------------------------
    def _hunt_cfg(self) -> dict:
        """config.json의 hunt 설정 (5초마다 다시 읽음)."""
        cfg, loaded_at = self._hunt_cfg_cache
        now = time.time()
        if cfg and now - loaded_at < 5.0:
            return cfg
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f).get("hunt", {}) or {}
        except Exception:
            cfg = {}
        self._hunt_cfg_cache = (cfg, now)
        return cfg

    def _hunt_anchor(self):
        """이탈 제한의 기준점(순찰 포인트). 없으면 None = 제한 없음.

        예전엔 내 자리를 기준점으로 돌려줬는데, 그러면 순찰 중이 아닐 때도
        leash(6칸)가 걸린다 - 실측: me=(1,13)에서 8~10칸 떨어진 처녀귀신이
        전부 걸러져 사냥이 멈췄다. 안 쫓게 만들려는 건 '순찰 포인트에서
        멀어지는 것'이지 '화면에 보이는 몬스터'가 아니다."""
        anchor = getattr(self.state, "nav_current_route_point", None)
        if anchor and len(anchor) == 2:
            return (int(anchor[0]), int(anchor[1]))
        return None

    def _hunt_is_stationary(self, gap: float) -> bool:
        """마지막 이동키가 나간 뒤 gap초가 지났는가.
        걷는 도중엔 캐릭터가 칸 사이에 있어서 계산한 pos가 반 칸 어긋난다."""
        return (time.time() - float(getattr(self.state, "last_move_time", 0.0) or 0.0)) >= gap

    def _clear_hunt_move_target(self):
        self.state.item_pickup_target = None
        self.state.item_pickup_arrived = False

    def _release_hunt_target_lock(self):
        """Tab으로 lock해둔 타겟을 놓는다. lock된 채로 방치하면 다음 몬스터를
        공격할 때 엉뚱한(죽은) 타겟에 대고 대상선택박스가 뜰 수 있다."""
        if self.state.target_locked:
            hw.humanized_press("esc")
        self.state.target_locked = False
        self.state.target_name = ""

    def _known_walls(self) -> set:
        try:
            map_name = str(getattr(self.state, "current_map", "") or "")
            walls = (getattr(self.state, "maps_db", {}) or {}).get(map_name, {}).get("walls") or []
            return {(int(c[0]), int(c[1])) for c in walls}
        except Exception:
            return set()

    def _hunt_idle_reason(self, why: str):
        """사냥이 한 발도 못 나간 이유를 남긴다(5초 제한).

        이 함수가 없던 동안 실패가 전부 조용한 return이라, '몬스터는 감지되는데
        [Hunt] 로그가 한 줄도 없다'는 상태에서 원인을 로그로 좁힐 수가 없었다.
        service_active부터 leash까지 어디서 멈췄는지 한 줄로 말하게 한다."""
        now = time.time()
        if now - getattr(self, "_last_hunt_idle_log", 0.0) < 5.0:
            return
        self._last_hunt_idle_log = now
        print(f"[HuntIdle] {why}")

    def _run_warrior_attack_loot_cycle(self):
        """격수 자동사냥 한 사이클. 이동은 RouteSvc가 하고 여기선 목표만 정한다."""
        if not bool(getattr(self.state, "service_active", False)):
            self._hunt_idle_reason("service_active=False (F2로 서비스가 안 켜짐)")
            self._hunt_started_map = None
            return
        cfg = self._hunt_cfg()
        if not cfg.get("enabled", True):
            self._hunt_idle_reason("config hunt.enabled=False")
            return

        # 사냥터를 벗어났으면 멈춘다. 실측: 흉가1의 벽을 왼쪽으로 우회하다
        # x=0의 포탈을 밟아 '천안궁성흉가입구'로 넘어갔고, 거기서도 사냥
        # 로직이 계속 돌았다. 다른 맵의 좌표계로 몬스터를 쫓으면 엉뚱한 데를
        # 헤매고 벽 학습까지 오염된다.
        cur_map = str(getattr(self.state, "current_map", "") or "")
        if cur_map:
            if self._hunt_started_map is None:
                self._hunt_started_map = cur_map
            elif cur_map != self._hunt_started_map:
                self._hunt_idle_reason(
                    f"사냥터를 벗어남: '{self._hunt_started_map}' -> '{cur_map}' "
                    "- 사냥 정지 (F2를 껐다 켜면 여기서 다시 시작)")
                self._clear_hunt_move_target()
                self._item_job = None
                return

        # 캐릭터 패턴을 못 찾은 프레임은 몬스터/아이템 pos를 만들 수 없다
        # (뺄셈의 한쪽이 없음). 추정하지 않고 사냥 판단만 쉰다 - 순찰
        # 이동은 RouteSvc가 pos(x,y)로 계속한다.
        my_screen = tuple(getattr(self.state, "my_screen_pos", (0, 0)) or (0, 0))
        if my_screen == (0, 0):
            self._hunt_idle_reason(
                "내 캐릭터 패턴(점프_*)을 못 찾음 - my_screen_pos=(0,0). "
                "config play_area.self_pattern_name 확인")
            return

        me = (int(getattr(self.state, "x", 0) or 0), int(getattr(self.state, "y", 0) or 0))
        if me == (0, 0):
            self._hunt_idle_reason("좌표 OCR이 (0,0) - pos ROI를 못 읽음")
            return

        # 1) 줍는 중이면 그것부터 끝낸다
        if self._item_job is not None:
            self._run_item_pickup_step(cfg)
            return

        # 2) 몬스터 (드랍은 잡아야 생기므로 몬스터가 먼저다)
        if self._run_monster_step(cfg, me):
            return

        # 3) 몬스터가 없을 때만 아이템을 집으러 간다
        self._start_item_pickup_if_any(cfg, me)

    def _run_monster_step(self, cfg: dict, me: tuple) -> bool:
        """몬스터를 골라 접근/공격한다. 뭔가 했으면 True."""
        now = time.time()
        leash = int(cfg.get("leash_tiles", 6))
        monsters = list(getattr(self.state, "detected_monsters_world", []) or [])

        self._hunt_giveup_until = {k: v for k, v in self._hunt_giveup_until.items() if v > now}
        if self._hunt_giveup_until:
            monsters = [m for m in monsters
                        if not any(hunt.chebyshev(m.get("world") or (0, 0), k) <= 1
                                   for k in self._hunt_giveup_until)]

        anchor = self._hunt_anchor()
        target = hunt.pick_target(monsters, me, anchor, leash,
                                  sticky=self._hunt_sticky, sticky_name=self._hunt_sticky_name)
        if not target and monsters:
            # 몬스터는 보이는데 하나도 안 골랐다 - leash가 anchor(순찰 포인트)
            # 기준이라, 순찰 포인트가 멀면 눈앞의 몬스터가 통째로 걸러진다.
            worst = [(m.get("name", "?"), m.get("world"),
                      hunt.chebyshev(anchor, m.get("world") or (0, 0)))
                     for m in monsters[:4]]
            self._hunt_idle_reason(
                f"몬스터 {len(monsters)}마리 보이는데 타겟 없음 | me={me} "
                f"anchor={anchor} leash={leash} | {worst}")
        if not target:
            if self._hunt_sticky is not None:
                # 실측: 몬스터가 실제로 옆에 있어도 스캔 한 프레임 정도는
                # 놓칠 수 있다(움직이는 대상 인식은 100% 안정적이지 않음) -
                # 한 사이클이라도 못 봤다고 바로 "죽었다"로 처리하면, 인식이
                # 잠깐 흔들릴 때마다 타겟을 놓치고 처음부터 다시 찾게 된다.
                # 마지막으로 본 지 grace(1.5s)가 지나야만 진짜 소멸로 본다.
                grace = float(cfg.get("target_miss_grace_sec", 4.0))
                missed_for = now - self._hunt_sticky_last_seen
                if missed_for > grace:
                    print(f"[Hunt] 타겟 소멸 (처치 또는 이탈, {missed_for:.2f}s 미검출)")
                    self._release_hunt_target_lock()
                    self._hunt_sticky = None
                    self._hunt_target_since = 0.0
                    self._clear_hunt_move_target()
            return False

        self._hunt_sticky_last_seen = now
        target_pos = (int(target["world"][0]), int(target["world"][1]))
        if self._hunt_sticky is None or hunt.chebyshev(self._hunt_sticky, target_pos) > 2:
            self._hunt_target_since = now
            print(f"[Hunt] 타겟: {target.get('name','')} @ {target_pos}")
        self._hunt_sticky = target_pos
        self._hunt_sticky_name = target.get("name", "")

        direction = hunt.attack_dir(me, target_pos)
        if direction:
            # 붙었다 - 이동 목표를 풀고, 그쪽을 보게 한다.
            # 몬스터가 그 칸을 막고 있으므로 방향키는 제자리 회전이 된다.
            self._clear_hunt_move_target()
            self._hunt_target_since = now
            # ponytail: force=True로 눌러 이동 게이트(is_combat_busy 등)를
            # 우회한다. 격수 전용 경로라 도사 지원 입력과 겹치지 않는다.
            hw.hold_move(direction, force=True)

            # tab>up>enter(_search_target_v3)를 여기서 부르지 않는다. 그건
            # '다른 대상을 찾아 red_tab을 세우는' 검토 절차이고, 격수의 공격
            # 마법에는 대상 지정이 필요 없다 - 그냥 방향만 보고 쏘면 된다.
            # 빼면서 같이 없어지는 것: tab_wait + up_wait + ocr_wait 대기와,
            # target_locked가 켜진 채로 남아 RouteSvc가 "전투 중"으로 보고
            # 이동을 통째로 거부하던 경로(아래 실측 버그 주석 참고).
            if now >= float(getattr(self, "_warrior_next_attack_at", 0.0) or 0.0):
                role = str(getattr(self.state, "role", "") or "")
                attack_key = str((cfg.get("attack_key") or {}).get(role, "3"))
                struck = self._press_hw_key(attack_key, variance=0.08, skip_focus_guard=True)
                self._warrior_next_attack_at = now + float(cfg.get("attack_repeat_sec", 0.35))
                if now - self._last_hunt_log_time >= 1.0:
                    # 키가 실제로 나간 경우에만 "공격"이라고 쓴다. 예전엔 전송
                    # 결과와 무관하게 이 줄을 찍어서, 게임에선 아무 일도 없는데
                    # 로그만 공격했다고 말하고 있었다.
                    if struck:
                        print(f"[Hunt] 공격 {direction} -> {target_pos}")
                    else:
                        print(f"[Hunt] 공격키 '{attack_key}' 전송 실패 - 공격 안 나감 "
                              f"({direction} -> {target_pos})")
                    self._last_hunt_log_time = now
            return True

        # 아직 멀다 - 몬스터의 상하좌우 4칸 중 갈 만한 칸을 목표로 준다.
        # (몬스터 칸 자체를 목표로 주면 몬스터가 막고 있어서 '도착'이
        #  영원히 오지 않고, 벽 학습이 그 칸을 벽으로 잘못 기억한다.)
        #
        # 실측 버그: 몬스터가 잠깐 옆칸에 왔다가 다시 멀어지면(맴도는 개체라
        # 흔함) target_locked가 이전 공격 때부터 켜진 채로 남아있었다.
        # RouteSvc._is_in_combat()이 target_locked만 보고도 "전투 중"으로
        # 판단해 _move_toward()를 통째로 거부하는데(가장 먼저 하는 검사),
        # 그 실패는 portal-follow 전용 로그 경로에만 찍혀서 사냥 이동에서는
        # 완전히 조용히 실패했다 - 캐릭터가 첫 공격 이후 영원히 제자리에
        # 묶이는데 로그엔 아무 단서도 안 남는 원인이었다. 다시 걸어야 하는
        # 상황이면 lock부터 풀어야 이동이 재개된다.
        if self.state.target_locked:
            self._release_hunt_target_lock()
        lost_sec = float(cfg.get("target_lost_sec", 8.0))
        if self._hunt_target_since and (now - self._hunt_target_since) > lost_sec:
            # ponytail: leash 안에서도 못 닿는 놈(벽 너머 등)에 영원히
            # 매달리지 않게 하는 안전판. 30초 뒤 다시 시도한다.
            print(f"[Hunt] 접근 실패 {lost_sec:.0f}s -> 포기: {target_pos}")
            self._release_hunt_target_lock()
            self._hunt_giveup_until[target_pos] = now + 30.0
            self._hunt_sticky = None
            self._hunt_target_since = 0.0
            self._clear_hunt_move_target()
            return False

        cell = hunt.approach_cell(me, target_pos, self._known_walls())
        if cell is None:
            print(f"[Hunt] 접근 가능한 옆칸 없음 -> 포기: {target_pos}")
            self._release_hunt_target_lock()
            self._hunt_giveup_until[target_pos] = now + 30.0
            self._hunt_sticky = None
            self._clear_hunt_move_target()
            return False
        self.state.item_pickup_target = cell
        self.state.item_pickup_arrived = False
        return True

    def _start_item_pickup_if_any(self, cfg: dict, me: tuple):
        items = list(getattr(self.state, "detected_items_world", []) or [])
        if not items:
            return
        # 아이템 좌표는 한 번 정해서 고정한다. 그래서 '멈춘 상태'에서만
        # 계산한다 - 걷는 중에 잡으면 반 칸 어긋나 틀린 칸에 고정된다.
        if not self._hunt_is_stationary(float(cfg.get("stationary_gap_sec", 0.25))):
            return
        if bool(getattr(self.state, "inventory_full", False)):
            if time.time() - self._last_hunt_log_time >= 5.0:
                print("[Hunt] 소지품이 가득 - 줍기 건너뜀 (사냥은 계속)")
                self._last_hunt_log_time = time.time()
            return
        now_i = time.time()
        self._item_giveup_until = {k: v for k, v in self._item_giveup_until.items() if v > now_i}
        if self._item_giveup_until:
            items = [i for i in items
                     if not any(hunt.chebyshev(i.get("world") or (0, 0), k) <= 1
                                for k in self._item_giveup_until)]
            if not items:
                return
        item = min(items, key=lambda i: hunt.chebyshev(me, i.get("world") or (0, 0)))
        pos = (int(item["world"][0]), int(item["world"][1]))
        # 아이템 칸에 직접 올라서서 ',' 로 줍는다. '0' 획득 마법(바라보는 방향
        # 1칸)도 있지만 이동 중에는 실패할 수 있어서, 확실한 ',' 를 쓴다.
        self._item_job = {"pos": pos, "tries": 0, "name": item.get("name", ""), "started": time.time()}
        self.state.item_pickup_target = pos
        self.state.item_pickup_arrived = False
        print(f"[Hunt] 아이템 줍기: {item.get('name','')} @ {pos}")

    def _run_item_pickup_step(self, cfg: dict):
        job = self._item_job
        now = time.time()
        if self._needs_hp_recovery() or self._needs_mp_recovery() or getattr(self.state, "is_user_detected", False):
            print("[Hunt] 줍기 중 위협/회복 -> 중단")
            self._item_job = None
            self._clear_hunt_move_target()
            return
        if now - float(job.get("started", now)) > 20.0:
            print("[Hunt] 줍기 시간 초과 -> 포기")
            self._item_job = None
            self._clear_hunt_move_target()
            return
        if not getattr(self.state, "item_pickup_arrived", False):
            return

        # 아이템과 같은 칸에 올라섰다 - 줍기 키를 순서대로 다 써본다.
        # ','(같은 칸)와 '0'(바라보는 방향 1칸)은 닿는 범위가 서로 달라서,
        # 좌표가 한 칸 어긋났을 때 어느 쪽이 맞을지 미리 알 수 없다. 둘 다
        # 눌러 성공률을 올린다 - 헛손질 한 번이 못 줍는 것보다 싸다.
        pk = cfg.get("pickup_key")
        if isinstance(pk, dict):                      # attack_key와 같은 role별 표기
            pk = pk.get(getattr(self.state, "role", ""))
        if isinstance(pk, (list, tuple)):
            pickup_keys = [str(k) for k in pk if str(k)]      # config가 명시하면 그대로
        elif self._is_warrior_role():
            # 격수는 둘 다 쓴다. config에 옛 기본값 ','만 남아 있어도
            # '0'을 빼먹지 않게 role로 결정한다.
            pickup_keys = [",", "0"]
        elif pk:
            pickup_keys = [str(pk)]
        else:
            pickup_keys = [","]
        for key in pickup_keys:
            if not self._press_hw_key(key, variance=0.08, skip_focus_guard=True):
                # 전송 실패를 조용히 넘기면 "주웠는데 아이템이 그대로"로 보여서
                # 좌표가 어긋난 것과 구별이 안 된다.
                print(f"[Hunt] 줍기키 '{key}' 전송 실패 - 줍기 안 나감")
            humanized_sleep(TIMING_CONFIG["key_down_hold"])
        humanized_sleep(TIMING_CONFIG["enter_wait"])
        job["tries"] = int(job.get("tries", 0)) + 1
        self._clear_hunt_move_target()

        # 아직 그 자리에 아이템이 보이면 좌표가 한 칸 어긋난 것 - 주변 1칸을 훑는다
        still = [i for i in (getattr(self.state, "detected_items_world", []) or [])
                 if i.get("world") and hunt.chebyshev(i["world"], job["pos"]) <= 1]
        if not still or job["tries"] >= int(cfg.get("pickup_retry", 3)):
            if still:
                # 포기한 좌표를 30초간 무시한다. 예전엔 바로 다음 사이클에
                # 같은 칸을 또 목표로 잡았고, 그게 패턴 오탐이면 아이템이
                # '사라져' 보여서 아래 '줍기 완료'로 기록됐다 - 실제로는
                # 아무것도 안 주웠는데 로그만 성공이라 원인 추적이 막혔다.
                print(f"[Hunt] 줍기 실패 {job['tries']}회 -> 포기: {job['pos']} (30초 무시)")
                self._item_giveup_until[job["pos"]] = time.time() + 30.0
            else:
                print(f"[Hunt] 줍기 완료: {job.get('name','')} @ {job['pos']}")
            self._item_job = None
            return
        nxt = (int(still[0]["world"][0]), int(still[0]["world"][1]))
        job["pos"] = nxt
        self.state.item_pickup_target = nxt
        self.state.item_pickup_arrived = False
        print(f"[Hunt] 줍기 재시도 {job['tries']} -> {nxt}")

    def _is_warrior_role(self) -> bool:
        role = str(getattr(self.state, "role", "") or "").strip()
        network_role = str(getattr(self.state, "network_role", "") or "").strip()
        return (role in {"격수", "Warrior", "寃‹湔"}
                or network_role in {"격수", "Warrior", "寃‹湔"})

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
        # ponytail: 술사 auto_hunt(헬파이어) 좌표 연동이 아직 부정확해서
        # 임시로 꺼둠 - 따라가기 도중엔 자힐/자버프만 수행. 정확한
        # follow-aware auto_hunt 재구현 시 이 블록을 복구할 것.
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
            monster_pos = m.get("world_pos")
            if not monster_pos or len(monster_pos) != 2:
                continue
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

        if not self.state.target_locked and self.state.detected_item_world:
            tx, ty = self.state.detected_item_world
            self.state.item_pickup_target = (tx, ty)
            print(f"[Item] 아이템 픽업 시도: {self.state.detected_item_name} @ World({tx}, {ty})")

            # 실제 이동은 RouteSvc가 item_pickup_target을 보고 수행한다
            # (LogicSvc는 이동 전용 스레드가 아니라서 여기선 목표만 세팅하고
            # 도착 신호(item_pickup_arrived)를 기다린다).
            while self.state.running and self.state.auto_hunt and self.state.item_pickup_target:
                if self.state.target_locked or self._needs_hp_recovery() or self._needs_mp_recovery():
                    print("[Action] 픽업 중 위협/회복 상황 발생 -> 루프 중단.")
                    self.state.item_pickup_target = None
                    break

                if self.state.item_pickup_arrived:
                    print(f"[Point] 아이템 위치 도착. 픽업 시도 (',')")
                    hw.humanized_press(",")
                    humanized_sleep(TIMING_CONFIG["enter_wait"])
                    self.state.item_pickup_target = None
                    self.state.item_pickup_arrived = False
                    self.state.detected_item_world = None
                    print("[OK] 아이템 픽업 완료.")
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
        if not self.state.target_locked and self.state.detected_item_world:
            tx, ty = self.state.detected_item_world
            self.state.item_pickup_target = (tx, ty)
            print(f"[Item] 아이템 픽업 시도: {self.state.detected_item_name} @ World({tx}, {ty})")

            while self.state.running and self.state.auto_hunt and self.state.item_pickup_target:
                if self.state.target_locked or self._needs_hp_recovery() or self._needs_mp_recovery():
                    print("[Action] 픽업 중 위협/회복 상황 발생 -> 루프 중단.")
                    self.state.item_pickup_target = None
                    break

                if self.state.item_pickup_arrived:
                    print(f"[Point] 아이템 위치 도착. 픽업 시도 (',')")
                    hw.humanized_press(",")
                    humanized_sleep(TIMING_CONFIG["enter_wait"])
                    self.state.item_pickup_target = None
                    self.state.item_pickup_arrived = False
                    self.state.detected_item_world = None
                    print("[OK] 아이템 픽업 완료.")
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
