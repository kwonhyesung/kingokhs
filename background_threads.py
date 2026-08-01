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
from config_utils import choose_preferred_local_ipv4, get_local_ipv4_candidates, load_network_config
from support_runtime_rules import is_hub_network_role, normalize_network_role
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
        self.role = normalize_network_role(self.cfg.get('role', getattr(state, 'network_role', '도사1')))
        self.server_ip = str(self.cfg.get('server_ip', getattr(state, 'network_server_ip', '192.168.137.1')) or '192.168.137.1')
        self.bind_host = str(self.cfg.get('bind_host', getattr(state, 'network_bind_host', '0.0.0.0')) or '0.0.0.0')
        self.telemetry_port = int(self.cfg.get('telemetry_port', getattr(state, 'network_telemetry_port', 5555)) or 5555)
        self.local_port = int(self.cfg.get('local_port', getattr(state, 'network_local_port', 5556)) or 5556)
        self.is_server = is_hub_network_role(self.role)
        self.sock: Optional[pysocket.socket] = None
        self._peers: dict[tuple[str, int], dict] = {}
        self._last_send = 0.0
        self._last_broadcast = 0.0
        self._warned_connreset = False
        self._warned_server_ip = False
        self._seen_rx_senders: set[str] = set()
        self._reconfigure_requested = threading.Event()
        self.send_interval = 0.05
        self.broadcast_interval = 0.05
        self.on_connection_change = on_connection_change  # 연결 상태 변경 시 호출할 콜백

        self.state.network_role = self.role
        self.state.network_server_ip = self.server_ip
        self.state.network_bind_host = self.bind_host
        self.state.network_telemetry_port = self.telemetry_port
        self.state.network_local_port = self.local_port

    def request_reconfigure(self, role: Optional[str] = None, server_ip: Optional[str] = None) -> None:
        """Apply GUI network settings on the network thread's next loop iteration."""
        next_role = normalize_network_role(role or self.role)
        next_server_ip = str(server_ip or self.server_ip).strip() or self.server_ip
        changed = next_role != self.role or next_server_ip != self.server_ip
        self.role = next_role
        self.server_ip = next_server_ip
        self.is_server = is_hub_network_role(next_role)
        self.state.network_role = next_role
        self.state.network_server_ip = next_server_ip
        if changed:
            self._reconfigure_requested.set()

    def _bind_socket(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = self._make_socket()
        self._peers.clear()
        self._seen_rx_senders.clear()
        if self.is_server:
            self.sock.bind((self.bind_host, self.telemetry_port))
            print(f'[Net] UDP hub connected: {self.bind_host}:{self.telemetry_port} (role={self.role})')
            preferred_ip = choose_preferred_local_ipv4()
            candidates = get_local_ipv4_candidates()
            if preferred_ip:
                print(f"[Net] Dosa PC recommended server_ip: {preferred_ip}")
            if candidates:
                print(f"[Net] Dosa PC IPv4 candidates: {', '.join(candidates)}")
        else:
            self.sock.bind((self.bind_host, self.local_port))
            print(f'[Net] UDP client connected: {self.bind_host}:{self.local_port} -> {self.server_ip}:{self.telemetry_port} (role={self.role})')
            if not self._is_reachable_host_ip(self.server_ip) and not self._warned_server_ip:
                print(f"[Net] WARNING: server_ip={self.server_ip} looks unusable.")
                self._warned_server_ip = True

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
        payload['hub_ack'] = {
            str(meta.get('sender') or ''): {
                'role': str(meta.get('role') or ''),
                'seen_at': float(meta.get('seen_at') or 0.0),
            }
            for meta in self._peers.values()
            if str(meta.get('sender') or '')
        }
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
            self._bind_socket()
            self.state.is_connected = True
            if self.on_connection_change:
                self.on_connection_change(True)

            while self.state.running:
                if self._reconfigure_requested.is_set():
                    self._reconfigure_requested.clear()
                    self._bind_socket()
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

                sender, remote_data = self.state.apply_remote_payload(
                    payload,
                    accept_relayed_peers=not self.is_server,
                )
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

