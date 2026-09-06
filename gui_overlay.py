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
from svc_numeric_scanner import NumericFieldScanner

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
from svc_monitor import MonitorSvc
from svc_sentinel import SentinelThread
from svc_capture import CaptureSvc
from svc_logic import LogicSvc
from svc_route import RouteSvc

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

from config_utils import grab_window_bg, load_config, save_config, dict_to_region

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
                print(f"[DEBUG] ROI: ({sx},{sy})-({dx},{dy}) | Adjusted: ({csx},{csy})-({cdx},{cdy})")
                if self.callback:
                    self.callback(sx, sy, dx, dy, crop)
            except Exception as e:
                print(f"[Overlay] Capture error: {e}")
        self.destroy()

    def on_cancel(self, event=None):
        self.destroy()

class ROIIndicator(tk.Toplevel):
    """Docstring."""
    def __init__(self, parent, hwnd, state):
        super().__init__(parent)
        self.hwnd = hwnd
        self.state = state
        self.visible = False
        
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-transparentcolor", "black")
        self.configure(bg="black")
        
        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.withdraw()

    def update_position(self, regions):
        """실제로 그린 마커 개수를 돌려준다 (호출부 진단용)."""
        has_markers = len(self.state.visual_markers) > 0
        if not self.visible and not has_markers:
            self.withdraw()
            return 0

        try:
            # ROI 사각형도 마커도 좌표계가 '캡처 프레임 = 화면'이다
            # (hw.click_pixel이 같은 값을 그대로 SetCursorPos에 넣는다).
            # 예전엔 오버레이를 게임창 '클라이언트' 영역에 맞췄는데, 그러면
            # 창 테두리 높이만큼 전부 어긋난다. 이 PC는 게임을 크롬 탭으로
            # 띄워서 find_game_window_any가 크롬 창("...바람의나라 클래식
            # - Chrome")을 게임창으로 잡았고, 그래서 마젠타 점이 엉뚱한
            # 자리로 밀려 안 보였다. 화면 전체를 덮으면 변환이 필요 없다.
            w, h = self.winfo_screenwidth(), self.winfo_screenheight()
            self.geometry(f"{w}x{h}+0+0")
            self.deiconify()
            self.lift()
            self.canvas.delete("all")
            
            if self.visible:
                colors = {"hp": "#FF4B4B", "mp": "#00D4FF", "exp": "#A855F7", "money": "#FFB800", "x": "#10B981", "y": "#10B981"}
                for name, reg in regions.items():
                    color = colors.get(name, "#FFFFFF")
                    self.canvas.create_rectangle(reg.sx - 3, reg.sy - 3, reg.dx + 3, reg.dy + 3, outline=color, width=2)
            
            now = time.time()
            markers = []
            with self.state._lock:
                markers = list(self.state.visual_markers)
                self.state.visual_markers = [m for m in self.state.visual_markers if m["expiry"] > now]

            drawn = 0
            for m in markers:
                if m["expiry"] > now:
                    r = m.get("size", 10) // 2
                    color = m.get("color", "red")
                    if color == "magenta":
                        continue
                    else:
                        self.canvas.create_oval(m["x"] - r, m["y"] - r, m["x"] + r, m["y"] + r, fill=color, outline="white", width=1)
                    drawn += 1
            return drawn
        except Exception as e:
            print(f"[Overlay] ROI update error: {e}")
            self.withdraw()
            return 0

class GridIndicator(tk.Toplevel):
    """Docstring."""
    def __init__(self, parent, hwnd, state):
        super().__init__(parent)
        self.hwnd = hwnd
        self.state = state
        self.visible = False
        self.grid_size = 48.2
        self.offset_x = 0
        self.offset_y = 0
        self.grid_alpha = 0.5
