"""
bot_v2_fsm.py  ─  AION Bot V4 FSM (Finite State Machine)
──────────────────────────────────────────────────────────────
▸ 모든 딜레이  → humanized_sleep / TIMING_CONFIG 경유
▸ 전투 Fail-safe  → combat_timeout 초과 시 루프 즉시 break + ESC
▸ 지능형 Stuck 탈출  → 후진 → 직각 랜덤 이동 → 재탐색
▸ 사전 검증  → "걸리지 않음" / 비몬스터 타겟 즉시 ESC
──────────────────────────────────────────────────────────────
"""

import time
import threading
import random

from svc_kernel import (
    AppStatus, TargetType, hw, GameState,
    TIMING_CONFIG, humanized_sleep, tc
)


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

    # ----------------------------------------------------------
    # ── 내부 헬퍼: 힐 시퀀스 ─────────────────────────────────
    # ----------------------------------------------------------
    def _execute_v3_heal(self, skill):
        """초고속 자가 회복: 스킬키 → Home → Enter (모두 humanized)."""
        if skill.hotkey:
            hw.humanized_press(skill.hotkey)
        humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
        hw.humanized_press("home")
        humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
        hw.humanized_press("enter")
        skill.last_cast_time = time.time()

    def _recover_hp(self):
        heal_name  = self.state.recovery_hp_spell
        if not heal_name or heal_name == "사용 안 함": return
        heal_skill = next((s for s in self.state.spells if s.name == heal_name), None)
        if not heal_skill: return
        if self.state.hp_trig_active:
            self._execute_v3_heal(heal_skill)
            humanized_sleep(TIMING_CONFIG["heal_gap"])

    def _recover_mp(self):
        mana_name  = self.state.recovery_mp_spell
        if not mana_name or mana_name == "사용 안 함": return
        mana_skill = next((s for s in self.state.spells if s.name == mana_name), None)
        if not mana_skill: return
        if self.state.mp_trig_active:
            print(f"[MP] MP 부족 감지 - {mana_name} 시전 중...")
            self._execute_v3_heal(mana_skill)
            humanized_sleep(TIMING_CONFIG["mp_recover_wait"])

    # ----------------------------------------------------------
    # ── 보무(Buff) 자동 관리 ─────────────────────────────────
    # ----------------------------------------------------------
    def _execute_bomu_buff_v3(self):
        """보무 버프: 8 → Home → Enter → 간격 → 9 → Home → Enter."""
        print("[Buff] 보무 버프 시전 중 (8 -> 9 순차)...")
        for key in ("8", "9"):
            hw.humanized_press(key)
            humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
            hw.humanized_press("home")
            humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
            hw.humanized_press("enter")
            if key == "8":
                humanized_sleep(TIMING_CONFIG["bomu_spell_gap"])
        self.state.last_bomu_time = time.time()
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
            hw.humanized_press(skill.hotkey)
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

        self.state.last_debuff_x = self.state.x
        self.state.last_debuff_y = self.state.y
        print("[OK] 디버프 스캔 완료.")

    # ----------------------------------------------------------
    # ── 타겟 탐색 ─────────────────────────────────────────────
    # ----------------------------------------------------------
    def _search_target_v3(self):
        """Tab → Up → Enter (모두 humanized_sleep 경유)."""
        hw.humanized_press("tab")
        humanized_sleep(TIMING_CONFIG["tab_wait"])
        hw.humanized_press("up")
        humanized_sleep(TIMING_CONFIG["up_wait"])
        hw.humanized_press("enter")
        humanized_sleep(TIMING_CONFIG["ocr_wait"])
        self.state.target_locked = True

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
            return False

        # ① '걸리지 않음' 즉시 취소
        if "걸리지 않습니다" in name:
            hw.humanized_press("esc")
            self.state.target_locked = False
            self.state.target_name   = ""
            return False

        # ② 몬스터 화이트리스트 검증
        if any(m in name for m in self.state.target_monster_names):
            return True

        # ③ 그 외 (유저, NPC 등) → ESC
        print(f"[Misc] 비몬스터 타겟: '{name}' -> ESC.")
        hw.humanized_press("esc")
        self.state.target_locked = False
        self.state.target_name   = ""
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
                self._search_target_v3()
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
                hw.humanized_press(s.hotkey)
                humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
                s.last_cast_time = time.time()
                break

    # ----------------------------------------------------------
    # ── 메인 FSM 루프 ─────────────────────────────────────────
    # ----------------------------------------------------------
    def run(self):
        print(f"[Action] V4 지능형 Action 엔진 시작... (역할: {self.state.role})")
        while self.state.running:

            # Hot-Reload
            if self.state.last_update_time > self.last_load_time:
                self.last_load_time = time.time()
                print("[Sync] [Action] 마법 설정 실시간 동기화.")

            # ── 역할별 분기 ───────────────────────────────────
            if self.state.role == "격수":
                # 격수: 데이터 공유 서버 역할만, 사냥 로직 끄기
                self._run_dps_server_mode()
            elif self.state.role == "도사":
                # 도사: 격수의 데이터 수신받아 지원 로직
                self._run_support_mode()
            elif self.state.role == "술사":
                # 술사: 첨사냥 로직
                self._run_priest_mode()
            else:
                # 기본: 기존 로직
                self._run_default_mode()

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
        if self.state.hp_trig_active:
            self._recover_hp()
            self.state.combat_start_time = 0.0

        if self.state.mp_trig_active:
            self._recover_mp()
            if self.state.hp_trig_active:
                self._recover_hp()

        # 보무 버프 유지 (185s 주기)
        if time.time() - self.state.last_bomu_time > TIMING_CONFIG["bomu_interval"]:
            self._execute_bomu_buff_v3()

        # 디버프 스캔 (격수도 필요)
        dist_change = (abs(self.state.x - self.state.last_debuff_x) +
                       abs(self.state.y - self.state.last_debuff_y))
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
        if self.state.hp_trig_active:
            self._recover_hp()
            self.state.combat_start_time = 0.0

        if self.state.mp_trig_active:
            self._recover_mp()
            if self.state.hp_trig_active:
                self._recover_hp()

        # 보무 버프 유지 (185s 주기)
        if time.time() - self.state.last_bomu_time > TIMING_CONFIG["bomu_interval"]:
            self._execute_bomu_buff_v3()

        # 디버프 스캔
        dist_change = (abs(self.state.x - self.state.last_debuff_x) +
                       abs(self.state.y - self.state.last_debuff_y))
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
        me = self.state.entities.get("me")
        if not isinstance(me, dict):
            return None, [], 0
        me_grid = me.get("grid")
        if not me_grid or len(me_grid) != 2:
            return None, [], 0

        valid_monsters = []
        dense_count = 0
        for m in self._get_priest_monsters():
            grid = m.get("grid") if isinstance(m, dict) else None
            if not grid or len(grid) != 2:
                continue
            dx = int(grid[0]) - int(me_grid[0])
            dy = int(grid[1]) - int(me_grid[1])
            dist = (dx * dx + dy * dy) ** 0.5
            valid_monsters.append({"monster": m, "dist": dist, "dx": dx, "dy": dy})
            if max(abs(dx), abs(dy)) <= 1:
                dense_count += 1
        return me_grid, valid_monsters, dense_count

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
        hw.humanized_press(skill.hotkey)
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
        if self.state.hp_trig_active:
            self._recover_hp()
            self.state.combat_start_time = 0.0

        if self.state.mp_trig_active:
            self._recover_mp()
            if self.state.hp_trig_active:
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
                if self.state.target_locked or self.state.hp_trig_active:
                    print("[Action] 획득 중 전투/위급 상황 발생 -> 루팅 중단.")
                    break

                # ── 회피 이동 (Stuck 감지) 통합 ──
                if self.state.nav_avoid_enabled and self._check_stuck():
                    self._escape_stuck()
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
        dist_change = (abs(self.state.x - self.state.last_debuff_x) +
                       abs(self.state.y - self.state.last_debuff_y))
        if self.state.auto_debuff_enabled and dist_change > 15:
            self._execute_debuff_scan_v3()

        me_grid, monster_infos, dense_count = self._calc_monster_metrics()
        has_monsters = len(monster_infos) > 0
        if has_monsters:
            aoe_mode = dense_count >= 2
            # 단일 타겟 스킬은 타겟 락이 필요하므로 기존 탐색 시퀀스 유지
            if (not aoe_mode) and (not self.state.target_locked):
                self.state.combat_start_time = 0.0
                self._search_target_v3()
                humanized_sleep(TIMING_CONFIG["ocr_fast"])

            skill = self._find_ready_attack_skill(aoe_mode=aoe_mode)
            if skill:
                mode_label = "광역(첨)" if aoe_mode else "단일"
                print(f"[Priest] 공격 분기: {mode_label} | 근접밀집={dense_count} | 레이더몹={len(monster_infos)}")
                self._cast_attack_skill(skill)
            else:
                self._execute_combat()
        else:
            # 레이더 데이터가 비었을 때는 기존 전투 루프 폴백
            if not self.state.target_locked:
                self.state.combat_start_time = 0.0
                self._search_target_v3()
            humanized_sleep(TIMING_CONFIG["ocr_fast"])
            self._execute_combat()

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
        if self.state.hp_trig_active:
            self._recover_hp()
            self.state.combat_start_time = 0.0

        if self.state.mp_trig_active:
            self._recover_mp()
            if self.state.hp_trig_active:
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
                if self.state.target_locked or self.state.hp_trig_active:
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
        dist_change = (abs(self.state.x - self.state.last_debuff_x) +
                       abs(self.state.y - self.state.last_debuff_y))
        if self.state.auto_debuff_enabled and dist_change > 15:
            self._execute_debuff_scan_v3()

        # ══════════════════════════════════════════════════
        # 5. 타겟 탐색 & 전투
        # ══════════════════════════════════════════════════
        if not self.state.target_locked:
            self.state.combat_start_time = 0.0
            self._search_target_v3()

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
#  Grid 좌표 변환 함수 (GridConverter 임포트 필요 시)
# ============================================================
def grid_name_to_coords(grid_name):
    """Grid 이름(S3, A10 등)을 좌표(col, row)로 변환"""
    if not grid_name or len(grid_name) < 2:
        return None, None
    # 알파벳 추출 (열)
    col_part = ""
    row_part = ""
    for i, char in enumerate(grid_name):
        if char.isalpha():
            col_part += char.upper()
        else:
            row_part = grid_name[i:]
            break
    if not col_part or not row_part:
        return None, None
    try:
        # 열 계산 (A=0, B=1, ..., S=18)
        col = 0
        for char in col_part:
            col = col * 26 + (ord(char) - ord('A'))
        # 행 계산 (1-indexed → 0-indexed)
        row = int(row_part) - 1
        return col, row
    except (ValueError, IndexError):
        return None, None

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
        self.last_pos = (0, 0)
        self.stuck_timer = time.time()
        self.stuck_count = 0
        # 시퀀스 기반 이동용 변수
        self.seq_phase = "entry"  # entry, points, exit
        self.seq_point_idx = 0
        self.seq_last_action_time = 0

    # ----------------------------------------------------------
    def _check_stuck(self) -> bool:
        """최근 1.5초간 좌표 변화가 없으면 stuck_count를 증가."""
        now = time.time()
        cx, cy = self.state.x, self.state.y
        elapsed = now - self.stuck_timer

        # 좌표가 변했면 타이머와 카운터 리셋
        if (cx, cy) != self.last_pos:
            self.last_pos = (cx, cy)
            self.stuck_timer = now
            self.stuck_count = 0
            return False

        # 0.7초 이상 변화 없으면 stuck_count 증가
        if elapsed > 0.7:  # 1.5s -> 0.7s (2x faster)
            self.stuck_count += 1
            self.stuck_timer = now
            print(f"[Stuck] 좌표 변화 없음 ({self.stuck_count}회 연속)")
            if self.stuck_count >= 2:  # 2회 연속 0.7초 변화 없으면 회피 기동
                return True
        return False

    # ----------------------------------------------------------
    def _escape_stuck(self):
        """
        지능형 회피 기동 시퀀스:
        1단계: 직전 좌표로 이동 (후진)
        2단계: 랜덤 횡이동 (좌측/우측)
        3단계: 2~3회 반복하여 장애물 우회
        4단계: 타겟/웨이포인트 리셋
        """
        print("[Warn] [STUCK] 막힘 감지! 회피 기동 시작...")
        self.state.is_stuck = True  # 회피 기동 중 플래그 설정

        # 2~3회 반복
        for i in range(random.randint(2, 3)):
            # 1단계: 직전 좌표로 이동 (후진)
            # last_pos가 유효하면 그 방향으로 이동
            if self.last_pos != (0, 0):
                cx, cy = self.state.x, self.state.y
                lx, ly = self.last_pos
                dx, dy = lx - cx, ly - cy
                
                # 방향 결정
                if abs(dx) > abs(dy):
                    move_dir = "right" if dx > 0 else "left"
                else:
                    move_dir = "down" if dy > 0 else "up"
                
                hw.hold_move(move_dir, "stuck_back_hold")
                humanized_sleep(TIMING_CONFIG["key_gap"])
            
            # 2단계: 랜덤 이동 (상하좌우 4방향 — 수직 장애물 탈출 포함)
            side_dir = random.choice(["left", "right", "up", "down"])
            hw.hold_move(side_dir, "stuck_side_hold")
            humanized_sleep(TIMING_CONFIG["key_gap"])

        # 3단계: 타겟/웨이포인트 리셋
        self.state.target_locked = False
        self.state.target_name = ""
        if self.wp_idx > 0:
            self.wp_idx -= 1  # 이전 웨이포인트로 되돌림
        
        # 회피 기동 완료
        self.state.is_stuck = False
        self.stuck_count = 0
        self.stuck_timer = time.time()
        print("[OK] [STUCK] 회피 기동 완료. 재탐색 개시.")

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
        격수의 last_move_dir을 기반으로 등 뒤 좌표 계산.
        격수가 바라보는 방향의 반대편 1~2칸 뒤를 타겟으로 설정.
        """
        # 네트워크에서 격수 데이터 확인
        remote_data = self.state.get_remote_data_by_role("격수") or self.state.get_remote_data()
        if not remote_data:
            return None

        dps_x = remote_data.get("x", 0)
        dps_y = remote_data.get("y", 0)
        dps_dir = remote_data.get("last_move_dir", None)

        # 방향이 없으면 격수 좌표 그대로 사용
        if not dps_dir or dps_dir not in self._ALL_DIRS:
            return (dps_x, dps_y)

        # 방향 반대 매핑 (격수가 보는 방향의 반대가 등 뒤)
        behind_offset = {
            "up": (0, 2),      # 격수가 위를 보면 아래 2칸이 등 뒤
            "down": (0, -2),    # 격수가 아래를 보면 위 2칸이 등 뒤
            "left": (2, 0),     # 격수가 왼쪽을 보면 오른쪽 2칸이 등 뒤
            "right": (-2, 0)    # 격수가 오른쪽을 보면 왼쪽 2칸이 등 뒤
        }.get(dps_dir, (0, 0))

        target_x = dps_x + behind_offset[0]
        target_y = dps_y + behind_offset[1]

        return (target_x, target_y)

    def _move_toward(self, tx: int, ty: int) -> bool:
        """
        목표 좌표 방향으로 1스텝 이동. 도달 시 True 반환.
        Grid 기반 좌표 변환, 벽 체크, 전투 중 이동 중단.
        """
        # 전투 중이면 즉시 이동 중단
        if self._is_in_combat():
            return False

        cx, cy = self.state.x, self.state.y
        dx, dy = tx - cx, ty - cy

        # Grid 기반 좌표 변환 (maps.json의 grid_size 활용)
        map_data = self.state.maps_db.get(self.state.current_map)
        if map_data:
            grid_size = map_data.get("grid_size", 32)
            # 절대 좌표를 Grid 좌표로 변환
            gx_dx = int(dx / grid_size)
            gy_dy = int(dy / grid_size)
        else:
            # 맵 데이터 없으면 절대 좌표 그대로 사용
            gx_dx = dx
            gy_dy = dy

        # ── 이동 로그 ─────────────────────────────────────
        print(f"[Nav] Target: ({tx}, {ty}) | Current: ({cx}, {cy}) | Grid: {self.state.char_grid}")

        # ── 주 이동 방향 및 벽(Wall) 회피 로직 ────────────────────────
        ccx, ccy = self.state.char_grid
        
        # 거리가 먼 축을 먼저 이동하여 자연스러운 대각선 궤적 형성
        if abs(gx_dx) > abs(gy_dy):
            axis_priority = ["x", "y"]
        elif abs(gy_dy) > abs(gx_dx):
            axis_priority = ["y", "x"]
        else:
            axis_priority = random.choice([["x", "y"], ["y", "x"]])
            
        step_dir = None
        for axis in axis_priority:
            if axis == "x" and abs(gx_dx) > 0:
                candidate_dir = "right" if gx_dx > 0 else "left"
                gx_next = ccx + (1 if gx_dx > 0 else -1)
                gy_next = ccy
                if not self._is_wall(gx_next, gy_next):
                    step_dir = candidate_dir
                    break
            elif axis == "y" and abs(gy_dy) > 0:
                candidate_dir = "down" if gy_dy > 0 else "up"
                gx_next = ccx
                gy_next = ccy + (1 if gy_dy > 0 else -1)
                if not self._is_wall(gx_next, gy_next):
                    step_dir = candidate_dir
                    break

        if step_dir is None:
            # 두 방향 모두 이동 불가(벽)이거나 도착 직전인 경우
            if abs(gx_dx) > 0 or abs(gy_dy) > 0:
                print(f"🚧 진행 가능한 모든 경로 벽 감지 -> 회피 가동")
                self._escape_stuck()
                return False
        else:
            hw.hold_move(step_dir, "move_hold")
            self.state.last_move_dir = step_dir

        arrived = abs(gx_dx) <= 1 and abs(gy_dy) <= 1
        if arrived:
            print(f"[Nav] 도착 완료: 웨이포인트 {self.wp_idx}번째 완료")
        return arrived

    # [V5] 아이템 획득용 격자 기반 이동
    def _move_toward_grid(self, gx: int, gy: int) -> bool:
        """아이템의 Grid 좌표로 한 칸씩 정밀 이동."""
        ccx, ccy = self.state.char_grid
        dgx, dgy = gx - ccx, gy - ccy

        # 갱신된 위치 확인을 위해 잠시 대기 (Action 루프)
        arrived = (abs(dgx) == 0 and abs(dgy) == 0)
        if arrived:
            return True

        if abs(dgx) > abs(dgy):
            axis_priority = ["x", "y"]
        elif abs(dgy) > abs(dgx):
            axis_priority = ["y", "x"]
        else:
            axis_priority = random.choice([["x", "y"], ["y", "x"]])

        step_dir = None
        for axis in axis_priority:
            if axis == "x" and abs(dgx) > 0:
                step_dir = "right" if dgx > 0 else "left"
                break
            elif axis == "y" and abs(dgy) > 0:
                step_dir = "down" if dgy > 0 else "up"
                break

        if step_dir:
            hw.hold_move(step_dir, "move_hold")
            
        return False

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
        print("[Nav] V5 Navigation Thread 시작...")
        while self.state.running:

            # Hot-Reload
            if self.state.last_update_time > self.last_load_time:
                self.last_load_time = time.time()
                print("[Sync] [Nav] 경로 설정 실시간 동기화.")

            if not self.state.auto_hunt:
                humanized_sleep(TIMING_CONFIG["idle_sleep"] * 2)
                continue

            # ── 0순위: 전투 중 이동 정지 (is_combat_busy 플래그 감시) ──
            if self.state.is_combat_busy:
                humanized_sleep(TIMING_CONFIG["nav_loop"])
                continue

            # ── 1순위: Stuck Escape (막힘 감지 시 다른 로직 일시 중단) ──
            if self.state.nav_avoid_enabled and self._check_stuck():
                self._escape_stuck()
                humanized_sleep(TIMING_CONFIG["move_hold"])
                continue

            # ── 술사 전용: 몹 밀집 중심으로 이동 후 대기 ───────────────
            if self.state.role == "술사" and not self._is_in_combat():
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
            if self.state.is_connected and self.state.nav_follow_enabled:
                follow_target = self._calc_follow_target()
                if follow_target:
                    tx, ty = follow_target
                    # 격수와의 거리 계산 (Grid 기반)
                    remote_data = self.state.get_remote_data_by_role("격수") or self.state.get_remote_data()
                    if remote_data:
                        dps_x = remote_data.get("x", 0)
                        dps_y = remote_data.get("y", 0)
                        cx, cy = self.state.x, self.state.y
                        dist = ((tx - cx) ** 2 + (ty - cy) ** 2) ** 0.5
                        
                        # 거리 1-2 Grid 내에 도달하면 정지
                        map_data = self.state.maps_db.get(self.state.current_map)
                        if map_data:
                            grid_size = map_data.get("grid_size", 32)
                            grid_dist = dist / grid_size
                            if grid_dist <= 2:
                                print(f"[Follow] 격수 근접 도달 (거리: {grid_dist:.1f} Grid)")
                                humanized_sleep(TIMING_CONFIG["nav_loop"])
                                continue
                    
                    self._move_toward(tx, ty)

            # ── 3순위: Waypoint Traversal (follow false/no target && is_in_combat false) ──
            elif self.state.nav_route_enabled and not self._is_in_combat():
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
                        arrived = self._move_toward(*wps[self.wp_idx])
                        if arrived:
                            print(f"[Point] 도달: {wps[self.wp_idx]}")
                            self.wp_idx += 1

            humanized_sleep(TIMING_CONFIG["nav_loop"])
