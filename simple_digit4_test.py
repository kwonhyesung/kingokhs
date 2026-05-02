#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import cv2
import numpy as np
import sys
import os

# 현재 디렉토리를 Python 경로에 추가
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from svc_monitor import PatternMatcher
from svc_findtext import match_bitmap
from config import WIN_KEY
from utils import find_game_window, grab_window_bg

def simple_digit4_test():
    """숫자 4 템플릿 매칭 점수 간단 테스트"""
    
    print("=" * 50)
    print("숫자 4 템플릿 매칭 점수 테스트")
    print("=" * 50)
    
    # PatternMatcher 초기화
    matcher = PatternMatcher("temple")
    
    # 숫자 4의 템플릿들
    digit_4_templates = matcher.AHK_PATTERNS.get("4", [])
    
    if isinstance(digit_4_templates, str):
        digit_4_templates = [digit_4_templates]
    
    print(f"숫자 4 템플릿 개수: {len(digit_4_templates)}")
    
    # 각 템플릿 디코딩
    templates = []
    for i, template_str in enumerate(digit_4_templates):
        try:
            pattern_data = matcher.decode_ahk_pattern(template_str)
            if pattern_data:
                templates.append({
                    'index': i,
                    'string': template_str[:50] + "...",
                    'data': pattern_data["data"],
                    'shape': pattern_data["data"].shape
                })
                print(f"템플릿 {i+1}: {templates[-1]['shape']}")
        except Exception as e:
            print(f"템플릿 {i+1} 디코딩 실패: {e}")
    
    if not templates:
        print("유효한 템플릿이 없습니다.")
        return
    
    # 테스트용 이미지 생성 (실제 화면 대신)
    print("\n테스트용 이미지 생성...")
    test_img = np.zeros((50, 200), dtype=np.uint8)
    
    # 간단한 숫자 4 모양 생성
    test_img[10:40, 20:30] = 255  # 세로선
    test_img[10:20, 20:50] = 255  # 위 가로선
    test_img[20:30, 30:50] = 255  # 중간 가로선
    
    print(f"테스트 이미지 크기: {test_img.shape}")
    
    # 각 템플릿에 대해 매칭 테스트
    print("\n" + "=" * 50)
    print("매칭 점수 결과")
    print("=" * 50)
    
    for template in templates:
        print(f"\n템플릿 {template['index']+1}:")
        print(f"  문자열: {template['string']}")
        print(f"  크기: {template['shape']}")
        
        try:
            # 매칭 테스트
            matches = match_bitmap(test_img, template['data'], sim0=0.0, sim1=0.0)
            
            if matches:
                # 최고 점수 찾기
                best_match = max(matches, key=lambda m: m[2])  # sim1 기준
                x, y, sim1_val, sim0_val = best_match
                print(f"  ✅ 매칭 성공!")
                print(f"  최고 점수: sim1={sim1_val:.3f}, sim0={sim0_val:.3f}")
                print(f"  위치: ({x}, {y})")
                
                # 임계값 테스트
                threshold_matches = match_bitmap(test_img, template['data'], sim0=0.65, sim1=0.75)
                if threshold_matches:
                    print(f"  ✅ 임계값 통과 (sim1>=0.75, sim0>=0.65)")
                else:
                    print(f"  ❌ 임계값 미통과")
                
            else:
                print(f"  ❌ 매칭 실패")
                
        except Exception as e:
            print(f"  ❌ 테스트 실패: {e}")
    
    print("\n" + "=" * 50)
    print("테스트 완료")
    print("=" * 50)

if __name__ == "__main__":
    simple_digit4_test()
