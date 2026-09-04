# -*- coding: utf-8 -*-
"""
단일 통합 실행 파일 (Main Entry Point)
어떤 PC에서든 이 실행 파일(main.py) 하나만 실행하고,
GUI 상단 툴바의 R(Role) 드롭다운에서 역할(도사1 허브, 도사2, 격수, 술사)을 선택하면 됩니다.
"""

import sys
import io
import os
import argparse

if sys.platform == 'win32':
    preferred_encoding = 'cp949'
    try:
        sys.stdin.reconfigure(encoding=preferred_encoding)
        sys.stdout.reconfigure(encoding=preferred_encoding, line_buffering=True)
        sys.stderr.reconfigure(encoding=preferred_encoding, line_buffering=True)
    except AttributeError:
        try:
            if sys.stdout.encoding != preferred_encoding:
                sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding=preferred_encoding, line_buffering=True)
            if sys.stderr.encoding != preferred_encoding:
                sys.stderr = io.TextIOWrapper(sys.stderr.detach(), encoding=preferred_encoding, line_buffering=True)
        except Exception:
            pass

from svc_worker import main as worker_main


def print_code_version():
    """실행 중인 코드의 커밋 해시를 찍는다.
    격수(DESKTOP2)는 별도 클론이라 여기 수정이 자동으로 가지 않는다 -
    두 PC의 이 줄을 눈으로 비교하면 버전 불일치를 바로 알 수 있다.
    (지난번 '도사가 격수를 힐 안 함' 사고의 진짜 원인이 이 불일치였다.)"""
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        out = subprocess.run(["git", "-C", here, "log", "-1", "--format=%h %cd", "--date=short"],
                             capture_output=True, text=True, timeout=5)
        version = (out.stdout or "").strip() or "unknown"
    except Exception:
        version = "unknown"
    print(f"[Version] code={version}")


def main(preset_role=None, preset_network_role=None, preset_auto_hunt=None):
    print_code_version()
    worker_main(
        preset_role=preset_role,
        preset_network_role=preset_network_role,
        preset_auto_hunt=preset_auto_hunt,
    )

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="도사1 허브 공통 실행 파일")
    parser.add_argument("--role", dest="preset_role", default=None, help="실행 역할: 도사1, 도사2, 격수, 술사")
    parser.add_argument("--network-role", dest="preset_network_role", default=None, help="UDP 송신 역할")
    parser.add_argument("--auto-hunt", dest="preset_auto_hunt", action="store_true", help="자동 사냥 활성화")
    parser.add_argument("--no-auto-hunt", dest="preset_auto_hunt", action="store_false", help="자동 사냥 비활성화")
    parser.set_defaults(preset_auto_hunt=None)
    args = parser.parse_args()
    main(
        preset_role=args.preset_role,
        preset_network_role=args.preset_network_role,
        preset_auto_hunt=args.preset_auto_hunt,
    )
