# 핵심 모듈(bis_core.py) 기능 설명

> 2026-08-20 현재 소스코드 기준으로 재작성. 이 문서는 원래 `svc_kernel.py`를 설명하던
> 문서였는데, 실제 시스템의 핵심 로직은 이미 오래전에 **`bis_core.py`**로 옮겨졌고
> `svc_kernel.py`는 그 이후로도 안 지워진 채 남아있던 예전 사본이다. 지금 살아서
> 쓰이는 건 `bis_core.py`이므로, 이 문서는 `bis_core.py` 기준으로 다시 썼다.
> `svc_kernel.py` 자체의 정리 상태는 맨 아래 "구조적 문제 & 정리 현황" 참고.

## 개요
`bis_core.py`는 시스템의 핵심 정의 및 유틸리티를 제공한다. 타이밍 제어, 공유 상태
(`GameState`), 하드웨어 입력, 게임 창 탐지 등을 포함한다. `svc_logic.py`(전투/힐 FSM),
`svc_route.py`(이동/추적), `svc_monitor.py`(화면 인식), `svc_worker.py`(전체 기동)를
비롯해 거의 모든 실행 파일이 여기서 `GameState`, `hw`, `TIMING_CONFIG` 등을 가져다 쓴다.

---

## 1. TIMING_CONFIG

### 설명
시스템 딜레이 제어 테이블(`config.json`의 `timing` 섹션으로 덮어쓰기 가능). 각
작업의 기본 지연 시간을 정의한다.

### 주요 파라미터
- `key_gap` / `key_down_hold`: 키 입력 간격 / 유지 시간
- `tab_wait` / `up_wait` / `enter_wait`: 대상선택박스 조작 대기 시간
- `spell_cast_gap` / `spell_confirm`: 스킬 캐스트 간격 / 확인 대기
- `heal_gap`: 힐 캐스트 간격 (35ms)
- `mp_recover_wait` / `mp_recover_interval`: MP 회복 입력 대기 / 재입력 간격(2초)
- `bomu_spell_gap` / `bomu_interval`: 보무 버프 간격(185초 주기)
- `move_hold`: 이동 유지 시간(100ms - 타일 씹힘 마지노선)
- `stuck_time`: 이동 중 좌표 미변경 시 stuck 판정 기준(4초)
- `entry_hold` / `exit_hold`: 포탈/입구·출구 진입 시 키 유지 시간(1초)
- `combat_timeout`, `action_loop`, `nav_loop`, `idle_sleep` 등 루프 주기 값들

### humanized_sleep / tc
`humanized_sleep(base_time)`은 기본 시간에 가우시안 노이즈(최소 20%~최대 300%)를
얹어 자연스러운 지연을 만든다. `tc(key)`는 `TIMING_CONFIG`에서 키로 기본값을 찾아
같은 방식으로 노이즈를 적용한 값을 반환한다.

---

## 2. 시스템 상태 및 환경 명칭 변경 (Anti-Cheat 대응)

### TargetType / AppStatus
탐지 회피를 위해 내부 상태/타겟 종류를 암호 같은 코드값(`"ST_2"`, `"TYPE_S"` 등)으로
별칭 처리한 열거형. 로직상 의미는 이름 그대로(EMERGENCY_HEAL, COMBAT, TARGET_SELECT 등).

### Skill (dataclass)
스킬 정의: `name`, `target_type`, `hotkey`, `spell_char`, `enable_red_tab`(캐스트
전에 격수 red_tab이 필요한지), `category`, `cooldown`, `last_cast_time`. `is_ready()`로
쿨다운 경과 여부 확인.

### 게임 창 탐지 - find_game_window / find_game_window_any / is_game_window_active
- `find_game_window(substring)`: 열려있는 모든 창을 훑어서 제목에 `substring`이
  포함된(보이는) 첫 창의 핸들을 반환.
- `find_game_window_any(substrings=("ory","바람"))`: 위 함수를 substrings 순서대로
  시도해 첫 매치를 반환. **게임 창 제목엔 "ory" 또는 "바람" 둘 중 하나만 있어도
  같은 창으로 인식한다** (2026-08-20 확인). 예전엔 파일마다 "ory" 한 단어만 찾는
  곳과 "바람"/"AION"까지 찾는 곳이 따로 있어서, 제목에 "ory"가 없는 창은 일부
  코드에서만 못 찾는 상태였다 - 지금은 창을 찾는 곳을 전부 이 함수 하나로 모았다.
- `is_game_window_active(state)`: 지금 이 게임 창이 활성(포그라운드)인지 확인.
  키 입력을 실제로 보내도 되는지 판단하는 안전장치로, 실전 로직 곳곳(힐/재타겟/
  버프 시전 전)에서 호출된다.

---

## 3. GameState

### 설명
스레드 안전성을 갖춘 공유 상태 클래스(`dataclass`). `LogicSvc`/`RouteSvc`/`MonitorSvc`가
전부 같은 `GameState` 인스턴스 하나를 공유하며, 서로 다른 스레드끼리는 **오직 이
객체를 통해서만** 정보를 주고받는다 (직접 서로를 참조하지 않음).

