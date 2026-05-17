# -*- coding: utf-8 -*-
"""Warrior-only launcher for the shared worker stack."""

import os

from svc_worker import main


if __name__ == "__main__":
    # Keep manual F2 start and expose OBS/capture diagnostics on warrior PC.
    os.environ.setdefault("SVC_VERBOSE_LOGS", "1")
    os.environ.setdefault("SVC_HW_LOGS", "0")
    main(
        preset_role="격수",
        preset_network_role="격수",
        preset_auto_hunt=False,
    )
