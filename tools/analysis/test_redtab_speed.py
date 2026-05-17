# -*- coding: utf-8 -*-
import numpy as np, re, cv2, mss, time

ahk_chars = "0123456789+/ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
c2v = {c: i for i, c in enumerate(ahk_chars)}
encoded = "zzzzzzzzzzzzzzzzs"; width = 49
bits = "".join(f"{c2v.get(ch,0):06b}" for ch in encoded)
bits = re.sub(r"10*$", "", bits)
h = len(bits) // width
bmp = np.array([1 if bits[i]=="1" else 0 for i in range(h*width)], dtype=np.uint8).reshape(h, width)
y1, x1 = np.where(bmp == 1); len1 = len(y1)

FG_R, FG_G, FG_B = 0xFF, 0x57, 0x57
max_dist_sq = int(9 * 255 * 255 * (1 - 0.90)**2)
ERR1 = 0.15; e1_max = max(1, int(len1 * ERR1))

print(f"[INFO] bitmap={width}x{h}, fg_px={len1}, e1_max={e1_max}, max_dist_sq={max_dist_sq}")

with mss.mss() as s:
    region = {"left": 274, "top": 50, "width": 917, "height": 787}
    shot = s.grab(region)
    img = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
    img_rgb = img[:, :, [2, 1, 0]]

sr = img_rgb[:, :, 0].astype(np.int32)
sg = img_rgb[:, :, 1].astype(np.int32)
sb = img_rgb[:, :, 2].astype(np.int32)
dist_sq = (sr - FG_R)**2 + (sg - FG_G)**2 + (sb - FG_B)**2
fg_map = (dist_sq <= max_dist_sq).astype(np.float32)
pattern_bin = bmp.astype(np.float32)

N = 200
t0 = time.perf_counter()
for _ in range(N):
    result = cv2.matchTemplate(fg_map, pattern_bin, cv2.TM_CCORR)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
elapsed = (time.perf_counter() - t0) / N * 1000
found = max_val >= (len1 - e1_max)
print(f"[SPEED] {elapsed:.3f}ms / 1회 (200회 평균)")
print(f"[RESULT] {'FOUND' if found else 'NOT FOUND'}, score={max_val:.0f}/{len1}, mismatch={int(len1-max_val)}/{e1_max}, loc={max_loc}")
