"""
digit_bootstrap.py - 게임 화면에서 직접 숫자 템플릿을 추출하는 도구

사용법:
  1. 게임을 실행한 상태에서 이 스크립트를 실행하세요.
  2. HP/MP 등 숫자 영역을 현재 config.json 좌표로 자동 캡처합니다.
  3. ocr_debug 폴더에 저장된 기존 캡처 이미지들을 templates/default 에 복사합니다.

  더 정확한 방법:
  - templates/default/0.png ~ 9.png 를 직접 준비하면 자동 생성 템플릿보다 인식률이 높아집니다.
  - 각 파일은 흰 배경에 검은 숫자 또는 검은 배경에 흰 숫자의 단일 숫자 PNG 이어야 합니다.
"""

import os, json, cv2, numpy as np
import win32gui
import mss

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
OUT_DIR     = os.path.join(SCRIPT_DIR, "templates", "default")

def find_game_window(substring):
    found = [None]
    def cb(hwnd, _):
        if substring in win32gui.GetWindowText(hwnd) and win32gui.IsWindowVisible(hwnd):
            found[0] = hwnd; return False
        return True
    try: win32gui.EnumWindows(cb, None)
    except: pass
    return found[0]

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    hwnd = find_game_window("바람")
    if not hwnd:
        print("❌ 게임 창을 찾을 수 없습니다."); return

    c_left, c_top = win32gui.ClientToScreen(hwnd, (0, 0))
    _, _, w, h    = win32gui.GetClientRect(hwnd)

    with mss.mss() as sct:
        monitor = {"top": c_top, "left": c_left, "width": w, "height": h}
        img_np = np.array(sct.grab(monitor))
        img_np = cv2.cvtColor(img_np, cv2.COLOR_BGRA2RGB)

    if img_np is None or img_np.size == 0:
        print("❌ 화면 캡처 실패"); return

    fields = ["hp", "mp", "exp", "money", "x", "y"]
    for field in fields:
        if field not in cfg or not isinstance(cfg[field], dict):
            continue
        reg  = cfg[field]
        crop = img_np[reg["sy"]:reg["dy"], reg["sx"]:reg["dx"]]
        if crop.size == 0: continue

        bgr    = cv2.cvtColor(crop, cv2.COLOR_RGB2BGR)
        scaled = cv2.resize(bgr, None, fx=2, fy=2, interpolation=cv2.INTER_LANCZOS4)
        gray   = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
        _, bw  = cv2.threshold(gray, 141, 255, cv2.THRESH_BINARY)
        path   = os.path.join(OUT_DIR, f"_sample_{field}.png")
        cv2.imwrite(path, bw)
        print(f"✅ 저장: {path}")

    print("\n📁 templates/default/ 에 0.png~9.png 를 직접 배치하면 인식률이 향상됩니다.")
    print("   위 샘플 이미지를 참고하여 게임의 숫자 폰트와 일치하는 PNG 를 준비하세요.")

if __name__ == "__main__": main()
