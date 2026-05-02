#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import cv2
import numpy as np
from PIL import ImageGrab
import sys
import os
import traceback

# 현재 디렉토리를 Python 경로에 추가
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    from svc_monitor import PatternMatcher
    from svc_findtext import match_bitmap
    from config import WIN_KEY
    from utils import find_game_window, grab_window_bg
    print("모듈 임포트 성공")
except ImportError as e:
    print(f"모듈 임포트 실패: {e}")
    traceback.print_exc()
    sys.exit(1)

def test_digit4_templates():
    """숫자 4의 모든 템플릿 매칭 점수를 테스트"""
    
    print("=" * 60)
    print("숫자 4 템플릿 매칭 점수 테스트")
    print("=" * 60)
    
    # PatternMatcher 초기화
    matcher = PatternMatcher("temple")
    
    # 숫자 4의 템플릿들
    digit_4_templates = matcher.AHK_PATTERNS.get("4", [])
    
    if isinstance(digit_4_templates, str):
        digit_4_templates = [digit_4_templates]
    
    print(f"숫자 4 템플릿 개수: {len(digit_4_templates)}")
    print()
    
    # 게임 윈도우 찾기
    hwnd = find_game_window(WIN_KEY)
    if not hwnd:
        print("게임 윈도우를 찾을 수 없습니다.")
        return
    
    print(f"게임 윈도우 찾음: {hwnd}")
    
    # 화면 캡처
    full_img = grab_window_bg(hwnd)
    if full_img is None:
        print("화면 캡처 실패")
        return
    
    print(f"화면 캡처 성공: {full_img.size}")
    
    # exp 영역 설정 (config.json에서 확인)
    exp_region = {
        "sx": 1429,
        "sy": 965,
        "dx": 1646,
        "dy": 993
    }
    
    print(f"exp 영역: sx={exp_region['sx']}, sy={exp_region['sy']}, dx={exp_region['dx']}, dy={exp_region['dy']}")
    
    # exp 영역 크롭
    crop_img = full_img.crop((
        exp_region["sx"],
        exp_region["sy"],
        exp_region["dx"],
        exp_region["dy"]
    ))
    
    print(f"크롭된 exp 영역: {crop_img.size}")
    
    # OpenCV 형식으로 변환
    crop_cv = cv2.cvtColor(np.array(crop_img), cv2.COLOR_RGB2BGR)
    
    print("\n" + "=" * 60)
    print("각 템플릿별 매칭 점수 결과")
    print("=" * 60)
    
    # 각 템플릿에 대해 매칭 테스트
    for i, template_str in enumerate(digit_4_templates):
        print(f"\n템플릿 {i+1}: {template_str[:50]}...")
        
        # AHK 패턴 디코딩
        try:
            pattern_data = matcher.decode_ahk_pattern(template_str)
            if pattern_data is None:
                print(f"  ❌ 패턴 디코딩 실패")
                continue
                
            template_img = pattern_data["data"]
            print(f"  템플릿 크기: {template_img.shape}")
            
            # 매칭 테스트
            matches = match_bitmap(crop_cv, template_img, sim0=0.65, sim1=0.75)
            
            if matches:
                print(f"  ✅ 매칭 성공!")
                print(f"  매칭된 위치 수: {len(matches)}")
                
                # 각 매칭 위치의 점수 표시
                for j, match in enumerate(matches):
                    x, y, sim1_val, sim0_val = match
                    print(f"    위치 {j+1}: ({x}, {y}) - sim1: {sim1_val:.3f}, sim0: {sim0_val:.3f}")
                    
                    # 최고 점수 저장
                    if j == 0 or sim1_val > max_sim1:
                        max_sim1 = sim1_val
                        max_sim0 = sim0_val
                        best_pos = (x, y)
                
                print(f"  최고 점수: sim1={max_sim1:.3f}, sim0={max_sim0:.3f} at {best_pos}")
                
            else:
                print(f"  ❌ 매칭 실패 (조건: sim1>=0.75, sim0>=0.65)")
                
                # 낮은 임계값으로 테스트하여 최고 점수 확인
                low_matches = match_bitmap(crop_cv, template_img, sim0=0.0, sim1=0.0)
                if low_matches:
                    best_match = max(low_matches, key=lambda m: m[2])  # sim1 기준 정렬
                    x, y, sim1_val, sim0_val = best_match
                    print(f"  📊 최고 점수 (임계값 무시): sim1={sim1_val:.3f}, sim0={sim0_val:.3f} at ({x}, {y})")
                else:
                    print(f"  📊 전혀 매칭되지 않음")
                    
        except Exception as e:
            print(f"  ❌ 테스트 실패: {e}")
    
    print("\n" + "=" * 60)
    print("테스트 완료")
    print("=" * 60)

if __name__ == "__main__":
    try:
        test_digit4_templates()
    except Exception as e:
        print(f"테스트 실행 중 오류 발생: {e}")
        traceback.print_exc()
