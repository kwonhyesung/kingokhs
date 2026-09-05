# -*- coding: utf-8 -*-
"""PC마다 다른 ESP32 명령 형식이 실제로 갈리는지 검증 (하드웨어 없이).

격수 PC(COM3)는 'K,<키>'만 먹히고 도사 PC(COM11)는 'D:/U:'만 먹힌다는 게
실측으로 확인됐다. 형식을 잘못 고르면 키가 나가는 것처럼 보이면서 캐릭터는
아무것도 안 하므로, 어느 형식이 실제로 회선에 실리는지가 핵심이다.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bis_core import BisHardware  # noqa: E402


def _capture(form):
    """send_force를 가로채서 실제로 나간 명령 문자열만 모은다."""
    hw = BisHardware.__new__(BisHardware)
    hw._move_command_form = form
    sent = []
    hw.send_force = lambda cmd: (sent.append(cmd), True)[1]
    return hw, sent


def test_k_form_sends_single_shot():
    hw, sent = _capture("K")
    assert hw.press_once("right", duration=0.09) is True
    assert sent == ["K,right"], f"K 형식인데 {sent}가 나갔다"


def test_du_form_sends_down_then_up():
    hw, sent = _capture("DU")
    assert hw.press_once("right", duration=0.01) is True
    assert sent == ["D:right", "U:right"], f"DU 형식인데 {sent}가 나갔다"


def test_du_releases_even_if_hold_raises():
    """키를 누른 채로 예외가 나면 캐릭터가 계속 걷는다 - 반드시 떼야 한다."""
    hw, sent = _capture("DU")
    import bis_core
    original = bis_core.humanized_sleep
    bis_core.humanized_sleep = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        hw.press_once("left", duration=0.01)
    except RuntimeError:
        pass
    finally:
        bis_core.humanized_sleep = original
    assert "U:left" in sent, "예외가 나도 키는 떼져야 한다"


def test_default_form_is_du():
    """검사를 안 돌린 PC는 지금까지의 동작 그대로여야 한다(도사 PC 보호)."""
    from config_utils import load_hardware_config
    assert load_hardware_config()["move_command_form"] in ("DU", "K")


if __name__ == "__main__":
    test_k_form_sends_single_shot()
    test_du_form_sends_down_then_up()
    test_du_releases_even_if_hold_raises()
    test_default_form_is_du()
    print("test_move_command_form OK")
