# -*- coding: utf-8 -*-
"""Dosa2 launcher for the shared worker stack."""

import sys
import io
import os

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

from svc_worker import main


if __name__ == '__main__':
    os.environ.setdefault("SVC_VERBOSE_LOGS", "1")
    main(
        preset_role='도사2',
        preset_network_role='도사2',
        preset_auto_hunt=False,
    )
