"""
bis_logic.py  ─  BIS Logic Engine (우선순위 기반 FSM)
──────────────────────────────────────────────────────────────
▸ 우선순위 체계: Emergency -> Combat -> Moving
▸ 전투 중에는 Navigation 즉시 대기 (인터럽트)
▸ 모든 딜레이  → humanized_sleep / TIMING_CONFIG 경유
▸ 전투 Fail-safe  → combat_timeout 초과 시 루프 즉시 break + ESC
▸ 지능형 Stuck 탈출  → 후진 → 직각 랜덤 이동 → 재탐색
▸ 사전 검증  → "걸리지 않음" / 비몬스터 타겟 즉시 ESC
──────────────────────────────────────────────────────────────
"""

import time
import threading
import random

from bis_core import (
    AppStatus, TargetType, hw, GameState,
    TIMING_CONFIG, humanized_sleep, tc, GridManager, Skill, normalize_game_hotkey
)
from bis_route import RouteManager


# ============================================================
#  ActionThread
# ============================================================
class LogicSvc(threading.Thread):
    """마법 시전 / 전투 FSM 스레드 (NavigationThread와 완전 병렬)."""

    def __init__(self, state: GameState):
        super().__init__(daemon=True)
        self.state         = state
        self.current_state = AppStatus.IDLE
        self.last_load_time = time.time()
        self.task_active   = False  # 서비스 활성화 플래그
        self._support_action_lock = threading.Lock()
        # Trigger reaction rate-limit (prevents input spam while keeping fast response).
        self._last_hp_recover_time = 0.0
        self._last_mp_recover_time = 0.0
        self._last_target_search_time = 0.0
        self._last_monster_seen_time = 0.0
        # RouteSvc와 공유되던 pause 관련 필드를 안전하게 기본값으로 둔다.
        self._was_in_combat = False
        self._advance_waypoint_after_combat = False
        self._combat_resume_context = None

    def _clear_nav_context(self):
        """LogicSvc는 네비게이션 컨텍스트를 사용하지 않으므로 no-op."""
        self._was_in_combat = False
        self._advance_waypoint_after_combat = False
        self._combat_resume_context = None

    # ----------------------------------------------------------
    # ── 내부 헬퍼: 힐 시퀀스 ─────────────────────────────────
    # ----------------------------------------------------------
    def _execute_v3_heal(self, skill):
        """초고속 자가 회복: 스킬키 → Home → Enter (모두 humanized)."""
        key = skill.effective_key() if skill is not None else ""
        if not key:
            return
        hw.humanized_press(key)
        humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
        hw.humanized_press("home")
        humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
        hw.humanized_press("enter")
        if skill is not None:
            skill.last_cast_time = time.time()

    def _press_fast(self, key: str, variance: float = 0.15):
        """
        Trigger reactions need to be fast (ft.ahk 스타일). If fast_press is available use it,
        otherwise fall back to humanized_press.
        """
        try:
            hw.fast_press(key, variance=variance)
        except Exception:
            hw.humanized_press(key, variance=variance)

    def _resolve_recovery_key(self, selected: str, default_key: str):
        """
        GUI에서 선택된 recovery_*_spell(스킬 이름)을 hotkey로 해석한다.
        - 스킬 DB에 있으면 hotkey 사용
        - selected 자체가 한 글자(숫자키 등)면 그대로 사용
        - 그 외는 default_key로 폴백
        """
        skill = None
        key = None
        try:
            if selected:
                skill = next((s for s in self.state.spells if getattr(s, "name", "") == selected), None)
                if skill is not None and getattr(skill, "effective_key", None):
                    key = skill.effective_key()
                elif isinstance(selected, str) and len(selected) == 1:
                    key = selected
        except Exception:
            skill = None
            key = None

        if not key:
            key = default_key
        return key, skill

    def _find_spell_by_name(self, *names: str) -> Skill | None:
        for name in names:
            if not name:
                continue
            needle = str(name).strip()
            if not needle:
                continue
            skill = next((s for s in self.state.spells if getattr(s, "name", "") == needle), None)
            if skill is not None:
                return skill
        return None

    def _resolve_support_cast(self, spell_ref: Skill | str | None = None, *, fallback_names: tuple[str, ...] = (), fallback_key: str | None = None) -> tuple[str, Skill | None]:
        skill = None
        if isinstance(spell_ref, Skill):
            skill = spell_ref
        elif isinstance(spell_ref, str):
            raw_ref = str(spell_ref).strip()
            skill = self._find_spell_by_name(raw_ref)
            if skill is None and raw_ref and (
                len(raw_ref) == 1 or raw_ref.lower() in {
                    "esc", "enter", "home", "tab", "up", "down", "left", "right",
                    "space", "shift", "ctrl", "alt", "f1", "f2", "f3", "f4"
                }
            ):
                return normalize_game_hotkey(raw_ref), None

        if skill is None and fallback_names:
            skill = self._find_spell_by_name(*fallback_names)

        if skill is not None:
            return skill.effective_key(), skill

        key = normalize_game_hotkey(fallback_key)
        return key, None

    def _stop_inputs_best_effort(self, repeat: int = 1, delay: float = 0.03, disconnect: bool = False):
        stop_fn = getattr(hw, "stop_all_inputs", None)
        if callable(stop_fn):
            try:
                stop_fn(repeat=repeat, delay=delay, disconnect=disconnect)
                return True
            except Exception:
                pass

        release_fn = getattr(hw, "panic_release", None)
        if callable(release_fn):
            try:
                release_fn()
                if disconnect:
                    try:
                        hw.disconnect()
                    except Exception:
                        pass
                return True
            except Exception:
                return False
        return False

    def _press_cast_key(self, key: str, gap_key: str = "spell_cast_gap") -> bool:
        if not key:
            return False
        hw.humanized_press(key)
        if gap_key:
            humanized_sleep(TIMING_CONFIG.get(gap_key, TIMING_CONFIG["spell_cast_gap"]))
        return True

    def _get_visible_monsters(self) -> list[dict]:
        monsters = self.state.entities.get("monsters", [])
        return monsters if isinstance(monsters, list) else []

    def _get_good_hp_threshold(self) -> int:
        try:
            return max(0, int(getattr(self.state, "good_hp", 0) or 0))
        except Exception:
            return 0

    def _get_good_mp_threshold(self) -> int:
        try:
            return max(0, int(getattr(self.state, "good_mp", 0) or 0))
        except Exception:
            return 0

    def _needs_hp_recovery(self) -> bool:
        current_hp = max(0, int(getattr(self.state, "hp", 0) or 0))
        good_hp = self._get_good_hp_threshold()
        if good_hp > 0 and current_hp < good_hp:
            return True
        return bool(getattr(self.state, "hp_trig_active", False))

    def _needs_mp_recovery(self) -> bool:
        current_mp = max(0, int(getattr(self.state, "mp", 0) or 0))
        good_mp = self._get_good_mp_threshold()
        if good_mp > 0 and current_mp < good_mp:
            return True
        return bool(getattr(self.state, "mp_trig_active", False))

    def _get_support_target_data(self) -> dict | None:
        """Return the ally snapshot that the dosa should support, if available."""
        remote = self.state.get_remote_data_by_role("격수") or self.state.get_remote_data()
        return remote if isinstance(remote, dict) and remote else None

    def _needs_hp_recovery_for(self, snapshot: dict | None) -> bool:
        if not snapshot:
            return self._needs_hp_recovery()
        if not snapshot.get("heal_request", False):
            return False
        if not snapshot.get("red_tab_enabled", False):
            return False
        current_hp = max(0, int(snapshot.get("hp", 0) or 0))
        good_hp = max(0, int(snapshot.get("good_hp", self._get_good_hp_threshold()) or 0))
        if good_hp > 0 and current_hp <= good_hp:
            return True
        return current_hp <= 10000

    def _needs_mp_recovery_for(self, snapshot: dict | None) -> bool:
        if not snapshot:
            return self._needs_mp_recovery()
        if not snapshot.get("mp_request", False):
            return False
        if not snapshot.get("red_tab_enabled", False):
            return False
        current_mp = max(0, int(snapshot.get("mp", 0) or 0))
        good_mp = max(0, int(snapshot.get("good_mp", self._get_good_mp_threshold()) or 0))
        if good_mp > 0 and current_mp <= good_mp:
            return True
        return current_mp <= 10000

    def _clear_target_state(self, reset_combat_timer: bool = True):
        self.state.target_locked = False
        self.state.target_name = ""
        if reset_combat_timer:
            self.state.combat_start_time = 0.0

    def _set_combat_busy(self, active: bool):
        self.state.is_combat_busy = bool(active)
        if not active and not self.state.target_locked:
            self.state.combat_start_time = 0.0

    def _should_handle_combat(self) -> bool:
        """
        이동과 전투를 분리하기 위한 전투 진입 판정.
        - 화면에 몬스터가 있으면 전투 우선
        - target_locked는 OCR 응답 대기 시간까지만 유지
        - 아무 몬스터도 없고 타겟도 없으면 즉시 이동으로 복귀
        """
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

        # HP 임계값 또는 검정 트리거가 켜졌을 때 즉시 회복 (esc -> key -> home -> enter)
        key, skill = self._resolve_recovery_key(getattr(self.state, "recovery_hp_spell", ""), default_key="3")

        now = time.time()
        min_interval = max(0.05, float(TIMING_CONFIG.get("spell_confirm", 0.20)) * 0.80)
        if now - self._last_hp_recover_time < min_interval:
            return

        if skill is not None and hasattr(skill, "is_ready") and not skill.is_ready():
            return

        self._last_hp_recover_time = now
        self._press_fast("esc")
        humanized_sleep(TIMING_CONFIG["key_gap"])
        self._press_fast(key)
        humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
        self._press_fast("home")
        humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
        self._press_fast("enter")

        if skill is not None:
            try:
                skill.last_cast_time = now
            except Exception:
                pass
        humanized_sleep(TIMING_CONFIG["heal_gap"])

    def _recover_mp(self):
        if not self._needs_mp_recovery():
            return

        # MP 임계값 또는 검정 트리거가 켜졌을 때 설정 키를 2초 간격으로 입력
        key, skill = self._resolve_recovery_key(getattr(self.state, "recovery_mp_spell", ""), default_key="2")

        now = time.time()
        min_interval = max(0.10, float(TIMING_CONFIG.get("mp_recover_interval", 2.0)))
        if now - self._last_mp_recover_time < min_interval:
            return

        if skill is not None and hasattr(skill, "is_ready") and not skill.is_ready():
            return

        self._last_mp_recover_time = now
        self._press_fast(key)
        if skill is not None:
            try:
                skill.last_cast_time = now
            except Exception:
                pass
        humanized_sleep(min(0.05, float(TIMING_CONFIG.get("key_gap", 0.05))))

    def cast_geumgang(self, spell_ref: Skill | str | None = None, *, fallback_key: str | None = None) -> bool:
        """금강(자기자신 버프): explicit spell or configured fallback key."""
        if not self._support_action_lock.acquire(blocking=False):
            return False
        try:
            key, skill = self._resolve_support_cast(spell_ref, fallback_names=("금강",), fallback_key=fallback_key)
            if not key:
                print("[Support] 금강 스킬을 찾지 못했습니다.")
                return False

            self._stop_inputs_best_effort(repeat=1, delay=0.02, disconnect=False)
            self._press_cast_key(key)
            if skill is not None:
                skill.last_cast_time = time.time()
            return True
        finally:
            self._support_action_lock.release()

    def cast_bomu(self, protection_ref: Skill | str | None = None, armor_ref: Skill | str | None = None) -> bool:
        """보무: 보호 -> 무장 순서로 시전."""
        if not self._support_action_lock.acquire(blocking=False):
            return False
        try:
            protection_key, protection_skill = self._resolve_support_cast(
                protection_ref,
                fallback_names=("보호",),
            )
            armor_key, armor_skill = self._resolve_support_cast(
                armor_ref,
                fallback_names=("무장",),
            )
            if not protection_key or not armor_key:
                print("[Support] 보무 스킬을 찾지 못했습니다.")
                return False

            self._stop_inputs_best_effort(repeat=1, delay=0.02, disconnect=False)
            now = time.time()
            self._press_cast_key(protection_key)
            humanized_sleep(TIMING_CONFIG["bomu_spell_gap"])
            self._press_cast_key(armor_key)
            self.state.last_bomu_time = now
            if protection_skill is not None:
                protection_skill.last_cast_time = now
            if armor_skill is not None:
                armor_skill.last_cast_time = now
            return True
        finally:
            self._support_action_lock.release()

    def cast_revival(
        self,
        revive_ref: Skill | str | None = None,
        heal_ref: Skill | str | None = None,
    ) -> bool:
        """유령(부활): 부활 -> Home -> Enter -> 힐 -> Enter -> 힐 -> Enter."""
        if not self._support_action_lock.acquire(blocking=False):
            return False
        try:
            revive_key, revive_skill = self._resolve_support_cast(revive_ref, fallback_names=("부활",))
            heal_key, heal_skill = self._resolve_support_cast(
                heal_ref if heal_ref is not None else getattr(self.state, "recovery_hp_spell", ""),
                fallback_names=("태양의기원", "공력증강"),
            )
            if not revive_key or not heal_key:
                print("[Support] 부활/힐 스킬을 찾지 못했습니다.")
                return False

            self._stop_inputs_best_effort(repeat=1, delay=0.02, disconnect=False)
            now = time.time()
            self._press_cast_key("esc", gap_key="key_gap")
            self._press_cast_key(revive_key, gap_key="spell_cast_gap")
            self._press_cast_key("home", gap_key="spell_cast_gap")
            self._press_cast_key("enter", gap_key="spell_cast_gap")
            self._press_cast_key(heal_key, gap_key="spell_cast_gap")
            self._press_cast_key("enter", gap_key="spell_cast_gap")
            self._press_cast_key(heal_key, gap_key="spell_cast_gap")
            self._press_cast_key("enter", gap_key="spell_cast_gap")

            if revive_skill is not None:
                revive_skill.last_cast_time = now
            if heal_skill is not None:
                heal_skill.last_cast_time = now
            self._clear_target_state(reset_combat_timer=True)
            return True
        finally:
            self._support_action_lock.release()

    def cast_self_heal(self, heal_ref: Skill | str | None = None) -> bool:
        """자힐: ESC -> 힐 -> Home -> Enter -> 힐 -> Enter -> 힐 -> Enter."""
        if not self._support_action_lock.acquire(blocking=False):
            return False
        try:
            heal_key, heal_skill = self._resolve_support_cast(
                heal_ref if heal_ref is not None else getattr(self.state, "recovery_hp_spell", ""),
                fallback_names=("태양의기원", "공력증강"),
            )
            if not heal_key:
                print("[Support] 자힐 스킬을 찾지 못했습니다.")
                return False

            self._stop_inputs_best_effort(repeat=1, delay=0.02, disconnect=False)
            now = time.time()
            self._press_cast_key("esc", gap_key="key_gap")
            self._press_cast_key(heal_key, gap_key="spell_cast_gap")
            self._press_cast_key("home", gap_key="spell_cast_gap")
            self._press_cast_key("enter", gap_key="spell_cast_gap")
            self._press_cast_key(heal_key, gap_key="spell_cast_gap")
            self._press_cast_key("enter", gap_key="spell_cast_gap")
            self._press_cast_key(heal_key, gap_key="spell_cast_gap")
            self._press_cast_key("enter", gap_key="spell_cast_gap")

            if heal_skill is not None:
                heal_skill.last_cast_time = now
            return True
        finally:
            self._support_action_lock.release()

    def cast_gongjeung(self, buff_ref: Skill | str | None = None) -> bool:
        """공증: MP가 매우 낮으면 emergency branch 후 버프 키를 시전."""
        if not self._support_action_lock.acquire(blocking=False):
            return False
        try:
            buff_key, buff_skill = self._resolve_support_cast(
                buff_ref if buff_ref is not None else getattr(self.state, "recovery_mp_spell", ""),
                fallback_names=("공력증강",),
            )
            if not buff_key:
                print("[Support] 공증 스킬을 찾지 못했습니다.")
                return False

            self._stop_inputs_best_effort(repeat=1, delay=0.02, disconnect=False)
            now = time.time()
            current_mp = max(0, int(getattr(self.state, "mp", 0) or 0))
            if current_mp < 200:
                hw.humanized_press("u")
                humanized_sleep(0.20)
                hw.humanized_press(random.choice(["e", "f", "g", "h"]))
                humanized_sleep(0.10)

            self._press_cast_key(buff_key)
            if buff_skill is not None:
                buff_skill.last_cast_time = now
            return True
        finally:
            self._support_action_lock.release()

    def cast_hon(self, debuff_ref: Skill | str | None = None, *, cycles: int = 1) -> bool:
        """혼 디버프: ESC -> 혼 -> LEFT -> ENTER 반복."""
        if not self._support_action_lock.acquire(blocking=False):
            return False
        try:
            debuff_key, debuff_skill = self._resolve_support_cast(
                debuff_ref if debuff_ref is not None else getattr(self.state, "recovery_debuff_spell", ""),
                fallback_names=("혼돈", "혼"),
            )
            if not debuff_key:
                print("[Support] 혼 디버프 스킬을 찾지 못했습니다.")
                return False

            self._stop_inputs_best_effort(repeat=1, delay=0.02, disconnect=False)
            now = time.time()
            self._press_cast_key("esc", gap_key="key_gap")

            loops = max(1, int(cycles or 1))
            for idx in range(loops):
                self._press_cast_key(debuff_key, gap_key="debuff_key_wait")
                self._press_cast_key("left", gap_key="debuff_up_wait")
                self._press_cast_key("enter", gap_key="debuff_ocr_wait")
                if idx + 1 < loops:
                    humanized_sleep(TIMING_CONFIG["key_gap"])

            if debuff_skill is not None:
                debuff_skill.last_cast_time = now
            self.state.last_debuff_x, self.state.last_debuff_y = self._get_current_pos()
            return True
        finally:
            self._support_action_lock.release()

    def _recover_party_hp(self, snapshot: dict | None):
        """Cast the configured HP heal for the currently supported warrior snapshot."""
        if not snapshot or not self._needs_hp_recovery_for(snapshot):
            return
        if not snapshot.get("red_tab_enabled", False):
            return

        key, skill = self._resolve_recovery_key(getattr(self.state, "recovery_hp_spell", ""), default_key="3")
        now = time.time()
        min_interval = max(0.05, float(TIMING_CONFIG.get("spell_confirm", 0.20)) * 0.80)
        if now - self._last_hp_recover_time < min_interval:
            return
        if skill is not None and hasattr(skill, "is_ready") and not skill.is_ready():
            return

        self._last_hp_recover_time = now
        self._press_fast(key)
        if skill is not None:
            try:
                skill.last_cast_time = now
            except Exception:
                pass
        print(f"[Support] HP heal cast on warrior: key={key}, hp={int(snapshot.get('hp', 0) or 0)}")
        humanized_sleep(TIMING_CONFIG["heal_gap"])

    def _recover_party_mp(self, snapshot: dict | None):
        """Cast the configured MP recovery for the currently supported warrior snapshot."""
        if not snapshot or not self._needs_mp_recovery_for(snapshot):
            return
        if not snapshot.get("red_tab_enabled", False):
            return

        key, skill = self._resolve_recovery_key(getattr(self.state, "recovery_mp_spell", ""), default_key="2")
        now = time.time()
        min_interval = max(0.10, float(TIMING_CONFIG.get("mp_recover_interval", 2.0)))
        if now - self._last_mp_recover_time < min_interval:
            return
        if skill is not None and hasattr(skill, "is_ready") and not skill.is_ready():
            return

        self._last_mp_recover_time = now
        self._press_fast(key)
        if skill is not None:
            try:
                skill.last_cast_time = now
            except Exception:
                pass
        print(f"[Support] MP heal cast on warrior: key={key}, mp={int(snapshot.get('mp', 0) or 0)}")
        humanized_sleep(min(0.05, float(TIMING_CONFIG.get("key_gap", 0.05))))

    # ----------------------------------------------------------
    # ── 보무(Buff) 자동 관리 ─────────────────────────────────
    # ----------------------------------------------------------
    def _execute_bomu_buff_v3(self):
        """보무 버프: 보호 -> 무장 순차 시전."""
        if self.cast_bomu():
            print("[OK] 보무 버프 완료.")

    # ----------------------------------------------------------
    # ── 지능형 디버프 스캔 ────────────────────────────────────
    # ----------------------------------------------------------
    def _execute_debuff_scan_v3(self):
        """
        저주 스캔 (10~20회 랜덤).
        - '걸리지 않습니다' 5회 연속 → 조기 break
        - combat_timeout 초과 → fail-safe break
        """
        debuff_spell = self.state.recovery_debuff_spell
        if not debuff_spell or debuff_spell == "사용 안 함": return

        skill = next((s for s in self.state.spells if s.name == debuff_spell), None)
        if not skill: return

        print(f"[Debuff] 디버프 스캔 시작: {debuff_spell}")
        num_cycles  = random.randint(10, 20)
        fail_count  = 0
        start_time  = time.time()
        timeout     = TIMING_CONFIG["combat_timeout"]

        for _ in range(num_cycles):
            # ── Fail-safe ───────────────────────────────────
            if time.time() - start_time > timeout * random.uniform(0.9, 1.1):
                print("[Failsafe] [FAIL-SAFE] 디버프 스캔 타임아웃 -> 루프 강제 중단.")
                hw.panic_escape()
                break

            # 1. 저주 키
            hw.humanized_press(skill.effective_key())
            humanized_sleep(TIMING_CONFIG["debuff_key_wait"])
            # 2. Up (다음 타겟)
            hw.humanized_press("up")
            humanized_sleep(TIMING_CONFIG["debuff_up_wait"])
            # 3. Enter (타겟 확정)
            hw.humanized_press("enter")
            humanized_sleep(TIMING_CONFIG["debuff_ocr_wait"])

            # 4. 실패 검증
            if "걸리지 않습니다" in self.state.target_name:
                fail_count += 1
                print(f"[Warn] 타겟 무효 ({fail_count}/5)")
                if fail_count >= 5:
                    print("[Stop] 유효 타겟 부재 -> 디버프 스캔 조기 종료.")
                    hw.panic_escape(2)
                    break
            else:
                fail_count = 0

        self.state.last_debuff_x, self.state.last_debuff_y = self._get_current_pos()
        print("[OK] 디버프 스캔 완료.")

    # ----------------------------------------------------------
    # ── 타겟 탐색 ─────────────────────────────────────────────
    # ----------------------------------------------------------
    def _search_target_v3(self):
        """Tab → Up → Enter (모두 humanized_sleep 경유)."""
        now = time.time()
        min_interval = max(0.15, float(TIMING_CONFIG.get("ocr_wait", 0.10)) * 1.5)
        if now - self._last_target_search_time < min_interval:
            return False
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
    # ── 사전 검증 ─────────────────────────────────────────────
    # ----------------------------------------------------------
    def _validate_target(self) -> bool:
        """
        True  → 타겟이 몬스터 목록에 포함 (공격 가능)
        False → 비몬스터 / '걸리지 않음' → ESC 처리
        """
        name = self.state.target_name
        if not name:
            acquire_grace = max(0.30, float(TIMING_CONFIG.get("ocr_wait", 0.10)) * 3.0)
            if time.time() - self._last_target_search_time > acquire_grace:
                self._clear_target_state()
            return False

        # ① '걸리지 않음' 즉시 취소
        if "걸리지 않습니다" in name:
            hw.humanized_press("esc")
            self._clear_target_state()
            return False

        # ② 몬스터 화이트리스트 검증
        if any(m in name for m in self.state.target_monster_names):
            return True

        # ③ 그 외 (유저, NPC 등) → ESC
        print(f"[Misc] 비몬스터 타겟: '{name}' -> ESC.")
        hw.humanized_press("esc")
        self._clear_target_state()
        return False

    # ----------------------------------------------------------
    # ── 유저 탐지 대응 ────────────────────────────────────────
    # ----------------------------------------------------------
    def _handle_unidentified_user(self):
        import winsound
        if self.state.user_alarm_enabled:
            winsound.MessageBeep()                   # 윈도우 기본 경고음
        if self.state.user_next_enabled:
            hw.humanized_press("esc")
            self.state.target_locked = False
            self.state.user_name     = ""
            self.state.is_user_detected = False
        if self.state.user_stop_enabled:
            print("[Stop] 미확인 유저 -> 사냥 중단.")
            self.state.auto_hunt = False

    # ----------------------------------------------------------
    # ── 엔티티 기반 우선순위 행동 제어 ───────────────────────
    # ----------------------------------------------------------
    def _handle_entity_priorities(self) -> bool:
        """
        entities 정보를 기반으로 우선순위에 따라 행동을 결정한다.

        우선순위:
            Priority 1 (최우선): HP/MP 위급 → 별도 메서드에서 처리 (여기서는 스킵)
            Priority 2: USER 감지 (화이트리스트 외) → ESC + 사냥 중단
            Priority 3: MONSTER 감지 → 사냥 루틴으로 진입
            Priority 4: ITEM 감지 → 루팅 (is_in_combat==False 일 때만)

        Returns:
            True  → 이 메서드에서 행동을 처리했으므로 메인 루프는 건너뜀
            False → 처리 없음, 메인 루프 계속
        """
        # --- Priority 2: 적대적 유저 ---
        hostile_users = [
            u for u in self.state.entities.get("users", [])
            if not u.get("is_whitelisted", True)
        ]
        if hostile_users or (
            getattr(self.state, "is_user_detected", False)
            and self.state.user_name
            and self.state.user_name not in self.state.whitelist_names
        ):
            user_name = (hostile_users[0]["name"] if hostile_users
                         else self.state.user_name)
            print(f"[Priority2] 적대적 유저 감지 → ESC 대피: {user_name}")
            self._handle_unidentified_user()
            if not self.state.auto_hunt:
                return True
            return True

        # --- Priority 3: 몬스터 감지 → 사냥 ---
        monsters = self.state.entities.get("monsters", [])
        if monsters:
            # 가장 가까운 몬스터 정보 출력 (0번째가 가장 먼저 추적된 몹)
            m = monsters[0]
            if not self.state.target_locked:
                print(f"[Priority3] MONSTER 감지 (ID={m.get('id','?')}) "
                      f"{m.get('name','')} @ {m.get('grid','?')} → 사냥 루틴")
                self.state.combat_start_time = 0.0
            return False   # 사냥은 메인 루프에서 계속 처리

        # --- Priority 4: 아이템 → 루팅 (비전투 중에만) ---
        items = self.state.entities.get("items", [])
        if items and not self._is_in_combat():
            item = items[0]
            grid = item.get("grid")
            if grid and not self.state.detected_item_grid:
                print(f"[Priority4] ITEM 감지 → 루팅: {item.get('name','')} @ {grid}")
                self.state.detected_item_grid = grid
                self.state.detected_item_name = item.get("name", "")
            return False

        return False



    # ----------------------------------------------------------
    # ── 전투 공격 루프 (Fail-safe 내장) ──────────────────────
    # ----------------------------------------------------------
    def _execute_combat(self):
        """
        현재 타겟에 공격 마법 1회 시전.
        combat_timeout 초과 시 루프 즉시 break → ESC → IDLE 복귀.
        """
        if not self._validate_target():
            return

        # 전투 시작 타이머 초기화
        if self.state.combat_start_time == 0.0:
            self.state.combat_start_time = time.time()

        # ── Fail-safe 검사 ──────────────────────────────────
        elapsed = time.time() - self.state.combat_start_time
        timeout = TIMING_CONFIG["combat_timeout"] * random.uniform(0.90, 1.10)
        if elapsed > timeout:
            print(f"[Failsafe] [FAIL-SAFE] 전투 타임아웃({elapsed:.1f}s) -> 강제 IDLE 복귀.")
            # 핵심: 가장 먼저 현재 공격 루프를 중단하고 ESC
            hw.panic_escape()
            self.state.target_locked      = False
            self.state.target_name        = ""
            self.state.combat_start_time  = 0.0
            self.current_state            = AppStatus.IDLE
            return

        # ── 공격 마법 시전 ──────────────────────────────────
        for s in self.state.spells:
            if s.category == "공격" and s.is_ready():
                hw.humanized_press(s.effective_key())
                humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
                s.last_cast_time = time.time()
                break

    # ----------------------------------------------------------
    # ── 메인 FSM 루프 (우선순위 체계) ─────────────────────────
    # ----------------------------------------------------------
    def run(self):
        print(f"[Logic] BIS Logic Engine started (role: {self.state.role})")
        print("[Logic] Priority: Emergency -> Combat -> Moving")
        while self.state.running:
            if getattr(self.state, "shutting_down", False):
                break
            if getattr(self.state, "automation_paused", False):
                self._clear_nav_context()
                self._was_in_combat = False
                self._advance_waypoint_after_combat = False
                self._combat_resume_context = None
                humanized_sleep(0.02)
                continue

            if not getattr(self.state, "service_active", False):
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

            # 이동 중 자동사냥은 Sentinel 기반 감지가 전제다.
            if self.state.auto_hunt and not self.state.sentinel_enabled:
                self.state.sentinel_enabled = True

            # ── 0순위: Emergency (비상 상황) ─────────────────────
            support_target = self._get_support_target_data() if self.state.role == "도사" else None
            if self.state.role == "도사" and self._run_dosa_service_cycle(support_target):
                continue
            if self._check_emergency(support_target):
                self._handle_emergency(support_target)
                continue

            # ── 1순위: Combat (전투) ───────────────────────────
            combat_needed = self._should_handle_combat()
            self._set_combat_busy(combat_needed)
            if combat_needed:
                self._handle_combat()
                continue

            # ── 2순위: Moving (이동/네비게이션) ──────────────────
            if self.state.auto_hunt:
                self._handle_moving()
                continue

            # ── 대기 상태 (최소화하여 빠른 반응) ─────────────────────────────────────
            humanized_sleep(0.001)  # 1ms만 대기하여 빠른 반응 속도 확保

    def _check_emergency(self, snapshot: dict | None = None) -> bool:
        """비상 상황 확인 (HP/MP 임계값, 유저 감지, 채팅)"""
        if self._needs_hp_recovery_for(snapshot) or self._needs_mp_recovery_for(snapshot):
            return True
        # 미확인 유저 감지
        if snapshot is None and self.state.is_user_detected:
            return True
        # 채팅 활성화
        if snapshot is None and self.state.is_chat_active:
            return True
        return False

    def _handle_emergency(self, snapshot: dict | None = None):
        """비상 상황 처리"""
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
        
        # 유저 감지 대응
        if self.state.is_user_detected:
            self._handle_unidentified_user()
        
        # 채팅 활성화 시 전투 중단
        if self.state.is_chat_active:
            if self.state.target_locked:
                hw.humanized_press("esc")
                self.state.target_locked = False

    def _run_dosa_service_cycle(self, support_target: dict | None) -> bool:
        """
        도사 서비스 전용 루프.
        격수 telemetry 기반 HP/MP 회복만 수행한다.
        버프/디버프는 별도 함수로 분리해 이후 추가한다.
        """
        if not self.state.service_active:
            return False
        if not support_target:
            return False

        handled = False
        if self._needs_hp_recovery_for(support_target):
            self._recover_party_hp(support_target)
            handled = True
        if self._needs_mp_recovery_for(support_target):
            self._recover_party_mp(support_target)
            handled = True
        return handled

    def _handle_combat(self):
        """전투 처리 (우선순위 1)"""
        # 전투 중에는 is_combat_busy 플래그 설정
        self._set_combat_busy(True)
        
        # 역할별 전투 로직
        if self.state.role == "격수":
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
        """이동/네비게이션 처리 (우선순위 2)"""
        # 전투 중이 아닐 때만 이동
        if not self.state.is_combat_busy:
            # 이동 로직은 RouteSvc에서 처리
            pass

    # ----------------------------------------------------------
    # ── 격수 모드: 데이터 공유 서버 역할만 ─────────────────────
    # ----------------------------------------------------------
    def _run_dps_server_mode(self):
        """격수 모드: 데이터 공유 서버 역할만 수행, 사냥 로직 비활성"""
        # ── 자동사냥 비활성 ─────────────────────────────
        if not self.state.auto_hunt:
            self.state.last_bomu_time    = 0
            self.state.combat_start_time = 0.0
            humanized_sleep(TIMING_CONFIG["idle_sleep"])
            return

        # ── 데이터 공유 서버 역할만 수행 ───────────────────
        # 격수는 자신의 상태만 유지하고, 도사/술사에게 데이터 공유
        # 사냥 로직(타겟 탐색, 전투, 아이템 획득)은 수동으로 수행
        
        # 생존 최우선 (HP/MP 회복) - 격수도 자가 회복 필요
        if self._needs_hp_recovery():
            self._recover_hp()
            self.state.combat_start_time = 0.0

        if self._needs_mp_recovery():
            self._recover_mp()
            if self._needs_hp_recovery():
                self._recover_hp()

        # 보무 버프 유지 (185s 주기)
        if time.time() - self.state.last_bomu_time > TIMING_CONFIG["bomu_interval"]:
            self._execute_bomu_buff_v3()

        # 디버프 스캔 (격수도 필요)
        cur_x, cur_y = self._get_current_pos()
        dist_change = (abs(cur_x - self.state.last_debuff_x) +
                       abs(cur_y - self.state.last_debuff_y))
        if self.state.auto_debuff_enabled and dist_change > 15:
            self._execute_debuff_scan_v3()

        humanized_sleep(TIMING_CONFIG["action_loop"])

    # ----------------------------------------------------------
    # ── 지원 모드: 도사 - 격수 데이터 수신 및 지원 ────────────────
    # ----------------------------------------------------------
    def _run_support_mode(self):
        """도사 모드: 격수의 데이터 수신받아 지원 로직 수행"""
        # ── 자동사냥 비활성 ─────────────────────────────
        if not self.state.auto_hunt:
            self.state.last_bomu_time    = 0
            self.state.combat_start_time = 0.0
            humanized_sleep(TIMING_CONFIG["idle_sleep"])
            return

        # ── 지원 로직 수행 ───────────────────────────────
        # 도사는 격수의 타겟을 따라가며 지원
        
        # 생존 최우선 (HP/MP 회복)
        if self._needs_hp_recovery():
            self._recover_hp()
            self.state.combat_start_time = 0.0

        if self._needs_mp_recovery():
            self._recover_mp()
            if self._needs_hp_recovery():
                self._recover_hp()

        # 보무 버프 유지 (185s 주기)
        if time.time() - self.state.last_bomu_time > TIMING_CONFIG["bomu_interval"]:
            self._execute_bomu_buff_v3()

        # 디버프 스캔
        cur_x, cur_y = self._get_current_pos()
        dist_change = (abs(cur_x - self.state.last_debuff_x) +
                       abs(cur_y - self.state.last_debuff_y))
        if self.state.auto_debuff_enabled and dist_change > 15:
            self._execute_debuff_scan_v3()

        # 유저 탐지 대응 + 엔티티 우선순위 행동 제어
        if self._handle_entity_priorities():
            humanized_sleep(TIMING_CONFIG["action_loop"])
            return

        # SentinelThread 결과 보고: 몬스터 존재 시 서포트 스킬 시전
        if self.state.monster_on_screen:
            # 격수가 타겟을 잡고 있으면 지원 스킬 시전
            self._execute_support_skills()

        humanized_sleep(TIMING_CONFIG["action_loop"])

    def _get_priest_monsters(self) -> list[dict]:
        monsters = self.state.entities.get("monsters", [])
        return monsters if isinstance(monsters, list) else []

    def _calc_monster_metrics(self):
        me_pos = self._get_current_pos()
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
                current_grid = self._get_current_grid()
                dx = int(grid[0]) - int(current_grid[0])
                dy = int(grid[1]) - int(current_grid[1])
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
        술사 첨사냥은 가까운 몬스터만 전투 진입 대상으로 본다.
        engage_range는 pos x/y 기준 체비쇼프 거리다.
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
        attack_skills = [s for s in self.state.spells if s.category == "공격" and s.is_ready()]
        if not attack_skills:
            return None

        if aoe_mode:
            for s in attack_skills:
                if "첨" in s.name:
                    return s
            return attack_skills[0]

        for s in attack_skills:
            if "성황령" in s.name:
                return s
        for s in attack_skills:
            if "첨" not in s.name:
                return s
        return attack_skills[0]

    def _cast_attack_skill(self, skill):
        hw.humanized_press(skill.effective_key())
        humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
        skill.last_cast_time = time.time()

    # ----------------------------------------------------------
    # ── 술사 모드: 첨사냥 로직 ────────────────────────────────
    # ----------------------------------------------------------
    def _run_priest_mode(self):
        """술사 모드: 첨사냥 로직 수행"""
        # ── 자동사냥 비활성 ─────────────────────────────
        if not self.state.auto_hunt:
            self.state.last_bomu_time    = 0
            self.state.combat_start_time = 0.0
            humanized_sleep(TIMING_CONFIG["idle_sleep"])
            return

        # ── 첨사냥 로직 수행 ────────────────────────────
        # 술사는 독자적으로 사냥 수행
        
        # 보무 버프 유지 (185s 주기)
        if time.time() - self.state.last_bomu_time > TIMING_CONFIG["bomu_interval"]:
            self._execute_bomu_buff_v3()

        # 생존 최우선 (HP/MP 회복)
        if self._needs_hp_recovery():
            self._recover_hp()
            self.state.combat_start_time = 0.0

        if self._needs_mp_recovery():
            self._recover_mp()
            if self._needs_hp_recovery():
                self._recover_hp()

        # 유저 탐지 대응 + 엔티티 우선순위 행동 제어
        if self._handle_entity_priorities():
            humanized_sleep(TIMING_CONFIG["action_loop"])
            return

        # 아이템 획득 (유휴 상태일 때만)
        if not self.state.target_locked and self.state.detected_item_grid:
            gx, gy = self.state.detected_item_grid
            print(f"[Item] 아이템 획득 시도: {self.state.detected_item_name} @ Grid({gx}, {gy})")

            while self.state.running and self.state.auto_hunt and self.state.detected_item_grid:
                if self.state.target_locked or self._needs_hp_recovery() or self._needs_mp_recovery():
                    print("[Action] 획득 중 전투/위급 상황 발생 -> 루팅 중단.")
                    break

                # ── 회피 이동 (Stuck 감지) 통합 ──
                if self._check_stuck():
                    self._escape_stuck(rewind_waypoint=False)
                    humanized_sleep(TIMING_CONFIG["move_hold"])
                    continue

                arrived = self._move_toward_grid(gx, gy)
                if arrived:
                    print(f"[Point] 아이템 위 도착. 획득 시도 (',')")
                    hw.humanized_press(",") 
                    humanized_sleep(TIMING_CONFIG["enter_wait"])
                    self.state.detected_item_grid = None
                    print("[OK] 아이템 획득 완료.")
                    break
                
                humanized_sleep(TIMING_CONFIG["nav_loop"])

        # 디버프 스캔
        cur_x, cur_y = self._get_current_pos()
        dist_change = (abs(cur_x - self.state.last_debuff_x) +
                       abs(cur_y - self.state.last_debuff_y))
        if self.state.auto_debuff_enabled and dist_change > 15:
            self._execute_debuff_scan_v3()

        me_grid, monster_infos, dense_count = self._get_priest_engage_metrics()
        has_monsters = len(monster_infos) > 0
        if has_monsters:
            aoe_mode = dense_count >= 2
            # 단일 타겟 스킬은 타겟 락이 필요하므로 기존 탐색 시퀀스 유지
            if (not aoe_mode) and (not self.state.target_locked):
                self.state.combat_start_time = 0.0
                if self._search_target_v3():
                    humanized_sleep(TIMING_CONFIG["ocr_fast"])
                return

            skill = self._find_ready_attack_skill(aoe_mode=aoe_mode)
            if skill:
                mode_label = "광역(첨)" if aoe_mode else "단일"
                print(f"[Priest] 공격 분기: {mode_label} | 근접밀집={dense_count} | 레이더몹={len(monster_infos)}")
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
    # ── 기본 모드: 기존 로직 ─────────────────────────────────
    # ----------------------------------------------------------
    def _run_default_mode(self):
        """기본 모드: 기존 로직 수행"""
        # ── 자동사냥 비활성 ─────────────────────────────
        if not self.state.auto_hunt:
            self.state.last_bomu_time    = 0
            self.state.combat_start_time = 0.0
            humanized_sleep(TIMING_CONFIG["idle_sleep"])
            return

        # ══════════════════════════════════════════════════
        # 0. 보무 버프 유지 (185s 주기)
        # ══════════════════════════════════════════════════
        if time.time() - self.state.last_bomu_time > TIMING_CONFIG["bomu_interval"]:
            self._execute_bomu_buff_v3()

        # ══════════════════════════════════════════════════
        # 1. 생존 최우선 (HP / MP 회복)
        # ══════════════════════════════════════════════════
        if self._needs_hp_recovery():
            self._recover_hp()
            self.state.combat_start_time = 0.0

        if self._needs_mp_recovery():
            self._recover_mp()
            if self._needs_hp_recovery():
                self._recover_hp()

        # ══════════════════════════════════════════════════
        # 2. 유저 탐지 대응 + 엔티티 우선순위 행동 제어
        # ══════════════════════════════════════════════════
        if self._handle_entity_priorities():
            humanized_sleep(TIMING_CONFIG["action_loop"])
            return

        # ══════════════════════════════════════════════════
        # 3. 아이템 획득 (유휴 상태일 때만)
        # ══════════════════════════════════════════════════
        if not self.state.target_locked and self.state.detected_item_grid:
            gx, gy = self.state.detected_item_grid
            print(f"[Item] 아이템 획득 시도: {self.state.detected_item_name} @ Grid({gx}, {gy})")

            while self.state.running and self.state.auto_hunt and self.state.detected_item_grid:
                if self.state.target_locked or self._needs_hp_recovery() or self._needs_mp_recovery():
                    print("[Action] 획득 중 전투/위급 상황 발생 -> 루팅 중단.")
                    break

                arrived = self._move_toward_grid(gx, gy)
                if arrived:
                    print(f"[Point] 아이템 위 도착. 획득 시도 (',')")
                    hw.humanized_press(",") 
                    humanized_sleep(TIMING_CONFIG["enter_wait"])
                    self.state.detected_item_grid = None
                    print("[OK] 아이템 획득 완료.")
                    break
                
                humanized_sleep(TIMING_CONFIG["nav_loop"])

        # ══════════════════════════════════════════════════
        # 4. 좌표 기반 자동 디버프 스캔
        # ══════════════════════════════════════════════════
        cur_x, cur_y = self._get_current_pos()
        dist_change = (abs(cur_x - self.state.last_debuff_x) +
                       abs(cur_y - self.state.last_debuff_y))
        if self.state.auto_debuff_enabled and dist_change > 15:
            self._execute_debuff_scan_v3()

        # ══════════════════════════════════════════════════
        # 5. 타겟 탐색 & 전투
        # ══════════════════════════════════════════════════
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
    # ── 지원 스킬 시전 (도사/술사) ───────────────────────────
    # ----------------------------------------------------------
    def _execute_support_skills(self):
        """도사/술사: 격수의 타겟에 지원 스킬 시전"""
        # 도사/술사 전용 지원 스킬 시전 로직
        # 예: 격수의 타겟에 버프 스킬 시전, 힐 스킬 시전 등
        # 이 부분은 각 역할별 스킬 설정에 따라 동작
        pass


