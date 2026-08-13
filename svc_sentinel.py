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
    def __init__(self, state: GameState, matcher: PatternMatcher, grid_indicator=None):
        super().__init__(name="SentinelThread", daemon=True)
        self.state = state
        self.matcher = matcher
        self.entity_tracker = EntityTracker(self.state)
        # grid_indicator는 호환성을 위해 유지하지만, GameState에서 값을 참조
        self.grid_indicator = grid_indicator
        self._blob_fail_count = 0
        self._last_run_time = 0
        self._last_overlay_time = 0
        # 디바운싱 로직: 3프레임 연속 감지 시 확정
        self._monster_history = {}  # {grid_key: [frame_count, last_seen_time]}
        self._debounce_frames = 3  # 연속 감지 필요 프레임 수

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

            # 이전 프레임에서 감지된 몬스터/아이템의 Grid 좌표를 우선 검색 영역으로 설정
            priority_grids = []
            for m in self.state.entities.get("monsters", []):
                g = m.get("grid")
                if g and g[0] >= 0:
                    priority_grids.append(g)
            for i in self.state.entities.get("items", []):
                g = i.get("grid")
                if g and g[0] >= 0:
                    priority_grids.append(g)

            # FindText 방식 슬라이딩 XOR 매칭 (색상 무시, 모양만 비교)
            scan_results = self.matcher.findtext_scan(
                play_area_rgb,
                folders=["monsters", "items"],
                threshold=0.90,
                stride=2,
                priority_grids=priority_grids if priority_grids else None
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

            # 월드 좌표 역계산 엔진: 픽셀 거리 기반 월드 좌표 계산
            my_screen_x, my_screen_y = self.state.my_screen_pos
            my_world_x, my_world_y = self.state.my_world_pos
            
            # 화살표 감지 실패 시(초기값 0,0) GPS 계산 스킵
            if my_screen_x == 0 and my_screen_y == 0:
                detected_entities = []
            else:
                # config.json에서 grid_size 읽어오기
                try:
                    import json as _cfg_json
                    _cfg_file = os.path.join(os.path.dirname(self.config_file), "config.json")
                    with open(_cfg_file, 'r', encoding='utf-8') as _cfg_f:
                        grid_pixel_size = float(_cfg_json.load(_cfg_f).get("play_area", {}).get("grid_size", 48.2))
                except Exception:
                    grid_pixel_size = 48.2

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
                if items:
                    self.state.detected_item_grid = items[0]["grid"]
                    self.state.detected_item_name = items[0]["name"]

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
