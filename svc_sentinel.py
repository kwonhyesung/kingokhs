"""svc_sentinel.py - 몬스터/유저 감시 스레드 (SentinelThread)."""

import time
import threading
import os
import json
import cv2
from svc_kernel import GameState
from svc_monitor_common import _monitor_log
from svc_pattern_matcher import PatternMatcher
from svc_entity_tracker import EntityTracker

_HUNT_MAPS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "hunt_maps.json")


class SentinelThread(threading.Thread):
    """
    몬스터, 아이템, 유저 감지를 전담하여 15 FPS 주기로 수행하는 분리된 스레드.
    Native Resolution (1:1) 매칭을 위해 리사이즈 없이 중심점 기준 크롭 방식을 사용함.
    """
    def __init__(self, state: GameState, matcher: PatternMatcher, config_file: str, grid_indicator=None):
        super().__init__(name="SentinelThread", daemon=True)
        self.state = state
        self.matcher = matcher
        self.config_file = config_file
        self.entity_tracker = EntityTracker(self.state)
        # grid_indicator는 호환성을 위해 유지하지만, GameState에서 값을 참조
        self.grid_indicator = grid_indicator
        self._blob_fail_count = 0
        self._last_run_time = 0
        self._last_overlay_time = 0
        # 디바운싱 로직: 3프레임 연속 감지 시 확정
        self._monster_history = {}  # {"x_y": [frame_count, last_seen_time]}
        self._char_pattern_lost_since = 0.0
        self._last_self_pos = ((0, 0), 0.0, (0, 0))  # (my_screen_pos, 시각, 그때의 월드좌표)
        self._last_slow_cycle_log_time = 0.0
        self._cfg_cache = (None, 0.0)
        self._maps_cfg_cache = (None, 0.0)
        self._last_no_monster_scan_log = 0.0
        # 콘솔 확인용: 감지된 이름 집합이 바뀔 때만 1줄 출력 (매 프레임 스팸 방지)
        self._last_announced_item_names = set()
        self._last_announced_monster_names = set()
        self._last_announced_party_names = set()
        # 확정에 필요한 감지 횟수. '프레임'이 아니라 '사이클' 수다 - 설계는
        # 30fps(3프레임=0.1초)였는데 실측 사이클이 3.5~4초라 3회면 첫 공격까지
        # 10초가 넘는다. 2회로 내리면 3.5초(한 사이클)로 줄면서, 이 값이 막으려던
        # '한 번 반짝하는 패턴 오탐'은 그대로 걸러진다.
        # 진짜 해결은 사이클을 30fps에 되돌리는 것이다 - [SentinelPerf]의 단계별
        # 숫자(party/monster/item/rest)가 어디서 시간이 나가는지 말해준다.
        self._debounce_frames = 2
        self._logged_missing_self_patterns = False
        self._last_frame_dump_time = 0.0

    def _cfg(self) -> dict:
        """config.json (5초 캐시). 예전엔 매 사이클 디스크에서 json.load 했다 -
        30fps를 목표로 도는 루프에서 초당 30번 파일을 여는 짓이라, 설정 반영이
        최대 5초 늦어지는 대가로 없앤다."""
        cfg, loaded_at = self._cfg_cache
        now = time.time()
        if cfg is not None and now - loaded_at < 5.0:
            return cfg
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        self._cfg_cache = (cfg, now)
        return cfg

    def _monsters_table(self, cfg: dict) -> dict:
        """맵별 몬스터 목록. hunt_maps.json이 있으면 그걸 쓰고, 없으면 예전처럼
        config.json의 hunt.monsters_by_map을 본다.

        별도 파일을 두는 이유: config.json은 PC마다 role/self_pattern_name/
        server_ip가 달라서 저장소 것과 항상 다르다. 거기에 공용 설정을 같이
        넣으면 저장소가 config.json을 고칠 때마다 다른 PC의 git pull이 충돌
        하거나 그 PC 고유 설정을 덮어쓴다. 공용 설정은 PC마다 다를 이유가
        없는 이 파일에 두면 pull이 항상 조용히 끝난다."""
        table = (self._maps_cfg() or {}).get("monsters_by_map")
        if isinstance(table, dict) and table:
            return table
        return (cfg.get("hunt", {}) or {}).get("monsters_by_map", {}) or {}

    def _maps_cfg(self) -> dict:
        """hunt_maps.json (5초 캐시). 없으면 빈 dict."""
        cfg, loaded_at = self._maps_cfg_cache
        now = time.time()
        if cfg is not None and now - loaded_at < 5.0:
            return cfg
        try:
            with open(_HUNT_MAPS_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                cfg = {}
        except Exception:
            cfg = {}
        self._maps_cfg_cache = (cfg, now)
        return cfg

    def _monsters_for_map(self, cfg: dict, map_name: str) -> set | None:
        """이 맵에서 찾을 몬스터 이름들.

        키는 맵 이름의 앞부분으로 맞춘다 - '흉가' 하나로 흉가1/흉가2가 다
        걸리고, 층이 30개인 도삭산도 한 줄로 끝난다.

        반환값 세 가지를 구분한다:
          None      설정 자체가 없다 -> 예전처럼 전부 스캔.
                    이걸 빈 set과 같이 취급하면, 설정을 아직 안 받은 PC에서
                    모든 맵의 몬스터가 통째로 안 잡힌다(조용한 전면 중단).
          빈 set    설정은 있는데 이 맵이 목록에 없다 -> 스캔하지 않는다.
          이름들    그 몬스터만 스캔한다.
        """
        table = self._monsters_table(cfg)
        if not table:
            return None
        for prefix, names in table.items():
            if map_name.startswith(str(prefix)):
                return {str(n) for n in (names or [])}
        return set()

    SELF_POS_TTL = 3.0   # 캐릭터 패턴을 놓쳐도 기준점을 유지하는 시간(초)

    def _self_pattern_name(self, pa_cfg: dict) -> str:
        """자기 캐릭터를 찾을 패턴 이름. pc_roles.json의 표가 config.json을 이긴다.

        config.json은 저장소에서 추적되는데 PC마다 값이 달라서, git pull 한 번에
        저장소 값(빈 문자열)으로 덮인다 - 실측으로 그렇게 격수가 자기 위치를
        아예 못 찾아 사냥이 통째로 멈췄다. role은 pc_roles.json이 PC 이름으로
        이미 못을 박고 있으므로, 거기에 한 줄 더 얹는 게 제일 싸다."""
        role = str(getattr(self.state, "network_role", "") or "")
        return str(self._role_table().get(role) or pa_cfg.get("self_pattern_name", "") or "")

    def _role_table(self) -> dict:
        """pc_roles.json의 self_pattern_by_role (60초 캐시)."""
        now = time.time()
        cached, at = getattr(self, "_role_table_cache", (None, 0.0))
        if cached is not None and now - at < 60.0:
            return cached
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pc_roles.json")
            with open(path, encoding="utf-8") as f:
                cached = json.load(f).get("self_pattern_by_role") or {}
        except Exception:
            cached = {}
        self._role_table_cache = (cached, now)
        return cached

    def _anchor_with_fallback(self, found: tuple, now: float, world: tuple) -> tuple:
        """이번 프레임에 찾은 캐릭터 위치. 못 찾았으면 마지막 값을 쓴다.

        단, '내가 안 움직였을 때'만이다. 카메라는 캐릭터를 항상 따라다니지
        않아서(맵 가장자리 등) 화면상 픽셀 위치는 고정값이 아니다 - 대신
        캐릭터가 움직이지 않았으면 화면도 그대로다. 그래서 좌표 OCR(world)이
        바뀌면 캐시를 버린다. 이팩트에 가려지는 상황은 둘러싸여 제자리에서
        때리는 중이라, 정작 필요한 그 순간엔 world가 안 변한다.
        TTL은 좌표 OCR 자체가 멎었을 때를 위한 2차 안전장치다."""
        if found != (0, 0):
            self._last_self_pos = (found, now, world)
            return found
        last_pos, last_at, last_world = self._last_self_pos
        if last_world != world:
            return (0, 0)
        if now - last_at <= self.SELF_POS_TTL:
            return last_pos
        return (0, 0)

    def _log_bad_world_pos(self, entity, world, me):
        """맵 밖 좌표로 계산된 감지를 버렸다고 남긴다(5초 제한).
        조용히 버리면 '왜 저 몬스터를 안 쫓지'가 다시 미궁이 된다."""
        now = time.time()
        if now - getattr(self, "_last_bad_world_log", 0.0) < 5.0:
            return
        self._last_bad_world_log = now
        print(f"[Sentinel] 맵 밖 좌표 무시: {entity.get('name', '?')}@{world} me={me} (패턴 오탐)")

    def _log_no_monster_scan(self, map_name: str):
        """몬스터를 안 찾기로 한 이유를 남긴다(1초 제한). 이게 없으면 '몬스터를
        못 찾는' 진짜 버그와 '안 찾기로 한 맵'이 로그에서 똑같이 보인다."""
        now = time.time()
        if now - self._last_no_monster_scan_log < 1.0:
            return
        self._last_no_monster_scan_log = now
        if not map_name or map_name == "기본맵":
            # 맵 이름을 아직 한 번도 못 읽은 상태. '몬스터가 없는 맵'이 아니라
            # '모르는 상태'라서 조용히 넘기면 안 된다.
            print("[Hunt] 맵 미확정(기본맵) - 몬스터 스캔 대기")
        else:
            print(f"[Hunt] '{map_name}'는 hunt.monsters_by_map에 없음 - 몬스터 스캔 안 함")

    @staticmethod
    def _crop_around(image, center, radius_px):
        """캐릭터 위치(center, play_area 기준 픽셀) 중심으로 image를 잘라서
        (crop, offset_x, offset_y)를 반환한다. offset은 crop 안의 좌표를 다시
        원본 image 기준으로 되돌릴 때 더해줄 값. center가 (0,0)이면(=마커
        인식 실패, 위치 모름) 안전하게 원본 전체를 그대로 반환한다."""
        cx, cy = center
        if cx == 0 and cy == 0 or radius_px <= 0:
            return image, 0, 0
        h, w = image.shape[:2]
        r = int(radius_px)
        x0 = max(0, int(cx) - r)
        y0 = max(0, int(cy) - r)
        x1 = min(w, int(cx) + r)
        y1 = min(h, int(cy) + r)
        if x1 <= x0 or y1 <= y0:
            return image, 0, 0
        return image[y0:y1, x0:x1], x0, y0

    @staticmethod
    def _offset_results(results, offset_x, offset_y):
        """crop 기준으로 나온 find_text_scan 결과를 원본 play_area 기준으로 되돌린다."""
        if offset_x == 0 and offset_y == 0:
            return results
        for r in results:
            r["x"] += offset_x
            r["y"] += offset_y
            r["cx"] += offset_x
            r["cy"] += offset_y
        return results

    def run(self):
        _monitor_log("[Sentinel] Entity scan thread start (FindText XOR mode)")
        while self.state.running:
            # Keep numeric OCR/UI responsive: idle unless explicitly enabled.
            if not getattr(self.state, "sentinel_enabled", False):
                time.sleep(0.05)
                continue
            now = time.time()
            # 30 FPS 제한 (약 33ms 주기)
            if now - self._last_run_time < 0.033:
                time.sleep(0.01)
                continue
            self._last_run_time = now

            play_area_rgb = getattr(self.state, "last_play_area_rgb", None)
            if play_area_rgb is None or play_area_rgb.size == 0:
                continue

            # 10초마다 지금 보고 있는 화면을 그대로 한 장 남긴다. "불귀신을
            # 왜 못 잡느냐" 같은 질문은 진짜 프레임 없이는 추측밖에 안 되는데,
            # 게임은 GPU로 그려서 창 캡처가 검게 나오고 캡처 장치는 봇이
            # 잡고 있어서 밖에서 얻을 방법이 없었다.
            if now - self._last_frame_dump_time >= 10.0:
                self._last_frame_dump_time = now
                try:
                    import cv2 as _cv2, os as _os
                    _d = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                       "logs", "runtime")
                    _os.makedirs(_d, exist_ok=True)
                    _cv2.imwrite(_os.path.join(_d, "play_area.png"),
                                 _cv2.cvtColor(play_area_rgb, _cv2.COLOR_RGB2BGR))
                except Exception:
                    pass

            # config.json에서 grid_size/내 캐릭터 이름 설정 읽기 (5초 캐시)
            cfg = self._cfg()
            _pa_cfg = cfg.get("play_area", {}) or {}
            grid_pixel_size = float(_pa_cfg.get("grid_size", 48.2) or 48.2)
            self_pattern_name = self._self_pattern_name(_pa_cfg)
            map_name = str(getattr(self.state, "current_map", "") or "")

            # 내 캐릭터 위치: 새로 캡처할 필요 없이, party 카테고리에 이미 저장된
            # 내 캐릭터 이름의 방향별 패턴(예: 점프_left/right/back/top - 다른
            # 파티원이 나를 인식하려고 캡처해둔 것)을 그대로 재사용한다. 스프라이트
            # 모양은 누가 보든 똑같이 생겼으므로 self 전용 카테고리가 따로 필요
            # 없다. config에 self_pattern_name이 없는 PC(도사/술사)는 그냥 계속
            # (0,0)=위치 모름으로 남는다 - 원래도 그 역할들은 이 좌표를 안 쓴다.
            # party는 아직 반경 제한 대상이 아니라(논의 안 됨) 전체 play_area를 그대로 쓴다.
            _t = time.perf_counter()
            party_hits = self.matcher.find_text_scan(play_area_rgb, category="party", ent_type="PARTY")
            self._stage_ms = {"party": (time.perf_counter() - _t) * 1000.0}
            my_screen_pos = (0, 0)
            if self_pattern_name:
                self_names = {self_pattern_name, *(f"{self_pattern_name}_{d}" for d in ("left", "right", "back", "top"))}
                self_hits = [h for h in party_hits if h["name"] in self_names]
                if self_hits:
                    best_self = max(self_hits, key=lambda h: h["score"])
                    my_screen_pos = (best_self["cx"], best_self["cy"])
                    # 내 캐릭터가 어느 쪽을 보고 있는지. find_text_scan은 모든
                    # 결과의 score를 1.0으로 고정해서 돌려주므로 best_self
                    # 하나만 쓰면 '점수가 가장 높은 방향'이 아니라 '먼저 나온
                    # 것'이 뽑힌다 - 그걸 방향이라고 찍으면 안 바뀌는 것처럼
                    # 보일 수 있다. 잡힌 방향 패턴을 전부 남긴다.
                    self.state.my_facing = ",".join(sorted(
                        h["name"].rsplit("_", 1)[-1] for h in self_hits))
                else:
                    # 방향 패턴은 스프라이트가 이팩트/몬스터에 가려지면 통째로
                    # 안 잡힌다. 실측 격수 로그: my_screen_pos=(0,0)이 계속돼
                    # "[HuntIdle] 내 캐릭터 패턴을 못 찾음"으로 공격도 줍기도
                    # 멈췄는데, 같은 프레임에서 이름표(점프_name)는 잡히고 있었다.
                    # 이름표는 캐릭터 머리 위 click_offset_y만큼 위에 있으므로
                    # 그만큼 내리면 몸이다 - 파티원을 클릭할 때와 같은 보정이다.
                    tag_prefix = f"{self_pattern_name}_name"
                    tag_hits = [h for h in party_hits
                                if str(h.get("name", "")).startswith(tag_prefix)]
                    if tag_hits:
                        best_tag = max(tag_hits, key=lambda h: h["score"])
                        my_screen_pos = (best_tag["cx"],
                                         best_tag["cy"] + int(_pa_cfg.get("click_offset_y", 26) or 26))
                if my_screen_pos == (0, 0) and not self._logged_missing_self_patterns:
                    # self 패턴을 설정해 놓고 한 번도 못 찾으면 조용히 넘어가지
                    # 않는다 - 그러면 격수의 사냥/줍기가 통째로 멈추는데 로그엔
                    # "캐릭터 패턴 미검출" 한 줄만 남아 원인이 '안 보임'인지
                    # '패턴이 저장돼 있지 않음'인지 구분이 안 됐다.
                    self._logged_missing_self_patterns = True
                    have = sorted(n for n in self.matcher.findtext_patterns.get("party", {})
                                  if n.startswith(self_pattern_name))
                    print(f"[Sentinel] self '{self_pattern_name}'의 방향 패턴을 화면에서 "
                          f"못 찾음 (저장된 것: {have or '없음'}) - 이 좌표가 없으면 몬스터/"
                          f"아이템 월드 좌표가 전부 '?'가 되어 격수의 줍기/사냥 판단이 멈춘다.")
            # 몬스터에 둘러싸이면 이팩트/스프라이트가 겹쳐 캐릭터 패턴이 통째로
            # 안 잡힌다 (실측: 사냥 중 '점프_* 못 찾음'이 반복되며 사냥이 멈춤).
            # 그런데 이 값은 매 프레임 새로 찾아야 하는 값이 아니다 - 카메라가
            # 캐릭터를 따라다녀서 화면상 픽셀 위치는 거의 안 변하는 '기준점'이다.
            # 그래서 못 찾은 프레임은 마지막으로 찾은 값을 재사용한다.
            # ponytail: TTL로 끊는 게 한계다 - 맵이 스크롤되면 기준점이 실제로
            # 어긋나는데 그걸 알 방법이 지금은 없다. 어긋난 채로 계속 사냥하면
            # 엉뚱한 칸을 때리므로, 오래된 값은 버리고 사냥을 쉬는 쪽을 택했다.
            # 더 버텨야 하면 hp_bar(피격/회복 중엔 이팩트 위에 뜬다)를 party
            # 패턴으로 하나 더 잡아서 2차 기준점으로 쓰면 된다.
            my_screen_pos = self._anchor_with_fallback(
                my_screen_pos, now, tuple(getattr(self.state, "my_world_pos", (0, 0)) or (0, 0)))
            self.state.my_screen_pos = my_screen_pos

            # ponytail: 반경 크롭을 뺐다 - party(전체 play_area 스캔)는 항상
            # 잡히는데 monster/item(반경 크롭)만 못 잡는 사례가 실전에서
            # 확인됐다(ft.py 전체화면 검색으로는 패턴이 매칭되는데, 반경
            # 안에서만 찾는 실제 스캔은 못 찾음 - 캐릭터 위치 계산이 살짝
            # 어긋나면 크롭 경계 밖으로 몬스터가 빠지는 게 원인으로 보임).
            # party가 이미 매 프레임 전체 play_area를 크롭 없이 스캔하고도
            # 성능 문제가 없었으니, 몬스터/아이템도 같은 방식으로 맞춘다.
            # 반경 튜닝값(item_radius_tiles/monster_radius_tiles)은 이제
            # 안 쓰지만, 나중에 실제로 스캔 속도가 문제가 되면 그때 다시
            # 크롭을 넣고 반경을 넉넉히 재보정할 것.
            item_crop, item_ox, item_oy = play_area_rgb, 0, 0
            monster_crop, monster_ox, monster_oy = play_area_rgb, 0, 0

            # 몬스터/아이템: ft.py로 저장한 FindText 패턴(findtext_patterns.json)으로 스캔.
            # find_text_scan은 카테고리 안의 패턴을 전부 '|'로 결합해 한 번에
            # 찾으므로(native find_text 자체가 이미지 전체를 벡터 연산 한 번으로
            # 훑음) findtext_scan()이 쓰던 priority_grids 최적화가 필요 없다.
            # party는 "USER" 타입을 쓰지 않는다 - USER는 hostile_users/whitelist
            # 경보 로직으로 들어가서, 파티원(격수/도사/술사)을 적대 유저로
            # 오인하게 됨. 그래서 별도 타입 "PARTY"로 분리해서 스캔한다.
            # party는 아직 반경 제한 대상이 아니라(논의 안 됨) 전체 play_area를 그대로 쓴다.
            # 순차 실행: 스레드풀로 병렬화했다가 되돌림 - cv2 작업이 계속 3개
            # 스레드에서 거의 쉬지 않고 돌면서 RouteSvc 등 다른 스레드의 CPU
            # 타임슬라이스를 뺏어, follow 이동 입력이 밀려서 "Stuck" 감지가
            # 실측 세션당 1회 -> 6~12회로 급증했다 (스캔속도 1.55배 향상보다
            # 이동 안정성이 훨씬 중요해서 되돌림).
            # 이 맵에 안 나오는 몬스터는 찾지 않는다 - 패턴 15개를 전부 훑으면
            # 흉가에서 안 나오는 갈산신 4방향까지 매 프레임 상관연산을 돌린다.
            # None = 설정 없음(전부 스캔), 빈 set = 이 맵은 스캔 안 함
            _t = time.perf_counter()
            monster_names = self._monsters_for_map(cfg, map_name)
            if monster_names is None or monster_names:
                monster_hits = self._offset_results(
                    self.matcher.find_text_scan(monster_crop, category="monster",
                                                ent_type="MONSTER",
                                                only_names=monster_names),
                    monster_ox, monster_oy,
                )
            else:
                monster_hits = []
                self._log_no_monster_scan(map_name)

            self._stage_ms["monster"] = (time.perf_counter() - _t) * 1000.0
            _t = time.perf_counter()
            scan_results = (
                monster_hits
                + self._offset_results(
                    self.matcher.find_text_scan(item_crop, category="item", ent_type="ITEM"),
                    item_ox, item_oy,
                )
                + party_hits  # 위에서 내 위치 찾을 때 이미 스캔한 결과를 재사용 (중복 스캔 방지)
            )

            self._stage_ms["item"] = (time.perf_counter() - _t) * 1000.0
            _t = time.perf_counter()
            whitelist = list(getattr(self.state, "whitelist_names", []))
            new_blobs = []
            for r in scan_results:
                base_entry = {
                    "name": r["name"],
                    "type": r["type"],
                    "score": r["score"],
                    "is_whitelisted": (r["name"] in whitelist) if r["type"] == "USER" else False,
                    "cx": r["cx"],
                    "cy": r["cy"],
                    "w": r["w"],
                    "h": r["h"],
                    "last_seen": now,
                }
                new_blobs.append(base_entry)

            # 트래커 업데이트 및 상태 반영
            tracked_all = self.entity_tracker.update(new_blobs)
            
            # 상태 플래그 계산
            monsters = [e for e in tracked_all if e.get("type") == "MONSTER"]
            items    = [e for e in tracked_all if e.get("type") == "ITEM"]
            users    = [e for e in tracked_all if e.get("type") == "USER"]
            party    = [e for e in tracked_all if e.get("type") == "PARTY"]

            # 중복 감지 방지: 같은 위치(픽셀 좌표)에 있는 엔티티 필터링
            def deduplicate_entities(entities):
                if not entities:
                    return []
                seen_positions = set()
                deduplicated = []
                for e in entities:
                    pos = (e.get("cx", 0), e.get("cy", 0))
                    # 같은 위치에 있는 엔티티는 하나만 유지 (거리 기준: 10픽셀 이내)
                    is_duplicate = False
                    for seen_pos in seen_positions:
                        if ((pos[0] - seen_pos[0])**2 + (pos[1] - seen_pos[1])**2)**0.5 < 10:
                            is_duplicate = True
                            break
                    if not is_duplicate:
                        seen_positions.add(pos)
                        deduplicated.append(e)
                return deduplicated

            monsters = deduplicate_entities(monsters)
            items = deduplicate_entities(items)
            users = deduplicate_entities(users)
            party = deduplicate_entities(party)

            # ── 디바운싱: 최근 몇 초 안에 3번 감지되면 확정 ────
            # 열쇠는 이름이다(위치는 몬스터가 맴돌면 매 프레임 다른 버킷으로
            # 넘어가서 못 쓴다 - 예전 버그).
            #
            # "3프레임 연속"이 아니라 "최근 몇 초 안에 3번"이다 - 한 프레임
            # 놓치면 카운트를 0으로 리셋하는 코드가 있었는데, 몬스터 패턴은
            # 캐릭터 패턴(점프_*)보다 프레임당 인식률이 낮아서(실측: 15~20초
            # 내내 화면에 있었는데도 확정이 한 번도 안 됨) 완벽하게 연속
            # 3번을 채우는 일이 거의 없었다. 굳이 연속일 필요가 없다.
            # 만료 시간을 8초로 넉넉히 잡는다 - 실측 스캔 주기가 30fps 설계와
            # 크게 어긋나는 사례(20초 유지 = 로그 8줄, 사이클당 1초 이상)가
            # 나와서, 그 실제 속도에서도 3번을 모을 여유를 준다. 진짜로
            # 사라진 몬스터는 hunt 쪽의 별도 유예(target_miss_grace_sec)가
            # 처리하므로, 여기서 너무 빡빡하게 지울 필요가 없다.
            confirmed_monsters = []
            for m in monsters:
                key = m.get("name", "")
                hist = self._monster_history.get(key)
                if hist is None:
                    self._monster_history[key] = [1, now]
                else:
                    hist[0] += 1
                    hist[1] = now
            for m in monsters:
                key = m.get("name", "")
                if self._monster_history.get(key, [0, 0])[0] >= self._debounce_frames:
                    confirmed_monsters.append(m)

            for k in [k for k, v in self._monster_history.items() if now - v[1] > 8.0]:
                del self._monster_history[k]

            monsters = confirmed_monsters

            # ── pos(x,y) 역계산 ────────────────────────────────
            # 몬스터/아이템 pos = 내 pos + (패턴 픽셀 - 캐릭터 패턴 픽셀) / grid_size
            # 캐릭터 패턴을 못 찾은 프레임(my_screen_pos == (0,0))은 뺄셈의 한쪽이
            # 없어서 계산 자체가 불가능하다 - 사냥 판단(자동사냥의 접근/공격)만
            # 쉬고, 감지/확정/화면 표시는 위에서 이미 끝났으므로 영향받지 않는다.
            my_screen_x, my_screen_y = self.state.my_screen_pos
            my_world_x, my_world_y = self.state.my_world_pos
            if my_screen_x == 0 and my_screen_y == 0:
                detected_entities = []
                self._char_pattern_lost_since = self._char_pattern_lost_since or now
                if now - self._char_pattern_lost_since >= 2.0:
                    print("[Sentinel] 캐릭터 패턴 2초 이상 미검출 - 사냥 판단 중지 중")
                    self._char_pattern_lost_since = now
            else:
                self._char_pattern_lost_since = 0.0
                detected_entities = []
                for e in tracked_all:
                    wx = round(my_world_x + (e.get("cx", 0) - my_screen_x) / grid_pixel_size)
                    wy = round(my_world_y + (e.get("cy", 0) - my_screen_y) / grid_pixel_size)
                    # 음수 칸은 맵에 존재하지 않는다. 실측에서 me=(6,14)인데
                    # 처녀귀신@(-2,22) 같은 좌표가 섞여 나왔다 - 패턴 오탐이라
                    # 여기서 버리지 않으면 사냥이 화면 밖 유령을 쫓아간다.
                    if wx < 0 or wy < 0:
                        self._log_bad_world_pos(e, (wx, wy), (my_world_x, my_world_y))
                        continue
                    e["world_pos"] = (wx, wy)
                    detected_entities.append(e)

            # 게임창 오버레이 점(0.5초). 로그 숫자만으로는 "기준점이 어긋난
            # 것"과 "몬스터 좌표 변환이 틀린 것"을 구분할 수 없다 - 눈으로 보면
            # 한 화면에서 갈린다.
            #   마젠타 = 봇이 클릭할 지점 (이름표 + click_offset_y). 이 점이
            #            캐릭터 위에 오도록 config 숫자만 맞추면 된다.
            #   빨강   = 감지된 몬스터.
            # 초록을 안 쓰는 이유: 게임이 이미 파티원 머리 위에 초록 표시를
            # 쓴다. 같은 색이면 봇이 찍은 점과 게임이 그린 표시가 섞인다.
            pa_sx, pa_sy = int(_pa_cfg.get("sx", 0) or 0), int(_pa_cfg.get("sy", 0) or 0)
            # 수명은 '지금'부터 잰다. now는 사이클 맨 위에서 찍은 값인데 한
            # 바퀴가 실측 4초까지 걸려서, now+2.0으로 만들면 마커가 저장되는
            # 순간 이미 2초 전에 만료된 상태가 된다 - GUI가 [MarkerDiag]
            # markers=0 drawn=0으로 계속 찍던 원인이 이것이다.
            mark_now = time.time()
            marks = [{"x": m.get("cx", 0) + pa_sx, "y": m.get("cy", 0) + pa_sy,
                      "color": "red", "size": 12, "expiry": mark_now + 5.0} for m in monsters]
            off_y = int(_pa_cfg.get("click_offset_y", 26) or 26)
            # 격수 이름표는 점프_name* 계열이다(pc_roles.json의
            # self_pattern_by_role["격수"]="점프"와 같은 이름). 도사 자신의
            # 이름표(졈프_name*)는 여기서 제외한다 - 섞이면 클릭 캐시가
            # 도사 자신을 가리키게 된다.
            warrior_hits = [
                h for h in party_hits
                if str(h.get("name", "")).startswith("점프_name")
            ]
            # 쌓지 않고 매 사이클 통째로 교체한다. 예전엔 extend라 지난
            # 사이클 점이 남았고, 수명(2초)이 사이클(실측 1~2초)과 비슷해서
            # 점이 깜빡였다. 교체하면 감지되는 동안 계속 떠 있고 사라질 때
            # 바로 사라진다. 수명은 센티넬이 멈췄을 때 점이 얼어붙지 않게
            # 하는 안전장치로만 남긴다.
            # 이름표 클릭 지점을 시각과 함께 남긴다. TargetConfirm은 esc/tab을
            # 누른 뒤 딱 한 번만 화면을 보는데, 하필 그 순간 이름표가 가려지면
            # 실패한다(실측 로그: 센티넬은 17번 찾는 동안 TargetConfirm은 9번
            # 못 찾음). 센티넬은 매 사이클 보고 있으니, 방금 본 위치를 물려준다.
            tags = [{"name": str(h.get("name", "")),
                     "x": h.get("cx", 0) + pa_sx,
                     "y": h.get("cy", 0) + pa_sy + off_y,
                     "at": mark_now}
                    for h in warrior_hits]
            with self.state._lock:
                self.state.visual_markers = marks
                self.state.party_nametag_hits = tags

            # 콘솔 확인용: 감지된 이름 집합이 바뀔 때만 1줄 출력 (테스트 중 눈으로 확인하기 위함).
            # 좌표(x,y)도 같이 찍는다 - 이름만으론 "감지는 되는데 위치가 맞는지"를
            # 눈으로 확인할 방법이 없다. self 위치를 몰라 world_pos가 없으면 "?"로 표시.
            def _fmt(e):
                w = e.get("world_pos")
                return f"{e['name']}@{tuple(w) if w else '?'}"

            current_item_names = {i["name"] for i in items}
            if current_item_names != self._last_announced_item_names:
                if current_item_names:
                    print(f"[Sentinel] 아이템 감지: {', '.join(_fmt(i) for i in items)}")
                self._last_announced_item_names = current_item_names
            current_monster_names = {m["name"] for m in monsters}
            if current_monster_names != self._last_announced_monster_names:
                if current_monster_names:
                    print(f"[Sentinel] 몬스터 감지: {', '.join(_fmt(m) for m in monsters)} "
                          f"| me=({my_world_x},{my_world_y})")
                self._last_announced_monster_names = current_monster_names

            current_party_names = {p["name"] for p in party}
            if current_party_names != self._last_announced_party_names:
                if current_party_names:
                    print(f"[Sentinel] 파티원 감지: {', '.join(sorted(current_party_names))}")
                self._last_announced_party_names = current_party_names

            hostile_users = [u for u in users if not u.get("is_whitelisted")]
            
            with getattr(self.state, "_lock", threading.Lock()):
                self.state.entities["monsters"] = monsters
                self.state.entities["items"] = items
                self.state.entities["users"] = users
                self.state.entities["party"] = party
                self.state.entities["objects"] = tracked_all
                self.state.detected_entities = detected_entities
                
                # 가공된 상태 업데이트 (MonitorSvc 역할을 가져옴)
                self.state.monster_on_screen = len(monsters) > 0
                if hostile_users:
                    self.state.is_user_detected = True
                    self.state.user_name = hostile_users[0]["name"]
                else:
                    self.state.is_user_detected = False
                
                # 최근 발견된 몬스터/아이템 정보 (Status 출력용)
                if monsters:
                    self.state.detected_monster_name = monsters[0]["name"]
                    self.state.detected_monster_world = monsters[0].get("world_pos")
                # 자동사냥용: 화면에 확정된 몬스터 전체의 world 좌표 목록
                # (detected_monster_world는 그중 첫 번째만 담는 요약값).
                self.state.detected_monsters_world = [
                    {"name": m["name"], "world": m.get("world_pos")}
                    for m in monsters if m.get("world_pos")
                ]
                if items:
                    self.state.detected_item_name = items[0]["name"]
                    self.state.detected_item_world = items[0].get("world_pos")
                # 자동사냥 줍기용: 화면에 있는 아이템 전체의 pos 목록
                self.state.detected_items_world = [
                    {"name": i["name"], "world": i.get("world_pos")}
                    for i in items if i.get("world_pos")
                ]

            # 실패 로그 모니터링 (5회 연속 실패 시 1회 요약 출력) - 비활성화
            # last_err = getattr(self.state, "last_vision_error", "")
            # if last_err:
            #     self._blob_fail_count += 1
            #     if self._blob_fail_count >= 5:
            #         print(f"   [Debug] 인식 실패 요약: {last_err}")
            #         self._blob_fail_count = 0
            #         self.state.last_vision_error = "" # 초기화하여 도배 방지
            # else:
            #     self._blob_fail_count = 0

            # 디버그 오버레이: 1초에 한 번 엔티티 박스 그리기
            if now - self._last_overlay_time >= 1.0:
                play_area_overlay = self.state.ocr_preview_img
                if play_area_overlay is not None:
                    # 몬스터: 빨간 박스
                    for m in monsters:
                        cx, cy = m.get("cx", 0), m.get("cy", 0)
                        w, h = m.get("w", 20), m.get("h", 20)
                        x1, y1 = cx - w // 2, cy - h // 2
                        x2, y2 = cx + w // 2, cy + h // 2
                        cv2.rectangle(play_area_overlay, (x1, y1), (x2, y2), (0, 0, 255), 2)
                        cv2.putText(play_area_overlay, m.get("name", ""), (x1, y1 - 5),
                                   cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1)
                    # 아이템: 파란 박스
                    for i in items:
                        cx, cy = i.get("cx", 0), i.get("cy", 0)
                        w, h = i.get("w", 20), i.get("h", 20)
                        x1, y1 = cx - w // 2, cy - h // 2
                        x2, y2 = cx + w // 2, cy + h // 2
                        cv2.rectangle(play_area_overlay, (x1, y1), (x2, y2), (255, 0, 0), 2)
                        cv2.putText(play_area_overlay, i.get("name", ""), (x1, y1 - 5),
                                   cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 0, 0), 1)
                self._last_overlay_time = now

            # 실측 사이클 소요 시간 진단: "20초 F2 유지 = 로그 8줄"처럼 스캔
            # 속도가 30fps 설계와 크게 어긋나는 사례가 나와서, 정확히 한
            # 사이클(스캔+매칭 전체)이 몇 ms 걸리는지 직접 찍는다(1초 제한).
            cycle_ms = (time.time() - now) * 1000.0
            if cycle_ms > 50.0 and time.time() - self._last_slow_cycle_log_time >= 1.0:
                self._last_slow_cycle_log_time = time.time()
                # 총합만으로는 어디가 느린지 알 수 없다. 실측 스캔은 합쳐서
                # 350ms인데 사이클은 3500ms까지 나온다 - 나머지 3초가 스캔
                # 밖에 있다는 뜻이라, 단계별로 쪼개서 같이 찍는다.
                st = getattr(self, "_stage_ms", {})
                st["rest"] = (time.perf_counter() - _t) * 1000.0
                parts = " ".join(f"{k}={v:.0f}" for k, v in st.items())
                print(f"[SentinelPerf] 사이클 {cycle_ms:.0f}ms (30fps 목표=33ms) | {parts}")
