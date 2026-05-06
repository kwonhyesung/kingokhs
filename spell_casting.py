#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
스킬 캐스팅 함수 모듈
common_func_v1.ahk의 함수들을 파이썬으로 구현
"""

import time
from typing import Optional
from svc_kernel import tc, BisHardware


class SpellCaster:
    """스킬 캐스팅 클래스"""
    
    def __init__(self, hardware: Optional[BisHardware] = None):
        self.hardware = hardware
    
    def send_key_sequence(self, keys: list, delay: float = 0.1):
        """키 시퀀스 전송"""
        if self.hardware:
            for key in keys:
                if key.startswith('+'):
                    # Shift 키 조합
                    self.hardware.press_key('shift')
                    time.sleep(0.01)
                    self.hardware.press_key(key[1:])
                    time.sleep(delay)
                    self.hardware.release_key(key[1:])
                    self.hardware.release_key('shift')
                elif key.startswith('^'):
                    # Ctrl 키 조합
                    self.hardware.press_key('ctrl')
                    time.sleep(0.01)
                    self.hardware.press_key(key[1:])
                    time.sleep(delay)
                    self.hardware.release_key(key[1:])
                    self.hardware.release_key('ctrl')
                else:
                    # 일반 키
                    self.hardware.press_key(key)
                    time.sleep(delay)
                    self.hardware.release_key(key)
                time.sleep(0.02)
        else:
            # 소프트웨어 입력 (fallback)
            import pyautogui
            pyautogui.PAUSE = 0.02
            for key in keys:
                pyautogui.press(key)
                time.sleep(delay)
    
    def cast_spell(self, spell_char: str, delay: float = 0.1):
        """CastSpell: 가장 간단한 단일 키 입력"""
        if self.hardware:
            # 가장 간단한 키 입력만 사용
            self.hardware.press_key(spell_char.lower())
            time.sleep(0.05)
            self.hardware.release_key(spell_char.lower())
        else:
            import pyautogui
            pyautogui.keyDown(spell_char.lower())
            time.sleep(0.05)
            pyautogui.keyUp(spell_char.lower())
    
    def spell_home_enter(self, spell_char: str, delay: float = 0.1):
        """SpellHomeEnter: Shift+Z+문자+Home+Enter+ESC"""
        keys = ['+z', spell_char.lower(), 'home', 'enter', 'escape']
        self.send_key_sequence(keys, delay)
    
    def spell_click_enter(self, spell_char: str, delay: float = 0.1):
        """SpellClickEnter: 키 다운만으로 변경하여 하드웨어 부하 최소화"""
        if self.hardware:
            # 키 다운만 전송 (업 신호 없음)
            self.hardware.press_key(spell_char.lower())
            # 100ms 동안 키를 누르고 있음
            time.sleep(0.1)
            # 키 업 전송
            self.hardware.release_key(spell_char.lower())
        else:
            # 하드웨어 없을 경우 기존 방식
            import pyautogui
            pyautogui.keyDown(spell_char.lower())
            time.sleep(0.1)
            pyautogui.keyUp(spell_char.lower())
    
    def spell_arrow_enter(self, spell_char: str, arrow: str = "up", delay: float = 0.1):
        """SpellArrowEnter: Shift+Z+문자+방향키+Enter+ESC"""
        keys = ['+z', spell_char.lower(), arrow.lower(), 'enter', 'escape']
        self.send_key_sequence(keys, delay)
    
    def spell_enter(self, spell_char: str, delay: float = 0.1):
        """SpellEnter: Shift+Z+문자+Enter+ESC"""
        keys = ['+z', spell_char.lower(), 'enter', 'escape']
        self.send_key_sequence(keys, delay)
    
    def spell_text_enter(self, spell_char: str, text: str, delay: float = 0.1):
        """SpellTextEnter: Shift+Z+문자+텍스트+Enter+ESC"""
        keys = ['+z', spell_char.lower()]
        self.send_key_sequence(keys, delay)
        time.sleep(0.05)
        # 텍스트 입력
        if self.hardware:
            # 하드웨어 텍스트 입력
            for char in text:
                self.hardware.press_key(char)
                time.sleep(0.02)
                self.hardware.release_key(char)
        else:
            import pyautogui
            pyautogui.typewrite(text, interval=0.02)
        time.sleep(0.05)
        keys = ['enter', 'escape']
        self.send_key_sequence(keys, delay)
    
    def check_spell(self, spell_name: str, delay: float = 0.1):
        """CheckSpell: /딜 스킬명"""
        keys = ['/', '딜', 'space', spell_name, 'enter', 'escape']
        self.send_key_sequence(keys, delay)
    
    def cast_and_check_spell(self, spell_char: str, spell_name: str, delay: float = 0.1):
        """CastnCheckSpell: 스킬 캐스트 후 딜 확인"""
        self.cast_spell(spell_char, delay)
        time.sleep(0.5)  # 스킬 캐스트 대기
        self.check_spell(spell_name, delay)
    
    def use_item(self, item_char: str, delay: float = 0.01):
        """UseItem: u+문자"""
        keys = ['u', item_char.lower()]
        self.send_key_sequence(keys, delay)
    
    def control_item(self, item_char: str, delay: float = 0.01):
        """ControlItem: Ctrl+문자"""
        keys = ['^' + item_char.lower()]
        self.send_key_sequence(keys, delay)


class TargetTypeMapper:
    """타겟 타입과 캐스팅 함수 매핑"""
    
    def __init__(self):
        self.mapping = {
            "대상선택형": "spell_arrow_enter",
            "즉발형": "spell_enter", 
            "대상입력형": "spell_text_enter"
        }
    
    def get_cast_function(self, target_type: str) -> str:
        """타겟 타입에 맞는 캐스팅 함수 반환"""
        return self.mapping.get(target_type, "spell_arrow_enter")
    
    def validate_target_type(self, target_type: str) -> bool:
        """유효한 타겟 타입인지 확인"""
        return target_type in self.mapping


# 전역 인스턴스
target_mapper = TargetTypeMapper()


def get_spell_caster(hardware: Optional[BisHardware] = None) -> SpellCaster:
    """스킬 캐스터 인스턴스 생성"""
    return SpellCaster(hardware)
