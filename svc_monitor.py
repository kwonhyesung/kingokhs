# -*- coding: utf-8 -*-
"""
svc_monitor.py - SYSTEM V5 분석 엔진
?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═
PatternMatcher
  ??templates/{digits,monsters,status,users,items}/ ?서 메모??재
  ??recognize(crop_rgb, folder)  ???일 최고 매칭 ?름
  ??match_all_locations(...)     ??NMS ?용 ?중 ?치 반환 (?이?용)

ReaderThread
  ???? ?리?(hp_trig / mp_trig)
  ???자 ?드  (hp / mp / exp / money / x / y)  ??digits 폴더
  ??target_info                               ??monsters 폴더
  ??user_info                                 ??users 폴더
  ??stat_info                                 ??status 폴더
  ??items_scan (?션)                         ??items 폴더 + Grid ????
?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═
"""

import time
import threading
import os
import json
import random
import re
import cv2
import numpy as np
import win32gui

try:
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
except Exception:
    pass

from typing import Optional, Union, Any, List, Dict, Tuple
from svc_kernel import GameState, find_game_window, Region
from dataclasses import asdict
from svc_findtext import FindTextEngine

VERBOSE_MONITOR_LOGS = os.environ.get("SVC_VERBOSE_LOGS", "0") == "1"


def _monitor_log(*args, **kwargs):
    if VERBOSE_MONITOR_LOGS:
        print(*args, **kwargs)

def imread_unicode(path, flags=cv2.IMREAD_COLOR):
    """한글 경로 등 유니코드 경로를 포함한 이미지를 읽기 위한 헬퍼 함수"""
    try:
        return cv2.imdecode(np.fromfile(path, dtype=np.uint8), flags)
    except Exception as e:
        _monitor_log(f"[Error] imread_unicode failed for {path}: {e}")
        return None

def dict_to_region(data: dict) -> Region:
    """dict?Region 객체?변?하???퍼 ?수"""
    if isinstance(data, Region):
        return data
    if isinstance(data, dict):
        return Region(data["sx"], data["sy"], data["dx"], data["dy"])
    raise ValueError(f"Invalid data type for Region: {type(data)}")

def get_camera():
    """OBS 가??카메???덱???동 감? ??결"""
    _monitor_log("[Camera] Attempting to connect to camera...")
    for index in range(3):  # 0, 1, 2 ?서???도
        _monitor_log(f"[Camera] Trying index {index}...")
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        _monitor_log(f"[Camera] VideoCapture created for index {index}, isOpened={cap.isOpened()}")
        if cap.isOpened():
            # ?상??고정 ?정
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
            cap.set(cv2.CAP_PROP_FPS, 60)
            # Reduce capture latency (best-effort; backend may ignore this).
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            _monitor_log(f"[Camera] OBS 가??카메??연결 성공 (index: {index})")
            return cap, index
        cap.release()
    _monitor_log("[Camera] OBS 가??카메???결 실패")
    return None, -1



class CaptureSvc(threading.Thread):
    """
    OBS 가상 카메라로부터 프레임을 획득하여 GameState에 공유하는 전담 스레드.
    60 FPS 이상의 속도로 최신 화면을 유지하여 전체 시스템 지연을 최소화함.
    """
    def __init__(self, state: GameState):
        super().__init__(name="CaptureSvc", daemon=True)
        self.state = state
        self.cap = None
        self._running = True

    def run(self):
        _monitor_log("[Capture] Frame capture thread start (60+ FPS target)")
        while self._running and self.state.running:
            # 카메라 연결 확인
            if self.cap is None or not self.cap.isOpened():
                self.cap, _ = get_camera()
                if self.cap is None:
                    time.sleep(1.0)
                    continue
            
            # 버퍼 비우기 및 최신 프레임 획득
            ok = True
            for _ in range(3):
                if not self.cap.grab():
                    ok = False
                    break
            if not ok:
                try:
                    self.cap.release()
                except Exception:
                    pass
                self.cap = None
                continue
            
            ret, frame = self.cap.retrieve()
            if not ret or frame is None:
                self.cap.release()
                self.cap = None
                continue

            # BGR -> RGB 미리 변환 (다른 스레드 부하 경감)
            img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            
            # 전역 공유
            self.state.last_frame = img_rgb
            self.state.last_frame_time = time.time()
            
            # 약 100~120 FPS 제한으로 CPU 부하 조절
            time.sleep(0.015)

    def stop(self):
        self._running = False
        if self.cap:
            self.cap.release()

