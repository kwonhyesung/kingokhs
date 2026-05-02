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
        ft.ahk Native 방식의 템플릿 매칭 (Python 포팅 - mode=2 Gray Threshold)
        
        Args:
            roi_bgr: ROI 영역 (BGR)
            p: 패턴 딕셔너리 {'bitmap': np.ndarray, 'width': int, 'height': int, 'color': int}
            err1: 글자 픽셀 오차 허용율 (0.1 = 10%)
            err0: 배경 픽셀 오차 허용율 (0.1 = 10%)
            find_all: 모든 매칭 반환 여부
        
        Returns:
            매칭 결과 리스트 [{'x': int, 'y': int, 'w': int, 'h': int, 'id': str}, ...]
        """
        # 1. ft.ahk 방식 이진화 (mode=2: Gray Threshold Mode)
        thr = p.get('color', 127)
        c = (thr + 1) << 7  # ft.ahk: c=(c+1)<<7
        # ft.ahk: Bmp[2+o]*38+Bmp[1+o]*75+Bmp[o]*15 < c
        # BGR 순서: B=0, G=1, R=2
        gray = roi_bgr[:,:,2].astype(np.uint32)*38 + roi_bgr[:,:,1].astype(np.uint32)*75 + roi_bgr[:,:,0].astype(np.uint32)*15
        screen_bin = (gray < c).astype(np.uint8)
        
        # PatternMatcher의 decode_ahk_pattern이 이미 0/1 비트맵을 반환하므로 추가 이진화 제거
        pattern_bin = p['bitmap'].astype(np.uint8)

        # 2. 매칭 좌표 추출 (ft.ahk 원래 방식: '1'이 글자 픽셀)
        y1_idx, x1_idx = np.where(pattern_bin == 1)  # 글자 픽셀 (1)
        y0_idx, x0_idx = np.where(pattern_bin == 0)  # 배경 픽셀 (0)

        len1, len0 = len(y1_idx), len(y0_idx)
        
        # ft.ahk: err1=(len1*err1)>>10, err0=(len0*err0)>>10
        e1_max = (len1 * int(err1 * 1024)) >> 10
        e0_max = (len0 * int(err0 * 1024)) >> 10
        
        # ft.ahk: if (err1>=len1) len1=0; if (err0>=len0) len0=0
        if e1_max >= len1:
            len1 = 0
        if e0_max >= len0:
            len0 = 0

        # 디버그: 비활성화 (CPU 부하 감소)
        # if not hasattr(self, '_debug_match_logged'):
        #     print(f"[FindText] _template_match_native: screen shape={screen_bin.shape}, pattern shape={pattern_bin.shape}, len1={len1}, len0={len0}, e1_max={e1_max}, e0_max={e0_max}")
        #     print(f"[FindText] screen_bin sum={np.sum(screen_bin)}, pattern_bin sum={np.sum(pattern_bin)}")
        #     print(f"[FindText] screen_bin sample: {screen_bin[0:5, 0:5]}")
        #     print(f"[FindText] pattern_bin sample: {pattern_bin[0:5, 0:5]}")
        #     self._debug_match_logged = True

        # 실시간 이진화 디버그 저장 (비활성화)
        # try:
        #     os.makedirs("ocr_debug", exist_ok=True)
        #     cv2.imwrite("ocr_debug/current_screen_bin.png", screen_bin * 255)
        #     print(f"[FindText] Saved screen_bin to ocr_debug/current_screen_bin.png")
        # except Exception as e:
        #     print(f"[FindText] Failed to save screen_bin: {e}")

        sh, sw = screen_bin.shape
        ph, pw = pattern_bin.shape
        
        if ph > sh or pw > sw:
            return []

        results = []
        min_mismatch1 = len1
        min_mismatch0 = len0

        for y in range(sh - ph + 1):
            for x in range(sw - pw + 1):
                crop = screen_bin[y:y+ph, x:x+pw]
                
                mismatch1 = 0
                if len1 > 0:
                    mismatch1 = np.sum(crop[y1_idx, x1_idx] == 0)
                    min_mismatch1 = min(min_mismatch1, mismatch1)
                    if mismatch1 > e1_max:
                        continue
                
                mismatch0 = 0
                if len0 > 0:
                    mismatch0 = np.sum(crop[y0_idx, x0_idx] == 1)
                    min_mismatch0 = min(min_mismatch0, mismatch0)
                    if mismatch0 > e0_max:
                        continue
                
                results.append({'x': x, 'y': y, 'w': pw, 'h': ph, 'id': p.get('comment', '')})
                if not find_all:
                    return results

        # 디버그: 최소 mismatch 로그
        if not hasattr(self, '_debug_min_mismatch_logged'):
            print(f"[FindText] Min mismatch: mismatch1={min_mismatch1}/{len1}, mismatch0={min_mismatch0}/{len0}, e1_max={e1_max}, e0_max={e0_max}, results={len(results)}")
            self._debug_min_mismatch_logged = True

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
        
        # 형식: *[color]$width.base64data
        # 예: *117$18.zzzzzzzVzzVzy0Dy0Dy0DkS3kS3kS3kS3kS3kS3kS3kS3kS3kS3kS3y0Dy0DzVzzVzzzzzzzzzzU
        # 여기서 *117은 color 부분, $ 뒤에 width.base64data
        
        # $로 분리
        if '$' not in text:
            return None
        
        parts = text.split('$', 1)
        color_part = parts[0]
        data_part = parts[1]
        
        # color_part에서 모드 결정 (ft.ahk PicInfo 함수 참조)
        # mode:=InStr(color,"##") ? 5 : InStr(color,"#") ? 4
        #   : InStr(color,"**") ? 3 : InStr(color,"*") ? 2 : 1
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
        
        # color 값 추출 (숫자 부분)
        # 예: *117 -> 117
        color_str = color_part.replace('*', '').replace('#', '').replace('@', '-')
        try:
            color_value = int(color_str) if color_str else 0
        except:
            color_value = 0
        
        # data_part에서 width와 base64 추출
        # 형식: width.base64data
        dot_match = re.match(r'(\d+)\.([\w+/]+)', data_part)
        if not dot_match:
            return None
        
        width = int(dot_match.group(1))
        base64_data = dot_match.group(2)
        
        # base64를 비트 문자열로 변환
        bit_string = self.base64tobit(base64_data)
        
        # 높이 계산
        height = len(bit_string) // width
        if len(bit_string) != width * height:
            print(f"Warning: bit string length {len(bit_string)} doesn't match width {width}")
        
        # 비트맵 생성
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