# ============================================================
#  Grid 좌표 변환 함수 (GridManager 사용)
# ============================================================
def grid_name_to_coords(grid_name):
    """Grid 이름(S3, A10 등)을 좌표(col, row)로 변환"""
    # GridManager의 name_to_grid 메서드 사용
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

    blockers: list[str] = []
    for direction in candidates:
        nx, ny = _nav_step_from_dir(ccx, ccy, direction)
        cell_blockers = nav_cell_blockers(state, nx, ny, include_entities=include_entities)
        if not cell_blockers:
            return direction, (nx, ny), []
        blockers.extend(cell_blockers)

    return None, None, list(dict.fromkeys(blockers))

# ============================================================
#  NavigationThread  ─  지능형 이동 + Stuck 탈출
# ============================================================
class RouteSvc(threading.Thread):
    """웨이포인트 이동 + 그룹원 추적 + 지능형 Stuck 탈출."""

    # 방향 정의
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
        # 지능형 회피 로직용 변수
        self.last_pos = None
        self.stuck_timer = 0.0
        self.stuck_count = 0
        # 시퀀스 기반 이동용 변수
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
        self._last_nav_debug_log_time = 0.0
        self._last_nav_block_log_time = 0.0
        self._last_nav_arrival_log_time = 0.0
        self._last_focus_block_log_time = 0.0
        self._last_follow_block_log_time = 0.0
        self._last_move_command_at = 0.0
        self._stuck_recovery_until = 0.0
        self._follow_close_until = 0.0
        self._follow_block_hits = 0
        self._follow_same_pos_cycles = 0
        self._follow_last_target_pos = None
        self._follow_last_progress_score = None
        self._follow_no_progress_cycles = 0
        self._follow_blocked_cells = {}
        self._follow_failed_moves = {}
        self._follow_pending_attempt = None

    def reset_runtime_state(self):
        """Reset transient navigation state when switching modes."""
        try:
            self.last_pos = self._get_current_pos()
        except Exception:
            self.last_pos = (0, 0)
        self.stuck_timer = 0.0
        self.stuck_count = 0
        self._nav_current_pos = None
        self._clear_nav_attempt_state()
        self._current_nav_context = None
        self._was_in_combat = False
        self._advance_waypoint_after_combat = False
        self._combat_resume_context = None
        self._last_move_command_at = 0.0
        self._stuck_recovery_until = 0.0
        self._follow_close_until = 0.0
        self._last_follow_block_log_time = 0.0
        self._follow_block_hits = 0
        self._follow_same_pos_cycles = 0
        self._follow_last_target_pos = None
        self._follow_last_progress_score = None
        self._follow_no_progress_cycles = 0
        self._follow_blocked_cells = {}
        self._follow_failed_moves = {}
        self._follow_pending_attempt = None
        try:
            self.state.is_stuck = False
            self.state.follow_target_pos = None
            self.state.follow_anchor_offset = (0, 0)
            self.state.last_key_context = "NONE"
        except Exception:
            pass

    def _get_current_pos(self) -> tuple[int, int]:
        """Return the canonical OCR-derived position for this character."""
        try:
            pos_x = int(getattr(self.state, "pos_x", 0) or 0)
            pos_y = int(getattr(self.state, "pos_y", 0) or 0)
            raw_x = int(getattr(self.state, "x", 0) or 0)
            raw_y = int(getattr(self.state, "y", 0) or 0)
            if pos_x or pos_y or (raw_x == 0 and raw_y == 0):
                return pos_x, pos_y
        except Exception:
            pass
        return int(getattr(self.state, "x", 0) or 0), int(getattr(self.state, "y", 0) or 0)

    @staticmethod
    def _get_remote_pos(remote_data: dict | None) -> tuple[int, int] | None:
        if not remote_data:
            return None
        try:
            return (
                int(remote_data.get("pos_x", remote_data.get("x", 0)) or 0),
                int(remote_data.get("pos_y", remote_data.get("y", 0)) or 0),
            )
        except Exception:
            return None

    def _get_current_grid(self) -> tuple[int, int]:
        """Return the best available grid coordinate for blocker checks."""
        try:
            me = self.state.entities.get("me", {}) or {}
            grid = me.get("grid")
            if isinstance(grid, (list, tuple)) and len(grid) == 2:
                return int(grid[0]), int(grid[1])
        except Exception:
            pass

        try:
            char_grid = getattr(self.state, "char_grid", None)
            if isinstance(char_grid, (list, tuple)) and len(char_grid) == 2:
                gx = int(char_grid[0])
                gy = int(char_grid[1])
                if gx or gy:
                    return gx, gy
        except Exception:
            pass

        return self._get_current_pos()

    # ----------------------------------------------------------
    def _check_stuck(self) -> bool:
        """Return True when a move key was sent and coordinates did not change for 1 second."""
        now = time.time()
        if self._stuck_recovery_until > now:
            return False
        try:
            capture_age_ms = float(getattr(self.state, "capture_age_ms", 0.0) or 0.0)
            if capture_age_ms > 260.0:
                return False
        except Exception:
            pass
        current_pos = self._get_current_pos()

        if self._nav_current_pos != current_pos:
            self.last_pos = self._nav_current_pos
            self._nav_current_pos = current_pos
            self._nav_attempt_pos = None
            self._nav_attempt_started_at = 0.0
            self.stuck_count = 0
            return False

        if self._nav_attempt_pos != current_pos:
            return False

        if self._nav_attempt_started_at <= 0.0:
            return False

        move_grace = max(0.45, float(TIMING_CONFIG.get("move_hold", 0.15)) * 2.5)
        if self._last_move_command_at > 0.0 and (now - self._last_move_command_at) < move_grace:
            return False

        stuck_timeout = max(1.6, move_grace + 0.9)
        if (now - self._nav_attempt_started_at) >= stuck_timeout:
            self.stuck_count += 1
            print(f"[Stuck] no coord change for {stuck_timeout:.1f}s ({self.stuck_count} consecutive) pos={current_pos}")
            return True
        return False

    def _mark_nav_attempt(self, force: bool = False):
        current_pos = self._get_current_pos()
        if force or self._nav_attempt_pos != current_pos:
            self._nav_attempt_pos = current_pos
            self._nav_attempt_started_at = time.time()

    def _clear_nav_attempt_state(self):
        self._nav_current_pos = self._get_current_pos()
        self._nav_attempt_pos = None
        self._nav_attempt_started_at = 0.0
        self.stuck_count = 0
        self._follow_block_hits = 0
        self._follow_same_pos_cycles = 0
        self._follow_last_target_pos = None
        self._follow_last_progress_score = None
        self._follow_no_progress_cycles = 0
        self._follow_failed_moves = {}
        self._follow_pending_attempt = None

    def _build_follow_detour_directions(self, target_pos: tuple[int, int] | None = None) -> list[str]:
        """follow stuck 시 순차적으로 시도할 우회 방향 목록을 만든다."""
        if target_pos is None:
            remote_data = self.state.get_remote_data_by_role("격수") or self.state.get_remote_data()
            target_pos = self._get_remote_pos(remote_data)

        if not target_pos:
            return []

        cx, cy = self._get_current_pos()
        tx, ty = target_pos
        dx, dy = tx - cx, ty - cy
        if dx == 0 and dy == 0:
            return []

        # 목표 축과 직교하는 방향을 먼저 시도하고, 그 다음에 목표 방향/반대 방향을 시도한다.
        candidates: list[str] = []

        def add(direction: str):
            if direction not in candidates:
                candidates.append(direction)

        if abs(dx) >= abs(dy):
            add("up")
            add("down")
            if dy >= 0:
                add("down")
                add("up")
            else:
                add("up")
                add("down")
            if dx > 0:
                add("right")
                add("left")
            else:
                add("left")
                add("right")
        else:
            add("left")
            add("right")
            if dx >= 0:
                add("right")
                add("left")
            else:
                add("left")
                add("right")
            if dy > 0:
                add("down")
                add("up")
            else:
                add("up")
                add("down")

        for direction in self._ALL_DIRS:
            add(direction)

        return candidates

    def _escape_stuck_follow(self):
        """follow 전용 stuck 처리: 짧게 우회하고, 다음 루프에서 최신 격수좌표로 다시 추종한다."""
        current_pos = self._get_current_pos()
        remote_data = self.state.get_remote_data_by_role("격수") or self.state.get_remote_data()
        target_pos = self._get_remote_pos(remote_data)

        print("[Warn] [STUCK] follow mode active, doing a short detour before refollowing...")
        self.state.is_stuck = True
        try:
            stop_fn = getattr(hw, "stop_all_inputs", None)
            if callable(stop_fn):
                stop_fn(repeat=1, delay=0.03, disconnect=False)
            else:
                release_fn = getattr(hw, "panic_release", None)
                if callable(release_fn):
                    release_fn()
        except Exception:
            pass

        humanized_sleep(TIMING_CONFIG["key_gap"])

        detour_dirs = self._build_follow_detour_directions(target_pos=target_pos)
        moved = False
        for idx, detour_dir in enumerate(detour_dirs, start=1):
            before_pos = self._get_current_pos()
            print(f"[Warn] [STUCK] follow detour try {idx}/{len(detour_dirs)}: {detour_dir}")

            try:
                hw.fast_press(detour_dir, variance=0.05)
            except Exception as exc:
                print(f"[Warn] [STUCK] follow detour input failed on {detour_dir}: {exc}")
                try:
                    stop_fn = getattr(hw, "stop_all_inputs", None)
                    if callable(stop_fn):
                        stop_fn(repeat=1, delay=0.02, disconnect=False)
                    else:
                        release_fn = getattr(hw, "panic_release", None)
                        if callable(release_fn):
                            release_fn()
                except Exception:
                    pass
                continue

            humanized_sleep(max(0.08, TIMING_CONFIG["key_gap"] * 0.6))

            after_pos = self._get_current_pos()
            if after_pos != before_pos:
                self._forget_follow_blocked_cell(after_pos, reason="detour movement confirmed")
                print(f"[OK] [STUCK] follow detour moved {before_pos} -> {after_pos} via {detour_dir}")
                moved = True
                current_pos = after_pos
                break

            try:
                hw.stop_all_inputs(repeat=1, delay=0.02, disconnect=False)
            except Exception:
                pass

        if not moved:
            print("[Warn] [STUCK] follow detour could not move character; retry will use fresh target next loop.")

        try:
            stop_fn = getattr(hw, "stop_all_inputs", None)
            if callable(stop_fn):
                stop_fn(repeat=1, delay=0.03, disconnect=False)
            else:
                release_fn = getattr(hw, "panic_release", None)
                if callable(release_fn):
                    release_fn()
        except Exception:
            pass

        self._clear_nav_context()
        self.state.is_stuck = False
        self.stuck_count = 0
        self.stuck_timer = 0.0
        self._nav_current_pos = current_pos
        self._clear_nav_attempt_state()
        self._follow_close_until = 0.0
        self._stuck_recovery_until = time.time() + (0.02 if moved else 0.06)
        if moved:
            print("[OK] [STUCK] follow detour complete, resume follow with latest warrior position.")
        else:
            print("[OK] [STUCK] follow detour complete, waiting for next fresh follow target.")

    # ----------------------------------------------------------
    def _escape_stuck(self, rewind_waypoint: bool = False):
        """
        Intelligent escape movement.
        1) step back toward the previous position when available
        2) random side-step
        3) repeat a few times
        4) reset target state
        """
        follow_only = bool(getattr(self.state, "nav_follow_enabled", False)) and not bool(
            getattr(self.state, "nav_route_enabled", False)
        )
        if follow_only:
            self._escape_stuck_follow()
            return

        print("[Warn] [STUCK] blocked movement detected, escaping...")
        self.state.is_stuck = True

        for _ in range(random.randint(2, 3)):
            if isinstance(self.last_pos, tuple) and len(self.last_pos) == 2:
                cx, cy = self._get_current_pos()
                lx, ly = self.last_pos
                dx, dy = lx - cx, ly - cy
                if abs(dx) > abs(dy):
                    move_dir = "right" if dx > 0 else "left"
                else:
                    move_dir = "down" if dy > 0 else "up"
                hw.hold_move(move_dir, "stuck_back_hold")
                humanized_sleep(TIMING_CONFIG["key_gap"])

            side_dir = random.choice(["left", "right", "up", "down"])
            hw.hold_move(side_dir, "stuck_side_hold")
            humanized_sleep(TIMING_CONFIG["key_gap"])

        self.state.target_locked = False
        self.state.target_name = ""
        if rewind_waypoint and self.wp_idx > 0:
            self.wp_idx -= 1
        self.state.is_stuck = False
        self.stuck_count = 0
        self.stuck_timer = 0.0
        self._clear_nav_attempt_state()
        self._stuck_recovery_until = time.time() + 0.5
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
                print(f"[Nav] 전투 종료 후 다음 사냥점으로 건너뜀: index {self.seq_point_idx}")
        elif kind == "list_wp":
            idx = int(ctx.get("wp_idx", -1))
            advance_delta = int(ctx.get("advance_delta", 1))
            if self.wp_idx == idx:
                self.wp_idx += advance_delta
                print(f"[Nav] 전투 종료 후 다음 웨이포인트로 건너뜀: index {self.wp_idx}")

        self._advance_waypoint_after_combat = False
        self._combat_resume_context = None

    # ----------------------------------------------------------
    def _is_in_combat(self) -> bool:
        """전투 중 상태 판별: target_locked == True 또는 combat_start_time으로부터 5초 이내."""
        if self.state.target_locked:
            return True
        if self.state.combat_start_time > 0:
            elapsed = time.time() - self.state.combat_start_time
            if elapsed < 5.0:
                return True
        return False

    def _resolve_follow_anchor_offset(self, warrior_pos: tuple[int, int], warrior_dir: str | None = None) -> tuple[int, int]:
        """
        follow 중 사용할 고정 오프셋을 정한다.
        follow는 pos 기준으로만 동작하므로 grid 기반 blocker 판정은 사용하지 않는다.
        """
        cx, cy = self._get_current_pos()
        wx, wy = warrior_pos

        offset = getattr(self.state, "follow_anchor_offset", (0, 0))
        try:
            ox = int(offset[0] or 0)
            oy = int(offset[1] or 0)
        except Exception:
            ox, oy = 0, 0

        if (ox, oy) in {(-1, 0), (1, 0)}:
            return (ox, oy)

        if cx < wx:
            preferred = (-1, 0)
        elif cx > wx:
            preferred = (1, 0)
        else:
            if warrior_dir == "left":
                preferred = (1, 0)
            elif warrior_dir == "right":
                preferred = (-1, 0)
            else:
                preferred = (-1, 0)

        self.state.follow_anchor_offset = preferred
        return preferred

    # ----------------------------------------------------------
    def _is_wall(self, gx: int, gy: int) -> bool:
        """현재 맵의 maps_db를 조회하여 해당 Grid가 벽인지 판별."""
        m = self.state.current_map
        map_data = self.state.maps_db.get(m)
        if not map_data:
            return False
        walls = map_data.get("walls", [])
        return [gx, gy] in walls

    # ----------------------------------------------------------
    def _calc_follow_target(self) -> tuple[int, int] | None:
        """
        격수의 현재 pos_x/y를 그대로 follow 목표로 사용한다.
        follow는 실시간 격수 좌표만 기준으로 동작한다.
        """
        # 네트워크에서 격수 데이터 확인
        remote_data = self.state.get_remote_data_by_role("격수") or self.state.get_remote_data()
        if not remote_data:
            return None

        try:
            remote_pos = self._get_remote_pos(remote_data)
            if remote_pos is None:
                return None
            dps_x, dps_y = remote_pos
        except Exception:
            return None

        target = (dps_x, dps_y)
        self.state.follow_target_pos = target

        return target

    def _is_remote_follow_target_fresh(self, remote_data: dict | None, max_age_s: float = 0.35) -> bool:
        """follow는 최신 격수 telemetry만 사용한다."""
        if not remote_data:
            return False
        try:
            received_at = float(remote_data.get("_received_at", 0.0) or 0.0)
        except Exception:
            return False
        if received_at <= 0.0:
            return False
        return (time.time() - received_at) <= max(0.05, float(max_age_s or 0.0))

    def _axis_from_direction(self, direction: str | None) -> str | None:
        if direction in self._HORIZ:
            return "x"
        if direction in self._VERT:
            return "y"
        return None

    def _direction_delta(self, direction: str | None) -> tuple[int, int]:
        if direction == "left":
            return (-1, 0)
        if direction == "right":
            return (1, 0)
        if direction == "up":
            return (0, -1)
        if direction == "down":
            return (0, 1)
        return (0, 0)

    def _opposite_direction(self, direction: str | None) -> str | None:
        return {"left": "right", "right": "left", "up": "down", "down": "up"}.get(direction)

    def _send_follow_move(self, direction: str) -> bool:
        """
        Follow movement needs a reliable short D/U pulse, but without the
        HumanBehaviorSimulator pause used by hold_move().
        """
        duration = max(0.060, min(0.110, float(TIMING_CONFIG.get("move_hold", 0.15)) * 0.55))
        opposite = self._opposite_direction(direction)
        try:
            send_force = getattr(hw, "send_force", None)
            if callable(send_force):
                if opposite:
                    send_force(f"U:{opposite}")
                send_force(f"U:{direction}")
                send_force(f"D:{direction}")
                humanized_sleep(duration, 0.04)
                send_force(f"U:{direction}")
                return True
        except Exception:
            pass

        try:
            hw.fast_press(direction, variance=0.04)
            return True
        except Exception:
            try:
                hw.hold_move(
                    direction,
                    "follow_move_hold",
                    duration=max(0.060, min(0.120, TIMING_CONFIG.get("move_hold", 0.15) * 0.55)),
                )
                return True
            except Exception:
                return False

    def _cleanup_follow_blocked_cells(self, now: float | None = None):
        if now is None:
            now = time.time()
        blocked = getattr(self, "_follow_blocked_cells", None)
        if not isinstance(blocked, dict):
            self._follow_blocked_cells = {}
            return
        expired = [pos for pos, until in blocked.items() if until <= now]
        for pos in expired:
            blocked.pop(pos, None)

    def _forget_follow_blocked_cell(self, pos: tuple[int, int], reason: str = ""):
        blocked = getattr(self, "_follow_blocked_cells", None)
        if not isinstance(blocked, dict):
            return False
        key = tuple(pos)
        if key not in blocked:
            return False
        blocked.pop(key, None)
        suffix = f" ({reason})" if reason else ""
        print(f"[Follow] blocked cell released: {key}{suffix}")
        return True

    def _clear_follow_blocked_around(self, pos: tuple[int, int]) -> int:
        blocked = getattr(self, "_follow_blocked_cells", None)
        if not isinstance(blocked, dict):
            return 0
        px, py = int(pos[0]), int(pos[1])
        neighbors = {(px + dx, py + dy) for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]}
        removed = 0
        for key in list(blocked.keys()):
            if tuple(key) in neighbors:
                blocked.pop(key, None)
                removed += 1
        if removed:
            print(f"[Follow] cleared {removed} blocked cells around {pos}; retrying local path")
        return removed

    def _cleanup_follow_failed_moves(self, now: float | None = None):
        if now is None:
            now = time.time()
        failed = getattr(self, "_follow_failed_moves", None)
        if not isinstance(failed, dict):
            self._follow_failed_moves = {}
            return
        expired = [key for key, item in failed.items() if float(item.get("until", 0.0) or 0.0) <= now]
        for key in expired:
            failed.pop(key, None)

    def _record_follow_failed_move(self, origin: tuple[int, int], direction: str, now: float) -> int:
        self._cleanup_follow_failed_moves(now=now)
        failed = getattr(self, "_follow_failed_moves", None)
        if not isinstance(failed, dict):
            self._follow_failed_moves = {}
            failed = self._follow_failed_moves
        key = (tuple(origin), str(direction))
        item = failed.get(key, {"count": 0, "until": 0.0})
        count = int(item.get("count", 0) or 0) + 1
        failed[key] = {"count": count, "until": now + 1.6}
        return count

    def _is_follow_cell_blocked(self, pos: tuple[int, int], now: float | None = None) -> bool:
        self._cleanup_follow_blocked_cells(now=now)
        blocked = getattr(self, "_follow_blocked_cells", {})
        expiry = blocked.get(tuple(pos))
        if expiry is None:
            return False
        if now is None:
            now = time.time()
        return expiry > now

    def _remember_follow_blocked_cell(self, origin: tuple[int, int], direction: str, ttl: float = 20.0):
        dx, dy = self._direction_delta(direction)
        if dx == 0 and dy == 0:
            return
        blocked_pos = (int(origin[0]) + dx, int(origin[1]) + dy)
        expiry = time.time() + max(1.0, float(ttl or 0.0))
        self._follow_blocked_cells[blocked_pos] = expiry
        print(f"[Follow] blocked cell remembered: {blocked_pos} via {direction} for {ttl:.0f}s")

    def _build_follow_direction_candidates(
        self,
        dx: int,
        dy: int,
        *,
        last_dir: str | None = None,
        prefer_axis: str | None = None,
    ) -> list[str]:
        """follow 방향 후보를 AHK처럼 단순하게 만든다."""
        if abs(dx) <= 1 and abs(dy) <= 1:
            return []

        candidates: list[str] = []

        def add(direction: str):
            if direction not in candidates:
                candidates.append(direction)

        if abs(dx) <= 1 and abs(dy) > 1:
            primary_axis = "y"
        elif abs(dy) <= 1 and abs(dx) > 1:
            primary_axis = "x"
        elif prefer_axis in {"x", "y"}:
            primary_axis = prefer_axis
        else:
            primary_axis = "x" if abs(dx) >= abs(dy) else "y"

        x_toward = "right" if dx > 0 else "left"
        y_toward = "down" if dy > 0 else "up"

        if primary_axis == "x":
            if abs(dx) > 1:
                add(x_toward)
            if abs(dy) > 1:
                add(y_toward)
        else:
            if abs(dy) > 1:
                add(y_toward)
            if abs(dx) > 1:
                add(x_toward)

        last_axis = self._axis_from_direction(last_dir)
        if last_axis == "x" and abs(dy) > 1:
            add(y_toward)
        elif last_axis == "y" and abs(dx) > 1:
            add(x_toward)
        return candidates

    def _resolve_follow_pending_attempt(self, current_pos: tuple[int, int], now: float) -> str:
        """
        Returns one of:
        - none: no pending move
        - wait: still waiting for the current move to settle
        - moved: pending move resolved with a coordinate change
        - blocked: pending move timed out without movement
        """
        pending = getattr(self, "_follow_pending_attempt", None)
        if not isinstance(pending, dict):
            return "none"

        origin = pending.get("origin")
        direction = pending.get("direction")
        started_at = float(pending.get("started_at", 0.0) or 0.0)
        start_seq = int(pending.get("pos_update_seq", -1) or -1)
        if not isinstance(origin, tuple) or len(origin) != 2 or not direction:
            self._follow_pending_attempt = None
            return "none"

        current_seq = int(getattr(self.state, "pos_update_seq", 0) or 0)
        if tuple(current_pos) != tuple(origin):
            self._follow_pending_attempt = None
            self._follow_no_progress_cycles = 0
            self._follow_same_pos_cycles = 0
            self._follow_block_hits = 0
            self._follow_failed_moves = {}
            self._forget_follow_blocked_cell(tuple(current_pos), reason="movement confirmed")
            return "moved"

        settle_delay = max(0.22, min(0.35, float(TIMING_CONFIG.get("move_hold", 0.15)) * 1.6))
        elapsed = now - started_at
        # Do not judge a move until at least one fresh coordinate OCR update arrived after the key press.
        if current_seq <= start_seq or elapsed < settle_delay:
            wait_log_at = float(pending.get("wait_log_at", 0.0) or 0.0)
            if now - wait_log_at >= 0.25:
                print(f"[Follow] waiting for movement settle ({elapsed:.2f}s, seq={current_seq}) pos={current_pos}")
                pending["wait_log_at"] = now
            self._follow_pending_attempt = pending
            return "wait"

        fail_count = self._record_follow_failed_move(origin, str(direction), now)
        self._follow_pending_attempt = None
        self._follow_block_hits += 1 if fail_count >= 2 else 0
        self._follow_same_pos_cycles += 1
        self._follow_no_progress_cycles += 1
        if fail_count >= 2:
            self._remember_follow_blocked_cell(origin, str(direction), ttl=20.0)
            print(f"[Follow] blocked move confirmed at {origin} via {direction}; remembering cell and retrying")
        else:
            print(f"[Follow] move did not settle at {origin} via {direction}; retrying another axis before blocking")
        return "blocked"

    def _choose_follow_step_direction(
        self,
        dx: int,
        dy: int,
        *,
        last_dir: str | None = None,
        prefer_axis: str | None = None,
        current_pos: tuple[int, int] | None = None,
        now: float | None = None,
    ) -> tuple[str | None, str | None]:
        """
        AHK follow에 가깝게 축을 먼저 고른다.
        기본은 더 가까운 축을 먼저, 막힘이 의심되면 반대 축을 우선 시도한다.
        """
        if abs(dx) <= 1 and abs(dy) <= 1:
            return None, None

        normal_axis = "x" if abs(dx) < abs(dy) else "y"
        axis = prefer_axis if prefer_axis in {"x", "y"} else normal_axis
        current_pos = current_pos or self._get_current_pos()
        now = now if now is not None else time.time()

        candidates = self._build_follow_direction_candidates(
            dx,
            dy,
            last_dir=last_dir,
            prefer_axis=axis,
        )
        for direction in candidates:
            ddx, ddy = self._direction_delta(direction)
            next_pos = (int(current_pos[0]) + ddx, int(current_pos[1]) + ddy)
            if self._is_follow_cell_blocked(next_pos, now=now):
                continue
            return direction, self._axis_from_direction(direction)

        return None, None

    def _move_toward_follow(self, warrior_pos: tuple[int, int]) -> bool:
        """follow 전용 direct steering: pos 기준으로만 이동한다."""
        if self._is_in_combat():
            return False

        cx, cy = self._get_current_pos()
        wx, wy = warrior_pos
        dx, dy = wx - cx, wy - cy
        now = time.time()
        if now - self._last_nav_debug_log_time >= 1.0:
            print(f"[Follow] Target: ({wx}, {wy}) | Pos: ({cx}, {cy})")
            self._last_nav_debug_log_time = now
        self._cleanup_follow_blocked_cells(now=now)
        self._forget_follow_blocked_cell((cx, cy), reason="current position")

        if abs(dx) <= 1 and abs(dy) <= 1:
            self._follow_last_target_pos = warrior_pos
            self._follow_last_progress_score = None
            self._follow_no_progress_cycles = 0
            self._follow_same_pos_cycles = 0
            self._follow_pending_attempt = None
            return True

        pending_state = self._resolve_follow_pending_attempt((cx, cy), now)
        if pending_state == "wait":
            return False

        if pending_state == "moved":
            self._follow_last_progress_score = None
            self._follow_no_progress_cycles = 0
            self._follow_same_pos_cycles = 0
        elif pending_state == "blocked":
            self._follow_last_progress_score = None

        target_changed = self._follow_last_target_pos != warrior_pos
        if target_changed:
            self._follow_last_target_pos = warrior_pos
            self._follow_last_progress_score = None
            self._follow_no_progress_cycles = 0
            self._follow_same_pos_cycles = 0
            if not getattr(self, "_follow_blocked_cells", {}):
                self._follow_block_hits = 0

        progress_score = abs(dx) + abs(dy)
        last_progress = self._follow_last_progress_score
        if last_progress is None:
            self._follow_last_progress_score = progress_score
        else:
            if progress_score <= last_progress:
                self._follow_no_progress_cycles = 0
            self._follow_last_progress_score = progress_score

        command_gap = 0.02
        if self._last_move_command_at > 0.0 and (now - self._last_move_command_at) < command_gap:
            return False

        prefer_axis = None
        if self._follow_no_progress_cycles >= 1 or self._follow_block_hits >= 1:
            last_axis = self._axis_from_direction(getattr(self.state, "last_move_dir", None))
            if last_axis == "x":
                prefer_axis = "y"
            elif last_axis == "y":
                prefer_axis = "x"

        if self._follow_block_hits >= 4 or self._follow_no_progress_cycles >= 5:
            if now - self._last_follow_block_log_time >= 0.6:
                print(f"[Follow] obstacle blocked follow, escaping via detour... pos=({cx}, {cy})")
                self._last_follow_block_log_time = now
            self._escape_stuck_follow()
            return False

        step_dir, _step_axis = self._choose_follow_step_direction(
            dx,
            dy,
            last_dir=getattr(self.state, "last_move_dir", None),
            prefer_axis=prefer_axis,
            current_pos=(cx, cy),
            now=now,
        )
        if step_dir is None:
            if now - self._last_follow_block_log_time >= 0.6:
                print(f"[Follow] no unblocked follow direction available; waiting for fresh path... pos=({cx}, {cy})")
                self._last_follow_block_log_time = now
            self._escape_stuck_follow()
            return False

        if (self._follow_no_progress_cycles >= 1 or self._follow_block_hits >= 1) and now - self._last_follow_block_log_time >= 0.6:
            print(f"[Follow] axis swap retry via {step_dir} (no_progress={self._follow_no_progress_cycles}, blocked={self._follow_block_hits})")
            self._last_follow_block_log_time = now

        prev_pos = (cx, cy)
        if not self._send_follow_move(step_dir):
            print(f"[Follow] input failed: {step_dir}")
            return False
        self.state.last_move_dir = step_dir
        self._last_move_command_at = time.time()
        self._mark_nav_attempt(force=True)

        step_dx, step_dy = self._direction_delta(step_dir)
        self._follow_pending_attempt = {
            "origin": prev_pos,
            "direction": step_dir,
            "started_at": time.time(),
            "target": (prev_pos[0] + step_dx, prev_pos[1] + step_dy),
            "pos_update_seq": int(getattr(self.state, "pos_update_seq", 0) or 0),
            "wait_log_at": 0.0,
        }
        return False

    def _move_toward(self, tx: int, ty: int) -> bool:
        """Move one step toward a world target, avoiding walls / monsters / users."""
        if self._is_in_combat():
            return False

        cx, cy = self._get_current_pos()
        dx, dy = tx - cx, ty - cy
        now = time.time()
        if now - self._last_nav_debug_log_time >= 1.0:
            print(f"[Nav] Target: ({tx}, {ty}) | Pos: ({cx}, {cy}) | Grid: {self._get_current_grid()}")
            self._last_nav_debug_log_time = now

        ccx, ccy = self._get_current_grid()
        step_dir, next_grid, blockers = nav_pick_step_direction(
            self.state,
            (ccx, ccy),
            dx,
            dy,
            include_entities=True,
        )

        if step_dir is None:
            if blockers:
                if now - self._last_nav_block_log_time >= 1.0:
                    print(f"[Nav] blocked: {','.join(blockers)}")
                    self._last_nav_block_log_time = now
            if self.state.nav_follow_enabled and not self.state.nav_route_enabled:
                return False
            self._escape_stuck(rewind_waypoint=False)
            return False

        base_hold = TIMING_CONFIG["move_hold"]
        hold_time = max(0.050, min(0.300, random.gauss(base_hold, 0.010)))
        hw.hold_move(step_dir, "move_hold", duration=hold_time)
        self.state.last_move_dir = step_dir
        self._last_move_command_at = time.time()
        self._mark_nav_attempt()

        arrived = abs(dx) <= 1 and abs(dy) <= 1
        if arrived:
            if now - self._last_nav_arrival_log_time >= 1.0:
                print(f"[Nav] arrived: waypoint {self.wp_idx}")
                self._last_nav_arrival_log_time = now
        return arrived

    # [V5] 아이템 획득용 격자 기반 이동
    def _move_toward_grid(self, gx: int, gy: int) -> bool:
        """Move toward a target expressed in grid space, but resolve the move target in pos space."""
        cx, cy = self._get_current_pos()
        ccx, ccy = self._get_current_grid()
        world_tx = gx + (cx - ccx)
        world_ty = gy + (cy - ccy)
        return self._move_toward(world_tx, world_ty)

    def _find_cluster_center(self) -> tuple[int, int] | None:
        """몬스터가 2마리 이상 뭉친 그리드의 중심점을 반환."""
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

    # [시퀀스 기반 이동]
    def _get_reverse_direction(self, direction, reverse_action):
        """역방향 방향키 결정: reverse_action 우선, 없으면 자동 계산"""
        if reverse_action:
            return reverse_action
        # 자동 계산
        reverse_map = {"up": "down", "down": "up", "left": "right", "right": "left"}
        return reverse_map.get(direction, direction)

    def _traverse_sequence(self, seq_data):
        """시퀀스 기반 이동: entry → points → exit (일반) / exit → points(역순) → entry (역방향)"""
        now = time.time()
        is_reverse = self.state.reverse_mode
        
        # 역방향 모드: Exit → Points(역순) → Entry
        if is_reverse:
            # Exit 단계 (역방향 시작점)
            if self.seq_phase == "entry":  # 역방향에서는 entry를 exit로 사용
                self._set_nav_context("seq_entry")
                exit_data = seq_data.get("exit", {"x": 0, "y": 0, "direction": "down", "reverse_action": ""})
                tx, ty = exit_data["x"], exit_data["y"]
                
                # 출구 좌표로 이동
                arrived = self._move_toward(tx, ty)
                if arrived:
                    # 역방향 방향키 결정
                    rev_dir = self._get_reverse_direction(exit_data["direction"], exit_data.get("reverse_action", ""))
                    print(f"[Seq-Rev] 출구 도달: ({tx}, {ty}) → 방향키 {rev_dir} 1초간 누름")
                    hw.hold_move(rev_dir, "exit_hold")
                    time.sleep(1.0)
                    self.seq_phase = "points"
                    self.seq_point_idx = len(seq_data.get("points", [])) - 1  # 마지막 사냥점부터
            
            # Points 단계 (역순)
            elif self.seq_phase == "points":
                points = seq_data.get("points", [])
                if self.seq_point_idx >= 0:
                    grid_name = points[self.seq_point_idx]
                    col, row = grid_name_to_coords(grid_name)
                    self._set_nav_context(
                        "seq_point",
                        seq_point_idx=self.seq_point_idx,
                        grid_name=grid_name,
                        advance_delta=-1,
                    )
                    
                    if col is not None and row is not None:
                        arrived = self._move_toward_grid(col, row)
                        if arrived:
                            print(f"[Seq-Rev] 사냥점 도달: {grid_name} (Grid: {col}, {row})")
                            self.seq_point_idx -= 1
                    else:
                        print(f"[Seq-Rev] 잘못된 Grid 이름: {grid_name}, 건너뜀")
                        self.seq_point_idx -= 1
                else:
                    self.seq_phase = "exit"  # 역방향에서는 exit를 entry로 사용
            
            # Entry 단계 (역방향 종료점)
            elif self.seq_phase == "exit":
                self._set_nav_context("seq_exit")
                entry = seq_data.get("entry", {"x": 0, "y": 0, "direction": "right", "reverse_action": ""})
                tx, ty = entry["x"], entry["y"]
                
                # 입구 좌표로 이동
                arrived = self._move_toward(tx, ty)
                if arrived:
                    # 역방향 방향키 결정
                    rev_dir = self._get_reverse_direction(entry["direction"], entry.get("reverse_action", ""))
                    print(f"[Seq-Rev] 입구 도달: ({tx}, {ty}) → 방향키 {rev_dir} 1초간 누름")
                    hw.hold_move(rev_dir, "entry_hold")
                    time.sleep(1.0)
                    # 시퀀스 완료, 다시 처음부터
                    self.seq_phase = "entry"
                    self.seq_point_idx = 0
        
        # 일반 모드: Entry → Points → Exit
        else:
            # Entry 단계
            if self.seq_phase == "entry":
                self._set_nav_context("seq_entry")
                entry = seq_data.get("entry", {"x": 0, "y": 0, "direction": "right", "reverse_action": ""})
                tx, ty = entry["x"], entry["y"]
                
                # 입구 좌표로 이동
                arrived = self._move_toward(tx, ty)
                if arrived:
                    print(f"[Seq] 입구 도달: ({tx}, {ty}) → 방향키 {entry['direction']} 1초간 누름")
                    hw.hold_move(entry["direction"], "entry_hold")
                    time.sleep(1.0)  # 1초간 hold
                    self.seq_phase = "points"
                    self.seq_point_idx = 0
            
            # Points 단계
            elif self.seq_phase == "points":
                points = seq_data.get("points", [])
                if self.seq_point_idx < len(points):
                    grid_name = points[self.seq_point_idx]
                    col, row = grid_name_to_coords(grid_name)
                    self._set_nav_context(
                        "seq_point",
                        seq_point_idx=self.seq_point_idx,
                        grid_name=grid_name,
                        advance_delta=1,
                    )
                    
                    if col is not None and row is not None:
                        # Grid 좌표로 이동
                        arrived = self._move_toward_grid(col, row)
                        if arrived:
                            print(f"[Seq] 사냥점 도달: {grid_name} (Grid: {col}, {row})")
                            self.seq_point_idx += 1
                    else:
                        print(f"[Seq] 잘못된 Grid 이름: {grid_name}, 건너뜀")
                        self.seq_point_idx += 1
                else:
                    self.seq_phase = "exit"
            
            # Exit 단계
            elif self.seq_phase == "exit":
                self._set_nav_context("seq_exit")
                exit_data = seq_data.get("exit", {"x": 0, "y": 0, "direction": "down", "reverse_action": ""})
                tx, ty = exit_data["x"], exit_data["y"]
                
                # 출구 좌표로 이동
                arrived = self._move_toward(tx, ty)
                if arrived:
                    print(f"[Seq] 출구 도달: ({tx}, {ty}) → 방향키 {exit_data['direction']} 1초간 누름")
                    hw.hold_move(exit_data["direction"], "exit_hold")
                    time.sleep(1.0)  # 1초간 hold
                    # 시퀀스 완료, 다시 처음부터
                    self.seq_phase = "entry"
                    self.seq_point_idx = 0

    # ----------------------------------------------------------
    def run(self):
        print("[Nav] V5 Navigation Thread started...")
        while self.state.running:
            if getattr(self.state, "shutting_down", False):
                break

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

            waypoint_routes_enabled = bool(getattr(self.state, "route_waypoints_enabled", False))
            nav_requested = (
                self.state.nav_follow_enabled
                or self.state.nav_avoid_enabled
                or (self.state.nav_route_enabled and waypoint_routes_enabled)
            )
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

            focus_fn = getattr(hw, "_is_game_window_focused", None)
            if callable(focus_fn) and not bool(focus_fn()):
                now = time.time()
                if now - self._last_focus_block_log_time >= 1.5:
                    print("[Nav] Game window not focused; navigation paused.")
                    self._last_focus_block_log_time = now
                self._clear_nav_context()
                self._clear_nav_attempt_state()
                self.state.is_stuck = False
                stop_fn = getattr(hw, "stop_all_inputs", None)
                if callable(stop_fn):
                    try:
                        stop_fn(repeat=1, delay=0.02, disconnect=False)
                    except Exception:
                        pass
                humanized_sleep(TIMING_CONFIG["idle_sleep"])
                continue

            in_combat = bool(self.state.is_combat_busy)
            if in_combat and not self._was_in_combat:
                self._mark_combat_pause()
            elif (not in_combat) and self._was_in_combat:
                self._advance_waypoint_after_interrupt()
            self._was_in_combat = in_combat

            follow_mode_active = bool(
                self.state.is_connected
                and self.state.nav_follow_enabled
                and not self.state.nav_route_enabled
            )

            # ── 0순위: 전투 중 이동 정지 (is_combat_busy 플래그 감시) ──
            if in_combat:
                humanized_sleep(TIMING_CONFIG["nav_loop"])
                continue

            self._clear_nav_context()

            # ── 1순위: Stuck Escape (막힘 감지 시 다른 로직 일시 중단) ──
            if not follow_mode_active and self._check_stuck():
                self._escape_stuck(rewind_waypoint=False)
                humanized_sleep(TIMING_CONFIG["move_hold"])
                continue

            # ── 술사 전용: 몹 밀집 중심으로 이동 후 대기 ───────────────
            if (
                self.state.role == "술사"
                and not self._is_in_combat()
                and not self.state.nav_route_enabled
                and not (self.state.is_connected and self.state.nav_follow_enabled)
            ):
                cluster_center = self._find_cluster_center()
                if cluster_center:
                    gx, gy = cluster_center
                    arrived = self._move_toward_grid(gx, gy)
                    if arrived:
                        print(f"[MobTrain] 밀집 중심 도착: Grid({gx}, {gy}) -> 대기")
                        humanized_sleep(TIMING_CONFIG["idle_sleep"])
                    else:
                        humanized_sleep(TIMING_CONFIG["nav_loop"])
                    continue

            # ── 2순위: Group Follow Mode (is_connected && nav_follow_enabled) ──
            if follow_mode_active:
                remote_data = self.state.get_remote_data_by_role("격수") or self.state.get_remote_data()
                warrior_pos = self._get_remote_pos(remote_data)
                follow_target = self._calc_follow_target()
                if warrior_pos:
                    cx, cy = self._get_current_pos()
                    now = time.time()
                    warrior_x, warrior_y = warrior_pos
                    if not self._is_remote_follow_target_fresh(remote_data, max_age_s=0.35):
                        if now - self._last_follow_block_log_time >= 1.0:
                            print("[Follow] waiting for fresh warrior telemetry...")
                            self._last_follow_block_log_time = now
                        self._clear_nav_attempt_state()
                        self._follow_close_until = 0.0
                        self._stuck_recovery_until = max(self._stuck_recovery_until, now + 0.03)
                        self.state.is_stuck = False
                        humanized_sleep(TIMING_CONFIG["nav_loop"])
                        continue
                    gap_x = abs(warrior_x - cx)
                    gap_y = abs(warrior_y - cy)
                    if gap_x <= 1 and gap_y <= 1:
                        if now - self._last_follow_close_log_time >= 1.5:
                            print(f"[Follow] Warrior is close (x-gap={gap_x}, y-gap={gap_y})")
                            self._last_follow_close_log_time = now
                        self._follow_close_until = max(self._follow_close_until, now + 0.06)
                        self._clear_nav_attempt_state()
                        self._stuck_recovery_until = now + 0.02
                        self.state.is_stuck = False
                        humanized_sleep(TIMING_CONFIG["nav_loop"])
                        continue

                    if self._follow_close_until > now:
                        self._clear_nav_attempt_state()
                        self._stuck_recovery_until = max(self._stuck_recovery_until, now + 0.02)
                        self.state.is_stuck = False
                        humanized_sleep(TIMING_CONFIG["nav_loop"])
                        continue
                    self._follow_close_until = 0.0

                    if follow_target:
                        tx, ty = follow_target
                        self._set_nav_context("follow", target=(tx, ty), warrior_pos=warrior_pos, current=(cx, cy))
                        self._move_toward_follow(warrior_pos)
                    else:
                        self._clear_nav_attempt_state()
                        humanized_sleep(TIMING_CONFIG["nav_loop"])
                        continue
                else:
                    self._clear_nav_attempt_state()
                    humanized_sleep(TIMING_CONFIG["nav_loop"])
                    continue

            # ── 3순위: Waypoint Traversal (follow false/no target && is_in_combat false) ──
            elif waypoint_routes_enabled and self.state.nav_route_enabled and not self._is_in_combat():
                m   = self.state.current_map
                map_data = self.state.waypoints_db.get(m, {})
                
                # 맵/층 계층 구조 지원
                if isinstance(map_data, dict):
                    # 현재 층 결정 (기본값: "1")
                    current_floor = "1"
                    for floor_key in map_data.keys():
                        if floor_key.isdigit():
                            current_floor = floor_key
                            break
                    
                    seq_data = map_data.get(current_floor, {})
                    
                    # 새로운 시퀀스 구조 지원 (entry, points, exit)
                    if isinstance(seq_data, dict) and "entry" in seq_data:
                        self._traverse_sequence(seq_data)
                
                # 기존 단일 맵 구조 지원 (좌표 리스트)
                elif isinstance(map_data, list):
                    wps = map_data
                    if wps:
                        if self.wp_idx >= len(wps):
                            self.wp_idx = 0
                        self._set_nav_context("list_wp", wp_idx=self.wp_idx, advance_delta=1)
                        arrived = self._move_toward(*wps[self.wp_idx])
                        if arrived:
                            print(f"[Point] 도달: {wps[self.wp_idx]}")
                            self.wp_idx += 1

            humanized_sleep(TIMING_CONFIG["nav_loop"])

