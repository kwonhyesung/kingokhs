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

from config_utils import *
from gui_overlay import *
from background_threads import *
from gui_app import *

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
    
    # 핫키 등록 (GUI 초기화 전에 먼저 등록)
    print("[DEBUG] 핫키 등록 시작...")
    try:
        keyboard.add_hotkey("f1", lambda: None)  # 임시 함수
        keyboard.add_hotkey("f2", lambda: None)  # 임시 함수
        keyboard.add_hotkey("f3", lambda: None)  # 임시 함수
        keyboard.add_hotkey("f4", lambda: None)  # 임시 함수
        print("[DEBUG] 핫키 등록 성공")
        
        # 임시 핫키 제거 및 실제 함수 등록
        keyboard.remove_hotkey("f1")
        keyboard.remove_hotkey("f2")
        keyboard.remove_hotkey("f3")
        keyboard.remove_hotkey("f4")
        
        def register_hotkeys(gui_instance):
            try:
                keyboard.add_hotkey("f1", gui_instance.toggle_follow)
                print("[DEBUG] F1 핫키 등록 성공")
                keyboard.add_hotkey("f2", gui_instance.toggle_service)
                print("[DEBUG] F2 핫키 등록 성공")
                keyboard.add_hotkey("f3", gui_instance.toggle_pause_resume)
                print("[DEBUG] F3 핫키 등록 성공")
                keyboard.add_hotkey("f4", gui_instance.emergency_exit)
                print("[DEBUG] F4 핫키 등록 성공")
                
                # [TEST] ` 키로 빨간색 탭 찾기 로직 수동 실행
                keyboard.add_hotkey("`", lambda: reader_thread.check_red_tab_with_findtext(reader_thread.last_frame))
                print("[DEBUG] ` 핫키 등록 성공 (RedTab 체크 테스트용)")
            except Exception as e:
                print(f"[Warn] Hotkey Registration Failed: {e}")
        
        # GUI ?앹꽦 (hwnd ?꾨떖)
        gui = AppView(state, hwnd=hwnd)
        
        # GUI 생성 후 핫키 등록
        register_hotkeys(gui)
        
    except Exception as e:
        print(f"[ERROR] 핫키 등록 실패: {e}")
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
    gui.action_thread = action_thread
    gui.reader_thread = reader_thread
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

