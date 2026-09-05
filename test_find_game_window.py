"""브라우저 창을 게임창으로 잡던 버그의 회귀 방지 검사.

실측 로그: 크롬 탭 제목 "• Discord | 사냥 1 | ... 바람의나라 클래식 - Chrome"이
토큰 "바람"에 걸려서, 봇이 크롬 창을 게임창으로 골랐다.
"""
import bis_core


class _FakeWin32:
    """EnumWindows만 흉내낸다. hwnd -> (제목, 클래스, 보임)."""
    def __init__(self, windows):
        self.windows = windows

    def EnumWindows(self, cb, extra):
        for hwnd in self.windows:
            if cb(hwnd, extra) is False:
                break

    def GetWindowText(self, hwnd):
        return self.windows[hwnd][0]

    def GetClassName(self, hwnd):
        return self.windows[hwnd][1]

    def IsWindowVisible(self, hwnd):
        return self.windows[hwnd][2]


def _run(windows, substring="바람"):
    real = bis_core.win32gui
    bis_core.win32gui = _FakeWin32(windows)
    try:
        return bis_core.find_game_window(substring)
    finally:
        bis_core.win32gui = real


CHROME = ("• Discord | 사냥 1 | '신화&시' 바람의나라 클래식 - Chrome", "Chrome_WidgetWin_1", True)
GAME = ("MapleStory Worlds-바람의나라 클래식", "UnityWndClass", True)


def demo():
    # 크롬이 먼저 열거돼도 게임창을 골라야 한다 (이게 그 버그였다)
    assert _run({1: CHROME, 2: GAME}) == 2
    # 게임창만 있으면 당연히 그것
    assert _run({2: GAME}) == 2
    # 크롬밖에 없으면 '못 찾음'. 크롬을 게임창이라고 우기는 것보다 낫다.
    assert _run({1: CHROME}) is None
    # 숨은 창은 원래대로 무시
    assert _run({2: ("MapleStory Worlds-바람의나라 클래식", "UnityWndClass", False)}) is None
    print("ok")


if __name__ == "__main__":
    demo()
