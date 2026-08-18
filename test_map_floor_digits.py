"""도삭산처럼 층별 통짜 패턴 대신 숫자 글리프(<이름>_<0-9>)로 층수를 조합하는
MonitorSvc._compose_map_text_with_floor_digits 자가 점검.
py test_map_floor_digits.py 로 직접 실행.
"""
from svc_monitor import MonitorSvc

compose = MonitorSvc._compose_map_text_with_floor_digits


def hit(name, x):
    return {"name": name, "x": x, "score": 1.0}


# 기본 이름 + 숫자 글리프 3개 (순서 뒤섞어서 넣어도 x좌표로 재정렬)
hits = [hit("도삭산_1", 30), hit("도삭산", 0), hit("도삭산_2", 40), hit("도삭산_0", 20)]
assert compose(hits) == ("도삭산012", 1.0)

# 숫자 1개짜리 층 (앞자리 0 없음 - 정수 형태)
hits = [hit("도삭산", 0), hit("도삭산_5", 20)]
assert compose(hits) == ("도삭산5", 1.0)

# 기본 이름 패턴만 잡히고 숫자는 아직 안 잡힌 프레임
hits = [hit("도삭산", 0)]
assert compose(hits) == ("도삭산", 1.0)

# 기본 이름 없이 숫자 글리프만 잡힌 경우도 조합
hits = [hit("도삭산_9", 20), hit("도삭산_1", 0)]
assert compose(hits) == ("도삭산19", 1.0)

# 일반 던전(통짜 이름+층수 패턴, digit-suffix 아님)은 그대로 첫 히트 사용
hits = [hit("흉가1", 0)]
assert compose(hits) == ("흉가1", 1.0)

# 매치 없음
assert compose([]) == ("", 0.0)

# 노이즈로 4자리 이상 잡혀도 3자리까지만
hits = [hit("도삭산", 0)] + [hit(f"도삭산_{d}", (d + 1) * 10) for d in range(5)]
assert compose(hits) == ("도삭산012", 1.0)

print("OK: map text + floor-digit composition")
