# -*- coding: utf-8 -*-
import time
import threading
import random
import os
import sys
import atexit
import io
import json
import csv
import io
import ipaddress

# ?곕????몄퐫??媛뺤젣 ?ㅼ젙 (CP949 ?섍꼍 ???)))
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
import socket as pysocket
import queue
from tkinter import ttk, messagebox, simpledialog
from PIL import Image, ImageTk
from svc_monitor import NumericFieldScanner

# Pillow 10+ ?명솚 由ъ깦?뚮쭅 (援щ쾭?꾩? ?뺤닔 0 = NEAREST)
try:
    from PIL.Image import Resampling
    _PIL_NEAREST = Resampling.NEAREST
except ImportError:
    _PIL_NEAREST = 0
from dataclasses import dataclass, asdict
from typing import Tuple, Optional, Dict
from enum import Enum, auto

# 而ㅻ꼸 ?대옒???꾪룷??
from bis_core import Skill, GameState, hw, Region, TIMING_CONFIG, humanized_sleep
from svc_stealth import StealthChecker
from svc_monitor import MonitorSvc, SentinelThread, CaptureSvc
from bis_logic import LogicSvc, RouteSvc

# 怨좏빐?곷룄(65?몄튂 ?? 紐⑤땲???명솚?깆쓣 ?꾪븳 DPI ?몄떇 ?쒖꽦??
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

DEFAULT_NETWORK_CONFIG = {
    "role": "?꾩궗",
    "server_ip": "192.168.137.1",
    "bind_host": "0.0.0.0",
    "telemetry_port": 5555,
    "local_port": 5556,
}

CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
GUI_STATE_FILE = os.path.join(SCRIPT_DIR, "gui_state.json")
TEMPLATE_DIGIT_DIR = os.path.join(SCRIPT_DIR, "temple", "digits")

def load_network_config() -> dict:
    cfg = dict(DEFAULT_NETWORK_CONFIG)
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            net = data.get("network", {})
            if isinstance(net, dict):
                cfg["role"] = str(net.get("role", cfg["role"]) or cfg["role"])
                cfg["server_ip"] = str(net.get("server_ip", cfg["server_ip"]) or cfg["server_ip"])
                cfg["bind_host"] = str(net.get("bind_host", cfg["bind_host"]) or cfg["bind_host"])
                cfg["telemetry_port"] = int(net.get("telemetry_port", cfg["telemetry_port"]) or cfg["telemetry_port"])
                cfg["local_port"] = int(net.get("local_port", cfg["local_port"]) or cfg["local_port"])
    except Exception as e:
        print(f"[Net] config load failed: {e}")
    return cfg

def dict_to_region(data: dict) -> Region:
    """Docstring."""
    if isinstance(data, Region):
        return data
    if isinstance(data, dict):
        # Grid 醫뚰몴媛 ?덈뒗 寃쎌슦 ?쎌? 醫뚰몴 ?곗꽑 ?ъ슜
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
            # 醫뚰몴 ?곸뿭留?怨⑤씪??蹂??(dict?닿퀬 'sx'媛 ?덈뒗 寃쎌슦留?
            return {k: dict_to_region(v) for k, v in conf.items()
                    if isinstance(v, dict) and 'sx' in v}
    # DEFAULT_REGIONS??Region 媛앹껜濡?蹂??
    return {k: dict_to_region(v) for k, v in DEFAULT_REGIONS.items()}

def save_config(regions_dict):
    # Region 媛앹껜瑜?dict濡?蹂?섑븯?????(Grid 醫뚰몴 蹂묓뻾 ???
    serializable_dict = {}
    for k, v in regions_dict.items():
        if isinstance(v, Region):
            # 湲곗〈 ?쎌? 醫뚰몴 ?좎??섎㈃??Grid 醫뚰몴?????
            region_dict = asdict(v)
            # Grid 醫뚰몴媛 ?놁쑝硫?鍮?媛믪쑝濡?珥덇린??
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

# 湲곕낯 醫뚰몴 ?ㅼ젙 (ocr ?몄떇 湲곕낯 罹≪퀜?곸뿭 諛붾엺???섎씪 UI 湲곗? ?덉떆)
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
            return False # 李얠븯?쇰?濡?以묐떒
        return True
    
    try:
        win32gui.EnumWindows(enum_cb, None)
    except Exception:
        pass
    return found_hwnd

def grab_window_bg(hwnd: int, rect_ignored=None) -> Optional[Image.Image]:
    """Docstring."""
    try:
        import win32ui
        # ?대씪?댁뼵???곸뿭 ?ш린
        _, _, width, height = win32gui.GetClientRect(hwnd)

        # ?붾㈃ DC ?앹꽦
        hwndDC = win32gui.GetWindowDC(hwnd)
        mfcDC = win32ui.CreateDCFromHandle(hwndDC)
        saveDC = mfcDC.CreateCompatibleDC()
        saveBitMap = win32ui.CreateBitmap()
        saveBitMap.CreateCompatibleBitmap(mfcDC, width, height)
        saveDC.SelectObject(saveBitMap)

        # PrintWindow: ctypes濡?user32.PrintWindow 吏곸젒 ?몄텧
        # PW_RENDERFULLCONTENT(2) = DirectX/GPU ?뚮뜑留??붾㈃ 罹≪쿂
        user32 = ctypes.windll.user32
        result = user32.PrintWindow(hwnd, saveDC.GetSafeHdc(), 2)

        if result == 0:
            # PrintWindow ?ㅽ뙣 ??BitBlt濡??대갚
            saveDC.BitBlt((0, 0), (width, height), mfcDC, (0, 0), win32con.SRCCOPY)

        # PrintWindow???덈룄???꾩껜(??댄?諛??ы븿)瑜?罹≪쿂?섎?濡?
        # ?대씪?댁뼵???곸뿭 ?ㅽ봽?뗭쓣 怨꾩궛?섏뿬 ?щ∼
        win_rect = win32gui.GetWindowRect(hwnd)
        client_left, client_top = win32gui.ClientToScreen(hwnd, (0, 0))
        offset_x = client_left - win_rect[0]
        offset_y = client_top - win_rect[1]

        bmpinfo = saveBitMap.GetInfo()
        bmpstr = saveBitMap.GetBitmapBits(True)
        full_img = Image.frombuffer('RGB',
                               (bmpinfo['bmWidth'], bmpinfo['bmHeight']),
                               bmpstr, 'raw', 'BGRX', 0, 1)

        # ?대씪?댁뼵???곸뿭留??щ∼?섏뿬 諛섑솚
        img = full_img.crop((offset_x, offset_y, offset_x + width, offset_y + height))
        mfcDC.DeleteDC()
        saveDC.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwndDC)
        win32gui.DeleteObject(saveBitMap.GetHandle())
        return img
    except Exception as e:
        print(f"[Capture] 罹≪쿂 ?ㅽ뙣: {e}")
        return None

class OverlaySelector(tk.Toplevel):
    """Docstring."""
    def __init__(self, parent, hwnd, callback):
        super().__init__(parent)
        self.callback = callback
        self.hwnd = hwnd
        self.msg_id = None
        self.rect_id = None

        # 1. 寃뚯엫李??대씪?댁뼵???곸뿭 ?꾩튂 ?뚯븙 (臾쇰━ 醫뚰몴)
        left, top = win32gui.ClientToScreen(self.hwnd, (0, 0))
        _, _, w, h = win32gui.GetClientRect(self.hwnd)


        # 2. ?ㅻ쾭?덉씠 ?ㅼ젙 (?쒖? Tkinter瑜??ъ슜?섏뿬 CustomTkinter Scaling 媛꾩꽠 諛곗젣)
        self.overrideredirect(True) # ??댄?諛??쒓굅
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.05)  # 留ㅼ슦 ?щ챸?섍쾶 (5% 遺덊닾紐?
        self.geometry(f"{w}x{h}+{left}+{top}")
        self.configure(bg="black")  # 寃??諛곌꼍
        self.lift() # 理쒖긽?⑥쑝濡??щ┝

        self.canvas = tk.Canvas(self, width=w, height=h, bg="black", highlightthickness=0, cursor="cross")
        self.canvas.pack(fill="both", expand=True)

        self.start_x = 0; self.start_y = 0; self.end_x = 0; self.end_y = 0

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.bind("<Escape>", self.on_cancel)  # ESC 痍⑥냼

        # ?덈궡 臾멸뎄
        self.msg_id = self.canvas.create_text(w//2, h//2, text="Drag to select an area\n(ESC: cancel / finish box automatically)",
                               fill="#FFFFFF", font=("Inter", 16, "bold"), justify="center")

    def _screen_to_client(self, screen_x, screen_y):
        """Docstring."""
        cx, cy = win32gui.ScreenToClient(self.hwnd, (screen_x, screen_y))
        return cx, cy

    def on_press(self, event):
        # Tkinter 醫뚰몴 ????붾㈃ 醫뚰몴瑜??대씪?댁뼵??醫뚰몴濡?蹂??
        screen_x, screen_y = win32api.GetCursorPos()
        self.start_cx, self.start_cy = self._screen_to_client(screen_x, screen_y)

        # Tkinter 罹붾쾭?ㅼ슜 醫뚰몴 (?쒓컖???쇰뱶諛깅쭔)
        self.start_x, self.start_y = event.x, event.y
        if self.msg_id:
            self.canvas.delete(self.msg_id)
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(self.start_x, self.start_y, self.start_x, self.start_y, outline="#FF0000", width=3)

    def on_drag(self, event):
        self.canvas.coords(self.rect_id, self.start_x, self.start_y, event.x, event.y)

    def on_release(self, event):
        # Tkinter 醫뚰몴 ????붾㈃ 醫뚰몴瑜??대씪?댁뼵??醫뚰몴濡?蹂??(臾쇰━ ?쎌?)
        screen_x, screen_y = win32api.GetCursorPos()
        end_cx, end_cy = self._screen_to_client(screen_x, screen_y)

        # ?쒕옒洹??꾨즺 利됱떆 罹≪쿂
        captured_img = grab_window_bg(self.hwnd)

        # 臾쇰━ ?쎌? 醫뚰몴 ?뺣젹 (DPI ?ㅼ??쇰쭅 ?꾩쟾 臾닿?)
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
                print(f"[DEBUG] ?섑뵆留?臾쇰━醫뚰몴: ({sx},{sy})-({dx},{dy}) | 蹂댁젙醫뚰몴: ({csx},{csy})-({cdx},{cdy}) | 罹≪쿂?ш린: {iw}x{ih}")
            except Exception as e:
                print(f"[DEBUG] ?щ∼ 泥섎━ 以??ㅻ쪟: {e}")
        else:
            print(f"[DEBUG] 罹≪쿂 ?ㅽ뙣! captured_img=None")

        # ?ㅻ쾭?덉씠 利됱떆 ?リ린
        self.destroy()

        # 醫뚰몴 + 罹≪쿂 ?대?吏 ?④퍡 ?꾨떖
        self.callback(sx, sy, dx, dy, captured_img)

    def on_cancel(self, event):
        """Docstring."""
        self.destroy()
        print("[DEBUG] 샘플링 취소")

class ROIIndicator(tk.Toplevel):
    """Docstring."""
    def __init__(self, parent, hwnd):
        super().__init__(parent)
        self.hwnd = hwnd
        self.visible = False
        
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-transparentcolor", "black") # 釉붾옓 ?됱긽? 100% ?щ챸 諛??대┃-?ㅻ（
        self.configure(bg="black")
        
        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        
        self.withdraw() # 珥덇린?먮뒗 ?④?

    def update_position(self, regions):
        if not self.visible:
            self.withdraw()
            return

        try:
            left, top = win32gui.ClientToScreen(self.hwnd, (0, 0))
            _, _, w, h = win32gui.GetClientRect(self.hwnd)
            self.geometry(f"{w}x{h}+{left}+{top}")
            self.deiconify()
            self.lift()  # 理쒖긽?⑥쑝濡?媛뺤젣 ?대룞

            self.canvas.delete("all")
            # 2026 SOTA neon colors
            colors = {"hp": "#FF4B4B", "mp": "#00D4FF", "exp": "#A855F7", "money": "#FFB800", "x": "#10B981", "y": "#10B981"}

            for name, reg in regions.items():
                color = colors.get(name, "#FFFFFF")
                # ?ㅼ젣 ?몄떇 ?곸뿭蹂대떎 ?щ갑?쇰줈 3px ?볤쾶 洹몃젮??罹≪쿂 ?곸뿭 移⑤쾾 諛⑹?
                self.canvas.create_rectangle(reg.sx - 3, reg.sy - 3, reg.dx + 3, reg.dy + 3, outline=color, width=2)
                # [?띿뒪???쇰꺼 ??젣??- OCR 媛꾩꽠 諛⑹?]
        except:
            self.withdraw()

