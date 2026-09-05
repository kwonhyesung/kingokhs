import time
import threading
import random
import os
import json
import csv
import re
import queue
import serial

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
    """
    인간처럼 자연스러운 지연시간 생성 함수 (+20% 추가 랜덤화)
    
    Args:
        base_time (float): 기본 지연시간 (초 단위)
        variance (float): 변동성 계수 (기본값 0.15 = 15% 변동)
    
    Algorithm:
        1. 기존 15% + 추가 20% = 최대 35% 랜덤 변동성 적용
        2. 가우시안 분포로 랜덤 지연시간 생성 (평균=base_time, 표준편차=base_time*final_variance*0.5)
        3. 최소/최대 범위 제한 (50%~200% 사이)
        4. 계산된 지연시간으로 sleep
        
    Purpose:
        - Anti-cheat 탐지 회피 (기계적 패턴 방지)
        - 자연스러운 키 입력 간격 구현
        - 사용자 행동 패턴 시뮬레이션
        - TIMING_CONFIG 값 기반 추가 랜덤화 (35% 변동성)
    """
    # 기존 15% + 추가 20% = 최대 35% 랜덤 변동성
    final_variance = variance + 0.2
    sigma  = base_time * final_variance * 0.5  # 표준편차 계산
    delay  = random.gauss(base_time, sigma)  # 가우시안 분포로 랜덤 지연
    delay  = max(base_time * 0.50, min(base_time * 2.0, delay))  # 50%~200% 범위 제한
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
    """
    스킬 타겟팅 방식 정의
    
    Purpose:
        - 스킬별 적절한 캐스팅 함수 매핑
        - target_type 기반 자동 스킬 시전 로직
        - UI에서 스킬 설정 시 선택 옵션 제공
    """
    INSTANT       = "TYPE_I"     # 즉발형: 바로 시전되는 스킬 (버프, 디버프 등)
    TARGET_SELECT = "TYPE_S"     # 대상선택형: 방향키로 타겟 선택 필요 (공격 스킬 등)
    TARGET_INPUT  = "TYPE_A"     # 대상입력형: 텍스트로 타겟 입력 필요 (특수 스킬)

class AppStatus(Enum):
    """
    애플리케이션 상태 정의 (FSM - Finite State Machine)
    
    Purpose:
        - 현재 시스템 상태 추적 및 관리
        - 상태별 우선순위 처리 로직
        - 디버깅 및 로깅을 위한 상태 식별
        - 상태 전환 규칙 정의
    
    State Flow:
        SETUP -> IDLE -> (COMBAT/SCANNING/EMERGENCY_HEAL) -> BUFFING/MANA_REGEN -> IDLE
    """
    SETUP          = "ST_0"        # 초기 설정 상태: 시스템 준비 중
    IDLE           = "ST_1"        # 대기 상태: 특별한 동작 없음, 기본 루프 실행
    EMERGENCY_HEAL = "ST_2"        # 비상 치유 상태: HP/MP 긴급 회복 필요
    MANA_REGEN     = "ST_3"        # 마나 재생 상태: MP 회복 스킬 사용 중
    BUFFING        = "ST_4"        # 버프 상태: 보호/무장 버프 적용 중
    DEBUFFING      = "ST_5"        # 디버프 상태: 적에게 디버프 스킬 사용 중
    COMBAT         = "ST_6"        # 전투 상태: 몬스터와 전투 중
    SCANNING       = "ST_7"        # 스캔 상태: 타겟 탐색 및 주변 스캔 중
    STUCK_ESCAPE   = "ST_8"        # 탈출 상태: 막힘 감지 후 이동 탈출 중

