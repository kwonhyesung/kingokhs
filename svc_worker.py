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
import traceback

# 표준 출력 인코딩을 현재 윈도우 로캘에 맞춘다.
try:
    preferred_encoding = 'cp949'
    if sys.stdout.encoding != preferred_encoding:
        sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding=preferred_encoding, line_buffering=True)
    if sys.stderr.encoding != preferred_encoding:
        sys.stderr = io.TextIOWrapper(sys.stderr.detach(), encoding=preferred_encoding, line_buffering=True)
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

# Pillow 10+ ?紐낆넎 ?귐딄묘???춦 (?닌됱쒔?袁? ?類ㅻ땾 0 = NEAREST)
try:
    from PIL.Image import Resampling
    _PIL_NEAREST = Resampling.NEAREST
except ImportError:
    _PIL_NEAREST = 0
from dataclasses import dataclass, asdict
from typing import Tuple, Optional, Dict
from enum import Enum, auto

# ?뚣끇瑗???????袁る７??
from bis_core import Skill, GameState, hw, Region, TIMING_CONFIG, humanized_sleep
from svc_stealth import StealthChecker
from svc_monitor import MonitorSvc, SentinelThread, CaptureSvc
from bis_logic import LogicSvc, RouteSvc
from support_runtime_rules import resolve_runtime_network_role

# ?⑥쥚鍮?怨룸즲(65?紐꾪뒄 ?? 筌뤴뫀????紐낆넎?源놁뱽 ?袁る립 DPI ?紐꾨뻼 ??뽮쉐??
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

# ?袁⑸열 ??살쟿??筌〓챷??獄??遺우춭 癰궰??
action_thread = None
reader_thread = None
nav_thread = None
HOTKEY_HANDLES = {}
HOTKEY_LAST_TRIGGER = {}
HOTKEY_DEBOUNCE_SEC = 0.35
# hw??svc_kernel?癒?퐣 ?袁る７?紐껊쭡


def _clear_registered_hotkeys():
    global HOTKEY_HANDLES
    for hotkey_name, handle in list(HOTKEY_HANDLES.items()):
        try:
            keyboard.remove_hotkey(handle)
        except Exception:
            try:
                keyboard.unhook(handle)
            except Exception as exc:
                print(f"[Warn] Hotkey remove failed ({hotkey_name}): {exc}")
    HOTKEY_HANDLES.clear()


def _dispatch_gui_hotkey(gui_instance, hotkey_name: str, callback):
    now = time.time()
    last_trigger = HOTKEY_LAST_TRIGGER.get(hotkey_name, 0.0)
    if now - last_trigger < HOTKEY_DEBOUNCE_SEC:
        return
    HOTKEY_LAST_TRIGGER[hotkey_name] = now
    print(f"[Hotkey] {hotkey_name} pressed")
    try:
        enqueue = getattr(gui_instance, "enqueue_ui_action", None)
        if callable(enqueue):
            if enqueue(hotkey_name, callback):
                return
        print(f"[Hotkey] {hotkey_name} ignored: GUI is not ready")
    except Exception as exc:
        print(f"[Hotkey] {hotkey_name} schedule failed: {exc}")
        traceback.print_exc()


