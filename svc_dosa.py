# -*- coding: utf-8 -*-
"""Dosa-only launcher for the shared worker stack."""

import sys
import io
import os
import time

if sys.platform == 'win32':
    os.system('chcp 65001 > nul 2>&1')
    try:
        sys.stdin.reconfigure(encoding='utf-8')
        sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
        sys.stderr.reconfigure(encoding='utf-8', line_buffering=True)
    except AttributeError:
        try:
            if sys.stdout.encoding != 'utf-8':
                sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding='utf-8', line_buffering=True)
            if sys.stderr.encoding != 'utf-8':
                sys.stderr = io.TextIOWrapper(sys.stderr.detach(), encoding='utf-8', line_buffering=True)
        except Exception:
            pass
else:
    try:
        if sys.stdout.encoding != 'utf-8':
            sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding='utf-8', line_buffering=True)
        if sys.stderr.encoding != 'utf-8':
            sys.stderr = io.TextIOWrapper(sys.stderr.detach(), encoding='utf-8', line_buffering=True)
    except Exception:
        pass

restart_delay = 0.0
try:
    restart_delay = float(os.environ.pop("DOSA_RESTART_DELAY", "0") or 0)
except Exception:
    restart_delay = 0.0
if restart_delay > 0:
    time.sleep(restart_delay)

from svc_worker import main


if __name__ == '__main__':
    main(
        preset_role='도사',
        preset_network_role='도사',
        preset_auto_hunt=False,
    )
