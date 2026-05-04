import cv2
import json
import numpy as np

def test_refined_hsv():
    with open("config.local.json", "r", encoding="utf-8") as f:
        config = json.load(f)
        
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    ret, frame = cap.read()
    if not ret: return
    
    for name in ["exp", "money", "y"]:
        reg = config.get(name)
        if not reg: continue
        crop = frame[reg["sy"]:reg["dy"], reg["sx"]:reg["dx"]]
        if crop.size == 0: continue
        
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        
        if name == "exp":
            # 초록색 정교한 타겟팅 (형광 초록)
            mask = cv2.inRange(hsv, np.array([45, 100, 150]), np.array([75, 255, 255]))
        else:
            # 황금색 정교한 타겟팅
            mask = cv2.inRange(hsv, np.array([20, 100, 150]), np.array([40, 255, 255]))
            
        print(f"\n=== {name} Refined Mask ===")
        for row in mask:
            print("".join(["#" if p > 128 else "." for p in row]))

if __name__ == "__main__":
    test_refined_hsv()
