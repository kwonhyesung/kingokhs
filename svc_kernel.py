import time
import threading
import serial
import random
import os
import json
import csv
import re
import queue

VERBOSE_STATE_LOGS = os.environ.get("SVC_VERBOSE_LOGS", "0") == "1"

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import win32gui
from svc_stealth import HumanBehaviorSimulator

# ============================================================
# ① TIMING_CONFIG  ─  시스템 딜레이 제어 테이블
# ============================================================
TIMING_CONFIG: dict = {
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
    "bomu_spell_gap" : 0.200,
    "bomu_interval"  : 185.0,
    "debuff_key_wait": 0.035,    # 35ms
    "debuff_up_wait" : 0.100,
    "debuff_ocr_wait": 0.150,
    "move_hold"      : 0.100,    # 100ms (이동 인식 최소 시간)
    "nav_loop"       : 0.010,
    "idle_sleep"     : 0.100,
    "stuck_time"     : 2.0,
    "stuck_back_hold": 0.100,    # 100ms
    "stuck_side_hold": 0.100,    # 100ms
    "combat_timeout" : 12.0,
    "action_loop"    : 0.040,
    "esc_gap"        : 0.035,    # 35ms
    "entry_hold"     : 1.0,      # 포탈/입구 진입 시 키 유지 시간
    "exit_hold"      : 1.0,      # 포탈/출구 진입 시 키 유지 시간
}

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


def normalize_game_hotkey(key: str | None) -> str:
    normalized = str(key or "").strip()
    if not normalized:
        return ""
    return GAME_HOTKEY_ALIAS.get(normalized, normalized)


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

    def __post_init__(self):
        resolved = normalize_game_hotkey(self.hotkey or self.spell_char)
        if resolved:
            self.hotkey = resolved

    def is_ready(self) -> bool:
        return (time.time() - self.last_cast_time) >= self.cooldown

    def effective_key(self) -> str:
        return normalize_game_hotkey(self.hotkey or self.spell_char)

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

