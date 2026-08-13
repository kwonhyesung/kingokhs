"""svc_monitor_common.py - svc_monitor 계열 파일들이 공유하는 작은 유틸리티."""

import os
from svc_kernel import Region

VERBOSE_MONITOR_LOGS = os.environ.get("SVC_VERBOSE_LOGS", "0") == "1"


def _monitor_log(*args, **kwargs):
    if VERBOSE_MONITOR_LOGS:
        print(*args, **kwargs)


def dict_to_region(data: dict) -> Region:
    """dict를 Region 객체로 변환하는 헬퍼 함수"""
    if isinstance(data, Region):
        return data
    if isinstance(data, dict):
        return Region(data["sx"], data["sy"], data["dx"], data["dy"])
    raise ValueError(f"Invalid data type for Region: {type(data)}")
