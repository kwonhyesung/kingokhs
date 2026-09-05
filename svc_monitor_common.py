"""svc_monitor_common.py - svc_monitor 계열 파일들이 공유하는 작은 유틸리티."""

import json
import os
from svc_kernel import Region

VERBOSE_MONITOR_LOGS = os.environ.get("SVC_VERBOSE_LOGS", "0") == "1"


def _monitor_log(*args, **kwargs):
    if VERBOSE_MONITOR_LOGS:
        print(*args, **kwargs)


def click_offset_y() -> int:
    """패턴 중심에서 '실제로 클릭해야 하는 지점'까지의 y 픽셀.

    실측: 점프_* 패턴 중심을 그대로 클릭하면 캐릭터가 안 잡히고, 24~26px
    아래를 찍어야 잡힌다. 더 내려가면 한 칸 아래에 있는 다른 대상(다른
    캐릭터나 몬스터)이 잡히므로 맞는 구간이 좁다 - 해상도/UI 배율이 다른
    PC가 있을 수 있어 config로 뺀다(play_area.click_offset_y, 기본 25).
    오버레이 초록점이 이 지점에 찍히니, 점이 캐릭터 위에 오도록 숫자만
    맞추면 된다."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        with open(path, encoding="utf-8") as f:
            return int((json.load(f).get("play_area") or {}).get("click_offset_y", 25))
    except Exception:
        return 25


def dict_to_region(data: dict) -> Region:
    """dict를 Region 객체로 변환하는 헬퍼 함수"""
    if isinstance(data, Region):
        return data
    if isinstance(data, dict):
        return Region(data["sx"], data["sy"], data["dx"], data["dy"])
    raise ValueError(f"Invalid data type for Region: {type(data)}")
