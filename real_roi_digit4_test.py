#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import cv2
import numpy as np
import sys
import os

# 현재 디렉토리를 Python 경로에 추가
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from svc_monitor import PatternMatcher
try:
    from config import WIN_KEY
    from utils import find_game_window, grab_window_bg
except ImportError:
    print("config 또는 utils 모듈을 찾을 수 없습니다. 기본값으로 진행합니다.")
    WIN_KEY = "MapleStory"
    
    def find_game_window(window_title):
        """간단한 윈도우 찾기 함수"""
        import win32gui
        def enum_windows_callback(hwnd, windows):
            if win32gui.IsWindowVisible(hwnd):
                window_title = win32gui.GetWindowText(hwnd)
                if window_title and "MapleStory" in window_title:
                    windows.append((hwnd, window_title))
            return True
        
        windows = []
        win32gui.EnumWindows(enum_windows_callback, windows)
        return windows[0][0] if windows else None
    
    def grab_window_bg(hwnd):
        """간단한 화면 캡처 함수"""
        try:
            import win32gui
            import win32ui
            from ctypes import windll
            
            # 윈도우 좌표 가져오기
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width = right - left
            height = bottom - top
            
            # 화면 캡처
            hwnd_dc = win32gui.GetWindowDC(hwnd)
            mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            
            save_bitmap = win32ui.CreateBitmap()
            save_bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
            save_dc.SelectObject(save_bitmap)
            
            # 비트맵 저장
            result = windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)
            
            # 이미지로 변환
            bmpinfo = save_bitmap.GetInfo()
            bmpstr = save_bitmap.GetBitmapBits(True)
            
            img = np.frombuffer(bmpstr, dtype=np.uint8)
            img.shape = (bmpinfo['bmHeight'], bmpinfo['bmWidth'], 4)
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2RGB)
            
            win32gui.DeleteObject(save_bitmap.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)
            
            from PIL import Image
            return Image.fromarray(img)
        except Exception as e:
            print(f"화면 캡처 실패: {e}")
            return None

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

def test_real_roi_digit4():
    """실제 ROI 영역에서 숫자 4 템플릿 매칭 점수 테스트"""
    
    print("=" * 60)
    print("실제 ROI 영역 숫자 4 템플릿 매칭 점수 테스트")
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
                    'data': pattern_data["bitmap"],
                    'shape': pattern_data["bitmap"].shape
                })
                print(f"템플릿 {i+1}: {templates[-1]['shape']} - {template_str[:30]}...")
        except Exception as e:
            print(f"템플릿 {i+1} 디코딩 실패: {e}")
    
    if not templates:
        print("유효한 템플릿이 없습니다.")
        return
    
    # 실제 화면 캡처
    print("\n실제 화면 캡처 시도...")
    try:
        hwnd = find_game_window(WIN_KEY)
        if not hwnd:
            print("게임 윈도우를 찾을 수 없습니다.")
            return
        
        print(f"게임 윈도우 찾음: {hwnd}")
        full_img = grab_window_bg(hwnd)
        if full_img is None:
            print("화면 캡처 실패")
            return
        
        print(f"화면 캡처 성공: {full_img.size}")
        
        # exp 영역 크롭 (config.json 기준)
        exp_region = full_img.crop((1429, 965, 1646, 993))
        print(f"exp 영역 크롭: {exp_region.size}")
        
        # 그레이스케일 및 이진화
        exp_gray = cv2.cvtColor(np.array(exp_region), cv2.COLOR_RGB2GRAY)
        
        # 여러 이진화 임계값 테스트
        thresholds = [50, 100, 127, 150, 200]
        
        for threshold in thresholds:
            print(f"\n--- 이진화 임계값: {threshold} ---")
            _, exp_bin = cv2.threshold(exp_gray, threshold, 255, cv2.THRESH_BINARY)
            print(f"이진화된 exp 영역: {exp_bin.shape}")
            
            # 테스트 이미지 저장
            cv2.imwrite(f"exp_region_threshold_{threshold}.png", exp_bin)
            
            # 각 템플릿에 대해 매칭 테스트
            print("\n매칭 점수 결과:")
            print("-" * 40)
            
            for template in templates:
                print(f"\n템플릿 {template['index']+1} (크기: {template['shape']}):")
                
                try:
                    # 모든 점수로 매칭 테스트
                    all_matches = match_bitmap_simple(exp_bin, template['data'], sim0=0.0, sim1=0.0)
                    
                    if all_matches:
                        # 최고 점수 찾기
                        best_match = max(all_matches, key=lambda m: m[2])  # sim1 기준
                        x, y, sim1_val, sim0_val = best_match
                        print(f"  최고 점수: sim1={sim1_val:.3f}, sim0={sim0_val:.3f}")
                        print(f"  위치: ({x}, {y})")
                        
                        # 임계값 테스트
                        threshold_matches = match_bitmap_simple(exp_bin, template['data'], sim0=0.65, sim1=0.75)
                        if threshold_matches:
                            print(f"  ✅ 임계값 통과 (sim1>=0.75, sim0>=0.65)")
                            print(f"  통과한 매칭 수: {len(threshold_matches)}")
                            
                            # 통과한 매칭 중 최고 점수
                            best_threshold_match = max(threshold_matches, key=lambda m: m[2])
                            tx, ty, tsim1_val, tsim0_val = best_threshold_match
                            print(f"  임계값 통과 최고 점수: sim1={tsim1_val:.3f}, sim0={tsim0_val:.3f}")
                        else:
                            print(f"  ❌ 임계값 미통과")
                        
                    else:
                        print(f"  ❌ 매칭 실패")
                        
                except Exception as e:
                    print(f"  ❌ 테스트 실패: {e}")
        
    except Exception as e:
        print(f"테스트 실패: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "=" * 60)
    print("테스트 완료")
    print("=" * 60)

if __name__ == "__main__":
    test_real_roi_digit4()
