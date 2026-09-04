"""svc_entity_tracker.py - Object_ID 기반 실시간 엔티티 추적 (EntityTracker)."""

import time
import os
from svc_kernel import GameState


class EntityTracker:
    """
    매 프레임 Blob 목록을 입력받아 Object_ID를 부여하고
    이전 프레임과 유클리드 거리를 비교해 같은 개체를 추적한다.

    Rules:
        - 거리 < MATCH_DIST(80px) 이면 이전 프레임 같은 개체로 판단
        - EXPIRE_SEC(1.5초) 동안 갱신없으면 제거
        - 새 개체에 next_id 할당
    """
    MATCH_DIST = 80    # 동일 개체로 판단할 최대 취소 거리 (px)
    EXPIRE_SEC = 1.5   # 이 시간동안 발견되지 않으면 제거

    def __init__(self, state):
        self.state = state
        self.next_id: int = 1
        self.tracked: dict = {}  # {id: {cx, cy, type, name, last_seen, prev_cx, prev_cy, direction_changes, ...}}
        self.coord_print_count = {}  # {id: 출력 횟수}
        self.entity_printed = {}  # {id: New Entity 메시지 출력 여부}
        # Grid ↔ POS 변환 관계 (J10 = Grid(9,9) = POS(9,33))

    def update(self, blobs: list) -> list:
        """
        blobs: [{cx, cy, type, name, score, is_whitelisted, ...}, ...]
        반환: 갱신된 tracked 리스트 ({'id': int, ...} 포함)
        """
        now = time.time()
        matched_old_ids: set = set()
        result: list = []

        # 캐릭터 정보 가져오기 (캐릭터 기준 계산용)
        # 좌표계는 pos(x,y) 하나뿐이다. 예전엔 여기서 캐릭터 grid를
        # 기준으로 blob의 grid/pos를 다시 만들었는데, char_grid는 어디서도
        # 값이 채워지지 않는 죽은 값이었다. 매칭은 픽셀 거리로만 한다.
        for blob in blobs:
            bx, by = blob.get("cx", 0), blob.get("cy", 0)
            blob_name = blob.get("name", "")
            blob_type = blob.get("type", "")
            best_id, best_dist = None, float("inf")

            # 같은 타입 중 픽셀 거리가 가장 가까운 개체와 매칭
            for oid, odata in self.tracked.items():
                if oid in matched_old_ids:
                    continue
                if odata.get("type", "") != blob_type:
                    continue  # 다른 타입은 매치 안 함
                # 그 외 거리 기준 매칭
                dist = ((bx - odata["cx"]) ** 2 + (by - odata["cy"]) ** 2) ** 0.5
                if dist < best_dist:
                    best_dist, best_id = dist, oid

            if best_id is not None and best_dist < self.MATCH_DIST:
                # 기존 개체 갱신
                prev_cx = self.tracked[best_id].get("cx", bx)
                prev_cy = self.tracked[best_id].get("cy", by)
                self.tracked[best_id].update(blob)
                self.tracked[best_id]["cx"] = bx
                self.tracked[best_id]["cy"] = by
                self.tracked[best_id]["last_seen"] = now
                self.tracked[best_id]["prev_cx"] = prev_cx
                self.tracked[best_id]["prev_cy"] = prev_cy

                # 이동 벡터 계산 (방향 변화 추적)
                dx = bx - prev_cx
                dy = by - prev_cy
                self.tracked[best_id]["dx"] = dx
                self.tracked[best_id]["dy"] = dy

                # 방향 변화 횟수 계산 (0.5초 이내)
                if "direction_changes" not in self.tracked[best_id]:
                    self.tracked[best_id]["direction_changes"] = 0
                    self.tracked[best_id]["last_direction"] = (dx, dy)
                    self.tracked[best_id]["last_dir_time"] = now
                else:
                    last_dx, last_dy = self.tracked[best_id]["last_direction"]
                    # 방향이 크게 바뀌었는지 확인 (내적 계산)
                    if abs(dx * last_dx + dy * last_dy) < 0:  # 방향이 반대 또는 크게 다름
                        if now - self.tracked[best_id]["last_dir_time"] < 0.5:
                            self.tracked[best_id]["direction_changes"] += 1
                        self.tracked[best_id]["last_direction"] = (dx, dy)
                        self.tracked[best_id]["last_dir_time"] = now

                # 몬스터: grid 좌표 변경 시만 좌표 출력 (GPS로 대체되어 주석 처리)
                # t = self.tracked[best_id].get("type")
                # name = self.tracked[best_id].get("name", "")
                # if t == "MONSTER" and blob_grid:
                #     last_grid = self.last_grid.get(best_id)
                #     if last_grid != blob_grid:
                #         self.last_grid[best_id] = blob_grid

                matched_old_ids.add(best_id)
                entry = dict(self.tracked[best_id])
                entry["id"] = best_id
                result.append(entry)
            else:
                # 신규 개체
                oid = self.next_id
                self.next_id += 1
                self.tracked[oid] = {**blob, "cx": bx, "cy": by, "last_seen": now}
                self.coord_print_count[oid] = 0
                self.entity_printed[oid] = False  # 메시지 출력 여부 초기화
                entry = dict(self.tracked[oid])
                entry["id"] = oid
                result.append(entry)

                # 신규 개체 발견 시 즉각 알림 (사용자 지시사항) - GPS로 대체되어 주석 처리
                # t = entry.get("type")
                # name = entry.get("name", "")
                # gx_d = int(grid[0]) if grid else -1
                # gy_d = int(grid[1]) if grid else -1
                # gname = (f"{chr(ord('A') + gx_d)}{gy_d + 1}" if grid and gx_d >= 0 else "?")
                # score = entry.get("score", 0.0)

                # # 좌표 계산 (내 캐릭터 기준) - GPS로 대체되어 주석 처리
                # me = self.state.entities.get("me", {})
                # me_grid = me.get("grid", (-1, -1))
                # me_screen = me.get("screen", (0, 0))
                # me_pos_x, me_pos_y = me_grid

                # if t == "MONSTER" or t == "ITEM":
                #     # 아이템: 1회만 좌표 출력, 몬스터: 첫 감지 시 출력
                #     if t == "ITEM" and self.coord_print_count[oid] < 1:
                #         self.coord_print_count[oid] += 1
                #         entity_pos_x, entity_pos_y = grid
                #         dx = entity_pos_x - me_pos_x
                #         dy = entity_pos_y - me_pos_y
                #         coord_msg = f"[Coord] 내 캐릭터: ({me_pos_x}, {me_pos_y}) | {t} '{name}': ({entity_pos_x}, {entity_pos_y}) | 상대: ({dx}, {dy})"
                #         print(coord_msg)
                #     elif t == "MONSTER":
                #         self.coord_print_count[oid] += 1
                #         entity_pos_x, entity_pos_y = grid
                #         dx = entity_pos_x - me_pos_x
                #         dy = entity_pos_y - me_pos_y
                #         coord_msg = f"[Coord] 내 캐릭터: ({me_pos_x}, {me_pos_y}) | {t} '{name}': ({entity_pos_x}, {entity_pos_y}) | 상대: ({dx}, {dy})"
                #         print(coord_msg)

                # # New Entity 메시지 1회만 출력 (GPS로 대체되어 주석 처리)
                # if not self.entity_printed[oid]:
                #     self.entity_printed[oid] = True
                #     msg = f"[New Entity] {t} '{name}' 감지 (Grid: {gname}, Score: {score:.2f})"
                #     print(msg)
                #     if hasattr(self.state, "last_event_log"):
                #         self.state.last_event_log = msg
                # elif t == "USER" and not entry.get("is_whitelisted", True):
                #     msg = f"[Warning] 미확인 유저 '{name}' 감지! (안전 지대 이동 대기중)"
                #     print(msg)
                #     if hasattr(self.state, "last_event_log"):
                #         self.state.last_event_log = msg

        # 만료 제거
        expired = [oid for oid, d in self.tracked.items()
                   if (now - d.get("last_seen", 0)) > self.EXPIRE_SEC]
        for oid in expired:
            del self.tracked[oid]
            if oid in self.coord_print_count:
                del self.coord_print_count[oid]
            if oid in self.entity_printed:
                del self.entity_printed[oid]

        return result
