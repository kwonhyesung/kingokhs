# -*- coding: utf-8 -*-
"""
svc_monitor.py - SYSTEM V5 분석 엔진 (MonitorSvc)

CaptureSvc/PatternMatcher/EntityTracker/NumericFieldScanner/SentinelThread는
각각 svc_capture.py / svc_pattern_matcher.py / svc_entity_tracker.py /
svc_numeric_scanner.py / svc_sentinel.py로 분리되었다.
"""

import time
import threading
import os
import json
import re
import cv2
import numpy as np
import win32gui

try:
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
except Exception:
    pass

from typing import Optional
from svc_kernel import GameState, find_game_window
from svc_findtext import FindTextEngine
from svc_monitor_common import VERBOSE_MONITOR_LOGS, _monitor_log, dict_to_region
from svc_capture import CaptureSvc, get_camera
from svc_pattern_matcher import PatternMatcher
from svc_entity_tracker import EntityTracker
from svc_numeric_scanner import NumericFieldScanner
from svc_sentinel import SentinelThread

_FLOOR_DIGIT_SUFFIX = re.compile(r"^(.+)_(\d)$")  # map_info가 매 사이클 도는 핫패스라 컴파일을 매 호출 밖으로 뺐다.


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
        self._last_focus_warn_time = 0.0
        self._focus_lost_since = 0.0
        self._own_gui_hwnd = 0

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
        # 동일 crop 재스캔 회피용 캐시 (map_info 스캔이 파이프라인에서 제일 비싸다).
        self._map_info_cache_signature = ""
        self._map_info_cached_text = ""
        self._map_info_cached_score = 0.0
        self._map_info_cached_fingerprint = ""
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
            "map_info": 0.0,  # x/y와 거의 동시에 포탈이동을 잡아야 해서 스로틀 없이 매 사이클 스캔.
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
    def _compose_map_text_with_floor_digits(hits: list[dict]) -> tuple[str, float]:
        """도삭산처럼 층 수가 너무 많아 층별로 통짜 패턴을 다 못 찍는 던전은,
        기본 이름("도삭산") 패턴 하나 + 숫자 글리프 패턴("도삭산_0" ~ "도삭산_9")
        여러 개를 조합해서 층 번호를 만든다 - 화면에 실제로 보이는 자릿수만큼만
        (1~3자리) 왼쪽부터 이어붙이고, 앞에 없는 0은 안 붙인다(정수 형태).
        일반 던전(패턴 하나가 전체 이름+층수를 통짜로 담음, 예: "흉가1")은
        digit-suffix 패턴이 아예 없으니 그대로 첫 히트를 쓴다."""
        # 히트마다 정규식은 딱 한 번만 매치하고 그 결과를 재사용한다(이전엔 같은
        # 문자열을 base/digit 분류, own_digits 필터, floor 조립에서 최대 4번씩
        # 다시 매치했다).
        matched = [(h, _FLOOR_DIGIT_SUFFIX.match(str(h.get("name", "")))) for h in hits]
        base_hits = [(h, m) for h, m in matched if not m]
        digit_hits = [(h, m) for h, m in matched if m]

        if base_hits:
            base_h, _ = base_hits[0]
            base_name = str(base_h["name"]).strip()
            score = float(base_h.get("score", 1.0))
            own_digits = [(h, m) for h, m in digit_hits if m.group(1) == base_name]
            if own_digits:
                own_digits.sort(key=lambda pair: pair[0].get("x", 0))
                floor = "".join(m.group(2) for _, m in own_digits[:3])
                return f"{base_name}{floor}", score
            return base_name, score

        if digit_hits:
            digit_hits.sort(key=lambda pair: pair[0].get("x", 0))
            base_name = digit_hits[0][1].group(1)
            floor = "".join(m.group(2) for _, m in digit_hits[:3])
            return f"{base_name}{floor}", 1.0

        return "", 0.0

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
            if crop.ndim == 2:
                gray = crop
            elif crop.ndim == 3 and crop.shape[2] == 4:
                gray = cv2.cvtColor(crop, cv2.COLOR_RGBA2GRAY)
            elif crop.ndim == 3 and crop.shape[2] == 3:
                gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
            else:
                msg = f"[MapFingerprint] unexpected crop shape: {crop.shape}"
                _monitor_log(msg)
                from bis_core import discord_notify
                discord_notify(msg)
                return ""
            normalized = cv2.resize(gray, (16, 8), interpolation=cv2.INTER_AREA)
            _threshold, binary = cv2.threshold(
                normalized,
                0,
                255,
                cv2.THRESH_BINARY | cv2.THRESH_OTSU,
            )
            return "".join("1" if value else "0" for value in binary.reshape(-1))
        except Exception as e:
            msg = f"[MapFingerprint] failed: {e}, crop_shape={getattr(crop, 'shape', None)}"
            _monitor_log(msg)
            from bis_core import discord_notify
            discord_notify(msg)
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

            if hwnd != self.state.hwnd:
                # ponytail: "ory"는 흔한 부분 문자열(팩토리/히스토리/메모리
                # 등)이라 실제 게임이 아닌 다른 창을 잘못 붙잡을 수 있다 -
                # 실전에서 게임창 비활성화 경고가 몇 분씩 계속되는 사례가
                # 나와서, 봇이 "게임"이라고 여기는 창이 진짜 맞는지부터
                # 확인해야 했다. hwnd가 바뀔 때만(스팸 방지) 그 제목을 찍는다.
                try:
                    tracked_title = win32gui.GetWindowText(hwnd) or "(제목 없음)"
                except Exception:
                    tracked_title = "(확인 실패)"
                print(f"[GameWindow] 게임 창으로 추적 시작: {tracked_title!r}")
            self.state.hwnd = hwnd

            # 이 봇 자신의 GUI 창(SYSTEM PARALLEL SOTA - gui_app.py의
            # self.root.title())도 "게임에 집중 중"으로 쳐준다. 실전에서
            # 몇 분씩 계속되던 "게임창 비활성화" 경고의 정체가 사실 이
            # GUI 자신이었다 - 상태 확인하려고 컨트롤 패널을 보는 것도
            # "자리를 비운 것"으로 취급해 서비스를 계속 꺼버리고 있었다.
            # 프로세스 수명 동안 안 바뀌므로 한 번만 찾아서 캐싱한다.
            if not self._own_gui_hwnd:
                # 못 찾으면(예: 시작 직후 GUI 창이 아직 안 떴을 때) 0을 영구
                # 고정하지 않고 다음 사이클에 다시 찾는다.
                self._own_gui_hwnd = find_game_window("SYSTEM PARALLEL SOTA") or 0

            # 게임창 비활성화 감지 시 Panic Release 방지
            foreground_hwnd = win32gui.GetForegroundWindow()
            if foreground_hwnd != hwnd and foreground_hwnd != self._own_gui_hwnd:
                now_focus = time.time()
                # ponytail: 예전엔 포커스 벗어난 프레임 1개만으로 즉시 껐다 -
                # service_active=False가 스로틀 없이 매 사이클 실행되고 있어서
                # (아래 print만 2초 제한), 실전에서 잠깐 다른 창 확인하는
                # 순간까지 전부 잡혀 계속 꺼졌다. 3초 이상 연속으로 벗어나야만
                # 끈다 - 짧은 알트탭은 봐주고, 진짜 자리를 비운 경우만 막는다.
                if self._focus_lost_since == 0.0:
                    self._focus_lost_since = now_focus
                if now_focus - self._focus_lost_since >= 3.0:
                    if hasattr(self.state, "f1_route_active"): self.state.f1_route_active = False
                    if hasattr(self.state, "service_active"): self.state.service_active = False

                    if now_focus - self._last_focus_warn_time >= 2.0:
                        self._last_focus_warn_time = now_focus
                        try:
                            stealer_title = win32gui.GetWindowText(foreground_hwnd) or "(제목 없음)"
                        except Exception:
                            stealer_title = "(확인 실패)"
                        try:
                            tracked_title = win32gui.GetWindowText(hwnd) or "(제목 없음)"
                        except Exception:
                            tracked_title = "(확인 실패)"
                        print(f"[Warn] 게임창 비활성화 감지 (봇이 추적 중인 창: {tracked_title!r}, "
                              f"현재 실제 활성 창: {stealer_title!r}) "
                              f"- 서비스만 중단하고 하드웨어 신호는 유지합니다.")
            else:
                self._focus_lost_since = 0.0
            
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

                probe_ntab = name == "user_info" and bool(getattr(self.state, "ntab_active", False))

                now_roi = time.time()
                if not self._should_process_roi(name, now_roi):
                    if probe_ntab:
                        print(f"[UserInfoProbe] {time.strftime('%H:%M:%S')} skipped by _should_process_roi interval throttle")
                    continue

                # 좌표 계산 및 crop 추출
                ox, oy, scx, scy = self.obs_params
                fh, fw = img_np.shape[:2]
                # FindText는 픽셀 단위 정확한 매칭이 필요하다 - map_info는 이미 패딩 없이 도트
                # 폰트를 정확히 crop한다. user_info(점프_user/졈프_user 신원 확인)도 findtext_scan
                # 기반이라 똑같이 정확한 crop이 필요한데 여기 빠져있었다: 5px 패딩을 넣고 CUBIC으로
                # 다시 리사이즈하면(아래 interp 분기) 도트 폰트가 뭉개져 매칭이 항상 실패한다.
                pad = 0 if name in ("map_info", "user_info") else 5
                sx = max(0, int(reg.sx * scx + ox - pad))
                sy = max(0, int(reg.sy * scy + oy - pad))
                dx = min(fw, int(reg.dx * scx + ox + pad))
                dy = min(fh, int(reg.dy * scy + oy + pad))
                if probe_ntab:
                    print(
                        f"[UserInfoProbe] {time.strftime('%H:%M:%S')} roi=({sx},{sy},{dx},{dy}) "
                        f"frame=({fw}x{fh}) obs_params={self.obs_params}"
                    )

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
                if crop.size == 0:
                    if probe_ntab:
                        print(f"[UserInfoProbe] {time.strftime('%H:%M:%S')} skipped: crop.size == 0")
                    continue

                target_w, target_h = reg.dx - reg.sx, reg.dy - reg.sy
                if target_w > 0 and target_h > 0:
                    if crop.shape[1] != target_w or crop.shape[0] != target_h:
                        # map_info는 도트 폰트 → NEAREST 필수 (CUBIC이면 FindText 매칭 붕괴)
                        interp = cv2.INTER_NEAREST if name in ("map_info", "user_info") else cv2.INTER_CUBIC
                        crop = cv2.resize(crop, (target_w, target_h), interpolation=interp)
                else:
                    if probe_ntab:
                        print(f"[UserInfoProbe] {time.strftime('%H:%M:%S')} skipped: target_w/h <= 0 ({target_w}x{target_h})")
                    continue

                cur_hash = self._fast_crop_signature(crop)
                # map_info는 상태 로그/재시도를 위해 해시 스킵에서 제외.
                # user_info도 마찬가지 이유로 제외해야 한다 - _confirm_and_lock_warrior_target()이
                # ntab_active 짧은 창(~0.42s) 동안 매 프레임 신선한 재스캔을 필요로 하는데,
                # 이 캐시가 걸리면 화면이 그대로인 것처럼 보이는 순간 이후로는 user_mode=="ntab"
                # 분기 자체가 실행되지 않아 신원 확인이 영원히 no_match로 끝난다
                # (target_info가 red_tab 신원 판정을 위해 이미 같은 이유로 예외 처리돼 있던 것과 동일).
                if (
                    name != "map_info"
                    and self.last_hashes.get(name) == cur_hash
                    and name in self.last_values
                    and name not in ("play_area", "target_info", "user_info")
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
                    if probe_ntab:
                        print(f"[UserInfoProbe] {time.strftime('%H:%M:%S')} reached ntab check, user_mode={user_mode}")
                        if now_user - float(getattr(self, "_last_user_info_probe_save", 0.0) or 0.0) >= 0.5:
                            self._last_user_info_probe_save = now_user
                            try:
                                debug_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr_debug")
                                os.makedirs(debug_dir, exist_ok=True)
                                out_path = os.path.join(debug_dir, "user_info_probe.png")
                                cv2.imwrite(out_path, crop)
                                print(f"[UserInfoProbe] saved crop -> {out_path} shape={crop.shape}")
                            except Exception as e:
                                print(f"[UserInfoProbe] crop save failed: {e}")
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

                    if user_mode == "ntab":
                        # 격수/도사 신원 확인: ft.py로 캡처한 findtext_patterns.json의
                        # party 카테고리("<이름>_user" 키)를 직접 스캔한다.
                        # patterns.json의 낡은 jump/dah 데이터는 여기서 더 이상 쓰지 않는다
                        # (그게 신원 확인이 계속 실패했던 원인 - 캡처는 findtext_patterns.json에
                        # 저장되는데 여기선 patterns.json을 보고 있었다).
                        identity_hits = [
                            h for h in self.matcher.find_text_scan(crop, "party", "USER")
                            if str(h.get("name", "")).endswith("_user")
                        ]
                        if identity_hits:
                            identity_hits.sort(key=lambda h: -float(h.get("score", 0.0) or 0.0))
                            matched_name = str(identity_hits[0].get("name", ""))
                            if matched_name != self._last_user_info_logged:
                                print(f"[UserInfo] ntab identity detected: {matched_name}")
                                self._last_user_info_logged = matched_name
                                self._last_user_info_source = "findtext-stable"
                            res_updates["user_name"] = matched_name
                            res_updates["user_info_text"] = matched_name
                            res_updates["user_kind"] = "USER"
                            res_updates["user_score"] = 1.0
                            res_updates["user_info_source"] = "findtext-stable"
                            res_updates["user_info_ambiguous"] = len(identity_hits) > 1
                            res_updates["is_user_detected"] = True
                        else:
                            if self._last_user_info_logged:
                                print("[UserInfo] ntab identity cleared")
                                self._last_user_info_logged = None
                                self._last_user_info_source = None
                            res_updates["user_score"] = 0.0
                            res_updates["user_info_source"] = ""
                            res_updates["user_info_ambiguous"] = False
                            res_updates["is_user_detected"] = False
                        continue

                    allowed_patterns = ["jump2"]
                    required_hits = self._party_user_required_hits
                    user_err_ratio = 0.10
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
                        bitwise_threshold = 0.82
                        if bitwise_name in allowed and float(bitwise_score or 0.0) >= bitwise_threshold:
                            user_name = bitwise_name
                            user_score = float(bitwise_score or 0.0)
                            matched_source = "bitwise"

                    # 아래는 self_status(도사 자신 상태 확인) 전용 경로다.
                    # ntab(격수/도사 신원 확인)은 위에서 이미 continue로 빠졌다.
                    if user_name:
                        if user_name != self._last_user_info_logged or matched_source != self._last_user_info_source:
                            print(f"[UserInfo] detected: {user_name} via {matched_source or 'unknown'}")
                            self._last_user_info_logged = user_name
                            self._last_user_info_source = matched_source
                        if user_name == "jump2":
                            res_updates["last_self_userinfo_seen"] = time.time()
                        res_updates["self_status_scan_active"] = False
                        res_updates["self_status_scan_until"] = 0.0
                        res_updates["user_name"] = user_name
                        res_updates["user_info_text"] = user_name
                        res_updates["user_kind"] = "USER"
                        res_updates["user_score"] = user_score
                        res_updates["user_info_source"] = matched_source or ""
                        res_updates["user_info_ambiguous"] = bool(party_ambiguous)
                        res_updates["is_user_detected"] = True
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

                    # 내 캐릭터 위치(my_screen_pos)는 이제 SentinelThread가 매 프레임
                    # FindText(category="self")로 직접 찾는다 - 예전엔 여기서 cv2 PNG
                    # 템플릿("my_arrow")으로 찾았는데 temple/marker 폴더가 비어 있어
                    # 항상 실패했고, 고정 좌표로 대체하는 것도 카메라가 맵 가장자리에서
                    # 멈추면 캐릭터가 화면 중앙을 벗어나서 못 쓴다(SentinelThread가
                    # 매 프레임 실측하면 이 문제가 없다).
                    continue



                # ?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═
                # 5. stat_info ??status 폴더
                # ?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═?═
                if name == "stat_info":
                    # 소지품이 가득 찼는지: findtext_patterns.json의 "status"
                    # 카테고리 패턴으로 판정한다 (ft.py로 캡처해서 저장).
                    # 패턴이 하나도 없으면 항상 False - 줍기를 막지 않는다.
                    try:
                        hits = self.matcher.find_text_scan(crop, category="status", ent_type="STATUS")
                    except Exception:
                        hits = []
                    res_updates["inventory_full"] = bool(hits)
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
                        map_signature = "|".join(map(str, self._fast_crop_signature(crop)))
                    except Exception:
                        map_signature = ""
                    map_info_changed = bool(map_signature and map_signature != self._map_info_last_signature)

                    # map_info는 (포탈을 x/y와 같은 순간에 잡으려고) 스로틀 없이 매
                    # 사이클 돌고, 아래 해시-스킵에서도 제외돼 있다. 그런데 이 ROI의
                    # 스캔(FindText 풀스캔 + dHash + 템플릿 폴백)은 파이프라인에서 제일
                    # 비싼 축이라, 화면이 그대로인 사이클에도 매번 다시 돌리면 모니터
                    # 루프 전체가 느려지고 그 뒤에 달린 힐/추적 반응까지 같이 굼떠진다
                    # (실측 리포트: 힐·추적 속도 저하). crop 픽셀이 그대로면 결정적으로
                    # 같은 결과가 나오므로 캐시를 그대로 쓴다 - 픽셀이 바뀌는 순간에는
                    # 여전히 그 즉시 풀스캔하므로 포탈 감지 속도는 그대로다.
                    if map_signature and not map_info_changed and self._map_info_cache_signature == map_signature:
                        map_text = self._map_info_cached_text
                        score = self._map_info_cached_score
                        map_fingerprint = self._map_info_cached_fingerprint
                    else:
                        # ft.py로 저장한 FindText map 패턴을 우선 시도 (사용자가 캡처한 맵이면
                        # 이걸로 잡힘). 없거나 못 찾으면 기존 선비족 전용 토큰 인식기로 폴백
                        # (기존에 동작하던 던전은 그대로 유지, 새로 캡처한 맵만 FindText로 확장).
                        try:
                            ft_map_hits = self.matcher.find_text_scan(crop, category="map", ent_type="MAP")
                        except Exception as e:
                            ft_map_hits = []
                            print(f"[MapOCR] find_text_scan error: {e}")
                        if ft_map_hits:
                            map_text, score = self._compose_map_text_with_floor_digits(ft_map_hits)
                            # composed 값이 실제로 바뀔 때만 남긴다(같은 값 반복 로그 방지).
                            if map_text != getattr(self, "_last_map_hits_logged_text", None):
                                self._last_map_hits_logged_text = map_text
                                hit_names = [str(h.get("name", "")) for h in ft_map_hits]
                                print(f"[MapOCR] hits={hit_names} -> composed={map_text!r}")
                        else:
                            try:
                                map_text = str(self.map_ocr.recognize(crop) or "").strip()
                                score = float(getattr(self.map_ocr, "last_score", 0.0) or 0.0)
                                dbg = dict(getattr(self.map_ocr, "last_debug", {}) or {})
                            except Exception as e:
                                print(f"[MapOCR] recognize error: {e}")
                        map_fingerprint = self._map_info_fingerprint(crop)

                        # Fallback: temple/maps 통짜 템플릿
                        if not map_text:
                            matched_map = self.matcher.recognize(crop, "maps")
                            if matched_map:
                                map_text = str(matched_map).strip()

                        self._map_info_cache_signature = map_signature
                        self._map_info_cached_text = map_text
                        self._map_info_cached_score = score
                        self._map_info_cached_fingerprint = map_fingerprint

                    if map_info_changed:
                        self._map_info_last_signature = map_signature
                        self._map_info_change_seq += 1
                        self._map_info_changed_at = now_map

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
                        # 맵 이름을 '방금' 읽었다는 증거. current_map은 한 번
                        # 정해지면 계속 남아있어서, 지금 확실히 읽고 있는지
                        # 아닌지를 구분할 수 없다 - 벽 학습은 이 시각을 보고
                        # 확실할 때만 기록한다 (틀린 맵에 벽을 쓰면 영구 오염).
                        res_updates["map_seen_at"] = time.time()
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
