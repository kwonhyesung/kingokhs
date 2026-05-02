import cv2
import json
import numpy as np

def dump_ascii():
    with open("config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
        
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    ret, frame = cap.read()
    cap.release()
    
    if not ret:
        print("카메라 프레임을 읽을 수 없습니다.")
        return
    
    for name in ["hp", "mp", "exp", "money", "x", "y"]:
        reg = config.get(name)
        if not reg: continue
        crop = frame[reg["sy"]:reg["dy"], reg["sx"]:reg["dx"]]
        if crop.size == 0:
            print(f"=== {name} 크기 0 (설정 오류) ===")
            continue
        
        if name in ("hp", "mp", "exp"):
            ch = crop[:, :, 1] # 초록색 폰트 (Green channel)
            thr = 117
        else:
            ch = crop[:, :, 2] # 황금색 폰트 (Red channel)
            thr = 117
            
        print(f"\n=== {name} 크기: {ch.shape} ===")
        _, bin_img = cv2.threshold(ch, thr, 1, cv2.THRESH_BINARY)
        for row in bin_img:
            print("".join(["#" if p > 0 else "." for p in row]))

if __name__ == "__main__":
    dump_ascii()
