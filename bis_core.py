import time
import threading
import serial
import random
import os
import json
import csv
import queue
import re

VERBOSE_STATE_LOGS = os.environ.get("SVC_VERBOSE_LOGS", "0") == "1"
VERBOSE_HW_LOGS = os.environ.get("SVC_HW_LOGS", "0") == "1"


def _hw_log(*args, **kwargs):
    """Hardware logging is extremely spammy; keep it opt-in."""
    if VERBOSE_HW_LOGS:
        print(*args, **kwargs)

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import win32gui
from svc_stealth import HumanBehaviorSimulator

# ============================================================
# ① TIMING_CONFIG  ─  시스템 딜레이 제어 테이블 (config.json에서 로드)
# ============================================================
DEFAULT_TIMING_CONFIG: dict = {
    "key_down_hold"  : 0.035,    # 35ms
    "key_gap"        : 0.050,    # 50ms
    "tab_wait"       : 0.040,
    "up_wait"        : 0.040,
    "enter_wait"     : 0.035,
    "spell_cast_gap" : 0.030,
    "spell_confirm"  : 0.200,
    "ocr_wait"       : 0.100,
    "ocr_fast"       : 0.050,
    "heal_gap"       : 0.035,    # 35ms
    "mp_recover_wait": 0.100,
    "mp_recover_interval": 2.0,  # mp_trig active -> press once every 2 seconds
    "bomu_spell_gap" : 0.200,
    "bomu_interval"  : 185.0,
    "debuff_key_wait": 0.035,    # 35ms
    "debuff_up_wait" : 0.100,
    "debuff_ocr_wait": 0.150,
    "move_hold"      : 0.100,    # [최적화] 130ms -> 100ms (이동속도 상향, 타일 씹힘 마지노선)
    "nav_loop"       : 0.003,   # [수정] 이동 루프 주기 추가 단축
    "idle_sleep"     : 0.100,
    "stuck_time"     : 4.0,     # [수정] 이동 중 4초 이상 좌표가 안 바뀌면 stuck 판정 (오탐지 방지)
    "stuck_back_hold": 0.100,    # 100ms
    "stuck_side_hold": 0.100,    # 100ms
    "combat_timeout" : 12.0,
    "action_loop"    : 0.040,
    "esc_gap"        : 0.035,    # 35ms
    "entry_hold"     : 1.0,      # 포탈/입구 진입 시 키 유지 시간
    "exit_hold"      : 1.0,      # 포탈/출구 진입 시 키 유지 시간
}

TIMING_CONFIG: dict = DEFAULT_TIMING_CONFIG.copy()

GAME_HOTKEY_ALIAS: dict[str, str] = {
    "1": "a",
    "2": "b",
    "3": "c",
    "4": "d",
    "5": "e",
    "6": "f",
    "7": "g",
    "8": "h",
    "9": "i",
    "0": "j",
}
GAME_HOTKEY_DISPLAY_ALIAS: dict[str, str] = {v: k for k, v in GAME_HOTKEY_ALIAS.items()}

def load_timing_config():
    """config.json에서 TIMING_CONFIG 로드"""
    global TIMING_CONFIG
    try:
        config_file = os.path.join(os.path.dirname(__file__), "config.json")
        if os.path.exists(config_file):
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                timing = config.get("timing", {})
                if timing:
                    TIMING_CONFIG.update(timing)
                    print(f"[Config] TIMING_CONFIG 로드 완료: {len(timing)}개 항목")
    except Exception as e:
        print(f"[Config] TIMING_CONFIG 로드 실패: {e}")

# 초기 로드
load_timing_config()


def normalize_game_hotkey(key: str | None) -> str:
    """게임 내부 단축키 체계 기준으로 숫자 별칭을 a~j로 정규화한다."""
    normalized = str(key or "").strip()
    if not normalized:
        return ""
    return GAME_HOTKEY_ALIAS.get(normalized, normalized)


def display_game_hotkey(key: str | None) -> str:
    """내부 a~j 키를 게임 표기 1~0으로 되돌린다."""
    normalized = str(key or "").strip()
    if not normalized:
        return ""
    return GAME_HOTKEY_DISPLAY_ALIAS.get(normalized, normalized)

def humanized_sleep(base_time: float, variance: float = 0.15) -> None:
    sigma  = base_time * variance * 0.5
    delay  = random.gauss(base_time, sigma)
    delay  = max(base_time * 0.50, min(base_time * 2.0, delay))
    time.sleep(delay)

def tc(key: str, variance: float = 0.15) -> float:
    base = TIMING_CONFIG.get(key, 0.05)
    sigma = base * variance * 0.5
    v = random.gauss(base, sigma)
    return max(base * 0.50, min(base * 2.0, v))

# ============================================================
# ② 시스템 상태 및 환경 명칭 변경 (Anti-Cheat 대응)
# ============================================================
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

