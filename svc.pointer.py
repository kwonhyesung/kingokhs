import cv2
import numpy as np

# 1. 게임 화면 캡처본(스크린샷)을 하나 불러옵니다.
img = cv2.imread('ocr_debug/check_hp.png') # 아까 봇이 저장한 디버그 파일
hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

# 2. 숫자 부분의 픽셀값 확인
def get_pixel_color(event, x, y, flags, param):
    if event == cv2.EVENT_LBUTTONDOWN:
        print(f"픽셀 좌표: {x}, {y} | HSV 값: {hsv[y, x]}")

cv2.imshow('Check Color', hsv)
cv2.setMouseCallback('Check Color', get_pixel_color)
cv2.waitKey(0)