# -*- coding: utf-8 -*-
"""Warrior-only launcher for the shared worker stack."""

from svc_worker import main


if __name__ == "__main__":
    main(
        preset_role="격수",
        preset_network_role="격수",
        preset_auto_hunt=False,
    )
