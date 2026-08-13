"""svc_numeric_scanner.py - HP/MP/EXP 등 숫자 필드 OCR 스레드 (NumericFieldScanner)."""

import time
import threading
import os
import json
import cv2
import numpy as np
from svc_kernel import GameState
from svc_findtext import FindTextEngine
from svc_monitor_common import dict_to_region
from svc_pattern_matcher import PatternMatcher


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
