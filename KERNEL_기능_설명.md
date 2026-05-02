# KERNEL 기능 설명

## 개요
svc_kernel.py는 시스템의 핵심 정의 및 유틸리티를 제공합니다. 타이밍 제어, 상태 관리, 하드웨어 인터페이스 등을 포함합니다.

---

## 1. TIMING_CONFIG

### 설명
시스템 딜레이 제어 테이블입니다. 각 작업의 기본 지연 시간을 정의합니다.

### 주요 파라미터
- `key_down_hold`: 키 누름 유지 시간 (0.012초)
- `key_gap`: 키 입력 간격 (0.020초)
- `tab_wait`: 탭 대기 시간 (0.040초)
- `up_wait`: 위쪽 키 대기 시간 (0.040초)
- `enter_wait`: 엔터 대기 시간 (0.035초)
- `spell_cast_gap`: 스킬 캐스트 간격 (0.030초)
- `spell_confirm`: 스킬 확인 대기 시간 (0.200초)
- `ocr_wait`: OCR 대기 시간 (0.100초)
- `ocr_fast`: 빠른 OCR 대기 시간 (0.050초)
- `heal_gap`: 치유 간격 (0.005초)
- `move_hold`: 이동 유지 시간 (0.100초)
- `idle_sleep`: 대기 시간 (0.100초)

### humanized_sleep
기본 시간에 가우시안 노이즈를 추가하여 자연스러운 지연을 생성합니다.

### tc
TIMING_CONFIG에서 기본 시간을 가져와 humanized_sleep을 적용합니다.

---

## 2. 시스템 상태 및 환경 명칭 변경 (Anti-Cheat 대응)

### TargetType
타겟 타입을 나타내는 열거형입니다.
- `INSTANT`: 즉시 타겟팅
- `TARGET_SELECT`: 타겟 선택
- `TARGET_INPUT`: 타겟 입력

### AppStatus
애플리케이션 상태를 나타내는 열거형입니다.
- `SETUP`: 초기 설정
- `IDLE`: 대기
- `EMERGENCY_HEAL`: 긴급 치유
- `MANA_REGEN`: 마나 회복
- `BUFFING`: 버프
- `DEBUFFING`: 디버프 제거
- `COMBAT`: 전투
- `SCANNING`: 스캔
- `STUCK_ESCAPE`: 막힘 탈출

### Skill
스킬 정보를 저장하는 데이터 클래스입니다.
- `name`: 스킬 이름
- `target_type`: 타겟 타입
- `hotkey`: 핫키
- `spell_char`: 스킬 문자
- `enable_red_tab`: 빨간 탭 사용 여부
- `category`: 카테고리
- `cooldown`: 쿨다운
- `last_cast_time`: 마지막 캐스트 시간

### find_game_window
게임 창을 찾는 함수입니다. 윈도우 제목에 특정 문자열이 포함된 창을 검색합니다.

---

## 3. GameState

### 설명
스레드 안전성을 갖춘 공유 상태를 관리하는 클래스입니다.

### 주요 속성
- `hwnd`: 게임 창 핸들
- `hp`, `mp`, `exp`, `money`: 게임 상태
- `x`, `y`: 좌표
- `target_locked`: 타겟 잠금 여부
- `running`: 실행 상태
- `ocr_enabled`: OCR 활성화 여부
- `auto_hunt`: 자동 사냥 여부
- `spells`: 스킬 목록
- `hp_trig_active`, `mp_trig_active`: HP/MP 트리거 활성화
- `recovery_hp_spell`, `recovery_mp_spell`: 회복 스킬
- `win_x`, `win_y`: 윈도우 좌표
- `nav_follow_enabled`: 네비게이션 추적 활성화
- `nav_route_enabled`: 네비게이션 경로 활성화
- `nav_avoid_enabled`: 네비게이션 회피 활성화
- `waypoints_db`: 웨이포인트 데이터베이스
- `current_map`: 현재 맵
- `follow_target_pos`: 추적 타겟 위치
- `last_pos`: 마지막 위치
- `stuck_start_time`: 막힘 시작 시간
- `last_key_context`: 마지막 키 컨텍스트
- `target_type`: 타겟 타입
- `target_name`: 타겟 이름
- `user_name`: 유저 이름
- `is_chat_active`: 채트 활성화 여부
- `shield_active`: 실드 활성화 여부
- `shield_expire_time`: 실드 만료 시간
- `target_monster_names`: 타겟 몬스터 이름 목록
- `whitelist_names`: 화이트리스트 이름 목록
- `last_debuff_x`, `last_debuff_y`: 마지막 디버프 좌표
- `last_bomu_time`: 마지막 봄 시간
- `auto_debuff_enabled`: 자동 디버프 활성화
- `recovery_debuff_spell`: 회복 디버프 스킬
- `user_alarm_enabled`: 유저 알람 활성화
- `user_stop_enabled`: 유저 정지 활성화
- `user_next_enabled`: 유저 넥스트 활성화
- `combat_start_time`: 전투 시작 시간
- `maps_db`: 맵 데이터베이스
- `char_grid`: 캐릭터 그리드
- `detected_item_grid`: 감지된 아이템 그리드
- `detected_item_name`: 감지된 아이템 이름

### 해상도 관련 속성
- `game_resolution`: 게임 창 해상도 (854, 480)
- `game_scale`: 배율 (1.0 또는 2.0)
- `gui_resolution`: GUI 창 해상도 (640, 480)

### 메서드
- `update`: 상태 업데이트 (스레드 안전)
- `get_all`: 모든 상태 반환 (스레드 안전)
- `set_game_scale`: 게임 창 배율 설정

---

## 4. DevInterface (HardwareController)

### 설명
무작위 패킷 패딩을 사용하여 하드웨어 직렬 통신을 처리하는 클래스입니다. 탐지를 피하기 위해 소프트웨어 대체 입력 방법을 비활성화합니다.

### 주요 기능
- **패킷 크기 가변화**: 명령어 뒤에 무작위 길이의 패딩(공백)을 추가하여 전송 데이터 크기를 불규칙하게 만듦
- **하드웨어 입력**: ESP32-S3 하드웨어 입력 사용
- **소프트웨어 입력 비활성화**: 탐지 방지

### 메서드
- `set_state`: 상태 설정
- `connect`: 시리얼 포트 연결
- `send`: 명령어 전송 (패킷 가변화 적용)
- `press_key`: 키 누름
- `release_key`: 키 해제
- `move`: 이동
- `stop`: 정지

---

## 5. 스텔스 작동 중요성

### Anti-Cheat 대응
- 상태 및 환경 명칭 변경 (TargetType, AppStatus)
- 하드웨어 입력 사용 (DevInterface)
- 패킷 크기 가변화 (탐지 방지)
- 인간화된 딜레이 (humanized_sleep)

### 보안
- 스레드 안전성 (GameState)
- 메모리 패턴 랜덤화 (패킷 가변화)
- 표준 API 사용 (OBS 가상 카메라)
