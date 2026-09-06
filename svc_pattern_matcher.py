"""svc_pattern_matcher.py - 템플릿 매칭 엔진 (PatternMatcher)."""

import time
import os
import json
import re
import cv2
import numpy as np
from svc_kernel import GameState
from svc_monitor_common import _monitor_log
from findtext_wrapper import get_findtext

FINDTEXT_PATTERNS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "findtext_patterns.json")


def imread_unicode(path, flags=cv2.IMREAD_COLOR):
    """한글 경로 등 유니코드 경로를 포함한 이미지를 읽기 위한 헬퍼 함수"""
    try:
        return cv2.imdecode(np.fromfile(path, dtype=np.uint8), flags)
    except Exception as e:
        _monitor_log(f"[Error] imread_unicode failed for {path}: {e}")
        return None


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

        # ft.py로 캡처한 FindText 패턴 (findtext_patterns.json, 카테고리: map/item/monster/party)
        self._ft = get_findtext()
        self.findtext_patterns: dict[str, dict[str, str]] = {}
        self._load_findtext_patterns()

    def _load_findtext_patterns(self):
        self.findtext_patterns = {}
        if os.path.exists(FINDTEXT_PATTERNS_FILE):
            try:
                with open(FINDTEXT_PATTERNS_FILE, "r", encoding="utf-8") as f:
                    self.findtext_patterns = json.load(f)
            except Exception as e:
                _monitor_log(f"[Error] findtext_patterns.json 로드 실패: {e}")

    # err1을 0.05로 조였다가 되돌렸다. "잉크가 글자"라고 가정했는데 실측해보니
    # 반대였다 - 이름표는 글자가 밝고(221~252) 주변 바닥이 어두운데(66~75),
    # 잉크(1)의 정의가 '어두운 픽셀'이라 이 패턴들에서 잉크는 글자가 아니라
    # 주변 바닥이다. 그래서 err1을 조이는 건 '글자'가 아니라 '바닥'을 조이는
    # 것이었고, 바닥이 불꽃으로 바뀌자 진짜 이름표까지 못 찾게 됐다.
    def find_text_scan(self, play_area_rgb: np.ndarray, category: str, ent_type: str,
                        err1: float | None = None, err0: float | None = None,
                        only_names: set | None = None) -> list[dict]:
        """findtext_patterns.json의 category(map/item/monster/party/magic)에 저장된
        패턴을 전부 '|'로 결합해 한 번에 스캔한다. 각 패턴에 이미 박혀있는
        <comment>가 그대로 엔티티 이름(id)이 된다 (ft.py 저장 시 comment=이름
        으로 강제하기 때문에 여기서 별도 이름 매핑이 필요 없음).

        Returns: findtext_scan()과 동일한 shape의 blob 리스트
                 [{"name","type","x","y","cx","cy","w","h","score","grid"}, ...]
        """
        if play_area_rgb is None or play_area_rgb.size == 0:
            return []
        patterns = self.findtext_patterns.get(category, {})
        if not patterns:
            return []
        if only_names is not None:
            # 패턴 이름은 '처녀귀신_left'처럼 <몬스터>_<방향> 형태다.
            # only_names에는 방향 없는 이름('처녀귀신')만 들어온다.
            patterns = {n: v for n, v in patterns.items()
                        if n in only_names
                        or n.rsplit("_", 1)[0] in only_names}
            if not patterns:
                return []
        combined = "".join(patterns.values())
        # find_text()/findtext_wrapper는 BGR을 가정한다(ft.py도 mss가 준 BGR을
        # 그대로 넘긴다 - gray=R*38+G*75+B*15 계산 자체가 채널0=B를 전제).
        # 하지만 이 함수로 들어오는 인자는 이름 그대로 RGB다(CaptureSvc가
        # last_frame을 RGB로 미리 변환해서 공유하기 때문). 흑백에 가까운
        # map 도트폰트는 R≈B라 우연히 매칭이 됐지만, party 카테고리처럼
        # 색이 뚜렷한 패턴(빨간 배경의 점프_user 등)은 R/B가 뒤바뀌면서
        # 색 허용오차를 벗어나 전혀 매칭되지 않았다 - ft.py(BGR)에서는
        # 찾아지는데 여기(RGB)서는 못 찾던 원인.
        screenshot_bgr = cv2.cvtColor(play_area_rgb, cv2.COLOR_RGB2BGR)
        matches = self._ft.find_text(combined, screenshot=screenshot_bgr,
                                      err1=err1, err0=err0, find_all=True)
        results = []
        for m in matches:
            w, h = m.get("w", 0), m.get("h", 0)
            x, y = m.get("x", 0), m.get("y", 0)
            results.append({
                "name": m.get("id", ""),
                "type": ent_type,
                "x": x, "y": y,
                "cx": x + w // 2, "cy": y + h // 2,
                "w": w, "h": h,
                "score": 1.0,
                "grid": (0, 0),
            })
        # err1/err0 허용오차 때문에 같은 물체가 1~2px 어긋난 위치에서 여러 번
        # 잡힐 수 있음 - findtext_scan()과 동일한 NMS로 중복 제거.
        if len(results) > 1:
            results = self._nms_results(results, overlap_thresh=0.3)
        return results

    # !!----------------------------------------------------------
    def reload(self, root: str):
        """기존 ?플릿을 비우??시 로드"""
        self.templates = {f: {} for f in self.FOLDERS}
        self.shape_templates = {f: {} for f in self.FOLDERS}
        self.digit_templates = {}
        self.binary_templates = {f: {} for f in self.FOLDERS}
        self._load_all(root)
        self._load_digit_templates(root)
        self._load_findtext_patterns()


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
