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
        self._monster_history = {}  # {grid_key: [frame_count, last_seen_time]}
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

            # config.json에서 grid_size/반경 설정 읽기 (캘리브레이션 슬라이더 + 반경 튜닝값)
            try:
                with open(self.config_file, 'r', encoding='utf-8') as _cfg_f:
                    _pa_cfg = json.load(_cfg_f).get("play_area", {})
                    grid_pixel_size = float(_pa_cfg.get("grid_size", 48.2))
                    item_radius_tiles = float(_pa_cfg.get("item_radius_tiles", 5))
                    monster_radius_tiles = float(_pa_cfg.get("monster_radius_tiles", 12))
            except Exception:
                grid_pixel_size = 48.2
                item_radius_tiles = 5
                monster_radius_tiles = 12

            # 캐릭터 위치(마커 인식 결과) 중심으로 검색 범위를 좁혀서 속도를 올린다.
            # 마커 인식이 그 프레임에 실패해 (0,0)이면(=위치 모름) 안전하게 전체
            # play_area를 그대로 스캔한다.
            my_screen_pos = tuple(getattr(self.state, "my_screen_pos", (0, 0)) or (0, 0))
            item_crop, item_ox, item_oy = self._crop_around(play_area_rgb, my_screen_pos, item_radius_tiles * grid_pixel_size)
            monster_crop, monster_ox, monster_oy = self._crop_around(play_area_rgb, my_screen_pos, monster_radius_tiles * grid_pixel_size)

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
                + self.matcher.find_text_scan(play_area_rgb, category="party", ent_type="PARTY")
            )

            whitelist = list(getattr(self.state, "whitelist_names", []))
            new_blobs = []
            for r in scan_results:
                base_entry = {
                    "name": r["name"],
                    "type": r["type"],
                    "score": r["score"],
                    "is_whitelisted": (r["name"] in whitelist) if r["type"] == "USER" else False,
                    "grid": r["grid"],
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

            # 디바운싱 로직: 3프레임 연속 감지 시만 확정
            now = time.time()
            confirmed_monsters = []
            
            # 현재 프레임에서 감지된 몬스터의 그리드 키 생성
            current_grid_keys = set()
            for m in monsters:
                grid = m.get("grid")
                if grid and len(grid) == 2:
                    grid_key = f"{grid[0]}_{grid[1]}"
                    current_grid_keys.add(grid_key)
                    
                    # 히스토리 업데이트
                    if grid_key not in self._monster_history:
                        self._monster_history[grid_key] = [1, now]
                    else:
                        self._monster_history[grid_key][0] += 1
                        self._monster_history[grid_key][1] = now
            
            # 3프레임 이상 연속 감지된 몬스터만 확정
            for m in monsters:
                grid = m.get("grid")
                if grid and len(grid) == 2:
                    grid_key = f"{grid[0]}_{grid[1]}"
                    if self._monster_history.get(grid_key, [0, 0])[0] >= self._debounce_frames:
                        confirmed_monsters.append(m)
            
            # 오래된 히스토리 제거 (1초 이상 감지되지 않은 경우)
            expired_keys = [k for k, v in self._monster_history.items() if now - v[1] > 1.0]
            for k in expired_keys:
                del self._monster_history[k]
            
            # 현재 프레임에서 감지되지 않은 키의 카운트 리셋
            for grid_key in self._monster_history:
                if grid_key not in current_grid_keys:
                    self._monster_history[grid_key][0] = 0
            
            monsters = confirmed_monsters

            # 콘솔 확인용: 감지된 이름 집합이 바뀔 때만 1줄 출력 (테스트 중 눈으로 확인하기 위함)
            current_item_names = {i["name"] for i in items}
            if current_item_names != self._last_announced_item_names:
                if current_item_names:
                    print(f"[Sentinel] 아이템 감지: {', '.join(sorted(current_item_names))}")
                self._last_announced_item_names = current_item_names
            current_monster_names = {m["name"] for m in monsters}
            if current_monster_names != self._last_announced_monster_names:
                if current_monster_names:
                    print(f"[Sentinel] 몬스터 감지: {', '.join(sorted(current_monster_names))}")
                self._last_announced_monster_names = current_monster_names
            current_party_names = {p["name"] for p in party}
            if current_party_names != self._last_announced_party_names:
                if current_party_names:
                    print(f"[Sentinel] 파티원 감지: {', '.join(sorted(current_party_names))}")
                self._last_announced_party_names = current_party_names

            # 월드 좌표 역계산 엔진: 픽셀 거리 기반 월드 좌표 계산
            my_screen_x, my_screen_y = self.state.my_screen_pos
            my_world_x, my_world_y = self.state.my_world_pos
            
            # 화살표 감지 실패 시(초기값 0,0) GPS 계산 스킵
            if my_screen_x == 0 and my_screen_y == 0:
                detected_entities = []
            else:
                # grid_pixel_size는 루프 상단에서 이미 읽었음 (재사용)
                detected_entities = []
                for e in tracked_all:
                    cx = e.get("cx", 0)
                    cy = e.get("cy", 0)
                    # 픽셀 차이 계산
                    pixel_diff_x = cx - my_screen_x
                    pixel_diff_y = cy - my_screen_y
                    # 월드 좌표 역계산
                    target_world_x = my_world_x + (pixel_diff_x / grid_pixel_size)
                    target_world_y = my_world_y + (pixel_diff_y / grid_pixel_size)
                    # 엔티티에 월드 좌표 추가 (정수로 반올림)
                    e["world_pos"] = (round(target_world_x), round(target_world_y))
                    detected_entities.append(e)

                    # 디버그 로그: 몬스터/아이템 발견 시 거리 계산 (비활성화 - 비트맵 이진화 완성 전까지)
                    # if e.get("type") in ["MONSTER", "ITEM"]:
                    #     dist_grid = ((target_world_x - my_world_x)**2 + (target_world_y - my_world_y)**2)**0.5
                    #     print(f"[GPS] 내 위치: ({my_world_x},{my_world_y}) | {e['type']} 발견: ({round(target_world_x)},{round(target_world_y)}) | 거리: {dist_grid:.1f} Grid")

            # GridIndicator에 아이템 위치 전달 (GameState에서 grid_overlay_enabled 확인)
            if self.grid_indicator and getattr(self.state, "grid_overlay_enabled", False):
                item_grids = [i.get("grid") for i in items if i.get("grid")]
                self.grid_indicator.update_item_positions(item_grids)
            
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
                    self.state.detected_monster_grid = monsters[0]["grid"]
                    self.state.detected_monster_name = monsters[0]["name"]
                    self.state.detected_monster_world = monsters[0].get("world_pos")
                # 자동사냥용: 화면에 확정된 몬스터 전체의 world 좌표 목록
                # (detected_monster_world는 그중 첫 번째만 담는 요약값).
                self.state.detected_monsters_world = [
                    {"name": m["name"], "world": m.get("world_pos")}
                    for m in monsters if m.get("world_pos")
                ]
                if items:
                    self.state.detected_item_grid = items[0]["grid"]
                    self.state.detected_item_name = items[0]["name"]
                    self.state.detected_item_world = items[0].get("world_pos")

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
