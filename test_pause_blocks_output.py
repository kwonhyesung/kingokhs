"""F3(일시정지) 중에는 '떼기'만 나가고 나머지 입력은 전부 막혀야 한다.

실측 회귀: "[Hotkey] Pause ON" 바로 다음 줄이
"[Support] HP heal casting on warrior: key=3" 이었다 - 긴 루틴이 자기 단계
사이에서 플래그를 안 보고 계속 키를 보냈다.
"""
from bis_core import hw


class _State:
    def __init__(self, paused):
        self.automation_paused = paused


def _sent(paused, commands):
    """paused 상태에서 실제로 하드웨어까지 내려간 명령만 돌려준다."""
    out = []
    real_state, real_deliver = hw.state, hw._write_hardware_command
    real_ready = hw.is_hardware_ready
    hw.state = _State(paused)
    hw.is_hardware_ready = lambda: True
    hw._write_hardware_command = lambda cmd: out.append(cmd)
    try:
        for c in commands:
            hw.send_force(c)
    finally:
        hw.state, hw._write_hardware_command = real_state, real_deliver
        hw.is_hardware_ready = real_ready
    return out


def demo():
    cmds = ["D:3", "K,3", "M:100,200", "CLICK", "U:left", "U:all", "RELEASE_ALL"]

    # 평소엔 전부 나간다
    assert _sent(False, cmds) == cmds

    # 일시정지 중엔 '떼기'만 나간다
    assert _sent(True, cmds) == ["U:left", "U:all", "RELEASE_ALL"]

    # 분류 자체도 못 박아둔다
    assert hw.is_release_command("RELEASE_ALL")
    assert hw.is_release_command("U:left")
    assert not hw.is_release_command("D:left")
    assert not hw.is_release_command("K,3")
    assert not hw.is_release_command("")
    print("ok")


if __name__ == "__main__":
    demo()