@dataclass
class Region:
    """OCR 영역 좌표 데이터 클래스"""
    sx: int  # 시작 X
    sy: int  # 시작 Y
    dx: int  # 끝 X
    dy: int  # 끝 Y

    def to_bbox(self, offset: tuple = (0, 0)) -> tuple:
        """PIL crop용 bbox 반환 (offset 적용)"""
        ox, oy = offset
        return (self.sx + ox, self.sy + oy, self.dx + ox, self.dy + oy)

def find_game_window(substring: str) -> Optional[int]:
    found_hwnd = None
    def enum_cb(hwnd, _):
        nonlocal found_hwnd
        title = win32gui.GetWindowText(hwnd)
        if substring in title and win32gui.IsWindowVisible(hwnd):
            found_hwnd = hwnd
            return False
        return True
    try:
        win32gui.EnumWindows(enum_cb, None)
    except Exception:
        pass
    return found_hwnd


def split_map_name_floor(map_text: str) -> tuple[str, str]:
    """
    Map OCR 결과를 (map_name, floor)로 분리한다.
    - "흉가 입구" -> ("흉가 입구", "")
    - "흉가01" -> ("흉가", "01")
    - "흉가 1층" -> ("흉가", "1")
    """
    text = str(map_text or "").strip()
    if not text:
        return "", ""

    match = re.match(r"^(.*?)(\d+)\s*층?$", text)
    if match:
        name = match.group(1).strip()
        floor = match.group(2).strip()
        return name or text, floor

    return text, ""

# ============================================================
# ③ GameState -> SysEnv (환경 정보 은닉)
# ============================================================
WHITELIST_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whitelist.csv")

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any
import numpy as np

