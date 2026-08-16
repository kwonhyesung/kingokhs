# -*- coding: utf-8 -*-
"""
마법, 회복, 스펠 통합 관리 모듈
이 파일은 바람의나라 게임에서 마법(스킬)을 사용하거나, 체력(HP) 및 마력(MP)을 회복하는 
모든 동작을 한 곳에서 관리하기 위해 만들어진 파일입니다.
파이썬을 처음 접하시는 분들도 쉽게 이해하고 수정할 수 있도록 모든 줄에 상세한 주석을 달았습니다.
"""

import json
import os
import time
import random # 시간 지연(sleep) 기능을 사용하기 위해 파이썬 기본 모듈인 time을 불러옵니다.
from typing import Optional # 변수나 함수가 '값이 없을 수도 있음(None)'을 표시하기 위해 불러옵니다.
from bis_core import hw, GameState, TIMING_CONFIG, humanized_sleep 
# 우리 프로그램의 핵심 설정(bis_core)에서 하드웨어 제어기(hw), 게임 상태(GameState), 
# 타이밍 설정(TIMING_CONFIG), 사람처럼 딜레이를 주는 함수(humanized_sleep)를 가져옵니다.

# ----------------------------------------------------------
# ── 하위/상위 계층 입력 지원 (spell_casting.py 통합) ───────
# ----------------------------------------------------------
class SpellCaster:
    """
    스킬 캐스팅 유틸리티 (하드웨어/소프트웨어 공통)
    키보드를 실제로 눌러서 게임 안에 마법을 시전하게 만들어주는 '마법 시전기' 역할을 하는 클래스(설계도)입니다.
    """
    
    def __init__(self, hardware=hw):
        # 클래스가 처음 생성될 때 실행되는 초기화 함수입니다.
        # 기본적으로 하드웨어 신호 발생기(hw)를 자기 자신(self.hardware)에 저장합니다.
        self.hardware = hardware

    def _press_fast(self, key: str, variance: float = 0.15):
        """
        초고속 키 입력 (ft.ahk 스타일 호환)
        단순히 키보드를 아주 빠르게 눌렀다 떼는 기능입니다.
        - key: 누를 키보드 키 (예: "esc", "enter", "3")
        - variance: 지연 시간에 사람처럼 랜덤한 오차를 주는 비율입니다. 기본값 0.15 (15% 오차)
        """
        try:
            # 먼저 아주 빠른 입력(fast_press)을 시도합니다.
            self.hardware.fast_press(key, variance=variance)
        except Exception:
            # 만약 위 방법이 실패하면, 조금 더 사람의 타이밍에 가까운 일반 입력(humanized_press)을 합니다.
            self.hardware.humanized_press(key, variance=variance)
            
    def send_key_sequence(self, keys: list, delay: float = 0.1):
        """
        여러 개의 키보드 입력을 순서대로 연속해서 누르는 함수입니다.
        예를 들어, Shift 누른 채로 z 누르기, Ctrl 누르기 등을 처리합니다.
        """
        for key in keys: # 목록(keys)에 있는 키를 하나씩 꺼내서 반복합니다.
            if key.startswith('+'): 
                # 키 이름 앞에 '+'가 붙어있으면 Shift 키를 조합해서 누른다는 뜻입니다. (예: '+z' -> Shift + z)
                self.hardware.press_key('shift') # Shift 키를 꾹 누릅니다.
                humanized_sleep(0.01, variance=0.40) # 0.01초 대기
                self.hardware.press_key(key[1:]) # '+' 다음 글자(예: z)를 누릅니다.
                humanized_sleep(delay, variance=0.40) # 입력 후 지정된 딜레이(지연시간)만큼 기다립니다.
                self.hardware.release_key(key[1:]) # z 키를 뗗니다.
                self.hardware.release_key('shift') # Shift 키도 뗗니다.
            elif key.startswith('^'):
                # 키 이름 앞에 '^'가 붙어있으면 Ctrl 키를 조합해서 누른다는 뜻입니다. (예: '^a' -> Ctrl + a)
                self.hardware.press_key('ctrl') # Ctrl 키를 누릅니다.
                humanized_sleep(0.3, variance=0.40)
                self.hardware.press_key(key[1:]) # '+' 다음 글자를 누릅니다.
                humanized_sleep(delay, variance=0.40)
                self.hardware.release_key(key[1:]) # 키를 뗗니다.
                self.hardware.release_key('ctrl') # Ctrl 키도 뗗니다.
            else:
                # 아무 기호가 없다면 그냥 평범한 키 입력입니다. (예: 'enter')
                self.hardware.press_key(key) # 해당 키를 누릅니다.
                humanized_sleep(delay, variance=0.40) # 잠깐 기다렸다가
                self.hardware.release_key(key) # 키를 뗗니다.
            humanized_sleep(0.02, variance=0.40) # 하나의 키 입력을 마치고 다음 키를 누르기 전에 0.02초 휴식합니다.

    def cast_spell(self, spell_char: str, delay: float = 0.1):
        """마법 단축키(예: a, b, c)를 한 번만 누르는 기능입니다."""
        self.hardware.press_key(spell_char.lower()) # 단축키를 소문자로 변환하여 누릅니다.
        humanized_sleep(0.05, variance=0.40) # 0.05초 대기
        self.hardware.release_key(spell_char.lower()) # 단축키를 뗗니다.
        
    def _release_movement_keys(self, block_duration: float = 0.9):
        """방향키가 눌려있으면 타겟선택 박스(Home/화살표 확정)가 캐릭터 대신 그
        박스를 움직여서 이동이 멈춰버린다. 대상선택형 시전 전엔 항상 놓고,
        박스가 떠있는 동안 이동 스레드(RouteSvc)가 방향키를 다시 못 누르도록
        support_input_blocked_until을 함께 세워 잠근다."""
        state = getattr(self.hardware, "state", None)
        if state is not None:
            until = time.time() + block_duration
            current = float(getattr(state, "support_input_blocked_until", 0.0) or 0.0)
            state.support_input_blocked_until = max(current, until)
        for direction in ("up", "down", "left", "right"):
            try:
                self.hardware.release_key(direction)
            except Exception:
                pass

    def spell_home_enter(self, spell_char: str, delay: float = 0.1):
        """자가 마법을 사용할 때 쓰는 매크로입니다. (단축키 -> Home 키 -> Enter)"""
        # +z (Shift+Z)는 대화창 등을 초기화하는 용도로 흔히 쓰입니다.
        self._release_movement_keys()
        self.send_key_sequence(['+z', spell_char.lower(), 'home', 'enter', 'escape'], delay)

    def spell_click_enter(self, spell_char: str, delay: float = 0.1):
        """특정 대상 없이 단축키만 눌러서 마우스 클릭 등으로 발동할 때 씁니다."""
        self.hardware.press_key(spell_char.lower())
        humanized_sleep(0.1, variance=0.40)
        self.hardware.release_key(spell_char.lower())

    def spell_arrow_enter(self, spell_char: str, arrow: str = "up", delay: float = 0.1):
        """마법을 쓰고 방향키(기본값: 위쪽)를 누른 뒤 엔터를 쳐서 마법을 발동시킵니다."""
        self._release_movement_keys()
        self.send_key_sequence(['+z', spell_char.lower(), arrow.lower(), 'enter', 'escape'], delay)
        
    def spell_enter(self, spell_char: str, delay: float = 0.1):
        """마법 단축키를 누르고 바로 엔터를 치는 기능 (즉발 마법용)"""
        self.send_key_sequence(['+z', spell_char.lower(), 'enter', 'escape'], delay)
        
    def spell_text_enter(self, spell_char: str, text: str, delay: float = 0.1):
        """마법을 쓰고 특정 글자(닉네임 등)를 직접 타이핑한 뒤 엔터를 치는 기능입니다."""
        self.send_key_sequence(['+z', spell_char.lower()], delay) # 일단 마법키를 누름
        humanized_sleep(0.05, variance=0.40)
        for char in text: # 입력할 글자(text)를 한 글자씩 반복해서 누릅니다.
            self.hardware.press_key(char)
            humanized_sleep(0.02, variance=0.40)
            self.hardware.release_key(char)
        humanized_sleep(0.05, variance=0.40)
        self.send_key_sequence(['enter', 'escape'], delay) # 타이핑 후 엔터치고 ESC로 혹시 모를 창을 닫습니다.
        
    def check_spell(self, spell_name: str, delay: float = 0.1):
        """채팅창에 '/딜 마법이름' 을 쳐서 쿨타임을 확인하는 용도입니다."""
        self.send_key_sequence(['/', '딜', 'space', spell_name, 'enter', 'escape'], delay)
        
    def cast_and_check_spell(self, spell_char: str, spell_name: str, delay: float = 0.1):
        """마법을 쏘고 바로 쿨타임을 확인하는 기능입니다."""
        self.cast_spell(spell_char, delay)
        humanized_sleep(0.5, variance=0.40)
        self.check_spell(spell_name, delay)
        
    def use_item(self, item_char: str, delay: float = 0.01):
        """아이템 단축키(u + 아이템알파벳)를 눌러서 아이템을 사용합니다."""
        self.send_key_sequence(['u', item_char.lower()], delay)
        
    def control_item(self, item_char: str, delay: float = 0.01):
        """Ctrl + 아이템알파벳을 눌러서 아이템을 제어합니다."""
        self.send_key_sequence(['^' + item_char.lower()], delay)


