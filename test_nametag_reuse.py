"""TargetConfirm이 자기 스캔에 실패해도 센티넬이 방금 본 위치를 쓴다.

실측 로그: 같은 구간에서 센티넬은 이름표를 17번 찾았는데 TargetConfirm은
9번 못 찾았다. TargetConfirm은 esc/tab을 누른 뒤 화면을 딱 한 번 보는데,
하필 그 순간 몬스터/이팩트가 이름표를 덮으면 실패한다.
"""
import time

from svc_logic import LogicSvc


class _State:
    def __init__(self, tags):
        self.party_nametag_hits = tags


def pick(tags, prefix=LogicSvc._WARRIOR_NAME_PREFIX):
    """svc_logic의 재사용 규칙과 같은 선택."""
    now = time.time()
    fresh = [t for t in tags
             if str(t.get("name", "")).startswith(prefix)
             and now - float(t.get("at", 0.0)) <= LogicSvc._NAMETAG_REUSE_SEC]
    if not fresh:
        return None
    t = max(fresh, key=lambda t: t["at"])
    return int(t["x"]), int(t["y"])


def demo():
    now = time.time()
    warrior = {"name": "점프_name_c", "x": 700, "y": 400, "at": now - 0.4}
    dosa = {"name": "졈프_name_white", "x": 300, "y": 250, "at": now - 0.1}
    stale = {"name": "점프_name_c", "x": 111, "y": 222, "at": now - 9.0}

    # 격수 것만 쓴다 - 도사 자기 이름표를 클릭하면 자기를 잡는다
    assert pick([warrior, dosa]) == (700, 400)
    # 오래된 위치는 안 쓴다 (그 사이 격수가 움직였다)
    assert pick([stale]) is None
    # 격수 것이 없으면 폴백도 없다
    assert pick([dosa]) is None
    # 최신 것을 고른다
    newer = {"name": "점프_name_c", "x": 800, "y": 500, "at": now - 0.1}
    assert pick([warrior, newer]) == (800, 500)
    print(f"ok (재사용 한도 {LogicSvc._NAMETAG_REUSE_SEC}초)")


if __name__ == "__main__":
    demo()
