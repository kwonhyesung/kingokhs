# -*- coding: utf-8 -*-
"""
술사 첨사냥 모드 - 단일 파일 구현
"""

import sys
import traceback

try:
    import time
    import threading
    import random
    import os
    import json
    import serial
    import serial.tools.list_ports
    import cv2
    import numpy as np
    import win32gui
    import win32ui
    import win32con
    import customtkinter as ctk
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog
    from PIL import Image, ImageTk
    from dataclasses import dataclass, asdict
    from enum import Enum
    from typing import Optional, Dict
    from datetime import datetime
except Exception as e:
    print(f"Import Error: {e}")
    traceback.print_exc()
    sys.exit(1)

# ============================================================
# ③ 경로 및 설정 상수
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.local.json")
MAPS_FILE = os.path.join(SCRIPT_DIR, "maps.json")
WAYPOINTS_FILE = os.path.join(SCRIPT_DIR, "waypoints.json")
SPELL_DB_FILE = os.path.join(SCRIPT_DIR, "spells.json")
GUI_STATE_FILE = os.path.join(SCRIPT_DIR, "gui_state.json")
TEMPLATE_ROOT = os.path.join(SCRIPT_DIR, "temple")
TEMPLATE_DIGIT_DIR = os.path.join(TEMPLATE_ROOT, "digits")
WIN_KEY = "바람의나라"  # 게임 윈도우 키워드

# 고해상도 모니터 호환성을 위한 DPI 인식 활성화
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

try:
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")
except Exception as e:
    print(f"CTK Setting Error: {e}")
    traceback.print_exc()
    sys.exit(1)

# ============================================================
# ① TIMING_CONFIG  ─  시스템 딜레이 제어 테이블
# ============================================================
TIMING_CONFIG: dict = {
    "key_down_hold"  : 0.012,
    "key_gap"        : 0.020,
    "tab_wait"       : 0.040,
    "up_wait"        : 0.040,
    "enter_wait"     : 0.035,
    "spell_cast_gap" : 0.030,
    "spell_confirm"  : 0.200,
    "ocr_wait"       : 0.100,
    "ocr_fast"       : 0.050,
    "heal_gap"       : 0.005,
    "mp_recover_wait": 0.100,
    "bomu_spell_gap" : 0.200,
    "bomu_interval"  : 185.0,
    "debuff_key_wait": 0.030,
    "debuff_up_wait" : 0.100,
    "debuff_ocr_wait": 0.150,
    "move_hold"      : 0.100,
    "nav_loop"       : 0.010,
    "idle_sleep"     : 0.100,
    "stuck_time"     : 2.0,
    "stuck_back_hold": 0.15,
    "stuck_side_hold": 0.12,
    "combat_timeout" : 12.0,
    "action_loop"    : 0.040,
    "esc_gap"        : 0.050,
}

def humanized_sleep(base_time: float, variance: float = 0.15) -> None:
    if base_time <= 0: return
    sigma  = base_time * variance * 0.5
    delay  = random.gauss(base_time, sigma)
    # 어떤 경우에도 0 이상의 값이 전달되도록 보장
    delay  = max(0.001, max(base_time * 0.50, min(base_time * 2.0, delay)))
    time.sleep(delay)

def tc(key: str, variance: float = 0.15) -> float:
    base = TIMING_CONFIG.get(key, 0.05)
    sigma = base * variance * 0.5
    v = random.gauss(base, sigma)
    return max(base * 0.50, min(base * 2.0, v))

# ============================================================
# ② 시스템 상태 및 환경 명칭 변경
# ============================================================
@dataclass
class Region:
    """OCR 영역을 나타내는 dataclass"""
    sx: int
    sy: int
    dx: int
    dy: int

    def to_bbox(self, offset=(0, 0)):
        ox, oy = offset
        return (self.sx + ox, self.sy + oy, self.dx + ox, self.dy + oy)

class TargetType(Enum):
    INSTANT       = "TYPE_I"
    TARGET_SELECT = "TYPE_S"
    TARGET_INPUT  = "TYPE_A"

class AppStatus(Enum):
    SETUP          = "ST_0"
    IDLE           = "ST_1"
    EMERGENCY_HEAL = "ST_2"
    MANA_REGEN     = "ST_3"
    BUFFING        = "ST_4"
    DEBUFFING      = "ST_5"
    COMBAT         = "ST_6"
    SCANNING       = "ST_7"
    STUCK_ESCAPE   = "ST_8"

def find_game_window(substring: str) -> Optional[int]:
    """게임 윈도우 핸들을 찾습니다."""
    found_hwnd = None
    def enum_cb(hwnd, _):
        nonlocal found_hwnd
        title = win32gui.GetWindowText(hwnd)
        if substring in title and win32gui.IsWindowVisible(hwnd):
            found_hwnd = hwnd
            return False
        return True
    win32gui.EnumWindows(enum_cb, None)
    return found_hwnd

def load_config() -> dict:
    """config.json을 로드합니다."""
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save_config(conf: dict) -> None:
    """config.json을 저장합니다."""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(conf, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"[Config] 저장 실패: {e}")

def grab_window_bg(hwnd: int, rect_ignored: any = None) -> Optional[Image.Image]:
    """win32gui 기반 화면 캡처"""
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = right - left
        height = bottom - top

        # win32gui로 캡처
        hwndDC = win32gui.GetWindowDC(hwnd)
        mfcDC = win32ui.CreateDCFromHandle(hwndDC)
        saveDC = mfcDC.CreateCompatibleDC()
        saveBitMap = win32ui.CreateBitmap()
        saveBitMap.CreateCompatibleBitmap(mfcDC, width, height)
        saveDC.SelectObject(saveBitMap)
        saveDC.BitBlt((0, 0), (width, height), mfcDC, (0, 0), win32con.SRCCOPY)
        bmpinfo = saveBitMap.GetInfo()
        bmpstr = saveBitMap.GetBitmapBits(True)
        img = Image.frombuffer('RGB', (bmpinfo['bmWidth'], bmpinfo['bmHeight']), bmpstr, 'raw', 'BGRX', 0, 1)

        mfcDC.DeleteDC()
        saveDC.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwndDC)
        win32gui.DeleteObject(saveBitMap.GetHandle())

        return img
    except Exception as e:
        print(f"[Capture] 캡처 실패: {e}")
        return None

@dataclass
class Skill:
    name:           str
    target_type:    str
    hotkey:         Optional[str] = None
    spell_char:     Optional[str] = None
    enable_red_tab: bool          = False
    category:       str           = "GENERIC"
    cooldown:       float         = 0.0
    last_cast_time: float         = 0.0

    def is_ready(self) -> bool:
        return (time.time() - self.last_cast_time) >= self.cooldown

# ============================================================
# ③ GameState
# ============================================================
class GameState:
    def __init__(self):
        self._lock = threading.Lock()
        self.hwnd    = None
        self.hp      = 0
        self.mp      = 0
        self.exp     = 0
        self.money   = 0
        self.x       = 0
        self.y       = 0
        self.target_locked = False
        self.last_update_time = time.time()
        self.running          = True
        self.ocr_enabled      = False
        self.auto_hunt        = False
        self.spells: list[Skill] = []
        self.hp_trig_active   = False
        self.mp_trig_active   = False
        self.recovery_hp_spell = ""
        self.recovery_mp_spell = ""
        self.win_x = None
        self.win_y = None
        self.nav_follow_enabled = True
        self.nav_route_enabled  = True
        self.nav_avoid_enabled  = True
        self.waypoints_db       = {}
        self.current_map        = "MAP_DEFAULT"
        self.follow_target_pos  = None
        self.last_pos           = (0, 0)
        self.stuck_start_time   = 0.0
        self.last_key_context = "NONE"
        self.target_type      = "NONE"
        self.target_name      = ""
        self.user_name        = ""
        self.is_chat_active   = False
        self.shield_active      = False
        self.shield_expire_time = 0.0
        self.target_monster_names: list[str] = []
        self.whitelist_names:      list[str] = []
        self.last_debuff_x         = 0
        self.last_debuff_y         = 0
        self.last_bomu_time        = 0.0
        self.auto_debuff_enabled   = True
        self.recovery_debuff_spell = "NONE"
        self.user_alarm_enabled = True
        self.user_stop_enabled  = False
        self.user_next_enabled  = True
        self.combat_start_time = 0.0
        self.maps_db = {}
        self.char_grid = (0, 0)
        self.detected_item_grid = None
        self.detected_item_name = ""
        
        # 해상도 관련 속성
        self.game_resolution = (854, 480)
        self.game_scale = 2.0
        self.gui_resolution = (640, 480)
        
        # 역할 관련 속성 (술사 고정)
        self.role = "술사"
        
        # SentinelThread 관련 속성
        self.monster_on_screen = False
        self.is_user_detected = False
        self.safe_zone_detected = False
        self.is_combat_busy = False
        self.sentinel_enabled = False
        
        # 맵 이동 관련 속성
        self.current_map = ""
        self.current_waypoint_index = 0
        self.map_direction = "forward"
        self.is_moving = False
        self.idle_time = 0.0
        self.last_position = (0, 0)
        self.obstacle_avoid_count = 0

        # OCR 영역 관련 속성
        self.regions = {}

    def update(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)

    def get_all(self) -> dict:
        with self._lock:
            return {
                "hp": self.hp, "mp": self.mp,
                "exp": self.exp, "money": self.money,
                "x": self.x, "y": self.y,
                "target_locked": self.target_locked
            }

    def set_game_scale(self, scale: float):
        """게임 창 배율 설정 (1.0 또는 2.0)"""
        with self._lock:
            self.game_scale = scale
            base_w, base_h = 854, 480
            new_w = int(16 + (base_w * scale))
            new_h = int(39 + (base_h * scale))
            self.game_resolution = (new_w, new_h)
            return new_w, new_h

# ============================================================
# ④ DevInterface
# ============================================================
class DevInterface:
    """하드웨어 직렬 통신을 처리하는 클래스입니다."""
    def __init__(self):
        self.ser   = None
        self._lock = threading.Lock()
        self.state: Optional[GameState] = None

    def set_state(self, state: GameState):
        """게임 상태 설정"""
        self.state = state

    def connect(self, port: str) -> bool:
        """시리얼 포트 연결 (115200 baud)"""
        try:
            self.ser = serial.Serial(port, 115200, timeout=1)
            return True
        except Exception:
            return False

    def send(self, cmd: str):
        """명령어 전송"""
        with self._lock:
            current_hwnd = win32gui.GetForegroundWindow()
            is_focused   = (self.state is not None and 
                            current_hwnd == self.state.hwnd)
            if not is_focused:
                return

            if self.ser and self.ser.is_open:
                try:
                    padding = ' ' * random.randint(1, 16)
                    payload = f"{cmd}{padding}\n".encode('ascii')
                    self.ser.write(payload)
                except Exception:
                    pass

    def humanized_press(self, key: str, variance: float = 0.15):
        pause = random.uniform(0.05, 0.15)
        time.sleep(pause)
        
        self.send(f"D:{key}")
        humanized_sleep(TIMING_CONFIG["key_down_hold"], variance)
        self.send(f"U:{key}")

    def press_with_gap(self, key: str, variance: float = 0.15):
        self.humanized_press(key, variance)
        pause = random.uniform(0.05, 0.15)
        time.sleep(pause)

    def panic_escape(self, count: int = None):
        n = count if count is not None else random.randint(2, 3)
        for _ in range(n):
            self.humanized_press("esc")
            humanized_sleep(TIMING_CONFIG["esc_gap"])

    def hold_move(self, direction: str, hold_key: str = "move_hold", variance: float = 0.15):
        pause = random.uniform(0.05, 0.15)
        time.sleep(pause)
        
        self.send(f"D:{direction}")
        humanized_sleep(TIMING_CONFIG[hold_key], variance)
        self.send(f"U:{direction}")

