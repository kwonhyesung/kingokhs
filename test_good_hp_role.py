"""역할별 good_hp 파싱(bis_core.GameState._resolve_good_hp_from_combat) 자가 점검.
py test_good_hp_role.py 로 직접 실행.
"""
from bis_core import GameState

resolve = GameState._resolve_good_hp_from_combat

# dict 형식: 역할별로 다른 값을 골라온다
combat = {"good_hp": {"격수": 1100000, "도사1": 150000}}
assert resolve(combat, "격수", 100000) == 1100000
assert resolve(combat, "도사1", 100000) == 150000
# 목록에 없는 역할은 fallback(기존 값) 유지
assert resolve(combat, "술사", 999999) == 999999

# 구버전 호환: 단일 숫자면 역할 무관하게 그 값(최소 100000 클램프)
combat_flat = {"good_hp": 200000}
assert resolve(combat_flat, "격수", 100000) == 200000
assert resolve({"good_hp": 50}, "격수", 100000) == 100000  # 100000 미만은 클램프

# combat 섹션 자체가 없으면 구버전 경로(단일 숫자)와 동일하게 최소 100000 클램프
assert resolve({}, "격수", 777) == 100000

print("OK: role-based good_hp resolution")