@dataclass
class GameState:
    """게임 상태를 관리하는 dataclass 기반 클래스"""
    # 스레드 안전성을 위한 락 (dataclass 외부에서 관리)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    
    # 엔티티 관리
    entities: Dict[str, Any] = field(default_factory=dict)
    
    # 기본 상태
    hwnd: Optional[int] = None
    hp: int = 0
    mp: int = 0
    exp: int = 0
    money: int = 0
    x: int = 0
    y: int = 0
    good_hp: int = 0
    good_mp: int = 0
    # OCR 원문(또는 누락 포함) 문자열. x/y는 4자리 고정, 누락은 'x'로 채움.
    hp_str: str = ""
    mp_str: str = ""
    exp_str: str = ""
    money_str: str = ""
    x_str: str = ""
    y_str: str = ""
    map_name: str = ""
    map_floor: str = ""
    current_floor: str = ""
    red_tab_enabled: bool = False
    target_info_text: str = ""
    user_info_text: str = ""
    target_kind: str = "UNKNOWN"
    user_kind: str = "UNKNOWN"
    heal_request: bool = False
    mp_request: bool = False
    debuff_request: bool = False
    follow_anchor_offset: Tuple[int, int] = (0, 0)
    safe_spot: Optional[Tuple[int, int]] = None
    battle_spot: Optional[Tuple[int, int]] = None
    network_role: str = "도사"
    network_server_ip: str = "192.168.137.1"
    network_bind_host: str = "0.0.0.0"
    network_telemetry_port: int = 5555
    network_local_port: int = 5556
    network_sequence: int = 0
    remote_sequences: Dict[str, int] = field(default_factory=dict)
    last_network_rx_ts: float = 0.0
    last_network_rx_sender: str = ""
    last_network_rx_role: str = ""
    last_network_rx_kind: str = ""
    last_network_rx_seq: int = 0
    
    # 타겟 관리
    target_locked: bool = False
    target_type: str = "NONE"
    target_name: str = ""
    target_monster_names: List[str] = field(default_factory=list)
    
    # 시스템 상태
    last_update_time: float = field(default_factory=time.time)
    running: bool = True
    ocr_enabled: bool = True
    auto_hunt: bool = False
    
    # OCR 설정
    bin_threshold: int = 128
    ocr_thresholds: Dict[str, float] = field(default_factory=lambda: {str(i): 0.35 for i in range(10)})
    ocr_fps: float = 0.0
    ocr_preview_img: Optional[np.ndarray] = None
    
    # GUI 업데이트 큐 (비동기 통신용)
    gui_update_queue: 'queue.Queue' = field(default_factory=lambda: queue.Queue(maxsize=1), init=False, repr=False)
    
    # 스킬 관리
    spells: List['Skill'] = field(default_factory=list)
    
    # 트리거 관리
    hp_trig_active: bool = False
    mp_trig_active: bool = False
    recovery_hp_spell: str = ""
    recovery_mp_spell: str = ""
    
    # 윈도우 정보
    win_x: Optional[int] = None
    win_y: Optional[int] = None
    
    # 네비게이션 설정
    nav_follow_enabled: bool = False
    nav_route_enabled: bool = False
    nav_avoid_enabled: bool = False
    reverse_mode: bool = False
    waypoints_db: Dict[str, Any] = field(default_factory=dict)
    current_map: str = "기본맵"
    follow_target_pos: Optional[Tuple[int, int]] = None
    last_pos: Tuple[int, int] = (0, 0)
    stuck_start_time: float = 0.0
    
    # 그리드 관련
    grid_overlay_enabled: bool = False
    grid_offset: Tuple[int, int] = (0, 0)
    
    # 전투 제어
    is_combat_busy: bool = False
    combat_start_time: float = 0.0
    
    # 유저 관리
    user_name: str = ""
    is_chat_active: bool = False
    is_user_detected: bool = False
    
    # 보호막 관리
    shield_active: bool = False
    shield_expire_time: float = 0.0
    
    # 역할 관리
    role: str = "기본"
    priest_engage_range: int = 2
    priest_test_attack_key: str = "0"
    priest_test_attack_min_interval: float = 0.8
    priest_test_attack_max_interval: float = 1.1
    priest_test_active: bool = False
    service_active: bool = False
    automation_paused: bool = False
    control_mode: str = "NONE"
    f1_route_active: bool = False
    
    # 네트워크 관리
    is_connected: bool = False
    other_pc_data: Dict[str, Any] = field(default_factory=dict)
    network_peer_name: str = field(default_factory=lambda: os.environ.get("COMPUTERNAME", "LOCAL"))
    network_outbox: 'queue.Queue' = field(default_factory=lambda: queue.Queue(maxsize=256), init=False, repr=False)
    
    # 공유 메모리 (성능 최적화)
    shared_frame: Optional[np.ndarray] = None
    last_play_area_rgb: Optional[np.ndarray] = None
    
    # 좌표 관리
    my_screen_pos: Tuple[int, int] = (0, 0)
    my_world_pos: Tuple[float, float] = (0.0, 0.0)
    
    # 아이템 관리
    detected_item_grid: Optional[Tuple[int, int]] = None
    detected_item_name: str = ""
    
    # 디버그/로깅
    last_vision_error: str = ""
    last_event_log: str = ""
    last_bomu_time: float = 0.0
    last_debuff_x: int = 0
    last_debuff_y: int = 0
    
    # UI 설정
    user_alarm_enabled: bool = False
    user_next_enabled: bool = False
    user_stop_enabled: bool = False
    
    # 추가 필드 (기존 코드 호환성)
    whitelist_names: List[str] = field(default_factory=list)
    auto_debuff_enabled: bool = True
    recovery_debuff_spell: str = "NONE"
    game_resolution: Tuple[int, int] = (854, 480)
    game_scale: float = 2.0
    gui_resolution: Tuple[int, int] = (640, 480)
    monster_on_screen: bool = False
    safe_zone_detected: bool = False
    sentinel_enabled: bool = False
    last_move_dir: Optional[str] = None
    is_stuck: bool = False
    char_grid: Tuple[int, int] = (0, 0)
    maps_db: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        """초기화 후 추가 설정"""
        self._load_thresholds()
        self._load_runtime_settings()
    
    def _load_thresholds(self):
        """OCR 임계값 로드"""
        try:
            import json
            import os
            config_file = os.path.join(os.path.dirname(__file__), "config.json")
            if os.path.exists(config_file):
                with open(config_file, 'r', encoding='utf-8') as f:
                    saved = json.load(f).get("ocr_thresholds", {})
                    if isinstance(saved, dict):
                        for k, v in saved.items():
                            if k in self.ocr_thresholds:
                                self.ocr_thresholds[k] = float(v)
        except Exception:
            pass

    def _load_runtime_settings(self):
        """config.json에서 전투/테스트 관련 런타임 설정 로드"""
        try:
            config_file = os.path.join(os.path.dirname(__file__), "config.json")
            if not os.path.exists(config_file):
                return
            with open(config_file, "r", encoding="utf-8") as f:
                conf = json.load(f)

            combat = conf.get("combat", {})
            if isinstance(combat, dict):
                engage_range = combat.get("priest_engage_range", self.priest_engage_range)
                self.priest_engage_range = max(0, int(engage_range))
                self.good_hp = max(0, int(combat.get("good_hp", self.good_hp) or 0))
                self.good_mp = max(0, int(combat.get("good_mp", self.good_mp) or 0))

            priest_test = conf.get("priest_test", {})
            if isinstance(priest_test, dict):
                attack_key = str(priest_test.get("attack_key", self.priest_test_attack_key) or "0").strip() or "0"
                self.priest_test_attack_key = attack_key

                attack_min = float(priest_test.get("attack_interval_min", self.priest_test_attack_min_interval))
                attack_max = float(priest_test.get("attack_interval_max", self.priest_test_attack_max_interval))
                if attack_max < attack_min:
                    attack_min, attack_max = attack_max, attack_min
                self.priest_test_attack_min_interval = max(0.05, attack_min)
                self.priest_test_attack_max_interval = max(self.priest_test_attack_min_interval, attack_max)

            network = conf.get("network", {})
            if isinstance(network, dict):
                self.network_role = str(network.get("role", self.network_role) or self.network_role)
                self.network_server_ip = str(network.get("server_ip", self.network_server_ip) or self.network_server_ip)
                self.network_bind_host = str(network.get("bind_host", self.network_bind_host) or self.network_bind_host)
                self.network_telemetry_port = int(network.get("telemetry_port", self.network_telemetry_port) or self.network_telemetry_port)
                self.network_local_port = int(network.get("local_port", self.network_local_port) or self.network_local_port)
        except Exception:
            pass
    
    def load_whitelist_csv(self):
        """whitelist.csv에서 닉네임 목록을 읽어 whitelist_names에 저장."""
        path = WHITELIST_CSV
        names = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        nick = row.get("nickname", "").strip()
                        if nick:
                            names.append(nick)
            except Exception as e:
                if VERBOSE_STATE_LOGS:
                    print(f"[Whitelist] 로드 실패: {e}")
        self.whitelist_names = names

    def save_whitelist_csv(self):
        """현재 whitelist_names를 whitelist.csv에 저장."""
        path = WHITELIST_CSV
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["nickname"])
                writer.writeheader()
                with self._lock:
                    names = list(self.whitelist_names)
                for nick in names:
                    writer.writerow({"nickname": nick})
            if VERBOSE_STATE_LOGS:
                print(f"[Whitelist] {len(names)}명 저장 완료.")
        except Exception as e:
            if VERBOSE_STATE_LOGS:
                print(f"[Whitelist] CSV 저장 오류: {e}")

    def update_from_dict(self, data: dict):
        """인식된 데이터를 동적으로 상태창에 반영"""
        with self._lock:
            for k, v in data.items():
                if hasattr(self, k):
                    setattr(self, k, v)
                else:
                    setattr(self, k, v)

    def update_other(self, name: str, data: dict):
        """다른 PC에서 전송된 데이터 병합"""
        with self._lock:
            self.other_pc_data[name] = data

    def update(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)


    def _refresh_support_request_flags_unlocked(self) -> tuple[bool, bool]:
        # Refresh local support-request flags from current hp/mp state.
        old_heal_request = self.heal_request
        old_mp_request = self.mp_request
        
        self.heal_request = bool(self.good_hp > 0 and self.hp > 0 and self.hp <= self.good_hp)
        self.mp_request = bool(self.good_mp > 0 and self.mp > 0 and self.mp <= self.good_mp)
        
        # 힐요청 상태 변경 시 로그
        if old_heal_request != self.heal_request or old_mp_request != self.mp_request:
            print(f"[DEBUG] 힐요청 상태 변경: HP={self.hp}, good_hp={self.good_hp}, 힐요청={self.heal_request}")
            print(f"[DEBUG] MP요청 상태 변경: MP={self.mp}, good_mp={self.good_mp}, MP요청={self.mp_request}")
            print(f"[DEBUG] 힐요청 조건: good_hp>0={self.good_hp>0}, hp>0={self.hp>0}, hp<=good_hp={self.hp<=self.good_hp}")
            print(f"[DEBUG] MP요청 조건: good_mp>0={self.good_mp>0}, mp>0={self.mp>0}, mp<=good_mp={self.mp<=self.good_mp}")
        
        return self.heal_request, self.mp_request

    def get_all(self) -> dict:
        with self._lock:
            self._refresh_support_request_flags_unlocked()
            return {
                "hp": self.hp, "mp": self.mp,
                "exp": self.exp, "money": self.money,
                "x": self.x, "y": self.y,
                "good_hp": self.good_hp, "good_mp": self.good_mp,
                "hp_str": self.hp_str, "mp_str": self.mp_str,
                "exp_str": self.exp_str, "money_str": self.money_str,
                "x_str": self.x_str, "y_str": self.y_str,
                "map_name": self.map_name,
                "map_floor": self.map_floor,
                "current_map": self.current_map,
                "current_floor": self.current_floor,
                "red_tab_enabled": self.red_tab_enabled,
                "target_info_text": self.target_info_text,
                "user_info_text": self.user_info_text,
                "target_kind": self.target_kind,
                "user_kind": self.user_kind,
                "heal_request": self.heal_request,
                "mp_request": self.mp_request,
                "debuff_request": self.debuff_request,
                "follow_anchor_offset": self.follow_anchor_offset,
                "safe_spot": self.safe_spot,
                "battle_spot": self.battle_spot,
                "network_role": self.network_role,
                "target_locked": self.target_locked,
                "last_move_dir": self.last_move_dir,
                "nav_follow_enabled": self.nav_follow_enabled,
                "nav_route_enabled": self.nav_route_enabled,
                "nav_avoid_enabled": self.nav_avoid_enabled,
                "service_active": self.service_active,
                "automation_paused": self.automation_paused,
                "control_mode": self.control_mode,
            }

    def build_share_payload(self) -> dict:
        """
        UDP 상태 스냅샷.
        local telemetry를 보내되, 서버 hub가 peer 상태를 함께 묶어 재전송할 수 있게 구성한다.
        """
        with self._lock:
            self._refresh_support_request_flags_unlocked()
            self.network_sequence += 1
            status = {
                "hp": self.hp,
                "mp": self.mp,
                "exp": self.exp,
                "x": self.x,
                "y": self.y,
                "good_hp": self.good_hp,
                "good_mp": self.good_mp,
                "hp_str": self.hp_str,
                "mp_str": self.mp_str,
                "exp_str": self.exp_str,
                "x_str": self.x_str,
                "y_str": self.y_str,
                "map_name": self.map_name,
                "map_floor": self.map_floor,
                "current_map": self.current_map,
                "current_floor": self.current_floor,
                "red_tab_enabled": self.red_tab_enabled,
                "target_info_text": self.target_info_text,
                "user_info_text": self.user_info_text,
                "target_kind": self.target_kind,
                "user_kind": self.user_kind,
                "heal_request": self.heal_request,
                "mp_request": self.mp_request,
                "debuff_request": self.debuff_request,
                "follow_anchor_offset": self.follow_anchor_offset,
                "safe_spot": self.safe_spot,
                "battle_spot": self.battle_spot,
                "network_role": self.network_role,
                # Existing follow logic still needs these fields.
                "last_move_dir": self.last_move_dir,
                "nav_follow_enabled": self.nav_follow_enabled,
                "nav_route_enabled": self.nav_route_enabled,
                "nav_avoid_enabled": self.nav_avoid_enabled,
                "service_active": self.service_active,
                "automation_paused": self.automation_paused,
                "control_mode": self.control_mode,
                "target_locked": self.target_locked,
            }
            return {
                "schema": "bis-state-v2",
                "sender": self.network_peer_name,
                "seq": self.network_sequence,
                "sent_at": time.time(),
                "status": status,
                "peers": {
                    name: dict(data)
                    for name, data in self.other_pc_data.items()
                    if isinstance(data, dict)
                },
            }

    def apply_remote_payload(self, payload: dict) -> tuple[str, dict]:
        """
        UDP payload를 정규화해서 저장한다.
        """
        sender = "LAPTOP"
        snapshot: dict = {}

        if isinstance(payload, dict) and isinstance(payload.get("status"), dict):
            sender = str(payload.get("sender") or sender)
            snapshot = dict(payload.get("status") or {})
            snapshot["_sent_at"] = payload.get("sent_at")
            snapshot["_schema"] = payload.get("schema", "bis-state-v2")
            snapshot["_seq"] = payload.get("seq", 0)
            snapshot["_sender"] = sender
            peers = payload.get("peers")
            if isinstance(peers, dict):
                for peer_name, peer_data in peers.items():
                    if isinstance(peer_data, dict):
                        self.update_other(str(peer_name), dict(peer_data))
        elif isinstance(payload, dict):
            snapshot = dict(payload)

        seq = int(snapshot.get("_seq", payload.get("seq", 0) if isinstance(payload, dict) else 0) or 0)
        snapshot["_received_at"] = time.time()
        self.last_network_rx_ts = float(snapshot["_received_at"] or 0.0)
        self.last_network_rx_sender = sender
        self.last_network_rx_role = str(snapshot.get("role") or snapshot.get("network_role") or "")
        self.last_network_rx_kind = str(payload.get("kind", "") if isinstance(payload, dict) else "")
        self.last_network_rx_seq = seq
        if seq:
            last_seq = int(self.remote_sequences.get(sender, 0) or 0)
            if seq <= last_seq:
                return sender, snapshot
            self.remote_sequences[sender] = seq
        self.update_other(sender, snapshot)
        return sender, snapshot

    def get_remote_data(self, preferred_name: str = "LAPTOP") -> Optional[dict]:
        """원격 상태 조회. preferred가 없으면 첫 peer를 반환한다."""
        with self._lock:
            if preferred_name and preferred_name in self.other_pc_data:
                return dict(self.other_pc_data[preferred_name])
            for _name, data in self.other_pc_data.items():
                return dict(data)
        return None

    def get_remote_data_by_role(self, role: str) -> Optional[dict]:
        with self._lock:
            for _name, data in self.other_pc_data.items():
                if not isinstance(data, dict):
                    continue
                remote_role = str(data.get("role") or data.get("network_role") or "").strip()
                if remote_role == role:
                    return dict(data)
        return self.get_remote_data()

    def queue_network_event(self, event_type: str, payload: Optional[dict] = None, repeat: int = 1) -> bool:
        """UDP ??? ?? ???? ???."""
        event = {
            "event_type": str(event_type or "").strip(),
            "payload": dict(payload or {}),
            "repeat": max(1, int(repeat or 1)),
            "queued_at": time.time(),
        }
        try:
            self.network_outbox.put_nowait(event)
            return True
        except queue.Full:
            return False

    def stop_all_inputs(self, repeat: int = 2, delay: float = 0.05, disconnect: bool = False):
        """Best-effort hardware cleanup for GUI close / panic / pause transitions."""
        tries = max(1, int(repeat or 1))
        pause = max(0.0, float(delay or 0.0))
        for idx in range(tries):
            try:
                self.panic_release()
            except Exception:
                pass
            if idx + 1 < tries and pause:
                time.sleep(pause)
        if disconnect:
            try:
                self.disconnect()
            except Exception:
                pass

    def set_game_scale(self, scale: float):
        """게임 창 배율 설정 (1.0 또는 2.0)"""
        with self._lock:
            self.game_scale = scale
            base_w, base_h = 854, 480
            new_w = int(16 + (base_w * scale))
            new_h = int(39 + (base_h * scale))
            self.game_resolution = (new_w, new_h)
            return new_w, new_h

    def _load_thresholds(self):
        """config.json에서 숫자별 OCR 임계값을 로드"""
        try:
            config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    conf = json.load(f)
                    saved = conf.get("ocr_thresholds")
                    if isinstance(saved, dict):
                        for k, v in saved.items():
                            if k in self.ocr_thresholds:
                                self.ocr_thresholds[k] = float(v)
        except Exception:
            pass

