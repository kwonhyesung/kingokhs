#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import cv2
import numpy as np
import sys
import os

# 현재 디렉토리를 Python 경로에 추가
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from svc_monitor import PatternMatcher

def match_bitmap_simple(screen_img, template_img, sim0=0.65, sim1=0.75):
    """간단한 매칭 함수"""
    th, tw = template_img.shape
    if th > screen_img.shape[0] or tw > screen_img.shape[1]:
        return []
    
    # OpenCV 매칭
    s1_v = cv2.matchTemplate(screen_img.astype(np.float32), template_img.astype(np.float32), cv2.TM_CCORR)
    s0_v = cv2.matchTemplate((1.0 - screen_img).astype(np.float32), (1 - template_img).astype(np.float32), cv2.TM_CCORR)
    
    sim1 = s1_v / np.sum(template_img)
    sim0 = s0_v / (th*tw - np.sum(template_img))
    
    # 임계값 필터
    mask = (sim1 >= sim1) & (sim0 >= sim0)
    matches = []
    
    if np.any(mask):
        positions = np.where(mask)
        for i in range(len(positions[0])):
            y, x = positions[0][i], positions[1][i]
            matches.append((x, y, sim1[y, x], sim0[y, x]))
    
    return matches

def test_digit4_scores():
    """숫자 4 템플릿 매칭 점수 테스트"""
    
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
    
    # 각 템플릿 디코딩
    templates = []
    for i, template_str in enumerate(digit_4_templates):
        try:
            pattern_data = matcher.decode_ahk_pattern(template_str)
            if pattern_data:
                templates.append({
                    'index': i,
                    'string': template_str[:50] + "...",
                    'data': pattern_data["bitmap"],  # 'data' -> 'bitmap'으로 수정
                    'shape': pattern_data["bitmap"].shape
                })
                print(f"템플릿 {i+1}: {templates[-1]['shape']} - {template_str[:30]}...")
        except Exception as e:
            print(f"템플릿 {i+1} 디코딩 실패: {e}")
    
    if not templates:
        print("유효한 템플릿이 없습니다.")
        return
    
    # 실제 화면 캡처 시도
    print("\n실제 화면 캡처 시도...")
    try:
        from utils import find_game_window, grab_window_bg
        from config import WIN_KEY
        
        hwnd = find_game_window(WIN_KEY)
        if hwnd:
            print(f"게임 윈도우 찾음: {hwnd}")
            full_img = grab_window_bg(hwnd)
            if full_img:
                print(f"화면 캡처 성공: {full_img.size}")
                
                # exp 영역 크롭 (config.json 기준)
                exp_region = full_img.crop((1429, 965, 1646, 993))
                print(f"exp 영역 크롭: {exp_region.size}")
                
                # 그레이스케일 및 이진화
                exp_gray = cv2.cvtColor(np.array(exp_region), cv2.COLOR_RGB2GRAY)
                _, exp_bin = cv2.threshold(exp_gray, 127, 255, cv2.THRESH_BINARY)
                
                print(f"이진화된 exp 영역: {exp_bin.shape}")
                test_img = exp_bin
                
                # 테스트 이미지 저장
                cv2.imwrite("exp_region_test.png", exp_bin)
                print("exp_region_test.png 저장 완료")
                
            else:
                print("화면 캡처 실패 - 테스트 이미지 사용")
                test_img = None
        else:
            print("게임 윈도우를 찾을 수 없음 - 테스트 이미지 사용")
            test_img = None
            
    except Exception as e:
        print(f"화면 캡처 실패: {e} - 테스트 이미지 사용")
        test_img = None
    
    # 테스트 이미지 생성 (실제 화면이 없을 경우)
    if test_img is None:
        print("\n테스트용 이미지 생성...")
        test_img = np.zeros((28, 217), dtype=np.uint8)
        
        # 숫자 4 모양 여러 개 생성
        # 첫 번째 4
        test_img[5:20, 10:15] = 255  # 세로선
        test_img[5:8, 10:25] = 255   # 위 가로선
        test_img[12:15, 15:25] = 255 # 중간 가로선
        
        # 두 번째 4 (약간 다른 모양)
        test_img[5:20, 40:45] = 255  # 세로선
        test_img[5:8, 40:55] = 255   # 위 가로선
        test_img[12:15, 45:55] = 255 # 중간 가로선
        
        print(f"테스트 이미지 크기: {test_img.shape}")
    
    # 각 템플릿에 대해 매칭 테스트
    print("\n" + "=" * 60)
    print("매칭 점수 결과")
    print("=" * 60)
    
    for template in templates:
        print(f"\n템플릿 {template['index']+1}:")
        print(f"  문자열: {template['string']}")
        print(f"  크기: {template['shape']}")
        
        try:
            # 모든 점수로 매칭 테스트
            all_matches = match_bitmap_simple(test_img, template['data'], sim0=0.0, sim1=0.0)
            
            if all_matches:
                # 최고 점수 찾기
                best_match = max(all_matches, key=lambda m: m[2])  # sim1 기준
                x, y, sim1_val, sim0_val = best_match
                print(f"  ✅ 매칭 성공!")
                print(f"  최고 점수: sim1={sim1_val:.3f}, sim0={sim0_val:.3f}")
                print(f"  위치: ({x}, {y})")
                
                # 임계값 테스트
                threshold_matches = match_bitmap_simple(test_img, template['data'], sim0=0.65, sim1=0.75)
                if threshold_matches:
                    print(f"  ✅ 임계값 통과 (sim1>=0.75, sim0>=0.65)")
                    print(f"  통과한 매칭 수: {len(threshold_matches)}")
                else:
                    print(f"  ❌ 임계값 미통과")
                
                # 다른 임계값 테스트
                low_matches = match_bitmap_simple(test_img, template['data'], sim0=0.6, sim1=0.7)
                if low_matches:
                    print(f"  ⚠️ 낮은 임계값 통과 (sim1>=0.7, sim0>=0.6)")
                
            else:
                print(f"  ❌ 매칭 실패")
                
        except Exception as e:
            print(f"  ❌ 테스트 실패: {e}")
            import traceback
            traceback.print_exc()
    
    print("\n" + "=" * 60)
    print("테스트 완료")
    print("=" * 60)

if __name__ == "__main__":
    test_digit4_scores()
