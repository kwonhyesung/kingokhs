"""
FindText 라이브러리 Python 포팅
ft.ahk의 핵심 기능을 Python으로 구현 (mode 2: Gray Threshold만 지원 — 이 저장소의
모든 패턴 데이터가 mode 2이므로 다른 모드는 포팅하지 않음. 상세: FINDTEXT_PORT_DESIGN.md)
"""
import re
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

# ft.ahk 전용 base64 문자셋 (표준 base64와 순서가 다름)
AHK_CHARS = "0123456789+/ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


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
        result = re.sub(r'[^01]', '', result)
        # 끝의 "10*" 패턴 제거
        result = re.sub(r'10*$', '', result)

        return result

    def bit2base64(self, bits: str) -> str:
        """
        비트 문자열('0'/'1')을 ft.ahk base64 문자열로 인코딩 (base64tobit의 역함수).
        ft.ahk bit2base64()와 동일: 6의 배수로 맞추기 위해 "100000"에서 필요한
        만큼(1~6자)을 뒤에 붙인 뒤(정지비트) 6비트씩 끊어 문자로 변환.
        """
        bits = re.sub(r'[^01]', '', bits)
        pad = 6 - (len(bits) % 6)
        bits += "100000"[:pad]
        return "".join(AHK_CHARS[int(bits[i:i+6], 2)] for i in range(0, len(bits), 6))

    def auto_threshold(self, roi_bgr: np.ndarray) -> int:
        """
        ft.ahk 캡처 도구의 자동 임계값 탐지(반복 등분 평균법, ft.ahk:1936-1958)를 이식.
        """
        gray = roi_bgr[:,:,2].astype(np.int64)*38 + roi_bgr[:,:,1].astype(np.int64)*75 + roi_bgr[:,:,0].astype(np.int64)*15
        gs = (gray >> 7).clip(0, 255)
        hist = np.bincount(gs.ravel(), minlength=256)[:256].astype(np.int64)
        idx = np.arange(256, dtype=np.int64)

        IP0 = int(np.sum(idx * hist))
        IS0 = int(np.sum(hist))
        if IS0 == 0:
            return 127
        thr = IP0 // IS0
        for _ in range(20):
            last = thr
            IP1 = int(np.sum(idx[:last+1] * hist[:last+1]))
            IS1 = int(np.sum(hist[:last+1]))
            IP2, IS2 = IP0 - IP1, IS0 - IS1
            if IS1 != 0 and IS2 != 0:
                thr = int((IP1 / IS1 + IP2 / IS2) / 2)
            if thr == last:
                break
        return thr

    def auto_crop_margins(self, bits: str, w: int) -> tuple:
        """
        ft.ahk GetTextFromScreen()의 여백 자동 크롭 이식 (ft.ahk:1967-1976, cut=1 기본값).
        맨 위/아래에서 한 행 전체가 전부 '0'이거나 전부 '1'인 동안 계속 제거한다
        (배경만 있는 빈 줄이든, 전경으로 꽉 찬 줄이든 상관없이 — ft.ahk 원본과 동일).
        반환: (잘린 비트열, cut_up 행수, cut_down 행수)
        """
        cut_up = cut_down = 0
        while bits[:w] == "0" * w or bits[:w] == "1" * w:
            bits = bits[w:]
            cut_up += 1
        while bits[-w:] == "0" * w or bits[-w:] == "1" * w:
            bits = bits[:-w]
            cut_down += 1
        return bits, cut_up, cut_down

    def capture_pattern(self, roi_bgr: np.ndarray, threshold: Optional[int] = None,
                         comment: str = "", cut: bool = True) -> str:
        """
        화면 ROI(BGR 이미지)를 ft.ahk 호환 FindText 패턴 문자열로 변환.
        threshold가 None이면 auto_threshold()로 자동 산출 (ft.ahk 캡처 도구와 동일 동작).
        cut=True(기본값, ft.ahk의 GetTextFromScreen과 동일)면 위/아래 여백 행을
        자동으로 잘라낸다 — 완전히 잘려서 빈 문자열이 되면(예: ROI 전체가 단색) 크롭을
        포기하고 원본을 그대로 쓴다.
        결과 형식: "|<comment>*threshold$width.base64data"
        """
        h, w = roi_bgr.shape[:2]
        if w < 1 or h < 1:
            raise ValueError("ROI가 비어 있습니다")
        if threshold is None:
            threshold = self.auto_threshold(roi_bgr)

        gray = roi_bgr[:,:,2].astype(np.int64)*38 + roi_bgr[:,:,1].astype(np.int64)*75 + roi_bgr[:,:,0].astype(np.int64)*15
        gs = gray >> 7
        bits = "".join("1" if v <= threshold else "0" for v in gs.flatten())

        if cut:
            cropped, cut_up, cut_down = self.auto_crop_margins(bits, w)
            if cropped:
                bits = cropped

        return f"|<{comment}>*{threshold}${w}.{self.bit2base64(bits)}"

    def sample_ink_color(self, roi_bgr: np.ndarray, threshold: Optional[int] = None):
        """
        ROI에서 "잉크"(threshold 이하로 어두운) 픽셀들의 평균 RGB를 뽑아준다.
        색상 모드 패턴을 만들 때 기본 색상 후보를 자동 제안하는 용도.
        반환: (r, g, b)
        """
        if threshold is None:
            threshold = self.auto_threshold(roi_bgr)
        gray = roi_bgr[:,:,2].astype(np.int64)*38 + roi_bgr[:,:,1].astype(np.int64)*75 + roi_bgr[:,:,0].astype(np.int64)*15
        gs = gray >> 7
        mask = gs <= threshold
        if not np.any(mask):
            mask = np.ones(gs.shape, dtype=bool)
        b = int(roi_bgr[:, :, 0][mask].mean())
        g = int(roi_bgr[:, :, 1][mask].mean())
        r = int(roi_bgr[:, :, 2][mask].mean())
        return r, g, b

    def capture_pattern_color(self, roi_bgr: np.ndarray, colors: List[Any],
                               threshold: Optional[int] = None, comment: str = "",
                               cut: bool = True) -> str:
        """
        색상 모드 패턴 생성. 모양(shape)은 capture_pattern()과 동일하게 threshold
        이진화로 추출하고, 색상만 별도로 붙인다 — 같은 모양에 색상만 바꿔가며
        여러 패턴을 만들 수 있다 ("동일 패턴 + 다른 색상" 멀티서치용).

        colors: [(r,g,b,tol), ...] 또는 [("RRGGBB", tol), ...] 둘 다 허용.
                여러 개 넣으면 그중 하나라도(OR) 맞으면 매치.
        결과 형식: "|<comment>@RRGGBB~TOL,...$width.base64shape"
        """
        h, w = roi_bgr.shape[:2]
        if w < 1 or h < 1:
            raise ValueError("ROI가 비어 있습니다")
        if not colors:
            raise ValueError("colors가 비어 있습니다 (색상 후보 최소 1개 필요)")
        if threshold is None:
            threshold = self.auto_threshold(roi_bgr)

        gray = roi_bgr[:,:,2].astype(np.int64)*38 + roi_bgr[:,:,1].astype(np.int64)*75 + roi_bgr[:,:,0].astype(np.int64)*15
        gs = gray >> 7
        bits = "".join("1" if v <= threshold else "0" for v in gs.flatten())

        if cut:
            cropped, _, _ = self.auto_crop_margins(bits, w)
            if cropped:
                bits = cropped

        tokens = []
        for c in colors:
            if len(c) == 2:
                hexcode, tol = c
            else:
                r, g, b, tol = c
                hexcode = f"{r:02X}{g:02X}{b:02X}"
            tokens.append(f"{str(hexcode).upper().lstrip('#')}~{int(tol)}")

        return f"|<{comment}>@{','.join(tokens)}${w}.{self.bit2base64(bits)}"

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
        # gray는 화면만 보면 정해지는 값이라 패턴이 몇 개든 똑같다. 패턴마다
        # 다시 계산하면(예전 동작) 26패턴짜리 스캔에서 917x787 uint32 곱셈을
        # 26번 반복해서 사이클당 ~175ms를 그냥 버렸다. 여기서 한 번만 만들어
        # 넘긴다 - 패턴별 이진화(gray < 임계값)는 그대로 패턴마다 한다.
        gray = None
        for p in patterns:
            if p.get('mode') == 'color':
                res = self._template_match_color(screenshot, p, err1, find_all)
            else:
                if gray is None:
                    gray = self._to_gray(screenshot)
                res = self._template_match_native(screenshot, p, err1, err0, find_all,
                                                  gray=gray)
            results.extend(res)
            if not find_all and results:
                break

        return results

    @staticmethod
    def _to_gray(roi_bgr: np.ndarray) -> np.ndarray:
        """ft.ahk의 gray = R*38+G*75+B*15 (BGR 순서이므로 B=0, G=1, R=2)."""
        return (roi_bgr[:, :, 2].astype(np.uint32) * 38
                + roi_bgr[:, :, 1].astype(np.uint32) * 75
                + roi_bgr[:, :, 0].astype(np.uint32) * 15)

    def _template_match_native(self,
                             roi_bgr: np.ndarray,
                             p: Dict[str, Any],
                             err1: float = 0.1, err0: float = 0.1,
                             find_all: bool = True,
                             gray: Optional[np.ndarray] = None) -> List[Dict[str, Any]]:
        """
        ft.ahk Native 방식의 템플릿 매칭 (mode=2 Gray Threshold, 픽셀 단위 정확 매칭).

        ft.ahk와 동일한 알고리즘(오차 허용 err1/err0 기반 정확 매칭)이지만, 매칭 위치
        전체를 cv2.matchTemplate(TM_CCORR) 상관연산 한 번으로 벡터 계산한다 (Python
        이중 for문 대신). 자세한 유도는 FINDTEXT_PORT_DESIGN.md §3.4 참고.

        Args:
            roi_bgr: ROI 영역 (BGR)
            p: 패턴 딕셔너리 {'bitmap': np.ndarray, 'width': int, 'height': int, 'color': int}
            err1: 글자 픽셀 오차 허용율 (0.1 = 10%)
            err0: 배경 픽셀 오차 허용율 (0.1 = 10%)
            find_all: 모든 매칭 반환 여부

        Returns:
            매칭 결과 리스트 [{'x': int, 'y': int, 'w': int, 'h': int, 'id': str}, ...]
            x, y는 매칭된 좌상단 좌표(ROI 기준, ft.ahk의 1,2 필드와 동일)
        """
        thr = p.get('color', 127)
        c = (thr + 1) << 7  # ft.ahk: c=(c+1)<<7
        # gray는 패턴과 무관하므로 호출자가 프레임당 한 번 계산해서 넘겨줄 수
        # 있다(find_text가 그렇게 한다). 직접 호출하는 쪽(svc_monitor의 숫자
        # 인식)을 위해 없으면 여기서 만든다.
        if gray is None:
            gray = self._to_gray(roi_bgr)
        screen_bin = (gray < c).astype(np.float32)

        pattern_bin = (p['bitmap'] > 0).astype(np.float32)
        ph, pw = pattern_bin.shape
        sh, sw = screen_bin.shape
        if ph > sh or pw > sw:
            return []

        len1 = int(np.sum(pattern_bin))
        len0 = ph * pw - len1

        e1_max = (len1 * int(err1 * 1024)) >> 10
        e0_max = (len0 * int(err0 * 1024)) >> 10
        if e1_max >= len1:
            len1 = 0
        if e0_max >= len0:
            len0 = 0

        out_h, out_w = sh - ph + 1, sw - pw + 1
        ok = None

        # 상관값은 0/1 곱의 합이라 항상 정수이고(패턴 넓이는 수천 이하라
        # float32가 정확히 표현한다), 그래서 round해서 int32 배열을 새로
        # 만들 필요가 없다 - 0.5만 물려서 부동소수로 바로 비교하면 결과가
        # 같다. 917x787짜리 중간 배열 두 개가 통째로 사라진다.
        if len1 > 0:
            corr1 = cv2.matchTemplate(screen_bin, pattern_bin, cv2.TM_CCORR)
            ok = corr1 >= (len1 - e1_max - 0.5)

        if len0 > 0:
            # 앞의 검사에서 이미 후보가 하나도 안 남았으면 두 번째 상관연산은
            # 계산할 이유가 없다. 실제 스캔에서는 대부분의 패턴이 대부분의
            # 프레임에서 하나도 안 맞으므로 여기서 절반이 그냥 빠진다
            # (실측 19.8ms -> 8.7ms, 결과는 동일).
            if ok is None or ok.any():
                corr0 = cv2.matchTemplate(screen_bin, 1.0 - pattern_bin, cv2.TM_CCORR)
                ok0 = corr0 <= (e0_max + 0.5)
                ok = ok0 if ok is None else (ok & ok0)

        if ok is None:
            ok = np.ones((out_h, out_w), dtype=bool)

        comment = p.get('comment', '')
        if not find_all:
            ys, xs = np.where(ok)
            if len(ys) == 0:
                return []
            return [{'x': int(xs[0]), 'y': int(ys[0]), 'w': pw, 'h': ph, 'id': comment}]

        # 오차 허용(err1/err0) 때문에 진짜 매칭 한 개가 인접 픽셀 수백~수만
        # 개로 잡힌다. 예전엔 그걸 전부 dict으로 만들어 NMS(O(n^2))에 넘겼다 -
        # 실측 13,584히트에서 100ms, 33,578히트에서 446ms가 여기서 사라졌고,
        # 아이템 패턴 2개가 281ms를 먹던 원인이 이것이다([SentinelPerf]
        # item=281 party=345 monster=304).
        # 붙어있는 픽셀은 어차피 같은 매칭이므로 C쪽에서 한 덩어리로 묶어
        # 대표 한 점씩만 돌려준다(같은 일을 4.6ms에 한다). 떨어져 있는 개체는
        # 스프라이트 한 칸(48px) 이상 벌어져서 따로 남는다.
        n_labels, _, stats, _ = cv2.connectedComponentsWithStats(
            np.ascontiguousarray(ok).view(np.uint8), 8)
        return [{'x': int(stats[i, cv2.CC_STAT_LEFT]), 'y': int(stats[i, cv2.CC_STAT_TOP]),
                 'w': pw, 'h': ph, 'id': comment}
                for i in range(1, n_labels)]

    def _template_match_color(self, roi_bgr: np.ndarray, p: Dict[str, Any],
                               err1: float = 0.1, find_all: bool = True) -> List[Dict[str, Any]]:
        """
        색상 모드 매칭. p['bitmap']의 '1'(잉크) 위치들이 p['colors']에 나열된 색상
        중 하나라도(OR, 채널별 절대차 tolerance) 맞으면 그 위치는 매치로 카운트한다.
        배경(잉크가 아닌) 픽셀의 색은 검사하지 않는다 — 캐릭터/아이콘 뒤 배경 타일이
        달라도 매치되게 하려는 의도적 설계. err1은 mode2와 동일한 오차 허용
        방식(허용 가능한 미스매치 잉크 픽셀 개수)을 재사용한다.
        """
        pattern_bin = (p['bitmap'] > 0).astype(np.float32)
        ph, pw = pattern_bin.shape
        sh, sw = roi_bgr.shape[:2]
        if ph > sh or pw > sw:
            return []

        b = roi_bgr[:, :, 0].astype(np.int16)
        g = roi_bgr[:, :, 1].astype(np.int16)
        r = roi_bgr[:, :, 2].astype(np.int16)

        color_ok = np.zeros((sh, sw), dtype=bool)
        for cr, cg, cb, tol in p['colors']:
            color_ok |= ((np.abs(r - cr) <= tol) & (np.abs(g - cg) <= tol) & (np.abs(b - cb) <= tol))

        len1 = int(np.sum(pattern_bin))
        if len1 == 0:
            return []
        e1_max = (len1 * int(err1 * 1024)) >> 10

        corr1 = cv2.matchTemplate(color_ok.astype(np.float32), pattern_bin, cv2.TM_CCORR)
        mismatch1 = len1 - np.round(corr1).astype(np.int32)
        match_mask = mismatch1 <= e1_max

        ys, xs = np.where(match_mask)
        comment = p.get('comment', '')
        if not find_all:
            if len(ys) == 0:
                return []
            return [{'x': int(xs[0]), 'y': int(ys[0]), 'w': pw, 'h': ph, 'id': comment}]

        return [{'x': int(x), 'y': int(y), 'w': pw, 'h': ph, 'id': comment}
                for y, x in zip(ys, xs)]

    def _parse_text_pattern(self, text: str) -> Optional[Dict[str, Any]]:
        """
        FindText 형식의 텍스트 패턴 파싱
        형식: |<comment>*mode$width.base64data
        :param text: FindText 형식의 텍스트
        :return: {bitmap, width, height, mode, comment} 또는 None
        """
        try:
            # ft.ahk 관례상 패턴 컬렉션은 항상 선행 '|'로 시작한다
            # (Text:="|<0>...|<1>...") — AHK의 `Loop Parse, text, "|"`는 그
            # 선행 구분자가 만드는 빈 첫 토큰을 그냥 건너뛴다. 여기서도 동일하게
            # 선행 '|'를 제거하고 나서 분리해야, 단일 패턴('|<0>*117$...')의
            # comment/threshold가 올바르게 파싱된다.
            body = text[1:] if text.startswith('|') else text
            if '|' in body:
                patterns = [self._parse_single_pattern(p) for p in body.split('|') if p]
                return patterns if len(patterns) > 1 else (patterns[0] if patterns else None)

            return self._parse_single_pattern(body)
        except Exception as e:
            print(f"Pattern parsing error: {e}")
            return None
    
    def _parse_single_pattern(self, text: str) -> Optional[Dict[str, Any]]:
        """단일 FindText 패턴 파싱"""
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

        # 색상 모드 (이 엔진 자체 확장 문법, ft.ahk에는 없음 — FINDTEXT_PORT_DESIGN.md §9 참고)
        # 형식: @RRGGBB~TOL,RRGGBB2~TOL2$width.base64shape
        if color_part.startswith('@'):
            return self._parse_color_pattern(color_part, data_part, comment)

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

    def _parse_color_pattern(self, color_part: str, data_part: str,
                              comment: str) -> Optional[Dict[str, Any]]:
        """
        색상 모드 패턴 파싱: '@RRGGBB~TOL,RRGGBB2~TOL2$width.base64shape'
        모양(shape)은 흑백 모드와 동일한 0/1 비트맵이다(어떤 픽셀이 "잉크"인지).
        그 잉크 픽셀들의 실제 화면 RGB가 나열된 색상 후보 중 하나라도(OR) 허용오차
        내에 들면 매치. 같은 모양을 색상만 바꿔 여러 개 만들면 "동일 패턴 + 다른
        색상" 멀티서치가 된다 (find_text 텍스트에서 '|'로 이어붙이면 됨).
        """
        dot_match = re.match(r'(\d+)\.([\w+/]+)', data_part)
        if not dot_match:
            return None
        width = int(dot_match.group(1))
        bit_string = self.base64tobit(dot_match.group(2))
        height = len(bit_string) // width
        if width < 1 or height < 1:
            return None

        bitmap = np.zeros((height, width), dtype=np.uint8)
        for i, bit in enumerate(bit_string[:width * height]):
            bitmap[i // width, i % width] = 255 if bit == '1' else 0

        colors = []
        for entry in color_part[1:].split(','):
            entry = entry.strip()
            if not entry:
                continue
            hexcode, _, tol = entry.partition('~')
            hexcode = hexcode.strip().lstrip('#')
            tol = int(tol) if tol else 20
            r, g, b = int(hexcode[0:2], 16), int(hexcode[2:4], 16), int(hexcode[4:6], 16)
            colors.append((r, g, b, tol))
        if not colors:
            return None

        return {
            'bitmap': bitmap, 'width': width, 'height': height,
            'mode': 'color', 'colors': colors, 'comment': comment,
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