# ============================================================
# ④ HardwareController -> DevInterface (입력 가변화 핵심)
# ============================================================
class BisHardware:
    """
    하드웨어 직렬 통신을 처리하는 클래스입니다.
    무작위 패킷 패딩을 사용하여 탐지를 피합니다.
    소프트웨어 대체 입력 방법을 비활성화합니다.
    """
    def __init__(self):
        self.ser   = None  # 시리얼 포트
        self._lock = threading.Lock()  # 스레드 안전성
        self.state: Optional[GameState] = None  # 게임 상태

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

    def disconnect(self):
        """시리얼 포트 연결 해제"""
        with self._lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None

    def stop_all_inputs(self, disconnect: bool = False, repeat: int = 1, delay: float = 0.05):
        """모든 키보드/마우스 하드웨어 입력 강제 해제"""
        for _ in range(repeat):
            self.send_force("RELEASE_ALL")
            if delay > 0:
                time.sleep(delay)
        if disconnect:
            self.disconnect()

    def send(self, cmd: str):
        """
        패킷 크기 가변화 로직 적용:
        명령어 뒤에 무작위 길이의 패딩(공백)을 추가하여 전송 데이터 크기를 불규칙하게 만듦.
        """
        with self._lock:
            current_hwnd = win32gui.GetForegroundWindow()
            window_text = win32gui.GetWindowText(current_hwnd)
            is_focused = False
            
            if self.state is not None:
                # 1. 핸들 직접 비교
                if current_hwnd == self.state.hwnd:
                    is_focused = True
                # 2. 전체화면 대응: 창 제목 키워드 포함 여부 확인
                elif any(x in window_text for x in ["바람", "AION", "ory"]):
                    is_focused = True
            
            # ── 듀얼 모니터 지능형 차단 ────────────────────
            if not is_focused:
                # 키 떼기(U:) 명령은 비상 정지 및 사후 처리를 위해 무조건 허용
                if not cmd.startswith("U:"):
                    _hw_log(f"[Hardware] 차단: {cmd} (포커스 아님, hwnd={current_hwnd}, state.hwnd={self.state.hwnd if self.state else None})")
                    return

            if self.ser and self.ser.is_open:
                try:
                    # 무작위 패딩(1~16자) 추가하여 패킷 크기 가변화
                    padding = ' ' * random.randint(1, 16)
                    payload = f"{cmd}{padding}\n".encode('ascii')
                    self.ser.write(payload)
                    _hw_log(f"[Hardware] 전송: {cmd} ({len(payload)}바이트)")
                except Exception as e:
                    _hw_log(f"[Hardware] 전송 실패: {cmd} - {e}")
            else:
                _hw_log(f"[Hardware] 미전송: {cmd} (ser={self.ser}, open={self.ser.is_open if self.ser else 'N/A'})")
            # 소프트웨어 폴백(pydirectinput)은 보안상 완전히 제거됨

    def send_force(self, cmd: str):
        """포커스 체크 없이 무조건 전송 (F1 이동 등 포커스 제어가 이미 된 상황에서 사용)"""
        with self._lock:
            if self.ser and self.ser.is_open:
                try:
                    padding = ' ' * random.randint(1, 16)
                    payload = f"{cmd}{padding}\n".encode('ascii')
                    self.ser.write(payload)
                    _hw_log(f"[Hardware] 강제전송: {cmd} ({len(payload)}바이트)")
                except Exception as e:
                    _hw_log(f"[Hardware] 강제전송 실패: {cmd} - {e}")
            else:
                _hw_log(f"[Hardware] 강제전송 불가: {cmd} (ser={self.ser}, open={self.ser.is_open if self.ser else 'N/A'})")

    def _reset_serial_buffers(self):
        """Best-effort: clear buffered serial I/O to reduce stuck input risk."""
        if self.ser and self.ser.is_open:
            try:
                # 이미 쌓인 D:left, D:right 등의 발송 대기열 삭제
                self.ser.reset_output_buffer()
                self.ser.reset_input_buffer()
                # 아두이노에 씹히지 않도록 연달아 발송
                for _ in range(2):
                    self.ser.write(b"RELEASE_ALL\n")
                self.ser.flush() # 물리적 회선으로 발송이 끝날때까지 대기
            except Exception:
                pass

    def humanized_press(self, key: str, variance: float = 0.15):
        # HumanBehaviorSimulator로 동작 간 휴식 시간 시뮬레이션
        pause = HumanBehaviorSimulator.simulate_pause('click')
        time.sleep(pause)
        
        self.send(f"D:{key}")
        humanized_sleep(TIMING_CONFIG["key_down_hold"], variance)
        self.send(f"U:{key}")

    def fast_press(self, key: str, variance: float = 0.15):
        """
        Ultra-fast key press (ft.ahk-style): no HumanBehaviorSimulator pause.
        Use this for trigger reactions where speed matters.
        """
        self.send(f"D:{key}")
        humanized_sleep(TIMING_CONFIG["key_down_hold"], variance)
        self.send(f"U:{key}")

    def force_press(self, key: str, variance: float = 0.15):
        """포커스 체크 없이 1회 키 입력. 이동 중 테스트 마법처럼 강제 전송이 필요할 때 사용."""
        # 단발 입력은 아두이노의 K,<key> 경로가 가장 안정적이다.
        # D/U 분리보다 전송 횟수가 적고, 키업 타이밍이 짧아지는 문제를 피한다.
        self.send_force(f"K,{key}")

    def press_with_gap(self, key: str, variance: float = 0.15):
        self.humanized_press(key, variance)
        # HumanBehaviorSimulator로 타이핑 동작 간 휴식 시간 시뮬레이션
        pause = HumanBehaviorSimulator.simulate_pause('type')
        time.sleep(pause)

    def panic_escape(self, count: int = None):
        n = count if count is not None else random.randint(2, 3)
        for _ in range(n):
            self.humanized_press("esc")
            humanized_sleep(TIMING_CONFIG["esc_gap"])

    def panic_release(self):
        """모든 하드웨어 신호를 즉시 소거 (비상 정지용)"""
        # Combine: clear buffers + release keys.
        try:
            self._reset_serial_buffers()
        except Exception:
            pass
        _hw_log("[Hardware] 모든 신호 강제 해제 (Panic Release)")
        keys = ["left", "right", "up", "down", "shift", "ctrl", "alt", "esc", "space", "enter"]
        for k in keys:
            self.send(f"U:{k}")
        self.send("U:all")

    def hold_move(self, direction: str, hold_key: str = "move_hold",
                  variance: float = 0.15, duration: float = None):
        # HumanBehaviorSimulator로 이동 동작 간 휴식 시간 시뮬레이션
        pause = HumanBehaviorSimulator.simulate_pause('move')
        time.sleep(pause)
        
        _hw_log(f"[Move] 방향키: {direction}")
        try:
            self.send_force(f"D:{direction}")
            if duration is not None:
                humanized_sleep(float(duration), variance)
            else:
                humanized_sleep(TIMING_CONFIG[hold_key], variance)
        finally:
            self.send_force(f"U:{direction}")

    def click(self, grid_x: int, grid_y: int, grid_manager=None):
        """
        Grid 좌표를 받아 Pixel 좌표로 변환 후 클릭
        grid_x, grid_y: 그리드 좌표 (0-based)
        grid_manager: GridManager 인스턴스 (선택사항, 없으면 state에서 가져옴)
        """
        if grid_manager is None and self.state:
            # GameState에서 GridManager 인스턴스 가져오기 (필요시)
            # 현재 구조에서는 직접 전달받는 것이 안전
            pass
        
        if grid_manager:
            pixel_pos = grid_manager.to_pixel(grid_x, grid_y)
            if pixel_pos:
                px, py = pixel_pos
                _hw_log(f"[Click] Grid({grid_x}, {grid_y}) -> Pixel({px}, {py})")
                # 마우스 클릭 구현 (win32api 사용)
                try:
                    import win32api
                    import win32con
                    # 절대 좌표로 클릭
                    win32api.SetCursorPos((px, py))
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                    time.sleep(0.05)
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                except Exception as e:
                    _hw_log(f"[Click] 마우스 클릭 실패: {e}")
            else:
                _hw_log(f"[Click] Grid 좌표 변환 실패: ({grid_x}, {grid_y})")
        else:
            _hw_log(f"[Click] GridManager가 없어 클릭 불가: ({grid_x}, {grid_y})")

