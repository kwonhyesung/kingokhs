import cv2
import json
import numpy as np
import os
import re

AHK_PATTERNS = {
    "0": "|<0>*117$18.zzzzzzzVzzVzy0Dy0Dy0DkS3kS3kS3kS3kS3kS3kS3kS3kS3kS3y0Dy0DzVzzVzzzzzzzzzzU",
    "1": "|<1>*114$15.zzzkTy3z0Ts3z0TU3w0Ty3zkTy3zkTy3zkTy3zkTy3z01s0A07U0zzzU",
    "2": "|<2>*113$20.zzzzk7zw1zw07z01zk0Tky7wDVzzsTzy7zzVzzsTzsTzy7zzVzz1zzkTzk03w00w01z00Tzzzzzzy",
    "3": "|<3>*115$18.zzzy01y01s01s01s01zwDzwDzkzzkzy0zy0zzkDzkDzkDzwDzwDs0Ds0DU0zU0zzzzU",
    "4": "|<4>*110$16.zzzzkTz1zk7z0Tw1z07w0S31sA63kMD1U0600M01U0600Tz1zw7zkTz1zzzzzzzz00000Dzzzzzzzzzz000008",
    "5": "|<5>*112$20.zzzzk0Dw03z07zk1zw0Tw7zz1zzk1zw0Tz01zk0Tzy7zzVzzsTzs7zy1zk1zw0Tw0Tz07zzzzs",
    "6": "|<6>*113$15.zzzzzzkDy1y0zk7sDz1zsDz07s0wDVVwADVVwAC1VkAC1U0w07s3z0Tzzw",
    "7": "|<7>*118$15.zzz01s0A01U0A01zwDzVzkzy7y3zkTsDz1zsDw1zUDwDzVzwDzVzzzzU",
    "8": "|<8>*127$18.zzzy0Dy0DsD1sD1sD1sA1sA1y0Dy0Ds0zs0zVwDVwDVwDVwDVwDVkDVkDs0zs0zzzzU",
    "9": "|<9>*110$16.zzzw3zkDsA7UkS31Uw63kMD1Uw600M01s0zU3y0Dzkzz3y0zs3y0zs3zzzy"
}

char_to_val = {c: i for i, c in enumerate("0123456789+/ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")}

def decode(ahk_str):
    star_pos = ahk_str.find('*')
    dollar_pos = ahk_str.find('$')
    dot_pos = ahk_str.find('.')
    threshold = int(ahk_str[star_pos + 1:dollar_pos])
    width = int(ahk_str[dollar_pos + 1:dot_pos])
    data_str = ahk_str[dot_pos + 1:]
    
    bits = ""
    for char in data_str:
        val = char_to_val.get(char, 0)
        bits += "".join([str((val >> i) & 1) for i in range(5, -1, -1)])
    bits = re.sub(r'10*$', '', bits)
    
    height = len(bits) // width
    bitmap = []
    for i in range(height):
        row_bits = bits[i * width : (i + 1) * width]
        bitmap.append([int(b) for b in row_bits])
        
    return np.array(bitmap, dtype=np.uint8), threshold, width

templates = {}
for k, v in AHK_PATTERNS.items():
    templates[k] = decode(v)

def match_bitmap(screen_bin, template_bin):
    th, tw = template_bin.shape
    sh, sw = screen_bin.shape
    if th > sh or tw > sw: return []
    
    t1 = template_bin.astype(np.float32)
    t0 = 1.0 - t1
    total_1 = np.sum(t1)
    total_0 = np.sum(t0)
    
    s1 = cv2.filter2D(screen_bin.astype(np.float32), cv2.CV_32F, t1, anchor=(0, 0))
    s0 = cv2.filter2D((1 - screen_bin).astype(np.float32), cv2.CV_32F, t0, anchor=(0, 0))
    
    sim1 = s1 / total_1 if total_1 > 0 else s1 * 0
    sim0 = s0 / total_0 if total_0 > 0 else s0 * 0
    
    # 순정 조건: 글자 일치율 0.85 이상, 노이즈 0.80 이상
    mask = (sim1 >= 0.80) & (sim0 >= 0.60)
    ys, xs = np.where(mask)
    
    results = []
    for y, x in zip(ys, xs):
        score = sim1[y, x]
        results.append((int(x), int(y), float(score)))
    return results

def run_test():
    with open("config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
        
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    ret, frame = cap.read()
    if not ret: return
    
    for name in ["hp", "mp", "exp", "money", "x", "y"]:
        reg = config.get(name)
        if not reg: continue
        crop = frame[reg["sy"]:reg["dy"], reg["sx"]:reg["dx"]]
        if crop.size == 0: continue
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        
        all_matches = []
        for digit, (t_bin, thr, w) in templates.items():
            _, b_orig = cv2.threshold(gray, thr, 1, cv2.THRESH_BINARY)
            s_bin = b_orig.astype(np.uint8)
            
            matches = match_bitmap(s_bin, t_bin)
            for x, y, score in matches:
                all_matches.append((x, y, digit, score, w))
                
        # NMS (겹침 제거)
        all_matches.sort(key=lambda m: m[3], reverse=True)
        final_matches = []
        for m in all_matches:
            x, y, digit, score, w = m
            overlap = False
            for fx, fy, fd, fs, fw in final_matches:
                ox = max(0, min(x + w, fx + fw) - max(x, fx))
                if ox > min(w, fw) * 0.1:
                    overlap = True
                    break
            if not overlap:
                final_matches.append(m)
                
        final_matches.sort(key=lambda m: m[0])
        res = "".join([m[2] for m in final_matches])
        print(f"{name}: {res}")

if __name__ == "__main__":
    run_test()