class TargetTypeMapper:
    """
    타겟 타입과 캐스팅 함수 매핑
    마법마다 사용 방식(타겟지정인지, 즉시발동인지 등)이 다른데, 이를 함수와 짝지어주는 역할을 합니다.
    """
    def __init__(self):
        # 딕셔너리(사전) 형태로 각 마법의 형태와 실행할 함수 이름을 매칭해둡니다.
        self.mapping = {
            "대상선택형": "spell_arrow_enter", # 화살표로 대상 지정 후 엔터
            "즉발형": "spell_enter",           # 누르자마자 바로 엔터
            "대상입력형": "spell_text_enter"   # 아이디를 타이핑해서 엔터
        }
    
    def get_cast_function(self, target_type: str) -> str:
        """종류를 알려주면 그에 맞는 함수 이름을 반환합니다."""
        return self.mapping.get(target_type, "spell_arrow_enter")
    
    def validate_target_type(self, target_type: str) -> bool:
        """이 마법 종류가 우리가 아는 종류인지 확인합니다."""
        return target_type in self.mapping


# 언제 어디서든 바로 사용할 수 있도록 전역 인스턴스(객체)를 하나씩 만들어둡니다.
target_mapper = TargetTypeMapper()
spell_caster = SpellCaster(hw)


# ----------------------------------------------------------
# ── 회복/힐 통합 매니저 (svc_logic & dosa_functions 통합) ──
# ----------------------------------------------------------
class RecoveryManager:
    """
    마법, HP/MP 회복, 힐 로직 등을 통합 관리
    가장 핵심적인 부분으로 체력과 마력이 떨어졌을 때 어떻게 행동할지 결정하는 곳입니다.
    """
    def __init__(self, state: GameState):
        self.state = state          # 현재 게임의 상태(HP, MP 등)를 알 수 있게 연결합니다.
        self.caster = spell_caster  # 아까 만든 키보드 입력기(spell_caster)를 가져옵니다.
        
        # 중복 방지를 위한 시간 기록 (Rate Limiting)
        # 너무 자주 연속으로 스킬이 나가지 않도록 '마지막에 언제 썼는지' 기억하는 변수들입니다.
        self._last_party_hp_recover_time = 0.0 
        self._last_hp_recover_time = 0.0
        self._last_mp_recover_time = 0.0
        
        # ---------------------------------------------------------
        # [수정 가이드] 기준값 설정
        # 힐이나 회복을 할 '조건(기준 체력/마력)'을 정하는 곳입니다!
        # 더 자주 힐을 주고 싶다면 이 숫자를 더 높게 수정하세요. 
        # (예: 체력 15만에서 힐을 주려면 150000 으로 변경)
        # ---------------------------------------------------------
        self.heal_threshold = 100000        # 격수(파티원) 체력이 이 수치 이하면 힐(희원)을 줍니다.
        self.self_heal_threshold = 30000    # 도사 본인의 체력이 이 수치 이하면 자기 자신에게 힐을 줍니다.
        self.mana_threshold = 30000         # 도사 본인의 마력이 이 수치 이하면 공력증강(MP회복)을 씁니다.
        self.dosa_heal_limit = 300000       # 도사 스스로 생명력을 회복할 때 고려하는 최대 한계치입니다.

    def resolve_recovery_key(self, selected: str, default_key: str):
        """
        GUI(프로그램 화면)에서 선택한 스킬 이름을 실제 키보드 단축키로 바꿔주는 함수입니다.
        만약 화면에서 스킬을 선택하지 않았다면, 기본값(default_key)을 사용합니다.
        """
        skill = None
        key = default_key # 초기값 설정
        try:
            if selected:
                # 사용자가 프로그램 화면에서 등록한 마법 목록(spells) 중에서 이름이 같은 것을 찾습니다.
                skill = next((s for s in self.state.spells if getattr(s, "name", "") == selected), None)
                if skill is not None and getattr(skill, "hotkey", None):
                    key = skill.hotkey # 찾은 마법에 할당된 핫키(단축키)를 가져옵니다.
                elif skill is not None and getattr(skill, "spell_char", None):
                    key = skill.spell_char
                elif isinstance(selected, str) and len(selected) == 1:
                    # 만약 이름 자체가 한 글자(예: '3')라면 그냥 그 글자를 키로 씁니다.
                    key = selected
        except Exception:
            pass # 에러가 나면 그냥 조용히 넘어가서 기본값을 씁니다.
            
        return key, skill

    def resolve_recovery_cast_function(self, selected: str, default_func: str = "SpellEnter") -> str:
        try:
            config_path = os.path.join(os.path.dirname(__file__), "spells_config.json")
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    spells = json.load(f)
                for spell in spells:
                    if str(spell.get("name", "") or "").strip() == str(selected or "").strip():
                        cast_function = str(spell.get("cast_function", "") or "").strip()
                        if cast_function in {"SpellEnter", "SpellHomeEnter", "SpellArrowEnter"}:
                            return cast_function
                        break
        except Exception:
            pass
        return default_func

    def cast_recovery_sequence(
        self,
        key: str,
        cast_function: str,
        arrow: str = "up",
    ) -> str:
        mode = cast_function if cast_function in {"SpellEnter", "SpellHomeEnter", "SpellArrowEnter"} else "SpellEnter"
        if mode in {"SpellHomeEnter", "SpellArrowEnter"}:
            self.caster._release_movement_keys()
        self.caster._press_fast(key)
        humanized_sleep(TIMING_CONFIG["spell_cast_gap"])

        if mode == "SpellHomeEnter":
            self.caster._press_fast("home")
            humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
        elif mode == "SpellArrowEnter":
            self.caster._press_fast(arrow)
            humanized_sleep(TIMING_CONFIG["spell_cast_gap"])

        self.caster._press_fast("enter")
        humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
        return mode

    # ==========================================================
    # ── 자체 회복(Self Recovery) 로직 (도사 본인 살리기)
    # ==========================================================
    def execute_self_hp_recovery(self):
        """
        자가 HP 회복 스킬 시전 (내 피가 없을 때 나에게 힐)
        """
        # 프로그램 화면에 등록된 '자가 회복 마법' 단축키를 찾습니다. 없으면 기본 키 '3'번을 씁니다.
        key, skill = self.resolve_recovery_key(getattr(self.state, "recovery_hp_spell", ""), default_key="3")
        now = time.time() # 지금 시간을 가져옵니다.
        self._last_hp_recover_time = now # 마지막 사용 시간을 '지금'으로 업데이트 합니다.

        # 스킬이 아직 쿨타임(기다리는 시간) 중이라면 더 진행하지 않고 멈춥니다(return).
        if skill is not None and hasattr(skill, "is_ready") and not skill.is_ready():
            return

        # 나 자신에게 힐을 주는 올바른 키보드 순서: ESC -> 숫자키(마법) -> Home(나 자신 지정) -> Enter
        # Home으로 대상선택 박스가 뜨는 동안엔 방향키가 캐릭터 대신 그 박스를
        # 움직이므로, 이동키가 눌려있으면 캐릭터가 멈춰버린다 - 먼저 놓는다.
        self.caster._release_movement_keys()
        self.caster._press_fast("esc") # 혹시 열려있는 창이 있을 수 있으니 ESC를 먼저 누릅니다.
        humanized_sleep(TIMING_CONFIG["key_gap"]) # 사람처럼 약간 기다립니다 (0.05초 등).
        
        self.caster._press_fast(key) # 힐 마법 단축키(예: 3)를 누릅니다.
        humanized_sleep(TIMING_CONFIG["spell_cast_gap"]) 
        
        self.caster._press_fast("home") # Home 키를 눌러 내 캐릭터를 타겟으로 잡습니다.
        humanized_sleep(TIMING_CONFIG["spell_cast_gap"]) 
        
        self.caster._press_fast("enter") # 엔터를 눌러 마법을 시전합니다.

        if skill is not None:
            try:
                skill.last_cast_time = now # 마법을 성공적으로 썼으니, 쓴 시간을 기록합니다.
            except Exception:
                pass
        humanized_sleep(TIMING_CONFIG["heal_gap"]) # 힐 시전 모션이 끝날 때까지 잠깐 기다려줍니다.

    def _recover_self_hp_after_boost(self, repeat_count: int | None = None):
        if repeat_count is None:
            repeat_count = random.randint(5, 8)
        repeat_count = max(1, int(repeat_count))
        print(f"[Recovery] ???? ? ?? ??: {repeat_count}?")
        for _ in range(repeat_count):
            self.execute_self_hp_recovery()
            humanized_sleep(min(0.08, float(TIMING_CONFIG.get("key_gap", 0.05))), variance=0.10)

    def execute_self_mp_recovery(self):
        """
        ?? MP ?? ?? ?? (????).
        MP? 40000 ???? ?? ?? MP(30)? ?? ?? ????,
        ????? MP? ???? ?? ?? ??? ??? ????.
        """
        current_mp = max(0, int(getattr(self.state, "mp", 0) or 0))
        good_mp = max(0, int(getattr(self.state, "good_mp", 0) or 0))

        if current_mp < 30:
            print(f"[Recovery] ???? ??: MP={current_mp} (?? 30 ??)")
            return

        if current_mp >= 40000 and (good_mp <= 0 or current_mp >= good_mp):
            return

        key, skill = self.resolve_recovery_key(getattr(self.state, "recovery_mp_spell", ""), default_key="2")
        now = time.time()
        self._last_mp_recover_time = now

        if skill is not None and hasattr(skill, "is_ready") and not skill.is_ready():
            return

        before_mp = current_mp
        # Self MP recovery should start from a cleared target state.
        self.caster._press_fast("esc")
        humanized_sleep(TIMING_CONFIG["key_gap"])
        cast_function = self.resolve_recovery_cast_function(
            getattr(self.state, "recovery_mp_spell", ""),
            default_func="SpellEnter",
        )
        self.cast_recovery_sequence(key, cast_function)
        if skill is not None:
            try:
                skill.last_cast_time = now
            except Exception:
                pass

        target_mp = max(40000, good_mp)
        success = False
        deadline = time.time() + 1.0
        while time.time() < deadline:
            refreshed_mp = max(0, int(getattr(self.state, "mp", 0) or 0))
            if refreshed_mp >= target_mp or refreshed_mp > before_mp:
                success = True
                break
            humanized_sleep(0.06, variance=0.10)

        if not success:
            print(f"[Recovery] ???? ?? ???: before={before_mp}, now={int(getattr(self.state, 'mp', 0) or 0)}")
            return

        print(f"[Recovery] ???? ??: before={before_mp}, now={int(getattr(self.state, 'mp', 0) or 0)}")
        self._recover_self_hp_after_boost()


    def execute_party_hp_recovery(self, snapshot: dict, use_red_tab: bool = False, force_direct: bool = False):
        """
        파티원(격수) HP 회복 스킬 시전
        격수의 체력(snapshot 데이터)을 보고 필요할 때 힐을 주는 함수입니다.
        """
        key, skill = self.resolve_recovery_key(getattr(self.state, "recovery_hp_spell", ""), default_key="c")
        now = time.time()

        elapsed = now - self._last_party_hp_recover_time
        if elapsed < 0.18:
            return

        self._last_party_hp_recover_time = now

        if skill is not None and hasattr(skill, "is_ready") and not skill.is_ready():
            print(f"[Support] HP heal skill on cooldown: {getattr(skill, 'name', key)}")
            return

        hp_val = int(snapshot.get('hp', 0) or 0)
        print(f"[Support] HP heal casting on warrior: key={key}, hp={hp_val}")
        cast_function = self.resolve_recovery_cast_function(
            getattr(self.state, "recovery_hp_spell", ""),
            default_func="SpellEnter",
        )

        red_locked = bool(getattr(self.state, "red_tab_enabled", False))
        if use_red_tab and not red_locked and not force_direct:
            print("[Support] HP heal skipped: warrior red_tab not locked.")
            return

        if use_red_tab and (red_locked or force_direct):
            used_mode = self.cast_recovery_sequence(key, cast_function)
            if skill is not None:
                try:
                    skill.last_cast_time = now
                except Exception:
                    pass
            if red_locked:
                print(f"[Support] HP heal complete. ({used_mode}, red_tab)")
            else:
                print(f"[Support] HP heal complete. ({used_mode}, red_tab forced)")
            return

        used_mode = self.cast_recovery_sequence(key, cast_function)

        if skill is not None:
            try:
                skill.last_cast_time = now
            except Exception:
                pass
        print(f"[Support] HP heal complete. ({used_mode})")

    def execute_party_mp_recovery(self, snapshot: dict, use_red_tab: bool = False, force_direct: bool = False):
        """
        파티원(격수) MP 회복 스킬 시전
        격수의 마력이 부족할 때 마나 회복 스킬을 발동합니다.
        """
        key, skill = self.resolve_recovery_key(getattr(self.state, "recovery_mp_spell", ""), default_key="2")
        now = time.time()
        self._last_mp_recover_time = now

        if skill is not None and hasattr(skill, "is_ready") and not skill.is_ready():
            return

        red_locked = bool(getattr(self.state, "red_tab_enabled", False))
        if use_red_tab and not red_locked and not force_direct:
            print("[Support] MP heal skipped: warrior red_tab not locked.")
            return

        cast_function = self.resolve_recovery_cast_function(
            getattr(self.state, "recovery_mp_spell", ""),
            default_func="SpellEnter",
        )
        used_mode = self.cast_recovery_sequence(key, cast_function)
        if skill is not None:
            try:
                skill.last_cast_time = now
            except Exception:
                pass
        if use_red_tab and (red_locked or force_direct):
            print(f"[Support] MP heal cast on warrior: key={key}, mp={int(snapshot.get('mp', 0) or 0)} ({used_mode}, red_tab)")
        else:
            print(f"[Support] MP heal cast on warrior: key={key}, mp={int(snapshot.get('mp', 0) or 0)} ({used_mode})")

    # ==========================================================
    # ==========================================================
    # ── 기타 통합 기능 (svc_logic v3_heal 등 공용)
    # ==========================================================
    def execute_v3_heal(self, skill):
        """
        초고속 자가 회복 (특정 로직에서 긴급할 때 사용)
        스킬키 -> Home -> Enter 를 사람처럼(humanized) 빠르게 누르는 동작입니다.
        """
        if skill.hotkey:
            self.caster._press_fast(skill.hotkey)
            humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
            self.caster._press_fast("home") # 2. 홈 키 (내 캐릭터 지정)
        humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
        
        self.caster._press_fast("enter") # 3. 엔터 (마법 발사)
        skill.last_cast_time = time.time() # 시전 시간 기록
