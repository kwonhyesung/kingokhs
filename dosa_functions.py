#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
 함수 모듈

"""

import time
from typing import Optional
from spell_casting import SpellCaster, target_mapper
from svc_kernel import tc, BisHardware


class DosaFunctions:
  
    
    def __init__(self, hardware: Optional[BisHardware] = None, state=None):
        self.spell_caster = SpellCaster(hardware)
        self.hardware = hardware
        self.state = state
        self.is_running = False
        self.is_heal_started = False
        
        # 스킬 번호 매핑 (spells_config.json과 통일)
        self.skill_mapping = {
            "힐": "c",         # spells_config.json 기준
            "혼": "m",         # 혼마
            "공증": "b",       # 공력증강
            "부활": "d",       # 부활
            "보호": "h",       # 보호
            "무장": "i",       # 무장
            "금강": "j",       # 금강
            "희원": "a",       # 희원
            "공주": "e",       # 공주
            "저주": "f",       # 저주
            "마비": "l",       # 마비
            "지진": "g",       # 지진
            "귀환": "q"        # 귀환
        }
        
        # 기준값 설정
        self.heal_threshold = 100000  # 희원기준체력
        self.self_heal_threshold = 30000  # 자힐기준체력
        self.mana_threshold = 30000  # 공증기준마력
        self.dosa_heal_limit = 300000  # 힐제한격수체력
    
    def send_key(self, key: str, delay: float = 0.1):
        """단일 키 전송"""
        if self.hardware:
            self.hardware.press_key(key)
            time.sleep(delay)
            self.hardware.release_key(key)
        else:
            import pyautogui
            pyautogui.press(key)
            time.sleep(delay)
    
    def send_esc(self):
        """ESC 키 전송"""
        self.send_key('esc', 0.1)
    
    def send_tab(self, count: int = 1):
        """Tab 키 전송"""
        for _ in range(count):
            self.send_key('tab', 0.09)
    
    def send_home_enter(self):
        """Home+Enter 조합"""
        self.send_key('home', 0.1)
        self.send_key('enter', 0.1)
    
    def check_execution_flags(self, *flags):
        """실행 플래그 확인 - 모든 플래그가 False가 될 때까지 대기"""
        # 실제 구현에서는 state 객체의 플래그들을 확인
        # 여기서는 간단한 시뮬레이션
        time.sleep(0.1)
    
    def heal_function(self, 격수체력: int = 0):
        """힐 함수 - 도사밀대V5.6.txt의 힐 라벨"""
        if not self.is_running:
            return
        
        # 힐 시작 시 한 번만 실행
        if not self.is_heal_started:
            self.send_escape()
            time.sleep(0.1)
            self.send_tab(2)  # 탭 2번
            self.is_heal_started = True
        
        # 격수 체력이 제한 이하일 때 힐
        if 격수체력 < self.dosa_heal_limit:
            self.spell_caster.spell_home_enter(self.skill_mapping["힐"])
        
        # 체력이 0이면 부활
        if 격수체력 == 0:
            time.sleep(0.1)
            self.spell_caster.cast_spell(self.skill_mapping["부활"])
        
        # 체력이 희원 기준 이하이면 희원
        if 격수체력 < self.heal_threshold:
            time.sleep(0.1)
            self.spell_caster.cast_spell(self.skill_mapping["희원"])
    
    def confusion_function(self):
        """혼 함수 - 도사밀대V5.6.txt의 혼 라벨"""
        if not self.is_running:
            return
        
        # 외부 명령 확인 (실제 구현에서는 state에서 확인)
        command = getattr(self.state, 'current_command', None)
        if command == "hon":
            self.check_execution_flags("혼_실행", "공증_실행", "유령_실행", "자힐_실행")
            
            self.send_escape()
            time.sleep(0.1)
            
            # 혼 입력 반복
            while command == "hon":
                self.spell_caster.cast_spell(self.skill_mapping["혼"])
                time.sleep(0.05)
                self.send_key('left', 0.1)
                time.sleep(0.1)
                self.send_key('enter', 0.05)
                time.sleep(0.05)
                
                # 명령 재확인
                command = getattr(self.state, 'current_command', None)
                if command == "":
                    self.is_heal_started = False
                    break
            
            self.is_heal_started = False
    
    def mana_boost_function(self, current_mp: int):
        """공증 함수 - 도사밀대V5.6.txt의 공증 라벨"""
        if current_mp < self.mana_threshold:
            # MP가 200 미만이면 아이템 사용
            if current_mp < 200:
                self.send_key('u', 0.2)
                time.sleep(0.2)
                
                # 랜덤 아이템 선택 (e, f, g, h)
                import random
                items = ['e', 'f', 'g', 'h']
                selected = random.choice(items)
                self.send_key(selected, 0.1)
                time.sleep(0.1)
            
            # 공력증강 스킬 사용
            self.spell_caster.cast_spell(self.skill_mapping["공증"])
            return True
        
        return False
    
    def ghost_function(self, current_hp: int):
        """유령 함수 - 도사밀대V5.6.txt의 유령 라벨"""
        if current_hp < 1:
            self.send_escape()
            time.sleep(0.1)
            self.spell_caster.cast_spell(self.skill_mapping["부활"])
            time.sleep(0.1)
            self.send_home_enter()
            time.sleep(0.1)
            
            # 힐 3번 사용
            for _ in range(3):
                self.spell_caster.spell_home_enter(self.skill_mapping["힐"])
                time.sleep(0.1)
                self.send_key('enter', 0.1)
                time.sleep(0.1)
            
            return True
        
        return False
    
    def self_heal_function(self, current_hp: int):
        """자힐 함수 - 도사밀대V5.6.txt의 자힐 라벨"""
        if not self.is_running:
            return
        
        if current_hp < self.self_heal_threshold:
            self.send_escape()
            time.sleep(0.03)
            self.spell_caster.cast_spell(self.skill_mapping["힐"])
            time.sleep(0.03)
            self.send_home_enter()
            time.sleep(0.1)
            self.send_key('enter', 0.1)
            
            # 힐 2번 추가 사용
            for _ in range(2):
                time.sleep(0.08)
                self.spell_caster.cast_spell(self.skill_mapping["힐"])
                time.sleep(0.1)
                self.send_key('enter', 0.1)
            
            self.is_heal_started = False
            return True
        
        return False
    
    def protection_function(self):
        """보무 함수 - 도사밀대V5.6.txt의 보무 라벨"""
        if not self.is_running:
            return
        
        # 보호와 무장 스킬 사용
        self.spell_caster.cast_spell(self.skill_mapping["보호"])
        time.sleep(0.1)
        self.spell_caster.cast_spell(self.skill_mapping["무장"])
        time.sleep(0.1)
    
    def geumgang_function(self):
        """금강 함수 - 도사밀대V5.6.txt의 금강 라벨"""
        # 금강 스킬 사용
        self.spell_caster.cast_spell(self.skill_mapping["금강"])
        time.sleep(0.1)
    
    def start_all_functions(self):
        """모든 함수 시작"""
        self.is_running = True
        self.is_heal_started = False
    
    def stop_all_functions(self):
        """모든 함수 정지"""
        self.is_running = False
        self.is_heal_started = False
    
    def get_skill_char(self, skill_name: str) -> str:
        """스킬 이름에 해당하는 문자 반환"""
        return self.skill_mapping.get(skill_name, "")


# 전역 인스턴스 생성 함수
def create_dosa_functions(hardware: Optional[DevInterface] = None, state=None) -> DosaFunctions:
    """도사 함수 인스턴스 생성"""
    return DosaFunctions(hardware, state)
