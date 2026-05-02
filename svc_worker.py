# -*- coding: utf-8 -*-
import time
import threading
import random
import os
import sys
import io
import json
import csv
import io

# 터미널 인코딩 강제 설정 (CP949 환경 대응))))
try:
    if sys.stdout.encoding != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding='utf-8', line_buffering=True)
    if sys.stderr.encoding != 'utf-8':
        sys.stderr = io.TextIOWrapper(sys.stderr.detach(), encoding='utf-8', line_buffering=True)
except Exception:
    pass

from typing import Dict, Any, Optional, Tuple

try:
    from google import genai
except ImportError:
    genai = None

import cv2
import numpy as np
import tkinter as tk
from tkinter import ttk
import customtkinter as ctk
import win32gui
import win32con
import win32api
import os
import json
import csv
import time
from datetime import datetime
import keyboard
import threading
import serial.tools.list_ports
import ctypes
import zmq
import queue
from tkinter import ttk, messagebox, simpledialog
from PIL import Image, ImageTk
from svc_monitor import NumericFieldScanner

# Pillow 10+ 호환 리샘플링 (구버전은 정수 0 = NEAREST)
try:
    from PIL.Image import Resampling
    _PIL_NEAREST = Resampling.NEAREST
except ImportError:
    _PIL_NEAREST = 0
from dataclasses import dataclass, asdict
from typing import Tuple, Optional, Dict
from enum import Enum, auto

# 커널 클래스 임포트
from bis_core import Skill, GameState, hw, Region, TIMING_CONFIG, humanized_sleep
from svc_stealth import StealthChecker
from svc_monitor import MonitorSvc, SentinelThread, CaptureSvc
from bis_logic import LogicSvc, RouteSvc

# 고해상도(65인치 등) 모니터 호환성을 위한 DPI 인식 활성화
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

WIN_KEY = "ory"
MULT = 1
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "game_log.csv")
LOG_FILE_STR = os.path.join(SCRIPT_DIR, "game_log_str.csv")

# ZeroMQ 설정 (노트북인 경우 PUB, 데스크탑인 경우 SUB 설정 가능)
IS_SERVER = True  # 노트북인 경우 True
SERVER_IP = "192.168.137.1" # 노트북의 ICS IP
ZMQ_PORT = "5555"

CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
GUI_STATE_FILE = os.path.join(SCRIPT_DIR, "gui_state.json")
TEMPLATE_DIGIT_DIR = os.path.join(SCRIPT_DIR, "temple", "digits")

def dict_to_region(data: dict) -> Region:
    """dict를 Region 객체로 변환하는 헬퍼 함수 (Grid 좌표 지원)"""
    if isinstance(data, Region):
        return data
    if isinstance(data, dict):
        # Grid 좌표가 있는 경우 픽셀 좌표 우선 사용
        sx = data.get("sx_pixel", data.get("sx", 0))
        sy = data.get("sy_pixel", data.get("sy", 0))
        dx = data.get("dx_pixel", data.get("dx", 0))
        dy = data.get("dy_pixel", data.get("dy", 0))
        return Region(sx, sy, dx, dy)
    raise ValueError(f"Invalid data type for Region: {type(data)}")

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            conf = json.load(f)
            # 좌표 영역만 골라서 변환 (dict이고 'sx'가 있는 경우만)
            return {k: dict_to_region(v) for k, v in conf.items()
                    if isinstance(v, dict) and 'sx' in v}
    # DEFAULT_REGIONS도 Region 객체로 변환
    return {k: dict_to_region(v) for k, v in DEFAULT_REGIONS.items()}

def save_config(regions_dict):
    # Region 객체를 dict로 변환하여 저장 (Grid 좌표 병행 저장)
    serializable_dict = {}
    for k, v in regions_dict.items():
        if isinstance(v, Region):
            # 기존 픽셀 좌표 유지하면서 Grid 좌표도 저장
            region_dict = asdict(v)
            # Grid 좌표가 없으면 빈 값으로 초기화
            region_dict["grid_start"] = region_dict.get("grid_start", [0, 0])
            region_dict["grid_end"] = region_dict.get("grid_end", [0, 0])
            serializable_dict[k] = region_dict
        else:
            serializable_dict[k] = v
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(serializable_dict, f, indent=4)

def load_gui_state():
    default_state = {"geometry": "600x680", "ui_scale": 1.0, "calibration_geometry": "420x270+180+180"}
    if not os.path.exists(GUI_STATE_FILE):
        return default_state

    try:
        with open(GUI_STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        geometry = data.get("geometry", default_state["geometry"])
        ui_scale = float(data.get("ui_scale", default_state["ui_scale"]))
        ui_scale = max(0.8, min(1.8, ui_scale))
        calibration_geometry = data.get("calibration_geometry", default_state["calibration_geometry"])
        return {"geometry": geometry, "ui_scale": ui_scale, "calibration_geometry": calibration_geometry}
    except Exception:
        return default_state

def save_gui_state(geometry: str, ui_scale: float, calibration_geometry: Optional[str] = None):
    data = {"geometry": geometry, "ui_scale": ui_scale}
    if calibration_geometry:
        data["calibration_geometry"] = calibration_geometry
    elif os.path.exists(GUI_STATE_FILE):
        try:
            with open(GUI_STATE_FILE, 'r', encoding='utf-8') as f:
                prev = json.load(f)
            if "calibration_geometry" in prev:
                data["calibration_geometry"] = prev["calibration_geometry"]
        except Exception:
            pass
    with open(GUI_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)

# 기본 좌표 설정 (ocr 인식 기본 캡쳐영역 바람의 나라 UI 기준 예시)
DEFAULT_REGIONS = {
    "hp": {"sx": 650, "sy": 434, "dx": 740, "dy": 446},
    "mp": {"sx": 650, "sy": 447, "dx": 740, "dy": 459},
    "exp": {"sx": 640, "sy": 460, "dx": 740, "dy": 472},
    "money": {"sx": 650, "sy": 473, "dx": 740, "dy": 485},
    "x": {"sx": 650, "sy": 486, "dx": 695, "dy": 498},
    "y": {"sx": 695, "sy": 486, "dx": 740, "dy": 498}
}

# --- Window Management ---
def find_game_window(substring: str) -> Optional[int]:
    found_hwnd = None
    def enum_cb(hwnd, _):
        nonlocal found_hwnd
        title = win32gui.GetWindowText(hwnd)
        if substring in title and win32gui.IsWindowVisible(hwnd):
            found_hwnd = hwnd
            return False # 찾았으므로 중단
        return True
    
    try:
        win32gui.EnumWindows(enum_cb, None)
    except Exception:
        pass
    return found_hwnd

def grab_window_bg(hwnd: int, rect_ignored=None) -> Optional[Image.Image]:
    """win32gui 기반 게임 화면 캡처 (PrintWindow로 DirectX 렌더링 캡처)"""
    try:
        import win32ui
        # 클라이언트 영역 크기
        _, _, width, height = win32gui.GetClientRect(hwnd)

        # 화면 DC 생성
        hwndDC = win32gui.GetWindowDC(hwnd)
        mfcDC = win32ui.CreateDCFromHandle(hwndDC)
        saveDC = mfcDC.CreateCompatibleDC()
        saveBitMap = win32ui.CreateBitmap()
        saveBitMap.CreateCompatibleBitmap(mfcDC, width, height)
        saveDC.SelectObject(saveBitMap)

        # PrintWindow: ctypes로 user32.PrintWindow 직접 호출
        # PW_RENDERFULLCONTENT(2) = DirectX/GPU 렌더링 화면 캡처
        user32 = ctypes.windll.user32
        result = user32.PrintWindow(hwnd, saveDC.GetSafeHdc(), 2)

        if result == 0:
            # PrintWindow 실패 시 BitBlt로 폴백
            saveDC.BitBlt((0, 0), (width, height), mfcDC, (0, 0), win32con.SRCCOPY)

        # PrintWindow는 윈도우 전체(타이틀바 포함)를 캡처하므로
        # 클라이언트 영역 오프셋을 계산하여 크롭
        win_rect = win32gui.GetWindowRect(hwnd)
        client_left, client_top = win32gui.ClientToScreen(hwnd, (0, 0))
        offset_x = client_left - win_rect[0]
        offset_y = client_top - win_rect[1]

        bmpinfo = saveBitMap.GetInfo()
        bmpstr = saveBitMap.GetBitmapBits(True)
        full_img = Image.frombuffer('RGB',
                               (bmpinfo['bmWidth'], bmpinfo['bmHeight']),
                               bmpstr, 'raw', 'BGRX', 0, 1)

        # 클라이언트 영역만 크롭하여 반환
        img = full_img.crop((offset_x, offset_y, offset_x + width, offset_y + height))
        mfcDC.DeleteDC()
        saveDC.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwndDC)
        win32gui.DeleteObject(saveBitMap.GetHandle())
        return img
    except Exception as e:
        print(f"[Capture] 캡처 실패: {e}")
        return None

class OverlaySelector(tk.Toplevel):
    """실시간 게임창 위에서 직접 영역을 지정하는 오버레이 선택기 (DPI 호환성 보강)"""
    def __init__(self, parent, hwnd, callback):
        super().__init__(parent)
        self.callback = callback
        self.hwnd = hwnd
        self.msg_id = None
        self.rect_id = None

        # 1. 게임창 클라이언트 영역 위치 파악 (물리 좌표)
        left, top = win32gui.ClientToScreen(self.hwnd, (0, 0))
        _, _, w, h = win32gui.GetClientRect(self.hwnd)


        # 2. 오버레이 설정 (표준 Tkinter를 사용하여 CustomTkinter Scaling 간섭 배제)
        self.overrideredirect(True) # 타이틀바 제거
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.05)  # 매우 투명하게 (5% 불투명)
        self.geometry(f"{w}x{h}+{left}+{top}")
        self.configure(bg="black")  # 검정 배경
        self.lift() # 최상단으로 올림

        self.canvas = tk.Canvas(self, width=w, height=h, bg="black", highlightthickness=0, cursor="cross")
        self.canvas.pack(fill="both", expand=True)

        self.start_x = 0; self.start_y = 0; self.end_x = 0; self.end_y = 0

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.bind("<Escape>", self.on_cancel)  # ESC 취소

        # 안내 문구
        self.msg_id = self.canvas.create_text(w//2, h//2, text="영역을 드래그하세요\n(ESC: 취소 / 드래그 완료 시 자동 저장)",
                               fill="#FFFFFF", font=("Inter", 16, "bold"), justify="center")

    def _screen_to_client(self, screen_x, screen_y):
        """화면 좌표 → 게임창 클라이언트 좌표 (물리 픽셀, DPI 무관)"""
        cx, cy = win32gui.ScreenToClient(self.hwnd, (screen_x, screen_y))
        return cx, cy

    def on_press(self, event):
        # Tkinter 좌표 대신 화면 좌표를 클라이언트 좌표로 변환
        screen_x, screen_y = win32api.GetCursorPos()
        self.start_cx, self.start_cy = self._screen_to_client(screen_x, screen_y)

        # Tkinter 캔버스용 좌표 (시각적 피드백만)
        self.start_x, self.start_y = event.x, event.y
        if self.msg_id:
            self.canvas.delete(self.msg_id)
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(self.start_x, self.start_y, self.start_x, self.start_y, outline="#FF0000", width=3)

    def on_drag(self, event):
        self.canvas.coords(self.rect_id, self.start_x, self.start_y, event.x, event.y)

    def on_release(self, event):
        # Tkinter 좌표 대신 화면 좌표를 클라이언트 좌표로 변환 (물리 픽셀)
        screen_x, screen_y = win32api.GetCursorPos()
        end_cx, end_cy = self._screen_to_client(screen_x, screen_y)

        # 드래그 완료 즉시 캡처
        captured_img = grab_window_bg(self.hwnd)

        # 물리 픽셀 좌표 정렬 (DPI 스케일링 완전 무관)
        sx = min(self.start_cx, end_cx)
        sy = min(self.start_cy, end_cy)
        dx = max(self.start_cx, end_cx)
        dy = max(self.start_cy, end_cy)

        if captured_img:
            try:
                iw, ih = captured_img.size
                csx = max(0, min(iw - 1, sx))
                csy = max(0, min(ih - 1, sy))
                cdx = max(csx + 1, min(iw, dx))
                cdy = max(csy + 1, min(ih, dy))
                
                crop = captured_img.crop((csx, csy, cdx, cdy))
                print(f"[DEBUG] 샘플링 물리좌표: ({sx},{sy})-({dx},{dy}) | 보정좌표: ({csx},{csy})-({cdx},{cdy}) | 캡처크기: {iw}x{ih}")
            except Exception as e:
                print(f"[DEBUG] 크롭 처리 중 오류: {e}")
        else:
            print(f"[DEBUG] 캡처 실패! captured_img=None")

        # 오버레이 즉시 닫기
        self.destroy()

        # 좌표 + 캡처 이미지 함께 전달
        self.callback(sx, sy, dx, dy, captured_img)

    def on_cancel(self, event):
        """ESC 키로 취소"""
        self.destroy()
        print("[DEBUG] 샘플링 취소됨")

class ROIIndicator(tk.Toplevel):
    """지정된 OCR 영역을 게임 화면 위에 상시 표시하는 가이드 레이어 (Click-through)"""
    def __init__(self, parent, hwnd):
        super().__init__(parent)
        self.hwnd = hwnd
        self.visible = False
        
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-transparentcolor", "black") # 블랙 색상은 100% 투명 및 클릭-스루
        self.configure(bg="black")
        
        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        
        self.withdraw() # 초기에는 숨김

    def update_position(self, regions):
        if not self.visible:
            self.withdraw()
            return

        try:
            left, top = win32gui.ClientToScreen(self.hwnd, (0, 0))
            _, _, w, h = win32gui.GetClientRect(self.hwnd)
            self.geometry(f"{w}x{h}+{left}+{top}")
            self.deiconify()
            self.lift()  # 최상단으로 강제 이동

            self.canvas.delete("all")
            # 2026 SOTA neon colors
            colors = {"hp": "#FF4B4B", "mp": "#00D4FF", "exp": "#A855F7", "money": "#FFB800", "x": "#10B981", "y": "#10B981"}

            for name, reg in regions.items():
                color = colors.get(name, "#FFFFFF")
                # 실제 인식 영역보다 사방으로 3px 넓게 그려서 캡처 영역 침범 방지
                self.canvas.create_rectangle(reg.sx - 3, reg.sy - 3, reg.dx + 3, reg.dy + 3, outline=color, width=2)
                # [텍스트 라벨 삭제됨 - OCR 간섭 방지]
        except:
            self.withdraw()

