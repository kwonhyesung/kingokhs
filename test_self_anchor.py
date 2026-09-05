"""방향 패턴이 없는 역할(도사=졈프)도 이름표로 자기 위치를 구해야 한다.

실측 회귀: 도사 party 패턴에는 졈프_name/졈프_user만 있고 졈프_left/right/
back/top이 없다. 그래서 my_screen_pos가 영원히 (0,0)이었고, 모든 몬스터/
아이템이 월드 좌표 없이 "호박@?"로 나와 줍기·사냥 판단이 통째로 멈췄다.
"""
import json, io
from svc_sentinel import SentinelThread

PARTY = set(json.load(io.open("findtext_patterns.json", encoding="utf-8"))["party"])


def _pick(self_pattern, hits, pa_cfg={"click_offset_y": 56}):
    """svc_sentinel의 self 기준점 선택과 같은 규칙."""
    names = {self_pattern, *(f"{self_pattern}_{d}" for d in ("left", "right", "back", "top"))}
    direct = [h for h in hits if h["name"] in names]
    if direct:
        best = max(direct, key=lambda h: h["score"])
        return (best["cx"], best["cy"])
    tag = [h for h in hits if h["name"] == f"{self_pattern}_name"]
    if tag:
        best = max(tag, key=lambda h: h["score"])
        return (best["cx"], best["cy"] + SentinelThread._self_anchor_offset(pa_cfg))
    return (0, 0)


def demo():
    # 저장소의 실제 패턴 상태를 못 박는다 - 도사는 방향 패턴이 없다.
    assert not {f"졈프_{d}" for d in ("left", "right", "back", "top")} & PARTY
    assert "졈프_name" in PARTY
    assert {f"점프_{d}" for d in ("left", "right", "back", "top")} <= PARTY

    tag = [{"name": "졈프_name", "cx": 300, "cy": 200, "score": 1.0}]
    # 도사: 이름표만 있어도 위치가 나와야 한다 (몸까지 offset만큼 내려서)
    assert _pick("졈프", tag) == (300, 256)
    # 격수: 방향 패턴이 있으면 그게 우선 (보정 없음)
    both = [{"name": "점프_left", "cx": 111, "cy": 222, "score": 1.0},
            {"name": "점프_name", "cx": 300, "cy": 200, "score": 1.0}]
    assert _pick("점프", both) == (111, 222)
    # 아무것도 없으면 예전대로 '모름'
    assert _pick("졈프", [{"name": "채희_gm", "cx": 5, "cy": 5, "score": 1.0}]) == (0, 0)
    print("ok")


if __name__ == "__main__":
    demo()