hw = BisHardware()


# ============================================================
# ⑤ GridManager  ─  픽셀/그리드 좌표 변환
# ============================================================
class GridManager:
    """
    픽셀 좌표와 그리드 좌표 간 변환을 담당하는 클래스
    맵별 grid_cols, grid_rows를 지원하여 유연한 그리드 시스템 제공
    """
    def __init__(self, state, grid_size=48):
        self.state = state
        self.grid_size = grid_size  # 48px (2배 크기)
        self.margin = 24  # 초기 여백
        self.grid_start_x = None  # 격자 시작점
        self.grid_start_y = None
        self.grid_cols = 20  # 가로 타일 수 (맵별 설정으로 변경 가능)
        self.grid_rows = 17  # 세로 타일 수 (맵별 설정으로 변경 가능)
        self.play_area = None  # play_area 좌표 (sx, sy, dx, dy)

    def set_map_config(self, grid_cols, grid_rows, play_area=None):
        """
        맵별 그리드 설정 적용
        grid_cols: 가로 타일 수
        grid_rows: 세로 타일 수
        play_area: (sx, sy, dx, dy) 튜플 (선택사항)
        """
        self.grid_cols = grid_cols
        self.grid_rows = grid_rows
        if play_area:
            self.play_area = play_area
            # play_area에서 격자 시작점 계산
            sx, sy, dx, dy = play_area
            self.grid_start_x = sx
            self.grid_start_y = sy

    def set_window_size(self, w, h):
        """창 크기에서 격자 시작점 계산 (전체화면 grid_cols 기준)"""
        cell_w = w // self.grid_cols
        cell_h = h // self.grid_rows
        
        # D1 위치: col=3, row=0 (고정)
        d1_x = 3 * cell_w
        d1_y = 0 * cell_h
        
        # 격자 시작점 (D1 위치 + 24px 여백)
        self.grid_start_x = d1_x + self.margin
        self.grid_start_y = d1_y + self.margin

    def to_grid(self, pixel_x, pixel_y):
        """
        픽셀 좌표 → 그리드 좌표 변환 (정수 인덱스)
        맵별 cols/rows를 사용하여 정확한 그리드 좌표 계산
        GridX = (PixelX - play_area.sx) / (play_area.width / grid_cols)
        GridY = (PixelY - play_area.sy) / (play_area.height / grid_rows)
        """
        if self.play_area:
            sx, sy, dx, dy = self.play_area
            play_width = dx - sx
            play_height = dy - sy
            
            if play_width > 0 and play_height > 0:
                gx = (pixel_x - sx) / (play_width / self.grid_cols)
                gy = (pixel_y - sy) / (play_height / self.grid_rows)
                
                # 유효성 검사
                if gx < 0 or gy < 0 or gx >= self.grid_cols or gy >= self.grid_rows:
                    return None
                
                return (int(gx), int(gy))
        
        # play_area가 없는 경우 기존 방식 사용
        if self.grid_start_x is None or self.grid_start_y is None:
            return None
        
        gx = (pixel_x - self.grid_start_x) // self.grid_size
        gy = (pixel_y - self.grid_start_y) // self.grid_size
        
        if gx < 0 or gy < 0 or gx >= self.grid_cols or gy >= self.grid_rows:
            return None
        
        return (int(gx), int(gy))

    def to_pixel(self, grid_x, grid_y):
        """
        그리드 좌표 → 픽셀 좌표 변환
        맵별 cols/rows를 사용하여 정확한 픽셀 좌표 계산
        PixelX = play_area.sx + (grid_x * play_area.width / grid_cols)
        PixelY = play_area.sy + (grid_y * play_area.height / grid_rows)
        """
        if self.play_area:
            sx, sy, dx, dy = self.play_area
            play_width = dx - sx
            play_height = dy - sy
            
            if play_width > 0 and play_height > 0:
                px = sx + (grid_x * play_width / self.grid_cols)
                py = sy + (grid_y * play_height / self.grid_rows)
                return (int(px), int(py))
        
        # play_area가 없는 경우 기존 방식 사용
        if self.grid_start_x is None or self.grid_start_y is None:
            return None
        
        px = grid_x * self.grid_size + self.grid_start_x
        py = grid_y * self.grid_size + self.grid_start_y
        return (int(px), int(py))

    def get_surrounding_grids(self, center_grid_x, center_grid_y):
        """
        중심 그리드 좌표 기준 상하좌우 그리드 좌표 반환
        반환: (현재, 왼쪽, 오른쪽, 위, 아래)
        """
        current = (center_grid_x, center_grid_y)
        left = (center_grid_x - 1, center_grid_y)
        right = (center_grid_x + 1, center_grid_y)
        up = (center_grid_x, center_grid_y - 1)
        down = (center_grid_x, center_grid_y + 1)
        return current, left, right, up, down

    def grid_to_name(self, grid_x, grid_y):
        """
        그리드 좌표 → 엑셀 방식 이름 변환
        예: (0, 0) → "A1", (1, 1) → "B2"
        """
        if grid_x < 0 or grid_y < 0:
            return None
        col_name = chr(ord('A') + grid_x)
        row_name = str(grid_y + 1)
        return f"{col_name}{row_name}"

    def name_to_grid(self, grid_name: str):
        """
        엑셀 방식 이름 → 그리드 좌표 변환
        예: "A1" → (0, 0), "B2" → (1, 1)
        """
        if not grid_name or len(grid_name) < 2:
            return None, None
        
        # 컬럼 추출 (알파벳 부분)
        col_part = ""
        row_part = ""
        
        for char in grid_name:
            if char.isalpha():
                col_part += char.upper()
            elif char.isdigit():
                row_part += char
        
        if not col_part or not row_part:
            return None, None
        
        # 컬럼 인덱스 계산 (A=0, B=1, ..., Z=25, AA=26, ...)
        col_idx = 0
        for char in col_part:
            col_idx = col_idx * 26 + (ord(char) - ord('A'))
        
        # 행 인덱스 계산 (1-based → 0-based)
        row_idx = int(row_part) - 1
        
        return col_idx, row_idx
