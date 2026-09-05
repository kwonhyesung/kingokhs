"""좌표가 이동보다 오래된 화면에서 읽힌 값이면 '안 움직였다'를 판정하지 않는다.

실측 회귀: (10,11)에서 right를 보내고 0.55초 만에 stuck 판정 -> (11,11)을
90초 차단했는데, 바로 다음 로그의 위치가 (12,11)이었다(그 칸을 지나갔다).
"""
from svc_route import RouteSvc


class _Stub:
    """_coord_fresh_since가 보는 것만 흉내낸다."""
    def __init__(self, last_read, age_ms):
        self.state = type("S", (), {"numeric_last_update_time": last_read,
                                    "capture_age_ms": age_ms})()


def fresh(last_read, age_ms, t0):
    return RouteSvc._coord_fresh_since(_Stub(last_read, age_ms), t0)


def demo():
    move_at = 100.0
    # 이동 0.5초 뒤에 읽었지만 그 화면은 이동 0.1초 '전'에 찍혔다 -> 아직 옛날 값
    assert fresh(100.5, 600, move_at) is False
    # 이동 0.5초 뒤에 읽었고 화면도 이동 뒤(0.3초 지연)에 찍혔다 -> 판정 가능
    assert fresh(100.5, 200, move_at) is True
    # 읽은 적이 아예 없으면 예전처럼 동작(막지 않는다)
    assert fresh(0.0, 0, move_at) is True
    # 이동 시각이 없으면(타이머 꺼짐) 막지 않는다
    assert fresh(100.5, 600, 0.0) is True
    print("ok")


if __name__ == "__main__":
    demo()