class GridIndicator(tk.Toplevel):
    """24x24 격자를 게임 화면 위에 상시 표시하는 오버레이 레이어 (Click-through)"""
    def __init__(self, parent, hwnd, state):
        super().__init__(parent)
        self.hwnd = hwnd
        self.state = state
        self.visible = False
        self.grid_size = 48.2  # float (누적오차 방지)
        
        # config.json에서 오프셋 불러오기
        config_file = os.path.join(SCRIPT_DIR, "config.json")
        self.offset_x = 0  # 기본값
        self.offset_y = 0  # 기본값
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    conf = json.load(f)
                    pa = conf.get("play_area", {})
                    self.grid_size = float(pa.get("grid_size", 48.2))
            except Exception:
                pass
        
        self.grid_alpha = 0.5  # 투명도 기본값 (0.0 ~ 1.0)
        # 캐릭터가 화면상 고정되는 기준 타일 (클릭 좌표 변환/표시 공통 사용)
        self.char_screen_grid_x = 9
        self.char_screen_grid_y = 11  # J12 타일 기준
        self.item_positions = []  # 아이템 grid 좌표 리스트 [(gx, gy), ...]
        
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-transparentcolor", "black") # 블랙 색상은 100% 투명 및 클릭-스루
        self.configure(bg="black")
        
        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        
        # 마우스 클릭 이벤트 바인딩
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        
        # 좌표 표시 라벨
        self.coord_label = tk.Label(self, text="", bg="black", fg="#00FF00", font=("Arial", 10))
        self.coord_label.place(x=10, y=10)
        
        self.withdraw() # 초기에는 숨김

    def set_alpha(self, alpha):
        """투명도 설정 (0.0 ~ 1.0)"""
        self.grid_alpha = max(0.0, min(1.0, alpha))
        self.attributes("-alpha", self.grid_alpha)

    def update_item_positions(self, item_grids):
        """아이템 grid 좌표 리스트 업데이트"""
        self.item_positions = item_grids

    def on_canvas_click(self, event):
        """마우스 클릭 시 좌표 표시"""
        if not self.visible:
            return

        # 윈도우 좌표 (클릭한 위치)
        window_x = event.x
        window_y = event.y

        # 게임 좌표 계산 (그리드 좌표로 변환)
        try:
            # ── 확정 오프셋: sx=276, sy=32 / 격자 크기 48.2 ──
            # 공식: GridX = (PixelX - 276) / 48.2, GridY = (PixelY - 32) / 48.2
            config_file = os.path.join(SCRIPT_DIR, "config.json")
            play_area_sx = 276  # 확정 오프셋
            play_area_sy = 32   # 확정 오프셋
            grid_size = 48.2    # float (누적오차 방지)
            if os.path.exists(config_file):
                try:
                    with open(config_file, 'r', encoding='utf-8') as f:
                        conf = json.load(f)
                        pa = conf.get("play_area", {})
                        play_area_sx = pa.get("sx", 276)
                        play_area_sy = pa.get("sy", 32)
                        grid_size = float(pa.get("grid_size", 48.2))
                except Exception:
                    pass

            # 격자 시작점 (play_area 기준 + offset)
            grid_start_x = play_area_sx + self.offset_x
            grid_start_y = play_area_sy + self.offset_y

            # 클릭한 화면 격자 인덱스: float 나눗셈 후 int 변환
            grid_x = int((window_x - grid_start_x) / grid_size)
            grid_y = int((window_y - grid_start_y) / grid_size)

            # GameState에서 최신 절대 좌표 취득
            data = self.state.get_all() if hasattr(self.state, "get_all") else {}
            my_world_x = int(data.get("x", getattr(self.state, "x", 0)))
            my_world_y = int(data.get("y", getattr(self.state, "y", 0)))

            # 표시용: x/y는 4자리 고정 문자열(x_str/y_str) 우선 (누락은 'x')
            my_world_x_str = str(data.get("x_str", ""))
            my_world_y_str = str(data.get("y_str", ""))
            if not my_world_x_str:
                my_world_x_str = f"{my_world_x:04d}"
            if not my_world_y_str:
                my_world_y_str = f"{my_world_y:04d}"

            # 캐릭터가 화면상에 있는 실제 동적 타일 좌표(me_grid)를 가져옴
            me_data = self.state.entities.get("me", {}) if hasattr(self.state, "entities") else {}
            me_grid = me_data.get("grid", (self.char_screen_grid_x, self.char_screen_grid_y))
            char_screen_grid_x, char_screen_grid_y = me_grid

            # 클릭한 월드 좌표 산출
            target_world_x = my_world_x + (grid_x - char_screen_grid_x)
            target_world_y = my_world_y + (grid_y - char_screen_grid_y)

            # 클릭한 타일명
            tile_col = chr(ord('A') + grid_x) if grid_x >= 0 else "?"
            tile_row = str(grid_y + 1)
            tile_name = f"{tile_col}{tile_row}"

            # 좌표 표시 (검증 가능한 상세 포맷)
            coord_text = (
                f"[내 좌표: {my_world_x_str}, {my_world_y_str}] | "
                f"클릭한 타일: {tile_name} | "
                f"계산된 월드 좌표: {target_world_x}, {target_world_y}"
            )
            self.coord_label.configure(text=coord_text)
            
            # 클릭한 위치에 녹색 점 표시
            self.canvas.delete("click_marker")
            self.canvas.create_oval(window_x - 3, window_y - 3, window_x + 3, window_y + 3,
                                  fill="#00FF00", outline="#00FF00", tags="click_marker")
        except Exception:
            pass

    def update_position(self):
        if not self.visible:
            self.withdraw()
            return

        try:
            left, top = win32gui.ClientToScreen(self.hwnd, (0, 0))
            _, _, w, h = win32gui.GetClientRect(self.hwnd)
            self.geometry(f"{w}x{h}+{left}+{top}")
            self.deiconify()
            self.lift()  # 최상단으로 강제 이동

            self.canvas.delete("all")

            # ── 확정 오프셋: sx=276, sy=32 / 격자 크기 48.2 ──
            # 공식: GridX = (PixelX - 276) / 48.2, GridY = (PixelY - 32) / 48.2
            config_file = os.path.join(SCRIPT_DIR, "config.json")
            play_area_sx = 276  # 확정 오프셋
            play_area_sy = 32   # 확정 오프셋
            grid_size = 48.2    # float (누적오차 방지)
            if os.path.exists(config_file):
                try:
                    with open(config_file, 'r', encoding='utf-8') as f:
                        conf = json.load(f)
                        pa = conf.get("play_area", {})
                        play_area_sx = pa.get("sx", 276)
                        play_area_sy = pa.get("sy", 32)
                        grid_size = float(pa.get("grid_size", 48.2))
                except Exception:
                    pass

            # 격자 시작점 (play_area 기준 + offset)
            grid_start_x = play_area_sx + self.offset_x
            grid_start_y = play_area_sy + self.offset_y
            tile_size = grid_size
            
            # 격자 수 제한 (가로 19칸 a1~S1, 세로 17칸 a1~a17)
            grid_cols = 19
            grid_rows = 17

            # 격자 라인 그리기 (float grid_size → round로 정수 변환)
            for i in range(grid_cols + 1):
                x = round(grid_start_x + i * tile_size)
                x_end = round(grid_start_y + grid_rows * tile_size)
                self.canvas.create_line(x, round(grid_start_y), x, x_end, fill="#FFFFFF", width=1)
            
            # 격자 라인 그리기 (17행 row=16 제외)
            for i in range(grid_rows + 1):
                if i == 16:  # 17행 건너뜀
                    continue
                y = round(grid_start_y + i * tile_size)
                x_end = round(grid_start_x + grid_cols * tile_size)
                self.canvas.create_line(round(grid_start_x), y, x_end, y, fill="#FFFFFF", width=1)
            
            # 테두리 칸에 4px 간격 추가 격자 그리기
            # a열 (col=0), r열 (col=17), 1행 (row=0), 17행 (row=16)
            border_cols = [0, grid_cols - 1]  # a열, r열
            border_rows = [0, grid_rows - 1]  # 1행, 17행
            fine_spacing = 4
            
            # 테두리 열에 세로로 추가 격자 (테두리 칸 내부)
            for col in border_cols:
                col_start_x = round(grid_start_x + col * tile_size)
                col_end_x = round(grid_start_x + (col + 1) * tile_size)
                for i in range(0, int(grid_rows * tile_size), fine_spacing):
                    y = round(grid_start_y) + i
                    self.canvas.create_line(col_start_x, y, col_end_x, y, fill="#FFFFFF", width=1)
            
            # 테두리 행에 가로로 추가 격자 (테두리 칸 내부)
            for row in border_rows:
                row_start_y = round(grid_start_y + row * tile_size)
                row_end_y = round(grid_start_y + (row + 1) * tile_size)
                for i in range(0, int(grid_cols * tile_size), fine_spacing):
                    x = round(grid_start_x) + i
                    self.canvas.create_line(x, row_start_y, x, row_end_y, fill="#FFFFFF", width=1)

            # 아이템 위치에 녹색 점 및 좌표 표시
            data = self.state.get_all() if hasattr(self.state, "get_all") else {}
            my_world_x = int(data.get("x", getattr(self.state, "x", 0)))
            my_world_y = int(data.get("y", getattr(self.state, "y", 0)))

            for gx, gy in self.item_positions:
                if 0 <= gx < grid_cols and 0 <= gy < grid_rows:
                    item_x = round(grid_start_x + gx * tile_size + tile_size / 2)
                    item_y = round(grid_start_y + gy * tile_size + tile_size / 2)
                    self.canvas.create_oval(item_x - 4, item_y - 4,
                                            item_x + 4, item_y + 4,
                                            fill="#00FF00", outline="#00FF00")

                    # Grid 좌표와 POS_X,Y 좌표 계산
                    col_name = chr(ord('A') + gx)
                    row_name = str(gy + 1)
                    grid_name = f"{col_name}{row_name}"
                    me_data = self.state.entities.get("me", {}) if hasattr(self.state, "entities") else {}
                    me_grid = me_data.get("grid", (self.char_screen_grid_x, self.char_screen_grid_y))
                    char_grid_x, char_grid_y = me_grid
                    
                    pos_x = my_world_x + (gx - char_grid_x)
                    pos_y = my_world_y + (gy - char_grid_y)
                    coord_text = f"{grid_name}\n({pos_x},{pos_y})"

                    # 좌표 텍스트 표시 (녹색 점 옆)
                    self.canvas.create_text(item_x + 8, item_y, text=coord_text,
                                            fill="#00FF00", font=("Arial", 7), anchor="w")

            # 격자 이름 표시
            for row in range(grid_rows):
                for col in range(grid_cols):
                    # 정수 인덱스
                    grid_x = col
                    grid_y = row
                    
                    # 엑셀 방식 변환 (A1, B2...)
                    col_name = chr(ord('A') + grid_x)
                    row_name = str(grid_y + 1)
                    grid_name = f"{col_name}{row_name}"
                    
                    # 격자 칸 중앙에 텍스트 표시
                    x = grid_start_x + grid_x * tile_size + tile_size // 2
                    y = grid_start_y + grid_y * tile_size + tile_size // 2
                    self.canvas.create_text(x, y, text=grid_name, fill="#FFFFFF", font=("Arial", 8))
        except:
            self.withdraw()

# hw는 svc_kernel에서 임포트됨
# AIController는 LogicSvc로 대체됨
# GameState는 svc_kernel에서 임포트됨

# DXCam 제거됨 - OBS 가상카메라 모드 사용

# --------------------------------------------------------------------------------
# 3. 게임 액션 함수 (원본 스크립트 이식)
# execute_* 함수들은 LogicSvc로 대체됨