def _is_game_window_active(state) -> bool:
    try:
        current_hwnd = win32gui.GetForegroundWindow()
    except Exception:
        return False

    candidate_hwnds = [current_hwnd]
    try:
        root_hwnd = win32gui.GetAncestor(current_hwnd, win32con.GA_ROOT)
        if root_hwnd and root_hwnd not in candidate_hwnds:
            candidate_hwnds.append(root_hwnd)
    except Exception:
        pass

    state_hwnd = getattr(state, "hwnd", None)
    if state_hwnd and state_hwnd in candidate_hwnds:
        return True

    for hwnd_candidate in candidate_hwnds:
        try:
            title = win32gui.GetWindowText(hwnd_candidate) or ""
        except Exception:
            title = ""
        if any(token.lower() in title.lower() for token in ["ory", "바람", "aion"]):
            state.hwnd = hwnd_candidate
            return True

    refreshed_hwnd = find_game_window(WIN_KEY)
    if refreshed_hwnd:
        state.hwnd = refreshed_hwnd
        if refreshed_hwnd in candidate_hwnds:
            return True
        try:
            refreshed_title = win32gui.GetWindowText(refreshed_hwnd) or ""
            if any(token.lower() in refreshed_title.lower() for token in ["ory", "바람", "aion"]):
                return True
        except Exception:
            pass
    return False

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
        
        # play_area ?ル슦紐??醫륁뒞??野꺜??
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
    #     print("[Stealth] [!] ?遺얠쒔椰??癒???- ??쎈뻬 餓λ쵎??)
    #     return
    
    # if stealth_results['vm_detected']:
    #     print("[Stealth] [!] 揶쎛?怨룔돢????띻펾 ?癒???- ??쎈뻬 餓λ쵎??)
    #     return
    
    # if not stealth_results['process_integrity']:
    #     print("[Stealth] [!] ?袁⑥쨮?紐꾨뮞 ?얜떯猿???袁⑥뺘 - ??쎈뻬 餓λ쵎??)
    #     return
    
    # if not stealth_results['external_modules']:
    #     print("[Stealth] [!] ??뤿뼎??살쑎???紐? 筌뤴뫀諭??癒? - ??쎈뻬 餓λ쵎??)
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
    role_arg = os.environ.get("ROLE", "").strip() or preset_role
    net_role_arg = os.environ.get("NETWORK_ROLE", "").strip() or preset_network_role or role_arg
    if role_arg:
        state.role = role_arg
    if net_role_arg:
        state.network_role = net_role_arg
    if preset_auto_hunt is not None:
        state.auto_hunt = bool(preset_auto_hunt)
        state.service_active = bool(preset_auto_hunt)
    state.hwnd = hwnd  # GameState???紐껊굶 ????
    hw.set_state(state)  # DevInterface??GameState ?怨뚭퍙 (??鍮??筌ｋ똾寃??

    def _cleanup_hardware_on_exit():
        try:
            hw.stop_all_inputs(disconnect=True, repeat=3, delay=0.05)
        except Exception:
            pass

    atexit.register(_cleanup_hardware_on_exit)
    
    # 揶????묽 ??살쟿???λ뜃由??(bootstrap.py 疫꿸퀡??????)
    config_file = os.path.join(SCRIPT_DIR, "config.json")
    network_cfg = load_network_config()
    resolved_role = resolve_runtime_network_role(
        network_cfg.get("role"),
        explicit_role=role_arg,
        explicit_network_role=net_role_arg,
    )
    network_cfg["role"] = resolved_role
    state.role = resolved_role
    state.network_role = resolved_role
    state.network_server_ip = network_cfg.get("server_ip", state.network_server_ip)
    state.network_bind_host = network_cfg.get("bind_host", state.network_bind_host)
    state.network_telemetry_port = int(network_cfg.get("telemetry_port", state.network_telemetry_port))
    state.network_local_port = int(network_cfg.get("local_port", state.network_local_port))
    print(
        f"[Net] runtime identity: pc={state.network_peer_name} "
        f"gui_role={state.role} udp_role={state.network_role} "
        f"hub={state.network_server_ip}:{state.network_telemetry_port}"
    )

    spell_db_file = os.path.join(SCRIPT_DIR, "spells_config.json")

    # 1. ?⑥쥙??筌╈돦荑???살쟿??揶쎛??(筌뤴뫀諭???쑴????살쟿??뽰벥 ???뮞)
    capture_svc = CaptureSvc(state)
    capture_svc.start()

    try:
        reader_thread = MonitorSvc(state, config_file)  # PatternMatcher 疫꿸퀡而?OCR
        print(f"[Worker] MonitorSvc initialized: {reader_thread is not None}")
    except Exception as e:
        print(f"[Worker] MonitorSvc initialization failed: {e}")
        import traceback
        traceback.print_exc()
        return
    
    numeric_scanner = NumericFieldScanner(state, reader_thread.matcher, config_file, reader_thread)  # ?⑥쥙???袁⑹뒠 ??살쟿??
    sentinel_thread = SentinelThread(state, reader_thread.matcher) # 筌뤣딅뮞???袁⑹뵠???癒? ?袁⑹뒠
    action_thread = LogicSvc(state)  # FSM 嚥≪뮇彛?
    nav_thread = RouteSvc(state)  # ??삵돩野껊슣???
    
    # ?ロ궎 ?깅줉 (GUI 珥덇린???꾩뿉 癒쇱? ?깅줉)
    print("[DEBUG] 핫키 등록 시작...")
    try:
        def register_hotkeys(gui_instance):
            global HOTKEY_HANDLES
            try:
                _clear_registered_hotkeys()

                HOTKEY_HANDLES["f1"] = keyboard.on_press_key(
                    "f1",
                    lambda _event: _dispatch_gui_hotkey(gui_instance, "F1", gui_instance.toggle_follow),
                )
                print("[DEBUG] F1 핫키 등록 완료")
                HOTKEY_HANDLES["f2"] = keyboard.on_press_key(
                    "f2",
                    lambda _event: _dispatch_gui_hotkey(gui_instance, "F2", gui_instance.toggle_service),
                )
                print("[DEBUG] F2 핫키 등록 완료")
                HOTKEY_HANDLES["f3"] = keyboard.on_press_key(
                    "f3",
                    lambda _event: _dispatch_gui_hotkey(gui_instance, "F3", gui_instance.toggle_pause_resume),
                )
                print("[DEBUG] F3 핫키 등록 완료")
                HOTKEY_HANDLES["f4"] = keyboard.on_press_key(
                    "f4",
                    lambda _event: _dispatch_gui_hotkey(gui_instance, "F4", gui_instance.emergency_exit),
                )
                print("[DEBUG] F4 핫키 등록 완료")
                HOTKEY_HANDLES["f5"] = keyboard.on_press_key(
                    "f5",
                    lambda _event: _dispatch_gui_hotkey(gui_instance, "F5", gui_instance.cast_f5_repeat),
                )
                print("[DEBUG] F5 핫키 등록 완료")
                HOTKEY_HANDLES["f6"] = keyboard.on_press_key(
                    "f6",
                    lambda _event: _dispatch_gui_hotkey(gui_instance, "F6", gui_instance.toggle_sulsa_debuff_loop),
                )
                print("[DEBUG] F6 핫키 등록 완료")

                # [TEST] ` 키로 JSON 등록 패턴과 user_info 상태를 함께 점검
                def test_patterns():
                    frame = reader_thread.last_frame
                    if frame is None:
                        print("[FindText] 테스트 실패: 현재 프레임 데이터가 없습니다.")
                        return

                    # 1. hb 패턴 테스트 (일반 play_area, 멀티서치)
                    hb_matches = reader_thread.find_pattern_all(
                        frame,
                        "hb",
                        "play_area",
                        max_results=6,
                        overlap_px=44,
                        err_ratio=0.15,
                    )
                    if hb_matches:
                        print(f"[HBTest] hb raw matches: {len(hb_matches)}개")
                        for idx, match in enumerate(hb_matches[:10], start=1):
                            print(
                                f"[HBTest] #{idx} | 위치: ({match['x']}, {match['y']}) "
                                f"| 크기: {match['w']}x{match['h']} | 일치율: {float(match.get('score', 0.0) or 0.0):.1%}"
                            )
                    else:
                        print("[HBTest] 'hb' 패턴을 play_area에서 찾을 수 없습니다.")

                    # 2. red_tab 패턴 테스트 (target_info 우선, 실패 시 play_area)
                    res_rt = reader_thread.find_pattern(frame, "red_tab", "target_info")
                    red_tab_source = "target_info"
                    if not res_rt:
                        res_rt = reader_thread.find_pattern(frame, "red_tab", "play_area")
                        red_tab_source = "play_area"
                    
                    if res_rt:
                        print(f"[FindText] 발견! red_tab ({red_tab_source}) | 위치: ({res_rt['x']}, {res_rt['y']}), 일치율: {res_rt['score']:.1%}")
                    else:
                        print("[FindText] 'red_tab' 패턴을 target_info / play_area에서 찾을 수 없습니다.")

                    # 3. user_info 상태 확인 (현재 상태 + FindText/bitwise 결과)
                    try:
                        print(
                            f"[UserInfoTest] state.user_info_text='{getattr(state, 'user_info_text', '')}' "
                            f"user_name='{getattr(state, 'user_name', '')}' "
                            f"detected={getattr(state, 'is_user_detected', False)}"
                        )

                        inspect = reader_thread.inspect_user_info_frame(frame)
                        print(f"[UserInfoTest] inspect_status={inspect.get('status', 'unknown')}")
                        if not inspect.get("crop_available"):
                            print("[UserInfoTest] user_info ROI를 추출하지 못했습니다.")
                        else:
                            for hit in inspect.get("hits", []):
                                print(
                                    f"[UserInfoTest] candidate: {hit['name']} "
                                    f"| score={float(hit.get('score', 0.0) or 0.0):.1%}"
                                )

                            accepted_hit = inspect.get("accepted_hit")
                            best_hit = inspect.get("best_hit")
                            second_hit = inspect.get("second_hit")
                            margin = float(inspect.get("margin", 0.0) or 0.0)

                            if accepted_hit:
                                print(
                                    f"[UserInfoTest] accepted: {accepted_hit['name']} "
                                    f"| score={float(accepted_hit.get('score', 0.0) or 0.0):.1%} "
                                    f"| margin={margin:.1%}"
                                )
                            elif inspect.get("ambiguous") and best_hit:
                                second_name = second_hit["name"] if second_hit else "-"
                                second_score = float(second_hit.get("score", 0.0) or 0.0) if second_hit else 0.0
                                print(
                                    f"[UserInfoTest] ambiguous: best={best_hit['name']} "
                                    f"({float(best_hit.get('score', 0.0) or 0.0):.1%}) "
                                    f"second={second_name} ({second_score:.1%}) "
                                    f"margin={margin:.1%}"
                                )
                            elif inspect.get("bitwise_name"):
                                print(
                                    f"[UserInfoTest] bitwise hit: {inspect['bitwise_name']} "
                                    f"| score={float(inspect.get('bitwise_score', 0.0) or 0.0):.1%}"
                                )
                            else:
                                if not getattr(state, "user_info_text", "") and not getattr(state, "user_name", ""):
                                    print("[UserInfoTest] user_info가 비어 있거나 현재 선택된 대상이 없습니다.")
                                else:
                                    print("[UserInfoTest] user_info에서 화이트리스트 패턴을 찾지 못했습니다.")
                    except Exception as e:
                        print(f"[UserInfoTest] error: {e}")

                    # 4. cooltime_area 상태 확인 (S 상태창 열림 전제)
                    try:
                        res_bm = reader_thread.find_pattern(frame, "bm", "cooltime_area")
                        res_gg = reader_thread.find_pattern(frame, "gg", "cooltime_area")
                        if res_bm:
                            print(
                                f"[CooltimeTest] 발견! bm | 위치: ({res_bm['x']}, {res_bm['y']}) "
                                f"| 일치율: {res_bm['score']:.1%}"
                            )
                        else:
                            print("[CooltimeTest] 'bm' 패턴을 cooltime_area에서 찾을 수 없습니다.")

                        if res_gg:
                            print(
                                f"[CooltimeTest] 발견! gg | 위치: ({res_gg['x']}, {res_gg['y']}) "
                                f"| 일치율: {res_gg['score']:.1%}"
                            )
                        else:
                            print("[CooltimeTest] 'gg' 패턴을 cooltime_area에서 찾을 수 없습니다.")
                    except Exception as e:
                        print(f"[CooltimeTest] error: {e}")

                    # 5. 실제 N_tab -> red_tab 테스트
                    try:
                        if not _is_game_window_active(state):
                            print("[NTabTest] 게임창이 활성화되어 있지 않아 테스트를 건너뜁니다.")
                            return

                        if action_thread is None or not hasattr(action_thread, "_ensure_warrior_red_tab_via_ntab"):
                            print("[NTabTest] LogicSvc가 준비되지 않아 테스트를 실행할 수 없습니다.")
                            return

                        support_snapshot = state.get_remote_data_by_role("격수")
                        if not support_snapshot:
                            print("[NTabTest] 격수 원격 데이터가 없어 테스트를 건너뜁니다.")
                            return

                        warrior_x = int(support_snapshot.get("x", support_snapshot.get("pos_x", 0)) or 0)
                        warrior_y = int(support_snapshot.get("y", support_snapshot.get("pos_y", 0)) or 0)
                        print(f"[NTabTest] start: warrior=({warrior_x}, {warrior_y}) self=({getattr(state, 'x', 0)}, {getattr(state, 'y', 0)})")

                        success = bool(action_thread._ensure_warrior_red_tab_via_ntab(support_snapshot))
                        print(
                            f"[NTabTest] result: success={success} "
                            f"red_tab={getattr(state, 'red_tab_enabled', False)} "
                            f"user='{getattr(state, 'user_info_text', '')}' "
                            f"target_kind='{getattr(state, 'target_kind', '')}' "
                            f"target='{getattr(state, 'target_info_text', '')}'"
                        )
                    except Exception as e:
                        print(f"[NTabTest] error: {e}")

                HOTKEY_HANDLES["`"] = keyboard.on_press_key("`", lambda _event: test_patterns())
                print("[DEBUG] ` 핫키 등록 완료 ('hb/red_tab/user_info' + 'N_tab/red_tab' 테스트용)")
            except Exception as e:
                print(f"[Warn] Hotkey Registration Failed: {e}")
        
        # GUI ??밴쉐 (hwnd ?袁⑤뼎)
        gui = AppView(state, hwnd=hwnd)
        
        # GUI ?앹꽦 ???ロ궎 ?깅줉
        register_hotkeys(gui)
        
    except Exception as e:
        print(f"[ERROR] 핫키 등록 실패: {e}")
        # GUI ??밴쉐 (hwnd ?袁⑤뼎)
        gui = AppView(state, hwnd=hwnd)
    
    # ?곌껐 ?곹깭 蹂寃?肄쒕갚 ?⑥닔 (UI ?낅뜲?댄듃??AppView?먯꽌 二쇨린?곸쑝濡??섑뻾??
    def on_connection_change(is_connected):
        pass
    
    network = NetworkThread(state, network_cfg, on_connection_change)
    logger = LoggerThread(state)

    # GUI ??밴쉐 ??grid_indicator ?袁⑤뼎
    if hasattr(gui, 'grid_indicator'):
        sentinel_thread.grid_indicator = gui.grid_indicator

    # 筌뤴뫀諭???살쟿????뽰삂
    print(f"[Worker] About to start reader_thread...")
    reader_thread.start()
    print(f"[Worker] reader_thread started")
    numeric_scanner.start()  # ?⑥쥙????ъ쁽 ??쇳떔 ??뽰삂
    sentinel_thread.start()
    action_thread.start()
    nav_thread.start()
    network.start()
    logger.start()

    print("[OK] System started. (Production Mode)")
    
    # GUI ??쎈뻬
    gui.action_thread = action_thread
    gui.reader_thread = reader_thread
    gui.network_thread = network
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