@dataclass
class Skill:
    """
    스킬 데이터 구조 정의
    
    Purpose:
        - spells_config.json 스킬 정보 매핑
        - 스킬 쿨다운 및 사용 가능 여부 관리
        - 스킬별 속성 및 설정 저장
        - UI에서 스킬 설정 및 관리
    
    Integration:
        - spell_casting.py: 스킬 캐스팅 함수 연동
        - svc_logic.py: 스킬 사용 로직에서 참조
        - svc_worker.py: GUI 스킬 설정 관리
    """
    name:           str              # 스킬 이름 (UI 표시 및 로깅용)
    target_type:    str              # 타겟 타입 (TargetType Enum 참조)
    hotkey:         Optional[str] = None  # 기존 핫키 (하위 호환성용)
    spell_char:     Optional[str] = None  # 스킬 문자 (Shift+Z+문자 조합용)
    enable_red_tab: bool          = False  # RedTab 활성화 여부 (UI 타겟팅)
    category:       str           = "GENERIC"  # 스킬 카테고리 (공격/회복/버프/디버프)
    cooldown:       float         = 0.0      # 스킬 쿨다운 시간 (초 단위)
    last_cast_time: float         = 0.0      # 마지막 스킬 사용 시간

    def is_ready(self) -> bool:
        """
        스킬 사용 가능 여부 확인
        
        Returns:
            bool: 쿨다운이 지났으면 True, 아니면 False
            
        Purpose:
            - 스킬 중복 사용 방지
            - 정확한 쿨다운 계산
            - 스킬 사용 제어 로직
        """
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

