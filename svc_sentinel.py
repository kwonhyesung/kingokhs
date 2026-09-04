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
        # 콘솔 확인용: 감지된 이름 집합이 바뀔 때만 1줄 출력 (매 프레임 스팸 방지)
        self._last_announced_item_names = set()
        self._last_announced_monster_names = set()
        self._last_announced_party_names = set()
        self._debounce_frames = 3  # 연속 감지 필요 프레임 수

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

            # config.json에서 grid_size/반경/내 캐릭터 이름 설정 읽기
            try:
                with open(self.config_file, 'r', encoding='utf-8') as _cfg_f:
                    _pa_cfg = json.load(_cfg_f).get("play_area", {})
                    grid_pixel_size = float(_pa_cfg.get("grid_size", 48.2))
                    item_radius_tiles = float(_pa_cfg.get("item_radius_tiles", 5))
                    monster_radius_tiles = float(_pa_cfg.get("monster_radius_tiles", 12))
                    self_pattern_name = str(_pa_cfg.get("self_pattern_name", "") or "")
            except Exception:
                grid_pixel_size = 48.2
                item_radius_tiles = 5
                monster_radius_tiles = 12
                self_pattern_name = ""

            # 내 캐릭터 위치: 새로 캡처할 필요 없이, party 카테고리에 이미 저장된
            # 내 캐릭터 이름의 방향별 패턴(예: 점프_left/right/back/top - 다른
            # 파티원이 나를 인식하려고 캡처해둔 것)을 그대로 재사용한다. 스프라이트
            # 모양은 누가 보든 똑같이 생겼으므로 self 전용 카테고리가 따로 필요
            # 없다. config에 self_pattern_name이 없는 PC(도사/술사)는 그냥 계속
            # (0,0)=위치 모름으로 남는다 - 원래도 그 역할들은 이 좌표를 안 쓴다.
            # party는 아직 반경 제한 대상이 아니라(논의 안 됨) 전체 play_area를 그대로 쓴다.
            party_hits = self.matcher.find_text_scan(play_area_rgb, category="party", ent_type="PARTY")
            my_screen_pos = (0, 0)
            if self_pattern_name:
                self_names = {self_pattern_name, *(f"{self_pattern_name}_{d}" for d in ("left", "right", "back", "top"))}
                self_hits = [h for h in party_hits if h["name"] in self_names]
                if self_hits:
                    best_self = max(self_hits, key=lambda h: h["score"])
                    my_screen_pos = (best_self["cx"], best_self["cy"])
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
            scan_results = (
                self._offset_results(
                    self.matcher.find_text_scan(monster_crop, category="monster", ent_type="MONSTER"),
                    monster_ox, monster_oy,
                )
                + self._offset_results(
                    self.matcher.find_text_scan(item_crop, category="item", ent_type="ITEM"),
                    item_ox, item_oy,
                )
                + party_hits  # 위에서 내 위치 찾을 때 이미 스캔한 결과를 재사용 (중복 스캔 방지)
            )

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

            # ── 디바운싱: 3프레임 연속 감지된 몬스터만 확정 ────
            # 열쇠는 이름이다. 예전엔 픽셀 위치(20px 버킷)를 썼는데, 몬스터가
            # 가만히 안 있고 맴돌면(실측: "몬스터가 격수 근처에서 맴돌고
            # 있다") 매 프레임 다른 버킷으로 넘어가서 같은 버킷이 3번 연속
            # 쌓일 일이 없어 확정이 영원히 안 됐다 - 원본 패턴 매칭 자체는
            # 매 프레임 성공하는데(ft.py 수동 테스트로 확인) 확정 카운터만
            # 계속 리셋되는 버그였다. 그 전엔 pos(월드 좌표)를 썼는데, 그러면
            # 캐릭터 패턴을 놓친 프레임엔 몬스터가 화면에 있어도 확정 집계에
            # 아예 못 들어가는 문제가 있었다(pos 계산 자체가 안 됨). 이름은
            # 위치나 자기 위치 인식 여부와 무관하게 안정적인 유일한 키다.
            confirmed_monsters = []
            current_keys = set()
            for m in monsters:
                key = m.get("name", "")
                current_keys.add(key)
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

            for k in [k for k, v in self._monster_history.items() if now - v[1] > 1.0]:
                del self._monster_history[k]
            for key in self._monster_history:
                if key not in current_keys:
                    self._monster_history[key][0] = 0

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
                    e["world_pos"] = (
                        round(my_world_x + (e.get("cx", 0) - my_screen_x) / grid_pixel_size),
                        round(my_world_y + (e.get("cy", 0) - my_screen_y) / grid_pixel_size),
                    )
                    detected_entities.append(e)

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