dev_link = DevInterface()

# ============================================================
# 5. PatternMatcher — 메모리 캐싱 / NMS 기반 다목적 엔진 (SOTA)
# ============================================================
class PatternMatcher:
    MATCH_THRESHOLD   = 0.75
    NMS_OVERLAP       = 0.30
    DIGIT_THRESHOLD   = 0.30
    DIGIT_NCC_WEIGHT  = 0.60
    DIGIT_XOR_WEIGHT  = 0.40
    TARGET_SIZE       = (16, 20)
    FOLDERS = ("digits", "monsters", "status", "users", "items", "maps")
    HSV_RANGES = [
        (np.array([30, 30, 50]), np.array([90, 200, 255])),  # 초록색
        (np.array([10, 50, 150]), np.array([35, 255, 255])),  # 황금색
    ]

    def __init__(self, template_root: str, state: GameState = None):
        self.state = state
        self.templates = {f: {} for f in self.FOLDERS}
        self.digit_templates = {}
        self._load_all(template_root)
        self._load_digit_templates(template_root)

    def _load_digit_templates(self, root: str):
        digits_path = os.path.join(root, "digits")
        if not os.path.isdir(digits_path): return
        tw, th = self.TARGET_SIZE
        for i in range(10):
            path = os.path.join(digits_path, f"{i}.png")
            if not os.path.exists(path): continue
            img = cv2.imread(path)
            if img is None: continue
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
            for low, high in self.HSV_RANGES:
                mask = cv2.bitwise_or(mask, cv2.inRange(hsv, low, high))
            cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not cnts: continue
            c = max(cnts, key=cv2.contourArea)
            x, y, w, h = cv2.boundingRect(c)
            body = mask[y:y+h, x:x+w]
            res = cv2.resize(body, (tw, th), interpolation=cv2.INTER_AREA)
            _, binary = cv2.threshold(res, 1, 1, cv2.THRESH_BINARY)
            self.digit_templates[i] = {"data": binary.astype(np.uint8)}

    def _load_all(self, root: str):
        for folder in self.FOLDERS:
            f_path = os.path.join(root, folder)
            if not os.path.isdir(f_path): continue
            for fn in os.listdir(f_path):
                if fn.lower().endswith((".png", ".jpg", ".bmp")):
                    img = cv2.imread(os.path.join(f_path, fn))
                    if img is not None:
                        self.templates[folder][os.path.splitext(fn)[0]] = img
        print(f"[PM] PatternMatcher 로드: { {f: len(v) for f, v in self.templates.items()} }")

    def recognize(self, crop, folder, field_name=""):
        if folder == "digits":
            if field_name in ("money", "x", "y"):
                text, _ = self._recognize_scored_green(crop, 1) # 황금색
            else:
                text, _ = self._recognize_scored_green(crop, 0) # 초록색
            return text
        return self._recognize_single(crop, folder)

    def _recognize_scored_green(self, crop, range_idx):
        if crop is None or crop.size == 0: return "", 0.0
        h, w = crop.shape[:2]
        hsv = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)
        low, high = self.HSV_RANGES[range_idx]
        mask = cv2.inRange(hsv, low, high)
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        for c in cnts:
            if cv2.contourArea(c) < 10: continue
            bx, by, bw, bh = cv2.boundingRect(c)
            if bh < h // 3: continue
            boxes.append((bx, by, bw, bh))
        boxes.sort(key=lambda b: b[0])
        chars, scores = [], []
        tw, th = self.TARGET_SIZE
        for bx, by, bw, bh in boxes:
            digit_crop = mask[by:by+bh, bx:bx+bw]
            res = cv2.resize(digit_crop, (tw, th), interpolation=cv2.INTER_AREA)
            _, binary = cv2.threshold(res, 1, 255, cv2.THRESH_BINARY)
            best_d, best_diff = None, float('inf')
            for d, tmpl in self.digit_templates.items():
                t_bin = (tmpl["data"] * 255).astype(np.uint8)
                diff = int(cv2.countNonZero(cv2.absdiff(binary, t_bin)))
                if diff < best_diff:
                    best_diff, best_d = diff, d
            if best_d is not None:
                chars.append(str(best_d))
                scores.append(1.0 - (best_diff / (tw * th)))
        return "".join(chars), (min(scores) if scores else 0.0)

    def _recognize_single(self, crop, folder):
        bgr = cv2.cvtColor(crop, cv2.COLOR_RGB2BGR)
        best_n, best_s = "", 0.0
        for name, tmpl in self.templates[folder].items():
            if tmpl.shape[0] > bgr.shape[0] or tmpl.shape[1] > bgr.shape[1]: continue
            res = cv2.matchTemplate(bgr, tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_v, _, _ = cv2.minMaxLoc(res)
            if max_v > best_s: best_s, best_n = max_v, name
        return best_n if best_s >= self.MATCH_THRESHOLD else ""

    def recognize_with_score(self, crop, folder):
        bgr = cv2.cvtColor(crop, cv2.COLOR_RGB2BGR)
        best_n, best_s = "", 0.0
        for name, tmpl in self.templates[folder].items():
            if tmpl.shape[0] > bgr.shape[0] or tmpl.shape[1] > bgr.shape[1]: continue
            res = cv2.matchTemplate(bgr, tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_v, _, _ = cv2.minMaxLoc(res)
            if max_v > best_s: best_s, best_n = max_v, name
        return best_n, best_s

    def match_all_locations(self, crop, folder):
        bgr = cv2.cvtColor(crop, cv2.COLOR_RGB2BGR)
        results = []
        for name, tmpl in self.templates[folder].items():
            h, w = tmpl.shape[:2]
            if h > bgr.shape[0] or w > bgr.shape[1]: continue
            res = cv2.matchTemplate(bgr, tmpl, cv2.TM_CCOEFF_NORMED)
            loc = np.where(res >= self.MATCH_THRESHOLD)
            for pt in zip(*loc[::-1]):
                results.append({"name": name, "x": pt[0], "y": pt[1], "w": w, "h": h, "score": res[pt[1], pt[0]]})
        return results

# ============================================================
# 6. MonitorSvc — 실시간 데이터 분석 엔진 (OBS 가상 카메라 대응)
# ============================================================
class MonitorSvc(threading.Thread):
    NUMERIC_FIELDS = {"hp", "mp", "exp", "money", "x", "y"}

    def __init__(self, state: GameState, config_file: str, maps_file: str = None):
        super().__init__(daemon=True)
        self.state = state
        self.config_file = config_file
        self.maps_file = maps_file or os.path.join(SCRIPT_DIR, "maps.json")
        self.regions = {}
        self.last_hashes = {}
        self.last_load_time = time.time()
        self.matcher = PatternMatcher(TEMPLATE_ROOT, self.state)
        self.load_roi()
        self.load_maps()
        self.obs_params = (0, 0, 1.0, 1.0) # (ox, oy, scx, scy)

    def load_roi(self):
        if os.path.exists(self.config_file):
            with open(self.config_file, "r", encoding="utf-8") as f:
                conf = json.load(f)
                self.regions = {k: Region(v["sx"], v["sy"], v["dx"], v["dy"]) for k, v in conf.items() if isinstance(v, dict) and "sx" in v}

    def load_maps(self):
        if self.maps_file and os.path.exists(self.maps_file):
            try:
                with open(self.maps_file, "r", encoding="utf-8") as f:
                    self.state.maps_db = json.load(f)
                print(f"[Map] maps.json 로드 완료 ({len(self.state.maps_db)}개 맵).")
            except Exception as e:
                print(f"[Map] 로드 실패: {e}")

    def detect_obs_game_offset(self, frame):
        fh, fw = frame.shape[:2]
        # 갈색(게임 테두리) 기반 창 감지
        mask = cv2.inRange(frame, np.array([10, 30, 60]), np.array([95, 135, 175]))
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if cnts:
            c = max(cnts, key=cv2.contourArea)
            x, y, w, h = cv2.boundingRect(c)
            if w > 800:
                scale = h / 1080.0 # 1080p 기준
                self.obs_params = (x, y, scale, scale)
                return self.obs_params
        self.obs_params = (0, 0, 1.0, 1.0)
        return self.obs_params

    def _calc_grid(self, abs_x: int, abs_y: int):
        """절대 픽셀 좌표 → 격자(Grid) 좌표 변환"""
        map_data = self.state.maps_db.get(self.state.current_map)
        if not map_data: return None
        pa = map_data["play_area"]
        gs = map_data.get("grid_size", 48)
        gx = (abs_x - pa["sx"]) // gs
        gy = (abs_y - pa["sy"]) // gs
        return (int(gx), int(gy))

    def run(self):
        print("[Vision] 분석 엔진 가동 (OBS 가상 카메라 모드)")
        indices = [0, 1, 2]
        cap = None
        for idx in indices:
            c = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
            if c.isOpened():
                cap = c; break
        if not cap: return

        while self.state.running:
            # ── Hot-Reload ──────────────────────────────────
            if self.state.last_update_time > self.last_load_time:
                self.load_roi()
                self.load_maps()
                self.matcher._load_all(TEMPLATE_ROOT)
                self.matcher._load_digit_templates(TEMPLATE_ROOT)
                self.last_load_time = time.time()
                self.last_hashes.clear()
                print("[Update] [Sync] 설정 및 템플릿 실시간 갱신 완료.")

            if not self.state.ocr_enabled:
                time.sleep(0.5); continue

            hwnd = find_game_window("바람의나라")
            if not hwnd:
                self.state.hwnd = None; time.sleep(2); continue
            self.state.hwnd = hwnd
            
            # ── 포커스 감시 및 비상 정지 ────────────────────
            if self.state.service_active:
                if win32gui.GetForegroundWindow() != hwnd:
                    self.state.service_active = False
                    dev_link.panic_release() # 하드웨어 신호 즉시 소거
                    print("[Warn] 게임창 비활성화 감지 - 비상 정지 실행")
                    if hasattr(self.state, "gui"):
                        self.state.gui.log("🚨 [비상정지] 게임창 포커스 해제됨")

            ret, frame = cap.read()
            if not ret: time.sleep(0.1); continue

            if self.obs_params == (0, 0, 1.0, 1.0):
                self.detect_obs_game_offset(frame)

            ox, oy, sc, _ = self.obs_params
            img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            self.state.ocr_preview_img = img_rgb.copy()
            
            updates = {}
            h_trig_active = False
            m_trig_active = False
            
            for name, reg in self.regions.items():
                sx = int(reg.sx * sc + ox)
                sy = int(reg.sy * sc + oy)
                dx = int(reg.dx * sc + ox)
                dy = int(reg.dy * sc + oy)
                
                # 픽셀 트리거
                if reg.sx == reg.dx:
                    try:
                        px = img_rgb[int(reg.sy * sc + oy), int(reg.sx * sc + ox)]
                        active = all(p < 55 for p in px)
                        if name == "hp_trig": h_trig_active = active
                        elif name == "mp_trig": m_trig_active = active
                    except: pass
                    continue

                crop = img_rgb[sy:dy, sx:dx]
                if crop.size == 0: continue
                
                h_val = hash(crop.tobytes())
                if self.last_hashes.get(name) == h_val: continue
                self.last_hashes[name] = h_val

                if name in self.NUMERIC_FIELDS:
                    res = self.matcher.recognize(crop, "digits", field_name=name)
                    if res:
                        try: updates[name] = int(res)
                        except: pass
                elif name == "target_info":
                    updates["target_name"], _ = self.matcher.recognize_with_score(crop, "monsters")
                elif name == "user_info":
                    updates["user_name"], _ = self.matcher.recognize_with_score(crop, "users")
                elif name == "items_scan":
                    detections = self.matcher.match_all_locations(crop, "items")
                    if detections:
                        best = sorted(detections, key=lambda d: d["score"], reverse=True)[0]
                        updates["detected_item_name"] = best["name"]
            
            self.state.hp_trig_active = h_trig_active
            self.state.mp_trig_active = m_trig_active
            if updates: self.state.update(**updates)
            
            map_data = self.state.maps_db.get(self.state.current_map)
            if map_data:
                pa = map_data["play_area"]
                char_abs_x = (pa["sx"] + pa["ex"]) // 2
                char_abs_y = (pa["sy"] + pa["ey"]) // 2
                self.state.update(char_grid=self._calc_grid(char_abs_x, char_abs_y))

            time.sleep(0.01)
        cap.release()

# ============================================================
# ⑦ SentinelThread
# ============================================================
class SentinelThread(threading.Thread):
    """술사 PC 전용 보안 감시병 스레드"""
    CONFIDENCE_THRESHOLD = 0.75
    
    def __init__(self, state: GameState, config_file: str):
        super().__init__(daemon=True)
        self.state = state
        self.config_file = config_file
        self.regions: dict = {}
        self.last_load_time = time.time()
        self.enabled = False
        self.scan_interval = random.randint(30, 180)
        self.debug_mode = False
        
        self.matcher = PatternMatcher(TEMPLATE_ROOT, self.state)
        
        self.load_roi()
    
    def load_roi(self):
        """ROI 설정 로드"""
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                conf = json.load(f)
            for name, coords in conf.items():
                if name in ["target_info", "user_info"]:
                    self.regions[name] = coords
        except Exception as e:
            if self.debug_mode:
                print(f"[Sentinel] ROI 로드 실패: {e}")
    
    def run(self):
        """메인 루프"""
        while self.state.running:
            if self.state.last_update_time > self.last_load_time:
                self.load_roi()
                self.last_load_time = time.time()
            
            if not self.state.sentinel_enabled:
                time.sleep(1)
                continue
            
            if self.state.hp_trig_active or self.state.is_combat_busy:
                time.sleep(1)
                continue
            
            self.scan()
            
            self.scan_interval = random.randint(30, 180)
            time.sleep(self.scan_interval)
    
    def scan(self):
        """스캔 수행"""
        if not self.regions.get("target_info") or not self.regions.get("user_info"):
            return
        
        cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        if not cap.isOpened():
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        cap.set(cv2.CAP_PROP_FPS, 60)
        
        hwnd = find_game_window("바람")
        if not hwnd:
            cap.release()
            return
        
        last_monster_name = self.state.target_name
        last_user_name = self.state.user_name
        
        monster_match_count = 0
        user_match_count = 0
        fail_count = 0
        max_scans = 15
        
        for _ in range(max_scans):
            dev_link.humanized_press("tab")
            humanized_sleep(TIMING_CONFIG["tab_wait"])
            dev_link.humanized_press("up")
            humanized_sleep(TIMING_CONFIG["up_wait"])
            dev_link.humanized_press("enter")
            humanized_sleep(TIMING_CONFIG["ocr_wait"])
            
            ret, frame = cap.read()
            if not ret:
                continue
            
            c_left, c_top = win32gui.ClientToScreen(hwnd, (0, 0))
            
            if "target_info" in self.regions:
                reg = self.regions["target_info"]
                sx, sy = c_left + reg["sx"], c_top + reg["sy"]
                dx, dy = c_left + reg["dx"], c_top + reg["dy"]
                
                cam_w, cam_h = frame.shape[1], frame.shape[0]
                win_w, win_h = dx - c_left, dy - c_top
                
                scale_x = cam_w / win_w
                scale_y = cam_h / win_h
                cam_sx = int(reg["sx"] * scale_x)
                cam_sy = int(reg["sy"] * scale_y)
                cam_dx = int(reg["dx"] * scale_x)
                cam_dy = int(reg["dy"] * scale_y)
                
                if cam_dx > cam_sx and cam_dy > cam_sy:
                    crop = frame[cam_sy:cam_dy, cam_sx:cam_dx]
                    if crop.size > 0:
                        monster_result, score = self.matcher.recognize_with_score(crop, "monsters")
                        if monster_result and score >= self.CONFIDENCE_THRESHOLD:
                            if monster_result != last_monster_name:
                                monster_match_count += 1
                                last_monster_name = monster_result
                                self.state.target_name = monster_result
                                if self.debug_mode:
                                    print(f"[Sentinel] 몬스터 감지: {monster_result} (score={score:.2f})")
            
            if "user_info" in self.regions:
                reg = self.regions["user_info"]
                
                cam_sx = int(reg["sx"] * scale_x)
                cam_sy = int(reg["sy"] * scale_y)
                cam_dx = int(reg["dx"] * scale_x)
                cam_dy = int(reg["dy"] * scale_y)
                
                if cam_dx > cam_sx and cam_dy > cam_sy:
                    crop = frame[cam_sy:cam_dy, cam_sx:cam_dx]
                    if crop.size > 0:
                        user_result, score = self.matcher.recognize_with_score(crop, "users")
                        if user_result and score >= self.CONFIDENCE_THRESHOLD:
                            if user_result not in self.state.whitelist_names:
                                self.state.is_user_detected = True
                                self.state.user_name = user_result
                                if self.debug_mode:
                                    print(f"[Sentinel] 미확인 유저 감지: {user_result} (score={score:.2f})")
                                dev_link.humanized_press("+")
                                humanized_sleep(TIMING_CONFIG["enter_wait"])
                                cap.release()
                                return
                            else:
                                user_match_count += 1
                                if user_match_count >= 5:
                                    self.state.safe_zone_detected = True
                                    if self.debug_mode:
                                        print("[Sentinel] 안전 지대 감지 (화이트리스트 유저 5회 연속)")
                                    dev_link.humanized_press("+")
                                    humanized_sleep(TIMING_CONFIG["enter_wait"])
                                    cap.release()
                                    return
            
            if "걸리지 않습니다" in self.state.target_name:
                fail_count += 1
                if fail_count >= 5:
                    if self.debug_mode:
                        print("[Sentinel] 유효 타겟 부재 -> 스캔 조기 종료")
                    dev_link.humanized_press("+")
                    humanized_sleep(TIMING_CONFIG["enter_wait"])
                    cap.release()
                    return
            else:
                fail_count = 0
            
            if self.state.target_name == last_monster_name and self.state.user_name == last_user_name:
                if self.debug_mode:
                    print("[Sentinel] 타겟팅 고정 -> 스캔 종료")
                dev_link.humanized_press("+")
                humanized_sleep(TIMING_CONFIG["enter_wait"])
                cap.release()
                return
            
            dev_link.humanized_press("+")
            humanized_sleep(TIMING_CONFIG["enter_wait"])
        
        cap.release()
        
        if monster_match_count >= 5:
            self.state.monster_on_screen = True
            if self.debug_mode:
                print("[Sentinel] 몬스터 존재 감지 (5회 이상 매칭)")
        else:
            self.state.monster_on_screen = False

# ============================================================
# ⑧ NavigationThread
# ============================================================
class NavigationThread(threading.Thread):
    """술사 첨사냥 모드 맵 이동 스레드"""
    
    def __init__(self, state: GameState, maps_file: str):
        super().__init__(daemon=True)
        self.state = state
        self.maps_file = maps_file
        self.enabled = False
        self.last_load_time = time.time()
        self.maps_db = {}
        self.load_maps()
    
    def load_maps(self):
        """맵 데이터 로드"""
        try:
            with open(self.maps_file, "r", encoding="utf-8") as f:
                self.maps_db = json.load(f)
        except Exception as e:
            print(f"[Navigation] 맵 로드 실패: {e}")
    
    def run(self):
        """메인 루프"""
        while self.state.running:
            if self.state.last_update_time > self.last_load_time:
                self.load_maps()
                self.last_load_time = time.time()
            
            if not self.state.is_moving:
                time.sleep(0.1)
                continue
            
            if self.state.current_map not in self.maps_db:
                print(f"[Navigation] 맵 없음: {self.state.current_map}")
                self.state.is_moving = False
                time.sleep(0.1)
                continue
            
            map_data = self.maps_db[self.state.current_map]
            waypoints = map_data.get("waypoints", [])
            
            if not waypoints:
                print("[Navigation] 웨이포인트 없음")
                self.state.is_moving = False
                time.sleep(0.1)
                continue
            
            if self.state.current_waypoint_index >= len(waypoints):
                exit_point = map_data.get("exit_point")
                if exit_point:
                    next_map = exit_point.get("next_map")
                    if next_map and next_map in self.maps_db:
                        self.state.current_map = next_map
                        self.state.current_waypoint_index = 0
                        print(f"[Navigation] 맵 이동: {next_map}")
                    else:
                        print("[Navigation] 다음 맵 없음")
                        self.state.is_moving = False
                else:
                    print("[Navigation] 종료지점 없음")
                    self.state.is_moving = False
                time.sleep(0.1)
                continue
            
            target_waypoint = waypoints[self.state.current_waypoint_index]
            target_x = target_waypoint["x"]
            target_y = target_waypoint["y"]
            
            current_pos = (self.state.x, self.state.y)
            if current_pos == self.state.last_position:
                self.state.idle_time += 0.1
            else:
                self.state.idle_time = 0.0
                self.state.last_position = current_pos
            
            if self.state.idle_time >= 1.5:
                print("[Navigation] 장애물 감지! 회피 기동")
                self._avoid_obstacle()
                self.state.idle_time = 0.0
                time.sleep(0.1)
                continue
            
            if abs(self.state.x - target_x) < 5 and abs(self.state.y - target_y) < 5:
                print(f"[Navigation] 웨이포인트 {self.state.current_waypoint_index} 도착")
                self.state.current_waypoint_index += 1
                wait_time = random.uniform(1.0, 5.0)
                time.sleep(wait_time)
                time.sleep(0.1)
                continue
            
            self._move_to_target(target_x, target_y)
            time.sleep(0.1)
    
    def _move_to_target(self, target_x: int, target_y: int):
        """타겟 좌표로 이동"""
        if abs(self.state.x - target_x) > abs(self.state.y - target_y):
            if self.state.x > target_x:
                dev_link.humanized_press("left")
            else:
                dev_link.humanized_press("right")
        else:
            if self.state.y > target_y:
                dev_link.humanized_press("up")
            else:
                dev_link.humanized_press("down")
    
    def _avoid_obstacle(self):
        """장애물 회피 로직"""
        self.state.obstacle_avoid_count += 1
        
        directions = ["up", "down", "left", "right"]
        direction = random.choice(directions)
        dev_link.humanized_press(direction)
        time.sleep(0.2)
        
        if self.state.obstacle_avoid_count >= 5:
            print("[Navigation] 장애물 회피 5회 이상 - 몬스터 탐지 시전")
            dev_link.humanized_press("tab")
            time.sleep(0.1)
            dev_link.humanized_press("up")
            time.sleep(0.1)
            dev_link.humanized_press("enter")
            time.sleep(0.1)
            
            if self.state.target_name and self.state.target_name != "":
                print("[Navigation] 몬스터 탐지됨 - 전투")
                self.state.obstacle_avoid_count = 0
                return
            
            dev_link.humanized_press("esc")
            time.sleep(0.1)
            self.state.obstacle_avoid_count = 0

# ============================================================
# ⑨ LogicSvc (술사 모드만)
# ============================================================
from svc_kernel import Skill, GameState, dev_link, Region, TIMING_CONFIG, humanized_sleep, find_game_window

class LogicSvc(threading.Thread):
    """술사 첨사냥 로직 스레드"""

    def __init__(self, state: GameState):
        super().__init__(daemon=True)
        self.state         = state
        self.current_state = AppStatus.IDLE
        self.last_load_time = time.time()
        self.task_active   = False

    def _execute_v3_heal(self, skill):
        """자가 회복"""
        if skill.hotkey:
            dev_link.humanized_press(skill.hotkey)
        humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
        dev_link.humanized_press("home")
        humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
        dev_link.humanized_press("enter")
        skill.last_cast_time = time.time()

    def _recover_hp(self):
        heal_name  = self.state.recovery_hp_spell
        if not heal_name or heal_name == "사용 안 함": return
        heal_skill = next((s for s in self.state.spells if s.name == heal_name), None)
        if not heal_skill: return
        if self.state.hp_trig_active:
            self._execute_v3_heal(heal_skill)
            humanized_sleep(TIMING_CONFIG["heal_gap"])

    def _recover_mp(self):
        mana_name  = self.state.recovery_mp_spell
        if not mana_name or mana_name == "사용 안 함": return
        mana_skill = next((s for s in self.state.spells if s.name == mana_name), None)
        if not mana_skill: return
        if self.state.mp_trig_active:
            print(f"[MP] MP 부족 감지 - {mana_name} 시전 중...")
            self._execute_v3_heal(mana_skill)
            humanized_sleep(TIMING_CONFIG["mp_recover_wait"])

    def _execute_bomu_buff_v3(self):
        """보무 버프"""
        print("[Buff] 보무 버프 시전 중 (8 -> 9 순차)...")
        for key in ("8", "9"):
            dev_link.humanized_press(key)
            humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
            dev_link.humanized_press("home")
            humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
            dev_link.humanized_press("enter")
            if key == "8":
                humanized_sleep(TIMING_CONFIG["bomu_spell_gap"])
        self.state.last_bomu_time = time.time()
        print("[OK] 보무 버프 완료.")

    def _execute_debuff_scan_v3(self):
        """디버프 스캔"""
        debuff_spell = self.state.recovery_debuff_spell
        if not debuff_spell or debuff_spell == "사용 안 함": return

        skill = next((s for s in self.state.spells if s.name == debuff_spell), None)
        if not skill: return

        print(f"[Debuff] 디버프 스캔 시작: {debuff_spell}")
        num_cycles  = random.randint(10, 20)
        fail_count  = 0
        start_time  = time.time()
        timeout     = TIMING_CONFIG["combat_timeout"]

        for _ in range(num_cycles):
            if time.time() - start_time > timeout * random.uniform(0.9, 1.1):
                print("[Failsafe] [FAIL-SAFE] 디버프 스캔 타임아웃 -> 루프 강제 중단.")
                dev_link.panic_escape()
                break

            dev_link.humanized_press(skill.hotkey)
            humanized_sleep(TIMING_CONFIG["debuff_key_wait"])
            dev_link.humanized_press("up")
            humanized_sleep(TIMING_CONFIG["debuff_up_wait"])
            dev_link.humanized_press("enter")
            humanized_sleep(TIMING_CONFIG["debuff_ocr_wait"])

            if "걸리지 않습니다" in self.state.target_name:
                fail_count += 1
                print(f"[Warn] 타겟 무효 ({fail_count}/5)")
                if fail_count >= 5:
                    print("[Stop] 유효 타겟 부재 -> 디버프 스캔 조기 종료.")
                    dev_link.panic_escape(2)
                    break
            else:
                fail_count = 0

        self.state.last_debuff_x = self.state.x
        self.state.last_debuff_y = self.state.y
        print("[OK] 디버프 스캔 완료.")

    def _search_target_v3(self):
        """타겟 탐색"""
        dev_link.humanized_press("tab")
        humanized_sleep(TIMING_CONFIG["tab_wait"])
        dev_link.humanized_press("up")
        humanized_sleep(TIMING_CONFIG["up_wait"])
        dev_link.humanized_press("enter")
        humanized_sleep(TIMING_CONFIG["ocr_wait"])
        self.state.target_locked = True

    def _validate_target(self) -> bool:
        """타겟 검증"""
        name = self.state.target_name
        if not name:
            return False

        if "걸리지 않습니다" in name:
            dev_link.humanized_press("esc")
            self.state.target_locked = False
            self.state.target_name   = ""
            return False

        if any(m in name for m in self.state.target_monster_names):
            return True

        print(f"[Misc] 비몬스터 타겟: '{name}' -> ESC.")
        dev_link.humanized_press("esc")
        self.state.target_locked = False
        self.state.target_name   = ""
        return False

    def _handle_unidentified_user(self):
        """미확인 유저 대응"""
        import winsound
        if self.state.user_alarm_enabled:
            winsound.MessageBeep()
        if self.state.user_next_enabled:
            dev_link.humanized_press("esc")
            self.state.target_locked = False
            self.state.user_name     = ""
        if self.state.user_stop_enabled:
            print("[Stop] 미확인 유저 -> 사냥 중단.")
            self.state.auto_hunt = False

    def _execute_combat(self):
        """전투"""
        if not self._validate_target():
            return

        if self.state.combat_start_time == 0.0:
            self.state.combat_start_time = time.time()

        elapsed = time.time() - self.state.combat_start_time
        timeout = TIMING_CONFIG["combat_timeout"] * random.uniform(0.90, 1.10)
        if elapsed > timeout:
            print(f"[Failsafe] [FAIL-SAFE] 전투 타임아웃({elapsed:.1f}s) -> 강제 IDLE 복귀.")
            dev_link.panic_escape()
            self.state.target_locked      = False
            self.state.target_name        = ""
            self.state.combat_start_time  = 0.0
            self.current_state            = AppStatus.IDLE
            return

        for s in self.state.spells:
            if s.category == "공격" and s.is_ready():
                dev_link.humanized_press(s.hotkey)
                humanized_sleep(TIMING_CONFIG["spell_cast_gap"])
                s.last_cast_time = time.time()
                break

    def _move_toward_grid(self, gx: int, gy: int) -> bool:
        """Grid 좌표로 이동"""
        ccx, ccy = self.state.char_grid
        dgx, dgy = gx - ccx, gy - ccy

        if abs(dgx) > 0:
            dev_link.hold_move("right" if dgx > 0 else "left", "move_hold")
        elif abs(dgy) > 0:
            dev_link.hold_move("down" if dgy > 0 else "up", "move_hold")
        
        return abs(dgx) == 0 and abs(dgy) == 0

    def _run_priest_mode(self):
        """술사 첨사냥 로직"""
        if not self.state.auto_hunt:
            self.state.last_bomu_time    = 0
            self.state.combat_start_time = 0.0
            self.state.is_moving = False
            humanized_sleep(TIMING_CONFIG["idle_sleep"])
            return

        if self.state.hp_trig_active:
            self._recover_hp()
            self.state.combat_start_time = 0.0

        if self.state.mp_trig_active:
            self._recover_mp()
            if self.state.hp_trig_active:
                self._recover_hp()

        if time.time() - self.state.last_bomu_time > TIMING_CONFIG["bomu_interval"]:
            self._execute_bomu_buff_v3()

        if (self.state.user_name and
                self.state.user_name not in self.state.whitelist_names):
            print(f"[Alert] 미확인 유저 감지: {self.state.user_name}")
            self._handle_unidentified_user()
            if not self.state.auto_hunt:
                return

        if self.state.current_map and self.state.is_moving:
            pass
        elif not self.state.is_moving and self.state.current_map:
            self.state.is_moving = True
            self.state.current_waypoint_index = 0
            print(f"[Priest] 맵 이동 시작: {self.state.current_map}")

        if not self.state.target_locked and self.state.detected_item_grid:
            gx, gy = self.state.detected_item_grid
            print(f"[Item] 아이템 획득 시도: {self.state.detected_item_name} @ Grid({gx}, {gy})")

            while self.state.running and self.state.auto_hunt and self.state.detected_item_grid:
                if self.state.target_locked or self.state.hp_trig_active:
                    print("[Action] 획득 중 전투/위급 상황 발생 -> 루팅 중단.")
                    break

                arrived = self._move_toward_grid(gx, gy)
                if arrived:
                    print(f"[Point] 아이템 위 도착. 획득 시도 (',')")
                    dev_link.humanized_press(",") 
                    humanized_sleep(TIMING_CONFIG["enter_wait"])
                    self.state.detected_item_grid = None
                    print("[OK] 아이템 획득 완료.")
                    break
                
                humanized_sleep(TIMING_CONFIG["nav_loop"])

        dist_change = (abs(self.state.x - self.state.last_debuff_x) +
                       abs(self.state.y - self.state.last_debuff_y))
        if self.state.auto_debuff_enabled and dist_change > 15:
            self._execute_debuff_scan_v3()

        if self.state.monster_on_screen:
            if not self.state.target_locked:
                self.state.combat_start_time = 0.0
                self._search_target_v3()
            
            humanized_sleep(TIMING_CONFIG["ocr_fast"])
            self._execute_combat()
            
            if not self.state.target_locked:
                print("[Priest] 전투 완료 - 다시 이동")
        else:
            if not self.state.target_locked:
                self.state.combat_start_time = 0.0
                self._search_target_v3()
            
            humanized_sleep(TIMING_CONFIG["ocr_fast"])
            self._execute_combat()

        humanized_sleep(TIMING_CONFIG["action_loop"])

    def run(self):
        print(f"[Action] 술사 첨사냥 엔진 시작...")
        while self.state.running:

            if self.state.last_update_time > self.last_load_time:
                self.last_load_time = time.time()
                print("[Sync] [Action] 마법 설정 실시간 동기화.")

            self._run_priest_mode()

# ============================================================
# ⑩ 캘리브레이션 오버레이 클래스
# ============================================================
class OverlaySelector(tk.Toplevel):
    """실시간 게임창 위에서 직접 영역을 지정하는 오버레이 선택기"""
    def __init__(self, parent, hwnd, callback):
        super().__init__(parent)
        self.callback = callback
        self.hwnd = hwnd
        self.msg_id = None
        self.rect_id = None

        # 게임창 클라이언트 영역 위치 파악
        left, top = win32gui.ClientToScreen(self.hwnd, (0, 0))
        _, _, w, h = win32gui.GetClientRect(self.hwnd)

        print(f"DEBUG: Overlay Positioning -> Physical Rect: {w}x{h} at ({left}, {top})")

        # 오버레이 설정
        self.overrideredirect(True)
        self.attributes("-alpha", 0.3)
        self.attributes("-topmost", True)
        self.geometry(f"{w}x{h}+{left}+{top}")
        self.configure(bg="black")
        self.lift()

        self.canvas = tk.Canvas(self, width=w, height=h, bg="black", highlightthickness=0, cursor="cross")
        self.canvas.pack(fill="both", expand=True)

        self.start_x = 0; self.start_y = 0; self.end_x = 0; self.end_y = 0

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

        # 안내 문구
        self.canvas.create_text(w//2, h//2, text="영역을 드래그하세요\n(드래그 완료 시 자동 저장 및 종료)",
                               fill="#FFFFFF", font=("Inter", 16, "bold"), justify="center")

    def on_press(self, event):
        self.start_x, self.start_y = event.x, event.y
        if self.rect_id: self.canvas.delete(self.rect_id)
        if self.msg_id: self.canvas.delete(self.msg_id)
        self.rect_id = self.canvas.create_rectangle(self.start_x, self.start_y, self.start_x, self.start_y, outline="#33FF33", width=3)

    def on_drag(self, event):
        self.canvas.coords(self.rect_id, self.start_x, self.start_y, event.x, event.y)

    def on_release(self, event):
        self.end_x, self.end_y = event.x, event.y
        sx, dx = sorted([self.start_x, self.end_x])
        sy, dy = sorted([self.start_y, self.end_y])

        # 성공 피드백 시각화
        self.canvas.itemconfig(self.rect_id, outline="#10B981", fill="#10B981", stipple="gray25")
        self.msg_id = self.canvas.create_text((sx+dx)//2, (sy+dy)//2, text="SAVE SUCCESS!", fill="white", font=("Inter", 18, "bold"))

        # 좌표 전달
        self.callback(sx, sy, dx, dy)
        self.after(800, self.destroy)

# ============================================================
# ⑪ GUI
# ============================================================
class AppView:
    def __init__(self, state: GameState):
        self.state = state
        self.root = ctk.CTk()
        self.root.title("술사 첨사냥 모드")
        self.root.geometry("900x600")

        # UI 스케일 관련 속성
        self.ui_scale = 1.0
        self.ui_scale_min = 0.8
        self.ui_scale_max = 1.8
        self.ui_scale_step = 0.1

        # 캘리브레이션 관련 속성
        self.calibration_popup = None
        self.calibration_geometry = None
        self.preview_zoom_var = None
        self.preview_trim = None
        self.preview_crop_pil = None
        self.preview_tkimg = None
        self.tune_target = None
        self.zoom_menu = None
        self.trim_info_lbl = None
        self.preview_meta_lbl = None
        self.preview_canvas = None

        # Toast 메시지 관련 속성
        self.toast_lbl = None

        self.setup_ui()
        self.bind_global_zoom_controls()
        self.last_preview_hash = None  # EYE 탭 캔버스 최적화용
        
        # 아두이노 자동 연결 시도 (메인 프로젝트 기능 이식)
        self.auto_connect_hardware()
        
        self.update_gui()

    def auto_connect_hardware(self):
        """프로그램 시작 시 아두이노 자동 연결 시도 (메인 프로젝트 기능 이식)"""
        try:
            import serial.tools.list_ports
            comports = serial.tools.list_ports.comports()
            # USB나 특정 칩셋 키워드가 포함된 포트 필터링
            ports = []
            for p in comports:
                desc = (p.description or "").upper()
                hwid = (p.hwid or "").upper()
                if any(k in desc or k in hwid for k in ["USB", "CH340", "CP210", "ARDUINO", "SERIAL"]):
                    ports.append(p.device)
            
            if not ports:
                print("[Hardware] 자동 연결 가능한 포트 없음")
                return False
            
            for port in ports:
                if dev_link.connect(port):
                    print(f"[Hardware] 자동 연결 성공: {port}")
                    # 포트 탭 레이블 업데이트 (있을 경우)
                    if hasattr(self, 'lbl_port'):
                        self.lbl_port.configure(text=f"포트: {port} (자동 연결)")
                    return True
        except Exception as e:
            print(f"[Hardware] 자동 연결 에러: {e}")
        return False
        
    def setup_ui(self):
        # 메인 프레임
        main_frame = ctk.CTkFrame(self.root)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        # 좌측 버튼 프레임
        left_frame = ctk.CTkFrame(main_frame, width=200)
        left_frame.pack(side="left", fill="y", padx=5, pady=5)
        
        # 버튼들
        self.btn_start = ctk.CTkButton(left_frame, text="START", command=self.toggle_service)
        self.btn_start.pack(pady=5)
        
        self.btn_stop = ctk.CTkButton(left_frame, text="STOP", command=self.stop_service)
        self.btn_stop.pack(pady=5)
        
        self.btn_sentinel = ctk.CTkButton(left_frame, text="SENTINEL OFF", command=self.toggle_sentinel)
        self.btn_sentinel.pack(pady=5)

        ctk.CTkLabel(left_frame, text="맵 선택").pack(pady=(10, 2))
        self.cb_map = ctk.CTkComboBox(left_frame, values=[], height=26, width=120, font=("Inter", 9), command=self.set_current_map)
        self.cb_map.pack(pady=2)

        self.btn_map_move = ctk.CTkButton(left_frame, text="이동 OFF", command=self.toggle_map_move)
        self.btn_map_move.pack(pady=5)

        # 게임 해상도 스케일 버튼
        scale_row = ctk.CTkFrame(left_frame, fg_color="transparent")
        scale_row.pack(pady=5)
        ctk.CTkButton(scale_row, text="1배", width=50, height=24, font=("Inter", 9), command=lambda: self.set_game_scale(1.0)).pack(side="left", padx=2)
        ctk.CTkButton(scale_row, text="2배", width=50, height=24, font=("Inter", 9), command=lambda: self.set_game_scale(2.0)).pack(side="left", padx=2)

        self.btn_port = ctk.CTkButton(left_frame, text="PORT", command=self.check_ports)
        self.btn_port.pack(pady=5)
        
        self.btn_connect = ctk.CTkButton(left_frame, text="연결", command=self.connect_port)
        self.btn_connect.pack(pady=5)
        
        # 우측 상태정보 프레임
        right_frame = ctk.CTkFrame(main_frame)
        right_frame.pack(side="right", fill="both", expand=True, padx=5, pady=5)
        
        # Tabbed GUI (Extended)
        # Tabbed GUI (Unified)
        self.tabview = ctk.CTkTabview(right_frame)
        self.tabview.pack(fill="both", expand=True)
        
        # 탭 일괄 생성
        self.tab_dashboard = self.tabview.add("대시보드")
        self.tab_eye       = self.tabview.add("EYE")
        self.tab_spells    = self.tabview.add("마법관리")
        self.tab_logic     = self.tabview.add("로직&AI")
        self.tab_nav       = self.tabview.add("네비게이션")
        self.tab_calib     = self.tabview.add("캘리브레이션")
        self.tab_port      = self.tabview.add("포트")

        # EYE 탭 구성 (실시간 프리뷰)
        self.canvas_eye = ctk.CTkCanvas(self.tab_eye, bg="black", highlightthickness=0)
        self.canvas_eye.pack(fill="both", expand=True, padx=5, pady=5)

        # 대시보드 탭 구성
        self.lbl_hp = ctk.CTkLabel(self.tab_dashboard, text="HP: -", font=("Consolas", 15, "bold"))
        self.lbl_hp.pack(pady=10)
        self.lbl_mp = ctk.CTkLabel(self.tab_dashboard, text="MP: -", font=("Consolas", 15, "bold"))
        self.lbl_mp.pack(pady=10)
        self.lbl_exp = ctk.CTkLabel(self.tab_dashboard, text="EXP: -")
        self.lbl_exp.pack(pady=5)
        self.lbl_money = ctk.CTkLabel(self.tab_dashboard, text="MONEY: -")
        self.lbl_money.pack(pady=5)
        self.lbl_pos = ctk.CTkLabel(self.tab_dashboard, text="POS: -")
        self.lbl_pos.pack(pady=5)
        
        # 마법관리 탭 구성
        self.spell_db = []
        self.load_spells_db()
        
        trig_ui = ctk.CTkFrame(self.tab_spells, fg_color="#1A1C23", corner_radius=10, border_width=1, border_color="#343A46")
        trig_ui.pack(fill="x", padx=10, pady=5)
        ctk.CTkLabel(trig_ui, text="🩹 자동 회복 전담 마법", font=("Inter", 11, "bold"), text_color="#10B981").pack(pady=5)

        sel_row = ctk.CTkFrame(trig_ui, fg_color="transparent")
        sel_row.pack(fill="x", pady=5, padx=10)
        
        spell_names = [s["name"] for s in self.spell_db if s.get("use", False)]
        if not spell_names: spell_names = ["사용 안 함"]

        ctk.CTkLabel(sel_row, text="HP 마법:", font=("Inter", 11)).grid(row=0, column=0, padx=5)
        self.cb_hp_trig = ctk.CTkComboBox(sel_row, values=spell_names, width=120, font=("Inter", 9))
        self.cb_hp_trig.set(getattr(self.state, "recovery_hp_spell", "사용 안 함"))
        self.cb_hp_trig.grid(row=0, column=1, padx=5)

        ctk.CTkLabel(sel_row, text="MP 마법:", font=("Inter", 11)).grid(row=0, column=2, padx=15)
        self.cb_mp_trig = ctk.CTkComboBox(sel_row, values=spell_names, width=120, font=("Inter", 9))
        self.cb_mp_trig.set(getattr(self.state, "recovery_mp_spell", "사용 안 함"))
        self.cb_mp_trig.grid(row=0, column=3, padx=5)

        sel_row2 = ctk.CTkFrame(trig_ui, fg_color="transparent")
        sel_row2.pack(fill="x", pady=5, padx=10)

        ctk.CTkLabel(sel_row2, text="저주 마법:", font=("Inter", 11)).grid(row=0, column=0, padx=5)
        self.cb_debuff_trig = ctk.CTkComboBox(sel_row2, values=spell_names, width=120, font=("Inter", 9))
        self.cb_debuff_trig.set(getattr(self.state, "recovery_debuff_spell", "사용 안 함"))
        self.cb_debuff_trig.grid(row=0, column=1, padx=5)

        # 스킬 슬롯 할당
        slot_ui = ctk.CTkFrame(self.tab_spells, fg_color="#1A1C23", corner_radius=10, border_width=1, border_color="#343A46")
        slot_ui.pack(fill="both", expand=True, padx=10, pady=5)

        ctk.CTkLabel(slot_ui, text="📜 스킬 슬롯 할당", font=("Inter", 11, "bold"), text_color="#00D4FF").pack(pady=5)

        self.slot_entries = {}
        for i in range(9):  # 1~9 슬롯
            row = ctk.CTkFrame(slot_ui, fg_color="transparent")
            row.pack(fill="x", pady=2, padx=10)
            ctk.CTkLabel(row, text=f"Slot {i+1}", font=("Inter", 10), width=50).pack(side="left", padx=5)
            entry = ctk.CTkEntry(row, width=150, font=("Inter", 9))
            entry.pack(side="left", padx=5)
            self.slot_entries[i+1] = entry
        
        # 로직&AI 관리 탭 구성 (중복 add 제거)
        self.sw_heal = ctk.CTkSwitch(self.tab_logic, text="Heal")
        self.sw_heal.pack(pady=5)
        self.sw_buff = ctk.CTkSwitch(self.tab_logic, text="Buff")
        self.sw_buff.pack(pady=5)
        self.sw_debuff = ctk.CTkSwitch(self.tab_logic, text="Debuff")
        self.sw_debuff.pack(pady=5)
        self.sw_attack = ctk.CTkSwitch(self.tab_logic, text="Attack")
        self.sw_attack.pack(pady=5)
        
        # 네비게이션 & 컨트롤 탭 구성 (중복 add 제거)
        nav_toggle_box = ctk.CTkFrame(self.tab_nav, fg_color="#1A1C23", border_width=1, border_color="#343A46")
        nav_toggle_box.pack(fill="x", padx=10, pady=5)
        ctk.CTkLabel(nav_toggle_box, text="🚀 NAVIGATION MODE", font=("Inter", 12, "bold"), text_color="#10B981").pack(pady=5)

        switch_row = ctk.CTkFrame(nav_toggle_box, fg_color="transparent")
        switch_row.pack(fill="x", pady=5, padx=10)

        self.sw_follow = ctk.CTkCheckBox(switch_row, text="그룹원 추적", font=("Inter", 11), command=self.update_nav_flags)
        self.sw_follow.pack(side="left", padx=15)
        if self.state.nav_follow_enabled:
            self.sw_follow.select()

        self.sw_route = ctk.CTkCheckBox(switch_row, text="경로(WP) 이동", font=("Inter", 11), command=self.update_nav_flags)
        self.sw_route.pack(side="left", padx=15)
        if self.state.nav_route_enabled:
            self.sw_route.select()

        self.sw_avoid = ctk.CTkCheckBox(switch_row, text="장애물 우회", font=("Inter", 11), command=self.update_nav_flags)
        self.sw_avoid.pack(side="left", padx=15)
        if self.state.nav_avoid_enabled:
            self.sw_avoid.select()

        # 유저 탐지 대응 섹션
        user_opt_box = ctk.CTkFrame(self.tab_nav, fg_color="#1A1C23", border_width=1, border_color="#343A46")
        user_opt_box.pack(fill="x", padx=10, pady=5)
        ctk.CTkLabel(user_opt_box, text="👤 USER DETECTION", font=("Inter", 12, "bold"), text_color="#F87171").pack(pady=5)

        # 화이트리스트
        white_row = ctk.CTkFrame(user_opt_box, fg_color="transparent")
        white_row.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(white_row, text="화이트리스트:", font=("Inter", 11)).pack(side="left")
        self.ent_whitelist = ctk.CTkEntry(white_row, width=250, placeholder_text="내이름,친구1,친구2 (쉼표 구분)")
        self.ent_whitelist.pack(side="left", padx=10)

        # 대응 옵션 스위치
        act_row = ctk.CTkFrame(user_opt_box, fg_color="transparent")
        act_row.pack(fill="x", pady=5, padx=10)

        self.sw_u_alarm = ctk.CTkCheckBox(act_row, text="경고음", font=("Inter", 11), command=self.sync_user_opts)
        self.sw_u_alarm.pack(side="left", padx=10)
        if self.state.user_alarm_enabled:
            self.sw_u_alarm.select()

        self.sw_u_stop = ctk.CTkCheckBox(act_row, text="사냥중지", font=("Inter", 11), command=self.sync_user_opts)
        self.sw_u_stop.pack(side="left", padx=10)
        if self.state.user_stop_enabled:
            self.sw_u_stop.select()

        self.sw_u_next = ctk.CTkCheckBox(act_row, text="무시/다음", font=("Inter", 11), command=self.sync_user_opts)
        self.sw_u_next.pack(side="left", padx=10)
        if self.state.user_next_enabled:
            self.sw_u_next.select()

        # 웨이포인트 관리 섹션
        wp_ui = ctk.CTkFrame(self.tab_nav, fg_color="#1A1C23", border_width=1, border_color="#343A46")
        wp_ui.pack(fill="both", expand=True, padx=10, pady=5)

        ctk.CTkLabel(wp_ui, text="📍 웨이포인트 관리", font=("Inter", 12, "bold"), text_color="#00D4FF").pack(pady=5)

        # 웨이포인트 리스트
        self.wp_listbox = tk.Listbox(wp_ui, height=10, font=("Inter", 9), bg="#11161D", fg="#FFFFFF", selectbackground="#10B981")
        self.wp_listbox.pack(fill="both", expand=True, padx=10, pady=5)

        # 웨이포인트 버튼들
        btn_row = ctk.CTkFrame(wp_ui, fg_color="transparent")
        btn_row.pack(fill="x", pady=5, padx=10)

        ctk.CTkButton(btn_row, text="현재 위치 추가", command=self.add_current_waypoint).pack(side="left", padx=5)
        ctk.CTkButton(btn_row, text="선택 삭제", command=self.delete_waypoint).pack(side="left", padx=5)
        ctk.CTkButton(btn_row, text="저장", command=self.save_waypoints).pack(side="left", padx=5)
        
        # 캘리브레이션 탭 상세 구성
        scroll_cal = ctk.CTkScrollableFrame(self.tab_calib)
        scroll_cal.pack(fill="both", expand=True, padx=5, pady=5)
        
        ctk.CTkLabel(scroll_cal, text="🔧 영역 분석 캘리브레이션", font=("Inter", 12, "bold"), text_color="#00D4FF").pack(pady=10)
        
        cal_targets = [
            ("HP 게이지 전체영역", "hp"),
            ("MP 게이지 전체영역", "mp"),
            ("HP 긴급 트리거(1px)", "hp_trig"),
            ("MP 긴급 트리거(1px)", "mp_trig"),
            ("타겟 이름 인식영역", "target_info"),
            ("유저 탐지 인식영역", "user_info"),
            ("바닥 아이템 스캔영역", "items_scan"),
            ("금전/좌표 수치영역", "money"),
            ("미니맵 맵 정보영역", "map_info")
        ]
        
        for label, key in cal_targets:
            row = ctk.CTkFrame(scroll_cal, fg_color="transparent")
            row.pack(fill="x", pady=2, padx=10)
            ctk.CTkLabel(row, text=label, width=180, anchor="w").pack(side="left")
            ctk.CTkButton(row, text="영역 설정", width=100, font=("Inter", 10),
                         command=lambda k=key: self.start_calibration(k)).pack(side="right")
        
        ctk.CTkButton(self.tab_calib, text="기존 팝업 열기", command=self.open_calibration_popup).pack(pady=5)
        
    def setup_port_tab(self):
        root = self.tab_port
        ctk.CTkLabel(root, text="🔌 하드웨어 포트 관리", font=("Inter", 13, "bold"), text_color="#00D4FF").pack(pady=15)
        
        self.lbl_port_st = ctk.CTkLabel(root, text="상태: 연결 대기 중", font=("Inter", 11))
        self.lbl_port_st.pack(pady=5)
        
        self.port_menu = ctk.CTkOptionMenu(root, values=["COM1", "COM2", "COM3"], width=200)
        self.port_menu.pack(pady=10)
        
        btn_row = ctk.CTkFrame(root, fg_color="transparent")
        btn_row.pack(pady=10)
        ctk.CTkButton(btn_row, text="목록 갱신", width=100, command=self.refresh_ports).pack(side="left", padx=5)
        ctk.CTkButton(btn_row, text="수동 연결", width=100, command=self.connect_port_manual).pack(side="left", padx=5)

    def refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if ports:
            self.port_menu.configure(values=ports)
            self.port_menu.set(ports[0])
            self.log(f"포트 목록 갱신 완료: {len(ports)}개 발견")
        else:
            self.log("연결 가능한 시리얼 포트가 없습니다.")

    def connect_port_manual(self):
        port = self.port_menu.get()
        if dev_link.connect(port):
            self.lbl_port_st.configure(text=f"상태: {port} 연결됨", text_color="#10B981")
            self.log(f"{port} 포트에 수동 연결되었습니다.")
        else:
            self.log(f"{port} 포트 연결 시도 실패")

    def log(self, msg):
        now = datetime.now().strftime("%H:%M:%S")
        self.log_txt.insert("end", f"[{now}] {msg}\n")
        self.log_txt.see("end")

    def draw_preview(self):
        img = self.state.ocr_preview_img
        if img is None: return
        
        try:
            cw = self.canvas_eye.winfo_width()
            ch = self.canvas_eye.winfo_height()
            if cw < 30 or ch < 30: return
            
            # 비율 유지하며 리사이즈 (PIL 사용)
            h, w = img.shape[:2]
            ratio = min(cw/w, ch/h)
            nw, nh = int(w * ratio), int(h * ratio)
            
            img_rs = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
            pil_img = Image.fromarray(img_rs)
            tk_img = ImageTk.PhotoImage(pil_img)
            
            self.canvas_eye.delete("all")
            self.canvas_eye.create_image(cw//2, ch//2, anchor="center", image=tk_img)
            self.canvas_eye._tkimg = tk_img 
        except Exception as e:
            pass

        # Toast 메시지 레이블
        self.toast_lbl = ctk.CTkLabel(self.root, text="", font=("Inter", 12, "bold"), text_color="#10B981")
        self.toast_lbl.pack(side="bottom", pady=10)

        # 업데이트 시작
        self.load_map_list()
        self.update_gui()

    def load_map_list(self):
        """맵 목록 로드"""
        try:
            with open(MAPS_FILE, "r", encoding="utf-8") as f:
                maps_db = json.load(f)
            map_names = list(maps_db.keys())
            self.cb_map.configure(values=map_names)
            if map_names:
                self.cb_map.set(map_names[0])
                self.state.current_map = map_names[0]
        except Exception as e:
            print(f"[Map] 맵 로드 실패: {e}")

    def set_current_map(self, choice):
        """현재 맵 설정"""
        self.state.current_map = choice
        print(f"[Map] 맵 설정: {choice}")
        self.show_toast(f"맵 설정: {choice}")
        self.load_waypoints()

    def load_spells_db(self):
        """스킬 데이터베이스 로드"""
        try:
            with open(SPELL_DB_FILE, "r", encoding="utf-8") as f:
                self.spell_db = json.load(f)
            print(f"[Spells] 스킬 데이터베이스 로드 완료: {len(self.spell_db)}개")
        except Exception as e:
            print(f"[Spells] 스킬 데이터베이스 로드 실패: {e}")
            self.spell_db = []

    def save_spells_db(self):
        """스킬 데이터베이스 저장"""
        try:
            with open(SPELL_DB_FILE, "w", encoding="utf-8") as f:
                json.dump(self.spell_db, f, indent=4, ensure_ascii=False)
            self.show_toast("스킬 데이터베이스 저장 완료")
        except Exception as e:
            print(f"[Spells] 스킬 데이터베이스 저장 실패: {e}")
            self.show_toast("스킬 데이터베이스 저장 실패", "#FF5555")

    def update_nav_flags(self):
        """네비게이션 플래그 업데이트"""
        self.state.nav_follow_enabled = self.sw_follow.get()
        self.state.nav_route_enabled = self.sw_route.get()
        self.state.nav_avoid_enabled = self.sw_avoid.get()
        print(f"[Nav] 플래그 업데이트: follow={self.state.nav_follow_enabled}, route={self.state.nav_route_enabled}, avoid={self.state.nav_avoid_enabled}")

    def sync_user_opts(self):
        """유저 탐지 옵션 동기화"""
        self.state.user_alarm_enabled = self.sw_u_alarm.get()
        self.state.user_stop_enabled = self.sw_u_stop.get()
        self.state.user_next_enabled = self.sw_u_next.get()

        # 화이트리스트 업데이트
        whitelist_text = self.ent_whitelist.get()
        self.state.whitelist_names = [name.strip() for name in whitelist_text.split(",") if name.strip()]

        print(f"[User] 유저 탐지 옵션 업데이트: alarm={self.state.user_alarm_enabled}, stop={self.state.user_stop_enabled}, next={self.state.user_next_enabled}")
        print(f"[User] 화이트리스트: {self.state.whitelist_names}")

    def set_game_scale(self, scale: float):
        """게임 창 배율 설정"""
        self.state.game_scale = scale
        new_w = int(854 * scale)
        new_h = int(480 * scale)
        self.state.game_resolution = (new_w, new_h)

        hwnd = find_game_window(WIN_KEY)
        if hwnd:
            try:
                win32gui.SetWindowPos(hwnd, 0, 0, 0, new_w, new_h,
                                     win32con.SWP_NOMOVE | win32con.SWP_NOZORDER)
                self.show_toast(f"해상도 변경: {new_w}x{new_h} ({int(scale)}배)")
            except Exception as e:
                self.show_toast(f"해상도 변경 실패: {e}", "#FF5555")
        else:
            self.show_toast("게임 창을 찾을 수 없음", "#FF5555")

    def bind_global_zoom_controls(self):
        """Ctrl+마우스 휠로 UI 스케일 조절"""
        def on_mouse_wheel(event):
            if event.state & 0x0004:  # Ctrl 키 확인
                delta = 1 if event.delta > 0 else -1
                next_scale = round(self.ui_scale + (delta * self.ui_scale_step), 2)
                next_scale = max(self.ui_scale_min, min(self.ui_scale_max, next_scale))
                if next_scale != self.ui_scale:
                    self.ui_scale = next_scale
                    ctk.set_widget_scaling(self.ui_scale)
                    ctk.set_window_scaling(self.ui_scale)
                    self.show_toast(f"UI SCALE: {int(self.ui_scale * 100)}%")
                return "break"
            return "break"

        self.root.bind("<Control-MouseWheel>", on_mouse_wheel)

    def load_waypoints(self):
        """웨이포인트 로드"""
        try:
            if self.state.current_map and self.state.current_map in self.state.maps_db:
                waypoints = self.state.maps_db[self.state.current_map].get("waypoints", [])
                self.wp_listbox.delete(0, tk.END)
                for wp in waypoints:
                    self.wp_listbox.insert(tk.END, f"({wp['x']}, {wp['y']})")
                print(f"[Waypoints] 웨이포인트 로드 완료: {len(waypoints)}개")
        except Exception as e:
            print(f"[Waypoints] 웨이포인트 로드 실패: {e}")

    def add_current_waypoint(self):
        """현재 위치를 웨이포인트에 추가"""
        x, y = self.state.x, self.state.y
        if x == 0 and y == 0:
            self.show_toast("위치 정보 없음", "#FF5555")
            return
        self.wp_listbox.insert(tk.END, f"({x}, {y})")
        self.show_toast(f"웨이포인트 추가: ({x}, {y})")

    def delete_waypoint(self):
        """선택한 웨이포인트 삭제"""
        selection = self.wp_listbox.curselection()
        if selection:
            self.wp_listbox.delete(selection)
            self.show_toast("웨이포인트 삭제 완료")

    def save_waypoints(self):
        """웨이포인트 저장"""
        try:
            waypoints = []
            for i in range(self.wp_listbox.size()):
                text = self.wp_listbox.get(i)
                # 파싱: (x, y) 형식
                import re
                match = re.match(r'\((\d+),\s*(\d+)\)', text)
                if match:
                    x, y = int(match.group(1)), int(match.group(2))
                    waypoints.append({"x": x, "y": y})

            if self.state.current_map:
                if self.state.current_map not in self.state.maps_db:
                    self.state.maps_db[self.state.current_map] = {}
                self.state.maps_db[self.state.current_map]["waypoints"] = waypoints
                with open(MAPS_FILE, "w", encoding="utf-8") as f:
                    json.dump(self.state.maps_db, f, indent=4, ensure_ascii=False)
                self.show_toast(f"웨이포인트 저장 완료: {len(waypoints)}개")
            else:
                self.show_toast("맵을 먼저 선택하세요", "#FF5555")
        except Exception as e:
            print(f"[Waypoints] 웨이포인트 저장 실패: {e}")
            self.show_toast("웨이포인트 저장 실패", "#FF5555")
        
    def toggle_service(self):
        self.state.auto_hunt = not self.state.auto_hunt
        self.state.ocr_enabled = True
        if self.state.auto_hunt:
            self.btn_start.configure(text="START ON", fg_color="#10B981")
            print("[Service] 자동사냥 활성화")
        else:
            self.btn_start.configure(text="START", fg_color="#343A40")
            print("[Service] 자동사냥 비활성화")
    
    def stop_service(self):
        self.state.auto_hunt = False
        self.state.ocr_enabled = False
        self.btn_start.configure(text="START", fg_color="#343A40")
        print("[Service] 자동사냥 중단")
    
    def toggle_sentinel(self):
        self.state.sentinel_enabled = not self.state.sentinel_enabled
        if self.state.sentinel_enabled:
            self.btn_sentinel.configure(text="SENTINEL ON", fg_color="#10B981")
            print("[Sentinel] 활성화")
        else:
            self.btn_sentinel.configure(text="SENTINEL OFF", fg_color="#343A40")
            print("[Sentinel] 비활성화")
    
    def toggle_map_move(self):
        self.state.is_moving = not self.state.is_moving
        if self.state.is_moving:
            self.btn_map_move.configure(text="이동 ON", fg_color="#10B981")
            self.state.current_waypoint_index = 0
            print("[Map] 맵 이동 활성화")
        else:
            self.btn_map_move.configure(text="이동 OFF", fg_color="#343A40")
            print("[Map] 맵 이동 비활성화")

    def open_calibration_popup(self):
        if hasattr(self, "calibration_popup") and self.calibration_popup and self.calibration_popup.winfo_exists():
            self.calibration_popup.lift()
            self.calibration_popup.focus_force()
            return

        self.calibration_popup = ctk.CTkToplevel(self.root)
        self.calibration_popup.title("Calibration")
        self.calibration_popup.geometry(self.calibration_geometry or "920x420+180+180")
        self.calibration_popup.attributes("-topmost", True)
        self.calibration_popup.transient(self.root)
        self.calibration_popup.minsize(860, 360)

        self.preview_zoom_var = ctk.StringVar(value="x3")
        self.preview_trim = {"left": 0, "right": 0, "top": 0, "bottom": 0}
        self.preview_crop_pil = None
        self.preview_tkimg = None

        body = ctk.CTkFrame(self.calibration_popup, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=6, pady=6)
        body.grid_columnconfigure(0, weight=0)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        left_panel = ctk.CTkFrame(body, width=280, corner_radius=8, fg_color="#1E2129")
        left_panel.grid(row=0, column=0, sticky="nsw", padx=(0, 6))
        left_panel.grid_propagate(False)

        ctk.CTkLabel(left_panel, text="CALIBRATION", font=("Inter", 11, "bold"), text_color="#00D4FF").pack(pady=(6, 4))
        target_grid = ctk.CTkFrame(left_panel, fg_color="transparent")
        target_grid.pack(pady=2)
        targets = ["hp", "mp", "exp", "money", "x", "y", "hp_trig", "mp_trig", "stat_info", "target_info", "user_info"]
        for i, t in enumerate(targets):
            r, c = divmod(i, 3)
            ctk.CTkButton(target_grid, text=t.upper(), width=64, height=22, font=("Inter", 9),
                          command=lambda n=t: self.open_visual_selector(n)).grid(row=r, column=c, padx=2, pady=2)

        ctk.CTkLabel(left_panel, text="Target", font=("Inter", 9)).pack(pady=(4, 1))
        self.tune_target = ctk.CTkOptionMenu(left_panel, values=targets, height=22, width=130, font=("Inter", 9))
        self.tune_target.pack(pady=1)

        dp = ctk.CTkFrame(left_panel, fg_color="transparent")
        dp.pack(pady=3)
        ctk.CTkButton(dp, text="▲", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("sy", -1)).grid(row=0, column=1)
        ctk.CTkButton(dp, text="◀", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("sx", -1)).grid(row=1, column=0)
        ctk.CTkButton(dp, text="▶", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("sx", 1)).grid(row=1, column=2)
        ctk.CTkButton(dp, text="▼", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("sy", 1)).grid(row=2, column=1)

        ctk.CTkLabel(left_panel, text="Preview Zoom", font=("Inter", 9)).pack(pady=(6, 1))
        self.zoom_menu = ctk.CTkOptionMenu(left_panel, values=["x3", "x4", "x5"], variable=self.preview_zoom_var, width=90, height=22, font=("Inter", 9),
                                           command=lambda _: self.update_calibration_preview())
        self.zoom_menu.pack(pady=1)

        trim_box = ctk.CTkFrame(left_panel, fg_color="transparent")
        trim_box.pack(pady=(6, 2))
        ctk.CTkLabel(trim_box, text="1px Crop", font=("Inter", 9, "bold")).grid(row=0, column=0, columnspan=4, pady=(0, 2))
        ctk.CTkButton(trim_box, text="L+", width=36, height=20, font=("Inter", 8), command=lambda: self.adjust_preview_trim("left", 1)).grid(row=1, column=0, padx=1, pady=1)
        ctk.CTkButton(trim_box, text="L-", width=36, height=20, font=("Inter", 8), command=lambda: self.adjust_preview_trim("left", -1)).grid(row=1, column=1, padx=1, pady=1)
        ctk.CTkButton(trim_box, text="R+", width=36, height=20, font=("Inter", 8), command=lambda: self.adjust_preview_trim("right", 1)).grid(row=1, column=2, padx=1, pady=1)
        ctk.CTkButton(trim_box, text="R-", width=36, height=20, font=("Inter", 8), command=lambda: self.adjust_preview_trim("right", -1)).grid(row=1, column=3, padx=1, pady=1)
        ctk.CTkButton(trim_box, text="T+", width=36, height=20, font=("Inter", 8), command=lambda: self.adjust_preview_trim("top", 1)).grid(row=2, column=0, padx=1, pady=1)
        ctk.CTkButton(trim_box, text="T-", width=36, height=20, font=("Inter", 8), command=lambda: self.adjust_preview_trim("top", -1)).grid(row=2, column=1, padx=1, pady=1)
        ctk.CTkButton(trim_box, text="B+", width=36, height=20, font=("Inter", 8), command=lambda: self.adjust_preview_trim("bottom", 1)).grid(row=2, column=2, padx=1, pady=1)
        ctk.CTkButton(trim_box, text="B-", width=36, height=20, font=("Inter", 8), command=lambda: self.adjust_preview_trim("bottom", -1)).grid(row=2, column=3, padx=1, pady=1)
        ctk.CTkButton(trim_box, text="RESET", width=148, height=20, font=("Inter", 8), command=self.reset_preview_trim).grid(row=3, column=0, columnspan=4, padx=1, pady=(2, 1))
        self.trim_info_lbl = ctk.CTkLabel(left_panel, text="L0 R0 T0 B0", font=("Inter", 8), text_color="#9CA3AF")
        self.trim_info_lbl.pack(pady=(1, 3))

        actions_row = ctk.CTkFrame(left_panel, fg_color="transparent")
        actions_row.pack(pady=(3, 6))
        ctk.CTkButton(actions_row, text="CAPTURE", width=90, height=24, font=("Inter", 9, "bold"), command=self.update_calibration_preview).pack(side="left", padx=2)
        ctk.CTkButton(actions_row, text="SAVE", width=90, height=24, font=("Inter", 9, "bold"), command=self.save_calibration_preview).pack(side="left", padx=2)

        ctk.CTkButton(left_panel, text="CLOSE", height=24, width=190, font=("Inter", 9), command=self.close_calibration_popup).pack(pady=(0, 6))

        right_panel = ctk.CTkFrame(body, corner_radius=8, fg_color="#11161D")
        right_panel.grid(row=0, column=1, sticky="nsew")
        ctk.CTkLabel(right_panel, text="LIVE PREVIEW", font=("Inter", 11, "bold"), text_color="#10B981").pack(pady=(6, 2))
        self.preview_meta_lbl = ctk.CTkLabel(right_panel, text="No capture", font=("Inter", 9), text_color="#9CA3AF")
        self.preview_meta_lbl.pack(pady=(0, 4))
        self.preview_canvas = tk.Canvas(right_panel, bg="#0B0F16", highlightthickness=0)
        self.preview_canvas.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        self.calibration_popup.protocol("WM_DELETE_WINDOW", self.close_calibration_popup)
        self.calibration_popup.bind("<Configure>", self.on_calibration_popup_configure)
        self.tune_target.configure(command=lambda _: self.update_calibration_preview())
        self.update_calibration_preview()
        self.show_toast("CALIBRATION POPUP OPEN")

    def on_calibration_popup_configure(self, event):
        if event.widget == self.calibration_popup:
            self.calibration_geometry = self.calibration_popup.geometry()

    def adjust_preview_trim(self, side, delta):
        self.preview_trim[side] = max(0, self.preview_trim.get(side, 0) + delta)
        self.trim_info_lbl.configure(
            text=f"L{self.preview_trim['left']} R{self.preview_trim['right']} T{self.preview_trim['top']} B{self.preview_trim['bottom']}"
        )
        self.update_calibration_preview()

    def reset_preview_trim(self):
        self.preview_trim = {"left": 0, "right": 0, "top": 0, "bottom": 0}
        self.trim_info_lbl.configure(text="L0 R0 T0 B0")
        self.update_calibration_preview()

    def get_trimmed_region_for_preview(self):
        target = self.tune_target.get()
        if not target or target not in self.state.regions:
            return None, target
        base = self.state.regions[target]
        sx = base.sx + self.preview_trim["left"]
        sy = base.sy + self.preview_trim["top"]
        dx = base.dx - self.preview_trim["right"]
        dy = base.dy - self.preview_trim["bottom"]
        if dx <= sx or dy <= sy:
            return None, target
        return Region(sx, sy, dx, dy), target

    def update_calibration_preview(self):
        if not hasattr(self, "calibration_popup") or not self.calibration_popup or not self.calibration_popup.winfo_exists():
            return
        hwnd = find_game_window(WIN_KEY)
        if not hwnd:
            self.preview_meta_lbl.configure(text="Game window not found")
            self.preview_canvas.delete("all")
            return
        reg, target = self.get_trimmed_region_for_preview()
        if reg is None:
            self.preview_meta_lbl.configure(text="Invalid crop range")
            self.preview_canvas.delete("all")
            return

        full_img = grab_window_bg(hwnd)
        if full_img is None:
            self.preview_meta_lbl.configure(text="Capture failed")
            self.preview_canvas.delete("all")
            return

        try:
            crop = full_img.crop(reg.to_bbox((0, 0)))
        except Exception:
            self.preview_meta_lbl.configure(text="Crop failed")
            self.preview_canvas.delete("all")
            return

        zoom = int(self.preview_zoom_var.get().replace("x", ""))
        preview_img = crop.resize((max(1, crop.width * zoom), max(1, crop.height * zoom)), Image.NEAREST)
        self.preview_crop_pil = crop.copy()
        self.preview_tkimg = ImageTk.PhotoImage(preview_img)

        cw = max(1, self.preview_canvas.winfo_width())
        ch = max(1, self.preview_canvas.winfo_height())
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(cw // 2, ch // 2, image=self.preview_tkimg, anchor="center")
        self.preview_meta_lbl.configure(text=f"{target.upper()} | {crop.width}x{crop.height} | zoom x{zoom}")

    def save_calibration_preview(self):
        if self.preview_crop_pil is None:
            messagebox.showwarning("Save Preview", "먼저 CAPTURE로 미리보기를 생성하세요.")
            return
        if not messagebox.askyesno("Save Preview", "현재 미리보기를 저장할까요?"):
            return

        os.makedirs(TEMPLATE_DIGIT_DIR, exist_ok=True)
        default_name = f"{self.tune_target.get()}_{int(time.time())}"
        name = simpledialog.askstring("파일명", "저장할 파일명을 입력하세요 (확장자 제외):", initialvalue=default_name, parent=self.calibration_popup)
        if not name:
            return

        safe_name = "".join(ch for ch in name if ch.isalnum() or ch in ("_", "-", ".")).strip(". ")
        if not safe_name:
            messagebox.showerror("Save Preview", "유효한 파일명을 입력하세요.")
            return
        path = os.path.join(TEMPLATE_DIGIT_DIR, f"{safe_name}.png")

        if os.path.exists(path):
            overwrite = messagebox.askyesno("중복 파일", f"이미 존재합니다.\n덮어쓸까요?\n{path}")
            if not overwrite:
                return

        try:
            self.preview_crop_pil.save(path)
            messagebox.showinfo("Save Preview", f"저장 완료:\n{path}")
        except Exception as e:
            messagebox.showerror("Save Preview", f"저장 실패:\n{e}")

    def close_calibration_popup(self):
        if hasattr(self, "calibration_popup") and self.calibration_popup and self.calibration_popup.winfo_exists():
            try:
                self.calibration_popup.update_idletasks()
                self.calibration_geometry = self.calibration_popup.geometry()
            except Exception:
                pass
            self.calibration_popup.destroy()

    def adj_val(self, attr, delta):
        target = self.tune_target.get()
        if not target or target not in self.state.regions:
            return
        reg = self.state.regions[target]
        curr = getattr(reg, attr)
        setattr(reg, attr, curr + delta)

        # 저장
        conf = load_config()
        conf[target] = asdict(reg)
        save_config(conf)
        self.show_toast(f"# {target.upper()} AREA ADJUSTED")

    def open_visual_selector(self, target_name):
        hwnd = find_game_window(WIN_KEY)
        if not hwnd:
            self.show_toast("[Err] WINDOW NOT FOUND", "#FF5555")
            return

        def save_result(sx, sy, dx, dy):
            self.state.regions[target_name] = Region(sx, sy, dx, dy)
            conf = load_config()
            conf[target_name] = {"sx": sx, "sy": sy, "dx": dx, "dy": dy}
            save_config(conf)
            self.show_toast(f"# {target_name.upper()} AREA SAVED")

        OverlaySelector(self.root, hwnd, save_result)

    def show_toast(self, msg, color="#10B981"):
        if self.toast_lbl:
            self.toast_lbl.configure(text=msg, text_color=color)
            self.root.after(2500, lambda: self.toast_lbl.configure(text=""))
        else:
            self.btn_map_move.configure(text="이동 OFF", fg_color="#343A40")
            print("[Map] 맵 이동 비활성화")
    
    def check_ports(self):
        import serial.tools.list_ports
        ports = serial.tools.list_ports.comports()
        port_list = "\n".join([p.device for p in ports])
        if hasattr(self, "lbl_port_st"):
            self.lbl_port_st.configure(text=f"상태: 탐색 완료\n{port_list}" if port_list else "상태: 포트 없음")
        print(f"[Port] 포트 목록: {port_list}")
        self.log(f"포트 스캔 완료: {len(ports)}개 발견")

    def connect_port(self):
        import serial.tools.list_ports
        ports = serial.tools.list_ports.comports()
        if ports:
            port = ports[0].device
            if dev_link.connect(port):
                if hasattr(self, "lbl_port_st"):
                    self.lbl_port_st.configure(text=f"상태: {port} 연결됨", text_color="#10B981")
                print(f"[Port] {port} 연결 성공")
                self.log(f"{port} 포트에 자동 연결되었습니다.")
            else:
                self.log(f"{port} 연결 시도 실패")
        else:
            self.log("연결할 포트가 없습니다.")
    
    def update_gui(self):
        if not self.state.running: return
        
        # 1. 수치 데이터 (50ms 단위 - 고속 갱신)
        hp_text = self.state.hp_str or str(self.state.hp)
        mp_text = self.state.mp_str or str(self.state.mp)
        exp_text = self.state.exp_str or str(self.state.exp)
        money_text = self.state.money_str or str(self.state.money)
        x_text = self.state.x_str or f"{self.state.x:04d}"
        y_text = self.state.y_str or f"{self.state.y:04d}"

        self.lbl_hp.configure(text=f"HP: {hp_text}")
        self.lbl_mp.configure(text=f"MP: {mp_text}")
        if hasattr(self, "lbl_exp"): self.lbl_exp.configure(text=f"EXP: {exp_text}")
        if hasattr(self, "lbl_money"): self.lbl_money.configure(text=f"MONEY: {money_text}")
        if hasattr(self, "lbl_pos"): self.lbl_pos.configure(text=f"POS: ({x_text}, {y_text})")
        
        # 2. EYE 탭 OCR 프리뷰 (100ms 단위 - 리소스 관리)
        current_time = time.time()
        if not hasattr(self, "_last_eye_update"): self._last_eye_update = 0
        
        if self.tabview.get() == "EYE" and (current_time - self._last_eye_update >= 0.1):
            if self.state.ocr_preview_img is not None:
                self.draw_preview()
                self._last_eye_update = current_time
            
        self.root.after(50, self.update_gui)
    
    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.mainloop()
    
    def on_close(self):
        self.state.running = False
        self.root.destroy()

# ============================================================
# ⑪ Main
# ============================================================
def main():
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    state = GameState()
    
    config_file = os.path.join(SCRIPT_DIR, "config.local.json")
    maps_file = os.path.join(SCRIPT_DIR, "maps.json")
    
    reader_thread = MonitorSvc(state, config_file, maps_file)
    action_thread = LogicSvc(state)
    navigation_thread = NavigationThread(state, maps_file)
    sentinel_thread = SentinelThread(state, config_file)
    
    reader_thread.start()
    action_thread.start()
    navigation_thread.start()
    sentinel_thread.start()
    
    print("[OK] System started.")
    
    gui = AppView(state)
    gui.run()
    
    print("[Stop] System closed.")

if __name__ == "__main__":
    main()
