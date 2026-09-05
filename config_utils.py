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

DEFAULT_NETWORK_CONFIG = {
    "role": "도사1",
    "server_ip": "192.168.137.1",
    "bind_host": "0.0.0.0",
    "telemetry_port": 5555,
    "local_port": 5556,
}

CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
LOCAL_CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".system_parallel_sota.local.json")
LEGACY_LOCAL_CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.local.json")
GUI_STATE_FILE = os.path.join(SCRIPT_DIR, "gui_state.json")
TEMPLATE_DIGIT_DIR = os.path.join(SCRIPT_DIR, "temple", "digits")

def _load_json_file(path: str) -> dict:
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_json_file(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def load_network_config() -> dict:
    cfg = dict(DEFAULT_NETWORK_CONFIG)
    try:
        base_data = _load_json_file(CONFIG_FILE)
        legacy_data = _load_json_file(LEGACY_LOCAL_CONFIG_FILE)
        user_data = _load_json_file(LOCAL_CONFIG_FILE)
        merged_network = {}
        for data in (base_data, legacy_data, user_data):
            net = data.get("network", {})
            if isinstance(net, dict):
                merged_network.update(net)
        cfg["role"] = str(merged_network.get("role", cfg["role"]) or cfg["role"])
        cfg["server_ip"] = str(merged_network.get("server_ip", cfg["server_ip"]) or cfg["server_ip"])
        cfg["bind_host"] = str(merged_network.get("bind_host", cfg["bind_host"]) or cfg["bind_host"])
        cfg["telemetry_port"] = int(merged_network.get("telemetry_port", cfg["telemetry_port"]) or cfg["telemetry_port"])
        cfg["local_port"] = int(merged_network.get("local_port", cfg["local_port"]) or cfg["local_port"])
    except Exception as e:
        print(f"[Net] config load failed: {e}")
    return cfg

def load_hardware_config() -> dict:
    """이 PC의 하드웨어 설정. network과 같은 3단계 병합을 쓴다.

    지금 담는 값은 move_command_form 하나다: 이 PC에 붙은 ESP32가 실제로
    인식하는 키 입력 형식("DU" = D:<키>/U:<키>, "K" = K,<키>).
    실측으로 둘이 정반대인 PC가 확인됐다 - 격수 PC(COM3)는 K,만 먹히고
    D:/U:는 무시되며, 도사 PC(COM11)는 D:/U:로 문을 통과하는데 K,로는
    한 번도 안 됐다(svc_route.py의 포탈 진입 주석 참고). 그래서 이건
    코드에 박을 수 없고 PC마다 달라야 한다.

    기본값은 "DU"다 - 지금까지의 동작 그대로라, 검사를 안 돌린 PC는
    아무것도 안 바뀐다.
    """
    cfg = {"move_command_form": "DU"}
    try:
        merged = {}
        for data in (_load_json_file(CONFIG_FILE),
                     _load_json_file(LEGACY_LOCAL_CONFIG_FILE),
                     _load_json_file(LOCAL_CONFIG_FILE)):
            section = data.get("hardware", {})
            if isinstance(section, dict):
                merged.update(section)
        form = str(merged.get("move_command_form", "") or "").upper()
        if form in ("DU", "K"):
            cfg["move_command_form"] = form
    except Exception as e:
        print(f"[Hardware] config load failed: {e}")
    return cfg


def save_hardware_config(updates: dict) -> None:
    """하드웨어 설정을 저장소 밖(홈 디렉터리)에 쓴다 - git pull이 못 건드린다."""
    try:
        data = _load_json_file(LOCAL_CONFIG_FILE)
        section = data.get("hardware", {})
        if not isinstance(section, dict):
            section = {}
        for key, value in dict(updates or {}).items():
            if value is not None:
                section[key] = value
        data["hardware"] = section
        _save_json_file(LOCAL_CONFIG_FILE, data)
        print(f"[Hardware] 설정 저장: {section} -> {LOCAL_CONFIG_FILE}")
    except Exception as e:
        print(f"[Hardware] config save failed: {e}")


def save_network_config(network_updates: dict) -> None:
    try:
        data = _load_json_file(LOCAL_CONFIG_FILE) or _load_json_file(CONFIG_FILE)
        network = data.get("network", {})
        if not isinstance(network, dict):
            network = {}
        for key, value in dict(network_updates or {}).items():
            if value is not None:
                network[key] = value
        data["network"] = network
        _save_json_file(LOCAL_CONFIG_FILE, data)
    except Exception as e:
        print(f"[Net] config save failed: {e}")

def get_local_ipv4_candidates() -> list[str]:
    """Return usable local IPv4 candidates for other PCs to connect to."""
    candidates: list[str] = []

    def add_ip(value: str) -> None:
        try:
            ip = ipaddress.ip_address(str(value).strip())
        except ValueError:
            return
        if ip.version != 4:
            return
        if ip.is_loopback or ip.is_unspecified or ip.is_link_local:
            return
        text = str(ip)
        if text not in candidates:
            candidates.append(text)

    # First entry: the address Windows would use for the default route.
    try:
        with pysocket.socket(pysocket.AF_INET, pysocket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            add_ip(sock.getsockname()[0])
    except Exception:
        pass

    try:
        hostname = pysocket.gethostname()
        for info in pysocket.getaddrinfo(hostname, None, pysocket.AF_INET):
            add_ip(info[4][0])
    except Exception:
        pass

    return candidates

def choose_preferred_local_ipv4() -> str:
    candidates = get_local_ipv4_candidates()
    return candidates[0] if candidates else ""

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