# ?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?╗
# ?? PatternMatcher  ??메모?캐싱 / NMS 기반 ?목???진   ??
# ?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?╝
class PatternMatcher:
    """
    templates/ ?위 폴더?__init__ ?서 모두 RAM ???재.
    ?행 ??일 I/O 이전 없음.

    폴더 구조:
        templates/
            digits/    0.png ~ 9.png  (?으?메모??동 ?성)
            monsters/  몬스?명.png
            status/    버프?이?png
            users/     ???레??png
            items/     ?이??png
    """

    # ?? ?계???????????????????????????????????????????????
    MATCH_THRESHOLD   = 0.75   # ?일 매칭 (monsters / items / status / users)
    NMS_OVERLAP       = 0.30   # NMS IOU (?일 매칭??
    DIGIT_THRESHOLD   = 0.30   # ?자 매칭 ?계?
    DIGIT_NMS_OVERLAP = 0.40   # ?자 NMS
    DIGIT_SCALE       = 1      # 1.0x (?플??일 배율 고정)
    DIGIT_NCC_WEIGHT  = 0.60   # ?합: ?규???? (질감 비중 ?향)
    DIGIT_XOR_WEIGHT  = 0.40   # ?합: 1 - (XOR?차/??)
    DIGIT_SCALE_TRY   = (0.90, 0.95, 1.0, 1.05, 1.10)

    FOLDERS = ("digits", "monsters", "status", "users", "items", "maps", "marker")
    TARGET_SIZE = (16, 20)  # (w, h) ?????상???해 ?장 (기존 10,12)
    # HSV ?상 기반 마스??추출 범위 (초록????스??- ?용??직접 분석)
    HSV_RANGES = [
        (np.array([30, 30, 50]), np.array([90, 200, 255])),  # 초록??(HP/MP/EXP) - H:30-90, S:30-200, V:50-255
        (np.array([10, 50, 150]), np.array([35, 255, 255])),  # ?금??(Money/X/Y)
    ]
    MARKER_LOWER_NEON_GREEN = np.array([50, 100, 150], dtype=np.uint8) # Deprecated
    MARKER_UPPER_NEON_GREEN = np.array([70, 255, 255], dtype=np.uint8) # Deprecated
    MARKER_THRESHOLD = 0.80  # XOR 매칭 점수 기준 복원

    # ── 템플릿별 개별 임계값 (기본 threshold 대신 이 값이 우선 적용됨) ──
    PER_TEMPLATE_THRESHOLD: dict = {
        "hobak": 0.98,     # 다중 감지 방지를 위해 0.95→0.98 증가
    }

    # AHK FindText 패턴 데이터 (0~9 숫자)
    AHK_PATTERNS = {
        "0": "|<0>*105$12.w7k1k1k13s3s3s3s3s3s3s3s3s3s3sk1k1U",
        "1": "|<1>*114$15.zzzkTy3z0Ts3z0TU3w0Ty3zkTy3zkTy3zkTy3zkTy3z01s0A07U0zzzU",
        "2": "|<2>*113$20.zzzzk7zw1zw07z01zk0Tky7wDVzzsTzy7zzVzzsTzsTzy7zzVzz1zzkTzk03w00w01z00Tzzzzzzy",
        "3": "|<3>*107$14.U0803z3zkzUzsDs3y0zs3y0zUDz3zks0C03U",
        "4": ["|<4>*46$11.zVz3s7kC0Q0s13260w1s000000000DsTkzVz2", "|<4-1>*116$13.zsTwDs7w3y1s0w0MAA60T0DU00000000003zVzkzsTw8", "|<4-2>*109$14.zsDy3y0zUDs3s0y0A631U1s0S000000000000Dy3zUzsDy2", "|<4-3>*74$14.zsDy3y0zUDs0s0C0A631U1s0S000000000000Dzzzzzzzzzzzzzzzw00U"],
        "5": "|<5>*126$11.00000s1k3bzDy1w3s1k3y7wDsT0y1kDUT3y7y",
        "6": "|<6>*113$15.zzzzzzkDy1y0zk7sDz1zsDz07s0wDVVwADVVwAC1VkAC1U0w07s3z0Tzzw",
        "7": "|<7>*118$15.zzz01s0A01U0A01zwDzVzkzy7y3zkTsDz1zsDw1zUDwDzVzwDzVzzzzU",
        "8": "|<8>*127$18.zzzy0Dy0DsD1sD1sD1sA1sA1y0Dy0Ds0zs0zVwDVwDVwDVwDVwDVkDVkDs0zs0zzzzU",
        "9": "|<9>*110$16.zzzw3zkDsA7UkS31Uw63kMD1Uw600M01s0zU3y0Dzkzz3y0zs3y0zs3zzzy"
    }

    # ----------------------------------------------------------
    def __init__(self, template_root: str, state: GameState = None):
        self.state = state
        self.bin_threshold = 128  # 이진화 임계값 (GUI 슬라이더로 조절 가능)
        # { folder: { name: ndarray(BGR) } }
        self.templates: dict[str, dict[str, np.ndarray]] = {f: {} for f in self.FOLDERS}
        self.shape_templates: dict[str, dict[str, dict]] = {f: {} for f in self.FOLDERS}
        self.digit_templates = {}  # {digit: {data: binary, pixels: int, ratio: float}}
        self.binary_templates: dict[str, dict[str, list[dict]]] = {f: {} for f in self.FOLDERS}
        self._last_debug_log_time = 0.0  # 디버그 로그 throttling 용
        
        # AHK 패턴 캐싱 (속도 최적화)
        self._ahk_pattern_cache = {}  # {digit: numpy array}
        for digit, ahk_pattern in self.AHK_PATTERNS.items():
            # 다중 템플릿 지원 - 리스트 형식 처리
            if isinstance(ahk_pattern, list):
                # 모든 템플릿을 디코딩하여 캐싱
                self._ahk_pattern_cache[digit] = [
                    self.decode_ahk_pattern(pattern) for pattern in ahk_pattern
                ]
            else:
                self._ahk_pattern_cache[digit] = self.decode_ahk_pattern(ahk_pattern)
        
        self._load_all(template_root)
        # self._load_digit_templates(template_root) # 이미지 기반 로딩 제거 (오직 AHK 패턴 문자열만 사용)

    # !!----------------------------------------------------------
    def reload(self, root: str):
        """기존 ?플릿을 비우??시 로드"""
        self.templates = {f: {} for f in self.FOLDERS}
        self.shape_templates = {f: {} for f in self.FOLDERS}
        self.digit_templates = {}
        self.binary_templates = {f: {} for f in self.FOLDERS}
        self._load_all(root)
        self._load_digit_templates(root)


    def decode_ahk_pattern(self, ahk_string: str) -> dict:
        """
        AHK FindText 패턴 문자열을 디코딩하여 임계값과 비트맵 배열 반환
        ft.ahk base64tobit와 동일한 방식 사용
        형식: |<숫자>*임계값$너비.데이터
        """
        try:
            # 1. 임계값 추출 (예: *117)
            star_idx = ahk_string.find('*')
            dollar_idx = ahk_string.find('$')
            threshold = 127 # 기본값
            if star_idx != -1 and dollar_idx != -1:
                threshold = int(ahk_string[star_idx+1:dollar_idx])
            
            # 2. 너비 및 데이터 추출
            width_part = ahk_string[dollar_idx + 1:]
            dot_idx = width_part.find('.')
            width = int(width_part[:dot_idx])
            data_str = width_part[dot_idx + 1:]
            
            # 3. 데이터 디코딩 (ft.ahk base64tobit와 동일)
            # 문자 순서: "0123456789+/ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            bits = ""
            chars = "0123456789+/ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            char_map = {c: i for i, c in enumerate(chars)}
            
            for char in data_str:
                if char == 'z': # z는 000000 (패딩/공백)
                    bits += "000000"
                else:
                    val = char_map.get(char, 0)
                    # MSB-first 비트 디코딩 (ft.ahk 방식)
                    bit_str = str((val >> 5) & 1)
                    bit_str += str((val >> 4) & 1)
                    bit_str += str((val >> 3) & 1)
                    bit_str += str((val >> 2) & 1)
                    bit_str += str((val >> 1) & 1)
                    bit_str += str(val & 1)
                    bits += bit_str
            
            # 끝의 "10*" 패턴 제거 (ft.ahk 방식)
            import re
            bits = re.sub(r'10*$', '', bits)
            
            # 비트 문자열을 2차원 배열로 변환
            height = len(bits) // width
            bitmap = []
            for i in range(height):
                row = bits[i*width:(i+1)*width]
                bitmap.append([int(c) for c in row])
            
            bitmap_arr = np.array(bitmap, dtype=np.uint8)
            
            # ft.ahk 원래 방식: 비트맵 반전 제거
            
            return {
                "threshold": threshold,
                "bitmap": bitmap_arr,
                "width": width
            }
        except Exception as e:
            _monitor_log(f"[AHK] 패턴 디코딩 실패: {e}")
            return {"threshold": 127, "bitmap": np.zeros((10, 10), dtype=np.uint8), "width": 10}

    @staticmethod
    def _zscore_2d(arr: np.ndarray) -> np.ndarray:
        """10×12 ???? ?치?float32 ?로?균·?위분산 근사."""
        a = arr.astype(np.float64).ravel()
        std = float(np.std(a))
        if std < 1e-6:
            return np.zeros_like(arr, dtype=np.float32)
        z = (arr.astype(np.float64) - np.mean(a)) / std
        return z.astype(np.float32)

    def _load_digit_templates(self, root: str):
        """
        digits/*.png 이미지들을 비트맵 패턴으로 로드
        """
        digits_path = os.path.join(root, "digits")
        if not os.path.isdir(digits_path): return

        for i in range(10):
            path = os.path.join(digits_path, f"{i}.png")
            if not os.path.exists(path): continue
            img = imread_unicode(path)
            if img is None: continue

            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
            for lower, upper in self.HSV_RANGES:
                mask = cv2.bitwise_or(mask, cv2.inRange(hsv, lower, upper))
            
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not contours: continue
            c = max(contours, key=cv2.contourArea)
            x, y, w, h = cv2.boundingRect(c)
            
            # 0/1 비트맵으로 저장 (임계값 정보와 함께)
            char = str(i)
            self._ahk_pattern_cache[char] = {
                "threshold": 127, # 이미지 기반은 이미 마스킹됨
                "bitmap": (mask[y:y+h, x:x+w] > 0).astype(np.uint8),
                "width": w
            }
            _monitor_log(f"[PM] Digit 비트맵 로드(Image): {char} ({w}x{h})")


        _monitor_log(f"[PM] Digit templates loaded: {list(self.digit_templates.keys())}")

    def _load_all(self, root: str):
        loaded_total = 0
        for folder in self.FOLDERS:
            folder_path = os.path.join(root, folder)
            if not os.path.isdir(folder_path):
                _monitor_log(f"[PM] {folder} 폴더 없음 - 건너?")
                continue
            loaded_in_folder = 0
            for fn in sorted(os.listdir(folder_path)):
                if not fn.lower().endswith((".png", ".jpg", ".bmp")):
                    continue
                img_path = os.path.join(folder_path, fn)
                if not os.path.exists(img_path):
                    continue
                img = imread_unicode(img_path)
                if img is not None:
                    name = os.path.splitext(fn)[0]
                    self.templates[folder][name] = img
                    # --- [Direction-Invariant XOR 매칭용 Binary 캐싱] ---
                    if folder in ["monsters", "users", "items", "maps", "marker"]:
                        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

                        # 표준 전처리: CLAHE → GaussianBlur → Threshold(bin_threshold)
                        # (AdaptiveThreshold 제거: 문맥 의존적이라 템플릿/소스 불일치 발생)
                        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
                        enhanced = clahe.apply(gray)
                        blurred = cv2.GaussianBlur(enhanced, (3, 3), 0)
                        _, binary = cv2.threshold(blurred, self.bin_threshold, 255, cv2.THRESH_BINARY)

                        # 비트율(1 픽셀 비율) 계산 - 사전 필터용
                        total_px = binary.shape[0] * binary.shape[1]
                        ones_px = cv2.countNonZero(binary)
                        bit_ratio = ones_px / float(max(1, total_px))

                        # monsters와 users는 파일명을 _ 기준으로 잘라 카테고리화
                        if folder in ["monsters", "users"]:
                            cat_name = name.split('_')[0]
                        else:
                            cat_name = name

                        if cat_name not in self.binary_templates[folder]:
                            self.binary_templates[folder][cat_name] = []

                        self.binary_templates[folder][cat_name].append({
                            "tmpl": binary,
                            "w": img.shape[1],
                            "h": img.shape[0],
                            "raw_name": name,
                            "bit_ratio": bit_ratio,
                        })
                    
                    loaded_in_folder += 1
                    loaded_total += 1
            if loaded_in_folder > 0:
                _monitor_log(f"[PM] {folder} folder: {loaded_in_folder} templates loaded.")

        counts = {f: len(v) for f, v in self.templates.items()}
        _monitor_log(f"[PM] PatternMatcher Load Complete: Total {loaded_total} templates. {counts}")

    # ----------------------------------------------------------
    # ?? 공개 ?터?이?????????????????????????????????????????
    # ----------------------------------------------------------
    def _match_binary_xor(self, play_area_rgb: np.ndarray, cx: int, cy: int, cw: int, ch: int, folder: str) -> tuple[str, float]:
        """
        중심점(cx, cy) 기반 XOR 픽셀 비교.
        유연한 크기 매칭(±2px) + Padding 보정 + SOTA 전처리 적용.
        """
        if play_area_rgb is None or play_area_rgb.size == 0 or folder not in self.binary_templates or not self.binary_templates[folder]:
            return "", 0.0

        full_h, full_w = play_area_rgb.shape[:2]

        best_name = ""
        best_score = 0.0
        best_diff_log = ""
        any_size_passed = False

        for cat_name, variants in self.binary_templates[folder].items():
            for v in variants:
                t_w, t_h = v["w"], v["h"]

                # 1. 유연한 크기 필터 (±2px 허용 + 20% 범위)
                size_close = abs(t_w - cw) <= 2 and abs(t_h - ch) <= 2
                size_in_range = (t_w * 0.8 <= cw <= t_w * 1.2 and t_h * 0.8 <= ch <= t_h * 1.2)
                if not (size_close or size_in_range):
                    continue

                any_size_passed = True

                # 2. 중심점 기준 Crop 추출 (템플릿 크기로 오려냄)
                x1 = max(0, cx - t_w // 2)
                y1 = max(0, cy - t_h // 2)
                x2 = x1 + t_w
                y2 = y1 + t_h

                # 화면 바깥 보정
                if x2 > full_w:
                    x2 = full_w
                    x1 = x2 - t_w
                if y2 > full_h:
                    y2 = full_h
                    y1 = y2 - t_h

                if x1 < 0 or y1 < 0:
                    continue

                target_crop = play_area_rgb[y1:y2, x1:x2]

                # 3. Padding 보정: crop이 템플릿 크기와 미세하게 다르면 패딩으로 맞춤
                crop_h, crop_w = target_crop.shape[:2]
                if crop_w != t_w or crop_h != t_h:
                    # 중심 기준으로 상하좌우 패딩 계산
                    pad_top = max(0, (t_h - crop_h) // 2)
                    pad_bottom = max(0, t_h - crop_h - pad_top)
                    pad_left = max(0, (t_w - crop_w) // 2)
                    pad_right = max(0, t_w - crop_w - pad_left)
                    if pad_top > 0 or pad_bottom > 0 or pad_left > 0 or pad_right > 0:
                        target_crop = cv2.copyMakeBorder(
                            target_crop, pad_top, pad_bottom, pad_left, pad_right,
                            cv2.BORDER_REPLICATE
                        )
                    # 패딩 후에도 크기가 다르면 INTER_NEAREST로 리사이즈
                    if target_crop.shape[0] != t_h or target_crop.shape[1] != t_w:
                        target_crop = cv2.resize(target_crop, (t_w, t_h), interpolation=cv2.INTER_NEAREST)

                # 4. 표준 전처리 (템플릿과 동일 파이프라인)
                gray = cv2.cvtColor(target_crop, cv2.COLOR_BGR2GRAY)
                clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
                enhanced = clahe.apply(gray)
                blurred = cv2.GaussianBlur(enhanced, (3, 3), 0)
                _, crop_bin = cv2.threshold(blurred, self.bin_threshold, 255, cv2.THRESH_BINARY)

                # 5. 1:1 픽셀 XOR 연산
                xor_diff = cv2.bitwise_xor(crop_bin, v["tmpl"])
                diff_count = cv2.countNonZero(xor_diff)
                total_pixels = t_w * t_h

                score = 1.0 - (diff_count / float(max(1, total_pixels)))
                if score > best_score:
                    best_score = score
                    best_name = cat_name
                    best_diff_log = (f"[DEBUG] {v['raw_name']}({folder}) "
                                  f"일치율: {score:.2f} (필요: 0.50) "
                                  f"덩어리 {cw}x{ch} | 템플릿 {t_w}x{t_h}")
                    # print(best_diff_log)  # 디버그 로그 출력 비활성화

        # 임계값: 0.50 이상 일치
        if best_score >= 0.70:
            return best_name, best_score

        # 디버그 로그: 사이즈 규격을 통과한 후보가 있었으나 점수 미달
        if any_size_passed and best_diff_log and "my_arrow" not in best_diff_log:
            self.state.last_vision_error = f"{best_diff_log}"
            import time
            now = time.time()
            if folder in ("items", "monsters") and best_score >= 0.30:
                if now - self._last_debug_log_time >= 5.0:
                    _monitor_log(f"{best_diff_log}")
                    self._last_debug_log_time = now

        return "", 0.0

    def findtext_scan(self, play_area_rgb: np.ndarray, folders: list[str] = None,
                      threshold: float = 0.90, stride: int = 2,
                      priority_grids: list[tuple[int, int]] = None) -> list[dict]:
        """
        FindText 방식 바이너리 패턴 매칭: 전체 화면 슬라이딩 XOR.
        AHK FindText와 동일 원리 - 색상 무시, 오직 모양만 비교.

        Args:
            play_area_rgb: RGB 이미지 (play_area 크롭)
            folders: 검색할 템플릿 폴더 목록 (기본: monsters, items)
            threshold: 일치율 임계값 (0.90 = 90% 일치)
            stride: 슬라이딩 간격 (2 = 2px마다 검사, 속도 4x 향상)
            priority_grids: 우선 검색할 Grid 좌표 리스트 [(gx, gy), ...]
        Returns:
            [{"name", "type", "x", "y", "w", "h", "score", "grid"}, ...]
        """
        if play_area_rgb is None or play_area_rgb.size == 0:
            return []

        if folders is None:
            folders = ["monsters", "items"]

        # stride 기본값 2, 템플릿 크기에 따라 동적 조정 (중복 감지 방지)
        dynamic_stride = stride

        # 1. play_area 전체를 바이너리화 (표준 전처리: 템플릿과 동일 파이프라인)
        # BGR 기준 통일 (입력이 이미 BGR이면 변환 불필요)
        if len(play_area_rgb.shape) == 3 and play_area_rgb.shape[2] == 3:
            gray = cv2.cvtColor(play_area_rgb, cv2.COLOR_BGR2GRAY)
        else:
            gray = cv2.cvtColor(play_area_rgb, cv2.COLOR_RGB2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        blurred = cv2.GaussianBlur(enhanced, (3, 3), 0)
        _, src_bin = cv2.threshold(blurred, self.bin_threshold, 255, cv2.THRESH_BINARY)

        src_h, src_w = src_bin.shape[:2]

        # 2. 검색 영역 결정: Grid 우선 → 전체
        search_regions = []  # [(x_start, y_start, x_end, y_end), ...]

        if priority_grids:
            map_data = self.state.maps_db.get(self.state.current_map, {})
            grid_size = map_data.get("grid_size", 48)
            for gx, gy in priority_grids:
                x_start = max(0, (gx - 1) * grid_size)
                y_start = max(0, (gy - 1) * grid_size)
                x_end = min(src_w, (gx + 2) * grid_size)
                y_end = min(src_h, (gy + 2) * grid_size)
                search_regions.append((x_start, y_start, x_end, y_end))

        # 전체 영역 추가
        search_regions.append((0, 0, src_w, src_h))

        # 3. 템플릿 순회하며 슬라이딩 XOR
        results = []
        matched_positions = set()  # 중복 매칭 방지

        for folder in folders:
            if folder not in self.binary_templates or not self.binary_templates[folder]:
                continue

            ent_type = {
                "monsters": "MONSTER", "items": "ITEM",
                "users": "USER", "marker": "ME"
            }.get(folder, "")

            for cat_name, variants in self.binary_templates[folder].items():
                for v in variants:
                    tmpl = v["tmpl"]
                    t_w, t_h = v["w"], v["h"]
                    tmpl_bit_ratio = v.get("bit_ratio", 0.5)

                    # 템플릿 크기에 따라 stride 동적 설정 (중복 감지 방지)
                    tmpl_stride = max(2, min(t_w, t_h) // 4)

                    if t_w > src_w or t_h > src_h:
                        continue

                    for (rx1, ry1, rx2, ry2) in search_regions:
                        sx_start = max(0, rx1)
                        sy_start = max(0, ry1)
                        sx_end = min(src_w - t_w, rx2 - t_w)
                        sy_end = min(src_h - t_h, ry2 - t_h)

                        if sx_start >= sx_end or sy_start >= sy_end:
                            continue

                        for y in range(sy_start, sy_end + 1, tmpl_stride):
                            for x in range(sx_start, sx_end + 1, tmpl_stride):
                                # 중복 매칭 방지: 템플릿 크기 단위로 그룹화
                                pos_key = (x // t_w, y // t_h, cat_name)
                                if pos_key in matched_positions:
                                    continue

                                crop = src_bin[y:y + t_h, x:x + t_w]

                                # 비트율 사전 필터: 1 비율이 크게 다르면 스킵
                                crop_ones = cv2.countNonZero(crop)
                                crop_total = t_w * t_h
                                crop_ratio = crop_ones / float(max(1, crop_total))
                                if abs(crop_ratio - tmpl_bit_ratio) > 0.15:
                                    continue

                                # XOR 연산
                                xor_diff = cv2.bitwise_xor(crop, tmpl)
                                diff_count = cv2.countNonZero(xor_diff)
                                total_pixels = t_w * t_h

                                score = 1.0 - (diff_count / float(max(1, total_pixels)))

                                # ── 템플릿별 개별 임계값 적용 (PER_TEMPLATE_THRESHOLD 우선) ──
                                effective_threshold = self.PER_TEMPLATE_THRESHOLD.get(
                                    cat_name, threshold)

                                # 디버그: threshold 미달이지만 일정 점수 이상이면 로그 (비활성화)
                                # if score >= 0.50 and score < effective_threshold:
                                #     if folder in ("items", "monsters"):
                                #         import time
                                #         now = time.time()
                                #         if now - self._last_debug_log_time >= 5.0:
                                #             print(f"[DEBUG] {cat_name}({folder}) "
                                #                   f"일치율: {score:.2f} (필요: {effective_threshold:.2f}) "
                                #                   f"→ 매칭 스킵")
                                #             self._last_debug_log_time = now

                                if score >= effective_threshold:
                                    cx = x + t_w // 2
                                    cy = y + t_h // 2
                                    # config.json의 play_area 사용 (캘리브레이션 기준)
                                    import json
                                    import os
                                    SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                                    config_file = os.path.join(SCRIPT_DIR, "config.json")
                                    pa_sx, pa_sy = 276, 32
                                    grid_size = 48.2  # float (누적오차 방지)
                                    if os.path.exists(config_file):
                                        try:
                                            with open(config_file, 'r', encoding='utf-8') as f:
                                                conf = json.load(f)
                                                pa = conf.get("play_area", {})
                                                pa_sx = pa.get("sx", 276)
                                                pa_sy = pa.get("sy", 32)
                                                grid_size = float(pa.get("grid_size", 48.2))
                                        except Exception:
                                            pass
                                    # crop 내부의 상대 좌표이므로 pa_sx를 뺄 필요 없음
                                    gx = int(cx / grid_size)
                                    gy = int(cy / grid_size)

                                    results.append({
                                        "name": cat_name,
                                        "type": ent_type,
                                        "x": int(x),
                                        "y": int(y),
                                        "cx": int(cx),
                                        "cy": int(cy),
                                        "w": t_w,
                                        "h": t_h,
                                        "score": score,
                                        "grid": (gx, gy),
                                    })
                                    matched_positions.add(pos_key)
                                    # break 제거: 동일 템플릿의 다른 위치도 계속 탐색
                            # else/continue/break 제거: 모든 위치 슬라이딩 계속

        # NMS (Non-Maximum Suppression) 적용
        if len(results) > 1:
            results = self._nms_results(results, overlap_thresh=0.3)

        return results

    @staticmethod
    def _nms_results(results: list[dict], overlap_thresh: float = 0.3) -> list[dict]:
        """결과에 NMS 적용하여 중복 제거"""
        if not results:
            return results

        boxes = []
        scores = []
        for r in results:
            boxes.append([r["x"], r["y"], r["w"], r["h"]])
            scores.append(r["score"])

        idxs = cv2.dnn.NMSBoxes(boxes, scores, 0.5, overlap_thresh)
        if len(idxs) == 0:
            return []
        return [results[i] for i in idxs.flatten()]

    def recognize(self, crop_rgb: np.ndarray, folder: str, field_name: str = "") -> str:
        """
        crop_rgb ?서 folder ???플릿과 가????맞는 ?름??반환.
        - "digits"  ???중 문자 ?자 문자??(NMS ?용)
        - ???    ???일 최고 ?수 ?플릿명
        ?계?미달 ?는 매칭 실패 ??"" 반환.
        """
        if crop_rgb is None or crop_rgb.size == 0:
            return ""
        # digits: digit_templates(HSV 로드)???어?인식 가?????digits 폴더 ?용
        if folder == "digits":
            # 기존 OCR 주석처리 - 비트맵 패턴 매칭 사용
            # if not self.digit_templates:
            #     return ""
            # # field_name???라 ?절??메서???출
            # if field_name in ("money", "x", "y"):
            #     text, _ = self._recognize_digits_scored_green2(crop_rgb, field_name)
            # elif field_name in ("hp", "mp", "exp"):
            #     text, _ = self._recognize_digits_scored_green4(crop_rgb, field_name)
            # else:
            #     text, _ = self._recognize_digits_scored_green2(crop_rgb, field_name)
            # return text
            
            # 비트맵 패턴 매칭 사용 (AHK FindText 기반)
            if field_name in ("hp", "mp", "exp", "money", "x", "y"):
                text, _ = self.recognize_digit_bitwise(crop_rgb)
                return text
            return ""
        if folder not in self.templates or not self.templates[folder]:
            return ""
        return self._recognize_single(crop_rgb, folder)
    
    def recognize_entity(self, play_area_rgb: np.ndarray, cx: int, cy: int, cw: int, ch: int,
                         whitelist: list | None = None) -> dict:
        """
        단일 Blob 후보에 대해 중심점(cx, cy) 기반으로 우선순위 탐색을 수행.
        순서: marker(ME) -> users(USER) -> monsters(MONSTER) -> items(ITEM)
        """
        if whitelist is None:
            whitelist = []

        # 1. ME (marker) -> XOR 매칭 기반 (threshold 복원)
        m_name, m_score = self._match_binary_xor(play_area_rgb, cx, cy, cw, ch, "marker")
        if m_name and m_score >= 0.85:
            return {"type": "ME", "name": m_name, "score": m_score, "is_whitelisted": False}

        # 2. USER (threshold 낮춰 인식률 향상)
        u_name, u_score = self._match_binary_xor(play_area_rgb, cx, cy, cw, ch, "users")
        if u_name and u_score >= 0.30:
            return {"type": "USER", "name": u_name,
                    "score": u_score, "is_whitelisted": (u_name in whitelist)}

        # 3. MONSTER (threshold 낮춰 인식률 향상)
        m_name, m_score = self._match_binary_xor(play_area_rgb, cx, cy, cw, ch, "monsters")
        if m_name and m_score >= 0.30:
            return {"type": "MONSTER", "name": m_name,
                    "score": m_score, "is_whitelisted": False}

        # 4. ITEM
        i_name, i_score = self._match_binary_xor(play_area_rgb, cx, cy, cw, ch, "items")
        if i_name and i_score >= 0.30:
            return {"type": "ITEM", "name": i_name,
                    "score": i_score, "is_whitelisted": False}

        return {"type": "", "name": "", "score": 0.0, "is_whitelisted": False}

    def recognize_entity_bitwise(self, crop_rgb: np.ndarray, folder: str,
                                  threshold: float = 0.80) -> tuple[str, float]:
        """
        초고속 비트맵 XOR 매칭 (RIMA, HOBAK 등 객체 탐지 전용).
        """
        if crop_rgb is None or crop_rgb.size == 0:
            return "", 0.0
        if folder not in self.binary_templates or not self.binary_templates[folder]:
            return "", 0.0

        if len(crop_rgb.shape) == 3 and crop_rgb.shape[2] == 3:
            gray = cv2.cvtColor(crop_rgb, cv2.COLOR_BGR2GRAY)
        else:
            gray = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2GRAY)
        
        _, crop_bin = cv2.threshold(gray, self.bin_threshold, 255, cv2.THRESH_BINARY)
        crop_h, crop_w = crop_bin.shape[:2]

        best_name, best_score = "", 0.0
        for cat_name, variants in self.binary_templates[folder].items():
            for v in variants:
                t_w, t_h = v["w"], v["h"]
                if not (t_w * 0.8 <= crop_w <= t_w * 1.2 and t_h * 0.8 <= crop_h <= t_h * 1.2): continue
                tmpl_bin = v["tmpl"]
                if tmpl_bin.shape[0] != crop_h or tmpl_bin.shape[1] != crop_w:
                    tmpl_bin = cv2.resize(tmpl_bin, (crop_w, crop_h), interpolation=cv2.INTER_NEAREST)
                xor_diff = cv2.bitwise_xor(crop_bin, tmpl_bin)
                score = 1.0 - (cv2.countNonZero(xor_diff) / float(max(1, crop_w * crop_h)))
                if score > best_score:
                    best_score, best_name = score, cat_name

        if best_score >= threshold:
            return best_name, best_score
        return "", 0.0

    def recognize_digit_bitwise(self, crop_rgb: np.ndarray) -> tuple[str, float]:
        """
        AHK FindText 방식의 임계값 기반 고정밀 비트맵 인식
        각 패턴별로 지정된 임계값(*117 등)을 사용하여 실시간 이진화 후 매칭
        """
        if crop_rgb is None or crop_rgb.size == 0:
            return "", 0.0

        try:
            # 1. 원본 그레이스케일 변환 (패턴별 개별 임계값 적용을 위해)
            gray = cv2.cvtColor(crop_rgb, cv2.COLOR_BGR2GRAY)
            h, w = gray.shape[:2]

            result_digits = []
            total_score = 0.0
            x = 0
            
            while x < w - 4:
                # 공백 확인 (글자 시작점 찾기 - 127 임계값 기준 대략적 확인)
                _, quick_bin = cv2.threshold(gray[:, x:min(x+2, w)], 127, 1, cv2.THRESH_BINARY)
                if np.sum(quick_bin) == 0:
                    x += 1
                    continue

                best_char, best_score, best_w = "", 0.0, 0
                
                # 등록된 모든 AHK 패턴 시도
                for char, data in self._ahk_pattern_cache.items():
                    thr = data["threshold"]
                    tmpl = data["bitmap"]
                    th, tw = tmpl.shape
                    if x + tw > w or h < th: continue
                    
                    # 2. 현재 패턴의 임계값으로 이진화 수행 (FindText 핵심 로직)
                    # 전체 영역이 아닌 필요한 부분만 이진화하여 속도 확보
                    _, crop_bin = cv2.threshold(gray[:, x:x+tw], thr, 1, cv2.THRESH_BINARY)
                    
                    # 3. 상하 2px 지터 매칭
                    for dy in range(max(1, h - th + 1)):
                        roi = crop_bin[dy:dy+th, :]
                        diff = np.count_nonzero(roi ^ tmpl)
                        score = 1.0 - (diff / (tw * th))
                        if score > best_score:
                            best_score, best_char, best_w = score, char, tw

                # 임계값: 0.90 이상이면 확실한 숫자로 간주 (글꼴이 동일하므로 매우 높게 설정)
                if best_score >= 0.85:
                    result_digits.append(best_char)
                    total_score += best_score
                    x += best_w
                else:
                    x += 1

            if result_digits:
                return "".join(result_digits), total_score / len(result_digits)
            return "", 0.0
        except Exception as e:
            _monitor_log(f"[Bitwise] 인식 실패: {e}")
            return "", 0.0

    def recognize_with_score(self, crop_rgb: np.ndarray, folder: str) -> tuple[str, float]:
        """
        crop_rgb 에서 folder 내 템플릿과 가장 잘 맞는 이름과 점수를 반환.
        Binary XOR 적용 (digits 등은 기존 방식).
        """
        if crop_rgb is None or crop_rgb.size == 0:
            return "", 0.0
        if folder == "digits":
            if not self.digit_templates:
                return "", 0.0
            return self._recognize_digits_scored(crop_rgb)
        if folder not in self.binary_templates or not self.binary_templates[folder]:
            return "", 0.0
            
        # 낱개 Crop(crop_rgb)의 경우 센터(cx, cy)와 크기(cw, ch)를 자기 자신 기준으로 설정하여 호출
        h, w = crop_rgb.shape[:2]
        cx, cy = w // 2, h // 2
        return self._match_binary_xor(crop_rgb, cx, cy, w, h, folder)

    def match_all_locations(self, crop_rgb: np.ndarray,
                            folder: str) -> list[dict]:
        """
        folder ??모든 ?플릿을 ?캔 ??NMS ?용 ??매칭??모든 ?치 반환.
        반환 ?식: [{"name": str, "x": int, "y": int, "w": int,
                      "h": int, "score": float}, ...]
        ?이?처???러 ?치가 모두 ?요?????용.
        """
        if crop_rgb is None or crop_rgb.size == 0:
            return []
        if folder not in self.templates or not self.templates[folder]:
            return []
        if folder == "marker":
            return self._recognize_marker(crop_rgb)

        # BGR 기준 통일 (입력이 이미 BGR이면 변환 불필요)
        # cv2.VideoCapture는 BGR을 반환하므로, crop_rgb가 실제로 BGR인 경우 변환 생략
        # 여기서는 호환성을 위해 RGB->BGR 변환 유지 (입력이 RGB인 경우)
        if len(crop_rgb.shape) == 3 and crop_rgb.shape[2] == 3:
            # RGB -> BGR 변환 (입력이 RGB인 경우)
            bgr = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2BGR)
        else:
            bgr = crop_rgb
        
        raw, boxes, scores = [], [], []

        for name, tmpl in self.templates[folder].items():
            th, tw = tmpl.shape[:2]
            ch, cw = bgr.shape[:2]
            if th > ch or tw > cw:
                continue
            res = cv2.matchTemplate(bgr, tmpl, cv2.TM_CCOEFF_NORMED)
            loc = np.where(res >= self.MATCH_THRESHOLD)
            for pt in zip(*loc[::-1]):           # (x, y)
                x, y = int(pt[0]), int(pt[1])
                score = float(res[y, x])
                raw.append({"name": name, "x": x, "y": y,
                             "w": tw, "h": th, "score": score})
                boxes.append([x, y, tw, th])
                scores.append(score)

        if not raw:
            return []

        idxs = cv2.dnn.NMSBoxes(boxes, scores,
                                  self.MATCH_THRESHOLD, self.NMS_OVERLAP)
        if len(idxs) == 0:
            return []
        return [raw[i] for i in idxs.flatten()]

    # (Deprecated) _marker_mask_from_rgb 및 _marker_mask_from_bgr 삭제됨

    def _recognize_marker(self, crop_rgb: np.ndarray) -> list[dict]:
        """
        marker 전용: 표준 전처리(CLAHE+Blur+Threshold128) 기반 TM_CCOEFF_NORMED 매칭.
        점수 0.80 이상인 위치만 반환.
        """
        if crop_rgb is None or crop_rgb.size == 0:
            return []

        # BGR 기준 통일 (입력이 이미 BGR이면 변환 불필요)
        if len(crop_rgb.shape) == 3 and crop_rgb.shape[2] == 3:
            gray = cv2.cvtColor(crop_rgb, cv2.COLOR_BGR2GRAY)
        else:
            gray = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        blurred = cv2.GaussianBlur(enhanced, (3, 3), 0)
        _, src_mask = cv2.threshold(blurred, self.bin_threshold, 255, cv2.THRESH_BINARY)
        
        raw, boxes, scores = [], [], []
        # GPS용 marker 템플릿의 경우 단일 카테고리(단일 폴더 파일들)로 묶임
        for cat_name, variants in self.binary_templates["marker"].items():
            for v in variants:
                tmpl_mask = v["tmpl"]
                th, tw = v["h"], v["w"]
                ch, cw = src_mask.shape[:2]
                if th > ch or tw > cw:
                    continue

                res = cv2.matchTemplate(src_mask, tmpl_mask, cv2.TM_CCOEFF_NORMED)
                # 0.80 이상의 픽셀 유사도를 가지는 포인트들을 획득
                loc = np.where(res >= 0.80)
                for pt in zip(*loc[::-1]):
                    x, y = int(pt[0]), int(pt[1])
                    score = float(res[y, x])
                    raw.append({"name": v["raw_name"], "x": x, "y": y,
                                "w": tw, "h": th, "score": score})
                    boxes.append([x, y, tw, th])
                    scores.append(score)

        if not raw:
            return []

        idxs = cv2.dnn.NMSBoxes(boxes, scores, 0.80, self.NMS_OVERLAP)
        if len(idxs) == 0:
            return []
        return [raw[i] for i in idxs.flatten()]

    def marker_best_score(self, crop_rgb: np.ndarray, marker_name: str = "my_arrow") -> float:
        """
        해당 마커의 최고 매칭 점수(TM_CCOEFF_NORMED)를 반환. (가장 클수록 좋음)
        매칭되는 마커가 없으면 0.0 반환.
        """
        hits = self._recognize_marker(crop_rgb)
        filtered = [h for h in hits if h["name"] == marker_name or h["name"].startswith(marker_name)]
        if not filtered:
            return 0.0
        
        best = max(filtered, key=lambda h: h["score"])
        return float(best["score"])

    # ----------------------------------------------------------
    # ?? ?? 구현 ?????????????????????????????????????????????
    # ----------------------------------------------------------
    def _recognize_single(self, crop_rgb: np.ndarray, folder: str) -> str:
        """폴더 내 모든 템플릿과 비교하여 최고 점수 1개 이름 반환. (XOR 매칭)"""
        h, w = crop_rgb.shape[:2]
        cx, cy = w // 2, h // 2
        name, _ = self._match_binary_xor(crop_rgb, cx, cy, w, h, folder)
        return name
    
    def _recognize_single_with_score(self, crop_rgb: np.ndarray, folder: str) -> tuple[str, float]:
        """폴더 내 모든 템플릿과 비교하여 최고 점수 1개 이름과 점수 반환. (XOR 매칭)"""
        h, w = crop_rgb.shape[:2]
        cx, cy = w // 2, h // 2
        return self._match_binary_xor(crop_rgb, cx, cy, w, h, folder)
    def _hsv_digit_mask_bgr(self, bgr: np.ndarray) -> np.ndarray:
        """digits ?플?로드(_load_digit_templates)? ?일??HSV 마스??"""
        if bgr is None or bgr.size == 0:
            return np.zeros((1, 1), dtype=np.uint8)
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        combined = np.zeros(hsv.shape[:2], dtype=np.uint8)
        for lower, upper in self.HSV_RANGES:
            combined = cv2.bitwise_or(combined, cv2.inRange(hsv, lower, upper))
        return combined

    # ?? SOTA ?처??틸리티 ?????????????????????????????????
    def _clahe_enhance(self, gray: np.ndarray) -> np.ndarray:
        """CLAHE(Contrast Limited AHE)??? ??극???"""
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(2, 2))
        return clahe.apply(gray)

    def _morpho_clean(self, binary: np.ndarray) -> np.ndarray:
        """모르?로지 Opening ?로 ?형 ???이??거."""
        h = binary.shape[0]
        ksize = max(1, min(2, h // 8))
        kernel = np.ones((ksize, ksize), np.uint8)
        return cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)

    def _cc_filter(self, binary: np.ndarray) -> np.ndarray:
        """
        강화??Connected-Component Analysis:
        면적, ?이뿐만 ?니??종횡?Aspect Ratio)?검?하???이??거.
        """
        h, w = binary.shape[:2]
        total = max(1, h * w)
        # 게임 ?태 지?용 ?자??보통 ?이가 ?체 ?롭??50% ?상??
        min_h = max(4, int(h * 0.45)) 
        min_area = max(4, total // 180)
        
        n, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
        out = np.zeros_like(binary)
        for i in range(1, n):
            cw = stats[i, cv2.CC_STAT_WIDTH]
            ch = stats[i, cv2.CC_STAT_HEIGHT]
            area = stats[i, cv2.CC_STAT_AREA]
            
            # 종횡?체크: ?자???로??직사각형 (보통 0.4 < w/h < 1.1)
            # 가로로 ?무 길거??바닥 ?이?, ?무 ??(?로 ?선) ??거
            aspect = cw / float(max(1, ch))
            if aspect > 1.3 or aspect < 0.15:
                continue
            
            # 면적 밀??Fullness) 체크: ?자???느 ?도 ?차있??
            fullness = area / float(max(1, cw * ch))
            if fullness < 0.25:
                continue

            if area >= min_area and ch >= min_h:
                out[labels == i] = 255
        return out

    def _border_clean(self, binary: np.ndarray, margin: int = 1) -> np.ndarray:
        """?롭 경계??붙? ?이??? ?거."""
        h, w = binary.shape[:2]
        if h <= 2 * margin or w <= 2 * margin:
            return binary
        result = binary.copy()
        result[:margin, :] = 0
        result[h - margin:, :] = 0
        result[:, :margin] = 0
        result[:, w - margin:] = 0
        return result

    def _post_clean(self, binary: np.ndarray) -> np.ndarray:
        """모르?로지 ??CC ?터 ??경계 ?리 ?합 ?이?라??"""
        bw = self._morpho_clean(binary)
        bw = self._cc_filter(bw)
        bw = self._border_clean(bw)
        return bw

    def _dark_bg_extract(self, bgr: np.ndarray) -> np.ndarray:
        """
        Phase 2 검??배경 ?거:
        Bilateral/Median 블러??이즈? 먼? 뭉갠 ?? HSV 기반 배경 분리.
        """
        # (1) ???이??거
        smooth = cv2.medianBlur(bgr, 3)
        hsv = cv2.cvtColor(smooth, cv2.COLOR_BGR2HSV)
        v_ch = hsv[:, :, 2]
        s_ch = hsv[:, :, 1]
        
        # (2) ?경 ?의: ?느 ?도 밝거??V>45), 채도가 ?거??S>30)
        # OBS 밝기 ??? 고려?여 V ?한???? ???되, 채도 결합?로 배경 ?스?배제
        mask_v = cv2.threshold(v_ch, 45, 255, cv2.THRESH_BINARY)[1]
        mask_s = cv2.threshold(s_ch, 30, 255, cv2.THRESH_BINARY)[1]
        mask_bright = cv2.threshold(v_ch, 95, 255, cv2.THRESH_BINARY)[1]
        
        # 탐색 글???금, 초록, ?랑) or 매우 밝? 탐색
        fg = cv2.bitwise_and(mask_v, cv2.bitwise_or(mask_s, mask_bright))
        
        # (3) ?태?적 모음 (?자 ?? 구멍 메우?
        kernel = np.ones((2, 1), np.uint8)
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, kernel)
        
        return fg

    def _preprocess_for_digits(self, img: np.ndarray) -> np.ndarray:
        """
        검??배경 기반 ?자 ?처?
        (1) 검?배??거(dark_bg_extract) ???심 (?이?최소)
        (2) HSV ?중범위 마스????보조
        (3) S+V 조합 ???백
        ?크 비율??가???절???보 ?택. ?력: BGR.
        """
        if img is None or img.size == 0:
            return np.zeros((1, 1), dtype=np.uint8)

        h, w = img.shape[:2]
        total = max(1, h * w)

        candidates: list[tuple[str, np.ndarray]] = []

        # (1) 검??배경 ?거 (?심 ???이?최소)
        dark_bg = self._dark_bg_extract(img)
        candidates.append(("dark_bg", dark_bg))

        # (2) HSV ?중 범위 마스??
        hsv_mask = self._hsv_digit_mask_bgr(img)
        candidates.append(("hsv", hsv_mask))

        # (3) S+V 조합 (채도 ?고 밝? ?역)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        sv = cv2.bitwise_and(
            cv2.threshold(hsv[:, :, 1], 30, 255, cv2.THRESH_BINARY)[1],
            cv2.threshold(hsv[:, :, 2], 45, 255, cv2.THRESH_BINARY)[1]
        )
        candidates.append(("sv", sv))

        # 최적 ?보 ?택: ?크 비율 + 컴포?트 ?렬??Y-Alignment)
        best_bw = candidates[0][1]
        best_score = -1.0

        for name, bw in candidates:
            # ?처??용 ????
            processed = self._post_clean(bw)
            ink = np.count_nonzero(processed)
            ratio = ink / total
            if ratio < 0.02 or ratio > 0.60:
                continue
            
            n_labels, _, stats, _ = cv2.connectedComponentsWithStats(processed, 8)
            if n_labels <= 1: continue
            
            # ?렬???수: ?효 컴포?트?이 가??직???사??Y)???는지 ?인
            y_centers = [stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT]/2.0 
                         for i in range(1, n_labels)]
            y_std = np.std(y_centers) if len(y_centers) > 1 else 10.0
            align_bonus = max(0, 1.0 - (y_std / (h/4.0))) # Y?차가 ???록 보너??
            
            # ?상??비율 ?수 + ?렬 보너??
            ratio_score = 1.0 - abs(ratio - 0.22)
            score = ratio_score * 0.4 + align_bonus * 0.6
            
            if score > best_score:
                best_score = score
                best_bw = processed

        # ?백: HSV Golden 범위 (??)
        if np.count_nonzero(best_bw) == 0:
            hsv2 = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            best_bw = cv2.inRange(hsv2, np.array([5, 30, 40]),
                                  np.array([35, 255, 255]))

        return best_bw

    @staticmethod
    def _pearson_ncc(a: np.ndarray, b: np.ndarray) -> float:
        a = a.astype(np.float64).ravel()
        b = b.astype(np.float64).ravel()
        if a.size != b.size:
            return 0.0
        a = a - np.mean(a)
        b = b - np.mean(b)
        den = np.linalg.norm(a) * np.linalg.norm(b)
        if den < 1e-9:
            return 0.0
        return float(np.clip(np.dot(a, b) / den, -1.0, 1.0))

    def _collect_preprocess_binaries(self, bgr: np.ndarray, crop_rgb: np.ndarray) -> list[tuple[str, np.ndarray]]:
        """
        ?상 기반 ?처??이?라??
        검?배??거 ??HSV 마스????S+V 조합 ??모르?로지+CC ?리.
        범용 Otsu/CLAHE/LAB???두??배경 ?스처? ?경?로 ?으므??거.
        """
        h, w = bgr.shape[:2]

        raw: list[tuple[str, np.ndarray]] = []

        # 1) 검??배경 ?거 (?심 ???이?최소)
        raw.append(("dark_bg", self._dark_bg_extract(bgr)))

        # 2) HSV ?중 범위 마스??(?플?로드? ?일)
        raw.append(("hsv", self._hsv_digit_mask_bgr(bgr)))

        # 3) SOTA preproc (dark_bg + hsv + sv 최적 ?택)
        raw.append(("preproc", self._preprocess_for_digits(bgr)))

        # 4) S+V 조합 (채도 ?고 ?둡지 ?? ?역)
        try:
            hsv_img = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
            sv = cv2.bitwise_and(
                cv2.threshold(hsv_img[:, :, 1], 30, 255, cv2.THRESH_BINARY)[1],
                cv2.threshold(hsv_img[:, :, 2], 45, 255, cv2.THRESH_BINARY)[1]
            )
            raw.append(("sv_combo", sv))
        except Exception:
            pass

        # 5) 좁? ?금???용 HSV (가????)
        try:
            hsv_narrow = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
            gold = cv2.inRange(hsv_narrow,
                               np.array([5, 50, 45]),
                               np.array([35, 255, 255]))
            raw.append(("gold", gold))
        except Exception:
            pass

        # === ?처? 모르?로지 + CC ?터 + 경계 ?리 ===
        processed: list[tuple[str, np.ndarray]] = []
        seen: set[int] = set()
        min_ink = max(3, (h * w) // 200)
        for name, bw in raw:
            cleaned = self._post_clean(bw)
            bw_hash = hash(cleaned.tobytes())
            if bw_hash in seen:
                continue
            if np.count_nonzero(cleaned) < min_ink:
                continue
            seen.add(bw_hash)
            processed.append((name, cleaned))

        # 최소 1?보장
        if not processed:
            for name, bw in raw:
                if np.count_nonzero(bw) > 0:
                    processed.append(("fallback", bw))
                    break
            else:
                processed.append(("fallback", raw[0][1]))

        return processed

    def _vertical_split_adaptive(self, binary: np.ndarray) -> list[tuple[int, int]]:
        h, w = binary.shape[:2]
        ink = (binary > 127).astype(np.uint8)
        col_counts = np.sum(ink, axis=0)
        if col_counts.size == 0 or col_counts.max() == 0:
            return []
        vmax = int(col_counts.max())
        gap_thr = max(1, min(int(0.08 * h), max(1, vmax // 8)))
        split_points: list[tuple[int, int]] = []
        in_digit = False
        start_x = 0
        for x, cnt in enumerate(col_counts):
            if cnt > gap_thr and not in_digit:
                in_digit = True
                start_x = x
            elif cnt <= gap_thr and in_digit:
                in_digit = False
                if x > start_x:
                    split_points.append((start_x, x))
        if in_digit:
            split_points.append((start_x, w))
        return split_points

    def _contour_split_ranges(self, binary: np.ndarray) -> list[tuple[int, int]]:
        h, w = binary.shape[:2]
        ink = ((binary > 127).astype(np.uint8) * 255).astype(np.uint8)
        contours, _ = cv2.findContours(ink, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        ranges: list[tuple[int, int]] = []
        min_area = max(4, (h * w) // 500)
        for c in contours:
            x, _y, cw, ch = cv2.boundingRect(c)
            if cw < 2 or ch < max(2, h // 12):
                continue
            if cv2.contourArea(c) < min_area:
                continue
            ranges.append((x, x + cw))
        ranges.sort(key=lambda t: t[0])
        return ranges

    def _split_variants(self, binary: np.ndarray) -> list[tuple[str, list[tuple[int, int]]]]:
        w = binary.shape[1]
        proj = self._vertical_split_adaptive(binary)
        cont = self._contour_split_ranges(binary)
        out: list[tuple[str, list[tuple[int, int]]]] = []
        if proj:
            out.append(("proj", proj))
        if cont and cont != proj:
            out.append(("contour", cont))
        if not out:
            out.append(("full", [(0, w)]))
        return out

    def _digit_fusion_score(self, bn01, tmpl, gz, grad_z):
        total = self.TARGET_SIZE[0] * self.TARGET_SIZE[1]
        diff = cv2.bitwise_xor(bn01, tmpl["data"])
        xor_err = int(cv2.countNonZero(diff))
        xor_sim = 1.0 - xor_err / float(total)
        ncc_g = self._pearson_ncc(gz, tmpl["gray_z"])
        ncc_r = self._pearson_ncc(grad_z, tmpl["grad_z"])
        ng = max(0.0, (ncc_g + 1.0) / 2.0)
        nr = max(0.0, (ncc_r + 1.0) / 2.0)
        ncc_mix = 0.65 * ng + 0.35 * nr
        fusion = self.DIGIT_XOR_WEIGHT * xor_sim + self.DIGIT_NCC_WEIGHT * ncc_mix
        return fusion, xor_err, ncc_g, ncc_r

    def _match_digit_at_scale(self, digit_bin, digit_gray, scale):
        hb, wb = digit_bin.shape[:2]
        if hb < 1 or wb < 1: return None, 0.0, {}
        nh, nw = max(4, int(round(hb * scale))), max(4, int(round(wb * scale)))
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        b_sc = cv2.resize(digit_bin, (nw, nh), interpolation=interp)
        g_sc = cv2.resize(digit_gray, (nw, nh), interpolation=interp)
        tw, th = self.TARGET_SIZE
        best_d, best_f, best_meta = None, -1.0, {}
        # 지?링: ?하좌우 1?????동?며 최적??탐색
        for dy in [-1, 0, 1]:
            for dx in [-1, 0, 1]:
                canvas_b = np.zeros((th + 2, tw + 2), dtype=np.uint8)
                canvas_g = np.zeros((th + 2, tw + 2), dtype=np.uint8)
                tmp_b = cv2.resize(b_sc, (tw, th), interpolation=cv2.INTER_NEAREST)
                tmp_g = cv2.resize(g_sc, (tw, th), interpolation=cv2.INTER_LINEAR)
                yoff, xoff = 1 + dy, 1 + dx
                canvas_b[yoff:yoff+th, xoff:xoff+tw] = tmp_b
                canvas_g[yoff:yoff+th, xoff:xoff+tw] = tmp_g
                win_b, win_g = canvas_b[1:1+th, 1:1+tw], canvas_g[1:1+th, 1:1+tw]
                _, bn = cv2.threshold(win_b, 127, 1, cv2.THRESH_BINARY)
                bn = bn.astype(np.uint8)
                gz = self._zscore_2d(win_g)
                gx = cv2.Sobel(win_g, cv2.CV_32F, 1, 0, ksize=3)
                gy = cv2.Sobel(win_g, cv2.CV_32F, 0, 1, ksize=3)
                mag = np.sqrt(gx*gx + gy*gy)
                grad_z = self._zscore_2d(mag)
                for digit, tmpl in self.digit_templates.items():
                    fus, xor_e, ng, nr = self._digit_fusion_score(bn, tmpl, gz, grad_z)
                    if fus > best_f:
                        best_f, best_d = fus, digit
                        best_meta = {"xor_err": xor_e, "ncc_g": ng, "ncc_r": nr, "scale": scale, "jitter": (dx, dy)}
        return best_d, best_f, best_meta

    def _threshold_for_digit(self, digit: int) -> float:
        t = 0.35
        if self.state is not None and getattr(self.state, "ocr_thresholds", None):
            t = float(self.state.ocr_thresholds.get(str(digit), 0.35))
        return max(0.05, min(0.99, t))

    def _decode_digit_string(
        self,
        binary: np.ndarray,
        gray: np.ndarray,
        split_ranges: list[tuple[int, int]],
        split_tag: str,
        pre_tag: str,
    ) -> tuple[str, float, list[float], list[dict], bool]:
        """?리통합·?????라미드. 반환: 문자?? min fusion, ?리 ?수, 메?, ???리 ?계 ?과 ??."""
        confs: list[float] = []
        metas: list[dict] = []
        chars: list[str] = []
        for start, end in split_ranges:
            if end <= start:
                continue
            d_bin = binary[:, start:end]
            d_gray = gray[:, start:end]
            if d_bin.size == 0:
                continue
            best_digit: int | None = None
            best_fusion = -1.0
            best_meta: dict = {}
            for sc in self.DIGIT_SCALE_TRY:
                bd, fus, meta = self._match_digit_at_scale(d_bin, d_gray, sc)
                if bd is not None and fus > best_fusion:
                    best_fusion = fus
                    best_digit = bd
                    best_meta = {**meta, "pre": pre_tag, "split": split_tag}
            if best_digit is None:
                continue
            thr = self._threshold_for_digit(int(best_digit))
            best_meta["thr"] = thr
            chars.append(str(best_digit))
            confs.append(best_fusion)
            metas.append(best_meta)
        if not chars:
            return "", 0.0, [], [], False
        min_f = min(confs)
        all_pass = all(
            confs[i] >= self._threshold_for_digit(int(chars[i]))
            for i in range(len(chars))
        )
        return "".join(chars), min_f, confs, metas, all_pass

    def _recognize_digits_scored_green2(self, crop_rgb: np.ndarray, field_name: str = "") -> tuple[str, float]:
        """
        초록????스??- HSV ?상 범위 ?닝 (money, x, y ?용):
        (1) HSV ?상 기반 마스??추출 (?금?만 탐색)
        (2) Contour 기반 개별 ?자 분할 ??렬
        (3) cv2.absdiff + cv2.countNonZero ?플?매칭
        반환: (?자 문자?? 최소 매칭 ?수; 실패 ??0.0).
        """
        if crop_rgb is None or crop_rgb.size == 0:
            return "", 0.0

        h, w = crop_rgb.shape[:2]

        # 1. HSV ?상 기반 마스??추출 (채널 ?서 명확?? RGB ??HSV)
        hsv = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2HSV)
        combined_mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
        # ?금?만 ?용 (money, x, y ?용)
        for lower, upper in self.HSV_RANGES:
            # ?금??범위??용 (??번째 범위)
            if lower[0] == 10:  # ?금???작 Hue
                mask = cv2.inRange(hsv, lower, upper)
                combined_mask = cv2.bitwise_or(combined_mask, mask)

        # 2. Contour 기반 개별 ?자 분할
        contours, _ = cv2.findContours(combined_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        # ?이??터?
        boxes = []
        min_area = max(15, (h * w) // 350)
        min_height = max(6, h // 3)
        
        for c in contours:
            area = cv2.contourArea(c)
            if area < min_area:
                continue
            
            x, y, cw, ch = cv2.boundingRect(c)
            if ch < min_height:
                continue
            
            boxes.append((x, y, cw, ch))
        
        # X 좌표 기? ?렬
        boxes.sort(key=lambda b: b[0])
        
        # 3. 개별 ?플?매칭 (cv2.absdiff + cv2.countNonZero)
        chars = []
        scores = []
        
        for x, y, cw, ch in boxes:
            # ?역 ?르?
            digit_crop = combined_mask[y:y+ch, x:x+cw]
            if digit_crop.size == 0:
                continue
            
            # ?규??
            tw, th = self.TARGET_SIZE
            resized = cv2.resize(digit_crop, (tw, th), interpolation=cv2.INTER_AREA)
            _, binary = cv2.threshold(resized, 1, 255, cv2.THRESH_BINARY)  # 0-255 범위??규??
            
            # ?플?매칭 (cv2.absdiff + cv2.countNonZero)
            best_digit = None
            best_diff = float('inf')
            
            for digit, tmpl in self.digit_templates.items():
                # ?플릿도 0-255 범위?변??
                tmpl_binary = (tmpl["data"] * 255).astype(np.uint8)
                
                # absdiff 매칭
                diff = cv2.absdiff(binary, tmpl_binary)
                diff_count = int(cv2.countNonZero(diff))
                
                if diff_count < best_diff:
                    best_diff = diff_count
                    best_digit = digit
            
            if best_digit is not None:
                chars.append(str(best_digit))
                # ?수 계산: 차이가 ?을?록 ?? ?수
                score = 1.0 - (best_diff / (tw * th))
                scores.append(score)
        
        # 4. ?스??결합
        if not chars:
            return "", 0.0
        
        text = "".join(chars)
        min_score = min(scores) if scores else 0.0
        
        # 로그 ?순?? ?식???자?출력
        # print(f"[OCR] {text}")
        
        return text, min_score

    def _recognize_digits_scored_green4(self, crop_rgb: np.ndarray, field_name: str = "") -> tuple[str, float]:
        """
        초록????스??- ?직 ?영 방식 (hp, mp, exp ?용):
        (1) HSV ?상 기반 마스??추출 (초록?만 탐색)
        (2) X??직 ?영 분할
        (3) ?플릿과 1:1 매칭
        반환: (?자 문자?? 최소 매칭 ?수; 실패 ??0.0).
        """
        if crop_rgb is None or crop_rgb.size == 0:
            return "", 0.0

        h, w = crop_rgb.shape[:2]

        # 1. HSV ?상 기반 마스??추출 (채널 ?서 명확?? RGB ??HSV)
        hsv = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2HSV)
        combined_mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
        # 초록?만 ?용 (hp, mp, exp ?용)
        for lower, upper in self.HSV_RANGES:
            # 초록??범위??용 (?번째 범위)
            if lower[0] == 30:  # 초록???작 Hue
                mask = cv2.inRange(hsv, lower, upper)
                combined_mask = cv2.bitwise_or(combined_mask, mask)

        # 2. X??직 ?영 분할
        # 가?방향(?1)?로 np.sum ?행
        projection = np.sum(combined_mask, axis=0)
        
        # sum 값이 0???닌 ?이 ?작?는 지?과 ?나??지?을 찾아???라?싱
        slices = []
        in_digit = False
        start_x = 0
        
        for x, val in enumerate(projection):
            if val > 0 and not in_digit:
                in_digit = True
                start_x = x
            elif val == 0 and in_digit:
                in_digit = False
                end_x = x
                slices.append((start_x, end_x))
        
        # 마???자가 ?까지 ?어지??경우
        if in_digit:
            slices.append((start_x, w))
        
        # 3. 개별 ?플?매칭
        chars = []
        scores = []
        
        for start_x, end_x in slices:
            cw = end_x - start_x
            
            # 가??비가 2px ?하???이?무시
            if cw <= 2:
                continue
            
            # ?역 ?르?
            digit_crop = combined_mask[:, start_x:end_x]
            if digit_crop.size == 0:
                continue
            
            # ?로 범위 찾기 (?공간 ?거)
            vertical_projection = np.sum(digit_crop, axis=1)
            non_zero_rows = np.where(vertical_projection > 0)[0]
            if len(non_zero_rows) == 0:
                continue
            top_y = non_zero_rows[0]
            bottom_y = non_zero_rows[-1] + 1
            digit_crop = digit_crop[top_y:bottom_y, :]
            
            # ?규??- ?플릿과 ?기 1:1 맞춤
            tw, th = self.TARGET_SIZE
            resized = cv2.resize(digit_crop, (tw, th), interpolation=cv2.INTER_NEAREST)
            _, binary = cv2.threshold(resized, 1, 255, cv2.THRESH_BINARY)
            
            # ?플?매칭 (cv2.absdiff + cv2.countNonZero)
            best_digit = None
            best_diff = float('inf')
            
            for digit, tmpl in self.digit_templates.items():
                # ?플릿도 0-255 범위?변??
                tmpl_binary = (tmpl["data"] * 255).astype(np.uint8)
                
                # absdiff 매칭
                diff = cv2.absdiff(binary, tmpl_binary)
                diff_count = int(cv2.countNonZero(diff))
                
                if diff_count < best_diff:
                    best_diff = diff_count
                    best_digit = digit
            
            if best_digit is not None:
                chars.append(str(best_digit))
                # ?수 계산: 차이가 ?을?록 ?? ?수
                score = 1.0 - (best_diff / (tw * th))
                scores.append(score)
        
        # 4. ?스??결합
        if not chars:
            return "", 0.0
        
        text = "".join(chars)
        min_score = min(scores) if scores else 0.0
        
        # 로그 ?순?? ?식???자?출력
        # print(f"[OCR] {text}")
        
        return text, min_score

    def _recognize_digits(self, crop_rgb: np.ndarray) -> str:
        """환경 래퍼."""
        s, _ = self._recognize_digits_scored(crop_rgb)
        return s


# ============================================================
#  EntityTracker  —  Object_ID 기반 실시간 추적
# ============================================================
class EntityTracker:
    """
    매 프레임 Blob 목록을 입력받아 Object_ID를 부여하고
    이전 프레임과 유클리드 거리를 비교해 같은 개체를 추적한다.

    Rules:
        - 거리 < MATCH_DIST(80px) 이면 이전 프레임 같은 개체로 판단
        - EXPIRE_SEC(1.5초) 동안 갱신없으면 제거
        - 새 개체에 next_id 할당
    """
    MATCH_DIST = 80    # 동일 개체로 판단할 최대 취소 거리 (px)
    EXPIRE_SEC = 1.5   # 이 시간동안 발견되지 않으면 제거

    def __init__(self, state):
        self.state = state
        self.next_id: int = 1
        self.tracked: dict = {}  # {id: {cx, cy, type, name, last_seen, prev_cx, prev_cy, direction_changes, ...}}
        self.coord_print_count = {}  # {id: 출력 횟수}
        self.last_grid = {}  # {id: 이전 grid 좌표 (gx, gy)}
        self.entity_printed = {}  # {id: New Entity 메시지 출력 여부}
        # Grid ↔ POS 변환 관계 (J10 = Grid(9,9) = POS(9,33))
        self.grid_to_pos_offset = (0, 24)  # POS = Grid + offset

    def grid_to_pos(self, grid_x, grid_y):
        """Grid 좌표를 POS 좌표로 변환"""
        pos_x = grid_x + self.grid_to_pos_offset[0]
        pos_y = grid_y + self.grid_to_pos_offset[1]
        return (pos_x, pos_y)

    def pos_to_grid(self, pos_x, pos_y):
        """POS 좌표를 Grid 좌표로 변환"""
        grid_x = pos_x - self.grid_to_pos_offset[0]
        grid_y = pos_y - self.grid_to_pos_offset[1]
        return (grid_x, grid_y)

    def update(self, blobs: list) -> list:
        """
        blobs: [{cx, cy, type, name, score, grid, screen, is_whitelisted, ...}, ...]
        반환: 갱신된 tracked 리스트 ({'id': int, ...} 포함)
        """
        now = time.time()
        matched_old_ids: set = set()
        result: list = []

        # 캐릭터 정보 가져오기 (캐릭터 기준 계산용)
        me = self.state.entities.get("me", {})
        char_grid = me.get("grid", (9, 11))  # 기본값 J12
        char_screen = me.get("screen", (0, 0))
        char_pixel_x, char_pixel_y = char_screen

        for blob in blobs:
            bx, by = blob.get("cx", 0), blob.get("cy", 0)
            blob_grid = blob.get("grid")
            blob_name = blob.get("name", "")
            blob_type = blob.get("type", "")
            best_id, best_dist = None, float("inf")

            # 캐릭터 기준으로 Grid 재계산 (맵 스크롤 대응)
            if char_pixel_x > 0 and char_pixel_y > 0:
                # 픽셀 거리를 Grid 거리로 변환
                pixel_dx = bx - char_pixel_x
                pixel_dy = by - char_pixel_y
                grid_dx = pixel_dx // 48
                grid_dy = pixel_dy // 48

                # 캐릭터 Grid 기준으로 아이템 Grid 계산
                blob_grid = (char_grid[0] + grid_dx, char_grid[1] + grid_dy)
                blob["grid"] = blob_grid

                # POS 좌표 계산
                blob_pos = self.grid_to_pos(blob_grid[0], blob_grid[1])
                blob["pos"] = blob_pos
            else:
                # 캐릭터 정보 없으면 기존 grid 사용
                blob["pos"] = self.grid_to_pos(blob_grid[0], blob_grid[1]) if blob_grid else (0, 0)

            # Grid 기반 중복 체크 (같은 grid + 같은 타입 + 같은 이름이면 기존 개체 갱신)
            for oid, odata in self.tracked.items():
                if oid in matched_old_ids:
                    continue
                if odata.get("type", "") != blob_type:
                    continue  # 다른 타입은 매치 안 함
                odata_grid = odata.get("grid")
                odata_name = odata.get("name", "")
                # 같은 grid + 같은 이름이면 즉시 매치 (거리 계산 생략)
                if blob_grid and odata_grid and blob_name == odata_name:
                    # 같은 grid면 무조건 매치 (정확한 일치)
                    if blob_grid == odata_grid:
                        best_id = oid
                        best_dist = 0
                        break
                # 그 외 거리 기준 매칭
                dist = ((bx - odata["cx"]) ** 2 + (by - odata["cy"]) ** 2) ** 0.5
                if dist < best_dist:
                    best_dist, best_id = dist, oid

            if best_id is not None and best_dist < self.MATCH_DIST:
                # 기존 개체 갱신
                prev_cx = self.tracked[best_id].get("cx", bx)
                prev_cy = self.tracked[best_id].get("cy", by)
                self.tracked[best_id].update(blob)
                self.tracked[best_id]["cx"] = bx
                self.tracked[best_id]["cy"] = by
                self.tracked[best_id]["last_seen"] = now
                self.tracked[best_id]["prev_cx"] = prev_cx
                self.tracked[best_id]["prev_cy"] = prev_cy

                # 이동 벡터 계산 (방향 변화 추적)
                dx = bx - prev_cx
                dy = by - prev_cy
                self.tracked[best_id]["dx"] = dx
                self.tracked[best_id]["dy"] = dy

                # 방향 변화 횟수 계산 (0.5초 이내)
                if "direction_changes" not in self.tracked[best_id]:
                    self.tracked[best_id]["direction_changes"] = 0
                    self.tracked[best_id]["last_direction"] = (dx, dy)
                    self.tracked[best_id]["last_dir_time"] = now
                else:
                    last_dx, last_dy = self.tracked[best_id]["last_direction"]
                    # 방향이 크게 바뀌었는지 확인 (내적 계산)
                    if abs(dx * last_dx + dy * last_dy) < 0:  # 방향이 반대 또는 크게 다름
                        if now - self.tracked[best_id]["last_dir_time"] < 0.5:
                            self.tracked[best_id]["direction_changes"] += 1
                        self.tracked[best_id]["last_direction"] = (dx, dy)
                        self.tracked[best_id]["last_dir_time"] = now

                # 몬스터: grid 좌표 변경 시만 좌표 출력 (GPS로 대체되어 주석 처리)
                # t = self.tracked[best_id].get("type")
                # name = self.tracked[best_id].get("name", "")
                # if t == "MONSTER" and blob_grid:
                #     last_grid = self.last_grid.get(best_id)
                #     if last_grid != blob_grid:
                #         self.last_grid[best_id] = blob_grid

                matched_old_ids.add(best_id)
                entry = dict(self.tracked[best_id])
                entry["id"] = best_id
                result.append(entry)
            else:
                # 신규 개체
                oid = self.next_id
                self.next_id += 1
                self.tracked[oid] = {**blob, "cx": bx, "cy": by, "last_seen": now}
                self.coord_print_count[oid] = 0
                grid = blob.get("grid")  # grid를 먼저 가져옴
                self.last_grid[oid] = grid  # 초기 grid 저장
                self.entity_printed[oid] = False  # 메시지 출력 여부 초기화
                entry = dict(self.tracked[oid])
                entry["id"] = oid
                result.append(entry)

                # 신규 개체 발견 시 즉각 알림 (사용자 지시사항) - GPS로 대체되어 주석 처리
                # t = entry.get("type")
                # name = entry.get("name", "")
                # gx_d = int(grid[0]) if grid else -1
                # gy_d = int(grid[1]) if grid else -1
                # gname = (f"{chr(ord('A') + gx_d)}{gy_d + 1}" if grid and gx_d >= 0 else "?")
                # score = entry.get("score", 0.0)

                # # 좌표 계산 (내 캐릭터 기준) - GPS로 대체되어 주석 처리
                # me = self.state.entities.get("me", {})
                # me_grid = me.get("grid", (-1, -1))
                # me_screen = me.get("screen", (0, 0))
                # me_pos_x, me_pos_y = me_grid

                # if t == "MONSTER" or t == "ITEM":
                #     # 아이템: 1회만 좌표 출력, 몬스터: 첫 감지 시 출력
                #     if t == "ITEM" and self.coord_print_count[oid] < 1:
                #         self.coord_print_count[oid] += 1
                #         entity_pos_x, entity_pos_y = grid
                #         dx = entity_pos_x - me_pos_x
                #         dy = entity_pos_y - me_pos_y
                #         coord_msg = f"[Coord] 내 캐릭터: ({me_pos_x}, {me_pos_y}) | {t} '{name}': ({entity_pos_x}, {entity_pos_y}) | 상대: ({dx}, {dy})"
                #         print(coord_msg)
                #     elif t == "MONSTER":
                #         self.coord_print_count[oid] += 1
                #         entity_pos_x, entity_pos_y = grid
                #         dx = entity_pos_x - me_pos_x
                #         dy = entity_pos_y - me_pos_y
                #         coord_msg = f"[Coord] 내 캐릭터: ({me_pos_x}, {me_pos_y}) | {t} '{name}': ({entity_pos_x}, {entity_pos_y}) | 상대: ({dx}, {dy})"
                #         print(coord_msg)

                # # New Entity 메시지 1회만 출력 (GPS로 대체되어 주석 처리)
                # if not self.entity_printed[oid]:
                #     self.entity_printed[oid] = True
                #     msg = f"[New Entity] {t} '{name}' 감지 (Grid: {gname}, Score: {score:.2f})"
                #     print(msg)
                #     if hasattr(self.state, "last_event_log"):
                #         self.state.last_event_log = msg
                # elif t == "USER" and not entry.get("is_whitelisted", True):
                #     msg = f"[Warning] 미확인 유저 '{name}' 감지! (안전 지대 이동 대기중)"
                #     print(msg)
                #     if hasattr(self.state, "last_event_log"):
                #         self.state.last_event_log = msg

        # 만료 제거
        expired = [oid for oid, d in self.tracked.items()
                   if (now - d.get("last_seen", 0)) > self.EXPIRE_SEC]
        for oid in expired:
            del self.tracked[oid]
            if oid in self.coord_print_count:
                del self.coord_print_count[oid]
            if oid in self.last_grid:
                del self.last_grid[oid]
            if oid in self.entity_printed:
                del self.entity_printed[oid]

        return result

# ═══════════════════════════════════════════════════════════════════════════════
# 숫자 필드 전용 초고속 스레드 (60 FPS = 16ms)
# ═══════════════════════════════════════════════════════════════════════════════
class NumericFieldScanner(threading.Thread):
    """
    숫자 필드 (hp, mp, exp, money, x, y)만 전담하여 60 FPS 주기로 스캔하는 별도 스레드.
    메인 OCR 루프와 독립적으로 동작하여 숫자 라벨 업데이트 지연을 해결.
    """
    def __init__(self, state: GameState, matcher: PatternMatcher, config_file: str, monitor_svc):
        super().__init__(name="NumericFieldScanner", daemon=True)
        self.state = state
        self.matcher = matcher
        self.config_file = config_file
        self.monitor_svc = monitor_svc  # 오프셋 정보만 참조
        self._last_run_time = 0
        self._running = True
        self.frame_count = 0
        self._last_processed_frame_time = 0.0
        self._fps_last_time = time.time()
        self._fps_frames = 0

        # FindText (ft.ahk Python 포팅) 초기화
        from findtext_wrapper import FindText
        self.findtext = FindText()
        self.findtext_engine = FindTextEngine(self.matcher.AHK_PATTERNS)
        self.numeric_green_ranges = [
            (np.array([30, 30, 50]), np.array([90, 255, 255])),
        ]
        self.numeric_gold_ranges = [
            (np.array([10, 50, 150]), np.array([35, 255, 255])),
        ]
        self.field_hsv_ranges = {
            "hp": self.numeric_green_ranges,
            "mp": self.numeric_green_ranges,
            "exp": self.numeric_green_ranges,
            "money": self.numeric_gold_ranges,
            "x": self.numeric_gold_ranges,
            "y": self.numeric_gold_ranges,
        }
        self.digit_template_masks = self._build_digit_template_masks()

        # 별도 카메라 인스턴스 (공유 충돌 방지)
        self.cap = None
        self.camera_index = -1
        self.retry_count = 0
        self.max_retries = 5

        # 숫자 필드 ROI 캐싱
        self.numeric_regions = {}
        self.load_numeric_regions()

    def load_numeric_regions(self):
        """숫자 필드 ROI만 로드하여 캐싱"""
        if os.path.exists(self.config_file):
            with open(self.config_file, "r", encoding="utf-8") as f:
                config_data = json.load(f)
                for k, v in config_data.items():
                    if isinstance(v, dict) and 'sx' in v and k in ("hp", "mp", "exp", "money", "x", "y"):
                        self.numeric_regions[k] = dict_to_region(v)

    def _build_digit_template_masks(self):
        templates = []
        digit_templates = getattr(self.matcher, "templates", {}).get("digits", {})
        template_ranges = self.numeric_green_ranges + self.numeric_gold_ranges

        for name, img in digit_templates.items():
            digit = str(name)
            if not digit.isdigit():
                continue

            mask = self._extract_text_mask(img, template_ranges, rgb_input=False, cleanup=False)
            glyph = self._tight_glyph(mask)
            if glyph is not None:
                templates.append((digit, glyph))

        return templates

    def _extract_text_mask(self, image, ranges, rgb_input=True, cleanup=True):
        if image is None or image.size == 0:
            return None

        code = cv2.COLOR_RGB2HSV if rgb_input else cv2.COLOR_BGR2HSV
        hsv = cv2.cvtColor(image, code)
        mask = np.zeros(image.shape[:2], dtype=np.uint8)
        for lower, upper in ranges:
            mask = cv2.bitwise_or(mask, cv2.inRange(hsv, lower, upper))

        if cleanup:
            kernel = np.ones((2, 2), dtype=np.uint8)
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

        return mask

    def _tight_glyph(self, mask):
        if mask is None or mask.size == 0:
            return None

        ys, xs = np.where(mask > 0)
        if len(xs) == 0:
            return None

        return (mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1] > 0).astype(np.uint8)

    def _find_digit_groups(self, mask):
        if mask is None or mask.size == 0:
            return []

        col_hits = (mask > 0).sum(axis=0)
        groups = []
        start = None

        for idx, hits in enumerate(col_hits):
            if hits >= 2:
                if start is None:
                    start = idx
            elif start is not None:
                groups.append((start, idx))
                start = None

        if start is not None:
            groups.append((start, len(col_hits)))

        merged = []
        for start, end in groups:
            if merged and start - merged[-1][1] <= 3:
                merged[-1] = (merged[-1][0], end)
            else:
                merged.append((start, end))

        return merged

    def _classify_digit_glyph(self, glyph):
        if glyph is None or glyph.size == 0 or not self.digit_template_masks:
            return "x", 0.0

        best_digit = "x"
        best_score = 0.0

        for digit, template in self.digit_template_masks:
            resized = cv2.resize(glyph, (template.shape[1], template.shape[0]), interpolation=cv2.INTER_NEAREST)
            eq = float((resized == template).mean())
            inter = float(np.logical_and(resized, template).sum())
            union = float(np.logical_or(resized, template).sum())
            iou = inter / max(1.0, union)
            score = iou * 0.7 + eq * 0.3

            if score > best_score:
                best_digit = digit
                best_score = score

        if best_score < 0.60:
            return "x", best_score

        return best_digit, best_score

    def _compose_fixed_xy(self, groups, mask):
        slot_centers = [((idx + 0.5) * mask.shape[1] / 4.0) for idx in range(4)]
        slot_best = [(0.0, "x") for _ in range(4)]

        for start, end in groups:
            glyph_mask = mask[:, max(0, start - 2):min(mask.shape[1], end + 2)]
            glyph = self._tight_glyph(glyph_mask)
            digit, score = self._classify_digit_glyph(glyph)
            center = (start + end) / 2.0
            slot_idx = min(range(4), key=lambda idx: abs(slot_centers[idx] - center))

            if score >= slot_best[slot_idx][0]:
                slot_best[slot_idx] = (score, digit)

        return "".join(digit for _, digit in slot_best)

    def _recognize_segmented_digits(self, crop, debug_name="unknown"):
        ranges = self.field_hsv_ranges.get(debug_name, self.numeric_green_ranges + self.numeric_gold_ranges)
        mask = self._extract_text_mask(crop, ranges, rgb_input=True, cleanup=True)
        groups = self._find_digit_groups(mask)

        if debug_name in ("x", "y"):
            if not groups:
                return "xxxx"
            return self._compose_fixed_xy(groups, mask)

        if not groups:
            return ""

        chars = []
        for start, end in groups:
            glyph_mask = mask[:, max(0, start - 2):min(mask.shape[1], end + 2)]
            glyph = self._tight_glyph(glyph_mask)
            digit, _ = self._classify_digit_glyph(glyph)
            chars.append(digit)

        return "".join(chars)

    def recognize_with_findtext(self, crop, debug_name="unknown"):
        """게임 숫자 폰트에 맞춘 초경량 비트맵 매칭. 실패 시 기존 FindTextEngine으로 폴백."""
        result = self._recognize_segmented_digits(crop, debug_name=debug_name)
        if result:
            return result
        return self.findtext_engine.recognize_with_findtext(crop, override_threshold=self.state.bin_threshold, debug_name=debug_name)

    def run(self):
        while self._running and self.state.running:
            now = time.time()
            # 100 FPS 제한 (약 10ms 주기) - 병목 해소를 위해 상향
            if now - self._last_run_time < 0.025:
                time.sleep(0.003)
                continue
            self._last_run_time = now

            # GameState의 캐싱된 프레임 사용
            if not hasattr(self.state, 'last_frame') or self.state.last_frame is None:
                time.sleep(0.02)
                continue

            # 프레임이 너무 오래되었으면 스킵 (500ms 이상으로 완화)
            if now - self.state.last_frame_time > 0.5:
                time.sleep(0.02)
                continue

            frame_time = self.state.last_frame_time
            if frame_time <= self._last_processed_frame_time:
                time.sleep(0.004)
                continue

            frame = self.state.last_frame
            self._last_processed_frame_time = frame_time
            
            # frame_count 증가
            self.frame_count += 1
            self._fps_frames += 1

            # Track capture-to-OCR latency + numeric OCR FPS (for GUI/perf debugging).
            try:
                age_ms = max(0.0, (now - frame_time) * 1000.0)
                setattr(self.state, "capture_age_ms", age_ms)
                setattr(self.state, "numeric_last_update_time", now)
                if now - self._fps_last_time >= 1.0:
                    fps = self._fps_frames / max(1e-6, (now - self._fps_last_time))
                    setattr(self.state, "numeric_fps", fps)
                    self._fps_last_time = now
                    self._fps_frames = 0
            except Exception:
                pass

            # GameState 직접 업데이트 (GUI 업데이트 큐 사용하지 않음)
            for name, reg in self.numeric_regions.items():
                ox, oy, scx, scy = getattr(self.state, "obs_params", (0, 0, 1.0, 1.0))
                fh, fw = frame.shape[:2]
                # [Test 73 - 1:1 Fix] 절대 1:1 좌표 강제
                sx = int(reg.sx)
                sy = int(reg.sy)
                dx = int(reg.dx)
                dy = int(reg.dy)

                if sx >= dx or sy >= dy:
                    continue

                # 숫자 ROI는 설정값이 이미 촘촘해 과한 padding이 오히려 오인식을 만든다.
                pad = 1
                h, w = frame.shape[:2]
                sy_p, dy_p = max(0, sy - pad), min(h, dy + pad)
                sx_p, dx_p = max(0, sx - pad), min(w, dx + pad)
                
                crop = frame[sy_p:dy_p, sx_p:dx_p]
                if crop.size == 0:
                    continue
                
                # FindTextEngine으로 인식 (문자열로 우선 보존)
                result = self.recognize_with_findtext(crop, debug_name=name)
                if result is None:
                    continue

                result_str = str(result)
                # x/y는 항상 4자리 고정 (누락은 'x')
                if name in ("x", "y"):
                    if len(result_str) < 4:
                        result_str = result_str.ljust(4, "x")
                    elif len(result_str) > 4:
                        result_str = result_str[:4]

                # 항상 *_str에 저장 (GUI/디버깅용)
                setattr(self.state, f"{name}_str", result_str)

                # 숫자-only일 때만 정수 값 갱신 (로직 안정성 유지)
                if result_str.isdigit():
                    val = int(result_str)
                    setattr(self.state, name, val)

                    # 좌표 연동 업데이트
                    if name == "x":
                        current_y = getattr(self.state, "y", 0)
                        self.state.my_world_pos = (val, current_y)
                    elif name == "y":
                        current_x = getattr(self.state, "x", 0)
                        self.state.my_world_pos = (current_x, val)

    def stop(self):
        self._running = False


# ═══════════════════════════════════════════════════════════════════════════════
# SentinelThread - 몬스터/아이템 감지 (30 FPS)
# ═══════════════════════════════════════════════════════════════════════════════
class SentinelThread(threading.Thread):
    """
    몬스터, 아이템, 유저 감지를 전담하여 15 FPS 주기로 수행하는 분리된 스레드.
    Native Resolution (1:1) 매칭을 위해 리사이즈 없이 중심점 기준 크롭 방식을 사용함.
    """
    def __init__(self, state: GameState, matcher: PatternMatcher, grid_indicator=None):
        super().__init__(name="SentinelThread", daemon=True)
        self.state = state
        self.matcher = matcher
        self.entity_tracker = EntityTracker(self.state)
        # grid_indicator는 호환성을 위해 유지하지만, GameState에서 값을 참조
        self.grid_indicator = grid_indicator
        self._blob_fail_count = 0
        self._last_run_time = 0
        self._last_overlay_time = 0
        # 디바운싱 로직: 3프레임 연속 감지 시 확정
        self._monster_history = {}  # {grid_key: [frame_count, last_seen_time]}
        self._debounce_frames = 3  # 연속 감지 필요 프레임 수

    def run(self):
        _monitor_log("[Sentinel] Entity scan thread start (FindText XOR mode)")
        while self.state.running:
            # Keep numeric OCR/UI responsive: idle unless explicitly enabled.
            if not getattr(self.state, "sentinel_enabled", False):
                time.sleep(0.05)
                continue
            now = time.time()
            # 30 FPS 제한 (약 33ms 주기)
            if now - self._last_run_time < 0.033:
                time.sleep(0.01)
                continue
            self._last_run_time = now

            play_area_rgb = getattr(self.state, "last_play_area_rgb", None)
            if play_area_rgb is None or play_area_rgb.size == 0:
                continue

            # 이전 프레임에서 감지된 몬스터/아이템의 Grid 좌표를 우선 검색 영역으로 설정
            priority_grids = []
            for m in self.state.entities.get("monsters", []):
                g = m.get("grid")
                if g and g[0] >= 0:
                    priority_grids.append(g)
            for i in self.state.entities.get("items", []):
                g = i.get("grid")
                if g and g[0] >= 0:
                    priority_grids.append(g)

            # FindText 방식 슬라이딩 XOR 매칭 (색상 무시, 모양만 비교)
            scan_results = self.matcher.findtext_scan(
                play_area_rgb,
                folders=["monsters", "items"],
                threshold=0.90,
                stride=2,
                priority_grids=priority_grids if priority_grids else None
            )

            whitelist = list(getattr(self.state, "whitelist_names", []))
            new_blobs = []
            for r in scan_results:
                base_entry = {
                    "name": r["name"],
                    "type": r["type"],
                    "score": r["score"],
                    "is_whitelisted": (r["name"] in whitelist) if r["type"] == "USER" else False,
                    "grid": r["grid"],
                    "cx": r["cx"],
                    "cy": r["cy"],
                    "w": r["w"],
                    "h": r["h"],
                    "last_seen": now,
                }
                new_blobs.append(base_entry)

            # 트래커 업데이트 및 상태 반영
            tracked_all = self.entity_tracker.update(new_blobs)
            
            # 상태 플래그 계산
            monsters = [e for e in tracked_all if e.get("type") == "MONSTER"]
            items    = [e for e in tracked_all if e.get("type") == "ITEM"]
            users    = [e for e in tracked_all if e.get("type") == "USER"]

            # 중복 감지 방지: 같은 위치(픽셀 좌표)에 있는 엔티티 필터링
            def deduplicate_entities(entities):
                if not entities:
                    return []
                seen_positions = set()
                deduplicated = []
                for e in entities:
                    pos = (e.get("cx", 0), e.get("cy", 0))
                    # 같은 위치에 있는 엔티티는 하나만 유지 (거리 기준: 10픽셀 이내)
                    is_duplicate = False
                    for seen_pos in seen_positions:
                        if ((pos[0] - seen_pos[0])**2 + (pos[1] - seen_pos[1])**2)**0.5 < 10:
                            is_duplicate = True
                            break
                    if not is_duplicate:
                        seen_positions.add(pos)
                        deduplicated.append(e)
                return deduplicated

            monsters = deduplicate_entities(monsters)
            items = deduplicate_entities(items)
            users = deduplicate_entities(users)

            # 디바운싱 로직: 3프레임 연속 감지 시만 확정
            now = time.time()
            confirmed_monsters = []
            
            # 현재 프레임에서 감지된 몬스터의 그리드 키 생성
            current_grid_keys = set()
            for m in monsters:
                grid = m.get("grid")
                if grid and len(grid) == 2:
                    grid_key = f"{grid[0]}_{grid[1]}"
                    current_grid_keys.add(grid_key)
                    
                    # 히스토리 업데이트
                    if grid_key not in self._monster_history:
                        self._monster_history[grid_key] = [1, now]
                    else:
                        self._monster_history[grid_key][0] += 1
                        self._monster_history[grid_key][1] = now
            
            # 3프레임 이상 연속 감지된 몬스터만 확정
            for m in monsters:
                grid = m.get("grid")
                if grid and len(grid) == 2:
                    grid_key = f"{grid[0]}_{grid[1]}"
                    if self._monster_history.get(grid_key, [0, 0])[0] >= self._debounce_frames:
                        confirmed_monsters.append(m)
            
            # 오래된 히스토리 제거 (1초 이상 감지되지 않은 경우)
            expired_keys = [k for k, v in self._monster_history.items() if now - v[1] > 1.0]
            for k in expired_keys:
                del self._monster_history[k]
            
            # 현재 프레임에서 감지되지 않은 키의 카운트 리셋
            for grid_key in self._monster_history:
                if grid_key not in current_grid_keys:
                    self._monster_history[grid_key][0] = 0
            
            monsters = confirmed_monsters

            # 월드 좌표 역계산 엔진: 픽셀 거리 기반 월드 좌표 계산
            my_screen_x, my_screen_y = self.state.my_screen_pos
            my_world_x, my_world_y = self.state.my_world_pos
            
            # 화살표 감지 실패 시(초기값 0,0) GPS 계산 스킵
            if my_screen_x == 0 and my_screen_y == 0:
                detected_entities = []
            else:
                # config.json에서 grid_size 읽어오기
                try:
                    import json as _cfg_json
                    _cfg_file = os.path.join(os.path.dirname(self.config_file), "config.json")
                    with open(_cfg_file, 'r', encoding='utf-8') as _cfg_f:
                        grid_pixel_size = float(_cfg_json.load(_cfg_f).get("play_area", {}).get("grid_size", 48.2))
                except Exception:
                    grid_pixel_size = 48.2

                detected_entities = []
                for e in tracked_all:
                    cx = e.get("cx", 0)
                    cy = e.get("cy", 0)
                    # 픽셀 차이 계산
                    pixel_diff_x = cx - my_screen_x
                    pixel_diff_y = cy - my_screen_y
                    # 월드 좌표 역계산
                    target_world_x = my_world_x + (pixel_diff_x / grid_pixel_size)
                    target_world_y = my_world_y + (pixel_diff_y / grid_pixel_size)
                    # 엔티티에 월드 좌표 추가 (정수로 반올림)
                    e["world_pos"] = (round(target_world_x), round(target_world_y))
                    detected_entities.append(e)

                    # 디버그 로그: 몬스터/아이템 발견 시 거리 계산 (비활성화 - 비트맵 이진화 완성 전까지)
                    # if e.get("type") in ["MONSTER", "ITEM"]:
                    #     dist_grid = ((target_world_x - my_world_x)**2 + (target_world_y - my_world_y)**2)**0.5
                    #     print(f"[GPS] 내 위치: ({my_world_x},{my_world_y}) | {e['type']} 발견: ({round(target_world_x)},{round(target_world_y)}) | 거리: {dist_grid:.1f} Grid")

            # GridIndicator에 아이템 위치 전달 (GameState에서 grid_overlay_enabled 확인)
            if self.grid_indicator and getattr(self.state, "grid_overlay_enabled", False):
                item_grids = [i.get("grid") for i in items if i.get("grid")]
                self.grid_indicator.update_item_positions(item_grids)
            
            hostile_users = [u for u in users if not u.get("is_whitelisted")]
            
            with getattr(self.state, "_lock", threading.Lock()):
                self.state.entities["monsters"] = monsters
                self.state.entities["items"] = items
                self.state.entities["users"] = users
                self.state.entities["objects"] = tracked_all
                self.state.detected_entities = detected_entities
                
                # 가공된 상태 업데이트 (MonitorSvc 역할을 가져옴)
                self.state.monster_on_screen = len(monsters) > 0
                if hostile_users:
                    self.state.is_user_detected = True
                    self.state.user_name = hostile_users[0]["name"]
                else:
                    self.state.is_user_detected = False
                
                # 최근 발견된 몬스터/아이템 정보 (Status 출력용)
                if monsters:
                    self.state.detected_monster_grid = monsters[0]["grid"]
                    self.state.detected_monster_name = monsters[0]["name"]
                if items:
                    self.state.detected_item_grid = items[0]["grid"]
                    self.state.detected_item_name = items[0]["name"]

            # 실패 로그 모니터링 (5회 연속 실패 시 1회 요약 출력) - 비활성화
            # last_err = getattr(self.state, "last_vision_error", "")
            # if last_err:
            #     self._blob_fail_count += 1
            #     if self._blob_fail_count >= 5:
            #         print(f"   [Debug] 인식 실패 요약: {last_err}")
            #         self._blob_fail_count = 0
            #         self.state.last_vision_error = "" # 초기화하여 도배 방지
            # else:
            #     self._blob_fail_count = 0

            # 디버그 오버레이: 1초에 한 번 엔티티 박스 그리기
            if now - self._last_overlay_time >= 1.0:
                play_area_overlay = self.state.ocr_preview_img
                if play_area_overlay is not None:
                    # 몬스터: 빨간 박스
                    for m in monsters:
                        cx, cy = m.get("cx", 0), m.get("cy", 0)
                        w, h = m.get("w", 20), m.get("h", 20)
                        x1, y1 = cx - w // 2, cy - h // 2
                        x2, y2 = cx + w // 2, cy + h // 2
                        cv2.rectangle(play_area_overlay, (x1, y1), (x2, y2), (0, 0, 255), 2)
                        cv2.putText(play_area_overlay, m.get("name", ""), (x1, y1 - 5),
                                   cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1)
                    # 아이템: 파란 박스
                    for i in items:
                        cx, cy = i.get("cx", 0), i.get("cy", 0)
                        w, h = i.get("w", 20), i.get("h", 20)
                        x1, y1 = cx - w // 2, cy - h // 2
                        x2, y2 = cx + w // 2, cy + h // 2
                        cv2.rectangle(play_area_overlay, (x1, y1), (x2, y2), (255, 0, 0), 2)
                        cv2.putText(play_area_overlay, i.get("name", ""), (x1, y1 - 5),
                                   cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 0, 0), 1)
                self._last_overlay_time = now

class MonitorSvc(threading.Thread):
    NUMERIC_FIELDS = {"hp", "mp", "exp", "money", "x", "y"}

    def __init__(self, state: GameState, config_file: str,
                 maps_file: str = None):
        super().__init__(daemon=True)
        self.state       = state
        self.config_file = config_file
        self.maps_file   = maps_file or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "maps.json")
        self.regions: dict     = {}
        self.last_hashes: dict = {}
        self.last_values: dict = {}
        self.last_frame_hashes: dict = {}  # 숫자 필드 변경 감지용 프레임 해시
        self.last_load_time    = time.time()
        self.enabled           = False  # OCR ?진 ?성???래?

        # [CaptureSvc 연동] MonitorSvc는 이제 직접 캡처하지 않고 공유 프레임을 소비합니다.
        self.cap           = None
        self.ocr_enabled      = True
        self.ocr_threshold    = 0.65  # 기본 인식 임계값
        self.ocr_preview_img  = None  # 실시간 인식 영역 미리보기 이미지
        self.auto_hunt        = False
        self.camera_index  = -1
        self.retry_count   = 0
        self.max_retries   = 5
        self._last_preview_dump_time = 0.0
        self._last_gps_log_time = 0.0
        self._last_gps_grid = None

        # PatternMatcher: temple/ 루트
        tmpl_root = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "temple")
        self.matcher = PatternMatcher(tmpl_root, self.state)
        marker_names = sorted(list(self.matcher.templates.get("marker", {}).keys()))
        _monitor_log(f"[GPS] 마커 템플릿 로드 완료: {marker_names}")

        # FindText (ft.ahk Python 포팅) 초기화
        from findtext_wrapper import FindText
        self.findtext_engine = FindTextEngine(self.matcher.AHK_PATTERNS)
        self.findtext = FindText()

        # map_info: 선비족 FindText 토큰 인식기
        from svc_map_ocr import MapNameRecognizer
        self.map_ocr = MapNameRecognizer(digit_patterns=self.matcher.AHK_PATTERNS)
        self._map_ocr_last_log_time = 0.0
        self._map_ocr_debug_enabled = False
        self._map_fail_count = 0
        self._map_info_last_signature = ""
        self._map_info_change_seq = 0
        self._map_info_changed_at = 0.0
        print(f"[MapOCR] ready tokens={list(self.map_ocr.tokens.keys())}")

        self.load_roi()
        self.load_maps()
        if "map_info" in self.regions:
            r = self.regions["map_info"]
            print(f"[MapOCR] map_info ROI loaded: ({r.sx},{r.sy})-({r.dx},{r.dy})")
        else:
            print("[MapOCR] WARN: map_info ROI missing in config")
        self.obs_params = (0, 0, 1.0, 1.0) # (ox, oy, scx, scy) 초기값
        self.offset_history = []
        self.offset_locked = False
        self.obs_ema_params = None  # EMA 파라미터 (최초 소거)
        self.ema_alpha = 0.2        # EMA 가중치 (0.3=반응속도 좋음, 0.1=부드러움)

        # EntityTracker — Object_ID 기반 실시간 추적
        self.entity_tracker = EntityTracker(self.state)

        # 2-Stage Detection 타이머 (Stage2: 25ms 간격에만 패턴매칭 실행)
        self._stage1_blobs: list = []         # Stage1 결과 캐시 (BBox 좌표)
        self._last_stage2_time: float = 0.0   # Stage2 마지막 실행 시각
        
        # 마지막 프레임 캐싱 (NumericFieldScanner 공유용)
        self.last_frame = None
        self.last_frame_time = 0

        # Red-tab tracker state. The selection box is only visible in play_area.
        self._red_tab_state = False
        self._red_tab_hit_streak = 0
        self._red_tab_miss_streak = 0
        self._red_tab_last_bbox = None
        self._red_tab_last_candidate = None
        self._red_tab_last_log_time = 0.0
        self._last_user_info_logged = None
        self._last_user_info_source = None
        self._party_user_candidate = ""
        self._party_user_candidate_hits = 0
        self._party_user_stable_name = ""
        self._party_user_stable_score = 0.0
        self._party_user_stable_source = None
        self._party_user_stable_until = 0.0
        self._party_user_required_hits = 2
        self._party_user_stable_ttl = 0.45
        self._last_user_info_mode = "idle"
        self._parsed_pattern_cache = {}
        self._roi_last_processed_at = {}
        self._roi_min_interval_sec = {
            "hp_trig": 0.020,
            "mp_trig": 0.020,
            "play_area": 0.020,
            "target_info": 0.025,
            "user_info": 0.070,
            "cooltime_area": 0.090,
            "stat_info": 0.140,
            "items_scan": 0.120,
            "map_info": 0.080,
        }

        # patterns.json 로드
        self.patterns = self._load_patterns()

    def _roi_interval_for(self, region_name: str) -> float:
        base = float(self._roi_min_interval_sec.get(region_name, 0.060))
        if region_name == "user_info":
            if bool(getattr(self.state, "self_status_scan_active", False)):
                return min(base, 0.040)
            if bool(getattr(self.state, "ntab_active", False)):
                return min(base, 0.045)
            if bool(getattr(self.state, "service_active", False)):
                return min(base, 0.055)
        if region_name == "cooltime_area" and bool(getattr(self.state, "service_active", False)):
            return min(base, 0.070)
        return base

    def _should_process_roi(self, region_name: str, now_ts: float) -> bool:
        if region_name in self.NUMERIC_FIELDS:
            return False
        last_ts = float(self._roi_last_processed_at.get(region_name, 0.0) or 0.0)
        min_interval = self._roi_interval_for(region_name)
        if (now_ts - last_ts) < min_interval:
            return False
        self._roi_last_processed_at[region_name] = now_ts
        return True

    @staticmethod
    def _fast_crop_signature(crop: np.ndarray) -> tuple:
        h, w = crop.shape[:2]
        c0 = crop[0, 0]
        cm = crop[h // 2, w // 2]
        c1 = crop[h - 1, w - 1]
        mean_val = float(crop.mean()) if crop.size else 0.0
        return (
            h,
            w,
            int(c0[0]), int(c0[1]), int(c0[2]),
            int(cm[0]), int(cm[1]), int(cm[2]),
            int(c1[0]), int(c1[1]), int(c1[2]),
            round(mean_val, 2),
        )

    @staticmethod
    def _map_info_fingerprint(crop: np.ndarray) -> str:
        if crop is None or crop.size == 0:
            return ""
        try:
            gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
            normalized = cv2.resize(gray, (16, 8), interpolation=cv2.INTER_AREA)
            _threshold, binary = cv2.threshold(
                normalized,
                0,
                255,
                cv2.THRESH_BINARY | cv2.THRESH_OTSU,
            )
            return "".join("1" if value else "0" for value in binary.reshape(-1))
        except Exception:
            return ""

    def _resolve_user_info_mode(self, now_ts: float) -> str:
        if bool(getattr(self.state, "ntab_active", False)):
            return "ntab"

        scan_until = float(getattr(self.state, "self_status_scan_until", 0.0) or 0.0)
        scan_active = bool(getattr(self.state, "self_status_scan_active", False))
        if scan_active and now_ts <= scan_until:
            return "self_status"
        if scan_active and now_ts > scan_until:
            self.state.self_status_scan_active = False
            self.state.self_status_scan_until = 0.0
        return "idle"

    def _recognize_with_findtext(self, crop):
        """findtext_wrapper의 _template_match_native를 사용하여 숫자 인식"""
        # 모든 숫자 패턴에 대해 검색
        all_matches = []
        
        for digit, ahk_pattern in self.matcher.AHK_PATTERNS.items():
            # 다중 템플릿 지원 - 리스트 형식 처리
            if isinstance(ahk_pattern, list):
                # 여러 템플릿 모두 시도하여 가장 좋은 결과 선택
                best_results = []
                for template_str in ahk_pattern:
                    pattern_data = self.matcher.decode_ahk_pattern(template_str)
                    if not pattern_data or 'bitmap' not in pattern_data:
                        continue
                    
                    # findtext_wrapper 형식으로 변환
                    pattern_dict = {
                        'bitmap': pattern_data['bitmap'],
                        'width': pattern_data['width'],
                        'height': pattern_data['bitmap'].shape[0],
                        'color': pattern_data.get('threshold', 127),
                        'comment': digit
                    }
                    
                    # findtext_wrapper의 _template_match_native 호출
                    results = self.findtext._template_match_native(crop, pattern_dict, 0.1, 300, find_all=True)
                    if results:
                        best_results.extend(results)
                
                # 이 숫자의 모든 템플릿 결과 추가
                if best_results:
                    all_matches.extend(best_results)
            else:
                # 단일 템플릿 처리 (기존 방식)
                pattern_data = self.matcher.decode_ahk_pattern(ahk_pattern)
                if not pattern_data or 'bitmap' not in pattern_data:
                    continue
                
                # findtext_wrapper 형식으로 변환
                pattern_dict = {
                    'bitmap': pattern_data['bitmap'],
                    'width': pattern_data['width'],
                    'height': pattern_data['bitmap'].shape[0],
                    'color': pattern_data.get('threshold', 127),
                    'comment': digit
                }
                
                # findtext_wrapper의 _template_match_native 호출
                results = self.findtext._template_match_native(crop, pattern_dict, 0.1, 300, find_all=True)
                if results:
                    all_matches.extend(results)
        
        if not all_matches:
            return ""
        
        # x 좌표 기준 정렬 (숫자 순서 보정)
        all_matches.sort(key=lambda m: m[0])
        
        # 중복 제거
        final_digits = []
        last_x = -999
        min_gap = 30  # 중복 제거 강화
        
        for x, y, digit in all_matches:
            if x - last_x > min_gap:
                final_digits.append(digit)
                last_x = x
        
        return "".join(final_digits)

    def detect_obs_game_offset(self, frame):
        """?비?????링 보정: ?제 게임 ?라?언???상?? 기반?로 비율 계산"""
        fh, fw = frame.shape[:2]
        lower_brown = np.array([10, 30, 60])
        upper_brown = np.array([95, 135, 175])
        mask = cv2.inRange(frame, lower_brown, upper_brown)
        
        ox, oy, scx, scy = 0, 0, 1.0, 1.0
        found = False

        # 게임???수 ?라?언???역 ?기 (보통 1920x1080 ?도????1041 ??
        client_w, client_h = 1920, 1080
        if getattr(self.state, 'hwnd', None):
            try:
                _, _, cw, ch = win32gui.GetClientRect(self.state.hwnd)
                if cw > 0 and ch > 0:
                    client_w, client_h = cw, ch
            except: pass

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            sorted_cnts = sorted(contours, key=cv2.contourArea, reverse=True)[:3]
            for cnt in sorted_cnts:
                x, y, w, h = cv2.boundingRect(cnt)
                
                if w >= 800:
                    ratio = w / h
                    if 1.5 < ratio < 1.9:
                        # OBS 캡처???도?????바? ?외???라?언???역???습?다
                        scale = h / float(client_h)
                        ox, oy, scx, scy = x, y, scale, scale
                        _monitor_log(f"[Vision] 게임 ?면 감?: ({x}, {y}) {w}x{h} [?라?언??{client_h}p 기? 배율: {scale:.3f}x]")
                        found = True
                        break
        
        # 명확???곽?이 ?으?강제 ?라?언??배율 ?정
        if not found and fw == 1920:
             scale = 1080.0 / float(client_h)
             ox = int((1920 - (client_w * scale)) / 2)
             scx, scy = scale, scale
             found = True

        # ?? EMA(Exponential Moving Average) ?터 ?용 (지?링 방?) ??
        current = np.array([float(ox), float(oy), float(scx), float(scy)])
        if self.obs_ema_params is None:
            self.obs_ema_params = current
        else:
            # 급격??변???면 ?환 ?? 감? ??리셋 (?계?50px ?상 차이)
            diff = np.abs(self.obs_ema_params[:2] - current[:2])
            if np.any(diff > 50):
                self.obs_ema_params = current
            else:
                self.obs_ema_params = self.ema_alpha * current + (1.0 - self.ema_alpha) * self.obs_ema_params

        self.obs_params = tuple(self.obs_ema_params)
        return self.obs_params

    # ----------------------------------------------------------
    def load_roi(self):
        if os.path.exists(self.config_file):
            with open(self.config_file, "r", encoding="utf-8") as f:
                self.config_data = json.load(f)
                # 좌표 ?역?골라??변??(dict?고 'sx'가 ?는 경우?
                self.regions = {k: dict_to_region(v) for k, v in self.config_data.items()
                                if isinstance(v, dict) and 'sx' in v}
                _monitor_log(f"[DEBUG] Loaded {len(self.regions)} regions from config.json")
                _monitor_log(f"[DEBUG] Regions keys: {list(self.regions.keys())}")
        else:
            self.config_data = {}
            _monitor_log(f"[DEBUG] Config file not found: {self.config_file}")

    def load_maps(self):
        if os.path.exists(self.maps_file):
            with open(self.maps_file, "r", encoding="utf-8") as f:
                self.state.maps_db = json.load(f)
            _monitor_log(f"[Map] maps.json 로드 ?료 ({len(self.state.maps_db)}??.")
        else:
            _monitor_log("[Warn] maps.json 없음 ??Grid 기능 비활??")

    @staticmethod
    def _bbox_iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        a2x, a2y = ax + aw, ay + ah
        b2x, b2y = bx + bw, by + bh
        ix1 = max(ax, bx)
        iy1 = max(ay, by)
        ix2 = min(a2x, b2x)
        iy2 = min(a2y, b2y)
        if ix2 <= ix1 or iy2 <= iy1:
            return 0.0
        inter = float((ix2 - ix1) * (iy2 - iy1))
        union = float(max(1, aw * ah + bw * bh - int(inter)))
        return inter / union

    def _load_patterns(self):
        """patterns.json 파일에서 FindText 문자열들을 로드합니다."""
        import json
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "patterns.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    patterns = json.load(f)
                    self._parsed_pattern_cache = {}
                    return patterns
            except Exception as e:
                print(f"[FindText] patterns.json 로드 실패: {e}")
        return {}

    def _get_cached_ft_pattern(self, pattern_name: str) -> dict | None:
        ft_string = self.patterns.get(pattern_name)
        if not ft_string:
            return None

        cached = self._parsed_pattern_cache.get(pattern_name)
        if cached and cached.get("ft_string") == ft_string:
            return cached.get("parsed")

        parsed = self._parse_ft_string(ft_string)
        self._parsed_pattern_cache[pattern_name] = {
            "ft_string": ft_string,
            "parsed": parsed,
        }
        return parsed

    def _match_ft_pattern_in_crop(self, crop: np.ndarray, pattern_name: str, err_ratio: float = 0.08) -> dict | None:
        parsed = self._get_cached_ft_pattern(pattern_name)
        if parsed is None or crop is None or crop.size == 0:
            return None

        bitmap = parsed["bitmap"]
        len1 = parsed["len1"]
        p_w = parsed["width"]
        p_h = parsed["height"]
        if len1 <= 0 or crop.shape[0] < p_h or crop.shape[1] < p_w:
            return None

        binary_map = self._build_binary_map(crop, parsed["config"])
        result = cv2.matchTemplate(binary_map, bitmap, cv2.TM_CCORR)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        e1_max = max(1, int(len1 * err_ratio))
        threshold_val = len1 - e1_max
        if max_val < threshold_val:
            return None

        score = float(max_val) / max(1.0, float(len1))
        return {
            "name": parsed["name"],
            "score": score,
            "x": int(max_loc[0]),
            "y": int(max_loc[1]),
            "w": p_w,
            "h": p_h,
        }

    def _evaluate_party_user_info(
        self,
        crop: np.ndarray,
        err_ratio: float = 0.08,
        allowed_patterns: list[str] | None = None,
    ) -> dict:
        hits = []
        if allowed_patterns is None:
            party_patterns = list(getattr(self.state, "party_userinfo_patterns", []) or [])
        else:
            party_patterns = list(allowed_patterns or [])
        for key in party_patterns:
            hit = self._match_ft_pattern_in_crop(crop, key, err_ratio=err_ratio)
            if hit:
                hits.append(hit)

        hits.sort(key=lambda item: float(item.get("score", 0.0) or 0.0), reverse=True)
        best_hit = hits[0] if hits else None
        second_hit = hits[1] if len(hits) > 1 else None
        best_score = float(best_hit.get("score", 0.0) or 0.0) if best_hit else 0.0
        second_score = float(second_hit.get("score", 0.0) or 0.0) if second_hit else 0.0
        margin = best_score - second_score
        accepted_hit = None
        ambiguous = False

        if best_hit:
            if best_score >= 0.90 and (margin >= 0.03 or second_score < 0.88):
                accepted_hit = best_hit
            else:
                ambiguous = True

        return {
            "hits": hits,
            "best_hit": best_hit,
            "second_hit": second_hit,
            "best_score": best_score,
            "second_score": second_score,
            "margin": margin,
            "accepted_hit": accepted_hit,
            "ambiguous": ambiguous,
        }

    def _select_party_user_hit(self, hits: list[dict]) -> dict:
        sorted_hits = sorted(
            hits,
            key=lambda item: float(item.get("score", 0.0) or 0.0),
            reverse=True,
        )
        best_hit = sorted_hits[0] if sorted_hits else None
        second_hit = sorted_hits[1] if len(sorted_hits) > 1 else None
        best_score = float(best_hit.get("score", 0.0) or 0.0) if best_hit else 0.0
        second_score = float(second_hit.get("score", 0.0) or 0.0) if second_hit else 0.0
        margin = best_score - second_score
        accepted_hit = None
        ambiguous = False

        if best_hit:
            if best_score >= 0.90 and (margin >= 0.03 or second_score < 0.88):
                accepted_hit = best_hit
            else:
                ambiguous = True

        return {
            "hits": sorted_hits,
            "best_hit": best_hit,
            "second_hit": second_hit,
            "best_score": best_score,
            "second_score": second_score,
            "margin": margin,
            "accepted_hit": accepted_hit,
            "ambiguous": ambiguous,
        }

    def _evaluate_party_user_info_from_frame(
        self,
        frame,
        allowed_patterns: list[str] | None = None,
        err_ratio: float = 0.08,
    ) -> dict:
        crop = self._extract_region_crop(frame, "user_info")
        if crop is None:
            return self._select_party_user_hit([])
        return self._evaluate_party_user_info(
            crop,
            err_ratio=err_ratio,
            allowed_patterns=allowed_patterns,
        )

    def _extract_region_crop(self, frame, region_name: str, pad: int = 5):
        reg = self.regions.get(region_name)
        if reg is None or frame is None:
            return None

        ox, oy, scx, scy = self.obs_params
        fh, fw = frame.shape[:2]
        sx = max(0, int(reg.sx * scx + ox - pad))
        sy = max(0, int(reg.sy * scy + oy - pad))
        dx = min(fw, int(reg.dx * scx + ox + pad))
        dy = min(fh, int(reg.dy * scy + oy + pad))
        crop = frame[sy:dy, sx:dx]
        if crop.size == 0:
            return None

        target_w = reg.dx - reg.sx
        target_h = reg.dy - reg.sy
        if target_w > 0 and target_h > 0 and (crop.shape[1] != target_w or crop.shape[0] != target_h):
            crop = cv2.resize(crop, (target_w, target_h), interpolation=cv2.INTER_CUBIC)
        return crop

    def inspect_user_info_frame(self, frame) -> dict:
        info = {
            "crop_available": False,
            "status": "crop_unavailable",
            "hits": [],
            "best_hit": None,
            "second_hit": None,
            "best_score": 0.0,
            "second_score": 0.0,
            "margin": 0.0,
            "accepted_hit": None,
            "ambiguous": False,
            "bitwise_name": "",
            "bitwise_score": 0.0,
        }
        crop = self._extract_region_crop(frame, "user_info")
        if crop is None:
            return info

        info["crop_available"] = True
        info["status"] = "no_match"
        info.update(self._evaluate_party_user_info_from_frame(frame))
        if info["accepted_hit"]:
            info["status"] = "accepted"
        elif info["ambiguous"]:
            info["status"] = "ambiguous"
        else:
            bitwise_name, bitwise_score = self.matcher.recognize_entity_bitwise(crop, "users")
            info["bitwise_name"] = bitwise_name or ""
            info["bitwise_score"] = float(bitwise_score or 0.0)
            if info["bitwise_name"]:
                info["status"] = "bitwise"
        return info

    def find_pattern(self, frame, pattern_name, region_name="play_area"):
        """
        JSON에 등록된 패턴 이름을 사용하여 검색을 수행합니다. (고속 cv2.matchTemplate 방식)
        """
        if frame is None: return None
        
        ft_string = self.patterns.get(pattern_name)
        if not ft_string:
            # 실시간 추가 반영을 위해 재로드 시도
            self.patterns = self._load_patterns()
            ft_string = self.patterns.get(pattern_name)
            if not ft_string:
                return None

        # 1. ROI 크롭
        reg = self.regions.get(region_name)
        if reg:
            ox, oy, scx, scy = self.obs_params
            fh, fw = frame.shape[:2]
            sx = max(0, int(reg.sx * scx + ox))
            sy = max(0, int(reg.sy * scy + oy))
            dx = min(fw, int(reg.dx * scx + ox))
            dy = min(fh, int(reg.dy * scy + oy))
            search_frame = frame[sy:dy, sx:dx]
        else:
            sx = sy = 0
            search_frame = frame

        if search_frame.size == 0: return None

        # 2. FindText 문자열 파싱 (Color Mode / Gray Threshold 자동 판별)
        try:
            # 형식: |<이름>설정$너비.데이터
            import re as _re
            match = _re.search(r"<(.*?)>(.*?)\$(\d+)\.(.*)", ft_string)
            if not match: return None
            
            p_name, p_config, p_width, p_data = match.groups()
            p_width = int(p_width)
            
            # 비트맵 복원
            ahk_chars = "0123456789+/ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            c2v = {c: i for i, c in enumerate(ahk_chars)}
            bits = "".join(f"{c2v.get(ch, 0):06b}" for ch in p_data)
            bits = _re.sub(r"10*$", "", bits)
            p_height = len(bits) // p_width
            if p_height == 0: return None
            
            bitmap = np.array([1 if bits[i] == '1' else 0 for i in range(p_width * p_height)], dtype=np.float32).reshape(p_height, p_width)
            len1 = np.sum(bitmap)
            
            # 이진 지도(Binary Map) 생성
            if "*" in p_config:
                # (1) Gray Threshold 모드
                thr = int(p_config.replace("*", ""))
                gray = (search_frame[:,:,0].astype(np.uint32)*38 + 
                        search_frame[:,:,1].astype(np.uint32)*75 + 
                        search_frame[:,:,2].astype(np.uint32)*15)
                binary_map = (gray < (thr + 1) << 7).astype(np.float32)
            else:
                # (2) Color Similarity 모드
                parts = p_config.split("-")
                color_hex = parts[0]
                sim = float(parts[1]) if len(parts) > 1 else 0.90
                
                r_target = int(color_hex[0:2], 16)
                g_target = int(color_hex[2:4], 16)
                b_target = int(color_hex[4:6], 16)
                max_dist_sq = int(9 * 255 * 255 * (1 - sim) ** 2)
                
                dist_sq = ((search_frame[:,:,0].astype(np.int32) - r_target)**2 + 
                           (search_frame[:,:,1].astype(np.int32) - g_target)**2 + 
                           (search_frame[:,:,2].astype(np.int32) - b_target)**2)
                binary_map = (dist_sq <= max_dist_sq).astype(np.float32)

            # 3. OpenCV 고속 매칭 (TM_SQDIFF: 제곱 차이 방식 - 전경/배경 모두 체크하여 오탐 방지)
            # TM_SQDIFF는 일치할수록 값이 0에 가깝습니다.
            result = cv2.matchTemplate(binary_map, bitmap, cv2.TM_SQDIFF)
            min_val, _, min_loc, _ = cv2.minMaxLoc(result)

            # 허용 오차: 전체 비트맵 크기의 10~15% 미만으로 틀려야 함
            total_pixels = p_width * p_height
            error_threshold = total_pixels * 0.15  # 15% 허용
            
            score = 1.0 - (min_val / total_pixels)
            
            if min_val <= error_threshold:
                res = {
                    "name": p_name,
                    "x": min_loc[0] + sx,
                    "y": min_loc[1] + sy,
                    "w": p_width,
                    "h": p_height,
                    "score": score
                }
                
                # 시각화 마커 추가 (0.5초 유지)
                if hasattr(self.state, "visual_markers"):
                    with self.state._lock:
                        self.state.visual_markers.append({
                            "x": res["x"] + p_width // 2,
                            "y": res["y"] + p_height // 2,
                            "color": "cyan",  # 오탐 방지용 새로운 색상
                            "size": 20,
                            "expiry": time.time() + 0.5
                        })
                
                return res
            else:
                # [DEBUG] 실패 시 최고 점수 및 색상 정보 출력
                if VERBOSE_MONITOR_LOGS or pattern_name in ("hb", "red_tab"):
                    avg_c = np.mean(search_frame, axis=(0, 1))
                    print(f"[FindText] '{pattern_name}' 실패: Score={score:.2f} (Err:{int(min_val)}/{int(error_threshold)}) | Avg Color(RGB): {avg_c}")
        except Exception as e:
            print(f"[FindText] 검색 중 오류: {e}")
        return None

    # ----------------------------------------------------------
    # ── FIND PATTERN 확장 API ────────────────────────────────
    # ----------------------------------------------------------

    def _parse_ft_string(self, ft_string: str) -> dict | None:
        """
        FindText 문자열을 파싱하여 비트맵 dict 반환.
        반환값: {name, bitmap(float32 2D), width, height, len1, config}
        실패 시 None 반환.

        지원 형식:
            |<이름>*임계값$너비.데이터      (Gray Threshold 모드)
            |<이름>RRGGBB-유사도$너비.데이터 (Color Similarity 모드)
        """
        import re as _re
        match = _re.search(r"<(.*?)>(.*?)\$(\d+)\.(.*)", ft_string)
        if not match:
            return None
        p_name, p_config, p_width_str, p_data = match.groups()
        p_width = int(p_width_str)

        ahk_chars = "0123456789+/ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        c2v = {c: i for i, c in enumerate(ahk_chars)}
        bits = "".join(f"{c2v.get(ch, 0):06b}" for ch in p_data)
        bits = _re.sub(r"10*$", "", bits)
        p_height = len(bits) // p_width if p_width > 0 else 0
        if p_height == 0:
            return None

        bitmap = np.array(
            [1 if bits[i] == '1' else 0 for i in range(p_width * p_height)],
            dtype=np.float32
        ).reshape(p_height, p_width)

        return {
            "name":    p_name,
            "config":  p_config,
            "bitmap":  bitmap,
            "width":   p_width,
            "height":  p_height,
            "len1":    float(np.sum(bitmap)),
        }

    def _build_binary_map(self, search_frame: np.ndarray, p_config: str) -> np.ndarray:
        """
        search_frame + p_config 조합으로 이진 맵 생성.
        * 접두사  → Gray Threshold 모드
        그 외      → Color Similarity 모드
        """
        if "*" in p_config:
            thr = int(p_config.replace("*", ""))
            gray = (search_frame[:, :, 0].astype(np.uint32) * 38 +
                    search_frame[:, :, 1].astype(np.uint32) * 75 +
                    search_frame[:, :, 2].astype(np.uint32) * 15)
            return (gray < (thr + 1) << 7).astype(np.float32)
        else:
            parts = p_config.split("-")
            color_hex = parts[0]
            sim = float(parts[1]) if len(parts) > 1 else 0.90
            r_t = int(color_hex[0:2], 16)
            g_t = int(color_hex[2:4], 16)
            b_t = int(color_hex[4:6], 16)
            max_dist_sq = int(9 * 255 * 255 * (1 - sim) ** 2)
            dist_sq = (
                (search_frame[:, :, 0].astype(np.int32) - r_t) ** 2 +
                (search_frame[:, :, 1].astype(np.int32) - g_t) ** 2 +
                (search_frame[:, :, 2].astype(np.int32) - b_t) ** 2
            )
            return (dist_sq <= max_dist_sq).astype(np.float32)

    def _crop_roi(self, frame: np.ndarray, region_name: str) -> tuple:
        """
        region_name에 해당하는 영역을 크롭하고 (cropped, ox, oy) 반환.
        region 없으면 (frame, 0, 0) 반환.
        """
        reg = self.regions.get(region_name)
        if reg:
            obs_ox, obs_oy, scx, scy = self.obs_params
            fh, fw = frame.shape[:2]
            sx = max(0, int(reg.sx * scx + obs_ox))
            sy = max(0, int(reg.sy * scy + obs_oy))
            dx = min(fw, int(reg.dx * scx + obs_ox))
            dy = min(fh, int(reg.dy * scy + obs_oy))
            cropped = frame[sy:dy, sx:dx]
            return (cropped, sx, sy)
        return (frame, 0, 0)

    def find_pattern_all(
        self,
        frame: np.ndarray,
        pattern_name: str,
        region_name: str = "play_area",
        max_results: int = 20,
        overlap_px: int = 5,
        err_ratio: float = 0.15,
    ) -> list[dict]:
        """
        patterns.json 패턴을 사용하여 화면에서 **모든** 일치 위치를 탐색한다.

        Args:
            frame:         캡처 프레임 (RGB ndarray)
            pattern_name:  patterns.json 키 이름
            region_name:   검색 ROI (기본 'play_area')
            max_results:   최대 반환 개수 (기본 20)
            overlap_px:    동일 위치로 간주하는 픽셀 거리 (NMS 역할)
            err_ratio:     허용 오차율 (기본 0.15 = 15%)

        Returns:
            list of dict: [{"name", "x", "y", "w", "h", "score"}, ...]
                          x, y 는 전체 프레임 절대 좌표
        """
        if frame is None:
            return []

        ft_string = self.patterns.get(pattern_name)
        if not ft_string:
            self.patterns = self._load_patterns()
            ft_string = self.patterns.get(pattern_name)
        if not ft_string:
            return []

        parsed = self._parse_ft_string(ft_string)
        if parsed is None:
            return []

        search_frame, ox, oy = self._crop_roi(frame, region_name)
        if search_frame.size == 0:
            return []

        binary_map = self._build_binary_map(search_frame, parsed["config"])
        bitmap     = parsed["bitmap"]
        len1       = parsed["len1"]
        p_w, p_h   = parsed["width"], parsed["height"]

        if len1 == 0:
            return []

        # OpenCV 상관 매칭
        result = cv2.matchTemplate(binary_map, bitmap, cv2.TM_CCORR)
        e1_max = max(1, int(len1 * err_ratio))
        threshold_val = len1 - e1_max

        # 임계값 이상인 모든 위치 추출
        ys, xs = np.where(result >= threshold_val)

        matches = []
        for x_local, y_local in zip(xs, ys):
            score = float(result[y_local, x_local]) / len1
            matches.append({
                "name":  parsed["name"],
                "x":     int(x_local) + ox,
                "y":     int(y_local) + oy,
                "w":     p_w,
                "h":     p_h,
                "score": round(score, 4),
            })

        if not matches:
            return []

        # 단순 NMS: score 내림차순 정렬 후 overlap_px 이내 제거
        matches.sort(key=lambda m: m["score"], reverse=True)
        kept: list[dict] = []
        close_x = overlap_px
        close_y = overlap_px
        if pattern_name == "hb":
            close_x = max(overlap_px, int(p_w * 2.2))
            close_y = max(overlap_px, int(p_h * 2.8))
        for m in matches:
            too_close = any(
                abs(m["x"] - k["x"]) < close_x and abs(m["y"] - k["y"]) < close_y
                for k in kept
            )
            if not too_close:
                kept.append(m)
            if len(kept) >= max_results:
                break

        return kept

    def cluster_pattern_objects(
        self,
        matches: list[dict],
        distance_px: int = 14,
        max_results: int = 20,
    ) -> list[dict]:
        """
        동일 객체 주변에서 나온 다중 후보를 하나의 객체로 묶는다.
        가까운 중심점을 가진 후보들을 하나의 군집으로 보고 최고 점수 후보를 대표로 남긴다.
        """
        if not matches:
            return []

        clusters: list[dict] = []
        sorted_matches = sorted(matches, key=lambda m: float(m.get("score", 0.0) or 0.0), reverse=True)
        for match in sorted_matches:
            cx = float(match["x"]) + float(match.get("w", 0)) / 2.0
            cy = float(match["y"]) + float(match.get("h", 0)) / 2.0
            assigned = False
            for cluster in clusters:
                if abs(cx - cluster["cx"]) <= distance_px and abs(cy - cluster["cy"]) <= distance_px:
                    cluster["members"].append(match)
                    if float(match.get("score", 0.0) or 0.0) > float(cluster["best"].get("score", 0.0) or 0.0):
                        cluster["best"] = match
                    member_count = len(cluster["members"])
                    cluster["cx"] = ((cluster["cx"] * (member_count - 1)) + cx) / member_count
                    cluster["cy"] = ((cluster["cy"] * (member_count - 1)) + cy) / member_count
                    assigned = True
                    break
            if not assigned:
                clusters.append({
                    "cx": cx,
                    "cy": cy,
                    "best": match,
                    "members": [match],
                })

        objects: list[dict] = []
        for cluster in clusters[:max_results]:
            best = dict(cluster["best"])
            best["count"] = len(cluster["members"])
            best["cx"] = round(cluster["cx"], 1)
            best["cy"] = round(cluster["cy"], 1)
            objects.append(best)
        return objects

    def find_pattern_wait(
        self,
        pattern_name: str,
        region_name: str = "play_area",
        timeout: float = 5.0,
        appear: bool = True,
        poll_interval: float = 0.05,
        err_ratio: float = 0.15,
    ) -> dict | None:
        """
        패턴이 **나타날 때까지** (또는 사라질 때까지) 대기한다.

        Args:
            pattern_name:  patterns.json 키
            region_name:   검색 ROI
            timeout:       최대 대기 시간 (초, 기본 5.0)
            appear:        True → 나타날 때까지 / False → 사라질 때까지
            poll_interval: 검사 주기 (초, 기본 0.05)
            err_ratio:     허용 오차율

        Returns:
            패턴이 조건을 충족하면 find_pattern 결과 dict, 타임아웃이면 None.

        사용 예:
            # 체력바 빨간 탭이 나타날 때까지 최대 3초 대기
            res = monitor.find_pattern_wait("hb", timeout=3.0, appear=True)
            if res:
                print(f"발견: {res['x']}, {res['y']}")
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            frame = getattr(self.state, "last_frame", None)
            if frame is not None:
                res = self.find_pattern(frame, pattern_name, region_name)
                found = res is not None
                if appear and found:
                    return res
                if not appear and not found:
                    return {"name": pattern_name, "x": -1, "y": -1, "w": 0, "h": 0, "score": 0.0}
            time.sleep(poll_interval)

        # 타임아웃
        print(f"[FindPattern] '{pattern_name}' 대기 타임아웃 ({timeout}s)")
        return None

    def find_pattern_region(
        self,
        frame: np.ndarray,
        pattern_name: str,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        err_ratio: float = 0.15,
        find_all: bool = False,
        max_results: int = 20,
        overlap_px: int = 5,
    ) -> dict | list | None:
        """
        **임의 픽셀 좌표** 직접 지정 버전.
        regions dict 없이 x1,y1,x2,y2로 ROI를 직접 지정한다.

        Args:
            frame:        캡처 프레임 (RGB ndarray)
            pattern_name: patterns.json 키
            x1, y1:       검색 영역 좌상단 (절대 픽셀)
            x2, y2:       검색 영역 우하단 (절대 픽셀)
            err_ratio:    허용 오차율
            find_all:     True → 모든 결과 리스트 / False → 첫 번째 결과 dict
            max_results:  find_all=True 일 때 최대 반환 수
            overlap_px:   NMS 픽셀 거리

        Returns:
            find_all=False: dict or None
            find_all=True:  list[dict]

        사용 예:
            # 화면 좌상단 400×200 영역에서 "boss_hp" 패턴 탐색
            res = monitor.find_pattern_region(frame, "boss_hp", 0, 0, 400, 200)
        """
        if frame is None:
            return [] if find_all else None

        ft_string = self.patterns.get(pattern_name)
        if not ft_string:
            self.patterns = self._load_patterns()
            ft_string = self.patterns.get(pattern_name)
        if not ft_string:
            return [] if find_all else None

        parsed = self._parse_ft_string(ft_string)
        if parsed is None:
            return [] if find_all else None

        # ROI 크롭 (직접 지정)
        fh, fw = frame.shape[:2]
        sx = max(0, x1);  sy = max(0, y1)
        dx = min(fw, x2); dy = min(fh, y2)
        search_frame = frame[sy:dy, sx:dx]
        if search_frame.size == 0:
            return [] if find_all else None

        binary_map = self._build_binary_map(search_frame, parsed["config"])
        bitmap     = parsed["bitmap"]
        len1       = parsed["len1"]
        p_w, p_h   = parsed["width"], parsed["height"]

        if len1 == 0:
            return [] if find_all else None

        result = cv2.matchTemplate(binary_map, bitmap, cv2.TM_CCORR)
        e1_max = max(1, int(len1 * err_ratio))
        threshold_val = len1 - e1_max

        if not find_all:
            # 단일 결과: 가장 높은 위치만 반환
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            if max_val >= threshold_val:
                return {
                    "name":  parsed["name"],
                    "x":     max_loc[0] + sx,
                    "y":     max_loc[1] + sy,
                    "w":     p_w,
                    "h":     p_h,
                    "score": round(float(max_val) / len1, 4),
                }
            return None

        # 다중 결과
        ys, xs = np.where(result >= threshold_val)
        matches = []
        for x_local, y_local in zip(xs, ys):
            score = float(result[y_local, x_local]) / len1
            matches.append({
                "name":  parsed["name"],
                "x":     int(x_local) + sx,
                "y":     int(y_local) + sy,
                "w":     p_w,
                "h":     p_h,
                "score": round(score, 4),
            })

        matches.sort(key=lambda m: m["score"], reverse=True)
        kept: list[dict] = []
        for m in matches:
            too_close = any(
                abs(m["x"] - k["x"]) < overlap_px and abs(m["y"] - k["y"]) < overlap_px
                for k in kept
            )
            if not too_close:
                kept.append(m)
            if len(kept) >= max_results:
                break

        return kept

    def check_red_tab_with_findtext(self, frame):
        """
        ft.ahk Color Mode (유사도 방식)를 Python으로 직접 구현하여
        play_area에서 red_tab 이미지를 검색합니다.

        FindText 문자열: |<redtab>FF5757-0.90$49.zzzzzzzzzzzzzzzzs
          - FF5757  = 전경색 RGB (빨간색)
          - -0.90   = ft.ahk 유사도: max_dist² = floor(9*255²*(1-0.90)²) = 5852
          - $49.    = 너비 49픽셀
          - 배경색 없음 → 전경픽셀(1)만 체크
        """
        if frame is None:
            print("[RedTab] 프레임 데이터가 없어 검색을 수행할 수 없습니다.")
            return False

        # ── 1. play_area 영역 크롭 ──
        pa_reg = self.regions.get("play_area")
        if pa_reg:
            ox, oy, scx, scy = self.obs_params
            fh, fw = frame.shape[:2]
            sx = max(0, int(pa_reg.sx * scx + ox))
            sy = max(0, int(pa_reg.sy * scy + oy))
            dx = min(fw, int(pa_reg.dx * scx + ox))
            dy = min(fh, int(pa_reg.dy * scy + oy))
            search_frame = frame[sy:dy, sx:dx]
            if search_frame.size == 0:
                search_frame = frame
        else:
            search_frame = frame

        # ── 2. ft.ahk AHK base64 디코더 ──
        ahk_chars = "0123456789+/ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        char_to_val = {c: i for i, c in enumerate(ahk_chars)}

        encoded = "zzzzzzzzzzzzzzzzs"
        width = 49

        bits_str = ""
        for ch in encoded:
            val = char_to_val.get(ch, 0)
            bits_str += f"{val:06b}"
        import re as _re
        bits_str = _re.sub(r'10*$', '', bits_str)

        height = len(bits_str) // width
        if height == 0:
            print("[RedTab] 비트맵 파싱 실패")
            return False

        total = width * height
        bitmap = np.zeros((height, width), dtype=np.uint8)
        for idx in range(min(total, len(bits_str))):
            bitmap[idx // width, idx % width] = 1 if bits_str[idx] == '1' else 0

        # ── 3. ft.ahk 유사도 방식 Color Mode 매칭 ──
        # frame은 RGB 형식 (CaptureSvc에서 BGR→RGB 변환됨)
        # similarity=0.90 → max_dist_sq = floor(9 * 255² * (1-0.90)²)
        FG_R, FG_G, FG_B = 0xFF, 0x57, 0x57  # FF5757 (빨간)
        similarity = 0.90
        max_dist_sq = int(9 * 255 * 255 * (1 - similarity) ** 2)  # = 5852
        ERR1 = 0.15  # 전경 픽셀 오차 허용율 (15%)

        sr = search_frame[:, :, 0].astype(np.int32)  # R (RGB 프레임)
        sg = search_frame[:, :, 1].astype(np.int32)  # G
        sb = search_frame[:, :, 2].astype(np.int32)  # B
        sh, sw = search_frame.shape[:2]

        # 전경 픽셀 인덱스
        y1, x1 = np.where(bitmap == 1)
        len1 = len(y1)

        if len1 < 5:
            print(f"[RedTab] 전경 픽셀 수 부족: {len1}개 (최소 5 필요)")
            return False

        e1_max = max(1, int(len1 * ERR1))

        # 유사도 방식: 유클리드 거리² 맵 생성
        # dist² = (R-FG_R)² + (G-FG_G)² + (B-FG_B)²
        dist_sq_map = (
            (sr - FG_R) ** 2 +
            (sg - FG_G) ** 2 +
            (sb - FG_B) ** 2
        )
        # 전경색 픽셀 맵: dist² <= max_dist_sq인 경우 1 (매칭)
        fg_map = (dist_sq_map <= max_dist_sq).astype(np.uint8)

        ph, pw = height, width
        if ph > sh or pw > sw:
            print(f"[RedTab] 패턴({pw}x{ph})이 검색 영역({sw}x{sh})보다 큼")
            return False

        # ── cv2.matchTemplate으로 고속 검색 ──
        # 원리: CCORR = 각 위치에서 fg_map과 pattern_bin의 내적합
        #   score[y,x] = sum(fg_map[y:y+ph, x:x+pw] * pattern_bin)
        #   score 최대값 = len1 (전경픽셀 모두 일치)
        #   mismatch1 = len1 - score → score >= (len1 - e1_max) 이면 매칭
        pattern_bin = bitmap.astype(np.float32)
        fg_map_f = fg_map.astype(np.float32)
        result = cv2.matchTemplate(fg_map_f, pattern_bin, cv2.TM_CCORR)

        threshold_score = float(len1 - e1_max)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        found = max_val >= threshold_score
        found_x, found_y = max_loc if found else (-1, -1)
        best_mismatch = int(len1 - max_val)

        # ── 4. 결과 출력 및 처리 ──
        if not found:
            print(f"[RedTab] 결과: 빨간색 탭 없음 "
                  f"(fg_px={len1}, best_miss={best_mismatch}, e1_max={e1_max}, "
                  f"max_dist_sq={max_dist_sq}) -> Tab x2 입력")
            try:
                from bis_core import hw, humanized_sleep
                hw.humanized_press("tab")
                humanized_sleep(0.3, variance=0.1)
                hw.humanized_press("tab")
            except Exception as e:
                print(f"[RedTab] 하드웨어 입력 오류: {e}")
            return False

        print(f"[RedTab] 결과: 빨간색 탭 발견! "
              f"(좌표: {found_x}, {found_y}, mismatch={best_mismatch}/{e1_max})")
        return True


    def _find_red_tab_candidates(
        self,
        play_area_rgb: np.ndarray,
        region: Optional[tuple[int, int, int, int]] = None,
        promotion_mode: bool = False,
    ) -> list[dict]:
        """Return red-tab box candidates within play_area or a clipped sub-region."""
        if play_area_rgb is None or play_area_rgb.size == 0:
            return []

        full_h, full_w = play_area_rgb.shape[:2]
        if region is not None:
            x1, y1, x2, y2 = region
            x1 = max(0, min(full_w, int(x1)))
            y1 = max(0, min(full_h, int(y1)))
            x2 = max(0, min(full_w, int(x2)))
            y2 = max(0, min(full_h, int(y2)))
            if x2 <= x1 or y2 <= y1:
                return []
            crop = play_area_rgb[y1:y2, x1:x2]
        else:
            x1 = y1 = 0
            crop = play_area_rgb

        if crop is None or crop.size == 0:
            return []

        try:
            hsv = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)
        except Exception:
            return []

        if promotion_mode:
            red1 = cv2.inRange(hsv, np.array([0, 40, 40]), np.array([28, 255, 255]))
            red2 = cv2.inRange(hsv, np.array([160, 40, 40]), np.array([180, 255, 255]))
        else:
            red1 = cv2.inRange(hsv, np.array([0, 70, 70]), np.array([18, 255, 255]))
            red2 = cv2.inRange(hsv, np.array([166, 70, 70]), np.array([180, 255, 255]))
        mask = cv2.bitwise_or(red1, red2)

        kernel = np.ones((3, 3), np.uint8)
        mask = cv2.GaussianBlur(mask, (3, 3), 0)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
        mask = cv2.dilate(mask, kernel, iterations=1)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates: list[dict] = []
        for c in contours:
            area = float(cv2.contourArea(c))
            if area < (12.0 if promotion_mode else 20.0):
                continue

            x, y, w, h = cv2.boundingRect(c)
            if w < (8 if promotion_mode else 12) or h < (8 if promotion_mode else 12):
                continue
            if w > 420 or h > 420:
                continue

            aspect = w / float(max(1, h))
            if aspect < 0.35 or aspect > 3.50:
                continue

            bbox_area = float(max(1, w * h))
            roi = mask[y:y + h, x:x + w]
            red_pixels = float(cv2.countNonZero(roi))
            if red_pixels < (10.0 if promotion_mode else 18.0):
                continue

            fill_ratio = red_pixels / bbox_area
            if fill_ratio < (0.005 if promotion_mode else 0.008):
                continue

            perimeter = float(max(1, 2 * (w + h)))
            edge_density = red_pixels / perimeter
            rectangularity = min(1.0, area / bbox_area)

            score = (
                red_pixels * 1.0
                + edge_density * 14.0
                + fill_ratio * 120.0
                + rectangularity * 20.0
            )

            candidates.append({
                "x": int(x1 + x),
                "y": int(y1 + y),
                "w": int(w),
                "h": int(h),
                "cx": int(x1 + x + (w // 2)),
                "cy": int(y1 + y + (h // 2)),
                "area": area,
                "red_pixels": red_pixels,
                "fill_ratio": fill_ratio,
                "score": score,
            })

        candidates.sort(key=lambda c: c["score"], reverse=True)
        return candidates

    def _is_red_tab_promotion_mode(self) -> bool:
        if bool(getattr(self.state, "red_tab_promotion_active", False)):
            return True
        promotion_until = float(getattr(self.state, "red_tab_promotion_until", 0.0) or 0.0)
        return time.time() < promotion_until

    def _detect_red_tab_in_target_info(self, target_info_rgb: np.ndarray) -> bool:
        if target_info_rgb is None or target_info_rgb.size == 0:
            return False
        try:
            hsv = cv2.cvtColor(target_info_rgb, cv2.COLOR_RGB2HSV)
        except Exception:
            return False

        promotion_mode = self._is_red_tab_promotion_mode()
        if promotion_mode:
            warm1 = cv2.inRange(hsv, np.array([0, 35, 35]), np.array([28, 255, 255]))
            warm2 = cv2.inRange(hsv, np.array([160, 35, 35]), np.array([180, 255, 255]))
        else:
            warm1 = cv2.inRange(hsv, np.array([0, 50, 50]), np.array([22, 255, 255]))
            warm2 = cv2.inRange(hsv, np.array([166, 50, 50]), np.array([180, 255, 255]))
        mask = cv2.bitwise_or(warm1, warm2)
        mask = cv2.GaussianBlur(mask, (3, 3), 0)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)

        h, w = mask.shape[:2]
        if h <= 0 or w <= 0:
            return False

        bw = max(2, int(w * 0.16))
        bh = max(2, int(h * 0.22))
        border_mask = np.zeros_like(mask)
        border_mask[:bh, :] = 255
        border_mask[-bh:, :] = 255
        border_mask[:, :bw] = 255
        border_mask[:, -bw:] = 255
        inner_mask = np.zeros_like(mask)
        if h > (bh * 2) and w > (bw * 2):
            inner_mask[bh:h - bh, bw:w - bw] = 255

        border_red = float(cv2.countNonZero(cv2.bitwise_and(mask, border_mask)))
        border_area = float(max(1, cv2.countNonZero(border_mask)))
        border_ratio = border_red / border_area
        inner_red = float(cv2.countNonZero(cv2.bitwise_and(mask, inner_mask)))
        inner_area = float(max(1, cv2.countNonZero(inner_mask)))
        inner_ratio = inner_red / inner_area if inner_area > 0 else 0.0

        min_border_red = 14.0 if promotion_mode else 18.0
        min_border_ratio = 0.020 if promotion_mode else 0.028
        if border_red < min_border_red or border_ratio < min_border_ratio:
            return False
        if inner_ratio > (border_ratio * 1.10):
            return False
        return True

    def _detect_red_tab(self, play_area_rgb: np.ndarray, target_info_hint: bool = False) -> bool:
        """Detect the red-tab target box directly from play_area."""
        if play_area_rgb is None or play_area_rgb.size == 0:
            return self._red_tab_state

        full_h, full_w = play_area_rgb.shape[:2]
        promotion_mode = self._is_red_tab_promotion_mode()
        search_regions: list[tuple[int, int, int, int]] = []

        if self._red_tab_last_bbox:
            lx, ly, lw, lh = self._red_tab_last_bbox
            pad_x = max(40, int(lw * 0.80))
            pad_y = max(40, int(lh * 0.80))
            search_regions.append((lx - pad_x, ly - pad_y, lx + lw + pad_x, ly + lh + pad_y))

        search_regions.append((0, 0, full_w, full_h))

        best = None
        for region in search_regions:
            candidates = self._find_red_tab_candidates(play_area_rgb, region, promotion_mode=promotion_mode)
            if not candidates:
                continue
            best = candidates[0]
            break

        now = time.time()
        if target_info_hint and promotion_mode:
            self._red_tab_hit_streak = max(1, self._red_tab_hit_streak + 1)
            self._red_tab_miss_streak = 0
            self._red_tab_state = True
            self._red_tab_last_log_time = now
            return True

        if best:
            bbox = (best["x"], best["y"], best["w"], best["h"])
            if self._red_tab_last_bbox:
                prev = self._red_tab_last_bbox
                prev_cx = prev[0] + (prev[2] / 2.0)
                prev_cy = prev[1] + (prev[3] / 2.0)
                dist = ((best["cx"] - prev_cx) ** 2 + (best["cy"] - prev_cy) ** 2) ** 0.5
                prev_diag = max(1.0, (prev[2] ** 2 + prev[3] ** 2) ** 0.5)
                iou = self._bbox_iou(bbox, prev)
                if dist <= max(28.0, prev_diag * 0.9) or iou >= 0.10:
                    self._red_tab_hit_streak += 1
                else:
                    self._red_tab_hit_streak = 1
            else:
                self._red_tab_hit_streak = 1

            self._red_tab_miss_streak = 0
            self._red_tab_last_bbox = bbox
            self._red_tab_last_candidate = best

            if self._red_tab_hit_streak >= 1:
                if not self._red_tab_state and now - self._red_tab_last_log_time >= 0.5:
                    _monitor_log(
                        f"[RedTab] ON bbox={bbox} score={best['score']:.1f} "
                        f"red={best['red_pixels']:.0f}"
                    )
                    self._red_tab_last_log_time = now
                self._red_tab_state = True
            return self._red_tab_state

        self._red_tab_hit_streak = 0
        self._red_tab_miss_streak += 1
        miss_limit = 3 if promotion_mode else 2
        if self._red_tab_miss_streak >= miss_limit:
            if self._red_tab_state and now - self._red_tab_last_log_time >= 0.5:
                _monitor_log("[RedTab] OFF")
                self._red_tab_last_log_time = now
            self._red_tab_state = False
            self._red_tab_last_bbox = None
            self._red_tab_last_candidate = None
        return self._red_tab_state

    def _build_gps_debug_mask(self, play_area_rgb: np.ndarray) -> np.ndarray:
        """my_arrow 탐색 실패 원인 파악용 이진 마스크 생성."""
        if play_area_rgb is None or play_area_rgb.size == 0:
            return np.zeros((1, 1), dtype=np.uint8)
        # marker 전용 형광 초록 범위와 동일하게 마스크 생성
        hsv = cv2.cvtColor(play_area_rgb, cv2.COLOR_RGB2HSV)
        mask = cv2.inRange(hsv, np.array([50, 100, 150]), np.array([70, 255, 255]))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        return mask

    def _extract_shape_candidates(self, play_area_rgb: np.ndarray) -> list[dict]:
        """
        1차 필터: 객체 후보 바운딩 박스 추출
        """
        if play_area_rgb is None or play_area_rgb.size == 0:
            return []

        gray = cv2.cvtColor(play_area_rgb, cv2.COLOR_RGB2GRAY)
        # 표준 전처리 (템플릿과 동일 파이프라인)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        blurred = cv2.GaussianBlur(enhanced, (3, 3), 0)
        _, binary = cv2.threshold(blurred, self.bin_threshold, 255, cv2.THRESH_BINARY)

        out = []
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # config.json 적용
        b_filter = getattr(self, "config_data", {}).get("blob_size_filter", {})
        min_w = b_filter.get("min_w", 16)
        min_h = b_filter.get("min_h", 20)
        max_w = b_filter.get("max_w", 200)
        max_h = b_filter.get("max_h", 200)

        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            # 설정한 크기 필터 규격 검사
            if w < min_w or h < min_h or w > max_w or h > max_h:
                continue

            out.append({
                "type": "shape",
                "x": int(x),
                "y": int(y),
                "w": int(w),
                "h": int(h),
            })

        # y좌표 기준 정렬 (위에서 아래로 읽기)
        out.sort(key=lambda b: b["y"])
        return out

    # ----------------------------------------------------------
    def _calc_grid(self, abs_x: int, abs_y: int):
        """
        게임 절대 좌표를 Grid(격자) 좌표로 변환
        GridX = (ScreenX - PlayArea_SX) // grid_size
        config.json의 play_area 사용 (캘리브레이션 기준)
        """
        import json
        import os
        SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_file = os.path.join(SCRIPT_DIR, "config.json")
        # 확정 오프셋 기본값: sx=272, sy=28
        pa_sx, pa_sy = 272, 28
        grid_size = 48
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    conf = json.load(f)
                    pa = conf.get("play_area", {})
                    pa_sx = pa.get("sx", 272)
                    pa_sy = pa.get("sy", 28)
            except Exception:
                pass
        # 확정 48px: GridX = (abs_x - pa_sx) // 48
        gx = (abs_x - pa_sx) // grid_size
        gy = (abs_y - pa_sy) // grid_size
        return (int(gx), int(gy))

    # ----------------------------------------------------------
    def run(self):
        # CaptureSvc updates state.last_frame / state.last_frame_time.
        # MonitorSvc only consumes the shared frames (do not open another VideoCapture here).
        self.cap = None
        self.camera_index = -1

        self.frame_count = 0
        self.last_fps_time = time.time()
        self.fps_frames = 0
        
        while self.state.running:
            start_time = time.time()
            self.frame_count += 1
            self.fps_frames += 1

            # FPS 계산
            now = time.time()
            if now - self.last_fps_time >= 1.0:
                self.state.ocr_fps = self.fps_frames / (now - self.last_fps_time)
                self.fps_frames = 0
                self.last_fps_time = now

            # FPS 제한 제거 (최대 속도 모드)
            # elapsed = now - start_time
            # if elapsed < 0.008:
            #     time.sleep(0.008 - elapsed)
            # [CaptureSvc 연동] 프레임 획득은 CaptureSvc에서 수행되므로 여기서는 패스합니다.

            # ?? Hot-Reload ??????????????????????????????????
            if self.state.last_update_time > self.last_load_time:
                self.load_roi()
                self.load_maps()
                # ?플릿도 ?시?리로??
                tmpl_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temple")
                self.matcher.reload(tmpl_root)
                
                self.last_load_time = time.time()
                self.last_hashes.clear()
                self.last_values.clear() # 이전 ?캐시??비워??즉시 갱신 ?도
                _monitor_log("[Update] [Sync] ?정 ??플??시?갱신 ?료.")

            if not self.state.ocr_enabled:
                time.sleep(0.5)
                continue

            # ?? 게임 ?탐색 ?????????????????????????????????
            # svc_worker ??WIN_KEY ????동???해 'ory' ??탐색??추?
            hwnd = find_game_window("바람") or find_game_window("ory") or find_game_window("AION")
            if not hwnd:
                self.state.hwnd = None
                time.sleep(1)
                continue

            self.state.hwnd = hwnd
            
            # 게임창 비활성화 감지 시 Panic Release 방지
            if win32gui.GetForegroundWindow() != hwnd:
                # 비상 정지 실행 (Panic Release 제외)
                if hasattr(self.state, "f1_route_active"): self.state.f1_route_active = False
                if hasattr(self.state, "service_active"): self.state.service_active = False
                
                # Panic Release 호출 제거 - 게임창 비활성화 시에도 하드웨어 신호 유지
                _monitor_log("[Warn] 게임창 비활성화 감지 - 서비스만 중단하고 하드웨어 신호는 유지합니다.")
            
            frame_time = getattr(self.state, "last_frame_time", 0)
            if frame_time <= self.last_frame_time:
                time.sleep(0.005)
                continue
            
            self.last_frame_time = frame_time

            # ?? 기???고정 ??????
            ox, oy, scx, scy = self.obs_params

            # CaptureSvc에서 프레임 가져오기 (last_frame 사용)
            img_np = getattr(self.state, 'last_frame', None)
            if img_np is None:
                time.sleep(0.01)
                continue

            # [MonitorSvc] 숫자 필드 처리는 이제 NumericFieldScanner 스레드에서 전담합니다.
            # 중복 실행 방지를 위해 이 섹션을 비워둡니다.
            res_updates = {}
            
            # CaptureSvc is the single producer of the shared frame; avoid full-frame copies here.
            self.last_frame = img_np
            
            # obs_params도 GameState에 저장
            self.state.obs_params = self.obs_params

            # OCR preview: keep a reference; if overlay is enabled, draw on a copy
            # (do not mutate the shared capture frame).
            if self.state.grid_overlay_enabled:
                img_np = img_np.copy()
            self.state.ocr_preview_img = img_np
            
            # 격자 ?버?이 ?성
            if self.state.grid_overlay_enabled:
                h, w = img_np.shape[:2]
                
                # ── 확정 오프셋: sx=276, sy=32 / 고정 48.2 ──
                # GridX = (PixelX - 276) / 48.2, GridY = (PixelY - 32) / 48.2
                config_file_grid = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
                grid_start_x = 276  # 확정 오프셋
                grid_start_y = 32   # 확정 오프셋
                tile_size = 48.2    # float (누적오차 방지)
                if os.path.exists(config_file_grid):
                    try:
                        import json as _json_grid
                        with open(config_file_grid, 'r', encoding='utf-8') as f_grid:
                            cfg_grid = _json_grid.load(f_grid)
                            pa = cfg_grid.get("play_area", {})
                            grid_start_x = pa.get("sx", 276)
                            grid_start_y = pa.get("sy", 32)
                            tile_size = float(pa.get("grid_size", 48.2))
                    except Exception:
                        pass
                
                # 격자 ???한 (가?19?a1~S1, ?로 17?a1~a17)
                grid_cols = 19
                grid_rows = 17
                
                # 빨간???을 캐릭???치(J9, col=9, row=8)??동
                char_grid_x = 9
                char_grid_y = 8
                char_x = round(grid_start_x + char_grid_x * tile_size + tile_size / 2)
                char_y = round(grid_start_y + char_grid_y * tile_size + tile_size / 2)
                cv2.circle(img_np, (char_x, char_y), 3, (0, 0, 255), -1)
                
                # 격자 라인 그리기 (float tile_size → round)
                for i in range(grid_cols + 1):
                    x = round(grid_start_x + i * tile_size)
                    cv2.line(img_np, (x, round(grid_start_y)), (x, round(grid_start_y + grid_rows * tile_size)), (255, 255, 255), 1)
                
                # 격자 라인 그리기 (17행 row=16 제외)
                for i in range(grid_rows + 1):
                    if i == 16:  # 17행 건너뜀
                        continue
                    y = round(grid_start_y + i * tile_size)
                    cv2.line(img_np, (round(grid_start_x), y), (round(grid_start_x + grid_cols * tile_size), y), (255, 255, 255), 1)
                
                # ?두?칸에 4px 간격 추? 격자 그리?
                # a??(col=0), r??(col=17), 1??(row=0), 17??(row=16)
                border_cols = [0, grid_cols - 1]  # a?? r??
                border_rows = [0, grid_rows - 1]  # 1?? 17??
                fine_spacing = 4
                
                # 테두리 열에 세로로 추가 격자 (테두리 칸 내부)
                for col in border_cols:
                    col_start_x = round(grid_start_x + col * tile_size)
                    col_end_x = round(grid_start_x + (col + 1) * tile_size)
                    for i in range(0, int(grid_rows * tile_size), fine_spacing):
                        y = round(grid_start_y) + i
                        cv2.line(img_np, (col_start_x, y), (col_end_x, y), (255, 255, 255), 1)
                
                # 테두리 행에 가로로 추가 격자 (테두리 칸 내부)
                for row in border_rows:
                    row_start_y = round(grid_start_y + row * tile_size)
                    row_end_y = round(grid_start_y + (row + 1) * tile_size)
                # [성능 최적화] 매 프레임 그리드 그리기는 병목의 원인입니다. (필요시에만 가동)
                # if getattr(self.state, 'show_grid', False):
                #     for row in range(grid_rows):
                #         for col in range(grid_cols):
                #             grid_x, grid_y = col, row
                #             col_name = chr(ord('A') + grid_x)
                #             row_name = str(grid_y + 1)
                #             grid_name = f"{col_name}{row_name}"
                #             x = round(grid_start_x + grid_x * tile_size + tile_size / 2)
                #             y = round(grid_start_y + grid_y * tile_size + tile_size / 2)
                #             cv2.putText(img_np, grid_name, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (255, 255, 255), 1)
                self.state.ocr_preview_img = img_np

            # 숫자 필드 중복 처리부 제거 (최상단으로 이동됨)
            # res_updates = {}
            h_trig_active = False
            m_trig_active = False
            target_info_red_tab_hint = False

            # 다른 필드 처리
            for name, reg in self.regions.items():
                # 숫자 필드는 이미 처리했으므로 스킵
                if name in self.NUMERIC_FIELDS:
                    continue

                now_roi = time.time()
                if not self._should_process_roi(name, now_roi):
                    continue

                # 좌표 계산 및 crop 추출
                ox, oy, scx, scy = self.obs_params
                fh, fw = img_np.shape[:2]
                pad = 0 if name == "map_info" else 5  # map_info는 패딩 없이 정확히 crop
                sx = max(0, int(reg.sx * scx + ox - pad))
                sy = max(0, int(reg.sy * scy + oy - pad))
                dx = min(fw, int(reg.dx * scx + ox + pad))
                dy = min(fh, int(reg.dy * scy + oy + pad))

                # hp_trig/mp_trig are point-style ROIs in config (often dx<=sx, dy<=sy).
                is_point = (reg.dx <= reg.sx) and (reg.dy <= reg.sy)
                if is_point:
                    try:
                        px = int(reg.sx * scx + ox)
                        py = int(reg.sy * scy + oy)
                        # Sample a tiny neighborhood for stability (vs. a single pixel).
                        r = 2
                        x0 = max(0, px - r)
                        x1 = min(fw - 1, px + r)
                        y0 = max(0, py - r)
                        y1 = min(fh - 1, py + r)
                        patch = img_np[y0:y1 + 1, x0:x1 + 1]
                        dark = (patch[:, :, 0] < 55) & (patch[:, :, 1] < 55) & (patch[:, :, 2] < 55)
                        is_dark = float(dark.mean()) >= 0.60
                        if name == "hp_trig":
                            h_trig_active = is_dark
                        elif name == "mp_trig":
                            m_trig_active = is_dark
                    except Exception:
                        pass
                    continue

                crop = img_np[sy:dy, sx:dx]
                if crop.size == 0: continue

                target_w, target_h = reg.dx - reg.sx, reg.dy - reg.sy
                if target_w > 0 and target_h > 0:
                    if crop.shape[1] != target_w or crop.shape[0] != target_h:
                        # map_info는 도트 폰트 → NEAREST 필수 (CUBIC이면 FindText 매칭 붕괴)
                        interp = cv2.INTER_NEAREST if name == "map_info" else cv2.INTER_CUBIC
                        crop = cv2.resize(crop, (target_w, target_h), interpolation=interp)
                else:
                    continue

                cur_hash = self._fast_crop_signature(crop)
                # map_info는 상태 로그/재시도를 위해 해시 스킵에서 제외
                if (
                    name != "map_info"
                    and self.last_hashes.get(name) == cur_hash
                    and name in self.last_values
                    and name not in ("play_area", "target_info")
                ):
                    continue
                self.last_hashes[name] = cur_hash

                # [타겟 정보 인식] - 몬스터/유저 판정 + red-tab 색상 감지
                if name == "target_info":
                    monster_name, monster_score = self.matcher.recognize_entity_bitwise(crop, "monsters")
                    user_name, user_score = self.matcher.recognize_entity_bitwise(crop, "users")
                    target_info_red_tab_hint = self._detect_red_tab_in_target_info(crop)

                    if monster_name:
                        res_updates["target_kind"] = "MONSTER"
                        res_updates["target_name"] = monster_name
                        res_updates["target_info_text"] = monster_name
                        res_updates["target_score"] = monster_score
                    elif user_name:
                        res_updates["target_kind"] = "USER"
                        res_updates["target_name"] = user_name
                        res_updates["target_info_text"] = user_name
                        res_updates["target_score"] = user_score
                    else:
                        res_updates["target_kind"] = "UNKNOWN"
                        res_updates["target_name"] = ""
                        res_updates["target_info_text"] = ""
                        res_updates["target_score"] = 0.0

                    res_updates["red_tab_enabled"] = bool(red_tab_enabled or target_info_red_tab_hint)
                    continue

                if name == "user_info":
                    now_user = time.time()
                    user_mode = self._resolve_user_info_mode(now_user)
                    if user_mode == "idle":
                        continue

                    if user_mode != self._last_user_info_mode:
                        self._party_user_candidate = ""
                        self._party_user_candidate_hits = 0
                        self._party_user_stable_name = ""
                        self._party_user_stable_score = 0.0
                        self._party_user_stable_source = None
                        self._party_user_stable_until = 0.0
                        self._last_user_info_mode = user_mode

                    allowed_patterns = ["jump"] if user_mode == "ntab" else ["jump2"]
                    required_hits = 1 if user_mode == "self_status" else self._party_user_required_hits
                    user_err_ratio = 0.13 if user_mode == "ntab" else 0.10
                    user_name = ""
                    user_score = 0.0
                    matched_source = None
                    party_ambiguous = False
                    party_result = {}
                    accepted_hit = None
                    try:
                        party_result = self._evaluate_party_user_info_from_frame(
                            img_np,
                            allowed_patterns=allowed_patterns,
                            err_ratio=user_err_ratio,
                        )
                        accepted_hit = party_result.get("accepted_hit")
                        party_ambiguous = bool(party_result.get("ambiguous", False))
                        if not accepted_hit:
                            best_hit = party_result.get("best_hit")
                            best_score = float((best_hit or {}).get("score", 0.0) or 0.0)
                            fallback_score = 0.86 if user_mode == "ntab" else 0.84
                            if best_hit and best_score >= fallback_score:
                                accepted_hit = best_hit
                                party_ambiguous = False
                    except Exception:
                        party_result = {}
                        accepted_hit = None
                        party_ambiguous = False

                    if accepted_hit:
                        candidate_name = str(accepted_hit.get("name", "") or "")
                        candidate_score = float(accepted_hit.get("score", 0.0) or 0.0)
                        if candidate_name:
                            if candidate_name == self._party_user_candidate:
                                self._party_user_candidate_hits += 1
                            else:
                                self._party_user_candidate = candidate_name
                                self._party_user_candidate_hits = 1

                            if self._party_user_candidate_hits >= required_hits:
                                self._party_user_stable_name = candidate_name
                                self._party_user_stable_score = candidate_score
                                self._party_user_stable_source = "findtext-stable"
                                self._party_user_stable_until = now_user + self._party_user_stable_ttl
                                user_name = candidate_name
                                user_score = candidate_score
                                matched_source = "findtext-stable"
                    else:
                        self._party_user_candidate = ""
                        self._party_user_candidate_hits = 0

                    if not user_name and now_user <= self._party_user_stable_until and self._party_user_stable_name:
                        user_name = self._party_user_stable_name
                        user_score = float(self._party_user_stable_score or 0.0)
                        matched_source = self._party_user_stable_source or "findtext-stable"

                    if not user_name and not party_ambiguous:
                        bitwise_name, bitwise_score = self.matcher.recognize_entity_bitwise(crop, "users")
                        allowed = set(allowed_patterns)
                        bitwise_threshold = 0.82 if user_mode == "self_status" else 0.88
                        if bitwise_name in allowed and float(bitwise_score or 0.0) >= bitwise_threshold:
                            user_name = bitwise_name
                            user_score = float(bitwise_score or 0.0)
                            matched_source = "bitwise"

                    if user_name:
                        if user_name != self._last_user_info_logged or matched_source != self._last_user_info_source:
                            print(f"[UserInfo] detected: {user_name} via {matched_source or 'unknown'}")
                            self._last_user_info_logged = user_name
                            self._last_user_info_source = matched_source
                        if user_mode == "self_status" and user_name == "jump2":
                            res_updates["last_self_userinfo_seen"] = time.time()
                        if user_mode == "self_status":
                            res_updates["self_status_scan_active"] = False
                            res_updates["self_status_scan_until"] = 0.0
                        res_updates["user_name"] = user_name
                        res_updates["user_info_text"] = user_name
                        res_updates["user_kind"] = "USER"
                        res_updates["user_score"] = user_score
                        res_updates["user_info_source"] = matched_source or ""
                        res_updates["user_info_ambiguous"] = bool(party_ambiguous)
                        res_updates["is_user_detected"] = True
                    else:
                        if self._last_user_info_logged and user_mode == "ntab":
                            print("[UserInfo] cleared")
                            self._last_user_info_logged = None
                            self._last_user_info_source = None
                        if user_mode == "ntab":
                            res_updates["user_score"] = 0.0
                            res_updates["user_info_source"] = ""
                            res_updates["user_info_ambiguous"] = bool(party_ambiguous)
                            res_updates["is_user_detected"] = False
                    continue

                if name == "cooltime_area":
                    bm_hit = self.find_pattern(img_np, "bm", "cooltime_area")
                    gg_hit = self.find_pattern(img_np, "gg", "cooltime_area")
                    bm_detected = bool(bm_hit)
                    gg_detected = bool(gg_hit)
                    res_updates["last_self_cooltime_scan_time"] = time.time()
                    res_updates["self_bm_detected"] = bm_detected
                    res_updates["self_gg_detected"] = gg_detected
                    if bm_detected or gg_detected:
                        res_updates["last_self_cooltime_seen"] = time.time()
                    continue

                # ════════════════════════════════════════════════
                # play_area: 2-Stage Entity Detection
                # ????????????????????????????????????????????????
                if name.lower() == "play_area":
                    # SentinelThread? ?? ?? ?? ??
                    self.state.last_play_area_rgb = crop.copy()
                    red_tab_enabled = self._detect_red_tab(crop, target_info_hint=target_info_red_tab_hint)
                    res_updates["red_tab_enabled"] = red_tab_enabled
                    hb_matches = self.find_pattern_all(
                        img_np,
                        "hb",
                        "play_area",
                        max_results=6,
                        overlap_px=44,
                        err_ratio=0.15,
                    )
                    res_updates["hb_matches"] = hb_matches
                    res_updates["hb_objects"] = hb_matches

                    play_area_overlay = self.state.ocr_preview_img
                    map_data = self.state.maps_db.get(self.state.current_map, {}) or {}
                    char_anchor_y_offset = int(map_data.get("char_anchor_y_offset", 134))

                    # ── 캐릭터 중앙 타일 기준 (J12: x=9, y=11) ──
                    grid_sz = float(map_data.get("grid_size", 48.2))
                    try:
                        import json as _jg
                        _cf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
                        if os.path.exists(_cf):
                            with open(_cf, 'r', encoding='utf-8') as _fg:
                                grid_sz = float(_jg.load(_fg).get("play_area", {}).get("grid_size", 48.2))
                    except Exception:
                        pass
                    center_x = sx + (9 * grid_sz) + grid_sz / 2
                    center_y = sy + (11 * grid_sz) + grid_sz / 2

                    # ── GPS Anchor (내 캐릭터 화살표) — 매 프레임 ──
                    # findtext_scan을 사용하여 0.75 이상인 모든 마커 후보를 가져옴
                    marker_match = self.matcher.findtext_scan(crop, ["marker"], threshold=0.75)
                    best_marker_score = self.matcher.marker_best_score(crop, "my_arrow")
                    
                    # 여러 개의 마커 후보 중, 점수가 threshold 이상이면서 화면 중앙(J12) 반경 4타일(192px) 이내인 것만 진짜로 간주
                    candidates = [
                        m for m in marker_match 
                        if m["name"] == "my_arrow" and m["score"] >= self.matcher.MARKER_THRESHOLD
                        and abs((sx + m["x"] + m["w"]//2) - center_x) < 192
                        and abs((sy + m["y"] + m["h"]//2) - center_y) < 192
                    ]
                    best_marker = None
                    
                    if candidates:
                        # 중앙과의 거리(pixel)를 기준으로 오름차순 정렬
                        candidates.sort(key=lambda m: ((sx + m["x"] + m["w"]//2 - center_x)**2 + (sy + m["y"] + m["h"]//2 - center_y)**2))
                        best_marker = candidates[0]

                    grid_pos = None
                    if best_marker:
                        feet_x = sx + best_marker["x"] + (best_marker["w"] // 2)
                        # 화살표 하단(y+h)에서 캐릭터 발밑까지 오프셋
                        feet_y = sy + best_marker["y"] + best_marker["h"] + 24

                        # 기준점(Anchor) 캘리브레이션: my_screen_pos 업데이트
                        self.state.my_screen_pos = (feet_x, feet_y)

                        grid_pos = self._calc_grid(feet_x, feet_y)
                    if grid_pos:
                        self.state.entities["me"] = {
                            "grid": grid_pos,
                            "screen": (feet_x, feet_y),
                            "last_seen": time.time(),
                        }
                        if play_area_overlay is not None:
                            cv2.circle(play_area_overlay,
                                       (int(feet_x), int(feet_y)), 6, (0, 255, 0), -1)
                        if self.frame_count % 10 == 0:
                            _monitor_log(f"   [GPS] >> Me: {grid_pos} | Anchor:({feet_x},{feet_y})")
                    now_log = time.time()
                    # GPS 로그 비활성화 (반복 출력 방지)
                    # if now_log - self._last_gps_log_time >= 3.0:
                    #     gx_d = int(grid_pos[0]) if grid_pos else -1
                    #     gy_d = int(grid_pos[1]) if grid_pos else -1
                    #     gname = (f"{chr(ord('A') + gx_d)}{gy_d + 1}"
                    #              if grid_pos and gx_d >= 0 else "?")
                    #     # 같은 GRID 값이면 출력하지 않음 (반복 출력 방지)
                    #     if best_marker and gname != self._last_gps_grid:
                    #         print(f"[GPS] 화살표 발견! 점수: {float(best_marker['score']):.2f} "
                    #               f"| GRID: {gname}")
                    #         self._last_gps_grid = gname
                    #     # else:
                    #     #     print(f"[GPS] 화살표 탐색 실패 (최고 점수: {best_marker_score:.2f})")
                    #     self._last_gps_log_time = now_log
                    continue



                # ?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═
                # 5. stat_info ??status 폴더
                # ?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═
                if name == "stat_info":
                    matched = self.matcher.recognize(crop, "status")
                    if matched:
                        res_updates["stat_info"] = matched
                    continue

                # ════════════════════════════════════════════════
                # 5.5. items_scan - 아이템 비트맵 XOR 매칭
                # ════════════════════════════════════════════════
                if name == "items_scan":
                    item_name, item_score = self.matcher.recognize_entity_bitwise(crop, "items")
                    if item_name:
                        res_updates["item_name"] = item_name
                        res_updates["item_score"] = item_score
                    continue

                # ════════════════════════════════════════════════
                # 6. map_info — FindText 토큰 OCR (선비족 3형태)
                # ════════════════════════════════════════════════
                if name == "map_info":
                    now_map = time.time()
                    map_text = ""
                    score = 0.0
                    dbg = {}
                    map_signature = ""
                    map_info_changed = False
                    try:
                        map_text = str(self.map_ocr.recognize(crop) or "").strip()
                        score = float(getattr(self.map_ocr, "last_score", 0.0) or 0.0)
                        dbg = dict(getattr(self.map_ocr, "last_debug", {}) or {})
                    except Exception as e:
                        print(f"[MapOCR] recognize error: {e}")
                    try:
                        map_signature = "|".join(map(str, self._fast_crop_signature(crop)))
                    except Exception:
                        map_signature = ""
                    map_fingerprint = self._map_info_fingerprint(crop)
                    map_info_changed = bool(map_signature and map_signature != self._map_info_last_signature)
                    if map_info_changed:
                        self._map_info_last_signature = map_signature
                        self._map_info_change_seq += 1
                        self._map_info_changed_at = now_map

                    # Fallback: temple/maps 통짜 템플릿
                    if not map_text:
                        matched_map = self.matcher.recognize(crop, "maps")
                        if matched_map:
                            map_text = str(matched_map).strip()

                    if map_text:
                        from bis_core import split_map_name_floor
                        map_name, map_floor = split_map_name_floor(map_text)

                        if map_text != getattr(self.state, "current_map", ""):
                            print(f"[Map] 맵 변경됨: {map_text}")
                            self.state.current_map = map_text
                            self.load_maps()

                        res_updates["current_map"] = map_text
                        res_updates["map_name"] = map_name
                        res_updates["map_floor"] = map_floor
                        res_updates["current_floor"] = map_floor
                        res_updates["map_info_text"] = map_text
                        if map_signature:
                            res_updates["map_info_signature"] = map_signature
                        if map_fingerprint:
                            res_updates["map_info_fingerprint"] = map_fingerprint
                        res_updates["map_info_change_seq"] = self._map_info_change_seq
                        res_updates["map_info_changed_at"] = self._map_info_changed_at
                        res_updates["map_info_changed"] = map_info_changed
                        self.last_values["map_info"] = map_text
                        self._map_fail_count = 0
                    else:
                        self._map_fail_count = int(getattr(self, "_map_fail_count", 0) or 0) + 1
                        if getattr(self, "_map_ocr_debug_enabled", False) and (
                            self._map_fail_count <= 5 or (self._map_fail_count % 20) == 0
                        ):
                            h, w = crop.shape[:2]
                            print(
                                f"[MapOCR] 인식 실패 #{self._map_fail_count} crop={w}x{h} "
                                f"prefix={float(dbg.get('prefix_score', 0.0)):.3f} "
                                f"(선비족 맵인지 / ROI에 흰 글자가 보이는지 확인)"
                            )
                    if map_signature:
                        res_updates["map_info_signature"] = map_signature
                    if map_fingerprint:
                        res_updates["map_info_fingerprint"] = map_fingerprint
                    res_updates["map_info_change_seq"] = self._map_info_change_seq
                    res_updates["map_info_changed_at"] = self._map_info_changed_at
                    res_updates["map_info_changed"] = map_info_changed
                    continue

            # Trigger flags must update even when no other ROI changed.
            try:
                if getattr(self.state, "hp_trig_active", False) != h_trig_active:
                    self.state.hp_trig_active = h_trig_active
                if getattr(self.state, "mp_trig_active", False) != m_trig_active:
                    self.state.mp_trig_active = m_trig_active
            except Exception:
                pass

            # ── [최종 업데이트 및 제어] ──────────────────────
            # 인식된 모든 변화를 GameState에 일괄 반영
            if res_updates:
                if hasattr(self.state, "update_from_dict"):
                    self.state.update_from_dict(res_updates)
                else:
                    # 하위 호환성: 직접 업데이트
                    for k, v in res_updates.items():
                        if hasattr(self.state, k):
                            setattr(self.state, k, v)
                
                # GUI 업데이트 큐에 데이터 전달 (비동기)
                try:
                    self.state.gui_update_queue.put_nowait(res_updates)
                    self._first_sync_done = True # 첫 동기화 완료 표시
                except:
                    # 큐가 꽉 차 있으면 오래된 데이터를 덮어쓰기
                    try:
                        self.state.gui_update_queue.get_nowait()
                        self.state.gui_update_queue.put_nowait(res_updates)
                    except:
                        pass
                    # 방어 코드: 메서드가 없을 경우 직접 속성 주입
                    with getattr(self.state, "_lock", threading.Lock()):
                        for k, v in res_updates.items():
                            setattr(self.state, k, v)
                # 터미널에 실시간 스캔 정보 출력 (필요시)

            # ── [실시간 상태 로그 엔진 (비활성화 - 터미널 반복 출력 방지)] ──────────────────────
            # now_status = time.time()
            # if not hasattr(self, "_last_status_log_time"):
            #     self._last_status_log_time = 0.0
            #
            # if now_status - self._last_status_log_time >= 10.0:
            #     self._last_status_log_time = now_status
            #
            #     map_str = getattr(self.state, "current_map", "?")
            #     fps_str = f"{getattr(self.state, 'ocr_fps', 0):.1f}"
            #
            #     monsters = self.state.entities.get("monsters", [])
            #     items    = self.state.entities.get("items", [])
            #     users    = self.state.entities.get("users", [])
            #
            #     m_count = len(monsters)
            #     i_count = len(items)
            #     u_count = len(users)
            #
            #     def _fmt_pos(ents):
            #         plist = []
            #         for e in ents:
            #             cx_val = e.get("cx", 0)
            #             cy_val = e.get("cy", 0)
            #             name = e.get("name", "?")
            #             plist.append(f"{name}(POS_{cx_val},{cy_val})")
            #         return ",".join(plist) if plist else "-"
            #
            #     m_pos = _fmt_pos(monsters)
            #     i_pos = _fmt_pos(items)
            #
            #     target_str = "없음"
            #     if self.state.target_locked:
            #         target_str = f"{getattr(self.state, 'target_name', '?')}"
            #         m_grid = next((m.get("grid", "?") for m in monsters
            #                        if m.get("name") == self.state.target_name), "?")
            #         if m_grid != "?" and len(m_grid) == 2:
            #             gname = f"{chr(ord('A') + m_grid[0])}{m_grid[1] + 1}" if m_grid[0] >= 0 else "?"
            #             target_str += f" (Grid: {gname})"
            #
            #     status_str = "사냥 중" if getattr(self.state, "auto_hunt", False) else "대기 중"
            #     last_event = getattr(self.state, "last_event_log", "없음")
            #
            #     print(f"[Status] Map:{map_str} | FPS: {fps_str}")
            #     print(f"[Entities] Monsters: {m_count}마리 [POS: {m_pos}], Items: {i_count}개 [POS: {i_pos}], User: {u_count}명")
            #     print(f"[Targeting] 현재 타겟: {target_str}")
            #     print(f"[Events] 상태: {status_str}, 최근 이벤트: {last_event}")
            #     print("-" * 50)

            # FPS 동기화 제거 (최대 성능 발휘)
            # elapsed = time.time() - start_time
            # sleep_time = max(0, 0.033 - elapsed)
            # time.sleep(sleep_time)
            # CPU 점유율 과열 방지를 위해 최소한의 양보만 수행 (0.5ms)
            time.sleep(0.0005)
            self.frame_count += 1

    def _calc_grid(self, px, py):
        """절대 픽셀 좌표를 현재 맵 기준 Grid 좌표로 변환
        확정 공식: GridX = (px - 276) / 48.2, GridY = (py - 32) / 48.2
        """
        # 1순위: 현재 ROI(config)의 play_area 기준 사용 (실화면 동기화)
        if "play_area" in self.regions:
            pa = self.regions["play_area"]
            play_sx, play_sy = int(pa.sx), int(pa.sy)
        else:
            # 2순위: 확정 오프셋 fallback (sx=276, sy=32)
            play_sx, play_sy = 276, 32

        # 오류 방지: 아주 작은 값이 들어오면 기본값 사용
        if play_sx < 100:
            play_sx = 276
        if play_sy < 10:
            play_sy = 32

        # config.json에서 grid_size 읽기 (float 지원)
        grid_size = 48.2
        try:
            import json, os
            SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            config_file = os.path.join(SCRIPT_DIR, "config.json")
            if os.path.exists(config_file):
                with open(config_file, 'r', encoding='utf-8') as f:
                    conf = json.load(f)
                    grid_size = float(conf.get("play_area", {}).get("grid_size", 48.2))
        except Exception:
            pass

        gy = int((py - play_sy) / grid_size)
        gx = int((px - play_sx) / grid_size)
        return (gx, gy)