### 주요 속성 (기능별로 묶음)

**좌표/자원**: `x`, `y`, `hp`, `mp`, `exp`, `money`, `good_hp`, `good_mp`

**맵 인식**: `map_name`, `map_floor`, `current_map`, `map_info_fingerprint`(화면
지문으로 맵 변경 감지), `map_info_change_seq`, `map_sync_status`

**타겟팅 / red_tab**: `red_tab_enabled`(대상 이름표가 빨간지 - 실시간 시각 감지값),
`red_tab_promotion_active`, `support_targeting_active`, `target_kind`/`target_name`/
`target_info_text`, `ntab_active`(대상선택박스 확인 모드), `user_name`/`user_kind`/
`user_info_text`(User_info ROI로 확인한 신원), `is_user_detected`

**포탈-팔로우** (도사가 격수의 문 통과를 따라가는 상태): `portal_follow_active`,
`portal_follow_coord`, `portal_follow_approach`, `portal_follow_dir`,
`portal_follow_retarget_requested`

**네트워크 (도사↔격수 실시간 공유)**: `network_role`("도사"/"격수" 등),
`network_server_ip`, `network_telemetry_port`, `network_sequence`,
`network_session_id`. `get_remote_data_by_role(role)` / `get_fresh_remote_data_by_role(role)`로
다른 PC(예: 격수)가 보낸 최신 상태를 읽어온다. `build_share_payload()`로 내 상태를
내보낸다.

**술사(sulsa) 관련**: `sulsa_attack_enabled`, `sulsa_debuff_enabled`,
`sulsa_item_slots`(아이템 슬롯 키 목록) 등 - 도사/격수 외에 술사 역할도 이 같은
`GameState`를 공유해서 쓴다.

**내비게이션/추적**: `nav_follow_enabled`, `nav_route_enabled`, `nav_avoid_enabled`,
`follow_target_pos`, `stuck_start_time`

**기타**: `spells`(스킬 목록), `whitelist_names`, `target_monster_names`,
`hwnd`(게임 창 핸들), `running`, `auto_hunt`, `service_active`

### 주요 메서드
- `update(**kwargs)` / `update_from_dict(data)`: 스레드 안전 상태 갱신
- `get_all()`: 전체 상태 스냅샷 반환
- `get_remote_data_by_role(role)` / `get_fresh_remote_data_by_role(role)`: 다른
  PC(격수 등)가 보낸 최신 텔레메트리 조회
- `build_share_payload()`: 다른 PC에 보낼 내 상태 payload 생성
- `apply_role_good_hp(role)`: 역할별 힐 기준 HP 적용

### 해상도 관련 속성
- `game_resolution`: 게임 창 해상도 (854, 480)
- `gui_resolution`: GUI 창 해상도 (640, 480)

---

## 4. BisHardware (하드웨어 입력)

### 설명
무작위 패킷 패딩을 사용해 하드웨어 직렬 통신을 처리하는 클래스. 탐지를 피하기
위해 소프트웨어 대체 입력 방법은 기본적으로 비활성화한다. 전역 인스턴스 `hw`로
어디서든 가져다 쓴다(`from bis_core import hw`).

### 주요 기능
- **패킷 크기 가변화**: 명령어 뒤에 무작위 길이의 패딩을 추가해 전송 데이터
  크기를 불규칙하게 만듦
- **ESP32-S3 하드웨어 입력** 사용, 자동 재연결(`auto_reconnect`)
- **소프트웨어 입력 비활성화**: 탐지 방지

### 주요 메서드
- `connect(port)` / `auto_reconnect()`: 시리얼 포트 연결
- `press_key(key)` / `release_key(key)`: 키 누름/뗌
- `send(cmd)` / `send_force(cmd)`: 명령어 전송(가변 패딩 적용)
- `hold_move(direction, hold_key)`: 방향키를 일정 시간 누르고 있기(실제 걷기)
- `stop_all_inputs()`: 눌려있는 모든 키 강제 해제
- `humanized_press(key)` / `fast_press(key)` / `force_press(key)`: 사람처럼/빠르게/즉시 입력

---

## 5. 스텔스 작동 중요성

### Anti-Cheat 대응
- 상태 및 환경 명칭 변경 (TargetType, AppStatus)
- 하드웨어 입력 사용 (BisHardware)
- 패킷 크기 가변화 (탐지 방지)
- 인간화된 딜레이 (humanized_sleep)

### 보안
- 스레드 안전성 (GameState 내부 락)
- 메모리 패턴 랜덤화 (패킷 가변화)
- 표준 API 사용 (OBS 가상 카메라 경유 화면 캡처)

---

## 6. 구조적 문제 & 정리 현황

이 프로젝트는 기능을 빠르게 늘려오면서 같은 역할을 하는 코드가 여러 파일에
따로 생기는 일이 잦았다. AI에게 코드를 다시 확인할 때마다 스스로 기억하기 어려운
부분이라, 발견/정리된 것과 아직 남은 것을 여기 기록해둔다.

