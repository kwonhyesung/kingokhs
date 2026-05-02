"""
bis_route.py - 경로 관리 모듈
nodes.csv/waypoints.csv를 읽어서 경로를 관리하고 도착 확인 및 타겟 반환을 담당
"""

import csv
import os
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass


@dataclass
class Waypoint:
    """웨이포인트 데이터 구조"""
    map_name: str
    floor: str
    type: str  # Entry, Hunt, Exit
    x: int
    y: int
    direction: str
    reverse_action: str
    grid_x: Optional[int] = None
    grid_y: Optional[int] = None


class RouteManager:
    """경로 관리자 - nodes.csv/waypoints.csv 로드 및 경로 계산"""
    
    def __init__(self, state):
        self.state = state
        self.waypoints: List[Waypoint] = []
        self.current_map: str = "기본맵"
        self.current_floor: str = "1"
        self.current_index: int = 0
        self._load_waypoints()
    
    def _load_waypoints(self):
        """waypoints.csv에서 웨이포인트 로드"""
        csv_path = os.path.join(os.path.dirname(__file__), "waypoints.csv")
        if not os.path.exists(csv_path):
            print(f"[Route] waypoints.csv 없음: {csv_path}")
            return
        
        self.waypoints = []
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    wp = Waypoint(
                        map_name=row.get('Map', ''),
                        floor=row.get('Floor', '1'),
                        type=row.get('Type', 'Hunt'),
                        x=int(row.get('X', 0)),
                        y=int(row.get('Y', 0)),
                        direction=row.get('Direction', ''),
                        reverse_action=row.get('ReverseAction', ''),
                        grid_x=int(row.get('grid_x', 0)) if row.get('grid_x') else None,
                        grid_y=int(row.get('grid_y', 0)) if row.get('grid_y') else None
                    )
                    self.waypoints.append(wp)
            print(f"[Route] {len(self.waypoints)}개 웨이포인트 로드 완료")
        except Exception as e:
            print(f"[Route] waypoints.csv 로드 실패: {e}")
    
    def set_map(self, map_name: str, floor: str = "1"):
        """현재 맵 설정 및 해당 맵의 웨이포인트 필터링"""
        self.current_map = map_name
        self.current_floor = floor
        self.current_index = 0
        print(f"[Route] 맵 변경: {map_name} (층: {floor})")
    
    def get_current_target(self) -> Optional[Waypoint]:
        """현재 타겟 웨이포인트 반환"""
        # 현재 맵/층에 해당하는 웨이포인트 필터링
        filtered = [wp for wp in self.waypoints 
                   if wp.map_name == self.current_map and wp.floor == self.current_floor]
        
        if not filtered:
            return None
        
        if self.current_index >= len(filtered):
            self.current_index = 0  # 순환
        
        return filtered[self.current_index]
    
    def check_arrival(self, current_x: int, current_y: int, tolerance: int = 5) -> bool:
        """
        현재 좌표가 타겟 웨이포인트에 도착했는지 확인
        tolerance: 허용 오차 (픽셀)
        """
        target = self.get_current_target()
        if target is None:
            return False
        
        # Grid 좌표가 있는 경우 Grid 기반 도착 확인
        if target.grid_x is not None and target.grid_y is not None:
            # 현재 그리드 좌표 계산 (GridManager 필요)
            # 임시로 픽셀 거리 기반 확인
            pass
        
        # 픽셀 좌표 기반 도착 확인
        distance = ((current_x - target.x) ** 2 + (current_y - target.y) ** 2) ** 0.5
        arrived = distance <= tolerance
        
        if arrived:
            print(f"[Route] 도착 확인: {target.type} @ ({target.x}, {target.y})")
            self.current_index += 1  # 다음 웨이포인트로 이동
        
        return arrived
    
    def get_next_waypoint(self) -> Optional[Waypoint]:
        """다음 웨이포인트 반환 (인덱스 증가)"""
        filtered = [wp for wp in self.waypoints 
                   if wp.map_name == self.current_map and wp.floor == self.current_floor]
        
        if not filtered:
            return None
        
        self.current_index += 1
        if self.current_index >= len(filtered):
            self.current_index = 0  # 순환
        
        return filtered[self.current_index]
    
    def reset_index(self):
        """인덱스 리셋"""
        self.current_index = 0
        print(f"[Route] 인덱스 리셋")
    
    def get_waypoints_for_map(self, map_name: str, floor: str = "1") -> List[Waypoint]:
        """특정 맵의 모든 웨이포인트 반환"""
        return [wp for wp in self.waypoints 
                if wp.map_name == map_name and wp.floor == floor]