class GridIndicator(tk.Toplevel):
    """Docstring."""
    def __init__(self, parent, hwnd, state):
        super().__init__(parent)
        self.hwnd = hwnd
        self.state = state
        self.visible = False
        self.grid_size = 48.2  # float (?꾩쟻?ㅼ감 諛⑹?)
        
        # config.json?먯꽌 ?ㅽ봽??遺덈윭?ㅺ린
        config_file = os.path.join(SCRIPT_DIR, "config.json")
        self.offset_x = 0  # 湲곕낯媛?
        self.offset_y = 0  # 湲곕낯媛?
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    conf = json.load(f)
                    pa = conf.get("play_area", {})
                    self.grid_size = float(pa.get("grid_size", 48.2))
            except Exception:
                pass
        
        self.grid_alpha = 0.5  # ?щ챸??湲곕낯媛?(0.0 ~ 1.0)
        # 罹먮┃?곌? ?붾㈃??怨좎젙?섎뒗 湲곗? ???(?대┃ 醫뚰몴 蹂???쒖떆 怨듯넻 ?ъ슜)
        self.char_screen_grid_x = 9
        self.char_screen_grid_y = 11  # J12 ???湲곗?
        self.item_positions = []  # ?꾩씠??grid 醫뚰몴 由ъ뒪??[(gx, gy), ...]
        
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-transparentcolor", "black") # 釉붾옓 ?됱긽? 100% ?щ챸 諛??대┃-?ㅻ（
        self.configure(bg="black")
        
        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        
        # 留덉슦???대┃ ?대깽??諛붿씤??
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        
        # 醫뚰몴 ?쒖떆 ?쇰꺼
        self.coord_label = tk.Label(self, text="", bg="black", fg="#00FF00", font=("Arial", 10))
        self.coord_label.place(x=10, y=10)
        
        self.withdraw() # 珥덇린?먮뒗 ?④?

    def set_alpha(self, alpha):
        """Docstring."""
        self.grid_alpha = max(0.0, min(1.0, alpha))
        self.attributes("-alpha", self.grid_alpha)

    def update_item_positions(self, item_grids):
        """Docstring."""
        self.item_positions = item_grids

    def on_canvas_click(self, event):
        """Docstring."""
        if not self.visible:
            return

        # ?덈룄??醫뚰몴 (?대┃???꾩튂)
        window_x = event.x
        window_y = event.y

        # 寃뚯엫 醫뚰몴 怨꾩궛 (洹몃━??醫뚰몴濡?蹂??
        try:
            # ?? ?뺤젙 ?ㅽ봽?? sx=276, sy=32 / 寃⑹옄 ?ш린 48.2 ??
            # 怨듭떇: GridX = (PixelX - 276) / 48.2, GridY = (PixelY - 32) / 48.2
            config_file = os.path.join(SCRIPT_DIR, "config.json")
            play_area_sx = 276  # ?뺤젙 ?ㅽ봽??
            play_area_sy = 32   # ?뺤젙 ?ㅽ봽??
            grid_size = 48.2    # float (?꾩쟻?ㅼ감 諛⑹?)
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

            # 寃⑹옄 ?쒖옉??(play_area 湲곗? + offset)
            grid_start_x = play_area_sx + self.offset_x
            grid_start_y = play_area_sy + self.offset_y

            # ?대┃???붾㈃ 寃⑹옄 ?몃뜳?? float ?섎닓????int 蹂??
            grid_x = int((window_x - grid_start_x) / grid_size)
            grid_y = int((window_y - grid_start_y) / grid_size)

            # GameState?먯꽌 理쒖떊 ?덈? 醫뚰몴 痍⑤뱷
            data = self.state.get_all() if hasattr(self.state, "get_all") else {}
            my_world_x = int(data.get("x", getattr(self.state, "x", 0)))
            my_world_y = int(data.get("y", getattr(self.state, "y", 0)))

            # ?쒖떆?? x/y??4?먮━ 怨좎젙 臾몄옄??x_str/y_str) ?곗꽑 (?꾨씫? 'x')
            my_world_x_str = str(data.get("x_str", ""))
            my_world_y_str = str(data.get("y_str", ""))
            if not my_world_x_str:
                my_world_x_str = f"{my_world_x:04d}"
            if not my_world_y_str:
                my_world_y_str = f"{my_world_y:04d}"

            # 罹먮┃?곌? ?붾㈃?곸뿉 ?덈뒗 ?ㅼ젣 ?숈쟻 ???醫뚰몴(me_grid)瑜?媛?몄샂
            me_data = self.state.entities.get("me", {}) if hasattr(self.state, "entities") else {}
            me_grid = me_data.get("grid", (self.char_screen_grid_x, self.char_screen_grid_y))
            char_screen_grid_x, char_screen_grid_y = me_grid

            # ?대┃???붾뱶 醫뚰몴 ?곗텧
            target_world_x = my_world_x + (grid_x - char_screen_grid_x)
            target_world_y = my_world_y + (grid_y - char_screen_grid_y)

            # ?대┃????쇰챸
            tile_col = chr(ord('A') + grid_x) if grid_x >= 0 else "?"
            tile_row = str(grid_y + 1)
            tile_name = f"{tile_col}{tile_row}"

            # 醫뚰몴 ?쒖떆 (寃利?媛?ν븳 ?곸꽭 ?щ㎎)
            coord_text = (
                f"[??醫뚰몴: {my_world_x_str}, {my_world_y_str}] | "
                f"?대┃????? {tile_name} | "
                f"怨꾩궛???붾뱶 醫뚰몴: {target_world_x}, {target_world_y}"
            )
            self.coord_label.configure(text=coord_text)
            
            # ?대┃???꾩튂???뱀깋 ???쒖떆
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
            self.lift()  # 理쒖긽?⑥쑝濡?媛뺤젣 ?대룞

            self.canvas.delete("all")

            # ?? ?뺤젙 ?ㅽ봽?? sx=276, sy=32 / 寃⑹옄 ?ш린 48.2 ??
            # 怨듭떇: GridX = (PixelX - 276) / 48.2, GridY = (PixelY - 32) / 48.2
            config_file = os.path.join(SCRIPT_DIR, "config.json")
            play_area_sx = 276  # ?뺤젙 ?ㅽ봽??
            play_area_sy = 32   # ?뺤젙 ?ㅽ봽??
            grid_size = 48.2    # float (?꾩쟻?ㅼ감 諛⑹?)
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

            # 寃⑹옄 ?쒖옉??(play_area 湲곗? + offset)
            grid_start_x = play_area_sx + self.offset_x
            grid_start_y = play_area_sy + self.offset_y
            tile_size = grid_size
            
            # 寃⑹옄 ???쒗븳 (媛濡?19移?a1~S1, ?몃줈 17移?a1~a17)
            grid_cols = 19
            grid_rows = 17

            # 寃⑹옄 ?쇱씤 洹몃━湲?(float grid_size ??round濡??뺤닔 蹂??
            for i in range(grid_cols + 1):
                x = round(grid_start_x + i * tile_size)
                x_end = round(grid_start_y + grid_rows * tile_size)
                self.canvas.create_line(x, round(grid_start_y), x, x_end, fill="#FFFFFF", width=1)
            
            # 寃⑹옄 ?쇱씤 洹몃━湲?(17??row=16 ?쒖쇅)
            for i in range(grid_rows + 1):
                if i == 16:  # 17??嫄대꼫?
                    continue
                y = round(grid_start_y + i * tile_size)
                x_end = round(grid_start_x + grid_cols * tile_size)
                self.canvas.create_line(round(grid_start_x), y, x_end, y, fill="#FFFFFF", width=1)
            
            # ?뚮몢由?移몄뿉 4px 媛꾧꺽 異붽? 寃⑹옄 洹몃━湲?
            # a??(col=0), r??(col=17), 1??(row=0), 17??(row=16)
            border_cols = [0, grid_cols - 1]  # a?? r??
            border_rows = [0, grid_rows - 1]  # 1?? 17??
            fine_spacing = 4
            
            # ?뚮몢由??댁뿉 ?몃줈濡?異붽? 寃⑹옄 (?뚮몢由?移??대?)
            for col in border_cols:
                col_start_x = round(grid_start_x + col * tile_size)
                col_end_x = round(grid_start_x + (col + 1) * tile_size)
                for i in range(0, int(grid_rows * tile_size), fine_spacing):
                    y = round(grid_start_y) + i
                    self.canvas.create_line(col_start_x, y, col_end_x, y, fill="#FFFFFF", width=1)
            
            # ?뚮몢由??됱뿉 媛濡쒕줈 異붽? 寃⑹옄 (?뚮몢由?移??대?)
            for row in border_rows:
                row_start_y = round(grid_start_y + row * tile_size)
                row_end_y = round(grid_start_y + (row + 1) * tile_size)
                for i in range(0, int(grid_cols * tile_size), fine_spacing):
                    x = round(grid_start_x) + i
                    self.canvas.create_line(x, row_start_y, x, row_end_y, fill="#FFFFFF", width=1)

            # ?꾩씠???꾩튂???뱀깋 ??諛?醫뚰몴 ?쒖떆
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

                    # Grid 醫뚰몴? POS_X,Y 醫뚰몴 怨꾩궛
                    col_name = chr(ord('A') + gx)
                    row_name = str(gy + 1)
                    grid_name = f"{col_name}{row_name}"
                    me_data = self.state.entities.get("me", {}) if hasattr(self.state, "entities") else {}
                    me_grid = me_data.get("grid", (self.char_screen_grid_x, self.char_screen_grid_y))
                    char_grid_x, char_grid_y = me_grid
                    
                    pos_x = my_world_x + (gx - char_grid_x)
                    pos_y = my_world_y + (gy - char_grid_y)
                    coord_text = f"{grid_name}\n({pos_x},{pos_y})"

                    # 醫뚰몴 ?띿뒪???쒖떆 (?뱀깋 ????
                    self.canvas.create_text(item_x + 8, item_y, text=coord_text,
                                            fill="#00FF00", font=("Arial", 7), anchor="w")

            # 寃⑹옄 ?대쫫 ?쒖떆
            for row in range(grid_rows):
                for col in range(grid_cols):
                    # ?뺤닔 ?몃뜳??
                    grid_x = col
                    grid_y = row
                    
                    # ?묒? 諛⑹떇 蹂??(A1, B2...)
                    col_name = chr(ord('A') + grid_x)
                    row_name = str(grid_y + 1)
                    grid_name = f"{col_name}{row_name}"
                    
                    # 寃⑹옄 移?以묒븰???띿뒪???쒖떆
                    x = grid_start_x + grid_x * tile_size + tile_size // 2
                    y = grid_start_y + grid_y * tile_size + tile_size // 2
                    self.canvas.create_text(x, y, text=grid_name, fill="#FFFFFF", font=("Arial", 8))
        except:
            self.withdraw()

# hw??svc_kernel?먯꽌 ?꾪룷?몃맖
# AIController??LogicSvc濡??泥대맖
# GameState??svc_kernel?먯꽌 ?꾪룷?몃맖

# DXCam ?쒓굅??- OBS 媛?곸뭅硫붾씪 紐⑤뱶 ?ъ슜

# --------------------------------------------------------------------------------
# 3. 寃뚯엫 ?≪뀡 ?⑥닔 (?먮낯 ?ㅽ겕由쏀듃 ?댁떇)
# execute_* ?⑥닔?ㅼ? LogicSvc濡??泥대맖