# --------------------------------------------------------------------------------
# 5. 데이터 저장 스레드 (Logger)
# --------------------------------------------------------------------------------
class LoggerThread(threading.Thread):
    def __init__(self, state: GameState):
        super().__init__(daemon=True)
        self.state = state
        self.header = ["timestamp", "hp", "mp", "exp", "money", "x", "y"]
        self.header_str = [
            "timestamp",
            "hp", "mp", "exp", "money", "x", "y",
            "hp_str", "mp_str", "exp_str", "money_str", "x_str", "y_str",
        ]

    def run(self):
        print("[Log] Logger Thread Started...")
        # 파일이 없으면 헤더 작성
        if not os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(self.header)
        if not os.path.exists(LOG_FILE_STR):
            with open(LOG_FILE_STR, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(self.header_str)

        while self.state.running:
            data = self.state.get_all()
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            row = [ts, data["hp"], data["mp"], data["exp"], data["money"], data["x"], data["y"]]
            with open(LOG_FILE, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(row)

            row_str = [
                ts,
                data.get("hp"), data.get("mp"), data.get("exp"), data.get("money"), data.get("x"), data.get("y"),
                data.get("hp_str", ""), data.get("mp_str", ""), data.get("exp_str", ""), data.get("money_str", ""),
                data.get("x_str", ""), data.get("y_str", ""),
            ]
            with open(LOG_FILE_STR, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(row_str)

            time.sleep(1)

# --------------------------------------------------------------------------------
# 6. ZeroMQ 네트워크 스레드 (PUB/SUB)
# --------------------------------------------------------------------------------
class NetworkThread(threading.Thread):
    def __init__(self, state: GameState, is_server=True):
        super().__init__(daemon=True)
        self.state = state
        self.is_server = is_server
        self.context = zmq.Context()

    def run(self):
        try:
            if self.is_server:
                socket = self.context.socket(zmq.PUB)
                socket.bind(f"tcp://*:{ZMQ_PORT}")
                self.state.is_connected = True
                print("[Net] 서버 모드 연결 성공")
            else:
                socket = self.context.socket(zmq.SUB)
                socket.connect(f"tcp://{SERVER_IP}:{ZMQ_PORT}")
                socket.setsockopt_string(zmq.SUBSCRIBE, "")
                self.state.is_connected = True
                print("[Net] 클라이언트 모드 연결 성공")

            while self.state.running:
                if self.is_server:
                    data = self.state.get_all()
                    socket.send_string(json.dumps(data) + (" " * random.randint(1,16)))
                    time.sleep(0.1)
                else:
                    try:
                        msg = socket.recv_string(flags=zmq.NOBLOCK)
                        remote_data = json.loads(msg)
                        self.state.update_other("LAPTOP", remote_data)
                        # 수신된 데이터 병합 (last_move_dir, nav_follow_enabled)
                        if "last_move_dir" in remote_data:
                            self.state.last_move_dir = remote_data["last_move_dir"]
                        if "nav_follow_enabled" in remote_data:
                            self.state.nav_follow_enabled = remote_data["nav_follow_enabled"]
                    except zmq.Again:
                        time.sleep(0.01)
        except Exception as e: 
            print(f"[Net] Error: {e}")
            self.state.is_connected = False

# --------------------------------------------------------------------------------
# 7. UI 클래스 (초밀집 하이엔드 디자인)
# --------------------------------------------------------------------------------
class AppView:
    def __init__(self, state: GameState, hwnd=None):
        self.state = state
        self.hwnd = hwnd
        self.root = ctk.CTk()
        # GameState의 gui_update_queue 사용 (중복 큐 제거)
        self.waypoint_index = 0  # 순차적 이동용 인덱스
        self.ui_scale = 1.0
        self.ui_scale_step = 0.05
        self.ui_scale_min = 0.5
        self.ui_scale_max = 2.0
        self.root.title("SYSTEM PARALLEL SOTA v2.0")
        
        # 폰트 먼저 선언 (Ultra-Compact 디자인: 9pt)
        self.header_font = ctk.CTkFont(family="Orbitron", size=12, weight="bold")
        self.main_font = ctk.CTkFont(family="Inter", size=9)
        
        # 상태 로드 (geometry 설정 등)
        self.restore_gui_state()
        
        # 마법 데이터 로드 및 초기화
        self.spell_db_path = os.path.join(SCRIPT_DIR, "spells_config.json")
        self.picked_spell = None
        self.dnd_spell = None
        self.dnd_label = None
        self.load_spells_db()
        
        self.create_widgets()
        self.bind_global_zoom_controls()
        self.last_values = {}  # Dirty Checking용 캐시
        self.frame_count = 0  # 성능 측정용 프레임 카운터
        
        # Tab별 렌더링 스위치 (리소스 관리)
        self.tab_enabled = {
            "dash": True,   # 숫자 라벨 업데이트
            "nav": True     # 네비게이션 상태
        }
        self.update_fast_labels()
        self.update_slow_ui()
        
        # 아두이노 자동 연결 시도
        self.auto_connect_hardware()

    def restore_gui_state(self):
        # 윈도우 크기: 가로 480px, 세로 900px 이상 (마우스 드래그로 조절 가능)
        self.root.geometry("480x900")
        self.root.resizable(True, True)  # 가로/세로 모두 조절 가능
        gui_state = load_gui_state()
        self.ui_scale = gui_state["ui_scale"]
        self.calibration_geometry = gui_state.get("calibration_geometry", "420x270+180+180")
        ctk.set_widget_scaling(self.ui_scale)
        ctk.set_window_scaling(self.ui_scale)
        try:
            self.root.geometry(gui_state["geometry"])
        except Exception:
            self.root.geometry("480x900")

    def create_widgets(self):
        # Header & Status
        h = ctk.CTkFrame(self.root, height=30, corner_radius=0, fg_color="#1A1C23")
        h.pack(side="top", fill="x")
        ctk.CTkLabel(h, text="SYSTEM PARALLEL SOTA v2.0", font=self.header_font, text_color="#00D4FF").pack(pady=1)

        s = ctk.CTkFrame(self.root, height=16, corner_radius=0, fg_color="#14161B")
        s.pack(side="top", fill="x")
        self.lbl_net = ctk.CTkLabel(s, text="[Net] -", font=("Inter", 8), text_color="#00D4FF")
        self.lbl_net.pack(side="left", padx=5)
        self.lbl_hw_status = ctk.CTkLabel(s, text="[Dev] -", font=("Inter", 8), text_color="#A855F7")
        self.lbl_hw_status.pack(side="right", padx=5)
        self.lbl_perf = ctk.CTkLabel(s, text="[Perf] -ms - FPS", font=("Inter", 8), text_color="#10B981")
        self.lbl_perf.pack(side="right", padx=5)

        # Tabs
        self.tabview = ctk.CTkTabview(self.root)
        self.tabview.pack(expand=True, fill="both", padx=1, pady=1)
        self.t_dash = self.tabview.add("DASH")
        self.c_dash = ctk.CTkScrollableFrame(self.t_dash, fg_color="transparent", label_text="")
        self.c_dash.pack(expand=True, fill="both", padx=1, pady=1)
        
        self.t_nav = self.tabview.add("NAV")
        self.c_nav = ctk.CTkScrollableFrame(self.t_nav, fg_color="transparent", label_text="")
        self.c_nav.pack(expand=True, fill="both", padx=1, pady=1)

        # Dashboard Tab - Ultra-Compact
        top_actions = ctk.CTkFrame(self.c_dash, fg_color="transparent")
        top_actions.pack(fill="x", padx=1, pady=1)
        
        # 첫 번째 줄: RUN, OCR, GUIDES, GRID, PORT
        row1 = ctk.CTkFrame(top_actions, fg_color="transparent")
        row1.pack(fill="x", pady=1)
        self.btn_macro = ctk.CTkButton(row1, text="RUN", height=22, width=60, font=("Inter", 9, "bold"), fg_color="#10B981", command=self.toggle_service)
        self.btn_macro.pack(side="left", padx=1)
        self.btn_ocr_toggle = ctk.CTkButton(row1, text="OCR", height=22, width=50, font=("Inter", 9), fg_color="#10B981", command=self.toggle_ocr_engine)
        self.btn_ocr_toggle.pack(side="left", padx=1)
        self.btn_roi_toggle = ctk.CTkButton(row1, text="GUIDE", height=22, width=50, font=("Inter", 9), command=self.toggle_roi_indicator)
        self.btn_roi_toggle.pack(side="left", padx=1)
        self.btn_grid_toggle = ctk.CTkButton(row1, text="GRID", height=22, width=50, font=("Inter", 9), command=self.toggle_grid_overlay)
        self.btn_grid_toggle.pack(side="left", padx=1)
        ctk.CTkLabel(row1, text="P", font=("Inter", 9), width=15).pack(side="left", padx=1)
        self.cb_port = ctk.CTkComboBox(row1, values=[p.device for p in serial.tools.list_ports.comports()] or ["COM?"], height=22, width=60, font=("Inter", 9))
        self.cb_port.pack(side="left", padx=1)
        self.btn_hw_connect = ctk.CTkButton(row1, text="HW", height=22, width=40, font=("Inter", 9), command=self.toggle_hardware)
        self.btn_hw_connect.pack(side="left", padx=1)
        
        # 두 번째 줄: CALIB, SCALE, ROLE, SENTINEL
        row2 = ctk.CTkFrame(top_actions, fg_color="transparent")
        row2.pack(fill="x", pady=1)
        self.btn_open_calib = ctk.CTkButton(row2, text="CALIB", height=22, width=50, font=("Inter", 9), command=self.open_calibration_popup)
        self.btn_open_calib.pack(side="left", padx=1)
        self.btn_scale_2x = ctk.CTkButton(row2, text="2x", height=22, width=30, font=("Inter", 9), command=lambda: self.set_game_scale(2.0))
        self.btn_scale_2x.pack(side="left", padx=1)
        self.btn_scale_1x = ctk.CTkButton(row2, text="1x", height=22, width=30, font=("Inter", 9), command=lambda: self.set_game_scale(1.0))
        self.btn_scale_1x.pack(side="left", padx=1)
        self.lbl_game_res = ctk.CTkLabel(row2, text="G:-", font=("Inter", 8), text_color="#00D4FF", width=50)
        self.lbl_game_res.pack(side="left", padx=1)
        self.lbl_gui_res = ctk.CTkLabel(row2, text="UI:-", font=("Inter", 8), text_color="#A855F7", width=50)
        self.lbl_gui_res.pack(side="left", padx=1)
        ctk.CTkLabel(row2, text="R", font=("Inter", 9), width=15).pack(side="left", padx=1)
        self.cb_role = ctk.CTkComboBox(row2, values=["격수", "도사", "술사"], height=22, width=50, font=("Inter", 9), command=self.set_role)
        self.cb_role.pack(side="left", padx=1)
        self.cb_role.set(self.state.role)
        self.btn_sentinel = ctk.CTkButton(row2, text="SNT", height=22, width=40, font=("Inter", 9), command=self.toggle_sentinel)
        self.btn_sentinel.pack(side="left", padx=1)
        
        # 세 번째 줄: OFFSET, ALPHA, GRID
        row3 = ctk.CTkFrame(top_actions, fg_color="transparent")
        row3.pack(fill="x", pady=1)
        self.lbl_grid_offset = ctk.CTkLabel(row3, text="O:(0,0)", font=("Inter", 8), width=60)
        self.lbl_grid_offset.pack(side="left", padx=1)
        btn_frame = ctk.CTkFrame(row3, fg_color="#2D3748", corner_radius=2)
        btn_frame.pack(side="left", padx=1)
        ctk.CTkButton(btn_frame, text="↑", height=18, width=18, font=("Inter", 7), command=lambda: self.move_grid(0, -1)).grid(row=0, column=1, padx=0)
        ctk.CTkButton(btn_frame, text="←", height=18, width=18, font=("Inter", 7), command=lambda: self.move_grid(-1, 0)).grid(row=1, column=0, padx=0)
        ctk.CTkButton(btn_frame, text="↓", height=18, width=18, font=("Inter", 7), command=lambda: self.move_grid(0, 1)).grid(row=1, column=1, padx=0)
        ctk.CTkButton(btn_frame, text="→", height=18, width=18, font=("Inter", 7), command=lambda: self.move_grid(1, 0)).grid(row=1, column=2, padx=0)
        ctk.CTkLabel(row3, text="A", font=("Inter", 8), width=15).pack(side="left", padx=1)
        self.slider_alpha = ctk.CTkSlider(row3, from_=0.1, to=1.0, number_of_steps=9, width=60, command=self.on_alpha_change)
        self.slider_alpha.set(0.5)
        self.slider_alpha.pack(side="left", padx=1)
        self.lbl_char_grid = ctk.CTkLabel(row3, text="G:-", font=("Inter", 8), text_color="#00D4FF", width=80)
        self.lbl_char_grid.pack(side="left", padx=1)
        
        # OCR Threshold 슬라이더 제거 (개별 임계값 로직으로 대체)
        
        # 상태 정보 - 가로 2열 배치

        # 상태 정보 - 가로 2열 배치
        st = ctk.CTkFrame(self.c_dash, fg_color="#1E2129", corner_radius=4, border_width=1, border_color="#343A46")
        st.pack(fill="x", padx=1, pady=1)
        g = ctk.CTkFrame(st, fg_color="transparent")
        g.pack(fill="x", padx=2, pady=2)
        
        # 가로 2열 배치
        ctk.CTkLabel(g, text="HP", font=("Inter", 9, "bold")).grid(row=0, column=0, sticky="w", padx=1)
        self.lbl_hp = ctk.CTkLabel(g, text="-", font=("Inter", 9), width=100, anchor="w")
        self.lbl_hp.grid(row=0, column=1, sticky="w", padx=1)
        ctk.CTkLabel(g, text="MP", font=("Inter", 9, "bold")).grid(row=0, column=2, sticky="w", padx=1)
        self.lbl_mp = ctk.CTkLabel(g, text="-", font=("Inter", 9), width=100, anchor="w")
        self.lbl_mp.grid(row=0, column=3, sticky="w", padx=1)
        
        ctk.CTkLabel(g, text="EXP", font=("Inter", 9, "bold")).grid(row=1, column=0, sticky="w", padx=1)
        self.lbl_exp = ctk.CTkLabel(g, text="-", font=("Inter", 9), width=100, anchor="w")
        self.lbl_exp.grid(row=1, column=1, sticky="w", padx=1)
        ctk.CTkLabel(g, text="M", font=("Inter", 9, "bold")).grid(row=1, column=2, sticky="w", padx=1)
        self.lbl_money = ctk.CTkLabel(g, text="-", font=("Inter", 9), width=100, anchor="w")
        self.lbl_money.grid(row=1, column=3, sticky="w", padx=1)
        
        ctk.CTkLabel(g, text="POS", font=("Inter", 9, "bold")).grid(row=2, column=0, sticky="w", padx=1)
        self.lbl_xy = ctk.CTkLabel(g, text="-", font=("Inter", 9), width=220, anchor="w")
        self.lbl_xy.grid(row=2, column=1, columnspan=3, sticky="w", padx=1)

        # Navigation & Control Tab
        self.build_navigation_control()

        # Footer / Toast Area
        self.toast_lbl = ctk.CTkLabel(self.root, text="", font=("Inter", 10, "bold"), text_color="#10B981")
        self.toast_lbl.pack(side="bottom", pady=5)

    def open_calibration_popup(self):
        print("[DEBUG] CALIB 버튼 클릭됨")
        pop = getattr(self, "calibration_popup", None)
        if pop is not None:
            try:
                if pop.winfo_exists():
                    print("[DEBUG] 기존 팝업을 찾아서 앞으로 가져옴")
                    pop.lift()
                    pop.focus_force()
                    return
            except tk.TclError:
                pass

        print("[DEBUG] 새로운 캘리브레이션 팝업 생성")
        try:
            self.calibration_popup = ctk.CTkToplevel(self.root)
            self.calibration_popup.title("Calibration")
            self.calibration_popup.geometry(self.calibration_geometry or "920x420+180+180")
            self.calibration_popup.attributes("-topmost", True)
            self.calibration_popup.transient(self.root)
            self.calibration_popup.minsize(860, 360)
            print("[DEBUG] 팝업 기본 설정 완료")
        except Exception as e:
            print(f"[DEBUG] 팝업 생성 실패: {e}")
            return

        try:
            self.preview_zoom_var = ctk.StringVar(value="x3")
            self.preview_crop_pil = None
            self.preview_tkimg = None
            print("[DEBUG] 변수 초기화 완료")
        except Exception as e:
            print(f"[DEBUG] 변수 초기화 실패: {e}")
            return

        try:
            body = ctk.CTkFrame(self.calibration_popup, fg_color="transparent")
            body.pack(fill="both", expand=True, padx=6, pady=6)
            body.grid_columnconfigure(0, weight=0)
            body.grid_columnconfigure(1, weight=1)
            body.grid_rowconfigure(0, weight=1)
            print("[DEBUG] body 프레임 생성 완료")
        except Exception as e:
            print(f"[DEBUG] body 프레임 생성 실패: {e}")
            return

        left_panel = ctk.CTkFrame(body, width=280, corner_radius=8, fg_color="#1E2129")
        left_panel.grid(row=0, column=0, sticky="nsw", padx=(0, 6))
        left_panel.grid_propagate(False)

        ctk.CTkLabel(left_panel, text="CALIBRATION", font=("Inter", 11, "bold"), text_color="#00D4FF").pack(pady=(6, 4))
        target_grid = ctk.CTkFrame(left_panel, fg_color="transparent")
        target_grid.pack(pady=2)
        targets = ["hp", "mp", "exp", "money", "x", "y", "hp_trig", "mp_trig", "stat_info", "target_info", "user_info", "map_info", "play_area"]
        for i, t in enumerate(targets):
            r, c = divmod(i, 3)
            ctk.CTkButton(target_grid, text=t.upper(), width=64, height=22, font=("Inter", 9),
                          command=lambda n=t: self.open_visual_selector(n)).grid(row=r, column=c, padx=2, pady=2)

        ctk.CTkLabel(left_panel, text="Target", font=("Inter", 9)).pack(pady=(4, 1))
        self.tune_target = ctk.CTkOptionMenu(left_panel, values=targets, height=22, width=130, font=("Inter", 9))
        self.tune_target.pack(pady=1)

        dp = ctk.CTkFrame(left_panel, fg_color="transparent")
        dp.pack(pady=3)
        # 시작 좌표 조절 (sx, sy)
        ctk.CTkButton(dp, text="▲", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("sy", -1)).grid(row=0, column=1)
        ctk.CTkButton(dp, text="◀", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("sx", -1)).grid(row=1, column=0)
        ctk.CTkButton(dp, text="▶", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("sx", 1)).grid(row=1, column=2)
        ctk.CTkButton(dp, text="▼", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("sy", 1)).grid(row=2, column=1)

        # 끝 좌표 조절 (dx, dy)
        ctk.CTkLabel(left_panel, text="End Position", font=("Inter", 9)).pack(pady=(4, 1))
        dp2 = ctk.CTkFrame(left_panel, fg_color="transparent")
        dp2.pack(pady=3)
        ctk.CTkButton(dp2, text="▲", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("dy", -1)).grid(row=0, column=1)
        ctk.CTkButton(dp2, text="◀", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("dx", -1)).grid(row=1, column=0)
        ctk.CTkButton(dp2, text="▶", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("dx", 1)).grid(row=1, column=2)
        ctk.CTkButton(dp2, text="▼", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("dy", 1)).grid(row=2, column=1)

        ctk.CTkLabel(left_panel, text="Preview Zoom", font=("Inter", 9)).pack(pady=(6, 1))
        self.zoom_menu = ctk.CTkOptionMenu(left_panel, values=["x3", "x4", "x5"], variable=self.preview_zoom_var, width=90, height=22, font=("Inter", 9),
                                           command=lambda _: self.update_calibration_preview())
        self.zoom_menu.pack(pady=1)

        # Threshold 슬라이더 (이진화 임계값 튜닝)
        thresh_frame = ctk.CTkFrame(left_panel, fg_color="transparent")
        thresh_frame.pack(pady=(6, 1), fill="x", padx=6)
        ctk.CTkLabel(thresh_frame, text="Threshold", font=("Inter", 9)).pack(side="left", padx=2)
        self.threshold_value_var = ctk.IntVar(value=128)
        self.threshold_label = ctk.CTkLabel(thresh_frame, text="128", font=("Inter", 9, "bold"), text_color="#00D4FF", width=30)
        self.threshold_label.pack(side="right", padx=2)
        self.threshold_slider = ctk.CTkSlider(left_panel, from_=50, to=200, number_of_steps=150,
                                               variable=self.threshold_value_var, height=14,
                                               command=self._on_threshold_slider_change)
        self.threshold_slider.pack(pady=(0, 1), padx=6, fill="x")


        # 샘플링 버튼 추가
        sample_row = ctk.CTkFrame(left_panel, fg_color="transparent")
        sample_row.pack(pady=(3, 6))
        ctk.CTkButton(sample_row, text="샘플링", width=190, height=24, font=("Inter", 9, "bold"),
                     fg_color="#A855F7", hover_color="#7E22CE", command=self.start_sampling_tool).pack()

        # 카테고리 선택 드롭다운
        category_row = ctk.CTkFrame(left_panel, fg_color="transparent")
        category_row.pack(pady=(2, 6))
        ctk.CTkLabel(category_row, text="카테고리:", font=("Inter", 8)).pack(side="left", padx=2)
        self.sample_category_var = ctk.StringVar(value="digits")
        ctk.CTkOptionMenu(category_row, values=["digits", "monsters", "status", "users", "items", "marker"],
                         variable=self.sample_category_var, width=120, height=22, font=("Inter", 8)).pack(side="left", padx=2)

        # 샘플링 1px 편집 UI
        self.sample_trim_frame = ctk.CTkFrame(left_panel, fg_color="transparent")
        self.sample_trim_frame.pack(pady=(3, 6))

        # 줌 선택
        zoom_row = ctk.CTkFrame(self.sample_trim_frame, fg_color="transparent")
        zoom_row.pack(pady=(2, 2))
        ctk.CTkLabel(zoom_row, text="줌:", font=("Inter", 8)).pack(side="left", padx=2)
        self.sample_zoom_var = ctk.StringVar(value="x2")
        ctk.CTkOptionMenu(zoom_row, values=["x1", "x2", "x4"], variable=self.sample_zoom_var, width=60, height=20, font=("Inter", 8),
                         command=lambda _: self.set_sample_zoom(int(self.sample_zoom_var.get().replace("x", "")))).pack(side="left", padx=2)

        # 1px 트림 버튼
        trim_grid = ctk.CTkFrame(self.sample_trim_frame, fg_color="transparent")
        trim_grid.pack(pady=2)
        ctk.CTkButton(trim_grid, text="L+", width=36, height=20, font=("Inter", 8), command=lambda: self.trim_sample("left", 1)).grid(row=0, column=0, padx=1, pady=1)
        ctk.CTkButton(trim_grid, text="L-", width=36, height=20, font=("Inter", 8), command=lambda: self.trim_sample("left", -1)).grid(row=0, column=1, padx=1, pady=1)
        ctk.CTkButton(trim_grid, text="R+", width=36, height=20, font=("Inter", 8), command=lambda: self.trim_sample("right", 1)).grid(row=0, column=2, padx=1, pady=1)
        ctk.CTkButton(trim_grid, text="R-", width=36, height=20, font=("Inter", 8), command=lambda: self.trim_sample("right", -1)).grid(row=0, column=3, padx=1, pady=1)
        ctk.CTkButton(trim_grid, text="T+", width=36, height=20, font=("Inter", 8), command=lambda: self.trim_sample("top", 1)).grid(row=1, column=0, padx=1, pady=1)
        ctk.CTkButton(trim_grid, text="T-", width=36, height=20, font=("Inter", 8), command=lambda: self.trim_sample("top", -1)).grid(row=1, column=1, padx=1, pady=1)
        ctk.CTkButton(trim_grid, text="B+", width=36, height=20, font=("Inter", 8), command=lambda: self.trim_sample("bottom", 1)).grid(row=1, column=2, padx=1, pady=1)
        ctk.CTkButton(trim_grid, text="B-", width=36, height=20, font=("Inter", 8), command=lambda: self.trim_sample("bottom", -1)).grid(row=1, column=3, padx=1, pady=1)

        # 리셋 버튼
        ctk.CTkButton(self.sample_trim_frame, text="리셋", width=148, height=20, font=("Inter", 8), command=self.reset_sample_crop).pack(pady=(2, 0))

        actions_row = ctk.CTkFrame(left_panel, fg_color="transparent")
        actions_row.pack(pady=(3, 6))
        ctk.CTkButton(actions_row, text="CAPTURE", width=90, height=24, font=("Inter", 9, "bold"), command=self.update_calibration_preview).pack(side="left", padx=2)
        ctk.CTkButton(actions_row, text="SAVE", width=90, height=24, font=("Inter", 9, "bold"), command=self.save_with_mode_check).pack(side="left", padx=2)

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
        
        print("[DEBUG] 팝업 설정 완료, 프리뷰 업데이트 시작")
        try:
            self.update_calibration_preview()
            print("[DEBUG] 프리뷰 업데이트 완료")
        except Exception as e:
            print(f"[DEBUG] 프리뷰 업데이트 실패: {e}")
        
        print("[DEBUG] 팝업 표시 시도")
        try:
            self.calibration_popup.lift()
            self.calibration_popup.focus_force()
            self.calibration_popup.update_idletasks()
            print("[DEBUG] 팝업 표시 성공")
        except Exception as e:
            print(f"[DEBUG] 팝업 표시 실패: {e}")
        
        self.show_toast("CALIBRATION POPUP OPEN")

    def on_calibration_popup_configure(self, event):
        if event.widget == self.calibration_popup:
            self.calibration_geometry = self.calibration_popup.geometry()

    def get_trimmed_region_for_preview(self):
        global reader_thread
        if reader_thread is None:
            return None, ""
        target = self.tune_target.get()
        if not target or target not in reader_thread.regions:
            return None, target
        base = reader_thread.regions[target]
        return Region(base.sx, base.sy, base.dx, base.dy), target

    # on_thr_change 제거 (슬라이더 미사용)

    def update_move_hold(self, value):
        """move_hold 슬라이더 콜백"""
        from bis_core import TIMING_CONFIG
        ms_val = float(value)
        TIMING_CONFIG["move_hold"] = ms_val / 1000.0  # ms -> s
        self.move_hold_label.configure(text=f"{int(ms_val)}ms")
        self.save_timing_config()
    
    def update_key_gap(self, value):
        """key_gap 슬라이더 콜백"""
        from bis_core import TIMING_CONFIG
        ms_val = float(value)
        TIMING_CONFIG["key_gap"] = ms_val / 1000.0  # ms -> s
        self.key_gap_label.configure(text=f"{int(ms_val)}ms")
        self.save_timing_config()
    
    def save_timing_config(self):
        """TIMING_CONFIG를 config.json에 저장"""
        try:
            from bis_core import TIMING_CONFIG
            config_file = os.path.join(os.path.dirname(__file__), "config.json")
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            config["timing"] = TIMING_CONFIG
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"[Config] TIMING_CONFIG 저장 실패: {e}")

    def update_calibration_preview(self):
        print("[DEBUG] 캘리브레이션 프리뷰 업데이트 시작")
        if not hasattr(self, "calibration_popup") or not self.calibration_popup.winfo_exists():
            print("[DEBUG] 캘리브레이션 팝업이 존재하지 않음")
            return
        hwnd = find_game_window(WIN_KEY)
        if not hwnd:
            print("[DEBUG] 게임 윈도우를 찾을 수 없음")
            self.preview_meta_lbl.configure(text="Game window not found")
            self.preview_canvas.delete("all")
            return
        print(f"[DEBUG] 게임 윈도우 찾음: {hwnd}")
        reg, target = self.get_trimmed_region_for_preview()
        if reg is None:
            print("[DEBUG] 잘못된 크롭 범위")
            self.preview_meta_lbl.configure(text="Invalid crop range")
            self.preview_canvas.delete("all")
            return

        full_img = grab_window_bg(hwnd)
        if full_img is None:
            print("[DEBUG] 화면 캡처 실패")
            self.preview_meta_lbl.configure(text="Capture failed")
            self.preview_canvas.delete("all")
            return

        print("[DEBUG] 화면 캡처 성공, 크롭 진행")
        try:
            crop = full_img.crop(reg.to_bbox((0, 0)))
        except Exception as e:
            print(f"[DEBUG] 크롭 실패: {e}")
            self.preview_meta_lbl.configure(text="Crop failed")
            self.preview_canvas.delete("all")
            return

        zoom = int(self.preview_zoom_var.get().replace("x", ""))
        preview_img = crop.resize((max(1, crop.width * zoom), max(1, crop.height * zoom)), _PIL_NEAREST)
        self.preview_crop_pil = crop.copy()
        self.preview_tkimg = ImageTk.PhotoImage(preview_img)

        cw = max(1, self.preview_canvas.winfo_width())
        ch = max(1, self.preview_canvas.winfo_height())
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(cw // 2, ch // 2, image=self.preview_tkimg, anchor="center")
        self.preview_meta_lbl.configure(text=f"{target.upper()} | {crop.width}x{crop.height} | zoom x{zoom}")

    def show_input_dialog(self, title, message, default_value=""):
        """CustomTkinter 스타일의 입력 다이얼로그"""
        dialog = ctk.CTkToplevel(self.root)
        dialog.title(title)
        dialog.geometry("400x200")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.attributes("-topmost", True)

        result = {"value": None}

        frame = ctk.CTkFrame(dialog, fg_color="transparent")
        frame.pack(expand=True, fill="both", padx=20, pady=20)

        label = ctk.CTkLabel(frame, text=message, font=("Inter", 12))
        label.pack(pady=(0, 10))

        entry = ctk.CTkEntry(frame, font=("Inter", 11))
        entry.pack(fill="x", pady=(0, 15))
        entry.insert(0, default_value)
        entry.select_range(0, "end")
        entry.focus()

        def on_ok():
            result["value"] = entry.get()
            dialog.destroy()

        def on_cancel():
            dialog.destroy()

        button_frame = ctk.CTkFrame(frame, fg_color="transparent")
        button_frame.pack(fill="x")

        ok_btn = ctk.CTkButton(button_frame, text="확인", width=80, command=on_ok)
        ok_btn.pack(side="right", padx=5)

        cancel_btn = ctk.CTkButton(button_frame, text="취소", width=80, command=on_cancel)
        cancel_btn.pack(side="right", padx=5)

        entry.bind("<Return>", lambda e: on_ok())
        entry.bind("<Escape>", lambda e: on_cancel())

        dialog.wait_window()
        return result["value"]

    def save_calibration_preview(self):
        if self.preview_crop_pil is None:
            messagebox.showwarning("Save Preview", "먼저 CAPTURE로 미리보기를 생성하세요.")
            return
        if not messagebox.askyesno("Save Preview", "현재 미리보기를 저장할까요?"):
            return

        os.makedirs(TEMPLATE_DIGIT_DIR, exist_ok=True)
        default_name = f"{self.tune_target.get()}_{int(time.time())}"
        name = self.show_input_dialog("파일명", "저장할 파일명을 입력하세요 (확장자 제외):", default_name)
        if not name:
            return

        name_str = str(name)
        safe_name = "".join(
            ch for ch in name_str if ch.isalnum() or ch in ("_", "-", ".")
        ).strip(". ")
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

    def save_with_mode_check(self):
        """샘플링 모드 확인 후 저장"""
        if hasattr(self, "sample_pil") and self.sample_pil is not None:
            self.save_sample()
        else:
            self.save_calibration_preview()

    def start_sampling_tool(self):
        """범용 템플릿 샘플링 모드 시작"""
        print("[DEBUG] 샘플링 버튼 클릭됨")
        hwnd = find_game_window(WIN_KEY)
        if not hwnd:
            print("[DEBUG] 게임창을 찾을 수 없음")
            self.show_toast("[Err] WINDOW NOT FOUND", "#FF5555")
            return

        print(f"[DEBUG] 게임창 찾음: hwnd={hwnd}")
        self.sample_region = None

        def save_sample(sx, sy, dx, dy, captured_img=None):
            print(f"[DEBUG] 샘플링 영역 저장: ({sx}, {sy}, {dx}, {dy})")
            x1, x2 = sorted((sx, dx))
            y1, y2 = sorted((sy, dy))
            self.sample_region = [x1, y1, x2, y2]
            self.sample_original = [x1, y1, x2, y2]  # 원본 백업
            self.sample_trim = {"left": 0, "right": 0, "top": 0, "bottom": 0}
            self.sample_captured_img = captured_img  # 즉시 캡처된 이미지 저장
            self.show_sample_preview()

        print("[DEBUG] OverlaySelector 호출 시작")
        OverlaySelector(self.root, hwnd, save_sample)
        print("[DEBUG] OverlaySelector 호출 완료")

    def image_to_binary_string(self, img):
        """이미지를 0/1 비트맵 문자열로 변환 (ft.ahk FindText 방식)"""
        # 그레이스케일 변환
        gray = img.convert('L')
        # 이진화 (임계값 128)
        binary = gray.point(lambda x: 0 if x < 128 else 1, '1')
        # 문자열로 변환
        width, height = binary.size
        binary_str = ""
        for y in range(height):
            for x in range(width):
                binary_str += str(binary.getpixel((x, y)))
            binary_str += "\n"
        return binary_str

    def show_sample_preview(self):
        """샘플링된 영역을 미리보기창에 표시 (1px 트림 적용)"""
        if not hasattr(self, "sample_region") or self.sample_region is None:
            return

        # 드래그 시 즉시 캡처한 이미지 우선 사용 (좌표 정합 보장)
        full_img = getattr(self, 'sample_captured_img', None)
        if full_img is None:
            hwnd = find_game_window(WIN_KEY)
            if not hwnd:
                return
            full_img = grab_window_bg(hwnd)
        if full_img is None:
            return

        # 트림 적용된 좌표 계산
        x1, y1, x2, y2 = self.sample_region
        trim = getattr(self, "sample_trim", {"left": 0, "right": 0, "top": 0, "bottom": 0})
        sx = x1 + trim["left"]
        sy = y1 + trim["top"]
        dx = x2 - trim["right"]
        dy = y2 - trim["bottom"]

        if dx <= sx or dy <= sy:
            self.preview_meta_lbl.configure(text="Invalid crop range")
            self.preview_canvas.delete("all")
            return

        try:
            crop = full_img.crop((sx, sy, dx, dy))
            # grab_window_bg가 이미 BGRX→RGB 변환을 수행하므로 추가 변환 불필요

            # 줌 적용 (기본 2x, 조정 가능)
            zoom = getattr(self, "sample_zoom", 2)
            preview_img = crop.resize((max(1, crop.width * zoom), max(1, crop.height * zoom)), _PIL_NEAREST)
            self.sample_pil = crop.copy()

            # 비트맵 문자열 생성 (ft.ahk FindText 방식)
            self.sample_binary_str = self.image_to_binary_string(crop)

            # 미리보기창에 표시
            self.preview_tkimg = ImageTk.PhotoImage(preview_img)
            cw = max(1, self.preview_canvas.winfo_width())
            ch = max(1, self.preview_canvas.winfo_height())
            self.preview_canvas.delete("all")
            self.preview_canvas.create_image(cw // 2, ch // 2, image=self.preview_tkimg, anchor="center")
            trim_info = f"L{trim['left']} R{trim['right']} T{trim['top']} B{trim['bottom']}"
            category = self.sample_category_var.get() if hasattr(self, "sample_category_var") else "digits"
            self.preview_meta_lbl.configure(text=f"SAMPLE [{category.upper()}] | {crop.width}x{crop.height} | x{zoom} | {trim_info}", font=("Inter", 11, "bold"))
            self.show_toast("샘플링 완료 - 1px 편집 후 SAVE 버튼 클릭")
        except Exception as e:
            self.show_toast(f"샘플링 실패: {e}", "#FF5555")

    def trim_sample(self, edge, delta=1):
        """샘플링 영역 1px 조정 (edge: left/right/top/bottom)"""
        if not hasattr(self, "sample_region") or self.sample_region is None:
            return
        if not hasattr(self, "sample_trim"):
            self.sample_trim = {"left": 0, "right": 0, "top": 0, "bottom": 0}

        x1, y1, x2, y2 = self.sample_region
        orig = getattr(self, "sample_original", [x1, y1, x2, y2])
        ow, oh = orig[2] - orig[0], orig[3] - orig[1]

        # 새 트림값 계산 (원본 범위 초과 방지)
        new_trim = dict(self.sample_trim)
        if edge == "left":
            new_trim["left"] = max(0, min(new_trim["left"] + delta, ow - new_trim["right"] - 1))
        elif edge == "right":
            new_trim["right"] = max(0, min(new_trim["right"] + delta, ow - new_trim["left"] - 1))
        elif edge == "top":
            new_trim["top"] = max(0, min(new_trim["top"] + delta, oh - new_trim["bottom"] - 1))
        elif edge == "bottom":
            new_trim["bottom"] = max(0, min(new_trim["bottom"] + delta, oh - new_trim["top"] - 1))

        self.sample_trim = new_trim
        self.show_sample_preview()

    def reset_sample_crop(self):
        """샘플링 영역 원본으로 복원"""
        if hasattr(self, "sample_original") and self.sample_original:
            self.sample_region = list(self.sample_original)
            self.sample_trim = {"left": 0, "right": 0, "top": 0, "bottom": 0}
            self.show_sample_preview()
            self.show_toast("크롭 리셋 완료")

    def set_sample_zoom(self, zoom):
        """샘플링 줌 설정 (1, 2, 4)"""
        self.sample_zoom = zoom
        if hasattr(self, "sample_region") and self.sample_region:
            self.show_sample_preview()

    def save_sample(self):
        """샘플링된 이미지를 선택한 카테고리 폴더에 저장"""
        print(f"[DEBUG] 저장 시작: sample_pil 존재 여부 확인")
        if not hasattr(self, "sample_pil") or self.sample_pil is None:
            print(f"[DEBUG] sample_pil 없음: hasattr={hasattr(self, 'sample_pil')}, sample_pil={getattr(self, 'sample_pil', None)}")
            self.show_toast("먼저 샘플링을 수행하세요", "#FF5555")
            return

        # 카테고리 가져오기
        category = self.sample_category_var.get() if hasattr(self, "sample_category_var") else "digits"
        print(f"[DEBUG] 카테고리: {category}")

        # 카테고리 폴더 경로 생성
        category_dir = os.path.join(SCRIPT_DIR, "temple", category)
        os.makedirs(category_dir, exist_ok=True)
        print(f"[DEBUG] 카테고리 폴더: {category_dir}")

        # 파일명 입력 다이얼로그
        name = self.show_input_dialog("파일명 입력", f"저장할 파일명을 입력하세요 ({category}):", "")
        print(f"[DEBUG] 입력된 파일명: {name}")
        if not name:
            self.show_toast("파일명을 입력하세요", "#FF5555")
            return

        # 파일명 안전화
        safe_name = "".join(
            ch for ch in str(name) if ch.isalnum() or ch in ("_", "-", ".")
        ).strip(". ")
        print(f"[DEBUG] 안전화된 파일명: {safe_name}")
        if not safe_name:
            self.show_toast("유효한 파일명을 입력하세요", "#FF5555")
            return

        path = os.path.join(category_dir, f"{safe_name}.png")
        print(f"[DEBUG] 저장 경로: {path}")

        # 중복 파일명 체크
        if os.path.exists(path):
            print(f"[DEBUG] 중복 파일명 존재: {path}")
            messagebox.showwarning("중복 파일", f"이미 존재하는 파일명입니다:\n{safe_name}.png\n다른 이름을 사용하세요.")
            return

        try:
            print(f"[DEBUG] 저장 시도: {path}")
            self.sample_pil.save(path)
            print(f"[DEBUG] 저장 성공: {path}")

            # 비트맵 문자열도 .txt 파일로 저장 (ft.ahk FindText 방식)
            if hasattr(self, "sample_binary_str"):
                txt_path = os.path.join(category_dir, f"{safe_name}.txt")
                with open(txt_path, 'w', encoding='utf-8') as f:
                    f.write(self.sample_binary_str)
                print(f"[DEBUG] 비트맵 문자열 저장 성공: {txt_path}")

            self.state.last_update_time = time.time()  # 비전 엔진 리로드 트리거
            self.show_toast(f"{category.upper()}/{safe_name}.png 저장 완료")
            # 샘플링 모드 종료
            if hasattr(self, "sample_pil"):
                delattr(self, "sample_pil")
            if hasattr(self, "sample_binary_str"):
                delattr(self, "sample_binary_str")
        except Exception as e:
            print(f"[DEBUG] 저장 실패: {e}")
            self.show_toast(f"저장 실패: {e}", "#FF5555")

    def _on_threshold_slider_change(self, value):
        """Threshold 슬라이더 변경 시: 라벨 업데이트 + PatternMatcher에 값 전달 + 템플릿 reload"""
        val = int(float(value))
        self.threshold_label.configure(text=str(val))
        # PatternMatcher의 이진화 임계값 동기화
        global reader_thread
        if reader_thread and hasattr(reader_thread, 'matcher'):
            reader_thread.matcher.bin_threshold = val
            # 템플릿 다시 로드 (새 threshold로 이진화)
            tmpl_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temple")
            reader_thread.matcher.reload(tmpl_root)
        # 캘리브레이션 프리뷰 갱신
        self.update_calibration_preview()

    def close_calibration_popup(self):
        if hasattr(self, "calibration_popup") and self.calibration_popup.winfo_exists():
            try:
                self.calibration_popup.update_idletasks()
                self.calibration_geometry = self.calibration_popup.geometry()
            except Exception:
                pass
            self.calibration_popup.destroy()

    def bind_global_zoom_controls(self):
        self.root.bind_all("<Control-MouseWheel>", self.on_ctrl_mousewheel)
        # 일부 환경(Linux/X11)에서 delta 대신 Button-4/5 이벤트를 사용
        self.root.bind_all("<Control-Button-4>", self.on_ctrl_mousewheel)
        self.root.bind_all("<Control-Button-5>", self.on_ctrl_mousewheel)

    def on_ctrl_mousewheel(self, event):
        delta = 0
        if hasattr(event, "delta") and event.delta != 0:
            delta = 1 if event.delta > 0 else -1
        elif getattr(event, "num", None) == 4:
            delta = 1
        elif getattr(event, "num", None) == 5:
            delta = -1

        if delta == 0:
            return "break"

        next_scale = round(self.ui_scale + (delta * self.ui_scale_step), 2)
        next_scale = max(self.ui_scale_min, min(self.ui_scale_max, next_scale))
        if next_scale == self.ui_scale:
            return "break"

        self.ui_scale = next_scale
        ctk.set_widget_scaling(self.ui_scale)
        ctk.set_window_scaling(self.ui_scale)
        self.show_toast(f"UI SCALE: {int(self.ui_scale * 100)}%")
        return "break"

    def build_navigation_control(self):
        # 네비게이션 모드 섹션 - Ultra-Compact
        nav_toggle_box = ctk.CTkFrame(self.c_nav, fg_color="#1A1C23", border_width=1, border_color="#343A46")
        nav_toggle_box.pack(fill="x", padx=1, pady=1)
        ctk.CTkLabel(nav_toggle_box, text="🚀 NAV", font=("Inter", 9, "bold"), text_color="#10B981").pack(pady=1)
        
        switch_row = ctk.CTkFrame(nav_toggle_box, fg_color="transparent")
        switch_row.pack(fill="x", pady=1, padx=2)
        
        self.sw_follow = ctk.CTkCheckBox(switch_row, text="추적", font=("Inter", 9), command=self.update_nav_flags)
        self.sw_follow.pack(side="left", padx=2)
        
        self.sw_route = ctk.CTkCheckBox(switch_row, text="경로", font=("Inter", 9), command=self.update_nav_flags)
        self.sw_route.pack(side="left", padx=2)
        
        self.sw_avoid = ctk.CTkCheckBox(switch_row, text="우회", font=("Inter", 9), command=self.update_nav_flags)
        self.sw_avoid.pack(side="left", padx=2)

        # 유저 탐지 대응 섹션 - Ultra-Compact
        user_opt_box = ctk.CTkFrame(self.c_nav, fg_color="#1A1C23", border_width=1, border_color="#343A46")
        user_opt_box.pack(fill="x", padx=1, pady=1)
        ctk.CTkLabel(user_opt_box, text="👤 USER", font=("Inter", 9, "bold"), text_color="#F87171").pack(pady=1)
        
        # ── 화이트리스트 CSV 편집 UI ──────────────────────────
        wl_box = ctk.CTkFrame(user_opt_box, fg_color="#0E1117", corner_radius=4)
        wl_box.pack(fill="x", padx=4, pady=2)
        ctk.CTkLabel(wl_box, text="✅ 화이트리스트 (whitelist.csv)", font=("Inter", 8, "bold"),
                     text_color="#10B981").pack(anchor="w", padx=4, pady=1)

        # Listbox 영역
        wl_list_frame = ctk.CTkFrame(wl_box, fg_color="#14161B")
        wl_list_frame.pack(fill="x", padx=4, pady=1)
        import tkinter as tk
        self.lb_whitelist = tk.Listbox(
            wl_list_frame, height=4, selectmode=tk.SINGLE,
            bg="#14161B", fg="#E2E8F0", font=("Inter", 8),
            highlightthickness=0, borderwidth=0, relief="flat",
            selectbackground="#10B981", selectforeground="#000"
        )
        self.lb_whitelist.pack(fill="x", padx=2, pady=1)

        # 입력창 + 버튼
        wl_edit_row = ctk.CTkFrame(wl_box, fg_color="transparent")
        wl_edit_row.pack(fill="x", padx=4, pady=1)
        self.ent_wl_add = ctk.CTkEntry(wl_edit_row, width=130, placeholder_text="닉네임 입력",
                                       height=20, font=("Inter", 8))
        self.ent_wl_add.pack(side="left", padx=1)
        ctk.CTkButton(wl_edit_row, text="+추가", height=20, width=40, font=("Inter", 8),
                      fg_color="#10B981", command=self._wl_add_nick).pack(side="left", padx=1)
        ctk.CTkButton(wl_edit_row, text="삭제", height=20, width=40, font=("Inter", 8),
                      fg_color="#EF4444", command=self._wl_del_nick).pack(side="left", padx=1)
        ctk.CTkButton(wl_edit_row, text="저장", height=20, width=40, font=("Inter", 8),
                      fg_color="#A855F7", command=self._wl_save).pack(side="left", padx=1)

        # 초기 로드
        self._wl_refresh_listbox()

        # 대응 옵션 스위치
        act_row = ctk.CTkFrame(user_opt_box, fg_color="transparent")
        act_row.pack(fill="x", pady=1, padx=2)
        
        self.sw_u_alarm = ctk.CTkCheckBox(act_row, text="알림", font=("Inter", 9), command=self.sync_user_opts)
        self.sw_u_alarm.pack(side="left", padx=2)
        
        self.sw_u_stop = ctk.CTkCheckBox(act_row, text="정지", font=("Inter", 9), command=self.sync_user_opts)
        self.sw_u_stop.pack(side="left", padx=2)
        
        self.sw_u_next = ctk.CTkCheckBox(act_row, text="무시", font=("Inter", 9), command=self.sync_user_opts)
        self.sw_u_next.pack(side="left", padx=2)
        
        self.sw_auto_debuff = ctk.CTkCheckBox(act_row, text="저주", font=("Inter", 9), command=self.sync_user_opts)
        self.sw_auto_debuff.pack(side="left", padx=2)

        # ── Grid 미세조정 슬라이더 ──────────────────────────
        grid_tune_box = ctk.CTkFrame(self.c_nav, fg_color="#1A1C23", border_width=1, border_color="#343A46")
        grid_tune_box.pack(fill="x", padx=1, pady=1)
        ctk.CTkLabel(grid_tune_box, text="📐 Grid 미세조정", font=("Inter", 9, "bold"), text_color="#F59E0B").pack(pady=1)

        # 오프셋 X 슬라이더
        sx_row = ctk.CTkFrame(grid_tune_box, fg_color="transparent")
        sx_row.pack(fill="x", padx=4, pady=1)
        ctk.CTkLabel(sx_row, text="Offset X", font=("Inter", 8), width=50).pack(side="left")
        self.grid_sx_var = tk.IntVar(value=276)
        self.grid_sx_slider = ctk.CTkSlider(sx_row, from_=250, to=300, variable=self.grid_sx_var,
                                            height=12, width=120, command=self._on_grid_tune)
        self.grid_sx_slider.pack(side="left", padx=2)
        self.grid_sx_lbl = ctk.CTkLabel(sx_row, text="276", font=("Inter", 8), width=30)
        self.grid_sx_lbl.pack(side="left")

        # 오프셋 Y 슬라이더
        sy_row = ctk.CTkFrame(grid_tune_box, fg_color="transparent")
        sy_row.pack(fill="x", padx=4, pady=1)
        ctk.CTkLabel(sy_row, text="Offset Y", font=("Inter", 8), width=50).pack(side="left")
        self.grid_sy_var = tk.IntVar(value=32)
        self.grid_sy_slider = ctk.CTkSlider(sy_row, from_=20, to=50, variable=self.grid_sy_var,
                                            height=12, width=120, command=self._on_grid_tune)
        self.grid_sy_slider.pack(side="left", padx=2)
        self.grid_sy_lbl = ctk.CTkLabel(sy_row, text="32", font=("Inter", 8), width=30)
        self.grid_sy_lbl.pack(side="left")

        # 그리드 크기 슬라이더
        gs_row = ctk.CTkFrame(grid_tune_box, fg_color="transparent")
        gs_row.pack(fill="x", padx=4, pady=1)
        ctk.CTkLabel(gs_row, text="Grid Size", font=("Inter", 8), width=50).pack(side="left")
        self.grid_size_var = tk.DoubleVar(value=48.2)
        self.grid_size_slider = ctk.CTkSlider(gs_row, from_=47.0, to=49.0, variable=self.grid_size_var,
                                              height=12, width=120, number_of_steps=20,
                                              command=self._on_grid_tune)
        self.grid_size_slider.pack(side="left", padx=2)
        self.grid_size_lbl = ctk.CTkLabel(gs_row, text="48.2", font=("Inter", 8), width=35)
        self.grid_size_lbl.pack(side="left")

        # 저장 버튼
        ctk.CTkButton(grid_tune_box, text="💾 저장", height=20, width=60, font=("Inter", 8),
                      fg_color="#F59E0B", command=self._save_grid_tune).pack(pady=2)

        # 사냥 경로 섹션 - Ultra-Compact
        seq_box = ctk.CTkFrame(self.c_nav, fg_color="#1A1C23", border_width=1, border_color="#343A46")
        seq_box.pack(fill="x", padx=1, pady=1)
        ctk.CTkLabel(seq_box, text="🧭 사냥 경로", font=("Inter", 9, "bold"), text_color="#A855F7").pack(pady=1)
        
        # 상단: 맵/층 선택 및 역방향 모드
        top_row = ctk.CTkFrame(seq_box, fg_color="transparent")
        top_row.pack(fill="x", pady=1, padx=2)
        
        ctk.CTkLabel(top_row, text="맵:", font=("Inter", 9)).pack(side="left", padx=1)
        self.hunting_map_var = ctk.StringVar(value="복건천리마굴")
        self.hunting_map_dropdown = ctk.CTkOptionMenu(top_row, variable=self.hunting_map_var, values=["복건천리마굴"], command=self.on_hunting_map_change, width=80)
        self.hunting_map_dropdown.pack(side="left", padx=1)
        
        ctk.CTkLabel(top_row, text="층:", font=("Inter", 9)).pack(side="left", padx=1)
        self.hunting_floor_var = ctk.StringVar(value="1")
        self.hunting_floor_dropdown = ctk.CTkOptionMenu(top_row, variable=self.hunting_floor_var, values=["1"], command=self.on_hunting_floor_change, width=50)
        self.hunting_floor_dropdown.pack(side="left", padx=1)
        
        self.sw_reverse_mode = ctk.CTkCheckBox(top_row, text="역", font=("Inter", 9), command=self.toggle_reverse_mode)
        self.sw_reverse_mode.pack(side="left", padx=1)
        
        # 관리 버튼 (한 줄에 4개)
        mid_row = ctk.CTkFrame(seq_box, fg_color="transparent")
        mid_row.pack(fill="x", pady=1, padx=2)
        
        ctk.CTkButton(mid_row, text="+맵", height=22, width=50, font=("Inter", 9), command=self.add_hunting_map).pack(side="left", padx=1)
        ctk.CTkButton(mid_row, text="+층", height=22, width=50, font=("Inter", 9), command=self.add_hunting_floor).pack(side="left", padx=1)
        ctk.CTkButton(mid_row, text="+좌표", height=22, width=50, font=("Inter", 9), command=self.add_current_coord).pack(side="left", padx=1)
        ctk.CTkButton(mid_row, text="저장", height=22, width=50, font=("Inter", 9), command=self.save_waypoints).pack(side="left", padx=1)
        
        # Treeview (엑셀 스타일 표)
        tree_container = ctk.CTkFrame(seq_box, fg_color="#14161B")
        tree_container.pack(fill="x", pady=1, padx=2)
        
        tree_frame = tk.Frame(tree_container, bg="#14161B")
        tree_frame.pack(fill="x", padx=2, pady=2)
        
        columns = ("type", "x", "y", "direction", "reverse_action", "grid")
        self.seq_tree = tk.ttk.Treeview(tree_frame, columns=columns, show="headings", height=12)
        self.seq_tree.heading("type", text="T")
        self.seq_tree.heading("x", text="X")
        self.seq_tree.heading("y", text="Y")
        self.seq_tree.heading("direction", text="D")
        self.seq_tree.heading("reverse_action", text="R")
        self.seq_tree.heading("grid", text="Grid")
        
        self.seq_tree.column("type", width=40)
        self.seq_tree.column("x", width=40)
        self.seq_tree.column("y", width=40)
        self.seq_tree.column("direction", width=40)
        self.seq_tree.column("reverse_action", width=40)
        self.seq_tree.column("grid", width=50)
        
        self.seq_tree.pack(fill="x")
        self.seq_tree.bind("<Double-1>", self.on_tree_double_click)
        self.seq_tree.bind("<Key>", self.on_tree_key_press)
        
        # 관리 도구 (▲ ▼ 삭제 저장 현재좌표입력 이동)
        tool_row = ctk.CTkFrame(seq_box, fg_color="transparent")
        tool_row.pack(fill="x", pady=1, padx=2)
        
        ctk.CTkButton(tool_row, text="▲", height=22, width=30, font=("Inter", 9), command=self.move_seq_up).pack(side="left", padx=1)
        ctk.CTkButton(tool_row, text="▼", height=22, width=30, font=("Inter", 9), command=self.move_seq_down).pack(side="left", padx=1)
        ctk.CTkButton(tool_row, text="×", height=22, width=30, font=("Inter", 9), command=self.delete_seq_item).pack(side="left", padx=1)
        ctk.CTkButton(tool_row, text="💾", height=22, width=30, font=("Inter", 9), command=self.save_waypoints).pack(side="left", padx=1)
        ctk.CTkButton(tool_row, text="현재좌표입력", height=22, width=70, font=("Inter", 9), command=self.set_current_coord_to_selected).pack(side="left", padx=1)
        ctk.CTkButton(tool_row, text="이동", height=22, width=40, font=("Inter", 9), command=self.move_to_selected_coord).pack(side="left", padx=1)
        
        # 추가 버튼 (입구 사냥 출구)
        add_row = ctk.CTkFrame(seq_box, fg_color="transparent")
        add_row.pack(fill="x", pady=1, padx=2)
        
        ctk.CTkButton(add_row, text="입구", height=22, width=80, font=("Inter", 9), command=self.add_seq_entry).pack(side="left", padx=1)
        ctk.CTkButton(add_row, text="사냥", height=22, width=80, font=("Inter", 9), command=self.add_seq_point).pack(side="left", padx=1)
        ctk.CTkButton(add_row, text="출구", height=22, width=80, font=("Inter", 9), command=self.add_seq_exit).pack(side="left", padx=1)
        
        # 초기 시퀀스 로드
        self.load_hunting_sequence()

    def update_nav_flags(self):
        self.state.nav_follow_enabled = self.sw_follow.get()
        self.state.nav_route_enabled = self.sw_route.get()
        self.state.nav_avoid_enabled = self.sw_avoid.get()
        self.show_toast("⚙️ 이동 모드 설정 변경됨")

    def sync_user_opts(self):
        self.state.user_alarm_enabled = self.sw_u_alarm.get()
        self.state.user_stop_enabled = self.sw_u_stop.get()
        self.state.user_next_enabled = self.sw_u_next.get()
        self.state.auto_debuff_enabled = self.sw_auto_debuff.get()
        self.show_toast("👤 유저 탐지 설정 변경됨")

    # ── Grid 미세조정 헬퍼 ──────────────────────────────
    def _on_grid_tune(self, val=None):
        """슬라이더 변경 시 라벨 업데이트 + config.json 즉시 저장"""
        sx = int(self.grid_sx_var.get())
        sy = int(self.grid_sy_var.get())
        gs = round(float(self.grid_size_var.get()), 1)
        self.grid_sx_lbl.configure(text=str(sx))
        self.grid_sy_lbl.configure(text=str(sy))
        self.grid_size_lbl.configure(text=f"{gs:.1f}")
        # config.json 즉시 저장
        self._save_grid_tune()

    def _save_grid_tune(self):
        """Grid 미세조정 값을 config.json에 저장"""
        try:
            sx = int(self.grid_sx_var.get())
            sy = int(self.grid_sy_var.get())
            gs = round(float(self.grid_size_var.get()), 1)
            config_file = os.path.join(SCRIPT_DIR, "config.json")
            conf = {}
            if os.path.exists(config_file):
                with open(config_file, 'r', encoding='utf-8') as f:
                    conf = json.load(f)
            if "play_area" not in conf:
                conf["play_area"] = {}
            conf["play_area"]["sx"] = sx
            conf["play_area"]["sy"] = sy
            conf["play_area"]["grid_size"] = gs
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(conf, f, indent=4, ensure_ascii=False)
            # GridIndicator에도 즉시 반영
            if hasattr(self, 'grid_indicator') and self.grid_indicator:
                self.grid_indicator.grid_size = gs
            self.show_toast(f"📐 Grid 조정: sx={sx}, sy={sy}, size={gs}")
        except Exception as e:
            print(f"[GridTune] 저장 실패: {e}")

    # ── 화이트리스트 관리 헬퍼 ──────────────────────────────
    def _wl_refresh_listbox(self):
        self.lb_whitelist.delete(0, tk.END)
        for nick in self.state.whitelist_names:
            self.lb_whitelist.insert(tk.END, nick)

    def _wl_add_nick(self):
        nick = self.ent_wl_add.get().strip()
        if not nick:
            return
        if nick not in self.state.whitelist_names:
            self.state.whitelist_names.append(nick)
            self._wl_refresh_listbox()
        self.ent_wl_add.delete(0, tk.END)

    def _wl_del_nick(self):
        sel = self.lb_whitelist.curselection()
        if not sel:
            return
        idx = sel[0]
        nick = self.lb_whitelist.get(idx)
        if nick in self.state.whitelist_names:
            self.state.whitelist_names.remove(nick)
            self._wl_refresh_listbox()

    def _wl_save(self):
        # GameState에 보관된 리스트를 CSV에 저장
        self.state.save_whitelist_csv()
        self.show_toast("✅ 화이트리스트 저장됨")


    class GridConverter:
        """Grid 좌표 변환 유틸리티 클래스"""
        
        @staticmethod
        def name_to_index(grid_name):
            """Grid 이름(S3, A10 등)을 내부 인덱스(col, row)로 변환"""
            if not grid_name or len(grid_name) < 2:
                return None, None
            # 알파벳 추출 (열)
            col_part = ""
            row_part = ""
            for i, char in enumerate(grid_name):
                if char.isalpha():
                    col_part += char.upper()
                else:
                    row_part = grid_name[i:]
                    break
            if not col_part or not row_part:
                return None, None
            try:
                # 열 계산 (A=0, B=1, ..., S=18)
                col = 0
                for char in col_part:
                    col = col * 26 + (ord(char) - ord('A'))
                # 행 계산 (1-indexed → 0-indexed)
                row = int(row_part) - 1
                return col, row
            except (ValueError, IndexError):
                return None, None
        
        @staticmethod
        def index_to_name(col, row):
            """내부 인덱스(col, row)를 Grid 이름(S3, A10 등)으로 변환"""
            try:
                # 열 계산 (0-indexed → 알파벳)
                col_name = ""
                temp_col = col
                while temp_col >= 0:
                    col_name = chr(ord('A') + (temp_col % 26)) + col_name
                    temp_col = temp_col // 26 - 1
                    if temp_col < 0:
                        break
                # 행 계산 (0-indexed → 1-indexed)
                row_name = str(row + 1)
                return f"{col_name}{row_name}"
            except:
                return None

    def grid_name_to_coords(self, grid_name):
        """Grid 이름(S3, A10 등)을 좌표(col, row)로 변환 (GridConverter 사용)"""
        return AppView.GridConverter.name_to_index(grid_name)

    def coords_to_grid_name(self, col, row):
        """좌표(col, row)를 Grid 이름(S3, A10 등)으로 변환 (GridConverter 사용)"""
        return AppView.GridConverter.index_to_name(col, row)

    def save_waypoints(self):
        waypoint_file = os.path.join(SCRIPT_DIR, "waypoints.json")
        with open(waypoint_file, 'w', encoding='utf-8') as f:
            json.dump(self.state.waypoints_db, f, indent=4)
            
        # CSV 추가 저장 (엑셀 관리용 표 생성)
        try:
            csv_path = os.path.join(SCRIPT_DIR, "waypoints.csv")
            with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(["Map", "Floor", "Type", "X", "Y", "Direction", "ReverseAction", "Grid"])
                
                for map_name, floors in self.state.waypoints_db.items():
                    for floor_num, data in floors.items():
                        if "entry" in data:
                            e = data["entry"]
                            writer.writerow([map_name, floor_num, "Entry", e.get("x",0), e.get("y",0), e.get("direction",""), e.get("reverse_action",""), ""])
                        if "points" in data:
                            for point in data["points"]:
                                writer.writerow([map_name, floor_num, "Hunt", 0, 0, "", "", point])
                        if "exit" in data:
                            ex = data["exit"]
                            writer.writerow([map_name, floor_num, "Exit", ex.get("x",0), ex.get("y",0), ex.get("direction",""), ex.get("reverse_action",""), ""])
        except Exception as e:
            print(f"[Warn] CSV 백업 실패: {e}")
            
        self.show_toast("💾 웨이포인트(JSON/CSV) 저장 완료")

    def load_hunting_sequence(self):
        """현재 선택된 맵/층의 시퀀스를 ListView에 로드 (CSV 우선)"""
        csv_path = os.path.join(SCRIPT_DIR, "waypoints.csv")
        waypoint_file = os.path.join(SCRIPT_DIR, "waypoints.json")
        loaded_db = {}
        
        # 1. CSV 파일 우선 로드 (사용자 직접 엑셀 편집 반영)
        if os.path.exists(csv_path):
            try:
                with open(csv_path, 'r', encoding='utf-8-sig') as f:
                    reader = csv.reader(f)
                    header = next(reader)
                    for row in reader:
                        if len(row) < 3: continue
                        m_name, f_num, t_type = row[0], row[1], row[2]
                        if m_name not in loaded_db: loaded_db[m_name] = {}
                        if f_num not in loaded_db[m_name]: 
                            loaded_db[m_name][f_num] = {"points": []}
                        
                        target_dict = loaded_db[m_name][f_num]
                        
                        x = int(row[3]) if len(row)>3 and row[3].isdigit() else 0
                        y = int(row[4]) if len(row)>4 and row[4].isdigit() else 0
                        d = row[5] if len(row)>5 else ""
                        r_act = row[6] if len(row)>6 else ""
                        g_str = row[7] if len(row)>7 else ""
                        
                        if t_type == "Entry":
                            target_dict["entry"] = {"x":x, "y":y, "direction":d, "reverse_action":r_act}
                        elif t_type == "Exit":
                            target_dict["exit"] = {"x":x, "y":y, "direction":d, "reverse_action":r_act}
                        elif t_type == "Hunt":
                            if g_str: target_dict["points"].append(g_str)
            except Exception as e:
                print(f"[Warn] CSV 로드 실패, JSON 폴백 시도: {e}")
        
        # 2. CSV가 없을 경우 기존 JSON.load
        if not loaded_db and os.path.exists(waypoint_file):
            try:
                with open(waypoint_file, "r", encoding="utf-8") as f:
                    loaded_db = json.load(f)
            except (OSError, json.JSONDecodeError):
                loaded_db = {}
        
        self.state.waypoints_db = loaded_db
        
        # 맵 드롭다운 업데이트
        map_names = list(self.state.waypoints_db.keys())
        self.hunting_map_dropdown.configure(values=map_names if map_names else ["복건천리마굴"])
        
        # 층 드롭다운 업데이트
        current_map = self.hunting_map_var.get()
        if current_map in self.state.waypoints_db:
            floors = list(self.state.waypoints_db[current_map].keys())
            self.hunting_floor_dropdown.configure(values=floors if floors else ["1"])
        
        # 시퀀스 로드
        self.refresh_seq_tree()

    def refresh_seq_tree(self):
        """시퀀스 Treeview 갱신"""
        for item in self.seq_tree.get_children():
            self.seq_tree.delete(item)
        
        current_map = self.hunting_map_var.get()
        current_floor = self.hunting_floor_var.get()
        
        if current_map in self.state.waypoints_db and current_floor in self.state.waypoints_db[current_map]:
            seq_data = self.state.waypoints_db[current_map][current_floor]
            
            # 입구
            if "entry" in seq_data:
                entry = seq_data["entry"]
                x, y = entry.get("x", 0), entry.get("y", 0)
                grid_str = self.calculate_grid_coord(x, y)
                self.seq_tree.insert("", "end", values=(
                    "Entry",
                    x,
                    y,
                    entry.get("direction", ""),
                    entry.get("reverse_action", ""),
                    grid_str
                ))
            
            # 사냥점
            if "points" in seq_data:
                for point in seq_data["points"]:
                    col, row = self.GridConverter.name_to_index(point)
                    grid_str = point if isinstance(point, str) and len(point) <= 5 else ""
                    self.seq_tree.insert("", "end", values=(
                        "Hunt",
                        col if col is not None else 0,
                        row if row is not None else 0,
                        "",
                        "",
                        grid_str
                    ))
            
            # 출구
            if "exit" in seq_data:
                exit_data = seq_data["exit"]
                x, y = exit_data.get("x", 0), exit_data.get("y", 0)
                grid_str = self.calculate_grid_coord(x, y)
                self.seq_tree.insert("", "end", values=(
                    "Exit",
                    x,
                    y,
                    exit_data.get("direction", ""),
                    exit_data.get("reverse_action", ""),
                    grid_str
                ))

    def on_hunting_map_change(self, value):
        """맵 드롭다운 변경 시 층 드롭다운 및 시퀀스 갱신"""
        current_map = value
        if current_map in self.state.waypoints_db:
            floors = list(self.state.waypoints_db[current_map].keys())
            self.hunting_floor_dropdown.configure(values=floors if floors else ["1"])
            if floors:
                self.hunting_floor_var.set(floors[0])
        self.refresh_seq_tree()

    def on_hunting_floor_change(self, value):
        """층 드롭다운 변경 시 시퀀스 갱신"""
        self.refresh_seq_tree()

    def add_hunting_floor(self):
        """새 층 추가"""
        current_map = self.hunting_map_var.get()
        if current_map not in self.state.waypoints_db:
            self.show_toast("맵을 먼저 선택해주세요", "#FF5555")
            return
        
        pop = ctk.CTkToplevel(self.root)
        pop.title("새 층 추가")
        pop.geometry("300x150")
        pop.attributes("-topmost", True)
        pop.transient(self.root)
        
        ctk.CTkLabel(pop, text="층 이름:", font=("Inter", 11)).pack(pady=10)
        ent_name = ctk.CTkEntry(pop, width=150)
        ent_name.pack(pady=5)
        
        def on_ok():
            name = ent_name.get().strip()
            if name:
                if name not in self.state.waypoints_db[current_map]:
                    self.state.waypoints_db[current_map][name] = {
                        "entry": {"x": 0, "y": 0, "direction": "right", "reverse_action": ""},
                        "exit": {"x": 0, "y": 0, "direction": "down", "reverse_action": ""},
                        "points": []
                    }
                    self.save_waypoints()
                    self.load_hunting_sequence()
                    self.hunting_floor_var.set(name)
                    self.show_toast(f"층 '{name}' 추가 완료")
                    pop.destroy()
                else:
                    self.show_toast("이미 존재하는 층입니다", "#FF5555")
        
        ctk.CTkButton(pop, text="확인", command=on_ok).pack(pady=10)

    def add_current_coord(self):
        """현재 좌표 추가"""
        current_map = self.hunting_map_var.get()
        current_floor = self.hunting_floor_var.get()
        if current_map not in self.state.waypoints_db or current_floor not in self.state.waypoints_db[current_map]:
            self.show_toast("맵과 층을 먼저 선택해주세요", "#FF5555")
            return
        
        data = self.state.get_all()
        x, y = data.get("x", 0), data.get("y", 0)
        seq_data = self.state.waypoints_db[current_map][current_floor]
        seq_data["points"].append(f"{x},{y}")
        self.save_waypoints()
        self.refresh_seq_tree()
        self.show_toast(f"현재 좌표 ({x}, {y}) 추가 완료")

    def on_tree_double_click(self, event):
        """Treeview 셀 더블클릭 시 편집"""
        item = self.seq_tree.selection()
        if not item:
            return
        item = item[0]
        column = self.seq_tree.identify_column(event.x)
        
        # 컬럼 인덱스 가져오기
        col_map = {"#1": "type", "#2": "x", "#3": "y", "#4": "direction", "#5": "reverse_action"}
        col_name = col_map.get(column, "")
        
        if not col_name:
            return
        
        current_values = self.seq_tree.item(item, "values")
        col_idx = list(col_map.values()).index(col_name)
        current_value = current_values[col_idx]
        
        # 편집 팝업
        pop = ctk.CTkToplevel(self.root)
        pop.title("셀 편집")
        pop.geometry("300x120")
        pop.attributes("-topmost", True)
        pop.transient(self.root)
        
        ctk.CTkLabel(pop, text=f"{col_name} 수정:", font=("Inter", 10)).pack(pady=5)
        ent = ctk.CTkEntry(pop, width=200)
        ent.insert(0, str(current_value))
        ent.pack(pady=5)
        ent.select_range(0, tk.END)
        ent.focus()
        
        def on_ok():
            new_value = ent.get().strip()
            values = list(current_values)
            values[col_idx] = new_value
            self.seq_tree.item(item, values=tuple(values))
            
            # waypoints.json에 저장
            self.save_tree_to_data()
            pop.destroy()
        
        def on_cancel():
            pop.destroy()
        
        btn_row = ctk.CTkFrame(pop, fg_color="transparent")
        btn_row.pack(pady=5)
        ctk.CTkButton(btn_row, text="확인", height=24, width=80, command=on_ok).pack(side="left", padx=5)
        ctk.CTkButton(btn_row, text="취소", height=24, width=80, command=on_cancel).pack(side="left", padx=5)
        
        ent.bind("<Return>", lambda e: on_ok())
        ent.bind("<Escape>", lambda e: on_cancel())

    def save_tree_to_data(self):
        """Treeview 데이터를 waypoints.json에 저장"""
        current_map = self.hunting_map_var.get()
        current_floor = self.hunting_floor_var.get()
        if current_map not in self.state.waypoints_db or current_floor not in self.state.waypoints_db[current_map]:
            return
        
        seq_data = self.state.waypoints_db[current_map][current_floor]
        entry_data = None
        exit_data = None
        points = []
        
        for item in self.seq_tree.get_children():
            values = self.seq_tree.item(item, "values")
            item_type = values[0]
            
            if item_type == "Entry":
                entry_data = {
                    "x": int(values[1]) if values[1] else 0,
                    "y": int(values[2]) if values[2] else 0,
                    "direction": values[3] if values[3] else "right",
                    "reverse_action": values[4] if values[4] else ""
                }
            elif item_type == "Exit":
                exit_data = {
                    "x": int(values[1]) if values[1] else 0,
                    "y": int(values[2]) if values[2] else 0,
                    "direction": values[3] if values[3] else "down",
                    "reverse_action": values[4] if values[4] else ""
                }
            elif item_type == "Hunt":
                x = int(values[1]) if values[1] else 0
                y = int(values[2]) if values[2] else 0
                grid_name = self.GridConverter.index_to_name(x, y)
                if grid_name:
                    points.append(grid_name)
        
        if entry_data:
            seq_data["entry"] = entry_data
        if exit_data:
            seq_data["exit"] = exit_data
        seq_data["points"] = points
        
        self.save_waypoints()

    def add_hunting_map(self):
        """새 맵 추가"""
        pop = ctk.CTkToplevel(self.root)
        pop.title("새 맵 추가")
        pop.geometry("300x150")
        pop.attributes("-topmost", True)
        pop.transient(self.root)
        
        ctk.CTkLabel(pop, text="맵 이름:", font=("Inter", 11)).pack(pady=10)
        ent_name = ctk.CTkEntry(pop, width=200)
        ent_name.pack(pady=5)
        
        def on_ok():
            name = ent_name.get().strip()
            if name:
                if name not in self.state.waypoints_db:
                    self.state.waypoints_db[name] = {
                        "1": {
                            "entry": {"x": 0, "y": 0, "direction": "right", "reverse_action": ""},
                            "exit": {"x": 0, "y": 0, "direction": "down", "reverse_action": ""},
                            "points": []
                        }
                    }
                    self.save_waypoints()
                    self.load_hunting_sequence()
                    self.hunting_map_var.set(name)
                    self.show_toast(f"맵 '{name}' 추가 완료")
                    pop.destroy()
                else:
                    self.show_toast("이미 존재하는 맵입니다", "#FF5555")
        
        ctk.CTkButton(pop, text="확인", command=on_ok).pack(pady=10)

    def delete_hunting_map(self):
        """현재 맵 삭제"""
        current_map = self.hunting_map_var.get()
        if current_map in self.state.waypoints_db:
            del self.state.waypoints_db[current_map]
            self.save_waypoints()
            self.load_hunting_sequence()
            self.show_toast(f"맵 '{current_map}' 삭제 완료")

    def delete_seq_item(self):
        """시퀀스 항목 삭제 (Treeview용)"""
        item = self.seq_tree.selection()
        if not item:
            return
        item = item[0]
        
        self.seq_tree.delete(item)
        self.save_tree_to_data()
        self.show_toast("항목 삭제 완료")

    def move_seq_up(self):
        """시퀀스 항목 위로 이동 (Treeview용)"""
        item = self.seq_tree.selection()
        if not item:
            return
        item = item[0]
        
        # 현재 아이템의 값 가져오기
        values = self.seq_tree.item(item, "values")
        
        # 이전 아이템 찾기
        items = self.seq_tree.get_children()
        idx = items.index(item)
        if idx == 0:
            return  # 첫 번째 아이템은 이동 불가
        
        # 이전 아이템과 값 교환
        prev_item = items[idx - 1]
        prev_values = self.seq_tree.item(prev_item, "values")
        
        self.seq_tree.item(item, values=prev_values)
        self.seq_tree.item(prev_item, values=values)
        self.seq_tree.selection_set(prev_item)
        
        self.save_tree_to_data()

    def move_seq_down(self):
        """시퀀스 항목 아래로 이동 (Treeview용)"""
        item = self.seq_tree.selection()
        if not item:
            return
        item = item[0]
        
        # 현재 아이템의 값 가져오기
        values = self.seq_tree.item(item, "values")
        
        # 다음 아이템 찾기
        items = self.seq_tree.get_children()
        idx = items.index(item)
        if idx == len(items) - 1:
            return  # 마지막 아이템은 이동 불가
        
        # 다음 아이템과 값 교환
        next_item = items[idx + 1]
        next_values = self.seq_tree.item(next_item, "values")
        
        self.seq_tree.item(item, values=next_values)
        self.seq_tree.item(next_item, values=values)
        self.seq_tree.selection_set(next_item)
        
        self.save_tree_to_data()

    def add_seq_entry(self):
        """입구 추가 (Treeview용)"""
        current_map = self.hunting_map_var.get()
        current_floor = self.hunting_floor_var.get()
        if current_map not in self.state.waypoints_db or current_floor not in self.state.waypoints_db[current_map]:
            return
        
        # Treeview에 입구 행 추가
        self.seq_tree.insert("", 0, values=("Entry", 0, 0, "right", "", ""))
        self.save_tree_to_data()
        self.show_toast("입구 추가 완료")

    def add_seq_point(self):
        """사냥점 추가 (Treeview용)"""
        current_map = self.hunting_map_var.get()
        current_floor = self.hunting_floor_var.get()
        if current_map not in self.state.waypoints_db or current_floor not in self.state.waypoints_db[current_map]:
            return
        
        # 현재 좌표 가져오기
        data = self.state.get_all()
        x, y = data.get("x", 0), data.get("y", 0)
        
        # grid 좌표 계산
        grid_str = self.calculate_grid_coord(x, y)
        
        # Treeview에 사냥점 행 추가
        self.seq_tree.insert("", "end", values=("Hunt", x, y, "", "", grid_str))
        self.save_tree_to_data()
        self.show_toast("사냥점 추가 완료")

    def add_seq_exit(self):
        """출구 추가 (Treeview용)"""
        current_map = self.hunting_map_var.get()
        current_floor = self.hunting_floor_var.get()
        if current_map not in self.state.waypoints_db or current_floor not in self.state.waypoints_db[current_map]:
            return
        
        # Treeview에 출구 행 추가
        self.seq_tree.insert("", "end", values=("Exit", 0, 0, "down", "", ""))
        self.save_tree_to_data()
        self.show_toast("출구 추가 완료")

    def on_tree_key_press(self, event):
        """Treeview 키보드 이벤트 처리 - 방향키로 direction 입력"""
        item = self.seq_tree.selection()
        if not item:
            return
        item = item[0]
        
        # 방향키 매핑
        direction_map = {
            "Up": "up",
            "Down": "down",
            "Left": "left",
            "Right": "right"
        }
        
        if event.keysym in direction_map:
            direction = direction_map[event.keysym]
            # 현재 값 가져오기
            values = list(self.seq_tree.item(item, "values"))
            # direction 컬럼 업데이트 (index 3)
            values[3] = direction
            self.seq_tree.item(item, values=values)
            self.save_tree_to_data()
            self.show_toast(f"방향: {direction}")

    def calculate_grid_coord(self, x, y):
        """x, y 좌표에서 grid 좌표 계산"""
        try:
            # hwnd 유효성 검사
            if not self.hwnd:
                return ""

            # ── 확정 오프셋: sx=276, sy=32 / 격자 크기 48.2 ──
            # 공식: GridX = (PixelX - 276) / 48.2, GridY = (PixelY - 32) / 48.2
            config_file = os.path.join(SCRIPT_DIR, "config.json")
            play_area_sx = 276  # 확정 오프셋
            play_area_sy = 32   # 확정 오프셋
            grid_size = 48.2    # float (누적오차 방지)
            if os.path.exists(config_file):
                try:
                    with open(config_file, 'r', encoding='utf-8') as f:
                        conf = json.load(f)
                        pa = conf.get("play_area", {})
                        play_area_sx = pa.get("sx", 276)
                        play_area_sy = pa.get("sy", 32)
                        grid_size = float(pa.get("grid_size", 48.2))
                except Exception:
                    pass

            grid_x = int((x - play_area_sx) / grid_size)
            grid_y = int((y - play_area_sy) / grid_size)
            
            # grid 좌표를 문자열로 변환 (예: A1, B2, C3)
            if grid_x < 0 or grid_y < 0:
                return ""
            
            col_char = chr(ord('A') + grid_x)
            grid_str = f"{col_char}{grid_y + 1}"
            
            return grid_str
        except Exception as e:
            print(f"grid 좌표 계산 오류: {e}")
            return ""

    def set_current_coord_to_selected(self):
        """현재 게임 좌표를 선택된 항목의 x,y에 입력"""
        item = self.seq_tree.selection()
        if not item:
            self.show_toast("항목을 선택해주세요", color="#EF4444")
            return
        item = item[0]
        
        # 현재 게임 좌표 가져오기
        data = self.state.get_all()
        x, y = data.get("x", 0), data.get("y", 0)
        
        # grid 좌표 계산
        grid_str = self.calculate_grid_coord(x, y)
        
        # 현재 값 가져오기
        values = list(self.seq_tree.item(item, "values"))
        # x, y, grid 컬럼 업데이트 (index 1, 2, 5)
        values[1] = str(x)
        values[2] = str(y)
        values[5] = grid_str
        self.seq_tree.item(item, values=values)
        self.save_tree_to_data()
        self.show_toast(f"좌표 입력: ({x}, {y}) Grid: {grid_str}")

    def move_to_selected_coord(self):
        """선택된 항목의 좌표로 이동"""
        print("[Move] F1 키 눌림")
        item = self.seq_tree.selection()
        if not item:
            print("[Move] 항목 선택 안됨, 첫 번째 항목 자동 선택")
            # 첫 번째 항목 자동 선택
            first_item = self.seq_tree.get_children()
            if first_item:
                self.seq_tree.selection_set(first_item[0])
                item = self.seq_tree.selection()
                if not item:
                    print("[Move] 첫 번째 항목 선택 실패")
                    self.show_toast("항목이 없습니다", color="#EF4444")
                    return
                item = item[0]
            else:
                print("[Move] 항목이 없음")
                self.show_toast("항목이 없습니다", color="#EF4444")
                return
        else:
            item = item[0]
        
        # 선택된 항목의 좌표 가져오기
        values = self.seq_tree.item(item, "values")
        x = int(values[1]) if values[1] else 0
        y = int(values[2]) if values[2] else 0
        grid_str = values[5] if len(values) > 5 else ""
        print(f"[Move] 선택된 좌표: ({x}, {y}), 그리드: {grid_str}")
        
        # 현재 좌표 가져오기
        data = self.state.get_all()
        current_x, current_y = data.get("x", 0), data.get("y", 0)
        print(f"[Move] 현재 좌표: ({current_x}, {current_y})")
        
        # 방향 결정
        dx = x - current_x
        dy = y - current_y
        print(f"[Move] dx: {dx}, dy: {dy}")
        
        # 방향키 입력 (아두이노 통신)
        if abs(dx) > abs(dy):
            direction = "right" if dx > 0 else "left"
        else:
            direction = "down" if dy > 0 else "up"
        
        print(f"[Move] 방향: {direction}")
        
        # 아두이노로 방향키 전송
        try:
            hw.hold_move(direction, "move_hold")
            print("[Move] 아두이노 전송 완료")
        except Exception as e:
            print(f"[Move] 아두이노 전송 실패: {e}")
        
        self.show_toast(f"이동: {direction} -> ({x}, {y}) Grid: {grid_str}")

    def toggle_reverse_mode(self):
        """역방향 회항 모드 토글"""
        self.state.reverse_mode = self.sw_reverse_mode.get()
        self.show_toast(f"역방향 회항 모드: {'ON' if self.state.reverse_mode else 'OFF'}")

    def show_toast(self, msg, color="#10B981"):
        """메인 스레드가 아닌 곳에서도 안전하게 토스트 메시지를 호출할 수 있도록 수정"""
        def _logic():
            self.toast_lbl.configure(text=msg, text_color=color)
            self.root.after(2200, lambda: self.toast_lbl.configure(text=""))
        
        try:
            self.root.after(0, _logic)
        except Exception:
            pass

    def save_triggers(self):
        config_file = os.path.join(SCRIPT_DIR, "config.json")
        if os.path.exists(config_file):
            with open(config_file, 'r', encoding='utf-8') as f:
                conf = json.load(f)
        else:
            conf = {}
        
        conf["recovery_hp_spell"] = self.cb_hp_trig.get()
        conf["recovery_mp_spell"] = self.cb_mp_trig.get()
        conf["recovery_debuff_spell"] = self.cb_debuff_trig.get()
        conf["auto_debuff_enabled"] = self.state.auto_debuff_enabled
        
        self.state.recovery_hp_spell = self.cb_hp_trig.get()
        self.state.recovery_mp_spell = self.cb_mp_trig.get()
        self.state.recovery_debuff_spell = self.cb_debuff_trig.get()
        
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(conf, f, indent=4)
        self.show_toast("💾 트리거 설정 저장 완료")

    def get_dynamic_font_size(self, text, max_width=34):
        # 텍스트 길이에 따라 폰트 크기 계산 (공간이 더 좁아짐에 따라 더 작은 폰트 허용)
        length = len(text)
        if length <= 2: return 10
        if length <= 4: return 8
        if length <= 6: return 7
        return 6

    def load_spells_db(self):
        if os.path.exists(self.spell_db_path):
            with open(self.spell_db_path, 'r', encoding='utf-8') as f:
                self.spell_db = json.load(f)
        else:
            self.spell_db = []
        self.sync_slots_from_db()

    def save_spells_db(self):
        with open(self.spell_db_path, 'w', encoding='utf-8') as f:
            json.dump(self.spell_db, f, indent=4, ensure_ascii=False)
        # GameState 동기화
        self.state.spells.clear()
        for s in self.spell_db:
            if s.get("slot") and s.get("use", True):
                self.state.spells.append(Skill(
                    name=s["name"],
                    target_type=s["target_type"],
                    hotkey=s["slot"] if len(s["slot"]) == 1 and s["slot"].isdigit() else None,
                    spell_char=s["slot"] if not (len(s["slot"]) == 1 and s["slot"].isdigit()) else None,
                    category=s.get("category", "공격")
                ))

    def sync_slots_from_db(self):
        # UI 업데이트 로직 (build 이후 호출 가능)
        if hasattr(self, 'slot_buttons'):
            # 초기화
            for info in self.slot_buttons.values():
                info["btn"].configure(text="", fg_color="#252A34", border_color="#4B5563")
            
            # DB에서 슬롯 매핑 확인
            for spell in self.spell_db:
                slot_key = spell.get("slot")
                if slot_key:
                    for pos, info in self.slot_buttons.items():
                        if info["key"] == slot_key:
                            name = spell["name"]
                            fs = self.get_dynamic_font_size(name)
                            color = self.get_category_color(spell.get("category"))
                            # 채워진 슬롯: 텍스트 추가, 다크톤 배경 및 유색 테두리
                            info["btn"].configure(text=name, font=("Inter", fs), fg_color="#1F2937", border_color=color)

    def get_category_color(self, cat):
        # 첨부 이미지의 느낌을 살린 선명한 색상 구성
        colors = {"공격": "#F43F5E", "회복": "#0EA5E9", "버프": "#8B5CF6", "디버프": "#F59E0B"}
        return colors.get(cat, "#475569")

    def refresh_storage_box(self):
        for widget in self.box_scroll.winfo_children():
            widget.destroy()
        
        for i, spell in enumerate(self.spell_db):
            r, c = divmod(i, 10) # 10열로 배열
            name = spell["name"]
            fs = self.get_dynamic_font_size(name)
            color = self.get_category_color(spell.get("category"))
            
            # 선택된 상태면 하이라이트(민트색 테두리)
            btn_color = "#1F2937" if self.picked_spell != name else "#111827"
            border_color = "#10B981" if self.picked_spell == name else color
            
            btn = ctk.CTkButton(self.box_scroll, text=name, width=34, height=34,
                               fg_color=btn_color, corner_radius=8,
                               border_width=2, border_color=border_color,
                               font=("Inter", fs),
                               command=lambda n=name: self.pick_spell(n))
            btn.grid(row=r, column=c, padx=2, pady=2)
            
            # 드래그 앤 드롭 바인딩
            btn.bind("<ButtonPress-1>", lambda e, n=name: self.on_drag_start(e, n))
            btn.bind("<B1-Motion>", self.on_drag_motion)
            btn.bind("<ButtonRelease-1>", self.on_drag_drop)

    def on_drag_start(self, event, name):
        self.dnd_spell = name
        if self.dnd_label:
            self.dnd_label.destroy()
            self.dnd_label = None
            
        fs = self.get_dynamic_font_size(name)
        # 플로팅 레이블 생성
        self.dnd_label = tk.Label(self.root, text=name, bg="#10B981", fg="#FFFFFF", 
                                 font=("Inter", fs+1, "bold"), relief="flat", padx=6, pady=3)
        
        x = event.x_root - self.root.winfo_rootx()
        y = event.y_root - self.root.winfo_rooty()
        self.dnd_label.place(x=x + 10, y=y + 10)

    def on_drag_motion(self, event):
        if self.dnd_label:
            x = event.x_root - self.root.winfo_rootx()
            y = event.y_root - self.root.winfo_rooty()
            self.dnd_label.place(x=x + 10, y=y + 10)

    def on_drag_drop(self, event):
        if self.dnd_label:
            self.dnd_label.destroy()
            self.dnd_label = None
            
        if not self.dnd_spell: return

        x, y = event.x_root, event.y_root
        
        # 드롭된 위치가 슬롯 내부인지 판별
        for (r, c), info in self.slot_buttons.items():
            btn = info["btn"]
            bx = btn.winfo_rootx()
            by = btn.winfo_rooty()
            bw = btn.winfo_width()
            bh = btn.winfo_height()
            
            if bx <= x <= bx + bw and by <= y <= by + bh:
                self.picked_spell = self.dnd_spell
                self.assign_to_slot(r, c)
                self.dnd_spell = None
                return
                
        self.dnd_spell = None

    def refresh_properties_list(self):
        for widget in self.prop_scroll.winfo_children():
            widget.destroy()
            
        for spell in self.spell_db:
            row = ctk.CTkFrame(self.prop_scroll, fg_color="transparent")
            row.pack(fill="x", pady=1)
            
            ctk.CTkLabel(row, text=spell["name"], width=120, anchor="w", font=("Inter", 11)).pack(side="left", padx=10)
            
            # Type Dropdown
            var_type = ctk.StringVar(value=spell["target_type"])
            cb_type = ctk.CTkOptionMenu(row, values=["즉발형", "대상선택형", "대상입력형"], 
                                      variable=var_type, width=100, font=("Inter", 10),
                                      command=lambda v, s=spell: self.update_spell_prop(s, "target_type", v))
            cb_type.pack(side="left", padx=5)
            
            # Category Dropdown
            var_cat = ctk.StringVar(value=spell.get("category", "공격"))
            cb_cat = ctk.CTkOptionMenu(row, values=["공격", "회복", "버프", "디버프"], 
                                     variable=var_cat, width=100, font=("Inter", 10),
                                     command=lambda v, s=spell: self.update_spell_prop(s, "category", v))
            cb_cat.pack(side="left", padx=5)

    def pick_spell(self, name):
        self.picked_spell = name
        self.show_toast(f"Selected: {name}")
        self.refresh_storage_box()

    def assign_to_slot(self, r, c):
        if not self.picked_spell:
            self.show_toast("Pick a spell first!", "red")
            return
        
        key = self.slot_buttons[(r, c)]["key"]
        
        # 이전 슬롯이 같은 키였다면 제거 (중복 방지)
        for s in self.spell_db:
            if s.get("slot") == key:
                s["slot"] = ""
            if s["name"] == self.picked_spell:
                s["slot"] = key
                s["use"] = True
        
        self.save_spells_db()
        self.sync_slots_from_db()
        self.picked_spell = None
        self.refresh_storage_box()
        self.show_toast("Assigned successfully")

    def clear_slot(self, r, c):
        key = self.slot_buttons[(r, c)]["key"]
        for s in self.spell_db:
            if s.get("slot") == key:
                s["slot"] = ""
        self.save_spells_db()
        self.sync_slots_from_db()
        self.show_toast("Slot cleared")

    def update_spell_prop(self, spell, key, value):
        spell[key] = value
        self.save_spells_db()
        self.refresh_storage_box() # 색상 등이 바뀔 수 있으므로 갱신
        self.show_toast(f"{spell['name']} updated")

    def open_visual_selector(self, target_name):
        global reader_thread
        if reader_thread is None:
            self.show_toast("[Err] VISION THREAD NOT READY", "#FF5555")
            return
        hwnd = find_game_window(WIN_KEY)
        if not hwnd:
            self.show_toast("[Err] WINDOW NOT FOUND", "#FF5555")
            return
            
        def save_result(sx, sy, dx, dy, *args):
            global reader_thread
            reader_thread.regions[target_name] = Region(sx, sy, dx, dy)
            conf = load_config()
            conf[target_name] = asdict(Region(sx, sy, dx, dy))
            save_config(conf)
            self.show_toast(f"# {target_name.upper()} AREA SAVED")
            # 즉시 프리뷰 갱신
            if hasattr(self, "update_calibration_preview"):
                self.update_calibration_preview()

        # 스크린샷 창 대신 실시간 오버레이 실행
        OverlaySelector(self.root, hwnd, save_result)

    def toggle_ocr_engine(self):
        self.state.ocr_enabled = not self.state.ocr_enabled
        status = "STARTED" if self.state.ocr_enabled else "STOPPED"
        color = "#10B981" if self.state.ocr_enabled else "#6366F1"
        self.btn_ocr_toggle.configure(text=f"OCR: {status}", fg_color=color)
        self.show_toast(f"OCR ENGINE {status}")

    def on_thresh_change(self, val):
        pass # Old global thresh slider removed

    def toggle_roi_indicator(self):
        if not hasattr(self, "indicator"):
            hwnd = find_game_window(WIN_KEY)
            if not hwnd:
                self.show_toast("❌ GAME NOT FOUND", "#FF5555")
                return
            self.indicator = ROIIndicator(self.root, hwnd)
            
        self.indicator.visible = not self.indicator.visible
        if self.indicator.visible:
            # 즉시 표시
            global reader_thread
            if reader_thread is not None and hasattr(reader_thread, "regions"):
                self.indicator.update_position(reader_thread.regions)
        else:
            self.indicator.withdraw() # 즉시 숨김 버그 수정

    def toggle_grid_overlay(self):
        if not hasattr(self, "grid_indicator"):
            hwnd = find_game_window(WIN_KEY)
            if not hwnd:
                self.show_toast("❌ GAME NOT FOUND", "#FF5555")
                return
            self.grid_indicator = GridIndicator(self.root, hwnd, self.state)
            
        self.state.grid_overlay_enabled = not self.state.grid_overlay_enabled
        self.grid_indicator.visible = self.state.grid_overlay_enabled
        
        if self.state.grid_overlay_enabled:
            self.btn_grid_toggle.configure(text="GRID ON", fg_color="#10B981")
            self.lbl_grid_offset.configure(text=f"OFFSET: ({self.grid_indicator.offset_x}, {self.grid_indicator.offset_y})")
            self.grid_indicator.update_position()
            self.show_toast("그리드 오버레이 활성화")
        else:
            self.btn_grid_toggle.configure(text="GRID OFF", fg_color="#FF5555")
            self.grid_indicator.withdraw()
            self.show_toast("그리드 오버레이 비활성화")

    def on_alpha_change(self, value):
        """불투명도 슬라이더 변경 시 호출"""
        if hasattr(self, "grid_indicator"):
            self.grid_indicator.set_alpha(float(value))

    def move_grid(self, dx, dy):
        """그리드 오프셋 1px 이동"""
        if not hasattr(self, "grid_indicator") or not self.grid_indicator.visible:
            self.show_toast("❌ 그리드가 활성화되지 않음", "#FF5555")
            return
        
        self.grid_indicator.offset_x += dx
        self.grid_indicator.offset_y += dy
        self.lbl_grid_offset.configure(text=f"OFFSET: ({self.grid_indicator.offset_x}, {self.grid_indicator.offset_y})")
        self.grid_indicator.update_position()
        
        # 오프셋을 config.json에 저장
        self.save_grid_offset()
    
    def save_grid_offset(self):
        """그리드 오프셋을 config.json에 저장"""
        if not hasattr(self, "grid_indicator"):
            return
        config_file = os.path.join(SCRIPT_DIR, "config.json")
        try:
            if os.path.exists(config_file):
                with open(config_file, 'r', encoding='utf-8') as f:
                    conf = json.load(f)
            else:
                conf = {}
            
            conf["grid_offset_x"] = self.grid_indicator.offset_x
            conf["grid_offset_y"] = self.grid_indicator.offset_y
            
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(conf, f, indent=4)
        except Exception:
            pass

    def adj_val(self, attr, delta):
        global reader_thread
        if reader_thread is None:
            self.show_toast("[Err] VISION THREAD NOT READY", "#FF5555")
            return
        target = self.tune_target.get()
        if target not in reader_thread.regions:
            self.show_toast("[Err] ROI TARGET INVALID", "#FF5555")
            return
        reg = reader_thread.regions[target]
        curr = getattr(reg, attr)
        setattr(reg, attr, curr + delta)
        
        # 저장
        conf = load_config()
        conf[target] = asdict(reg)
        save_config(conf)
        # 즉시 피드백
        if hasattr(self, "indicator") and self.indicator.visible:
            self.indicator.update_position(reader_thread.regions)

    def auto_connect_hardware(self):
        """프로그램 시작 시 아두이노 자동 연결 시도"""
        global hw
        try:
            # 사용 가능한 시리얼 포트 스캔
            comports = serial.tools.list_ports.comports()
            all_ports = [p.device for p in comports]
            if all_ports and hasattr(self, "cb_port"):
                self.cb_port.configure(values=all_ports)
                
            # USB나 특정 키워드가 포함된 포트만 필터링 (메인보드 가상 COM포트 제외)
            ports = []
            for p in comports:
                desc = (p.description or "").upper()
                hwid = (p.hwid or "").upper()
                if "USB" in desc or "USB" in hwid or "CH340" in desc or "CP210" in desc or "ARDUINO" in desc:
                    ports.append(p.device)
                    
            if not ports:
                print("[Hardware] 자동 연결 가능한 USB 시리얼 포트 없음 (가상 포트 제외됨)")
                return
            
            # 모든 호환 포트 순차적으로 시도
            for port in ports:
                if hw.connect(port):
                    self.btn_hw_connect.configure(text="DISCONNECT", fg_color="#EF4444")
                    self.lbl_hw_status.configure(text=f"🔌 HARDWARE: {port}", text_color="#10B981")
                    self.cb_port.set(port)
                    print(f"[Hardware] 자동 연결 성공: {port}")
                    self.show_toast("HARDWARE SECURELY CONNECTED")
                    return
                else:
                    print(f"[Hardware] 연결 실패: {port}, 다음 포트 시도...")
            
            print("[Hardware] 호환 포트 연결 실패, 수동 선택 필요")
        except Exception as e:
            print(f"[Hardware] 자동 연결 오류: {e}")

    def toggle_hardware(self):
        global hw
        if hw.ser and hw.ser.is_open:
            hw.disconnect()
            self.btn_hw_connect.configure(text="CONNECT ESP32", fg_color="#6366F1")
            self.lbl_hw_status.configure(text="🔌 HARDWARE: NONE", text_color="#FF5555")
            self.show_toast("HARDWARE DISCONNECTED", "#FFB800")
        else:
            port = self.cb_port.get()
            if hw.connect(port):
                self.btn_hw_connect.configure(text="DISCONNECT", fg_color="#EF4444")
                self.lbl_hw_status.configure(text=f"🔌 HARDWARE: {port}", text_color="#10B981")
                self.show_toast("HARDWARE SECURELY CONNECTED")
            else:
                self.show_toast("CONNECTION FAILED", "#FF5555")

    def set_role(self, role: str):
        """역할 설정 및 상태 동기화"""
        self.state.role = role
        self.show_toast(f"Role set to: {role}")

    def toggle_sentinel(self):
        """SentinelThread 활성화/비활성화 토글"""
        self.state.sentinel_enabled = not self.state.sentinel_enabled
        status = "ON" if self.state.sentinel_enabled else "OFF"
        color = "#10B981" if self.state.sentinel_enabled else "#343A40"
        
        # 버튼 텍스트와 색상 변경
        if hasattr(self, 'btn_sentinel'):
            self.btn_sentinel.configure(text=f"SENTINEL {status}", fg_color=color)
        
        self.show_toast(f"Sentinel: {status}")
        print(f"[GUI] Sentinel Toggle: {status}")

    def toggle_service(self):
        global action_thread
        action_thread.task_active = not action_thread.task_active
        
        # UI 업데이트는 메인 스레드에서 실행 (RuntimeError 방지)
        def _update_ui():
            status = "STOPPED" if not action_thread.task_active else "ACTIVE"
            btn_text = "RUN (F2)" if not action_thread.task_active else "STOP (F2)"
            btn_color = "#10B981" if not action_thread.task_active else "#EF4444"
            self.btn_macro.configure(text=btn_text, fg_color=btn_color)
            self.show_toast(f"SERVICE: {status}")
            
        try:
            self.root.after(0, _update_ui)
        except:
            pass

    def set_game_scale(self, scale: float):
        """게임 창 배율 설정 및 윈도우 크기 변경"""
        new_w, new_h = self.state.set_game_scale(scale)
        
        # 게임 창 크기 변경
        hwnd = self.state.hwnd
        if hwnd:
            try:
                win32gui.SetWindowPos(hwnd, 0, 0, 0, new_w, new_h, 
                                     win32con.SWP_NOMOVE | win32con.SWP_NOZORDER)
                self.show_toast(f"해상도 변경: {new_w}x{new_h} ({int(scale)}배)")
            except Exception as e:
                self.show_toast(f"해상도 변경 실패: {e}")
        
        # 해상도 표시 업데이트
        self.update_resolution_display()

    def update_resolution_display(self):
        """해상도 표시 업데이트"""
        try:
            # 실제 게임창 해상도 가져오기
            hwnd = find_game_window(WIN_KEY)
            if hwnd:
                rect = win32gui.GetWindowRect(hwnd)
                game_w = rect[2] - rect[0]
                game_h = rect[3] - rect[1]
                # 클라이언트 영역 크기 가져오기
                client_rect = win32gui.GetClientRect(hwnd)
                client_w = client_rect[2]
                client_h = client_rect[3]
                if hasattr(self, 'lbl_game_res') and self.lbl_game_res.winfo_exists():
                    self.lbl_game_res.configure(text=f"[Game] {client_w}x{client_h}")
            else:
                if hasattr(self, 'lbl_game_res') and self.lbl_game_res.winfo_exists():
                    self.lbl_game_res.configure(text="[Game] -")
        except Exception as e:
            print(f"[GUI] 해상도 업데이트 오류: {e}")

        try:
            gui_w, gui_h = self.root.winfo_width(), self.root.winfo_height()
            if hasattr(self, 'lbl_gui_res') and self.lbl_gui_res.winfo_exists():
                self.lbl_gui_res.configure(text=f"[GUI] {gui_w}x{gui_h}")
        except Exception as e:
            print(f"[GUI] GUI 해상도 업데이트 오류: {e}")

    def toggle_follow(self):
        """Follow 모드 토글 (F1)"""
        self.state.nav_follow_enabled = not self.state.nav_follow_enabled
        status = "ON" if self.state.nav_follow_enabled else "OFF"
        print(f"[Hotkey] Follow Mode {status}")
        self._update_status_display()

    def debug_ocr(self):
        global reader_thread
        if reader_thread is None:
            self.show_toast("[Err] VISION THREAD NOT READY", "#FF5555")
            return
            
        self.show_toast("DEBUG IMAGE SAVE DISABLED")

    def load_waypoints(self):
        """과거 호환성 유지를 위한 더미 혹은 통합 리로드 함수"""
        self.load_hunting_sequence()
        self.show_toast("WAYPOINTS LOADED (CSV/JSON)")

    def move_roi(self, dx, dy):
        global reader_thread
        if reader_thread is None:
            self.show_toast("[Err] VISION THREAD NOT READY", "#FF5555")
            return
        target = self.ocr_preview_target.get()
        if target not in reader_thread.regions:
            self.show_toast("[Err] ROI TARGET INVALID", "#FF5555")
            return
        reg = reader_thread.regions[target]
        reg.sx += dx; reg.dx += dx
        reg.sy += dy; reg.dy += dy
        conf = load_config()
        conf[target] = {"sx": reg.sx, "sy": reg.sy, "dx": reg.dx, "dy": reg.dy}
        save_config(conf)
        self.show_toast(f"[Loc] {target.upper()} MOVED 1PX")

    def toggle_route_f1_mode(self):
        """F1: 전체 웨이포인트 순차 이동 토글 (Thread-safe)"""
        # ── 중단(Stop) 로직: 핫키 스레드에서 즉시 처리 (속도 최우선) ──
        if getattr(self, '_f1_route_active', False):
            self._f1_route_active = False
            try:
                hw.panic_release()
            except:
                pass
            print("[Move] F1 경로 이동 중단 요청 (비상 정지 적용)")
            self.show_toast("경로 이동 중단", color="#EF4444")
            return

        # ── 시작(Start) 로직: Tkinter 위젯 접근을 위해 메인 스레드로 위임 ──
        try:
            self.root.after(0, self._start_route_f1_logic)
        except:
            pass

    def _start_route_f1_logic(self):
        """F1 이동 시작을 위한 UI 기반 초기화 및 스레드 런칭 (Main Thread에서 실행)"""
        items = self.seq_tree.get_children()
        if not items:
            self.show_toast("테이블에 항목 없음", color="#EF4444")
            return

        self._f1_route_active = True
        self.last_f1_pos = (0, 0)
        self.f1_stuck_timer = time.time()
        self.f1_stuck_count = 0
        print("[Move] F1 키 눌림 - 전체 경로 순차 이동 시작")

        def move_process():
            # 게임창 포커스 시도
            try:
                if self.hwnd and self.hwnd != 0:
                    win32gui.SetForegroundWindow(self.hwnd)
                    time.sleep(0.2)
            except Exception:
                pass

            wp_index = 0  # 항상 Entry(첫 번째)부터 시작

            while self._f1_route_active and self.state.running:
                # 현재 테이블 항목 조회 (UI 변경 대응)
                current_items = self.seq_tree.get_children()
                if not current_items:
                    break

                # 마지막까지 완료 → Entry부터 반복
                if wp_index >= len(current_items):
                    wp_index = 0
                    print("[Move] 전체 경로 완료 → Entry부터 반복")
                    self.show_toast("경로 완료! 처음부터 반복", color="#A855F7")
                    time.sleep(0.3)
                    continue

                item_id = current_items[wp_index]
                values = self.seq_tree.item(item_id, "values")
                row_type = values[0] if values else "?"

                # x, y 파싱 (비어있거나 0이면 스킵)
                try:
                    tx = int(values[1]) if len(values) > 1 and values[1] else 0
                    ty = int(values[2]) if len(values) > 2 and values[2] else 0
                except (ValueError, TypeError):
                    tx, ty = 0, 0

                if tx == 0 and ty == 0:
                    print(f"[Move] [{wp_index+1}/{len(current_items)}] {row_type} 좌표 없음 - 스킵")
                    wp_index += 1
                    continue

                print(f"[Move] [{wp_index+1}/{len(current_items)}] {row_type} → ({tx}, {ty}) 이동 시작")
                self.show_toast(f"[{wp_index+1}/{len(current_items)}] {row_type} → ({tx},{ty})")

                # stuck 감지 초기화 (waypoint 전환마다 리셋)
                self.last_f1_pos = (0, 0)
                self.f1_stuck_timer = time.time()
                self.f1_stuck_count = 0

                # ── 해당 waypoint 도달까지 루프 ──
                waypoint_reached = False
                while self._f1_route_active and self.state.running:
                    data = self.state.get_all()
                    cx, cy = data.get("x", 0), data.get("y", 0)

                    # 도달 판정 (좌표 차이 2 미만, 인식 실패(0) 제외)
                    if cx > 0 and cy > 0:
                        if abs(tx - cx) < 2 and abs(ty - cy) < 2:
                            print(f"[Move] [{wp_index+1}] {row_type} 도달 완료: ({tx}, {ty})")
                            self.show_toast(f"도달! {row_type} ({tx},{ty})", color="#10B981")
                            wp_index += 1
                            waypoint_reached = True
                            break

                    # ── Stuck 감지 & 회피 기동 ──
                    if self.state.nav_avoid_enabled:
                        now = time.time()
                        if (cx, cy) != self.last_f1_pos:
                            self.last_f1_pos = (cx, cy)
                            self.f1_stuck_timer = now
                            self.f1_stuck_count = 0
                        elif now - self.f1_stuck_timer > 0.7:  # 1.5초 -> 0.7초 (판단 속도 2배 상향)
                            self.f1_stuck_count += 1
                            self.f1_stuck_timer = now
                            print(f"[F1-Stuck] 좌표 변화 없음 ({self.f1_stuck_count}회 연속)")
                            if self.f1_stuck_count >= 2:
                                print("[F1-Stuck] 막힘 감지! 회피 기동 실행.")
                                self._f1_escape_stuck()
                                continue

                    # ── 방향 결정: 거리 먼 축 우선 + 랜덤 ──
                    dx_v, dy_v = tx - cx, ty - cy

                    if abs(dx_v) > abs(dy_v):
                        axis_priority = ["x", "y"]
                    elif abs(dy_v) > abs(dx_v):
                        axis_priority = ["y", "x"]
                    else:
                        axis_priority = random.choice([["x", "y"], ["y", "x"]])

                    direction = None
                    for axis in axis_priority:
                        if axis == "x" and abs(dx_v) > 1:
                            direction = "right" if dx_v > 0 else "left"
                            break
                        elif axis == "y" and abs(dy_v) > 1:
                            direction = "down" if dy_v > 0 else "up"
                            break

                    if direction is None:
                        humanized_sleep(TIMING_CONFIG["nav_loop"])
                        continue

                    # ── 하드웨어 이동 명령 ──
                    print(f"[Move] 방향키: {direction}")
                    try:
                        hw.hold_move(direction, "move_hold")
                    except Exception as e:
                        print(f"[Move] 이동 명령 실패: {e}")
                        self._f1_route_active = False
                        break

                    humanized_sleep(TIMING_CONFIG["nav_loop"])

                if not waypoint_reached and not self._f1_route_active:
                    break  # 중단 요청됨

            self._f1_route_active = False
            
            # 인터럽트 실패(무한 입력 방지): 스레드 종료 전 잔여 하드웨어 눌림을 아예 강제 해제합니다.
            try:
                hw.panic_release()
            except Exception:
                pass
                
            print("[Move] F1 경로 이동 스레드 종료")

        # UI 스레드 차단 방지를 위해 별도 스레드에서 실행
        threading.Thread(target=move_process, daemon=True).start()

    def _f1_escape_stuck(self):
        """F1 이동 전용 회피 기동 (svc_executor logic 복제)"""
        print("[Warn] [F1-STUCK] 회피 기동 중...")
        for _ in range(random.randint(2, 3)):
            # 횡이동 랜덤
            side_dir = random.choice(["left", "right", "up", "down"])
            hw.hold_move(side_dir, "stuck_side_hold")
            humanized_sleep(TIMING_CONFIG["key_gap"])
        self.f1_stuck_count = 0
        self.f1_stuck_timer = time.time()

    def update_fast_labels(self):
        """숫자 라벨 빠른 업데이트 (16ms = 60 FPS)"""
        try:
            if not self.root.winfo_exists(): return
        except tk.TclError:
            return  # 윈도우 파괴 중이면 즉시 종료

        # Tab 렌더링 스위치 확인
        if not self.tab_enabled.get("dash", True):
            # 탭 비활성화 시 타이머만 재등록하고 리턴
            try:
                if self.root.winfo_exists():
                    self.root.after(16, self.update_fast_labels)
            except tk.TclError:
                pass
            return

        perf_start = time.perf_counter()

        try:
            # 로컬 캐싱으로 속도 최적화
            state = self.state

            # GameState 속성 직접 읽기 (큐 오버헤드 제거)
            # 비전 엔진이 이미 GameState 속성을 업데이트하므로 직접 읽기가 더 빠름
            hp_val = state.hp_str or str(state.hp)
            mp_val = state.mp_str or str(state.mp)
            exp_val = state.exp_str or str(state.exp)
            money_val = state.money_str or str(state.money)
            x_val = state.x_str or f"{state.x:04d}"
            y_val = state.y_str or f"{state.y:04d}"
            char_grid = state.char_grid

            if hp_val != self.last_values.get("dash_hp"):
                self.lbl_hp.configure(text=hp_val)
                self.last_values["dash_hp"] = hp_val
            if mp_val != self.last_values.get("dash_mp"):
                self.lbl_mp.configure(text=mp_val)
                self.last_values["dash_mp"] = mp_val
            if exp_val != self.last_values.get("dash_exp"):
                self.lbl_exp.configure(text=exp_val)
                self.last_values["dash_exp"] = exp_val
            if money_val != self.last_values.get("dash_money"):
                self.lbl_money.configure(text=money_val)
                self.last_values["dash_money"] = money_val

            xy_text = f"{x_val},{y_val}"
            if xy_text != self.last_values.get("dash_xy"):
                self.lbl_xy.configure(text=xy_text)
                self.last_values["dash_xy"] = xy_text
            if hasattr(self, 'lbl_char_grid'):
                char_grid_text = f"GRID: {char_grid[0]},{char_grid[1]}"
                if char_grid_text != self.last_values.get("dash_char_grid"):
                    self.lbl_char_grid.configure(text=char_grid_text)
                    self.last_values["dash_char_grid"] = char_grid_text
            
            # 성능 측정
            self.frame_count += 1
            perf_end = time.perf_counter()
            perf_ms = (perf_end - perf_start) * 1000
            fps = 1000 / perf_ms if perf_ms > 0 else 0

            now = time.perf_counter()
            if now - self.last_values.get("dash_perf_time", 0.0) >= 0.25:
                ocr_fps = getattr(state, "numeric_fps", 0.0)
                cap_age = getattr(state, "capture_age_ms", 0.0)
                self.lbl_perf.configure(
                    text=f"[Perf] UI {perf_ms:.1f}ms {fps:.0f}FPS | OCR {ocr_fps:.0f}FPS | Age {cap_age:.0f}ms"
                )
                self.last_values["dash_perf_time"] = now
                    
        except Exception as e:
            print(f"[GUI] update_fast_labels 오류: {e}")
        finally:
            # Tk 메인루프는 1ms 폴링보다 16ms 주기가 훨씬 안정적이고 부하가 적다.
            try:
                if self.root.winfo_exists():
                    self.root.after(16, self.update_fast_labels)
            except tk.TclError:
                pass

    def update_slow_ui(self):
        """보조 UI 업데이트 (30초 주기, 상태 변경 시 즉시 반영)"""
        try:
            if not self.root.winfo_exists(): return
        except tk.TclError:
            return

        # Tab 렌더링 스위치 확인
        if not self.tab_enabled.get("logic", True) and not self.tab_enabled.get("nav", True):
            # 탭 비활성화 시 타이머만 재등록하고 리턴
            try:
                if self.root.winfo_exists():
                    self.root.after(30000, self.update_slow_ui)
            except tk.TclError:
                pass
            return

        try:
            # [NAV] 네비게이션 상태 표시
            if self.tab_enabled.get("nav", True):
                try:
                    nav_status = "ACTIVE" if self.state.nav_enabled else "IDLE"
                    if nav_status != self.last_values.get('nav_status'):
                        if hasattr(self, 'lbl_nav_status'):
                            self.lbl_nav_status.configure(text=f"NAV: {nav_status}")
                        self.last_values['nav_status'] = nav_status
                except Exception:
                    pass
            
            # [HARDWARE] 아두이노 연결 상태 표시 (터미널 출력으로 대체)
            try:
                global hw
                if hw.ser and hw.ser.is_open:
                    hw_status = "READY"
                else:
                    hw_status = "DISCONNECTED"
                
                if hw_status != self.last_values.get('hw_status'):
                    print(f"[Hardware] 상태: {hw_status}")
                    self.last_values['hw_status'] = hw_status
            except Exception:
                pass
            
            # 가이드 레이어 동기화
            try:
                if hasattr(self, "indicator") and self.indicator.visible:
                    global reader_thread
                    if reader_thread is not None:
                        self.indicator.update_position(reader_thread.regions)
            except Exception:
                pass

            # 스킬 플래그 동기화 (GUI -> State)
            if self.tab_enabled.get("logic", True):
                try:
                    use_heal = self.sw_heal.get()
                    use_buff = self.sw_buff.get()
                    use_debuff = self.sw_debuff.get()
                    use_attack = self.sw_attack.get()
                    use_smart_ai = self.sw_ai.get()

                    # 상태 변경 시에만 업데이트
                    if use_heal != self.last_values.get('use_heal'):
                        self.state.use_heal = use_heal
                        self.last_values['use_heal'] = use_heal
                    if use_buff != self.last_values.get('use_buff'):
                        self.state.use_buff = use_buff
                        self.last_values['use_buff'] = use_buff
                    if use_debuff != self.last_values.get('use_debuff'):
                        self.state.use_debuff = use_debuff
                        self.last_values['use_debuff'] = use_debuff
                    if use_attack != self.last_values.get('use_attack'):
                        self.state.use_attack = use_attack
                        self.last_values['use_attack'] = use_attack
                    if use_smart_ai != self.last_values.get('use_smart_ai'):
                        self.state.use_smart_ai = use_smart_ai
                        self.last_values['use_smart_ai'] = use_smart_ai
                except Exception:
                    pass
        except Exception as e:
            print(f"[GUI] update_slow_ui 오류: {e}")
        finally:
            # 타이머 재등록 (30초 = 30000ms)
            try:
                if self.root.winfo_exists():
                    self.root.after(30000, self.update_slow_ui)
            except tk.TclError:
                pass

    def on_close(self):
        print("[System] 종료 시퀀스 시작. 모든 동작 중단 및 스레드 종료 요청...")
        self.state.running = False
        
        # 아두이노 키보드/마우스 입력 해제 및 버퍼 초기화를 위한 RELEASE 전송
        try:
            hw.panic_release()
            time.sleep(0.05) # 명령 전송 대기 타임
            hw.disconnect()
        except Exception:
            pass
            
        try:
            self.close_calibration_popup()
            self.root.update_idletasks()
            save_gui_state(self.root.geometry(), self.ui_scale, self.calibration_geometry)
        except Exception:
            pass
        self.root.destroy()
        
        # 데몬 스레드들의 잔여 연산 차단 및 즉시 프로세스 종료
        os._exit(0)

    def panic_shutdown(self):
        """F3 비상 정지: 모든 지연된 스레드와 저장을 무시하고 즉각 하드웨어 릴리즈 후 강제 종료"""
        print("\n!!! [PANIC] F3 비상 정지 활성화 !!!")
        try:
            hw.panic_release()
        except:
            pass
        os._exit(0)

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        # 전역 핫키 설정
        try:
            keyboard.add_hotkey("f1", self.toggle_route_f1_mode)
            keyboard.add_hotkey("f2", self.toggle_service)
            keyboard.add_hotkey("f3", self.panic_shutdown)  # 스레드 생성 오버헤드 제거하여 즉시 호출
        except Exception as e:
            print(f"[Warn] Hotkey Registration Failed: {e}")
        
        # 타이머는 __init__에서 이미 시작됨 (update_fast_labels, update_slow_ui)
        self.root.update()
        self.root.mainloop()

# 전역 스레드 참조 및 엔진 변수
action_thread = None
reader_thread = None
nav_thread = None
# hw는 svc_kernel에서 임포트됨

def main():
    # 0. config.json 유효성 검사 (안전 빌드)
    print("[Config] config.json 유효성 검사 중...")
    config_file = os.path.join(os.path.dirname(__file__), "config.json")
    
    if not os.path.exists(config_file):
        messagebox.showerror("오류", f"config.json 파일이 없습니다:\n{config_file}")
        return
    
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        play_area = config.get("play_area", {})
        sx = play_area.get("sx", 0)
        sy = play_area.get("sy", 0)
        dx = play_area.get("dx", 0)
        dy = play_area.get("dy", 0)
        
        # play_area 좌표 유효성 검사
        if sx == 0 or sy == 0 or dx == 0 or dy == 0:
            messagebox.showerror(
                "오류",
                f"config.json의 play_area 좌표가 유효하지 않습니다.\n"
                f"sx: {sx}, sy: {sy}, dx: {dx}, dy: {dy}\n"
                f"모든 좌표는 0이 아니어야 합니다."
            )
            return
        
        if sx >= dx or sy >= dy:
            messagebox.showerror(
                "오류",
                f"config.json의 play_area 좌표가 잘못되었습니다.\n"
                f"sx({sx}) >= dx({dx}) 또는 sy({sy}) >= dy({dy})\n"
                f"시작 좌표는 끝 좌표보다 작아야 합니다."
            )
            return
        
        print(f"[Config] play_area 유효성 확인: ({sx}, {sy}) -> ({dx}, {dy})")
        
    except Exception as e:
        messagebox.showerror("오류", f"config.json 로드 실패:\n{e}")
        return
    
    # 1. 스텔스 환경 체크 (Anti-Cheat 대응) - [Bypass for Test 73]
    print("[Stealth] 환경 안전성 검사 중... (Bypass Enabled)")
    # stealth_results = StealthChecker.run_all_checks()
    
    # if stealth_results['debugger_detected']:
    #     print("[Stealth] [!] 디버거 탐지됨 - 실행 중단")
    #     return
    
    # if stealth_results['vm_detected']:
    #     print("[Stealth] [!] 가상머신 환경 탐지됨 - 실행 중단")
    #     return
    
    # if not stealth_results['process_integrity']:
    #     print("[Stealth] [!] 프로세스 무결성 위반 - 실행 중단")
    #     return
    
    # if not stealth_results['external_modules']:
    #     print("[Stealth] [!] 의심스러운 외부 모듈 탐지 - 실행 중단")
    #     return
    
    print("[Stealth] [OK] 환경 안전성 확인 완료")

    # 게임창을 0,0 좌표로 이동 및 크기 고정 (1920x1080 타겟)
    hwnd = find_game_window(WIN_KEY)
    if hwnd:
        import win32con
        # 윈도우 크기를 강제로 1920x1080로 고정
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, 1920, 1080, win32con.SWP_SHOWWINDOW)
        print(f"[Window] 게임창을 (0, 0) 좌표로 이동하고 크기를 1920x1080으로 고정했습니다.")
    else:
        print(f"[Warn] 게임창을 찾을 수 없습니다. (키워드: {WIN_KEY})")

    global action_thread, reader_thread
    state = GameState()
    state.hwnd = hwnd  # GameState에 핸들 저장
    hw.set_state(state)  # DevInterface에 GameState 연결 (포커스 체크용)
    
    # 각 워커 스레드 초기화 (bootstrap.py 기능 통합)
    config_file = os.path.join(SCRIPT_DIR, "config.json")
    spell_db_file = os.path.join(SCRIPT_DIR, "spells_config.json")

    # 1. 고속 캡처 스레드 가동 (모든 비전 스레드의 소스)
    capture_svc = CaptureSvc(state)
    capture_svc.start()

    try:
        reader_thread = MonitorSvc(state, config_file)  # PatternMatcher 기반 OCR
        print(f"[Worker] MonitorSvc initialized: {reader_thread is not None}")
    except Exception as e:
        print(f"[Worker] MonitorSvc initialization failed: {e}")
        import traceback
        traceback.print_exc()
        return
    
    numeric_scanner = NumericFieldScanner(state, reader_thread.matcher, config_file, reader_thread)  # 고속 전용 스레드
    sentinel_thread = SentinelThread(state, reader_thread.matcher) # 몬스터/아이템 탐지 전용
    action_thread = LogicSvc(state)  # FSM 로직
    nav_thread = RouteSvc(state)  # 네비게이션
    network = NetworkThread(state, is_server=IS_SERVER)
    logger = LoggerThread(state)

    # GUI 생성 (hwnd 전달)
    gui = AppView(state, hwnd=hwnd)

    # GUI 생성 후 grid_indicator 전달
    if hasattr(gui, 'grid_indicator'):
        sentinel_thread.grid_indicator = gui.grid_indicator

    # 모든 스레드 시작
    print(f"[Worker] About to start reader_thread...")
    reader_thread.start()
    print(f"[Worker] reader_thread started")
    numeric_scanner.start()  # 고속 숫자 스캔 시작
    sentinel_thread.start()
    action_thread.start()
    nav_thread.start()
    network.start()
    logger.start()

    print("[OK] System started. (Production Mode)")
    
    # GUI 실행
    gui.run()

    print("[Stop] System closed.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"\n[FATAL ERROR] {e}")
        traceback.print_exc()
        input("Press Enter to exit...")