from bis_core import find_game_window  # 창 탐색은 bis_core 한 곳에만 둔다


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
        self.good_hp = 100000
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
        self.my_facing             = ""     # 내 캐릭터가 보는 방향 패턴 이름
        self.my_world_pos          = (0, 0)  # OCR로 읽은 월드 좌표 (world_x, world_y)
        self.detected_entities     = []      # 월드 좌표 역계산된 엔티티 리스트
        self.last_bomu_time        = 0.0
        self.auto_debuff_enabled   = True
        self.recovery_debuff_spell = "NONE"
        self.user_alarm_enabled = True
        self.user_stop_enabled  = False
        self.user_next_enabled  = True
        self.combat_start_time = 0.0
        self.visual_markers = []    # [{ 'x', 'y', 'color', 'size', 'expiry' }, ...] 오버레이용
        self.maps_db = {}
        self.char_grid = (0, 0)
        self.detected_item_grid = None
        self.detected_item_name = ""
        self.detected_item_world = None
        self.detected_monster_world = None
        self.detected_monsters_world = []  # [{"name","world":(x,y)}, ...] 자동사냥용 전체 목록
        self.item_pickup_target = None   # (tx, ty) world 좌표. LogicSvc가 설정, RouteSvc가 이동 실행
        self.item_pickup_arrived = False  # RouteSvc가 도착 시 True. LogicSvc가 소비 후 리셋
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
        self._auto_reconnect_enabled = True  # 자동 재연결 활성화
        self._last_connect_attempt = 0  # 마지막 연결 시도 시간
        self._preferred_port = None  # 필요할 때만 수동으로 우선 포트 지정

    def set_state(self, state: GameState):
        """게임 상태 설정"""
        self.state = state
        
        # 상태가 연결되면 Bluetooth를 제외한 USB 시리얼 포트를 자동 탐색한다.
        if not (self.ser and self.ser.is_open):
            self.auto_reconnect()

    def _list_candidate_ports(self):
        """Bluetooth 가상 COM 포트를 제외한 USB 시리얼 포트만 반환한다."""
        import serial.tools.list_ports

        candidates = []
        for port in serial.tools.list_ports.comports():
            device = (port.device or "").upper()
            description = (port.description or "").upper()
            hwid = (port.hwid or "").upper()
            manufacturer = (getattr(port, "manufacturer", None) or "").upper()

            if not device.startswith("COM"):
                continue
            if "BTH" in hwid or "BLUETOOTH" in description or "BLUETOOTH" in manufacturer:
                continue
            if "USB" in description or "USB" in hwid or "VID_" in hwid:
                candidates.append(port.device)

        return candidates

    def connect(self, port: str) -> bool:
        """시리얼 포트 연결 (115200 baud)"""
        try:
            self.ser = serial.Serial(port, 115200, timeout=1)
            print(f"[Hardware] 연결 성공: {port}")
            
            # 연결 성공 후 service_active 자동 활성화
            if self.state:
                self.state.service_active = True
                print("[Hardware] service_active 자동 활성화")
            
            return True
        except Exception as e:
            print(f"[Hardware] 연결 실패: {port} - {e}")
            return False

    def auto_reconnect(self) -> bool:
        """자동 재연결 기능"""
        if not self._auto_reconnect_enabled:
            return False
            
        current_time = time.time()
        # 1초 간격으로 재연결 시도 (더 빠른 재연결)
        if current_time - self._last_connect_attempt < 1:
            return False
            
        self._last_connect_attempt = current_time
        print("[Hardware] 자동 재연결 시도 시작...")
        
        available_ports = self._list_candidate_ports()
        
        if not available_ports:
            print("[Hardware] 사용 가능한 USB 시리얼 포트 없음 (Bluetooth 제외)")
            return False
            
        print(f"[Hardware] 발견된 USB 시리얼 포트: {available_ports}")
        
        # 수동 우선 포트가 있으면 먼저 시도
        if self._preferred_port in available_ports:
            print(f"[Hardware] 우선 USB 포트 시도: {self._preferred_port}")
            if self.connect(self._preferred_port):
                print(f"[Hardware] 우선 USB 포트 연결 성공: {self._preferred_port}")
                return True
        
        # 나머지 USB 시리얼 포트 시도
        for port in available_ports:
            if port == self._preferred_port:
                continue  # 이미 시도함
            print(f"[Hardware] USB 포트 시도: {port}")
            if self.connect(port):
                print(f"[Hardware] 자동 USB 재연결 성공: {port}")
                return True
                
        print("[Hardware] 자동 USB 재연결 실패: 모든 포트 시도 완료")
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
                elif any(x in window_text for x in ["바람","ory"]):
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
                    
                    # 전송 성공 후 짧은 지연으로 안정성 확보
                    time.sleep(0.01)
                    
                except Exception as e:
                    print(f"[Hardware] 전송 실패: {cmd} - {e}")
                    # 연결 끊김 감지 시 자동 재연결 시도 (Panic Release 방지)
                    if self._auto_reconnect_enabled:
                        print("[Hardware] 연결 끊김 감지, 자동 재연결 시도...")
                        if self.auto_reconnect():
                            # 재연결 성공 시 명령어 재전송
                            try:
                                padding = ' ' * random.randint(1, 16)
                                payload = f"{cmd}{padding}\n".encode('ascii')
                                self.ser.write(payload)
                                print(f"[Hardware] 재전송 성공: {cmd}")
                                time.sleep(0.01)
                            except Exception as retry_e:
                                print(f"[Hardware] 재전송 실패: {cmd} - {retry_e}")
                        else:
                            # 재연결 실패 시 Panic Release 방지
                            print("[Hardware] 재연결 실패, Panic Release 방지")
            else:
                print(f"[Hardware] 미전송: {cmd} (ser={self.ser}, open={self.ser.is_open if self.ser else 'N/A'})")
                # 연결 없을 때 자동 재연결 시도
                if self._auto_reconnect_enabled and cmd.startswith("U:"):
                    # 키 업 명령은 재연결 후 전송 시도
                    if self.auto_reconnect():
                        try:
                            padding = ' ' * random.randint(1, 16)
                            payload = f"{cmd}{padding}\n".encode('ascii')
                            self.ser.write(payload)
                            print(f"[Hardware] 재연결 후 전송 성공: {cmd}")
                            time.sleep(0.01)
                        except Exception as e:
                            print(f"[Hardware] 재연결 후 전송 실패: {cmd} - {e}")
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
                    # 연결 끊김 시 자동 재연결 시도
                    if self._auto_reconnect_enabled:
                        print("[Hardware] 강제전송 실패로 자동 재연결 시도...")
                        if self.auto_reconnect():
                            # 재연결 성공 시 명령어 재전송
                            try:
                                padding = ' ' * random.randint(1, 16)
                                payload = f"{cmd}{padding}\n".encode('ascii')
                                self.ser.write(payload)
                                print(f"[Hardware] 강제전송 재전송 성공: {cmd}")
                            except Exception as retry_e:
                                print(f"[Hardware] 강제전송 재전송 실패: {cmd} - {retry_e}")
            else:
                print(f"[Hardware] 강제전송 불가: {cmd} (ser={self.ser}, open={self.ser.is_open if self.ser else 'N/A'})")
                # 하드웨어 연결 없을 때 이동 명령어는 자동 재연결 시도
                if self._auto_reconnect_enabled and (cmd.startswith('L') or cmd.startswith('R') or cmd.startswith('SU') or cmd.startswith('SD')):
                    if self.auto_reconnect():
                        try:
                            padding = ' ' * random.randint(1, 16)
                            payload = f"{cmd}{padding}\n".encode('ascii')
                            self.ser.write(payload)
                            print(f"[Hardware] 이동 명령어 재전송 성공: {cmd}")
                        except Exception as e:
                            print(f"[Hardware] 이동 명령어 재전송 실패: {cmd} - {e}")

    def press_key(self, key: str):
        """단일 키 다운 신호 전송"""
        self.send(f"D:{key}")
    
    def release_key(self, key: str):
        """단일 키 업 신호 전송"""
        self.send(f"U:{key}")

    def panic_release(self):
        """강제 정지 시 통신 버퍼를 비우고 모든 키를 해제합니다. (락 무시)"""
        # Panic Release 방지를 위한 조건 확인
        if hasattr(self, '_last_panic_release_time'):
            current_time = time.time()
            if current_time - self._last_panic_release_time < 2.0:  # 2초 내 중복 방지
                return  # 중복 Panic Release 방지
        
        self._last_panic_release_time = time.time()
        print("[Hardware] 모든 신호 강제 해제 (Panic Release)")
        
        # Panic Release 후 자동 재연결 시도
        if self._auto_reconnect_enabled:
            print("[Hardware] Panic Release 후 자동 재연결 시도...")
            self.auto_reconnect()
        if self.ser and self.ser.is_open:
            try:
                self.ser.reset_output_buffer()
                self.ser.reset_input_buffer()
                # 모든 키 업 신호 전송
                self.send("U:a")
                self.send("U:b")
                self.send("U:c")
                self.send("U:d")
                self.send("U:e")
                self.send("U:f")
                self.send("U:g")
                self.send("U:h")
                self.send("U:i")
                self.send("U:j")
                self.send("U:k")
                self.send("U:l")
                self.send("U:m")
                self.send("U:n")
                self.send("U:o")
                self.send("U:p")
                self.send("U:q")
                self.send("U:r")
                self.send("U:s")
                self.send("U:t")
                self.send("U:u")
                self.send("U:v")
                self.send("U:w")
                self.send("U:x")
                self.send("U:y")
                self.send("U:z")
                self.send("U:0")
                self.send("U:1")
                self.send("U:2")
                self.send("U:3")
                self.send("U:4")
                self.send("U:5")
                self.send("U:6")
                self.send("U:7")
                self.send("U:8")
                self.send("U:9")
                self.send("U:shift")
                self.send("U:ctrl")
                self.send("U:alt")
                self.send("U:space")
                self.send("U:enter")
                self.send("U:esc")
                self.send("U:tab")
                self.send("U:up")
                self.send("U:down")
                self.send("U:left")
                self.send("U:right")
                self.send("U:home")
                self.send("U:end")
                self.send("U:pgup")
                self.send("U:pgdn")
                self.send("U:f1")
                self.send("U:f2")
                self.send("U:f3")
                self.send("U:f4")
                self.send("U:f5")
                self.send("U:f6")
                self.send("U:f7")
                self.send("U:f8")
                self.send("U:f9")
                self.send("U:f10")
                self.send("U:f11")
                self.send("U:f12")
                print("[Hardware] 모든 키 업 신호 전송 완료")
            except Exception as e:
                print(f"[Hardware] Panic Release 중 오류: {e}")

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

    def stop_all_inputs(self, disconnect: bool = False, repeat: int = 1, delay: float = 0.05):
        """모든 입력을 중단하고 선택적으로 연결을 해제합니다."""
        for _ in range(repeat):
            self.panic_release()
            if delay > 0:
                time.sleep(delay)
        if disconnect:
            self.disconnect()

    def hold_move(self, direction: str, hold_key: str = "move_hold", variance: float = 0.15, duration: float = None):
        # HumanBehaviorSimulator로 이동 동작 간 휴식 시간 시뮬레이션
        pause = HumanBehaviorSimulator.simulate_pause('move')
        time.sleep(pause)

        if self.state is not None:
            support_blocked = time.time() < float(getattr(self.state, "support_input_blocked_until", 0.0) or 0.0)
            if support_blocked or bool(getattr(self.state, "is_combat_busy", False)):
                return
        
        # duration이 명시되면 해당 시간을 사용, 없으면 설정값 사용
        hold_time = duration if duration is not None else TIMING_CONFIG.get(hold_key, 0.15)
        
        print(f"[Move] 방향키: {direction} ({hold_time:.3f}s)")
        try:
            self.send_force(f"D:{direction}")
            humanized_sleep(hold_time, variance)
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