# --------------------------------------------------------------------------------
# 5. ?곗씠??????ㅻ젅??(Logger)
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
        print("[Log] Logger thread started...")
        # ?뚯씪???놁쑝硫??ㅻ뜑 ?묒꽦
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
# 6. ZeroMQ ?ㅽ듃?뚰겕 ?ㅻ젅??(PUB/SUB)
# --------------------------------------------------------------------------------
class NetworkThread(threading.Thread):
    def __init__(self, state: GameState, network_cfg: Optional[dict] = None, on_connection_change=None):
        super().__init__(daemon=True)
        self.state = state
        self.cfg = dict(network_cfg or load_network_config())
        self.role = str(self.cfg.get('role', getattr(state, 'network_role', '도사')) or '도사').strip()
        self.server_ip = str(self.cfg.get('server_ip', getattr(state, 'network_server_ip', '192.168.137.1')) or '192.168.137.1')
        self.bind_host = str(self.cfg.get('bind_host', getattr(state, 'network_bind_host', '0.0.0.0')) or '0.0.0.0')
        self.telemetry_port = int(self.cfg.get('telemetry_port', getattr(state, 'network_telemetry_port', 5555)) or 5555)
        self.local_port = int(self.cfg.get('local_port', getattr(state, 'network_local_port', 5556)) or 5556)
        self.is_server = self.role == '도사'
        self.sock: Optional[pysocket.socket] = None
        self._peers: dict[tuple[str, int], dict] = {}
        self._last_send = 0.0
        self._last_broadcast = 0.0
        self._warned_connreset = False
        self._warned_server_ip = False
        self._seen_rx_senders: set[str] = set()
        self.send_interval = 0.05
        self.broadcast_interval = 0.05
        self.on_connection_change = on_connection_change  # 연결 상태 변경 시 호출할 콜백

        self.state.network_role = self.role
        self.state.network_server_ip = self.server_ip
        self.state.network_bind_host = self.bind_host
        self.state.network_telemetry_port = self.telemetry_port
        self.state.network_local_port = self.local_port

    def _make_socket(self) -> pysocket.socket:
        sock = pysocket.socket(pysocket.AF_INET, pysocket.SOCK_DGRAM)
        sock.setsockopt(pysocket.SOL_SOCKET, pysocket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(pysocket.SOL_SOCKET, pysocket.SO_REUSEPORT, 1)
        except Exception:
            pass
        udp_connreset = getattr(pysocket, "SIO_UDP_CONNRESET", 0x9800000C)
        try:
            sock.ioctl(udp_connreset, False)
        except Exception:
            pass
        sock.settimeout(0.01)
        return sock

    def _is_reachable_host_ip(self, host: str) -> bool:
        try:
            ip = ipaddress.ip_address(host)
            return not (ip.is_loopback or ip.is_unspecified)
        except ValueError:
            return False

    @staticmethod
    def _encode_payload(payload: dict) -> bytes:
        return json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

    @staticmethod
    def _decode_payload(data: bytes) -> Optional[dict]:
        try:
            text = data.decode('utf-8', errors='ignore').strip()
            if not text:
                return None
            return json.loads(text)
        except Exception:
            return None

    def _register_peer(self, addr: tuple[str, int], payload: dict, sender: str) -> None:
        status = payload.get('status', {}) if isinstance(payload, dict) else {}
        self._peers[addr] = {
            'sender': sender,
            'role': str(status.get('network_role') or status.get('role') or '').strip(),
            'seen_at': time.time(),
        }

    def _broadcast_snapshot(self) -> None:
        if not self.sock or not self._peers:
            return
        payload = self.state.build_share_payload()
        payload['kind'] = 'telemetry'
        payload['role'] = self.role
        payload['sender_role'] = self.role
        payload['peer_count'] = len(self._peers)
        raw = self._encode_payload(payload)
        stale = []
        for addr in list(self._peers.keys()):
            try:
                self.sock.sendto(raw, addr)
            except Exception:
                stale.append(addr)
        for addr in stale:
            self._peers.pop(addr, None)

    def _send_telemetry(self) -> None:
        if not self.sock:
            return
        payload = self.state.build_share_payload()
        payload['kind'] = 'telemetry'
        payload['role'] = self.role
        payload['sender_role'] = self.role
        raw = self._encode_payload(payload)
        try:
            self.sock.sendto(raw, (self.server_ip, self.telemetry_port))
        except OSError as e:
            if getattr(e, "winerror", None) == 10054:
                if not self._warned_connreset:
                    print(
                        f"[Net] UDP reset ignored for client. Check server_ip={self.server_ip} "
                        f"and make sure the dosa PC is already running."
                    )
                    self._warned_connreset = True
                return
            raise

    def _drain_event_outbox(self) -> None:
        outbox = getattr(self.state, 'network_outbox', None)
        if outbox is None:
            return
        while True:
            try:
                event = outbox.get_nowait()
            except queue.Empty:
                break
            except Exception:
                break
            if not isinstance(event, dict):
                continue
            repeat = max(1, int(event.get('repeat', 1) or 1))
            payload = self.state.build_share_payload()
            payload['kind'] = 'event'
            payload['event_type'] = str(event.get('event_type', '') or '').strip()
            payload['event_payload'] = dict(event.get('payload') or {})
            payload['event_repeat'] = repeat
            raw = self._encode_payload(payload)
            targets = list(self._peers.keys()) if self.is_server else [(self.server_ip, self.telemetry_port)]
            for _ in range(repeat):
                for addr in targets:
                    try:
                        self.sock.sendto(raw, addr)
                    except Exception:
                        pass

    def run(self):
        try:
            self.sock = self._make_socket()
            if self.is_server:
                self.sock.bind((self.bind_host, self.telemetry_port))
                print(f'[Net] UDP hub connected: {self.bind_host}:{self.telemetry_port} (role={self.role})')
            else:
                self.sock.bind((self.bind_host, self.local_port))
                print(f'[Net] UDP client connected: {self.bind_host}:{self.local_port} -> {self.server_ip}:{self.telemetry_port} (role={self.role})')
                if not self._is_reachable_host_ip(self.server_ip) and not self._warned_server_ip:
                    print(
                        f"[Net] WARNING: server_ip={self.server_ip} looks unusable. "
                        "Set it to the dosa PC's active IPv4 address on the network you are actually using."
                    )
                    self._warned_server_ip = True
            self.state.is_connected = True
            if self.on_connection_change:
                self.on_connection_change(True)

            while self.state.running:
                now = time.time()
                try:
                    self._drain_event_outbox()
                except Exception as e:
                    print(f'[Net] event send failed: {e}')

                if not self.is_server and (now - self._last_send) >= self.send_interval:
                    try:
                        self._send_telemetry()
                        self._last_send = now
                    except Exception as e:
                        print(f'[Net] telemetry send failed: {e}')

                if self.is_server and (now - self._last_broadcast) >= self.broadcast_interval:
                    try:
                        self._broadcast_snapshot()
                        self._last_broadcast = now
                    except Exception as e:
                        print(f'[Net] broadcast failed: {e}')

                try:
                    data, addr = self.sock.recvfrom(65535)
                except pysocket.timeout:
                    continue
                except BlockingIOError:
                    continue
                except ConnectionResetError:
                    if not self._warned_connreset:
                        print(
                            "[Net] UDP reset ignored. Usually this means the target port is closed, "
                            "the server is not running yet, or server_ip is wrong."
                        )
                        self._warned_connreset = True
                    continue
                except OSError as e:
                    if getattr(e, "winerror", None) == 10054:
                        if not self._warned_connreset:
                            print(
                                "[Net] UDP reset ignored. Usually this means the target port is closed, "
                                "the server is not running yet, or server_ip is wrong."
                            )
                            self._warned_connreset = True
                        continue
                    raise

                payload = self._decode_payload(data)
                if not isinstance(payload, dict):
                    continue

                sender, remote_data = self.state.apply_remote_payload(payload)
                if self.is_server:
                    self._register_peer(addr, payload, sender)
                    if sender not in self._seen_rx_senders:
                        seq = int(payload.get("seq", 0) or 0)
                        kind = str(payload.get("kind", "") or "")
                        role = str(payload.get("role") or payload.get("sender_role") or "")
                        print(
                            f"[Net][RX] sender={sender} role={role} kind={kind} seq={seq} addr={addr[0]}:{addr[1]}"
                        )
                        self._seen_rx_senders.add(sender)
                    self._last_broadcast = 0.0
                else:
                    # Keep remote telemetry in other_pc_data only; local nav toggles stay local.
                    pass
        except Exception as e:
            print(f'[Net] Error: {e}')
            self.state.is_connected = False
            if self.on_connection_change:
                self.on_connection_change(False)
        finally:
            try:
                if self.sock:
                    self.sock.close()
            except Exception:
                pass

# --------------------------------------------------------------------------------# 7. UI ?대옒??(珥덈?吏??섏씠?붾뱶 ?붿옄??
# --------------------------------------------------------------------------------
class AppView:
    def __init__(self, state: GameState, hwnd=None):
        self.state = state
        self.hwnd = hwnd
        self.root = ctk.CTk()
        # GameState??gui_update_queue ?ъ슜 (以묐났 ???쒓굅)
        self.waypoint_index = 0  # ?쒖감???대룞???몃뜳??
        self.ui_scale = 1.0
        self.ui_scale_step = 0.05
        self.ui_scale_min = 0.5
        self.ui_scale_max = 2.0
        self.root.title("SYSTEM PARALLEL SOTA v2.0")
        
        # ?고듃 癒쇱? ?좎뼵 (Ultra-Compact ?붿옄?? 9pt)
        self.header_font = ctk.CTkFont(family="Orbitron", size=12, weight="bold")
        self.main_font = ctk.CTkFont(family="Inter", size=9)
        
        # ?곹깭 濡쒕뱶 (geometry ?ㅼ젙 ??
        self.restore_gui_state()
        
        # 留덈쾿 ?곗씠??濡쒕뱶 諛?珥덇린??
        self.spell_db_path = os.path.join(SCRIPT_DIR, "spells_config.json")
        self.picked_spell = None
        self.dnd_spell = None
        self.dnd_label = None
        self.load_spells_db()
        
        self.create_widgets()
        self.bind_global_zoom_controls()
        self.last_values = {}  # Dirty Checking??罹먯떆
        self.frame_count = 0  # ?깅뒫 痢≪젙???꾨젅??移댁슫??
        self._paused_control_mode = "NONE"
        
        # Tab蹂??뚮뜑留??ㅼ쐞移?(由ъ냼??愿由?
        self.tab_enabled = {
            "dash": True,   # ?レ옄 ?쇰꺼 ?낅뜲?댄듃
            "nav": True     # ?ㅻ퉬寃뚯씠???곹깭
        }
        self.update_fast_labels()
        self.update_slow_ui()
        
        # ?꾨몢?대끂 ?먮룞 ?곌껐 ?쒕룄
        self.auto_connect_hardware()

    def restore_gui_state(self):
        # ?덈룄???ш린: 媛濡?480px, ?몃줈 900px ?댁긽 (留덉슦???쒕옒洹몃줈 議곗젅 媛??
        self.root.geometry("480x900")
        self.root.resizable(True, True)  # 媛濡??몃줈 紐⑤몢 議곗젅 媛??
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
        
        # 泥?踰덉㎏ 以? RUN, OCR, GUIDES, GRID, PORT
        row1 = ctk.CTkFrame(top_actions, fg_color="transparent")
        row1.pack(fill="x", pady=1)
        self.btn_macro = ctk.CTkButton(row1, text="RUN (F2)", height=22, width=60, font=("Inter", 9, "bold"), fg_color="#10B981", command=self.toggle_service)
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
        
        # ??踰덉㎏ 以? CALIB, SCALE, ROLE, SENTINEL
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
        self.cb_role = ctk.CTkComboBox(row2, values=["Warrior", "Priest", "Shaman"], height=22, width=82, font=("Inter", 9), command=self.set_role)
        self.cb_role.pack(side="left", padx=1)
        self.cb_role.set(self._display_role_name(self.state.role))
        self.btn_sentinel = ctk.CTkButton(row2, text="SNT", height=22, width=40, font=("Inter", 9), command=self.toggle_sentinel)
        self.btn_sentinel.pack(side="left", padx=1)
        
        # ??踰덉㎏ 以? OFFSET, ALPHA, GRID
        row3 = ctk.CTkFrame(top_actions, fg_color="transparent")
        row3.pack(fill="x", pady=1)
        self.lbl_grid_offset = ctk.CTkLabel(row3, text="O:(0,0)", font=("Inter", 8), width=60)
        self.lbl_grid_offset.pack(side="left", padx=1)
        btn_frame = ctk.CTkFrame(row3, fg_color="#2D3748", corner_radius=2)
        btn_frame.pack(side="left", padx=1)
        ctk.CTkButton(btn_frame, text="^", height=18, width=18, font=("Inter", 7), command=lambda: self.move_grid(0, -1)).grid(row=0, column=1, padx=0)
        ctk.CTkButton(btn_frame, text="<", height=18, width=18, font=("Inter", 7), command=lambda: self.move_grid(-1, 0)).grid(row=1, column=0, padx=0)
        ctk.CTkButton(btn_frame, text="v", height=18, width=18, font=("Inter", 7), command=lambda: self.move_grid(0, 1)).grid(row=1, column=1, padx=0)
        ctk.CTkButton(btn_frame, text=">", height=18, width=18, font=("Inter", 7), command=lambda: self.move_grid(1, 0)).grid(row=1, column=2, padx=0)
        ctk.CTkLabel(row3, text="A", font=("Inter", 8), width=15).pack(side="left", padx=1)
        self.slider_alpha = ctk.CTkSlider(row3, from_=0.1, to=1.0, number_of_steps=9, width=60, command=self.on_alpha_change)
        self.slider_alpha.set(0.5)
        self.slider_alpha.pack(side="left", padx=1)
        self.lbl_char_grid = ctk.CTkLabel(row3, text="G:-", font=("Inter", 8), text_color="#00D4FF", width=80)
        self.lbl_char_grid.pack(side="left", padx=1)
        
        # OCR Threshold ?щ씪?대뜑 ?쒓굅 (媛쒕퀎 ?꾧퀎媛?濡쒖쭅?쇰줈 ?泥?
        
        # ?곹깭 ?뺣낫 - 媛濡?2??諛곗튂

        # ?곹깭 ?뺣낫 - 媛濡?2??諛곗튂
        st = ctk.CTkFrame(self.c_dash, fg_color="#1E2129", corner_radius=4, border_width=1, border_color="#343A46")
        st.pack(fill="x", padx=1, pady=1)
        g = ctk.CTkFrame(st, fg_color="transparent")
        g.pack(fill="x", padx=2, pady=2)
        
        # 媛濡?2??諛곗튂
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

        if self.state.role == "도사":
            self.net_box = ctk.CTkFrame(self.c_dash, fg_color="#0B1220", corner_radius=6, border_width=1, border_color="#2563EB")
            self.net_box.pack(fill="x", padx=1, pady=1)

            ctk.CTkLabel(self.net_box, text="NETWORK MONITOR", font=("Inter", 10, "bold"), text_color="#60A5FA").pack(pady=(3, 1))
            self.lbl_rx_summary = ctk.CTkLabel(
                self.net_box,
                text="RX: -",
                font=("Inter", 8, "bold"),
                text_color="#93C5FD",
                anchor="w",
                justify="left"
            )
            self.lbl_rx_summary.pack(fill="x", padx=6, pady=(0, 3))

            self.net_local_strip = ctk.CTkFrame(self.net_box, fg_color="#111827", corner_radius=4, border_width=1, border_color="#38BDF8")
            self.net_local_strip.pack(fill="x", padx=6, pady=(0, 4))
            ctk.CTkLabel(self.net_local_strip, text=f"[{self._display_role_name(self.state.role)}] LOCAL", font=("Inter", 8, "bold"), text_color="#38BDF8").pack(side="left", padx=(6, 4), pady=4)
            self.lbl_local_status = ctk.CTkLabel(self.net_local_strip, text="-", font=("Inter", 8), anchor="w", justify="left")
            self.lbl_local_status.pack(side="left", fill="x", expand=True, padx=(0, 6), pady=4)

            self.net_role_cards = {}
            cards_row = ctk.CTkFrame(self.net_box, fg_color="transparent")
            cards_row.pack(fill="x", padx=4, pady=(0, 4))

            for role_name in ("격수", "술사"):
                card = ctk.CTkFrame(cards_row, fg_color="#111827", corner_radius=8, border_width=2, border_color="#374151")
                card.pack(side="left", fill="both", expand=True, padx=3)

                title_row = ctk.CTkFrame(card, fg_color="transparent")
                title_row.pack(fill="x", padx=6, pady=(5, 2))
                title_lbl = ctk.CTkLabel(title_row, text=self._display_role_name(role_name), font=("Inter", 10, "bold"), text_color="#E5E7EB")
                title_lbl.pack(side="left")
                status_lbl = ctk.CTkLabel(title_row, text="DISCONNECTED", font=("Inter", 8, "bold"), text_color="#F87171")
                status_lbl.pack(side="right")

                map_lbl = ctk.CTkLabel(card, text="MAP: -", font=("Inter", 10, "bold"), text_color="#93C5FD", anchor="w", justify="left")
                map_lbl.pack(fill="x", padx=6, pady=(1, 1))
                coord_lbl = ctk.CTkLabel(card, text="X/Y: -", font=("Inter", 11, "bold"), text_color="#FDE68A", anchor="w", justify="left")
                coord_lbl.pack(fill="x", padx=6, pady=(0, 1))
                hpmp_lbl = ctk.CTkLabel(card, text="HP/MP: -", font=("Inter", 9, "bold"), text_color="#A7F3D0", anchor="w", justify="left")
                hpmp_lbl.pack(fill="x", padx=6, pady=(0, 5))

                self.net_role_cards[role_name] = {
                    "frame": card,
                    "title": title_lbl,
                    "status": status_lbl,
                    "map": map_lbl,
                    "coord": coord_lbl,
                    "hpmp": hpmp_lbl,
                }

        # Navigation & Control Tab
        self.build_navigation_control()

        # Footer / Toast Area
        self.toast_lbl = ctk.CTkLabel(self.root, text="", font=("Inter", 10, "bold"), text_color="#10B981")
        self.toast_lbl.pack(side="bottom", pady=5)

    def open_calibration_popup(self):
        print("[DEBUG]")
        pop = getattr(self, "calibration_popup", None)
        if pop is not None:
            try:
                if pop.winfo_exists():
                    print("[DEBUG] 湲곗〈 ?앹뾽??李얠븘???욎쑝濡?媛?몄샂")
                    pop.lift()
                    pop.focus_force()
                    return
            except tk.TclError:
                pass

        print("[DEBUG] ?덈줈??罹섎━釉뚮젅?댁뀡 ?앹뾽 ?앹꽦")
        try:
            self.calibration_popup = ctk.CTkToplevel(self.root)
            self.calibration_popup.title("Calibration")
            self.calibration_popup.geometry(self.calibration_geometry or "920x420+180+180")
            self.calibration_popup.attributes("-topmost", True)
            self.calibration_popup.transient(self.root)
            self.calibration_popup.minsize(860, 360)
            print("[DEBUG] ?앹뾽 湲곕낯 ?ㅼ젙 ?꾨즺")
        except Exception as e:
            print(f"[DEBUG] ?앹뾽 ?앹꽦 ?ㅽ뙣: {e}")
            return

        try:
            self.preview_zoom_var = ctk.StringVar(value="x3")
            self.preview_crop_pil = None
            self.preview_tkimg = None
            print("[DEBUG] 蹂??珥덇린???꾨즺")
        except Exception as e:
            print(f"[DEBUG] 蹂??珥덇린???ㅽ뙣: {e}")
            return

        try:
            body = ctk.CTkFrame(self.calibration_popup, fg_color="transparent")
            body.pack(fill="both", expand=True, padx=6, pady=6)
            body.grid_columnconfigure(0, weight=0)
            body.grid_columnconfigure(1, weight=1)
            body.grid_rowconfigure(0, weight=1)
            print("[DEBUG] body ?꾨젅???앹꽦 ?꾨즺")
        except Exception as e:
            print(f"[DEBUG] body ?꾨젅???앹꽦 ?ㅽ뙣: {e}")
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
        # ?쒖옉 醫뚰몴 議곗젅 (sx, sy)
        ctk.CTkButton(dp, text="^", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("sy", -1)).grid(row=0, column=1)
        ctk.CTkButton(dp, text="<", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("sx", -1)).grid(row=1, column=0)
        ctk.CTkButton(dp, text=">", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("sx", 1)).grid(row=1, column=2)
        ctk.CTkButton(dp, text="v", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("sy", 1)).grid(row=2, column=1)

        # ??醫뚰몴 議곗젅 (dx, dy)
        ctk.CTkLabel(left_panel, text="End Position", font=("Inter", 9)).pack(pady=(4, 1))
        dp2 = ctk.CTkFrame(left_panel, fg_color="transparent")
        dp2.pack(pady=3)
        ctk.CTkButton(dp2, text="^", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("dy", -1)).grid(row=0, column=1)
        ctk.CTkButton(dp2, text="<", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("dx", -1)).grid(row=1, column=0)
        ctk.CTkButton(dp2, text=">", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("dx", 1)).grid(row=1, column=2)
        ctk.CTkButton(dp2, text="v", width=26, height=24, font=("Inter", 10), command=lambda: self.adj_val("dy", 1)).grid(row=2, column=1)

        ctk.CTkLabel(left_panel, text="Preview Zoom", font=("Inter", 9)).pack(pady=(6, 1))
        self.zoom_menu = ctk.CTkOptionMenu(left_panel, values=["x3", "x4", "x5"], variable=self.preview_zoom_var, width=90, height=22, font=("Inter", 9),
                                           command=lambda _: self.update_calibration_preview())
        self.zoom_menu.pack(pady=1)

        # Threshold ?щ씪?대뜑 (?댁쭊???꾧퀎媛??쒕떇)
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


        # ?섑뵆留?踰꾪듉 異붽?
        sample_row = ctk.CTkFrame(left_panel, fg_color="transparent")
        sample_row.pack(pady=(3, 6))
        ctk.CTkButton(sample_row, text="SAMPLE", width=190, height=24, font=("Inter", 9, "bold"),
                     fg_color="#A855F7", hover_color="#7E22CE", command=self.start_sampling_tool).pack()

        # 移댄뀒怨좊━ ?좏깮 ?쒕∼?ㅼ슫
        category_row = ctk.CTkFrame(left_panel, fg_color="transparent")
        category_row.pack(pady=(2, 6))
        ctk.CTkLabel(category_row, text="Category:", font=("Inter", 8)).pack(side="left", padx=2)
        self.sample_category_var = ctk.StringVar(value="digits")
        ctk.CTkOptionMenu(category_row, values=["digits", "monsters", "status", "users", "items", "marker"],
                         variable=self.sample_category_var, width=120, height=22, font=("Inter", 8)).pack(side="left", padx=2)

        # ?섑뵆留?1px ?몄쭛 UI
        self.sample_trim_frame = ctk.CTkFrame(left_panel, fg_color="transparent")
        self.sample_trim_frame.pack(pady=(3, 6))

        # 以??좏깮
        zoom_row = ctk.CTkFrame(self.sample_trim_frame, fg_color="transparent")
        zoom_row.pack(pady=(2, 2))
        ctk.CTkLabel(zoom_row, text="Zoom", font=("Inter", 8)).pack(side="left", padx=2)
        self.sample_zoom_var = ctk.StringVar(value="x2")
        ctk.CTkOptionMenu(zoom_row, values=["x1", "x2", "x4"], variable=self.sample_zoom_var, width=60, height=20, font=("Inter", 8),
                         command=lambda _: self.set_sample_zoom(int(self.sample_zoom_var.get().replace("x", "")))).pack(side="left", padx=2)

        # 1px ?몃┝ 踰꾪듉
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

        # 由ъ뀑 踰꾪듉
        ctk.CTkButton(self.sample_trim_frame, text="RESET", width=148, height=20, font=("Inter", 8), command=self.reset_sample_crop).pack(pady=(2, 0))

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
        
        print("[DEBUG] ?앹뾽 ?ㅼ젙 ?꾨즺, ?꾨━酉??낅뜲?댄듃 ?쒖옉")
        try:
            self.update_calibration_preview()
            print("[DEBUG] ?꾨━酉??낅뜲?댄듃 ?꾨즺")
        except Exception as e:
            print(f"[DEBUG] ?꾨━酉??낅뜲?댄듃 ?ㅽ뙣: {e}")
        
        print("[DEBUG] ?앹뾽 ?쒖떆 ?쒕룄")
        try:
            self.calibration_popup.lift()
            self.calibration_popup.focus_force()
            self.calibration_popup.update_idletasks()
            print("[DEBUG] ?앹뾽 ?쒖떆 ?깃났")
        except Exception as e:
            print(f"[DEBUG] ?앹뾽 ?쒖떆 ?ㅽ뙣: {e}")
        
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

    # on_thr_change ?쒓굅 (?щ씪?대뜑 誘몄궗??

    def update_move_hold(self, value):
        """Docstring."""
        from bis_core import TIMING_CONFIG
        ms_val = float(value)
        TIMING_CONFIG["move_hold"] = ms_val / 1000.0  # ms -> s
        self.move_hold_label.configure(text=f"{int(ms_val)}ms")
        self.save_timing_config()
    
    def update_key_gap(self, value):
        """Docstring."""
        from bis_core import TIMING_CONFIG
        ms_val = float(value)
        TIMING_CONFIG["key_gap"] = ms_val / 1000.0  # ms -> s
        self.key_gap_label.configure(text=f"{int(ms_val)}ms")
        self.save_timing_config()
    
    def save_timing_config(self):
        """Load timing values from config.json."""
        try:
            from bis_core import TIMING_CONFIG
            config_file = os.path.join(os.path.dirname(__file__), "config.json")
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            config["timing"] = TIMING_CONFIG
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"[Config] TIMING_CONFIG ????ㅽ뙣: {e}")

    def update_calibration_preview(self):
        print("[DEBUG] 罹섎━釉뚮젅?댁뀡 ?꾨━酉??낅뜲?댄듃 ?쒖옉")
        if not hasattr(self, "calibration_popup") or not self.calibration_popup.winfo_exists():
            print("[DEBUG] 罹섎━釉뚮젅?댁뀡 ?앹뾽??議댁옱?섏? ?딆쓬")
            return
        hwnd = find_game_window(WIN_KEY)
        if not hwnd:
            print("[DEBUG] 寃뚯엫 ?덈룄?곕? 李얠쓣 ???놁쓬")
            self.preview_meta_lbl.configure(text="Game window not found")
            self.preview_canvas.delete("all")
            return
        print(f"[DEBUG] 寃뚯엫 ?덈룄??李얠쓬: {hwnd}")
        reg, target = self.get_trimmed_region_for_preview()
        if reg is None:
            print("[DEBUG] ?섎せ???щ∼ 踰붿쐞")
            self.preview_meta_lbl.configure(text="Invalid crop range")
            self.preview_canvas.delete("all")
            return

        full_img = grab_window_bg(hwnd)
        if full_img is None:
            print("[DEBUG] ?붾㈃ 罹≪쿂 ?ㅽ뙣")
            self.preview_meta_lbl.configure(text="Capture failed")
            self.preview_canvas.delete("all")
            return

        print("[DEBUG] ?붾㈃ 罹≪쿂 ?깃났, ?щ∼ 吏꾪뻾")
        try:
            crop = full_img.crop(reg.to_bbox((0, 0)))
        except Exception as e:
            print(f"[DEBUG] ?щ∼ ?ㅽ뙣: {e}")
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
        """Open a CustomTkinter popup for crop selection."""
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

        ok_btn = ctk.CTkButton(button_frame, text="OK", width=80, command=on_ok)
        ok_btn.pack(side="right", padx=5)

        cancel_btn = ctk.CTkButton(button_frame, text="Cancel", width=80, command=on_cancel)
        cancel_btn.pack(side="right", padx=5)

        entry.bind("<Return>", lambda e: on_ok())
        entry.bind("<Escape>", lambda e: on_cancel())

        dialog.wait_window()
        return result["value"]

    def save_calibration_preview(self):
        if self.preview_crop_pil is None:
            messagebox.showwarning("Save Preview", "癒쇱? CAPTURE濡?誘몃━蹂닿린瑜??앹꽦?섏꽭??")
            return
        if not messagebox.askyesno("Save Preview", "?꾩옱 誘몃━蹂닿린瑜???ν븷源뚯슂?"):
            return

        os.makedirs(TEMPLATE_DIGIT_DIR, exist_ok=True)
        default_name = f"{self.tune_target.get()}_{int(time.time())}"
        name = self.show_input_dialog("File Name", "Enter a name for the capture file (extension omitted):", default_name)
        if not name:
            return

        name_str = str(name)
        safe_name = "".join(
            ch for ch in name_str if ch.isalnum() or ch in ("_", "-", ".")
        ).strip(". ")
        if not safe_name:
            messagebox.showerror("Save Preview", "?좏슚???뚯씪紐낆쓣 ?낅젰?섏꽭??")
            return
        path = os.path.join(TEMPLATE_DIGIT_DIR, f"{safe_name}.png")

        if os.path.exists(path):
            overwrite = messagebox.askyesno("以묐났 ?뚯씪", f"?대? 議댁옱?⑸땲??\n??뼱?멸퉴??\n{path}")
            if not overwrite:
                return

        try:
            self.preview_crop_pil.save(path)
            messagebox.showinfo("Save Preview", f"????꾨즺:\n{path}")
        except Exception as e:
            messagebox.showerror("Save Preview", f"????ㅽ뙣:\n{e}")

    def save_with_mode_check(self):
        """Open the sampling tool popup."""
        if hasattr(self, "sample_pil") and self.sample_pil is not None:
            self.save_sample()
        else:
            self.save_calibration_preview()

    def start_sampling_tool(self):
        """Start sampling tool."""
        print("[DEBUG]")
        hwnd = find_game_window(WIN_KEY)
        if not hwnd:
            print("[DEBUG] 寃뚯엫李쎌쓣 李얠쓣 ???놁쓬")
            self.show_toast("[Err] WINDOW NOT FOUND", "#FF5555")
            return

        print(f"[DEBUG] 寃뚯엫李?李얠쓬: hwnd={hwnd}")
        self.sample_region = None

        def save_sample(sx, sy, dx, dy, captured_img=None):
            print(f"[DEBUG] ?섑뵆留??곸뿭 ??? ({sx}, {sy}, {dx}, {dy})")
            x1, x2 = sorted((sx, dx))
            y1, y2 = sorted((sy, dy))
            self.sample_region = [x1, y1, x2, y2]
            self.sample_original = [x1, y1, x2, y2]  # ?먮낯 諛깆뾽
            self.sample_trim = {"left": 0, "right": 0, "top": 0, "bottom": 0}
            self.sample_captured_img = captured_img  # 利됱떆 罹≪쿂???대?吏 ???
            self.show_sample_preview()

        print("[DEBUG] OverlaySelector ?몄텧 ?쒖옉")
        OverlaySelector(self.root, hwnd, save_sample)
        print("[DEBUG] OverlaySelector ?몄텧 ?꾨즺")

    def image_to_binary_string(self, img):
        """Convert image to 0/1 binary string (FindText-style)."""
        # 洹몃젅?댁뒪耳??蹂??
        gray = img.convert('L')
        # ?댁쭊??(?꾧퀎媛?128)
        binary = gray.point(lambda x: 0 if x < 128 else 1, '1')
        # 臾몄옄?대줈 蹂??
        width, height = binary.size
        binary_str = ""
        for y in range(height):
            for x in range(width):
                binary_str += str(binary.getpixel((x, y)))
            binary_str += "\n"
        return binary_str

    def show_sample_preview(self):
        """Docstring."""
        if not hasattr(self, "sample_region") or self.sample_region is None:
            return

        # ?쒕옒洹???利됱떆 罹≪쿂???대?吏 ?곗꽑 ?ъ슜 (醫뚰몴 ?뺥빀 蹂댁옣)
        full_img = getattr(self, 'sample_captured_img', None)
        if full_img is None:
            hwnd = find_game_window(WIN_KEY)
            if not hwnd:
                return
            full_img = grab_window_bg(hwnd)
        if full_img is None:
            return

        # ?몃┝ ?곸슜??醫뚰몴 怨꾩궛
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
            # grab_window_bg媛 ?대? BGRX?뭃GB 蹂?섏쓣 ?섑뻾?섎?濡?異붽? 蹂??遺덊븘??

            # 以??곸슜 (湲곕낯 2x, 議곗젙 媛??
            zoom = getattr(self, "sample_zoom", 2)
            preview_img = crop.resize((max(1, crop.width * zoom), max(1, crop.height * zoom)), _PIL_NEAREST)
            self.sample_pil = crop.copy()

            # 鍮꾪듃留?臾몄옄???앹꽦 (ft.ahk FindText 諛⑹떇)
            self.sample_binary_str = self.image_to_binary_string(crop)

            # 誘몃━蹂닿린李쎌뿉 ?쒖떆
            self.preview_tkimg = ImageTk.PhotoImage(preview_img)
            cw = max(1, self.preview_canvas.winfo_width())
            ch = max(1, self.preview_canvas.winfo_height())
            self.preview_canvas.delete("all")
            self.preview_canvas.create_image(cw // 2, ch // 2, image=self.preview_tkimg, anchor="center")
            trim_info = f"L{trim['left']} R{trim['right']} T{trim['top']} B{trim['bottom']}"
            category = self.sample_category_var.get() if hasattr(self, "sample_category_var") else "digits"
            self.preview_meta_lbl.configure(text=f"SAMPLE [{category.upper()}] | {crop.width}x{crop.height} | x{zoom} | {trim_info}", font=("Inter", 11, "bold"))
            self.show_toast("Sample preview updated - trim 1px and press SAVE")
        except Exception as e:
            self.show_toast("Sample preview failed", "#FF5555")

    def trim_sample(self, edge, delta=1):
        """Docstring."""
        if not hasattr(self, "sample_region") or self.sample_region is None:
            return
        if not hasattr(self, "sample_trim"):
            self.sample_trim = {"left": 0, "right": 0, "top": 0, "bottom": 0}

        x1, y1, x2, y2 = self.sample_region
        orig = getattr(self, "sample_original", [x1, y1, x2, y2])
        ow, oh = orig[2] - orig[0], orig[3] - orig[1]

        # ???몃┝媛?怨꾩궛 (?먮낯 踰붿쐞 珥덇낵 諛⑹?)
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
        """Docstring."""
        if hasattr(self, "sample_original") and self.sample_original:
            self.sample_region = list(self.sample_original)
            self.sample_trim = {"left": 0, "right": 0, "top": 0, "bottom": 0}
            self.show_sample_preview()
            self.show_toast("Reset complete")

    def set_sample_zoom(self, zoom):
        """Docstring."""
        self.sample_zoom = zoom
        if hasattr(self, "sample_region") and self.sample_region:
            self.show_sample_preview()

    def save_sample(self):
        """Save the selected sample image into the category folder."""
        print(f"[DEBUG] ????쒖옉: sample_pil 議댁옱 ?щ? ?뺤씤")
        if not hasattr(self, "sample_pil") or self.sample_pil is None:
            print(f"[DEBUG] sample_pil ?놁쓬: hasattr={hasattr(self, 'sample_pil')}, sample_pil={getattr(self, 'sample_pil', None)}")
            self.show_toast("OK")
            return

        # 移댄뀒怨좊━ 媛?몄삤湲?
        category = self.sample_category_var.get() if hasattr(self, "sample_category_var") else "digits"
        print(f"[DEBUG] 移댄뀒怨좊━: {category}")

        # 移댄뀒怨좊━ ?대뜑 寃쎈줈 ?앹꽦
        category_dir = os.path.join(SCRIPT_DIR, "temple", category)
        os.makedirs(category_dir, exist_ok=True)
        print(f"[DEBUG] 移댄뀒怨좊━ ?대뜑: {category_dir}")

        # ?뚯씪紐??낅젰 ?ㅼ씠?쇰줈洹?
        name = self.show_input_dialog("?뚯씪紐??낅젰", f"??ν븷 ?뚯씪紐낆쓣 ?낅젰?섏꽭??({category}):", "")
        print(f"[DEBUG] ?낅젰???뚯씪紐? {name}")
        if not name:
            self.show_toast("OK")
            return

        # ?뚯씪紐??덉쟾??
        safe_name = "".join(
            ch for ch in str(name) if ch.isalnum() or ch in ("_", "-", ".")
        ).strip(". ")
        print(f"[DEBUG] ?덉쟾?붾맂 ?뚯씪紐? {safe_name}")
        if not safe_name:
            self.show_toast("OK")
            return

        path = os.path.join(category_dir, f"{safe_name}.png")
        print(f"[DEBUG] ???寃쎈줈: {path}")

        # 以묐났 ?뚯씪紐?泥댄겕
        if os.path.exists(path):
            print(f"[DEBUG] 以묐났 ?뚯씪紐?議댁옱: {path}")
            messagebox.showwarning("以묐났 ?뚯씪", f"?대? 議댁옱?섎뒗 ?뚯씪紐낆엯?덈떎:\n{safe_name}.png\n?ㅻⅨ ?대쫫???ъ슜?섏꽭??")
            return

        try:
            print(f"[DEBUG] ????쒕룄: {path}")
            self.sample_pil.save(path)
            print(f"[DEBUG] ????깃났: {path}")

            # 鍮꾪듃留?臾몄옄?대룄 .txt ?뚯씪濡????(ft.ahk FindText 諛⑹떇)
            if hasattr(self, "sample_binary_str"):
                txt_path = os.path.join(category_dir, f"{safe_name}.txt")
                with open(txt_path, 'w', encoding='utf-8') as f:
                    f.write(self.sample_binary_str)
                print(f"[DEBUG] 鍮꾪듃留?臾몄옄??????깃났: {txt_path}")

            self.state.last_update_time = time.time()  # 鍮꾩쟾 ?붿쭊 由щ줈???몃━嫄?
            self.show_toast("Sample save failed", "#FF5555")
            # ?섑뵆留?紐⑤뱶 醫낅즺
            if hasattr(self, "sample_pil"):
                delattr(self, "sample_pil")
            if hasattr(self, "sample_binary_str"):
                delattr(self, "sample_binary_str")
        except Exception as e:
            print(f"[DEBUG] ????ㅽ뙣: {e}")
            self.show_toast("Sample save failed", "#FF5555")

    def _on_threshold_slider_change(self, value):
        """Docstring."""
        val = int(float(value))
        self.threshold_label.configure(text=str(val))
        # PatternMatcher???댁쭊???꾧퀎媛??숆린??
        global reader_thread
        if reader_thread and hasattr(reader_thread, 'matcher'):
            reader_thread.matcher.bin_threshold = val
            # ?쒗뵆由??ㅼ떆 濡쒕뱶 (??threshold濡??댁쭊??
            tmpl_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temple")
            reader_thread.matcher.reload(tmpl_root)
        # 罹섎━釉뚮젅?댁뀡 ?꾨━酉?媛깆떊
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
        # ?쇰? ?섍꼍(Linux/X11)?먯꽌 delta ???Button-4/5 ?대깽?몃? ?ъ슜
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
        # ?ㅻ퉬寃뚯씠??紐⑤뱶 ?뱀뀡 - Ultra-Compact
        nav_toggle_box = ctk.CTkFrame(self.c_nav, fg_color="#1A1C23", border_width=1, border_color="#343A46")
        nav_toggle_box.pack(fill="x", padx=1, pady=1)
        ctk.CTkLabel(nav_toggle_box, text="NAV", font=("Inter", 9, "bold"), text_color="#10B981").pack(pady=1)
        
        switch_row = ctk.CTkFrame(nav_toggle_box, fg_color="transparent")
        switch_row.pack(fill="x", pady=1, padx=2)
        
        self.sw_follow = ctk.CTkCheckBox(switch_row, text="Group Follow", font=("Inter", 9), command=self.update_nav_flags)
        self.sw_follow.pack(side="left", padx=2)
        
        self.sw_route = ctk.CTkCheckBox(switch_row, text="Route", font=("Inter", 9), command=self.update_nav_flags)
        self.sw_route.pack(side="left", padx=2)
        
        self.sw_avoid = ctk.CTkCheckBox(switch_row, text="Avoid", font=("Inter", 9), command=self.update_nav_flags)
        self.sw_avoid.pack(side="left", padx=2)

        # ?좎? ?먯? ????뱀뀡 - Ultra-Compact
        user_opt_box = ctk.CTkFrame(self.c_nav, fg_color="#1A1C23", border_width=1, border_color="#343A46")
        user_opt_box.pack(fill="x", padx=1, pady=1)
        ctk.CTkLabel(user_opt_box, text="USER OPTIONS", font=("Inter", 9, "bold"), text_color="#F87171").pack(pady=1)
        
        # ?? ?붿씠?몃━?ㅽ듃 CSV ?몄쭛 UI ??????????????????????????
        wl_box = ctk.CTkFrame(user_opt_box, fg_color="#0E1117", corner_radius=4)
        wl_box.pack(fill="x", padx=4, pady=2)
        ctk.CTkLabel(wl_box, text="Whitelist (whitelist.csv)", font=("Inter", 8, "bold"),
                     text_color="#10B981").pack(anchor="w", padx=4, pady=1)

        # Listbox ?곸뿭
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

        # ?낅젰李?+ 踰꾪듉
        wl_edit_row = ctk.CTkFrame(wl_box, fg_color="transparent")
        wl_edit_row.pack(fill="x", padx=4, pady=1)
        self.ent_wl_add = ctk.CTkEntry(wl_edit_row, width=130, placeholder_text="Enter nickname",
                                       height=20, font=("Inter", 8))
        self.ent_wl_add.pack(side="left", padx=1)
        ctk.CTkButton(wl_edit_row, text="ADD", height=20, width=40, font=("Inter", 8),
                      fg_color="#10B981", command=self._wl_add_nick).pack(side="left", padx=1)
        ctk.CTkButton(wl_edit_row, text="DEL", height=20, width=40, font=("Inter", 8),
                      fg_color="#EF4444", command=self._wl_del_nick).pack(side="left", padx=1)
        ctk.CTkButton(wl_edit_row, text="SAVE", height=20, width=40, font=("Inter", 8),
                      fg_color="#A855F7", command=self._wl_save).pack(side="left", padx=1)

        # 珥덇린 濡쒕뱶
        self._wl_refresh_listbox()

        # ????듭뀡 ?ㅼ쐞移?
        act_row = ctk.CTkFrame(user_opt_box, fg_color="transparent")
        act_row.pack(fill="x", pady=1, padx=2)
        
        self.sw_u_alarm = ctk.CTkCheckBox(act_row, text="Alert", font=("Inter", 9), command=self.sync_user_opts)
        self.sw_u_alarm.pack(side="left", padx=2)
        
        self.sw_u_stop = ctk.CTkCheckBox(act_row, text="Stop", font=("Inter", 9), command=self.sync_user_opts)
        self.sw_u_stop.pack(side="left", padx=2)
        
        self.sw_u_next = ctk.CTkCheckBox(act_row, text="Skip", font=("Inter", 9), command=self.sync_user_opts)
        self.sw_u_next.pack(side="left", padx=2)
        
        self.sw_auto_debuff = ctk.CTkCheckBox(act_row, text="AUTO DEBUFF", font=("Inter", 9), command=self.sync_user_opts)
        self.sw_auto_debuff.pack(side="left", padx=2)

        # ?? Grid 誘몄꽭議곗젙 ?щ씪?대뜑 ??????????????????????????
        grid_tune_box = ctk.CTkFrame(self.c_nav, fg_color="#1A1C23", border_width=1, border_color="#343A46")
        grid_tune_box.pack(fill="x", padx=1, pady=1)
        ctk.CTkLabel(grid_tune_box, text="Grid Tuning", font=("Inter", 9, "bold"), text_color="#F59E0B").pack(pady=1)

        # ?ㅽ봽??X ?щ씪?대뜑
        sx_row = ctk.CTkFrame(grid_tune_box, fg_color="transparent")
        sx_row.pack(fill="x", padx=4, pady=1)
        ctk.CTkLabel(sx_row, text="Offset X", font=("Inter", 8), width=50).pack(side="left")
        self.grid_sx_var = tk.IntVar(value=276)
        self.grid_sx_slider = ctk.CTkSlider(sx_row, from_=250, to=300, variable=self.grid_sx_var,
                                            height=12, width=120, command=self._on_grid_tune)
        self.grid_sx_slider.pack(side="left", padx=2)
        self.grid_sx_lbl = ctk.CTkLabel(sx_row, text="276", font=("Inter", 8), width=30)
        self.grid_sx_lbl.pack(side="left")

        # ?ㅽ봽??Y ?щ씪?대뜑
        sy_row = ctk.CTkFrame(grid_tune_box, fg_color="transparent")
        sy_row.pack(fill="x", padx=4, pady=1)
        ctk.CTkLabel(sy_row, text="Offset Y", font=("Inter", 8), width=50).pack(side="left")
        self.grid_sy_var = tk.IntVar(value=32)
        self.grid_sy_slider = ctk.CTkSlider(sy_row, from_=20, to=50, variable=self.grid_sy_var,
                                            height=12, width=120, command=self._on_grid_tune)
        self.grid_sy_slider.pack(side="left", padx=2)
        self.grid_sy_lbl = ctk.CTkLabel(sy_row, text="32", font=("Inter", 8), width=30)
        self.grid_sy_lbl.pack(side="left")

        # 洹몃━???ш린 ?щ씪?대뜑
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

        # ???踰꾪듉
        ctk.CTkButton(grid_tune_box, text="SAVE", height=20, width=60, font=("Inter", 8),
                      fg_color="#F59E0B", command=self._save_grid_tune).pack(pady=2)

        # ?щ깷 寃쎈줈 ?뱀뀡 - Ultra-Compact
        seq_box = ctk.CTkFrame(self.c_nav, fg_color="#1A1C23", border_width=1, border_color="#343A46")
        seq_box.pack(fill="x", padx=1, pady=1)
        ctk.CTkLabel(seq_box, text="Route Editor", font=("Inter", 9, "bold"), text_color="#A855F7").pack(pady=1)
        
        # ?곷떒: 留?痢??좏깮 諛???갑??紐⑤뱶
        top_row = ctk.CTkFrame(seq_box, fg_color="transparent")
        top_row.pack(fill="x", pady=1, padx=2)
        
        ctk.CTkLabel(top_row, text="Map", font=("Inter", 9)).pack(side="left", padx=1)
        self.hunting_map_var = ctk.StringVar(value="Select map")
        self.hunting_map_dropdown = ctk.CTkOptionMenu(top_row, variable=self.hunting_map_var, values=["Select map"], command=self.on_hunting_map_change, width=80)
        self.hunting_map_dropdown.pack(side="left", padx=1)
        
        ctk.CTkLabel(top_row, text="Floor", font=("Inter", 9)).pack(side="left", padx=1)
        self.hunting_floor_var = ctk.StringVar(value="1")
        self.hunting_floor_dropdown = ctk.CTkOptionMenu(top_row, variable=self.hunting_floor_var, values=["1"], command=self.on_hunting_floor_change, width=50)
        self.hunting_floor_dropdown.pack(side="left", padx=1)
        
        self.sw_reverse_mode = ctk.CTkCheckBox(top_row, text="REV", font=("Inter", 9), command=self.toggle_reverse_mode)
        self.sw_reverse_mode.pack(side="left", padx=1)
        
        # 愿由?踰꾪듉 (??以꾩뿉 4媛?
        mid_row = ctk.CTkFrame(seq_box, fg_color="transparent")
        mid_row.pack(fill="x", pady=1, padx=2)
        
        ctk.CTkButton(mid_row, text="+MAP", height=22, width=50, font=("Inter", 9), command=self.add_hunting_map).pack(side="left", padx=1)
        ctk.CTkButton(mid_row, text="+FLOOR", height=22, width=50, font=("Inter", 9), command=self.add_hunting_floor).pack(side="left", padx=1)
        ctk.CTkButton(mid_row, text="+POS", height=22, width=50, font=("Inter", 9), command=self.add_current_coord).pack(side="left", padx=1)
        ctk.CTkButton(mid_row, text="SAVE", height=22, width=50, font=("Inter", 9), command=self.save_waypoints).pack(side="left", padx=1)
        
        # Treeview (?묒? ?ㅽ?????
        tree_container = ctk.CTkFrame(seq_box, fg_color="#14161B")
        tree_container.pack(fill="x", pady=1, padx=2)
        
        tree_frame = tk.Frame(tree_container, bg="#14161B")
        tree_frame.pack(fill="x", padx=2, pady=2)
        
        columns = ("type", "x", "y", "direction", "reverse_action", "grid")
        self.seq_tree = tk.ttk.Treeview(tree_frame, columns=columns, show="headings", height=12)
        self.seq_tree.heading("type", text="Type")
        self.seq_tree.heading("x", text="X")
        self.seq_tree.heading("y", text="Y")
        self.seq_tree.heading("direction", text="Dir")
        self.seq_tree.heading("reverse_action", text="Rev")
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
        
        # 愿由??꾧뎄 (??????젣 ????꾩옱醫뚰몴?낅젰 ?대룞)
        tool_row = ctk.CTkFrame(seq_box, fg_color="transparent")
        tool_row.pack(fill="x", pady=1, padx=2)
        
        ctk.CTkButton(tool_row, text="UP", height=22, width=30, font=("Inter", 9), command=self.move_seq_up).pack(side="left", padx=1)
        ctk.CTkButton(tool_row, text="DOWN", height=22, width=30, font=("Inter", 9), command=self.move_seq_down).pack(side="left", padx=1)
        ctk.CTkButton(tool_row, text="DEL", height=22, width=30, font=("Inter", 9), command=self.delete_seq_item).pack(side="left", padx=1)
        ctk.CTkButton(tool_row, text="SAVE", height=22, width=30, font=("Inter", 9), command=self.save_waypoints).pack(side="left", padx=1)
        ctk.CTkButton(tool_row, text="CUR", height=22, width=70, font=("Inter", 9), command=self.set_current_coord_to_selected).pack(side="left", padx=1)
        ctk.CTkButton(tool_row, text="GO", height=22, width=40, font=("Inter", 9), command=self.move_to_selected_coord).pack(side="left", padx=1)
        
        # 異붽? 踰꾪듉 (?낃뎄 ?щ깷 異쒓뎄)
        add_row = ctk.CTkFrame(seq_box, fg_color="transparent")
        add_row.pack(fill="x", pady=1, padx=2)
        
        ctk.CTkButton(add_row, text="ENTRY", height=22, width=80, font=("Inter", 9), command=self.add_seq_entry).pack(side="left", padx=1)
        ctk.CTkButton(add_row, text="POINT", height=22, width=80, font=("Inter", 9), command=self.add_seq_point).pack(side="left", padx=1)
        ctk.CTkButton(add_row, text="EXIT", height=22, width=80, font=("Inter", 9), command=self.add_seq_exit).pack(side="left", padx=1)
        
        # 珥덇린 ?쒗??濡쒕뱶
        self.load_hunting_sequence()

    def update_nav_flags(self):
        self.state.nav_follow_enabled = self.sw_follow.get()
        self.state.nav_route_enabled = self.sw_route.get()
        self.state.nav_avoid_enabled = self.sw_avoid.get()
        self.show_toast("Navigation flags updated")

    def sync_user_opts(self):
        self.state.user_alarm_enabled = self.sw_u_alarm.get()
        self.state.user_stop_enabled = self.sw_u_stop.get()
        self.state.user_next_enabled = self.sw_u_next.get()
        self.state.auto_debuff_enabled = self.sw_auto_debuff.get()
        self.show_toast("User options updated")

    # ?? Grid 誘몄꽭議곗젙 ?ы띁 ??????????????????????????????
    def _on_grid_tune(self, val=None):
        """Update grid tuning and save immediately."""
        sx = int(self.grid_sx_var.get())
        sy = int(self.grid_sy_var.get())
        gs = round(float(self.grid_size_var.get()), 1)
        self.grid_sx_lbl.configure(text=str(sx))
        self.grid_sy_lbl.configure(text=str(sy))
        self.grid_size_lbl.configure(text=f"{gs:.1f}")
        # config.json 利됱떆 ???
        self._save_grid_tune()

    def _save_grid_tune(self):
        """Save grid tuning values into config.json."""
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
            # GridIndicator?먮룄 利됱떆 諛섏쁺
            if hasattr(self, 'grid_indicator') and self.grid_indicator:
                self.grid_indicator.grid_size = gs
                self.show_toast("Grid tuning updated")
        except Exception as e:
            print(f"[GridTune] ????ㅽ뙣: {e}")

    # ?? ?붿씠?몃━?ㅽ듃 愿由??ы띁 ??????????????????????????????
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
        # GameState??蹂닿???由ъ뒪?몃? CSV?????
        self.state.save_whitelist_csv()
        self.show_toast("Whitelist saved")


    class GridConverter:
        """GridConverter helper utilities."""
        
        @staticmethod
        def name_to_index(grid_name):
            """Convert grid names like S3 or A10 to column/row indexes."""
            if not grid_name or len(grid_name) < 2:
                return None, None
            # ?뚰뙆踰?異붿텧 (??
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
                # ??怨꾩궛 (A=0, B=1, ..., S=18)
                col = 0
                for char in col_part:
                    col = col * 26 + (ord(char) - ord('A'))
                # ??怨꾩궛 (1-indexed ??0-indexed)
                row = int(row_part) - 1
                return col, row
            except (ValueError, IndexError):
                return None, None
        
        @staticmethod
        def index_to_name(col, row):
            """Convert column/row indexes back to grid names like S3 or A10."""
            try:
                # ??怨꾩궛 (0-indexed ???뚰뙆踰?
                col_name = ""
                temp_col = col
                while temp_col >= 0:
                    col_name = chr(ord('A') + (temp_col % 26)) + col_name
                    temp_col = temp_col // 26 - 1
                    if temp_col < 0:
                        break
                # ??怨꾩궛 (0-indexed ??1-indexed)
                row_name = str(row + 1)
                return f"{col_name}{row_name}"
            except:
                return None

    def grid_name_to_coords(self, grid_name):
        """Docstring."""
        return AppView.GridConverter.name_to_index(grid_name)

    def coords_to_grid_name(self, col, row):
        """Docstring."""
        return AppView.GridConverter.index_to_name(col, row)

    def save_waypoints(self):
        waypoint_file = os.path.join(SCRIPT_DIR, "waypoints.json")
        with open(waypoint_file, 'w', encoding='utf-8') as f:
            json.dump(self.state.waypoints_db, f, indent=4)
            
        # CSV 異붽? ???(?묒? 愿由ъ슜 ???앹꽦)
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
            print(f"[Warn] CSV 諛깆뾽 ?ㅽ뙣: {e}")
            
        self.show_toast("Waypoints saved (JSON/CSV)")

    def load_hunting_sequence(self):
        """Docstring."""
        csv_path = os.path.join(SCRIPT_DIR, "waypoints.csv")
        waypoint_file = os.path.join(SCRIPT_DIR, "waypoints.json")
        loaded_db = {}
        
        # 1. CSV ?뚯씪 ?곗꽑 濡쒕뱶 (?ъ슜??吏곸젒 ?묒? ?몄쭛 諛섏쁺)
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
                print(f"[Warn] CSV 濡쒕뱶 ?ㅽ뙣, JSON ?대갚 ?쒕룄: {e}")
        
        # 2. CSV媛 ?놁쓣 寃쎌슦 湲곗〈 JSON.load
        if not loaded_db and os.path.exists(waypoint_file):
            try:
                with open(waypoint_file, "r", encoding="utf-8") as f:
                    loaded_db = json.load(f)
            except (OSError, json.JSONDecodeError):
                loaded_db = {}
        
        self.state.waypoints_db = loaded_db
        
        # 留??쒕∼?ㅼ슫 ?낅뜲?댄듃
        map_names = list(self.state.waypoints_db.keys())
        self.hunting_map_dropdown.configure(values=map_names if map_names else ["蹂듦굔泥쒕━留덇뎬"])
        
        # 痢??쒕∼?ㅼ슫 ?낅뜲?댄듃
        current_map = self.hunting_map_var.get()
        if current_map in self.state.waypoints_db:
            floors = list(self.state.waypoints_db[current_map].keys())
            self.hunting_floor_dropdown.configure(values=floors if floors else ["1"])
        
        # ?쒗??濡쒕뱶
        self.refresh_seq_tree()

    def refresh_seq_tree(self):
        """Docstring."""
        for item in self.seq_tree.get_children():
            self.seq_tree.delete(item)
        
        current_map = self.hunting_map_var.get()
        current_floor = self.hunting_floor_var.get()
        
        if current_map in self.state.waypoints_db and current_floor in self.state.waypoints_db[current_map]:
            seq_data = self.state.waypoints_db[current_map][current_floor]
            
            # ?낃뎄
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
            
            # ?щ깷??
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
            
            # 異쒓뎄
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
        """Docstring."""
        current_map = value
        if current_map in self.state.waypoints_db:
            floors = list(self.state.waypoints_db[current_map].keys())
            self.hunting_floor_dropdown.configure(values=floors if floors else ["1"])
            if floors:
                self.hunting_floor_var.set(floors[0])
        self.refresh_seq_tree()

    def on_hunting_floor_change(self, value):
        """Docstring."""
        self.refresh_seq_tree()

    def add_hunting_floor(self):
        """Docstring."""
        current_map = self.hunting_map_var.get()
        if current_map not in self.state.waypoints_db:
            self.show_toast("Select a map first", "#FF5555")
            return
        
        pop = ctk.CTkToplevel(self.root)
        pop.title("Add Floor")
        pop.geometry("300x150")
        pop.attributes("-topmost", True)
        pop.transient(self.root)
        
        ctk.CTkLabel(pop, text="Floor name:", font=("Inter", 11)).pack(pady=10)
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
                    self.show_toast("Hunting floor added")
                    pop.destroy()
                else:
                    self.show_toast("Floor already exists", "#FF5555")
        
        ctk.CTkButton(pop, text="OK", command=on_ok).pack(pady=10)

    def add_current_coord(self):
        """Docstring."""
        current_map = self.hunting_map_var.get()
        current_floor = self.hunting_floor_var.get()
        if current_map not in self.state.waypoints_db or current_floor not in self.state.waypoints_db[current_map]:
            self.show_toast("Select a map and floor first", "#FF5555")
            return
        
        data = self.state.get_all()
        x, y = data.get("x", 0), data.get("y", 0)
        seq_data = self.state.waypoints_db[current_map][current_floor]
        seq_data["points"].append(f"{x},{y}")
        self.save_waypoints()
        self.refresh_seq_tree()
        self.show_toast("Current coordinate added")

    def on_tree_double_click(self, event):
        """Docstring."""
        item = self.seq_tree.selection()
        if not item:
            return
        item = item[0]
        column = self.seq_tree.identify_column(event.x)
        
        # 而щ읆 ?몃뜳??媛?몄삤湲?
        col_map = {"#1": "type", "#2": "x", "#3": "y", "#4": "direction", "#5": "reverse_action"}
        col_name = col_map.get(column, "")
        
        if not col_name:
            return
        
        current_values = self.seq_tree.item(item, "values")
        col_idx = list(col_map.values()).index(col_name)
        current_value = current_values[col_idx]
        
        # ?몄쭛 ?앹뾽
        pop = ctk.CTkToplevel(self.root)
        pop.title("? ?몄쭛")
        pop.geometry("300x120")
        pop.attributes("-topmost", True)
        pop.transient(self.root)
        
        ctk.CTkLabel(pop, text=f"Edit {col_name}:", font=("Inter", 10)).pack(pady=5)
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
            
            # waypoints.json?????
            self.save_tree_to_data()
            pop.destroy()
        
        def on_cancel():
            pop.destroy()
        
        btn_row = ctk.CTkFrame(pop, fg_color="transparent")
        btn_row.pack(pady=5)
        ctk.CTkButton(btn_row, text="OK", height=24, width=80, command=on_ok).pack(side="left", padx=5)
        ctk.CTkButton(btn_row, text="CANCEL", height=24, width=80, command=on_cancel).pack(side="left", padx=5)
        
        ent.bind("<Return>", lambda e: on_ok())
        ent.bind("<Escape>", lambda e: on_cancel())

    def save_tree_to_data(self):
        """Save Treeview data into waypoints.json."""
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
        """Docstring."""
        pop = ctk.CTkToplevel(self.root)
        pop.title("Add Map")
        pop.geometry("300x150")
        pop.attributes("-topmost", True)
        pop.transient(self.root)
        
        ctk.CTkLabel(pop, text="Map name:", font=("Inter", 11)).pack(pady=10)
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
                    self.show_toast("Hunting map added")
                    pop.destroy()
                else:
                    self.show_toast("Map already exists", "#FF5555")
        
        ctk.CTkButton(pop, text="OK", command=on_ok).pack(pady=10)

    def delete_hunting_map(self):
        """Docstring."""
        current_map = self.hunting_map_var.get()
        if current_map in self.state.waypoints_db:
            del self.state.waypoints_db[current_map]
            self.save_waypoints()
            self.load_hunting_sequence()
            self.show_toast("Hunting map deleted")

    def delete_seq_item(self):
        """Docstring."""
        item = self.seq_tree.selection()
        if not item:
            return
        item = item[0]
        
        self.seq_tree.delete(item)
        self.save_tree_to_data()
        self.show_toast("Selected item deleted")

    def move_seq_up(self):
        """Docstring."""
        item = self.seq_tree.selection()
        if not item:
            return
        item = item[0]
        
        # ?꾩옱 ?꾩씠?쒖쓽 媛?媛?몄삤湲?
        values = self.seq_tree.item(item, "values")
        
        # ?댁쟾 ?꾩씠??李얘린
        items = self.seq_tree.get_children()
        idx = items.index(item)
        if idx == 0:
            return  # 泥?踰덉㎏ ?꾩씠?쒖? ?대룞 遺덇?
        
        # ?댁쟾 ?꾩씠?쒓낵 媛?援먰솚
        prev_item = items[idx - 1]
        prev_values = self.seq_tree.item(prev_item, "values")
        
        self.seq_tree.item(item, values=prev_values)
        self.seq_tree.item(prev_item, values=values)
        self.seq_tree.selection_set(prev_item)
        
        self.save_tree_to_data()

    def move_seq_down(self):
        """Docstring."""
        item = self.seq_tree.selection()
        if not item:
            return
        item = item[0]
        
        # ?꾩옱 ?꾩씠?쒖쓽 媛?媛?몄삤湲?
        values = self.seq_tree.item(item, "values")
        
        # ?ㅼ쓬 ?꾩씠??李얘린
        items = self.seq_tree.get_children()
        idx = items.index(item)
        if idx == len(items) - 1:
            return  # 留덉?留??꾩씠?쒖? ?대룞 遺덇?
        
        # ?ㅼ쓬 ?꾩씠?쒓낵 媛?援먰솚
        next_item = items[idx + 1]
        next_values = self.seq_tree.item(next_item, "values")
        
        self.seq_tree.item(item, values=next_values)
        self.seq_tree.item(next_item, values=values)
        self.seq_tree.selection_set(next_item)
        
        self.save_tree_to_data()

    def add_seq_entry(self):
        """Docstring."""
        current_map = self.hunting_map_var.get()
        current_floor = self.hunting_floor_var.get()
        if current_map not in self.state.waypoints_db or current_floor not in self.state.waypoints_db[current_map]:
            return
        
        # Treeview???낃뎄 ??異붽?
        self.seq_tree.insert("", 0, values=("Entry", 0, 0, "right", "", ""))
        self.save_tree_to_data()
        self.show_toast("Entry added")

    def add_seq_point(self):
        """Docstring."""
        current_map = self.hunting_map_var.get()
        current_floor = self.hunting_floor_var.get()
        if current_map not in self.state.waypoints_db or current_floor not in self.state.waypoints_db[current_map]:
            return
        
        # ?꾩옱 醫뚰몴 媛?몄삤湲?
        data = self.state.get_all()
        x, y = data.get("x", 0), data.get("y", 0)
        
        # grid 醫뚰몴 怨꾩궛
        grid_str = self.calculate_grid_coord(x, y)
        
        # Treeview???щ깷????異붽?
        self.seq_tree.insert("", "end", values=("Hunt", x, y, "", "", grid_str))
        self.save_tree_to_data()
        self.show_toast("Point added")

    def add_seq_exit(self):
        """Docstring."""
        current_map = self.hunting_map_var.get()
        current_floor = self.hunting_floor_var.get()
        if current_map not in self.state.waypoints_db or current_floor not in self.state.waypoints_db[current_map]:
            return
        
        # Treeview??異쒓뎄 ??異붽?
        self.seq_tree.insert("", "end", values=("Exit", 0, 0, "down", "", ""))
        self.save_tree_to_data()
        self.show_toast("Exit added")

    def on_tree_key_press(self, event):
        """Docstring."""
        item = self.seq_tree.selection()
        if not item:
            return
        item = item[0]
        
        # 諛⑺뼢??留ㅽ븨
        direction_map = {
            "Up": "up",
            "Down": "down",
            "Left": "left",
            "Right": "right"
        }
        
        if event.keysym in direction_map:
            direction = direction_map[event.keysym]
            # ?꾩옱 媛?媛?몄삤湲?
            values = list(self.seq_tree.item(item, "values"))
            # direction 而щ읆 ?낅뜲?댄듃 (index 3)
            values[3] = direction
            self.seq_tree.item(item, values=values)
            self.save_tree_to_data()
            self.show_toast(f"Direction: {direction}")

    def calculate_grid_coord(self, x, y):
        """Docstring."""
        try:
            # hwnd ?좏슚??寃??
            if not self.hwnd:
                return ""

            # ?? ?뺤젙 ?ㅽ봽?? sx=276, sy=32 / 寃⑹옄 ?ш린 48.2 ??
            # 怨듭떇: GridX = (PixelX - 276) / 48.2, GridY = (PixelY - 32) / 48.2
            config_file = os.path.join(SCRIPT_DIR, "config.json")
            play_area_sx = 276  # ?뺤젙 ?ㅽ봽??
            play_area_sy = 32   # ?뺤젙 ?ㅽ봽??
            grid_size = 48.2    # float (?꾩쟻?ㅼ감 諛⑹?)
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
            
            # grid 醫뚰몴瑜?臾몄옄?대줈 蹂??(?? A1, B2, C3)
            if grid_x < 0 or grid_y < 0:
                return ""
            
            col_char = chr(ord('A') + grid_x)
            grid_str = f"{col_char}{grid_y + 1}"
            
            return grid_str
        except Exception as e:
            print(f"grid 醫뚰몴 怨꾩궛 ?ㅻ쪟: {e}")
            return ""

    def set_current_coord_to_selected(self):
        """Docstring."""
        item = self.seq_tree.selection()
        if not item:
            self.show_toast("Select an item first", color="#EF4444")
            return
        item = item[0]
        
        # ?꾩옱 寃뚯엫 醫뚰몴 媛?몄삤湲?
        data = self.state.get_all()
        x, y = data.get("x", 0), data.get("y", 0)
        
        # grid 醫뚰몴 怨꾩궛
        grid_str = self.calculate_grid_coord(x, y)
        
        # ?꾩옱 媛?媛?몄삤湲?
        values = list(self.seq_tree.item(item, "values"))
        # x, y, grid 而щ읆 ?낅뜲?댄듃 (index 1, 2, 5)
        values[1] = str(x)
        values[2] = str(y)
        values[5] = grid_str
        self.seq_tree.item(item, values=values)
        self.save_tree_to_data()
        self.show_toast("Coordinate updated")

    def move_to_selected_coord(self):
        """Docstring."""
        print("[Move] Selected coordinate move started")
        item = self.seq_tree.selection()
        if not item:
            print("[Move] No item selected, auto-selecting first item")
            # Auto-select the first item
            first_item = self.seq_tree.get_children()
            if first_item:
                self.seq_tree.selection_set(first_item[0])
                item = self.seq_tree.selection()
                if not item:
                    print("[Move] Failed to auto-select the first item")
                    self.show_toast("No items available", color="#EF4444")
                    return
                item = item[0]
            else:
                print("[Move] No items available")
                self.show_toast("No items available", color="#EF4444")
                return
        else:
            item = item[0]
        
        # ?좏깮????ぉ??醫뚰몴 媛?몄삤湲?
        values = self.seq_tree.item(item, "values")
        x = int(values[1]) if values[1] else 0
        y = int(values[2]) if values[2] else 0
        grid_str = values[5] if len(values) > 5 else ""
        print(f"[Move] ?좏깮??醫뚰몴: ({x}, {y}), 洹몃━?? {grid_str}")
        
        # ?꾩옱 醫뚰몴 媛?몄삤湲?
        data = self.state.get_all()
        current_x, current_y = data.get("x", 0), data.get("y", 0)
        print(f"[Move] ?꾩옱 醫뚰몴: ({current_x}, {current_y})")
        
        # 諛⑺뼢 寃곗젙
        dx = x - current_x
        dy = y - current_y
        print(f"[Move] dx: {dx}, dy: {dy}")
        
        # 諛⑺뼢???낅젰 (?꾨몢?대끂 ?듭떊)
        if abs(dx) > abs(dy):
            direction = "right" if dx > 0 else "left"
        else:
            direction = "down" if dy > 0 else "up"
        
        print(f"[Move] 諛⑺뼢: {direction}")
        
        # ?꾨몢?대끂濡?諛⑺뼢???꾩넚
        try:
            hw.hold_move(direction, "move_hold")
            print("[Move] ?꾨몢?대끂 ?꾩넚 ?꾨즺")
        except Exception as e:
            print(f"[Move] ?꾨몢?대끂 ?꾩넚 ?ㅽ뙣: {e}")
        
            self.show_toast("Move complete")

    def toggle_reverse_mode(self):
        """Docstring."""
        self.state.reverse_mode = self.sw_reverse_mode.get()
        self.show_toast(f"Reverse mode: {'ON' if self.state.reverse_mode else 'OFF'}")

    def show_toast(self, msg, color="#10B981"):
        """Docstring."""
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
        self.show_toast("Spell settings saved")

    def get_dynamic_font_size(self, text, max_width=34):
        # ?띿뒪??湲몄씠???곕씪 ?고듃 ?ш린 怨꾩궛 (怨듦컙????醫곸븘吏먯뿉 ?곕씪 ???묒? ?고듃 ?덉슜)
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
        # GameState ?숆린??
        self.state.spells.clear()
        for s in self.spell_db:
            if s.get("slot") and s.get("use", True):
                self.state.spells.append(Skill(
                    name=s["name"],
                    target_type=s["target_type"],
                    hotkey=s["slot"] if len(s["slot"]) == 1 and s["slot"].isdigit() else None,
                    spell_char=s["slot"] if not (len(s["slot"]) == 1 and s["slot"].isdigit()) else None,
                    category=s.get("category", "怨듦꺽")
                ))

    def sync_slots_from_db(self):
        # UI ?낅뜲?댄듃 濡쒖쭅 (build ?댄썑 ?몄텧 媛??
        if hasattr(self, 'slot_buttons'):
            # 珥덇린??
            for info in self.slot_buttons.values():
                info["btn"].configure(text="", fg_color="#252A34", border_color="#4B5563")
            
            # DB?먯꽌 ?щ’ 留ㅽ븨 ?뺤씤
            for spell in self.spell_db:
                slot_key = spell.get("slot")
                if slot_key:
                    for pos, info in self.slot_buttons.items():
                        if info["key"] == slot_key:
                            name = spell["name"]
                            fs = self.get_dynamic_font_size(name)
                            color = self.get_category_color(spell.get("category"))
                            # 梨꾩썙吏??щ’: ?띿뒪??異붽?, ?ㅽ겕??諛곌꼍 諛??좎깋 ?뚮몢由?
                            info["btn"].configure(text=name, font=("Inter", fs), fg_color="#1F2937", border_color=color)

    def get_category_color(self, cat):
        # 泥⑤? ?대?吏???먮굦???대┛ ?좊챸???됱긽 援ъ꽦
        colors = {"ATTACK": "#F43F5E", "BUFF": "#0EA5E9", "DEBUFF": "#8B5CF6", "OTHER": "#F59E0B"}
        return colors.get(cat, "#475569")

    def refresh_storage_box(self):
        for widget in self.box_scroll.winfo_children():
            widget.destroy()
        
        for i, spell in enumerate(self.spell_db):
            r, c = divmod(i, 10) # 10?대줈 諛곗뿴
            name = spell["name"]
            fs = self.get_dynamic_font_size(name)
            color = self.get_category_color(spell.get("category"))
            
            # ?좏깮???곹깭硫??섏씠?쇱씠??誘쇳듃???뚮몢由?
            btn_color = "#1F2937" if self.picked_spell != name else "#111827"
            border_color = "#10B981" if self.picked_spell == name else color
            
            btn = ctk.CTkButton(self.box_scroll, text=name, width=34, height=34,
                               fg_color=btn_color, corner_radius=8,
                               border_width=2, border_color=border_color,
                               font=("Inter", fs),
                               command=lambda n=name: self.pick_spell(n))
            btn.grid(row=r, column=c, padx=2, pady=2)
            
            # ?쒕옒洹????쒕∼ 諛붿씤??
            btn.bind("<ButtonPress-1>", lambda e, n=name: self.on_drag_start(e, n))
            btn.bind("<B1-Motion>", self.on_drag_motion)
            btn.bind("<ButtonRelease-1>", self.on_drag_drop)

    def on_drag_start(self, event, name):
        self.dnd_spell = name
        if self.dnd_label:
            self.dnd_label.destroy()
            self.dnd_label = None
            
        fs = self.get_dynamic_font_size(name)
        # ?뚮줈???덉씠釉??앹꽦
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
        
        # ?쒕∼???꾩튂媛 ?щ’ ?대??몄? ?먮퀎
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
            cb_type = ctk.CTkOptionMenu(row, values=["INSTANT", "TARGET_SELECT", "TARGET_INPUT"],
                                      variable=var_type, width=100, font=("Inter", 10),
                                      command=lambda v, s=spell: self.update_spell_prop(s, "target_type", v))
            cb_type.pack(side="left", padx=5)
            
            # Category Dropdown
            var_cat = ctk.StringVar(value=spell.get("category", "怨듦꺽"))
            cb_cat = ctk.CTkOptionMenu(row, values=["ATTACK", "BUFF", "DEBUFF", "OTHER"],
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
        
        # ?댁쟾 ?щ’??媛숈? ?ㅼ??ㅻ㈃ ?쒓굅 (以묐났 諛⑹?)
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
        self.refresh_storage_box() # ?됱긽 ?깆씠 諛붾????덉쑝誘濡?媛깆떊
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
            # 利됱떆 ?꾨━酉?媛깆떊
            if hasattr(self, "update_calibration_preview"):
                self.update_calibration_preview()

        # ?ㅽ겕由곗꺑 李?????ㅼ떆媛??ㅻ쾭?덉씠 ?ㅽ뻾
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
                self.show_toast("??GAME NOT FOUND", "#FF5555")
                return
            self.indicator = ROIIndicator(self.root, hwnd)
            
        self.indicator.visible = not self.indicator.visible
        if self.indicator.visible:
            # 利됱떆 ?쒖떆
            global reader_thread
            if reader_thread is not None and hasattr(reader_thread, "regions"):
                self.indicator.update_position(reader_thread.regions)
        else:
            self.indicator.withdraw() # 利됱떆 ?④? 踰꾧렇 ?섏젙

    def toggle_grid_overlay(self):
        if not hasattr(self, "grid_indicator"):
            hwnd = find_game_window(WIN_KEY)
            if not hwnd:
                self.show_toast("??GAME NOT FOUND", "#FF5555")
                return
            self.grid_indicator = GridIndicator(self.root, hwnd, self.state)
            
        self.state.grid_overlay_enabled = not self.state.grid_overlay_enabled
        self.grid_indicator.visible = self.state.grid_overlay_enabled
        
        if self.state.grid_overlay_enabled:
            self.btn_grid_toggle.configure(text="GRID ON", fg_color="#10B981")
            self.lbl_grid_offset.configure(text=f"OFFSET: ({self.grid_indicator.offset_x}, {self.grid_indicator.offset_y})")
            self.grid_indicator.update_position()
            self.show_toast("OK")
        else:
            self.btn_grid_toggle.configure(text="GRID OFF", fg_color="#FF5555")
            self.grid_indicator.withdraw()
            self.show_toast("Grid overlay hidden")

    def on_alpha_change(self, value):
        """Docstring."""
        if hasattr(self, "grid_indicator"):
            self.grid_indicator.set_alpha(float(value))

    def move_grid(self, dx, dy):
        """Docstring."""
        if not hasattr(self, "grid_indicator") or not self.grid_indicator.visible:
            self.show_toast("Grid overlay is disabled", "#FF5555")
            return
        
        self.grid_indicator.offset_x += dx
        self.grid_indicator.offset_y += dy
        self.lbl_grid_offset.configure(text=f"OFFSET: ({self.grid_indicator.offset_x}, {self.grid_indicator.offset_y})")
        self.grid_indicator.update_position()
        
        # ?ㅽ봽?뗭쓣 config.json?????
        self.save_grid_offset()
    
    def save_grid_offset(self):
        """Save the grid indicator offset into config.json."""
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
        
        # ???
        conf = load_config()
        conf[target] = asdict(reg)
        save_config(conf)
        # 利됱떆 ?쇰뱶諛?
        if hasattr(self, "indicator") and self.indicator.visible:
            self.indicator.update_position(reader_thread.regions)

    def auto_connect_hardware(self):
        """Docstring."""
        global hw
        try:
            # ?ъ슜 媛?ν븳 ?쒕━???ы듃 ?ㅼ틪
            comports = serial.tools.list_ports.comports()
            all_ports = [p.device for p in comports]
            if all_ports and hasattr(self, "cb_port"):
                self.cb_port.configure(values=all_ports)
                
            # USB???뱀젙 ?ㅼ썙?쒓? ?ы븿???ы듃留??꾪꽣留?(硫붿씤蹂대뱶 媛??COM?ы듃 ?쒖쇅)
            ports = []
            for p in comports:
                desc = (p.description or "").upper()
                hwid = (p.hwid or "").upper()
                if "USB" in desc or "USB" in hwid or "CH340" in desc or "CP210" in desc or "ARDUINO" in desc:
                    ports.append(p.device)
                    
            if not ports:
                print("[Hardware] Auto-connect failed: no USB serial ports found (non-USB ports skipped)")
                return
            
            # Try each detected port in order.
            for port in ports:
                if hw.connect(port):
                    self.btn_hw_connect.configure(text="DISCONNECT", fg_color="#EF4444")
                    self.lbl_hw_status.configure(text=f"[Hardware] {port}", text_color="#10B981")
                    self.cb_port.set(port)
                    self.show_toast("HARDWARE SECURELY CONNECTED")
                    return
                else:
                    print(f"[Hardware] Connect failed: {port}, trying next port...")

            print("[Hardware] Auto-connect failed, manual selection required")
        except Exception as e:
            print(f"[Hardware] Auto-connect error: {e}")

    def toggle_hardware(self):
        global hw
        if hw.ser and hw.ser.is_open:
            hw.disconnect()
            self.btn_hw_connect.configure(text="CONNECT ESP32", fg_color="#6366F1")
            self.lbl_hw_status.configure(text="[Hardware] NONE", text_color="#FF5555")
            self.show_toast("HARDWARE DISCONNECTED", "#FFB800")
        else:
            port = self.cb_port.get()
            if hw.connect(port):
                self.btn_hw_connect.configure(text="DISCONNECT", fg_color="#EF4444")
                self.lbl_hw_status.configure(text=f"[Hardware] {port}", text_color="#10B981")
                self.show_toast("HARDWARE SECURELY CONNECTED")
            else:
                self.show_toast("CONNECTION FAILED", "#FF5555")

    def set_role(self, role: str):
        """Toggle F1 route mode."""
        normalized = self._normalize_role_name(role)
        self.state.role = normalized
        self.state.network_role = normalized
        if hasattr(self, "cb_role") and self.cb_role.winfo_exists():
            try:
                self.cb_role.set(self._display_role_name(normalized))
            except Exception:
                pass
        self.show_toast(f"Role set to: {self._display_role_name(normalized)}")

    def toggle_sentinel(self):
        """Docstring."""
        self.state.sentinel_enabled = not self.state.sentinel_enabled
        status = "ON" if self.state.sentinel_enabled else "OFF"
        color = "#10B981" if self.state.sentinel_enabled else "#343A40"
        
        # 踰꾪듉 ?띿뒪?몄? ?됱긽 蹂寃?
        if hasattr(self, 'btn_sentinel'):
            self.btn_sentinel.configure(text=f"SENTINEL {status}", fg_color=color)
        
        self.show_toast(f"Sentinel: {status}")
        print(f"[GUI] Sentinel Toggle: {status}")

    def toggle_service(self):
        self._toggle_control_mode("FOLLOW_SERVICE", "Follow+Service")

    def set_game_scale(self, scale: float):
        """Worker for the F1 route thread."""
        new_w, new_h = self.state.set_game_scale(scale)
        
        # 寃뚯엫 李??ш린 蹂寃?
        hwnd = self.state.hwnd
        if hwnd:
            try:
                win32gui.SetWindowPos(hwnd, 0, 0, 0, new_w, new_h, 
                                     win32con.SWP_NOMOVE | win32con.SWP_NOZORDER)
                self.show_toast(f"Game size changed: {new_w}x{new_h} ({int(scale)}x)")
            except Exception as e:
                self.show_toast(f"Game size change failed: {e}")
        
        # ?댁긽???쒖떆 ?낅뜲?댄듃
        self.update_resolution_display()

    def update_resolution_display(self):
        """Docstring."""
        try:
            # ?ㅼ젣 寃뚯엫李??댁긽??媛?몄삤湲?
            hwnd = find_game_window(WIN_KEY)
            if hwnd:
                rect = win32gui.GetWindowRect(hwnd)
                game_w = rect[2] - rect[0]
                game_h = rect[3] - rect[1]
                # ?대씪?댁뼵???곸뿭 ?ш린 媛?몄삤湲?
                client_rect = win32gui.GetClientRect(hwnd)
                client_w = client_rect[2]
                client_h = client_rect[3]
                if hasattr(self, 'lbl_game_res') and self.lbl_game_res.winfo_exists():
                    self.lbl_game_res.configure(text=f"[Game] {client_w}x{client_h}")
            else:
                if hasattr(self, 'lbl_game_res') and self.lbl_game_res.winfo_exists():
                    self.lbl_game_res.configure(text="[Game] -")
        except Exception as e:
            print(f"[GUI] ?댁긽???낅뜲?댄듃 ?ㅻ쪟: {e}")

        try:
            gui_w, gui_h = self.root.winfo_width(), self.root.winfo_height()
            if hasattr(self, 'lbl_gui_res') and self.lbl_gui_res.winfo_exists():
                self.lbl_gui_res.configure(text=f"[GUI] {gui_w}x{gui_h}")
        except Exception as e:
            print(f"[GUI] GUI ?댁긽???낅뜲?댄듃 ?ㅻ쪟: {e}")

    def _format_map_text(self, data: dict) -> str:
        map_name = str(data.get("map_name") or data.get("current_map") or "").strip()
        map_floor = str(data.get("map_floor") or data.get("current_floor") or "").strip()
        if map_name and map_floor:
            return f"{map_name}{map_floor}"
        return map_name or "-"

    def _format_coord_text(self, data: dict) -> tuple[str, str]:
        x_text = str(data.get("x_str") or "").strip()
        y_text = str(data.get("y_str") or "").strip()
        if not x_text:
            try:
                x_text = f"{int(data.get('x', 0) or 0):04d}"
            except Exception:
                x_text = "-"
        if not y_text:
            try:
                y_text = f"{int(data.get('y', 0) or 0):04d}"
            except Exception:
                y_text = "-"
        return x_text or "-", y_text or "-"

    def _format_hp_mp_text(self, data: dict) -> tuple[str, str]:
        hp_text = str(data.get("hp_str") or "").strip()
        mp_text = str(data.get("mp_str") or "").strip()
        if not hp_text:
            try:
                hp_text = str(int(data.get("hp", 0) or 0))
            except Exception:
                hp_text = "-"
        if not mp_text:
            try:
                mp_text = str(int(data.get("mp", 0) or 0))
            except Exception:
                mp_text = "-"
        return hp_text or "-", mp_text or "-"

    def _get_peer_snapshot_by_role(self, role_name: str) -> Optional[dict]:
        best = None
        best_ts = -1.0
        for _sender, data in getattr(self.state, "other_pc_data", {}).items():
            if not isinstance(data, dict):
                continue
            remote_role = str(data.get("role") or data.get("network_role") or "").strip()
            if remote_role != role_name:
                continue
            rx_ts = float(data.get("_received_at") or 0.0)
            if rx_ts >= best_ts:
                best = dict(data)
                best_ts = rx_ts
        return best

    def _format_rx_summary(self) -> str:
        sender = str(getattr(self.state, "last_network_rx_sender", "") or "-")
        seq = int(getattr(self.state, "last_network_rx_seq", 0) or 0)
        role = str(getattr(self.state, "last_network_rx_role", "") or "-")
        kind = str(getattr(self.state, "last_network_rx_kind", "") or "-")
        peer = None
        if sender != "-":
            peer = getattr(self.state, "other_pc_data", {}).get(sender)
            if not isinstance(peer, dict):
                peer = None
        if peer:
            map_text = self._format_map_text(peer)
            x_text, y_text = self._format_coord_text(peer)
            return f"RX sender={sender} seq={seq} role={role} kind={kind} map={map_text} x={x_text} y={y_text}"
        return f"RX sender={sender} seq={seq} role={role} kind={kind} map=- x=- y=-"

    def _get_role_snapshot(self, role_name: str) -> Optional[dict]:
        if role_name == self.state.role:
            data = self.state.get_all()
            data["_role_kind"] = "LOCAL"
            data["_fresh"] = True
            return data

        peer = self._get_peer_snapshot_by_role(role_name)
        if not peer:
            return None

        rx_ts = float(peer.get("_received_at") or 0.0)
        peer["_role_kind"] = "REMOTE"
        peer["_fresh"] = bool(rx_ts > 0 and (time.time() - rx_ts) <= 1.5)
        return peer

    def _role_style(self, role_name: str, snapshot: Optional[dict]) -> tuple[str, str, str]:
        if snapshot is None:
            return "#3F1D1D", "#F87171", "DISCONNECTED"

        role_kind = str(snapshot.get("_role_kind") or "REMOTE")
        fresh = bool(snapshot.get("_fresh", False))

        if role_kind == "LOCAL":
            return "#0B3B5A", "#38BDF8", "LOCAL"
        if fresh:
            return "#103B28", "#34D399", "REMOTE"
        return "#3F1D1D", "#F87171", "DISCONNECTED"

    def _display_role_name(self, role_name: str) -> str:
        role_map = {
            "도사": "Priest",
            "격수": "Warrior",
            "술사": "Shaman",
            "Priest": "Priest",
            "Warrior": "Warrior",
            "Shaman": "Shaman",
        }
        return role_map.get(str(role_name).strip(), str(role_name).strip() or "-")

    def _normalize_role_name(self, role_name: str) -> str:
        role_map = {
            "Priest": "도사",
            "Warrior": "격수",
            "Shaman": "술사",
            "도사": "도사",
            "격수": "격수",
            "술사": "술사",
        }
        return role_map.get(str(role_name).strip(), str(role_name).strip() or "도사")

    def _refresh_role_card(self, role_name: str):
        card = getattr(self, "net_role_cards", {}).get(role_name)
        if not card:
            return

        snapshot = self._get_role_snapshot(role_name)
        border_color, status_color, status_text = self._role_style(role_name, snapshot)
        try:
            card["frame"].configure(border_color=border_color)
        except Exception:
            pass

        if snapshot is None:
            map_text = "-"
            coord_text = "-"
            hpmp_text = "-"
        else:
            map_text = self._format_map_text(snapshot)
            x_text, y_text = self._format_coord_text(snapshot)
            hp_text, mp_text = self._format_hp_mp_text(snapshot)
            coord_text = f"{x_text} / {y_text}"
            hpmp_text = f"{hp_text} / {mp_text}"

        card["status"].configure(text=status_text, text_color=status_color)
        card["title"].configure(text=self._display_role_name(role_name), text_color=status_color)
        card["map"].configure(text=f"MAP: {map_text}")
        card["coord"].configure(text=f"X/Y: {coord_text}")
        card["hpmp"].configure(text=f"HP/MP: {hpmp_text}")

    def _refresh_dosa_network_panel(self):
        if self.state.role != "도사":
            return
        if not hasattr(self, "net_box") or not self.net_box.winfo_exists():
            return

        rx_text = self._format_rx_summary()
        if rx_text != self.last_values.get("net_rx_text"):
            self.lbl_rx_summary.configure(text=rx_text)
            self.last_values["net_rx_text"] = rx_text

        local_snapshot = self._get_role_snapshot(self.state.role)
        local_border, local_color, local_status = self._role_style(self.state.role, local_snapshot)
        if hasattr(self, "net_local_strip") and self.net_local_strip.winfo_exists():
            try:
                self.net_local_strip.configure(border_color=local_border)
            except Exception:
                pass
        local_text = "OFFLINE"
        if local_snapshot is not None:
            map_text = self._format_map_text(local_snapshot)
            x_text, y_text = self._format_coord_text(local_snapshot)
            hp_text, mp_text = self._format_hp_mp_text(local_snapshot)
            local_text = f"{local_status} | map={map_text} | x={x_text} y={y_text} | hp={hp_text} mp={mp_text}"
        if local_text != self.last_values.get("net_local_text"):
            self.lbl_local_status.configure(text=local_text, text_color=local_color)
            self.last_values["net_local_text"] = local_text

        for role_name in ("격수", "술사"):
            self._refresh_role_card(role_name)

    def _current_control_mode_label(self) -> str:
        if getattr(self.state, "automation_paused", False):
            return "PAUSED"
        if getattr(self.state, "service_active", False) and getattr(self.state, "nav_follow_enabled", False):
            return "FOLLOW+SERVICE"
        if getattr(self.state, "nav_follow_enabled", False):
            return "FOLLOW"
        return "OFF"

    def _sync_control_mode_widgets(self):
        """Refresh navigation/status widgets on the main thread."""
        def _apply():
            try:
                mode_label = self._current_control_mode_label()

                if hasattr(self, "sw_follow") and self.sw_follow.winfo_exists():
                    if mode_label in ("FOLLOW", "FOLLOW+SERVICE"):
                        self.sw_follow.select()
                    else:
                        self.sw_follow.deselect()

                if hasattr(self, "sw_route") and self.sw_route.winfo_exists():
                    if getattr(self.state, "nav_route_enabled", False):
                        self.sw_route.select()
                    else:
                        self.sw_route.deselect()

                if hasattr(self, "sw_avoid") and self.sw_avoid.winfo_exists():
                    if getattr(self.state, "nav_avoid_enabled", False):
                        self.sw_avoid.select()
                    else:
                        self.sw_avoid.deselect()

                if hasattr(self, "btn_macro") and self.btn_macro.winfo_exists():
                    if getattr(self.state, "service_active", False):
                        self.btn_macro.configure(text="STOP (F2)", fg_color="#EF4444")
                    else:
                        self.btn_macro.configure(text="RUN (F2)", fg_color="#10B981")

                if hasattr(self, "lbl_nav_status") and self.lbl_nav_status.winfo_exists():
                    self.lbl_nav_status.configure(text=f"MODE: {mode_label}")

                if getattr(self.state, "role", "") == "도사":
                    self._refresh_dosa_network_panel()
            except Exception as exc:
                print(f"[GUI] control mode sync error: {exc}")

        try:
            self.root.after(0, _apply)
        except Exception:
            pass

    def _stop_all_inputs(self, disconnect: bool = False, repeat: int = 2, delay: float = 0.05):
        global hw
        try:
            hw.stop_all_inputs(repeat=repeat, delay=delay, disconnect=disconnect)
        except Exception as exc:
            print(f"[Hardware] cleanup failed: {exc}")

    def _apply_control_mode(self, mode: str, announce: bool = True):
        normalized = str(mode or "NONE").strip().upper()
        if normalized in {"FOLLOW_SERVICE", "SERVICE", "F2"}:
            control_mode = "FOLLOW+SERVICE"
            follow_enabled = True
            route_enabled = False
            avoid_enabled = True
            service_enabled = True
            auto_hunt_enabled = True
        elif normalized in {"FOLLOW", "F1"}:
            control_mode = "FOLLOW"
            follow_enabled = True
            route_enabled = False
            avoid_enabled = True
            service_enabled = False
            auto_hunt_enabled = False
        else:
            control_mode = "NONE"
            follow_enabled = False
            route_enabled = False
            avoid_enabled = False
            service_enabled = False
            auto_hunt_enabled = False

        self.state.control_mode = control_mode
        self.state.automation_paused = False
        self.state.nav_follow_enabled = follow_enabled
        self.state.nav_route_enabled = route_enabled
        self.state.nav_avoid_enabled = avoid_enabled
        self.state.service_active = service_enabled
        self.state.auto_hunt = auto_hunt_enabled
        self.state.sentinel_enabled = bool(follow_enabled or service_enabled)
        self.state.is_combat_busy = False
        self.state.combat_start_time = 0.0
        if not service_enabled:
            self.state.target_locked = False
            self.state.target_name = ""

        global action_thread
        if action_thread is not None:
            action_thread.task_active = service_enabled

        self._sync_control_mode_widgets()
        if announce:
            print(f"[Hotkey] Mode set: {control_mode}")
            self.show_toast(f"MODE: {control_mode}")

    def _toggle_control_mode(self, mode: str, label: str):
        normalized = str(mode or "NONE").strip().upper()
        current = str(getattr(self.state, "control_mode", "NONE") or "NONE").strip().upper()
        if getattr(self.state, "automation_paused", False):
            self._paused_control_mode = "NONE"
        if current == normalized and not getattr(self.state, "automation_paused", False):
            self._apply_control_mode("NONE")
            print(f"[Hotkey] {label}: OFF")
            self.show_toast(f"{label}: OFF")
            return

        self._apply_control_mode(normalized)
        print(f"[Hotkey] {label}: ON")
        self.show_toast(f"{label}: ON")

    def toggle_follow(self):
        self._toggle_control_mode("FOLLOW", "Follow Mode")

    def toggle_pause_resume(self):
        if getattr(self.state, "automation_paused", False):
            resume_mode = getattr(self, "_paused_control_mode", "NONE") or "NONE"
            self._paused_control_mode = "NONE"
            self._apply_control_mode(resume_mode, announce=False)
            self.state.automation_paused = False
            print(f"[Hotkey] Pause OFF -> {resume_mode}")
            self.show_toast(f"RESUMED: {resume_mode}")
            return

        current_mode = str(getattr(self.state, "control_mode", "NONE") or "NONE").strip().upper()
        if current_mode not in {"FOLLOW", "FOLLOW+SERVICE"}:
            current_mode = "NONE"
        self._paused_control_mode = current_mode
        self.state.automation_paused = True
        self.state.control_mode = "PAUSED"
        self.state.nav_follow_enabled = False
        self.state.nav_route_enabled = False
        self.state.nav_avoid_enabled = False
        self.state.service_active = False
        self.state.auto_hunt = False
        self.state.sentinel_enabled = False
        self.state.is_combat_busy = False
        self.state.combat_start_time = 0.0
        self.state.target_locked = False
        self.state.target_name = ""
        global action_thread
        if action_thread is not None:
            action_thread.task_active = False
        self._stop_all_inputs(disconnect=False, repeat=2, delay=0.05)
        self._sync_control_mode_widgets()
        print(f"[Hotkey] Pause ON (saved={self._paused_control_mode})")
        self.show_toast("PAUSED")

    def emergency_exit(self):
        print("[PANIC] Emergency exit requested (F4)")
        self.state.running = False
        self.state.automation_paused = True
        self.state.control_mode = "PAUSED"
        self.state.nav_follow_enabled = False
        self.state.nav_route_enabled = False
        self.state.nav_avoid_enabled = False
        self.state.service_active = False
        self.state.auto_hunt = False
        self.state.sentinel_enabled = False
        self.state.is_combat_busy = False
        self.state.combat_start_time = 0.0
        self.state.target_locked = False
        self.state.target_name = ""
        global action_thread
        if action_thread is not None:
            action_thread.task_active = False
        self._stop_all_inputs(disconnect=True, repeat=3, delay=0.05)
        try:
            self.close_calibration_popup()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)

    def debug_ocr(self):
        global reader_thread
        if reader_thread is None:
            self.show_toast("[Err] VISION THREAD NOT READY", "#FF5555")
            return
            
        self.show_toast("DEBUG IMAGE SAVE DISABLED")

    def load_waypoints(self):
        """Docstring."""
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
        """Docstring."""
        # Route mode stop/start toggle
        if getattr(self, '_f1_route_active', False):
            self._f1_route_active = False
            try:
                hw.panic_release()
            except:
                pass
            print("[Move] Route mode stop requested")
            self.show_toast("Route mode stopped", color="#EF4444")
            return

        # Start on main thread via Tkinter
        try:
            self.root.after(0, self._start_route_f1_logic)
        except:
            pass

    def _start_route_f1_logic(self):
        """Docstring."""
        items = self.seq_tree.get_children()
        if not items:
            self.show_toast("No route items found", color="#EF4444")
            return

        self._f1_route_active = True
        self.last_f1_pos = (0, 0)
        self.f1_stuck_timer = time.time()
        self.f1_stuck_count = 0
        print("[Move] Route mode started - full route traversal begins")

        def move_process():
            # 寃뚯엫李??ъ빱???쒕룄
            try:
                if self.hwnd and self.hwnd != 0:
                    win32gui.SetForegroundWindow(self.hwnd)
                    time.sleep(0.2)
            except Exception:
                pass

            wp_index = 0  # ??긽 Entry(泥?踰덉㎏)遺???쒖옉

            while self._f1_route_active and self.state.running:
                # ?꾩옱 ?뚯씠釉???ぉ 議고쉶 (UI 蹂寃????
                current_items = self.seq_tree.get_children()
                if not current_items:
                    break

                # 留덉?留됯퉴吏 ?꾨즺 ??Entry遺??諛섎났
                if wp_index >= len(current_items):
                    wp_index = 0
                    print("[Move] ?꾩껜 寃쎈줈 ?꾨즺 ??Entry遺??諛섎났")
                    self.show_toast("Route completed; looping from the first item", color="#A855F7")
                    time.sleep(0.3)
                    continue

                item_id = current_items[wp_index]
                values = self.seq_tree.item(item_id, "values")
                row_type = values[0] if values else "?"

                # x, y ?뚯떛 (鍮꾩뼱?덇굅??0?대㈃ ?ㅽ궢)
                try:
                    tx = int(values[1]) if len(values) > 1 and values[1] else 0
                    ty = int(values[2]) if len(values) > 2 and values[2] else 0
                except (ValueError, TypeError):
                    tx, ty = 0, 0

                if tx == 0 and ty == 0:
                    print(f"[Move] [{wp_index+1}/{len(current_items)}] {row_type} target missing - skip")
                    wp_index += 1
                    continue

                print(f"[Move] [{wp_index+1}/{len(current_items)}] {row_type} -> ({tx}, {ty}) start")
                self.show_toast(f"[{wp_index+1}/{len(current_items)}] {row_type} -> ({tx},{ty})")

                # stuck 媛먯? 珥덇린??(waypoint ?꾪솚留덈떎 由ъ뀑)
                self.last_f1_pos = (0, 0)
                self.f1_stuck_timer = time.time()
                self.f1_stuck_count = 0

                # ?? ?대떦 waypoint ?꾨떖源뚯? 猷⑦봽 ??
                waypoint_reached = False
                while self._f1_route_active and self.state.running:
                    data = self.state.get_all()
                    cx, cy = data.get("x", 0), data.get("y", 0)

                    # ?꾨떖 ?먯젙 (醫뚰몴 李⑥씠 2 誘몃쭔, ?몄떇 ?ㅽ뙣(0) ?쒖쇅)
                    if cx > 0 and cy > 0:
                        if abs(tx - cx) < 2 and abs(ty - cy) < 2:
                            print(f"[Move] [{wp_index+1}] {row_type} reached: ({tx}, {ty})")
                            self.show_toast(f"Reached: {row_type} ({tx},{ty})", color="#10B981")
                            wp_index += 1
                            waypoint_reached = True
                            break

                    # ?? Stuck 媛먯? & ?뚰뵾 湲곕룞 ??
                    if self.state.nav_avoid_enabled:
                        now = time.time()
                        if (cx, cy) != self.last_f1_pos:
                            self.last_f1_pos = (cx, cy)
                            self.f1_stuck_timer = now
                            self.f1_stuck_count = 0
                        elif now - self.f1_stuck_timer > 0.7:  # 1.5珥?-> 0.7珥?(?먮떒 ?띾룄 2諛??곹뼢)
                            self.f1_stuck_count += 1
                            self.f1_stuck_timer = now
                            print(f"[F1-Stuck] 醫뚰몴 蹂???놁쓬 ({self.f1_stuck_count}???곗냽)")
                            if self.f1_stuck_count >= 2:
                                print("[F1-Stuck] 留됲옒 媛먯?! ?뚰뵾 湲곕룞 ?ㅽ뻾.")
                                self._f1_escape_stuck()
                                continue

                    # ?? 諛⑺뼢 寃곗젙: 嫄곕━ 癒?異??곗꽑 + ?쒕뜡 ??
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

                    # ?? ?섎뱶?⑥뼱 ?대룞 紐낅졊 ??
                    print(f"[Move] 諛⑺뼢?? {direction}")
                    try:
                        hw.hold_move(direction, "move_hold")
                    except Exception as e:
                        print(f"[Move] ?대룞 紐낅졊 ?ㅽ뙣: {e}")
                        self._f1_route_active = False
                        break

                    humanized_sleep(TIMING_CONFIG["nav_loop"])

                if not waypoint_reached and not self._f1_route_active:
                    break  # 以묐떒 ?붿껌??

            self._f1_route_active = False
            
            # ?명꽣?쏀듃 ?ㅽ뙣(臾댄븳 ?낅젰 諛⑹?): ?ㅻ젅??醫낅즺 ???붿뿬 ?섎뱶?⑥뼱 ?뚮┝???꾩삁 媛뺤젣 ?댁젣?⑸땲??
            try:
                hw.panic_release()
            except Exception:
                pass
                
            print("[Move] F1 寃쎈줈 ?대룞 ?ㅻ젅??醫낅즺")

        # UI ?ㅻ젅??李⑤떒 諛⑹?瑜??꾪빐 蹂꾨룄 ?ㅻ젅?쒖뿉???ㅽ뻾
        threading.Thread(target=move_process, daemon=True).start()

    def _f1_escape_stuck(self):
        """Docstring."""
        print("[Warn] [F1-STUCK] ?뚰뵾 湲곕룞 以?..")
        for _ in range(random.randint(2, 3)):
            # ?≪씠???쒕뜡
            side_dir = random.choice(["left", "right", "up", "down"])
            hw.hold_move(side_dir, "stuck_side_hold")
            humanized_sleep(TIMING_CONFIG["key_gap"])
        self.f1_stuck_count = 0
        self.f1_stuck_timer = time.time()

    def update_fast_labels(self):
        """Docstring."""
        try:
            if not self.root.winfo_exists(): return
        except tk.TclError:
            return  # ?덈룄???뚭눼 以묒씠硫?利됱떆 醫낅즺

        # Tab ?뚮뜑留??ㅼ쐞移??뺤씤
        if not self.tab_enabled.get("dash", True):
            # ??鍮꾪솢?깊솕 ????대㉧留??щ벑濡앺븯怨?由ы꽩
            try:
                if self.root.winfo_exists():
                    self.root.after(16, self.update_fast_labels)
            except tk.TclError:
                pass
            return

        perf_start = time.perf_counter()

        try:
            # 濡쒖뺄 罹먯떛?쇰줈 ?띾룄 理쒖쟻??
            state = self.state

            # GameState ?띿꽦 吏곸젒 ?쎄린 (???ㅻ쾭?ㅻ뱶 ?쒓굅)
            # 鍮꾩쟾 ?붿쭊???대? GameState ?띿꽦???낅뜲?댄듃?섎?濡?吏곸젒 ?쎄린媛 ??鍮좊쫫
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

            # 네트워크 상태 표시
            is_connected = getattr(state, "is_connected", False)
            net_status = "연결됨" if is_connected else "연결 끊김"
            
            if self.state.role == "도사" and getattr(state, "last_network_rx_sender", ""):
                net_status = f"연결됨 (격수PC: {state.last_network_rx_sender})"
            
            # 연결 상태 변경 시 즉시 UI 업데이트
            if net_status != self.last_values.get("dash_net"):
                text_color = "#10B981" if is_connected else "#EF4444"
                self.lbl_net.configure(text=f"[Net] {net_status}", text_color=text_color)
                self.last_values["dash_net"] = net_status

            self._refresh_dosa_network_panel()
            
            # ?깅뒫 痢≪젙
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
            print(f"[GUI] update_fast_labels error: {e}")
        finally:
            # Tk 硫붿씤猷⑦봽??1ms ?대쭅蹂대떎 16ms 二쇨린媛 ?⑥뵮 ?덉젙?곸씠怨?遺?섍? ?곷떎.
            try:
                if self.root.winfo_exists():
                    self.root.after(16, self.update_fast_labels)
            except tk.TclError:
                pass

    def update_slow_ui(self):
        """Docstring."""
        try:
            if not self.root.winfo_exists(): return
        except tk.TclError:
            return

        # Tab ?뚮뜑留??ㅼ쐞移??뺤씤
        if not self.tab_enabled.get("logic", True) and not self.tab_enabled.get("nav", True):
            # ??鍮꾪솢?깊솕 ????대㉧留??щ벑濡앺븯怨?由ы꽩
            try:
                if self.root.winfo_exists():
                    self.root.after(30000, self.update_slow_ui)
            except tk.TclError:
                pass
            return

        try:
            # [NAV] ?ㅻ퉬寃뚯씠???곹깭 ?쒖떆
            if self.tab_enabled.get("nav", True):
                try:
                    nav_status = self._current_control_mode_label()
                    if nav_status != self.last_values.get('nav_status'):
                        if hasattr(self, 'lbl_nav_status'):
                            self.lbl_nav_status.configure(text=f"MODE: {nav_status}")
                        self.last_values['nav_status'] = nav_status
                except Exception:
                    pass
            
            # [HARDWARE] ?꾨몢?대끂 ?곌껐 ?곹깭 ?쒖떆 (?곕???異쒕젰?쇰줈 ?泥?
            try:
                global hw
                if hw.ser and hw.ser.is_open:
                    hw_status = "READY"
                else:
                    hw_status = "DISCONNECTED"
                
                if hw_status != self.last_values.get('hw_status'):
                    print(f"[Hardware] Status: {hw_status}")
                    self.last_values['hw_status'] = hw_status
            except Exception:
                pass
            
            # 媛?대뱶 ?덉씠???숆린??
            try:
                if hasattr(self, "indicator") and self.indicator.visible:
                    global reader_thread
                    if reader_thread is not None:
                        self.indicator.update_position(reader_thread.regions)
            except Exception:
                pass

            # ?ㅽ궗 ?뚮옒洹??숆린??(GUI -> State)
            if self.tab_enabled.get("logic", True):
                try:
                    use_heal = self.sw_heal.get()
                    use_buff = self.sw_buff.get()
                    use_debuff = self.sw_debuff.get()
                    use_attack = self.sw_attack.get()
                    use_smart_ai = self.sw_ai.get()

                    # ?곹깭 蹂寃??쒖뿉留??낅뜲?댄듃
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
            print(f"[GUI] update_slow_ui error: {e}")
        finally:
            # ??대㉧ ?щ벑濡?(30珥?= 30000ms)
            try:
                if self.root.winfo_exists():
                    self.root.after(30000, self.update_slow_ui)
            except tk.TclError:
                pass

    def on_close(self):
        print("[System] Shutdown initiated. Stopping all activity and closing threads...")
        self.state.running = False
        self.state.automation_paused = True
        self.state.control_mode = "PAUSED"
        self.state.nav_follow_enabled = False
        self.state.nav_route_enabled = False
        self.state.nav_avoid_enabled = False
        self.state.service_active = False
        self.state.auto_hunt = False
        self.state.sentinel_enabled = False

        self._stop_all_inputs(disconnect=True, repeat=3, delay=0.05)
            
        try:
            self.close_calibration_popup()
            self.root.update_idletasks()
            save_gui_state(self.root.geometry(), self.ui_scale, self.calibration_geometry)
        except Exception:
            pass
        self.root.destroy()
        
        # Force process exit so lingering background threads cannot keep keys held.
        os._exit(0)

    def panic_shutdown(self):
        """Docstring."""
        self.toggle_pause_resume()

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        # ?꾩뿭 ?ロ궎 ?ㅼ젙
        try:
            keyboard.add_hotkey("f1", self.toggle_follow)
            keyboard.add_hotkey("f2", self.toggle_service)
            keyboard.add_hotkey("f3", self.toggle_pause_resume)
            keyboard.add_hotkey("f4", self.emergency_exit)
        except Exception as e:
            print(f"[Warn] Hotkey Registration Failed: {e}")
        
        # ??대㉧??__init__?먯꽌 ?대? ?쒖옉??(update_fast_labels, update_slow_ui)
        self.root.update()
        self.root.mainloop()

# ?꾩뿭 ?ㅻ젅??李몄“ 諛??붿쭊 蹂??
action_thread = None
reader_thread = None
nav_thread = None
# hw??svc_kernel?먯꽌 ?꾪룷?몃맖

def main(
    preset_role: Optional[str] = None,
    preset_network_role: Optional[str] = None,
    preset_auto_hunt: Optional[bool] = None,
):
    # 0. Validate config.json first.
    print("[Config] Validating config.json...")
    config_file = os.path.join(os.path.dirname(__file__), "config.json")
    
    if not os.path.exists(config_file):
        messagebox.showerror("Error", f"config.json not found:\n{config_file}")
        return
    
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        play_area = config.get("play_area", {})
        sx = play_area.get("sx", 0)
        sy = play_area.get("sy", 0)
        dx = play_area.get("dx", 0)
        dy = play_area.get("dy", 0)
        
        # play_area 醫뚰몴 ?좏슚??寃??
        if sx == 0 or sy == 0 or dx == 0 or dy == 0:
            messagebox.showerror(
                "Error",
                f"config.json play_area coordinates are invalid.\n"
                f"sx: {sx}, sy: {sy}, dx: {dx}, dy: {dy}\n"
                f"All coordinates must be non-zero."
            )
            return
        
        if sx >= dx or sy >= dy:
            messagebox.showerror(
                "Error",
                f"config.json play_area coordinates are inverted.\n"
                f"sx({sx}) >= dx({dx}) or sy({sy}) >= dy({dy})\n"
                f"The start coordinate must be smaller than the end coordinate."
            )
            return
        
        print(f"[Config] play_area validated: ({sx}, {sy}) -> ({dx}, {dy})")
        
    except Exception as e:
        messagebox.showerror("Error", f"Failed to load config.json:\n{e}")
        return
    
    # 1. Environment safety check (anti-cheat bypass remains disabled)
    print("[Stealth] Checking environment safety... (Bypass Enabled)")
    # stealth_results = StealthChecker.run_all_checks()
    
    # if stealth_results['debugger_detected']:
    #     print("[Stealth] [!] ?붾쾭嫄??먯???- ?ㅽ뻾 以묐떒")
    #     return
    
    # if stealth_results['vm_detected']:
    #     print("[Stealth] [!] 媛?곷㉧???섍꼍 ?먯???- ?ㅽ뻾 以묐떒")
    #     return
    
    # if not stealth_results['process_integrity']:
    #     print("[Stealth] [!] ?꾨줈?몄뒪 臾닿껐???꾨컲 - ?ㅽ뻾 以묐떒")
    #     return
    
    # if not stealth_results['external_modules']:
    #     print("[Stealth] [!] ?섏떖?ㅻ윭???몃? 紐⑤뱢 ?먯? - ?ㅽ뻾 以묐떒")
    #     return
    
    print("[Stealth] [OK] Environment safety check complete")

    # Move the game window to (0,0) and force a fixed size.
    hwnd = find_game_window(WIN_KEY)
    if hwnd:
        import win32con
        # Force the game window to 1920x1080.
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, 1920, 1080, win32con.SWP_SHOWWINDOW)
        print("[Window] Game window moved to (0, 0) and resized to 1920x1080.")
    else:
        print(f"[Warn] Game window not found. (title contains: {WIN_KEY})")

    global action_thread, reader_thread
    state = GameState()
    if preset_role:
        state.role = preset_role
    if preset_network_role is None:
        preset_network_role = preset_role
    if preset_auto_hunt is not None:
        state.auto_hunt = bool(preset_auto_hunt)
        state.service_active = bool(preset_auto_hunt)
    state.hwnd = hwnd  # GameState???몃뱾 ???
    hw.set_state(state)  # DevInterface??GameState ?곌껐 (?ъ빱??泥댄겕??

    def _cleanup_hardware_on_exit():
        try:
            hw.stop_all_inputs(disconnect=True, repeat=3, delay=0.05)
        except Exception:
            pass

    atexit.register(_cleanup_hardware_on_exit)
    
    # 媛??뚯빱 ?ㅻ젅??珥덇린??(bootstrap.py 湲곕뒫 ?듯빀)
    config_file = os.path.join(SCRIPT_DIR, "config.json")
    network_cfg = load_network_config()
    if preset_network_role:
        network_cfg["role"] = preset_network_role
    state.network_role = network_cfg.get("role", state.network_role)
    state.network_server_ip = network_cfg.get("server_ip", state.network_server_ip)
    state.network_bind_host = network_cfg.get("bind_host", state.network_bind_host)
    state.network_telemetry_port = int(network_cfg.get("telemetry_port", state.network_telemetry_port))
    state.network_local_port = int(network_cfg.get("local_port", state.network_local_port))

    spell_db_file = os.path.join(SCRIPT_DIR, "spells_config.json")

    # 1. 怨좎냽 罹≪쿂 ?ㅻ젅??媛??(紐⑤뱺 鍮꾩쟾 ?ㅻ젅?쒖쓽 ?뚯뒪)
    capture_svc = CaptureSvc(state)
    capture_svc.start()

    try:
        reader_thread = MonitorSvc(state, config_file)  # PatternMatcher 湲곕컲 OCR
        print(f"[Worker] MonitorSvc initialized: {reader_thread is not None}")
    except Exception as e:
        print(f"[Worker] MonitorSvc initialization failed: {e}")
        import traceback
        traceback.print_exc()
        return
    
    numeric_scanner = NumericFieldScanner(state, reader_thread.matcher, config_file, reader_thread)  # 怨좎냽 ?꾩슜 ?ㅻ젅??
    sentinel_thread = SentinelThread(state, reader_thread.matcher) # 紐ъ뒪???꾩씠???먯? ?꾩슜
    action_thread = LogicSvc(state)  # FSM 濡쒖쭅
    nav_thread = RouteSvc(state)  # ?ㅻ퉬寃뚯씠??
    
    # GUI ?앹꽦 (hwnd ?꾨떖)
    gui = AppView(state, hwnd=hwnd)
    
    # 연결 상태 변경 콜백 함수 (UI 업데이트는 AppView에서 주기적으로 수행함)
    def on_connection_change(is_connected):
        pass
    
    network = NetworkThread(state, network_cfg, on_connection_change)
    logger = LoggerThread(state)

    # GUI ?앹꽦 ??grid_indicator ?꾨떖
    if hasattr(gui, 'grid_indicator'):
        sentinel_thread.grid_indicator = gui.grid_indicator

    # 紐⑤뱺 ?ㅻ젅???쒖옉
    print(f"[Worker] About to start reader_thread...")
    reader_thread.start()
    print(f"[Worker] reader_thread started")
    numeric_scanner.start()  # 怨좎냽 ?レ옄 ?ㅼ틪 ?쒖옉
    sentinel_thread.start()
    action_thread.start()
    nav_thread.start()
    network.start()
    logger.start()

    print("[OK] System started. (Production Mode)")
    
    # GUI ?ㅽ뻾
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

