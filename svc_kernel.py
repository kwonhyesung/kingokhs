import time
import threading
import serial
import random
import os
import json
import csv

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
        # OCR 원문(또는 누락 포함) 문자열. x/y는 4자리 고정, 누락은 'x'로 채움.
        self.hp_str = ""
        self.mp_str = ""
        self.exp_str = ""
        self.money_str = ""
        self.x_str = ""
        self.y_str = ""
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
                "hp_str": getattr(self, "hp_str", ""),
                "mp_str": getattr(self, "mp_str", ""),
                "exp_str": getattr(self, "exp_str", ""),
                "money_str": getattr(self, "money_str", ""),
                "x_str": getattr(self, "x_str", ""),
                "y_str": getattr(self, "y_str", ""),
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
                    print(f"[Hardware] 차단: {cmd} (포커스 아님, hwnd={current_hwnd}, state.hwnd={self.state.hwnd if self.state else None})")
                    return

            if self.ser and self.ser.is_open:
                try:
                    # 무작위 패딩(1~16자) 추가하여 패킷 크기 가변화
                    padding = ' ' * random.randint(1, 16)
                    payload = f"{cmd}{padding}\n".encode('ascii')
                    self.ser.write(payload)
                    print(f"[Hardware] 전송: {cmd} ({len(payload)}바이트)")
                except Exception as e:
                    print(f"[Hardware] 전송 실패: {cmd} - {e}")
            else:
                print(f"[Hardware] 미전송: {cmd} (ser={self.ser}, open={self.ser.is_open if self.ser else 'N/A'})")
            # 소프트웨어 폴백(pydirectinput)은 보안상 완전히 제거됨

    def send_force(self, cmd: str):
        """포커스 체크 없이 무조건 전송 (F1 이동 등 포커스 제어가 이미 된 상황에서 사용)"""
        with self._lock:
            if self.ser and self.ser.is_open:
                try:
                    padding = ' ' * random.randint(1, 16)
                    payload = f"{cmd}{padding}\n".encode('ascii')
                    self.ser.write(payload)
                    print(f"[Hardware] 강제전송: {cmd} ({len(payload)}바이트)")
                except Exception as e:
                    print(f"[Hardware] 강제전송 실패: {cmd} - {e}")
            else:
                print(f"[Hardware] 강제전송 불가: {cmd} (ser={self.ser}, open={self.ser.is_open if self.ser else 'N/A'})")

    def panic_release(self):
        """강제 정지 시 파이썬 측 통신 버퍼를 싹 비우고 아두이노에 키보드 즉시 해제 명령 강제 전송. (락 무시)"""
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
        print("[Hardware] 모든 신호 강제 해제 (Panic Release)")
        keys = ["left", "right", "up", "down", "shift", "ctrl", "alt", "esc", "space", "enter"]
        for k in keys:
            self.send(f"U:{k}")
        self.send("U:all")

    def hold_move(self, direction: str, hold_key: str = "move_hold", variance: float = 0.15):
        # HumanBehaviorSimulator로 이동 동작 간 휴식 시간 시뮬레이션
        pause = HumanBehaviorSimulator.simulate_pause('move')
        time.sleep(pause)
        
        print(f"[Move] 방향키: {direction}")
        try:
            self.send_force(f"D:{direction}")
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
                print(f"[Click] Grid({grid_x}, {grid_y}) -> Pixel({px}, {py})")
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
                    print(f"[Click] 마우스 클릭 실패: {e}")
            else:
                print(f"[Click] Grid 좌표 변환 실패: ({grid_x}, {grid_y})")
        else:
            print(f"[Click] GridManager가 없어 클릭 불가: ({grid_x}, {grid_y})")

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