class GameState:
    def __init__(self):
        self._lock = threading.Lock()
        self.entities = {}  # 모든 필드 엔티티 (나, 유저, 몬스터 등) 저장소
        self.hwnd    = None
        self.hp      = 0
        self.mp      = 0
        self.exp     = 0
        self.money   = 0
        self.x       = 0
        self.y       = 0
        self.pos_x   = 0
        self.pos_y   = 0
        self.pos_last_update_time = 0.0
        self.pos_update_seq = 0
        self.good_hp = 0
        self.good_mp = 0
        # OCR 원문(또는 누락 포함) 문자열. x/y는 4자리 고정, 누락은 'x'로 채움.
        self.hp_str = ""
        self.mp_str = ""
        self.exp_str = ""
        self.money_str = ""
        self.x_str = ""
        self.y_str = ""
        self.map_name = ""
        self.map_floor = ""
        self.current_floor = ""
        self.red_tab_enabled = False
        self.target_info_text = ""
        self.user_info_text = ""
        self.target_kind = "UNKNOWN"
        self.user_kind = "UNKNOWN"
        self.heal_request = False
        self.mp_request = False
        self.debuff_request = False
        self.follow_anchor_offset = (0, 0)
        self.safe_spot = None
        self.battle_spot = None
        self.target_locked = False
        self.last_update_time = time.time()
        self.running          = True
        self.bin_threshold    = 128
        self.ocr_thresholds   = {str(i): 0.35 for i in range(10)}
        self._load_thresholds()
        self.ocr_fps          = 0.0 # 실시간 인식 속도
        self.ocr_preview_img  = None # 현재 인식 중인 돋보기 화면
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
        self.route_waypoints_enabled = False
        # Grid overlay 관련 (SentinelThread가 GameState에서 참조)
        self.grid_overlay_enabled = False
        self.grid_offset = (0, 0)  # (offset_x, offset_y)
        # 전투 중 이동 제어 플래그
        self.is_combat_busy = False
        self.nav_avoid_enabled  = True
        self.reverse_mode        = False  # 역방향 회항 모드
        self.waypoints_db       = {}
        self.current_map        = "기본맵"
        self.follow_target_pos  = None
        self.last_pos           = (0, 0)
        self.stuck_start_time   = 0.0
        self.last_key_context = "NONE"
        self.target_type      = "NONE"
        self.target_name      = ""
        self.user_name        = ""
        self.is_chat_active   = False
        self.shield_active      = False
        # MonitorSvc와 공유용 프레임 및 파라미터
        self.last_frame = None
        self.last_frame_time = 0
        self.obs_params = (0, 0, 1.0, 1.0)
        self.shield_expire_time = 0.0
        self.target_monster_names: list[str] = []
        self.whitelist_names:      list[str] = []
        self.last_event_log:       str = "없음"  # 최근 이벤트 기록용
        self.load_whitelist_csv()  # whitelist.csv 자동 로드
        self.last_debuff_x         = 0
        self.last_debuff_y         = 0
        # 캐릭터 중심 상대 좌표계
        self.my_screen_pos         = (0, 0)  # my_arrow 화면 좌표 (screen_x, screen_y)
        self.my_world_pos          = (0, 0)  # OCR로 읽은 월드 좌표 (world_x, world_y)
        self.detected_entities     = []      # 월드 좌표 역계산된 엔티티 리스트
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
        self.network_role = "도사"
        self.network_server_ip = "192.168.137.1"
        self.network_bind_host = "0.0.0.0"
        self.network_telemetry_port = 5555
        self.network_local_port = 5556
        self.network_peer_name = os.environ.get("COMPUTERNAME", "LOCAL")
        self.network_outbox = queue.Queue(maxsize=256)
        self.network_sequence = 0
        self.remote_sequences = {}
        
        # 해상도 관련 속성
        self.game_resolution = (854, 480)  # 기본 해상도
        self.game_scale = 2.0  # 배율 (1배 또는 2배)
        self.gui_resolution = (640, 480)  # GUI 창 해상도
        
        # 역할 관련 속성 (격수/도사/술사)
        self.role = "격수"  # 기본 역할: 격수, 도사, 술사
        
        # SentinelThread 관련 속성 (술사 PC 전용 보안 감시병)
        self.monster_on_screen = False  # 화면에 몬스터 존재 여부 (서버로 브로드캐스팅)
        self.is_user_detected = False  # 미확인 유저 감지 여부 (알람 플래그)
        self.safe_zone_detected = False  # 안전 지대 감지 여부 (화이트리스트 유저 5회 연속 감지)
        self.is_combat_busy = False  # 전투 중 상태 (공력증강 사용 후 5초 이내)
        self.sentinel_enabled = False  # SentinelThread 활성화 플래그
        self.other_pc_data: dict = {}   # 다른 PC 상태 데이터

        # 네트워크 동기화 (다른 PC 데이터)
        self.is_connected = False  # 네트워크 연결 상태
        self.last_move_dir = None  # 마지막 이동 방향 (격수 추적용)
        self.is_stuck = False  # 막힘 상태 (회피 기동 중)
        self.grid_overlay_enabled = False  # 그리드 오버레이 활성화 플래그

    # ------------------------------------------------------------------
    # whitelist.csv 관련
    # ------------------------------------------------------------------
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
                    print(f"[Whitelist] CSV 로드 오류: {e}")
        with self._lock:
            self.whitelist_names = names
        if VERBOSE_STATE_LOGS:
            print(f"[Whitelist] {len(names)}명 로드: {names}")

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
                    # 새로운 속성이거나 엔티티 정보인 경우에도 수용
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

    def get_all(self) -> dict:
        with self._lock:
            return {
                "hp": self.hp, "mp": self.mp,
                "exp": self.exp, "money": self.money,
                "x": self.x, "y": self.y,
                "pos_x": getattr(self, "pos_x", self.x),
                "pos_y": getattr(self, "pos_y", self.y),
                "pos_last_update_time": getattr(self, "pos_last_update_time", 0.0),
                "pos_update_seq": getattr(self, "pos_update_seq", 0),
                "good_hp": self.good_hp,
                "good_mp": self.good_mp,
                "hp_str": getattr(self, "hp_str", ""),
                "mp_str": getattr(self, "mp_str", ""),
                "exp_str": getattr(self, "exp_str", ""),
                "money_str": getattr(self, "money_str", ""),
                "x_str": getattr(self, "x_str", ""),
                "y_str": getattr(self, "y_str", ""),
                "map_name": getattr(self, "map_name", ""),
                "map_floor": getattr(self, "map_floor", ""),
                "current_map": getattr(self, "current_map", ""),
                "current_floor": getattr(self, "current_floor", ""),
                "red_tab_enabled": getattr(self, "red_tab_enabled", False),
                "target_info_text": getattr(self, "target_info_text", ""),
                "user_info_text": getattr(self, "user_info_text", ""),
                "target_kind": getattr(self, "target_kind", "UNKNOWN"),
                "user_kind": getattr(self, "user_kind", "UNKNOWN"),
                "heal_request": getattr(self, "heal_request", False),
                "mp_request": getattr(self, "mp_request", False),
                "debuff_request": getattr(self, "debuff_request", False),
                "follow_anchor_offset": getattr(self, "follow_anchor_offset", (0, 0)),
                "safe_spot": getattr(self, "safe_spot", None),
                "battle_spot": getattr(self, "battle_spot", None),
                "network_role": getattr(self, "network_role", "도사"),
                "target_locked": self.target_locked,
                "last_move_dir": self.last_move_dir,
                "nav_follow_enabled": self.nav_follow_enabled
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

    def _load_thresholds(self):
        """config.json에서 숫자별 OCR 임계값을 로드"""
        try:
            config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.local.json")
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
                    self._reset_serial_buffers()
                except Exception:
                    pass
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None

    def _reset_serial_buffers(self):
        """Best-effort: clear buffered serial I/O to reduce stuck input risk."""
        if self.ser and self.ser.is_open:
            try:
                self.ser.reset_output_buffer()
                self.ser.reset_input_buffer()
                for _ in range(2):
                    self.ser.write(b"RELEASE_ALL\n")
                self.ser.flush()
            except Exception:
                pass

    def _is_game_window_focused(self) -> bool:
        try:
            current_hwnd = win32gui.GetForegroundWindow()
            if not current_hwnd:
                return False
            if self.state is not None and getattr(self.state, "hwnd", None):
                if current_hwnd == self.state.hwnd:
                    return True
            try:
                window_text = win32gui.GetWindowText(current_hwnd) or ""
            except Exception:
                window_text = ""
            return any(keyword in window_text for keyword in ["??", "AION", "ory"])
        except Exception:
            return False

    def _write_serial_command(self, cmd: str, *, require_focus: bool = True, log_prefix: str = "[Hardware]") -> bool:
        if require_focus and not self._is_game_window_focused():
            print(f"{log_prefix} blocked: {cmd} (game window not focused)")
            return False
        if self.ser and self.ser.is_open:
            try:
                padding = ' ' * random.randint(1, 16)
                payload = f"{cmd}{padding}\n".encode('ascii')
                self.ser.write(payload)
                print(f"{log_prefix} sent: {cmd} ({len(payload)} bytes)")
                return True
            except Exception as e:
                print(f"{log_prefix} send failed: {cmd} - {e}")
        else:
            print(f"{log_prefix} not sent: {cmd} (ser={self.ser}, open={self.ser.is_open if self.ser else 'N/A'})")
        return False

    def send(self, cmd: str):
        """
        Packet size variability:
        add a random-length padding to keep payload sizes irregular.
        """
        with self._lock:
            self._write_serial_command(cmd, require_focus=True, log_prefix="[Hardware]")

    def send_force(self, cmd: str):
        """Send with the same foreground-window guard as normal input."""
        with self._lock:
            self._write_serial_command(cmd, require_focus=True, log_prefix="[Hardware]")

    def panic_release(self):
        """?? ???? ??? ?? ?? (?? ???)"""
        try:
            self._reset_serial_buffers()
        except Exception:
            pass
        print("[Hardware][Cleanup] ?? ?? ?? ?? (Panic Release)")
        keys = ["left", "right", "up", "down", "shift", "ctrl", "alt", "esc", "space", "enter"]
        for k in keys:
            self._write_serial_command(f"U:{k}", require_focus=False, log_prefix="[Hardware][Cleanup]")
        self._write_serial_command("U:all", require_focus=False, log_prefix="[Hardware][Cleanup]")

    def stop_all_inputs(self, repeat: int = 2, delay: float = 0.05, disconnect: bool = False):
        """Best-effort cleanup for GUI close / panic / pause transitions."""
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

    def hold_move(self, direction: str, hold_key: str = "move_hold", variance: float = 0.15):
        if not self._is_game_window_focused():
            print(f"[Move] blocked: {direction} (game window not focused)")
            return
        # HumanBehaviorSimulator? ?? ?? ? ?? ?? ?????
        pause = HumanBehaviorSimulator.simulate_pause('move')
        time.sleep(pause)
        
        print(f"[Move] ???: {direction}")
        try:
            self.send_force(f"D:{direction}")
            humanized_sleep(TIMING_CONFIG[hold_key], variance)
        finally:
            self.send_force(f"U:{direction}")
hw = BisHardware()


# ============================================================
#  GridManager  -  Grid/Pixel coordinate conversion
# ============================================================
class GridManager:
    def __init__(self, state, grid_size=48):
        self.state = state
        self.grid_size = grid_size
        self.margin = 24
        self.grid_start_x = None
        self.grid_start_y = None
        self.grid_cols = 20
        self.grid_rows = 17
        self.play_area = None

    def set_map_config(self, grid_cols, grid_rows, play_area=None):
        self.grid_cols = grid_cols
        self.grid_rows = grid_rows
        if play_area:
            self.play_area = play_area
            sx, sy, dx, dy = play_area
            self.grid_start_x = sx
            self.grid_start_y = sy

    def set_window_size(self, w, h):
        cell_w = w // self.grid_cols
        cell_h = h // self.grid_rows
        d1_x = 3 * cell_w
        d1_y = 0 * cell_h
        self.grid_start_x = d1_x + self.margin
        self.grid_start_y = d1_y + self.margin

    def to_grid(self, pixel_x, pixel_y):
        if self.play_area:
            sx, sy, dx, dy = self.play_area
            play_width = dx - sx
            play_height = dy - sy
            if play_width > 0 and play_height > 0:
                gx = (pixel_x - sx) // (play_width / self.grid_cols)
                gy = (pixel_y - sy) // (play_height / self.grid_rows)
                if gx < 0 or gy < 0 or gx >= self.grid_cols or gy >= self.grid_rows:
                    return None
                return (int(gx), int(gy))

        if self.grid_start_x is None or self.grid_start_y is None:
            return None

        gx = (pixel_x - self.grid_start_x) // self.grid_size
        gy = (pixel_y - self.grid_start_y) // self.grid_size
        if gx < 0 or gy < 0 or gx >= self.grid_cols or gy >= self.grid_rows:
            return None
        return (int(gx), int(gy))

    def to_pixel(self, grid_x, grid_y):
        if self.play_area:
            sx, sy, dx, dy = self.play_area
            play_width = dx - sx
            play_height = dy - sy
            if play_width > 0 and play_height > 0:
                px = sx + (grid_x * play_width / self.grid_cols)
                py = sy + (grid_y * play_height / self.grid_rows)
                return (int(px), int(py))

        if self.grid_start_x is None or self.grid_start_y is None:
            return None

        px = grid_x * self.grid_size + self.grid_start_x
        py = grid_y * self.grid_size + self.grid_start_y
        return (int(px), int(py))

    def get_surrounding_grids(self, center_grid_x, center_grid_y):
        current = (center_grid_x, center_grid_y)
        left = (center_grid_x - 1, center_grid_y)
        right = (center_grid_x + 1, center_grid_y)
        up = (center_grid_x, center_grid_y - 1)
        down = (center_grid_x, center_grid_y + 1)
        return current, left, right, up, down

    def grid_to_name(self, grid_x, grid_y):
        if grid_x < 0 or grid_y < 0:
            return None
        col_name = chr(ord('A') + grid_x)
        row_name = str(grid_y + 1)
        return f"{col_name}{row_name}"

    def name_to_grid(self, grid_name):
        if not grid_name or len(grid_name) < 2:
            return None
        grid_name = str(grid_name).strip().upper()
        col = ord(grid_name[0]) - ord('A')
        try:
            row = int(grid_name[1:]) - 1
        except Exception:
            return None
        if col < 0 or row < 0:
            return None
        return (col, row)