### 정리 완료 (2026-08-20)
- `MonitorSvc._calc_grid`가 같은 클래스 안에 두 번 정의돼 있어서(파이썬 특성상
  나중 정의가 앞을 조용히 덮어씀) 앞쪽 버전이 죽은 코드였음 → 삭제.
- `_is_game_window_active`가 `svc_logic.py`/`svc_worker.py`에 각각 따로(내용도
  다르게) 있었음 → `bis_core.is_game_window_active()` 하나로 통합.
- 게임 창 제목 매칭이 파일마다 "ory"만 찾는 곳과 "바람"/"AION"까지 찾는 곳으로
  갈려 있었음 → `find_game_window_any()`로 통합, "ory" 또는 "바람" 둘 중 하나면
  인식하도록 통일.
- `_is_follow_reposition_needed`가 `svc_logic.py`(간이 버전)/`svc_route.py`(포탈
  인지 포함, 근데 아무도 안 씀) 두 벌 → `LogicSvc` 버전에 `state.portal_follow_active`
  체크를 이식해 포탈 인지 기능만 옮기고, `RouteSvc` 쪽 죽은 버전은 삭제.
  (두 클래스가 별도 스레드라 완전 통합은 불가능 - 공유 `state`를 거쳐서만 정보 교환.)
- 도사가 격수 대상을 잘못(몬스터/자기자신/다른 유저) 선택할 수 있던 문제 → TAB→Enter로
  후보를 확정하고 User_info ROI에서 신원(`findtext_patterns.json`의 `점프_user`/
  `졈프_user`)을 확인한 뒤에만 최종 lock하는 `_confirm_and_lock_warrior_target()` 추가
  (현재 HP 힐 경로에만 연결됨 - MP/비상 재획득 경로는 아직 미연결, 아래 참고).
- `_should_hold_follow_at_gap`가 `svc_logic.py`/`svc_route.py`에 각각 있었는데 둘
  다 아무도 안 씀 → 둘 다 삭제.
- follow가 막혔을 때 red_tab 상태와 무관하게 무조건 "대상선택박스 충돌"로 간주해
  ESC+재타겟을 시도하던 fallback 경로(`svc_route.py`) → 근거 없는 추측이라 삭제,
  이미 있는 일반 회피 이동(`_escape_stuck`)으로 대체.
- `gui_app.py`/`svc_worker.py`가 `config_utils`/`gui_overlay`/`background_threads`/
  (svc_worker.py는 `gui_app`까지) 를 `import *`(통째)로 가져와서, `bis_core`에서
  명시적으로 가져온 이름과 겹치면 나중 import가 조용히 덮어쓸 수 있던 구조 →
  pyflakes로 실제 참조되는 이름만 추려서 두 파일 다 명시적 import로 교체.
  실제로 안 쓰이던 `gui_overlay`의 통째 import는 두 파일 다 완전히 제거됨
  (아무 이름도 안 쓰고 있었음). 부수적으로 `gui_app.py`가 `traceback`을 한 번도
  import 안 한 채 `traceback.print_exc()`를 쓰고 있던 잠재 버그(핫키 콜백 에러
  처리 중 발생 시 NameError로 죽었을 것)도 같이 발견해서 고침.
  **아직 실제 게임 환경에서 검증 전 - 다른 PC에서 게임 실행 후 확인 필요.**

### 아직 남은 문제

**`find_game_window`가 4개 파일에 각각 정의돼 있음**: `bis_core.py`(살아있음),
`svc_kernel.py`, `config_utils.py`, `svc_loader.py`(파일 안 내용은 `digit_bootstrap.py`라는
별개 스크립트라 파일명과 내용도 안 맞음). `WIN_KEY = "ory"` 상수도 5개 파일에
각각 박혀있음. 로직 자체는 4곳 다 동일하지만, 유지보수할 때마다 4곳을 다 고쳐야
하는 부담이 남아있음.

**`svc_kernel.py`가 `bis_core.py`의 거의 완전한 병렬 포크**: `find_game_window`,
`split_map_name_floor`, `humanized_sleep`/`tc`, `GameState`, 하드웨어 클래스,
`GridManager`가 두 파일에 각각 구현돼 있음. `svc_monitor.py`/`svc_sentinel.py`/
`svc_pattern_matcher.py`가 아직 `svc_kernel.py`에서 `GameState`/`find_game_window`
타입 힌트·함수를 가져다 쓰고 있어서 완전히 지우려면 이 세 파일의 import를
`bis_core.py`로 옮기는 작업이 먼저 필요함.

**MP 회복/비상 재획득 red_tab 경로가 아직 신원 미확인**: `_ensure_warrior_red_tab_via_ntab`,
`_reacquire_warrior_red_tab_after_emergency`는 여전히 "뭔가 빨갛게 선택되면 통과"
방식이라, HP 힐 경로와 달리 몬스터/자기자신/다른 유저를 잘못 잡을 수 있음.
