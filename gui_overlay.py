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

