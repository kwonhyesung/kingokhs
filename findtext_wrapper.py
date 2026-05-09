"""
FindText 라이브러리 Python 포팅
ft.ahk의 핵심 기능을 Python으로 구현
"""
import cv2
import numpy as np
import subprocess
import json
import os
from typing import List, Dict, Any, Optional

VERBOSE_FINDTEXT_LOGS = os.environ.get("SVC_VERBOSE_LOGS", "0") == "1"
try:
    from ahk_engine import AHKEngine
except ImportError:
    AHKEngine = None


class FindText:
    """FindText 라이브러리의 Python 구현"""
    
    def __init__(self):
        self.cached_patterns: Dict[str, np.ndarray] = {}
        self.dll_engine = None
        
        # sys_core.dll이 존재하면 AHKEngine 사용 시도
        dll_path = os.path.join(os.path.dirname(__file__), "sys_core.dll")
        if AHKEngine and os.path.exists(dll_path):
            try:
                self.dll_engine = AHKEngine(dll_name=dll_path, ft_lib_path=os.path.join(os.path.dirname(__file__), "ft.ahk"))
                if VERBOSE_FINDTEXT_LOGS:
                    print("[FindText] AutoHotkey.dll 엔진 로드 성공.")
            except Exception as e:
                if VERBOSE_FINDTEXT_LOGS:
                    print(f"[FindText] DLL 엔진 로드 실패 (Python 엔진 사용): {e}")
        else:
            if VERBOSE_FINDTEXT_LOGS:
                print("[FindText] DLL 파일이 없거나 로드할 수 없어 Native Python 엔진을 사용합니다.")
    
    def capture_screen(self, x: int = 0, y: int = 0, w: int = 0, h: int = 0) -> np.ndarray:
        """
        화면 캡처 (AHK 스크립트 방식에서는 사용되지 않음)
        :param x, y: 시작 좌표 (0이면 전체 화면)
        :param w, h: 캡처 크기 (0이면 전체 화면)
        :return: 캡처된 이미지 (BGR)
        """
        # AHK 스크립트 방식에서는 사용되지 않음
        return np.zeros((100, 100, 3), dtype=np.uint8)
    
    def base64tobit(self, s: str) -> str:
        """
        base64 문자열을 비트 문자열로 변환 (ft.ahk 구현)
        :param s: base64 인코딩된 문자열
        :return: 0과 1로 구성된 비트 문자열
        """
        chars = "0123456789+/ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        char_to_bits = {}
        
        for i, c in enumerate(chars):
            bits = ((i >> 5) & 1)
            bits = str(bits) + str((i >> 4) & 1)
            bits = bits + str((i >> 3) & 1)
            bits = bits + str((i >> 2) & 1)
            bits = bits + str((i >> 1) & 1)
            bits = bits + str(i & 1)
            char_to_bits[c] = bits
        
        result = ""
        for c in s:
            if c in char_to_bits:
                result += char_to_bits[c]
        
        # 0과 1이 아닌 문자 제거
        import re
        result = re.sub(r'[^01]', '', result)
        # 끝의 "10*" 패턴 제거
        result = re.sub(r'10*$', '', result)
        
        return result
    
    def find_text(self, 
                  text: str,
                  x1: int = 0, y1: int = 0, 
                  x2: int = 0, y2: int = 0,
                  err1: float = 0.1, err0: float = 0.1,
                  screenshot: Optional[np.ndarray] = None,
                  find_all: bool = True) -> List[Dict[str, Any]]:
        """
        화면에서 텍스트 패턴 찾기
        """
        # DLL 엔진이 활성화되어 있고 screenshot이 없으면 (전체 화면 검색)
        if self.dll_engine and screenshot is None:
            return self.dll_engine.find_text(text, x1, y1, x2, y2, err1, err0, find_all)

        # 패턴 파싱 및 캐싱 (Native 엔진용)
        if text not in self.cached_patterns:
            parsed = self._parse_text_pattern(text)
            if not parsed: return []
            self.cached_patterns[text] = parsed
        
        patterns = self.cached_patterns[text]
        if not isinstance(patterns, list):
            patterns = [patterns]
            
        if screenshot is None:
            # 전체 화면 검색 (AHK 호출 방식 유지 가능하나 여기선 네이티브로 구현 권장)
            # 일단 ROI 매칭이 주 목적이므로 screenshot이 필수라고 가정
            return []
            
        results = []
        for p in patterns:
            res = self._template_match_native(screenshot, p, err1, err0, find_all)
            results.extend(res)
            if not find_all and results:
                break
                
        return results

    def _template_match_native(self, 
                             roi_bgr: np.ndarray,
                             p: Dict[str, Any],
                             err1: float = 0.1, err0: float = 0.1, 
                             find_all: bool = True) -> List[Dict[str, Any]]:
        """
        ft.ahk Native 방식의 템플릿 매칭 Python 완전 구현.
        - mode=1 (Color Mode): fg/bg 색상 직접 비교 (FF5757-323232 형식)
        - mode=2 (Gray Threshold): ft.ahk 가중치 그레이스케일 이진화 방식
        """
        mode = p.get('mode', 2)
        pattern_bin = p['bitmap'].astype(np.uint8)  # 0 또는 1
        ph, pw = pattern_bin.shape
        sh, sw = roi_bgr.shape[:2]

        if ph > sh or pw > sw:
            return []

        # ── 글자(1) / 배경(0) 픽셀 인덱스 ──
        y1_idx, x1_idx = np.where(pattern_bin == 1)
        y0_idx, x0_idx = np.where(pattern_bin == 0)
        len1, len0 = len(y1_idx), len(y0_idx)

        # ft.ahk 오차 한도 계산: (len * err) >> 10
        e1_max = max(0, (len1 * int(err1 * 1024)) >> 10)
        e0_max = max(0, (len0 * int(err0 * 1024)) >> 10)
        # len이 0이면 해당 체크 비활성화
        if e1_max >= len1: len1 = 0
        if e0_max >= len0: len0 = 0

        results = []

        if mode == 1:
            # ── Color Mode: 전경색(fg)·배경색(bg) 직접 비교 ──
            # ft.ahk mode=1: 비트맵 1 픽셀 → fg_color ± 허용오차, 0 픽셀 → bg_color ± 허용오차
            fg_color = p.get('fg_color')  # (R, G, B) or None
            bg_color = p.get('bg_color')  # (R, G, B) or None

            # fg/bg 색상이 없으면 gray threshold로 폴백
            if fg_color is None:
                mode = 2
            else:
                # ft.ahk 기본 허용 오차: err1/err0 로부터 각 채널 최대 편차 계산
                # 색상 오차 = max(R편차, G편차, B편차) 대신 각 채널 독립 비교
                # err1 허용 픽셀 수(전경), err0 허용 픽셀 수(배경)
                fg_r, fg_g, fg_b = fg_color
                screen_r = roi_bgr[:, :, 2].astype(np.int32)  # BGR -> R
                screen_g = roi_bgr[:, :, 1].astype(np.int32)
                screen_b = roi_bgr[:, :, 0].astype(np.int32)  # BGR -> B

                # 색상 편차 허용 범위 (각 채널 ±variation)
                color_var = 30  # 기본 ±30 허용 (ft.ahk 기본 오차 수준)

                # 각 픽셀이 전경색에 해당하는지 여부 맵
                fg_match_map = (
                    (np.abs(screen_r - fg_r) <= color_var) &
                    (np.abs(screen_g - fg_g) <= color_var) &
                    (np.abs(screen_b - fg_b) <= color_var)
                ).astype(np.uint8)

                if bg_color is not None:
                    bg_r, bg_g, bg_b = bg_color
                    bg_match_map = (
                        (np.abs(screen_r - bg_r) <= color_var) &
                        (np.abs(screen_g - bg_g) <= color_var) &
                        (np.abs(screen_b - bg_b) <= color_var)
                    ).astype(np.uint8)
                else:
                    bg_match_map = None

                for y in range(sh - ph + 1):
                    for x in range(sw - pw + 1):
                        # 비트맵 1 픽셀 → 전경색 일치 확인
                        if len1 > 0:
                            fg_crop = fg_match_map[y:y+ph, x:x+pw]
                            mismatch1 = int(np.sum(fg_crop[y1_idx, x1_idx] == 0))
                            if mismatch1 > e1_max:
                                continue
                        # 비트맵 0 픽셀 → 배경색 일치 확인
                        if len0 > 0 and bg_match_map is not None:
                            bg_crop = bg_match_map[y:y+ph, x:x+pw]
                            mismatch0 = int(np.sum(bg_crop[y0_idx, x0_idx] == 0))
                            if mismatch0 > e0_max:
                                continue
                        results.append({'x': x, 'y': y, 'w': pw, 'h': ph, 'id': p.get('comment', '')})
                        if not find_all:
                            return results
                return results

        # ── mode=2 Gray Threshold Mode (ft.ahk 기본) ──
        # ft.ahk: gray = R*38 + G*75 + B*15, screen_bin = (gray < (thr+1)<<7)
        thr = p.get('color', 127)
        c = (thr + 1) << 7
        gray = (roi_bgr[:,:,2].astype(np.uint32)*38
              + roi_bgr[:,:,1].astype(np.uint32)*75
              + roi_bgr[:,:,0].astype(np.uint32)*15)
        screen_bin = (gray < c).astype(np.uint8)

        for y in range(sh - ph + 1):
            for x in range(sw - pw + 1):
                crop = screen_bin[y:y+ph, x:x+pw]
                if len1 > 0 and np.sum(crop[y1_idx, x1_idx] == 0) > e1_max:
                    continue
                if len0 > 0 and np.sum(crop[y0_idx, x0_idx] == 1) > e0_max:
                    continue
                results.append({'x': x, 'y': y, 'w': pw, 'h': ph, 'id': p.get('comment', '')})
                if not find_all:
                    return results

        return results
    
    def _parse_text_pattern(self, text: str) -> Optional[Dict[str, Any]]:
        """
        FindText 형식의 텍스트 패턴 파싱
        형식: |<comment>*mode$width.base64data
        :param text: FindText 형식의 텍스트
        :return: {bitmap, width, height, mode, comment} 또는 None
        """
        import re
        
        try:
            # 파이프로 구분된 여러 패턴 처리
            if '|' in text and not text.startswith('|'):
                patterns = text.split('|')
                return [self._parse_single_pattern(p) for p in patterns if p]
            
            return self._parse_single_pattern(text)
        except Exception as e:
            print(f"Pattern parsing error: {e}")
            return None
    
    def _parse_single_pattern(self, text: str) -> Optional[Dict[str, Any]]:
        """단일 FindText 패턴 파싱"""
        import re
        
        text = text.strip()
        if not text:
            return None
        
        # 주석 추출 <comment>
        comment = ""
        comment_match = re.match(r'<([^>]*)>', text)
        if comment_match:
            comment = comment_match.group(1)
            text = text[comment_match.end():]
        
        # $로 분리
        if '$' not in text:
            return None
        
        parts = text.split('$', 1)
        color_part = parts[0]
        data_part = parts[1]
        
        # color_part에서 모드 결정
        if '##' in color_part:
            mode = 5
        elif '#' in color_part:
            mode = 4
        elif '**' in color_part:
            mode = 3
        elif '*' in color_part:
            mode = 2
        else:
            mode = 1
        
        # color 값 추출
        color_str = color_part.replace('*', '').replace('#', '').replace('@', '-').strip()
        
        # mode=1 (Color Mode): 'RRGGBB' 또는 'RRGGBB-RRGGBB' 형식
        fg_color = None
        bg_color = None
        color_value = 0
        
        if mode == 1:
            clean = color_str.replace('0x', '').replace('0X', '').strip()
            if '-' in clean:
                parts2 = clean.split('-', 1)
                fg_hex = parts2[0].strip()
                bg_hex = parts2[1].strip()
            else:
                fg_hex = clean.strip()
                bg_hex = None
            try:
                if len(fg_hex) == 6:
                    r = int(fg_hex[0:2], 16)
                    g = int(fg_hex[2:4], 16)
                    b = int(fg_hex[4:6], 16)
                    fg_color = (r, g, b)
            except Exception:
                fg_color = None
            try:
                if bg_hex and len(bg_hex) == 6:
                    r = int(bg_hex[0:2], 16)
                    g = int(bg_hex[2:4], 16)
                    b = int(bg_hex[4:6], 16)
                    bg_color = (r, g, b)
            except Exception:
                bg_color = None
        else:
            try:
                color_value = int(color_str) if color_str else 0
            except Exception:
                color_value = 0
        
        # data_part에서 width와 base64 추출
        dot_match = re.match(r'(\d+)\.([\w+/]+)', data_part)
        if not dot_match:
            return None
        
        width = int(dot_match.group(1))
        base64_data = dot_match.group(2)
        
        # base64를 비트 문자열로 변환
        bit_string = self.base64tobit(base64_data)
        
        # 높이 계산
        height = len(bit_string) // width
        if height == 0:
            return None
        
        # 비트맵 생성 (0 또는 1)
        bitmap = np.zeros((height, width), dtype=np.uint8)
        for i, bit in enumerate(bit_string):
            if i < width * height:
                y = i // width
                x = i % width
                # 1이면 흰색(255), 0이면 검은색(0)
                bitmap[y, x] = 255 if bit == '1' else 0
        
        return {
            'bitmap': bitmap,
            'width': width,
            'height': height,
            'mode': mode,
            'comment': comment,
            'color': color_value
        }
    
    def _template_match(self, 
                       screen: np.ndarray,
                       pattern_data: Any,
                       err1: float, err0: float,
                       find_all: bool) -> List[Dict[str, Any]]:
        """
        ft.ahk 방식의 템플릿 매칭 수행
        픽셀 단위 비교 및 오차 허용 계산
        """
        # pattern_data가 딕셔너리인지 확인
        if isinstance(pattern_data, dict):
            pattern = pattern_data['bitmap']
            mode = pattern_data.get('mode', 2)  # 기본 mode=2 (Gray Threshold)
            comment = pattern_data.get('comment', '')
        elif isinstance(pattern_data, list):
            # 여러 패턴인 경우 첫 번째만 사용
            if pattern_data and pattern_data[0]:
                pattern = pattern_data[0]['bitmap']
                mode = pattern_data[0].get('mode', 2)
                comment = pattern_data[0].get('comment', '')
            else:
                return []
        else:
            pattern = pattern_data
            mode = 2
            comment = ''
        
        if len(screen.shape) == 3:
            screen_gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
        else:
            screen_gray = screen
        
        # 패턴이 화면보다 크면 검색 불가 (리사이징하지 않음)
        ph, pw = pattern.shape
        sh, sw = screen_gray.shape
        
        if ph > sh or pw > sw:
            return []
        
        # mode=2 (Gray Threshold Mode): ft.ahk 방식으로 이진화
        if mode == 2:
            # ft.ahk의 임계값 계산: c=(c+1)<<7
            color = pattern_data.get('color', 0)
            threshold = (color + 1) << 7
            
            # ft.ahk의 그레이 변환 공식: R*38 + G*75 + B*15
            if len(screen.shape) == 3:
                # BGR 형식
                screen_gray_manual = screen[:, :, 2] * 38 + screen[:, :, 1] * 75 + screen[:, :, 0] * 15
            else:
                screen_gray_manual = screen_gray * 128  # 이미 그레이인 경우
            
            # 임계값으로 이진화
            screen_bin = (screen_gray_manual < threshold).astype(np.uint8)
            # 패턴도 0/1로 정규화
            pattern_bin = (pattern > 127).astype(np.uint8)
        else:
            # mode=1 (Color Mode) - 간단한 구현
            screen_bin = (screen_gray > 127).astype(np.uint8)
            pattern_bin = (pattern > 127).astype(np.uint8)
        
        # 패턴의 1 픽셀과 0 픽셀 위치 추출
        ones_y, ones_x = np.where(pattern_bin == 1)
        zeros_y, zeros_x = np.where(pattern_bin == 0)
        
        len1 = len(ones_y)
        len0 = len(zeros_y)
        
        # ft.ahk 방식의 오차 허용 계산: err1=(len1*err1)>>10
        # err1과 err0은 0~1024 범위의 정수
        # 예: err1=10이면 (len1*10)/1024 만큼의 오차 허용
        err1_count = int((len1 * err1) >> 10)
        err0_count = int((len0 * err0) >> 10)
        
        # 오차 허용이 픽셀 수보다 크면 해당 픽셀 수를 0으로 설정
        if err1_count >= len1:
            len1 = 0
        if err0_count >= len0:
            len0 = 0
        
        results = []
        
        # 슬라이딩 윈도우로 매칭
        for y in range(sh - ph + 1):
            for x in range(sw - pw + 1):
                e1 = err1_count
                e0 = err0_count
                
                # 1 픽셀 위치 비교
                for i in range(len1):
                    py, px = ones_y[i], ones_x[i]
                    if screen_bin[y + py, x + px] != 1:
                        e1 -= 1
                        if e1 < 0:
                            break
                
                if e1 < 0:
                    continue
                
                # 0 픽셀 위치 비교
                for i in range(len0):
                    py, px = zeros_y[i], zeros_x[i]
                    if screen_bin[y + py, x + px] != 0:
                        e0 -= 1
                        if e0 < 0:
                            break
                
                if e0 < 0:
                    continue
                
                # 매칭 성공
                results.append({
                    'x': x + pw // 2,
                    'y': y + ph // 2,
                    '1': x,
                    '2': y,
                    '3': pw,
                    '4': ph,
                    'id': comment
                })
                
                if not find_all:
                    return results
        
        return results
    
    def find_color(self,
                   color: int,
                   variation: int = 10,
                   x1: int = 0, y1: int = 0,
                   x2: int = 0, y2: int = 0,
                   screenshot: Optional[np.ndarray] = None) -> List[Dict[str, Any]]:
        """
        특정 색상 찾기
        :param color: 찾을 색상 (RGB)
        :param variation: 색상 변화 허용 범위
        :param x1, y1, x2, y2: 검색 영역
        :param screenshot: 캡처된 이미지
        :return: 찾은 위치 리스트
        """
        if screenshot is None:
            screen = self.capture_screen(x1, y1, x2-x1 if x2 else 0, y2-y1 if y2 else 0)
        else:
            screen = screenshot.copy()
        
        # BGR로 변환
        b = (color >> 16) & 0xFF
        g = (color >> 8) & 0xFF
        r = color & 0xFF
        
        lower = np.array([max(0, b-variation), max(0, g-variation), max(0, r-variation)])
        upper = np.array([min(255, b+variation), min(255, g+variation), min(255, r+variation)])
        
        mask = cv2.inRange(screen, lower, upper)
        locs = np.where(mask > 0)
        
        results = []
        for y, x in zip(*locs):
            results.append({
                'x': x,
                'y': y,
                '1': x,
                '2': y,
                '3': 1,
                '4': 1,
                'id': ''
            })
        
        return results
    
    def wait_text(self,
                 text: str,
                 timeout: float = 5.0,
                 appear: bool = True,
                 x1: int = 0, y1: int = 0,
                 x2: int = 0, y2: int = 0,
                 err1: float = 0.1, err0: float = 0.1) -> Optional[List[Dict[str, Any]]]:
        """
        텍스트가 나타나거나 사라질 때까지 대기
        :param text: 찾을 텍스트
        :param timeout: 타임아웃 (초)
        :param appear: 나타날 때까지 대기 (False면 사라질 때까지)
        :param x1, y1, x2, y2: 검색 영역
        :param err1, err0: 오차 허용율
        :return: 찾은 결과 또는 None
        """
        import time
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            result = self.find_text(text, x1, y1, x2, y2, err1, err0, find_all=False)
            
            if appear and result:
                return result
            elif not appear and not result:
                return [1]
            
            time.sleep(0.05)
        
        return None


# 전역 인스턴스
_find_text_instance = None

def get_findtext() -> FindText:
    """FindText 인스턴스 가져오기 (싱글톤)"""
    global _find_text_instance
    if _find_text_instance is None:
        _find_text_instance = FindText()
    return _find_text_instance
