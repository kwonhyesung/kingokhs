import os
import time
import json
import threading
import cv2
import numpy as np
import tkinter as tk
import customtkinter as ctk
from PIL import Image, ImageTk

# 기존 프로젝트 모듈 임포트
from svc_kernel import GameState, Region
from svc_monitor import PatternMatcher, get_camera
# FindText 엔진 임포트
from svc_findtext import FindTextEngine

class OCRTesterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("BIS OCR 비트맵 정밀 테스트 (AHK Threshold Mode)")
        self.root.geometry("1200x900")
        
        # 상태 및 엔진 초기화
        self.state = GameState()
        self.config_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.local.json")
        self.tmpl_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temple")
        self.matcher = PatternMatcher(self.tmpl_root, self.state)
        
        # FindText 숫자 패턴들
        self.findtext_patterns = {
            "0": "|<0>*117$18.zzzzzzzVzzVzy0Dy0Dy0DkS3kS3kS3kS3kS3kS3kS3kS3kS3kS3kS3y0Dy0DzVzzVzzzzzzzzzzU",
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
        
        # FindText 엔진 초기화 (템플릿 사전 디코딩)
        self.findtext_engine = FindTextEngine(self.findtext_patterns)
        
        self.regions = {}
        self.load_roi()
        
        # GUI 구성
        self.setup_ui()
        self.img_refs = {} 
        
        # 카메라 스레드 시작
        self.running = True
        self.thread = threading.Thread(target=self.update_loop, daemon=True)
        self.thread.start()

    def load_roi(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    fields = ["hp", "mp", "exp", "money", "x", "y"]
                    for field in fields:
                        if field in config:
                            reg = config[field]
                            self.regions[field] = Region(reg["sx"], reg["sy"], reg["dx"], reg["dy"])
            except Exception: pass

    def setup_ui(self):
        self.top_frame = ctk.CTkFrame(self.root, height=50)
        self.top_frame.pack(fill="x", padx=10, pady=5)
        self.lbl_fps = ctk.CTkLabel(self.top_frame, text="FPS: 0", font=("Inter", 12))
        self.lbl_fps.pack(side="left", padx=20)
        
        self.main_frame = ctk.CTkScrollableFrame(self.root)
        self.main_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        self.widgets = {}
        fields = ["hp", "mp", "exp", "money", "x", "y"]
        for i, field in enumerate(fields):
            f_frame = ctk.CTkFrame(self.main_frame, border_width=1, border_color="#343A46")
            f_frame.grid(row=i, column=0, padx=10, pady=5, sticky="nsew")
            
            lbl_title = ctk.CTkLabel(f_frame, text=field.upper(), width=80, font=("Inter", 14, "bold"), text_color="#10B981")
            lbl_title.pack(side="left", padx=10)
            
            canvas_group = ctk.CTkFrame(f_frame, fg_color="transparent")
            canvas_group.pack(side="left", fill="x", expand=True)
            
            self.widgets[field] = {}
            for tag, title in [("raw", "RAW"), ("bin", "AHK BIN"), ("tmpl", "MATCHED TEMPLATE")]:
                box = ctk.CTkFrame(canvas_group, fg_color="transparent")
                box.pack(side="left", padx=5)
                ctk.CTkLabel(box, text=title, font=("Inter", 8)).pack()
                canvas = tk.Canvas(box, width=150, height=40, bg="#1A1C23", highlightthickness=0)
                canvas.pack()
                self.widgets[field][f"c_{tag}"] = canvas

            res_box = ctk.CTkFrame(f_frame, width=200, fg_color="transparent")
            res_box.pack(side="left", padx=20)
            self.widgets[field]["lbl_result"] = ctk.CTkLabel(res_box, text="-", font=("Inter", 20, "bold"))
            self.widgets[field]["lbl_result"].pack()
            self.widgets[field]["lbl_score"] = ctk.CTkLabel(res_box, text="Score: 0.00", font=("Inter", 10))
            self.widgets[field]["lbl_score"].pack()

    def update_loop(self):
        cap, _ = get_camera()
        if not cap: return

        frame_count = 0
        last_time = time.time()
        
        while self.running:
            for _ in range(2): cap.grab()
            ret, frame = cap.retrieve()
            if not ret or frame is None: continue

            fh, fw = frame.shape[:2]
            for name, reg in self.regions.items():
                sx, sy, dx, dy = int(reg.sx), int(reg.sy), int(reg.dx), int(reg.dy)
                crop = frame[max(0,sy):min(fh,dy), max(0,sx):min(fw,dx)]
                if crop.size == 0: continue
                
                # 1. 기존 고정밀 인식 수행
                result_text, score = self.matcher.recognize_digit_bitwise(crop)
                
                # 2. FindText 패턴 매칭으로 인식 시도 (화면 전체에서 검색 후 ROI 필터링)
                findtext_result = self.recognize_with_findtext(frame, sx, sy, dx, dy)
                
                # FindText 결과가 있으면 사용, 없으면 기존 결과 사용
                if findtext_result:
                    result_text = findtext_result
                    score = 1.0  # FindText 매칭은 높은 점수 부여
                
                # 3. 시각화용 데이터 생성
                gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
                # 시각화용 BIN은 첫 번째 인식된 숫자의 임계값을 사용하거나 기본 127 사용
                viz_thr = 127
                if result_text and result_text[0] in self.matcher._ahk_pattern_cache:
                    viz_thr = self.matcher._ahk_pattern_cache[result_text[0]]["threshold"]
                
                _, viz_bin = cv2.threshold(gray, viz_thr, 255, cv2.THRESH_BINARY)
                
                matched_bitmap = np.zeros_like(viz_bin)
                if result_text:
                    curr_x = 0
                    for char in result_text:
                        data = self.matcher._ahk_pattern_cache.get(char)
                        if data:
                            tmpl = data["bitmap"]
                            th, tw = tmpl.shape
                            if curr_x + tw <= matched_bitmap.shape[1]:
                                matched_bitmap[0:min(th, matched_bitmap.shape[0]), curr_x:curr_x+tw] = tmpl[0:min(th, matched_bitmap.shape[0]), :] * 255
                                curr_x += tw + 1
                
                self.root.after(0, self.safe_update_ui, name, crop.copy(), viz_bin.copy(), matched_bitmap.copy(), result_text, score)

            frame_count += 1
            if time.time() - last_time >= 1.0:
                fps = frame_count / (time.time() - last_time)
                self.root.after(0, lambda f=fps: self.lbl_fps.configure(text=f"FPS: {f:.1f}"))
                frame_count = 0
                last_time = time.time()
            time.sleep(0.05)

        cap.release()

    def safe_update_ui(self, name, raw, viz_bin, tmpl_bit, result, score):
        if not self.running: return
        try:
            w = self.widgets[name]
            img_raw = Image.fromarray(cv2.cvtColor(raw, cv2.COLOR_BGR2RGB)).resize((150, 40), Image.NEAREST)
            self.img_refs[f"{name}_raw"] = ImageTk.PhotoImage(img_raw)
            w["c_raw"].create_image(75, 20, image=self.img_refs[f"{name}_raw"])
            
            img_bin = Image.fromarray(viz_bin).resize((150, 40), Image.NEAREST)
            self.img_refs[f"{name}_bin"] = ImageTk.PhotoImage(img_bin)
            w["c_bin"].create_image(75, 20, image=self.img_refs[f"{name}_bin"])
            
            img_tmpl = Image.fromarray(tmpl_bit).resize((150, 40), Image.NEAREST)
            self.img_refs[f"{name}_tmpl"] = ImageTk.PhotoImage(img_tmpl)
            w["c_tmpl"].create_image(75, 20, image=self.img_refs[f"{name}_tmpl"])
            
            # 결과를 정수로 변환하여 표시
            int_result = ""
            if result:
                try:
                    int_result = str(int(result))
                except ValueError:
                    int_result = result
            
            w["lbl_result"].configure(text=int_result if int_result else "FAIL",
                                     text_color="#10B981" if int_result else "#FF5555")
            w["lbl_score"].configure(text=f"Score: {score:.2f}")
        except Exception: pass

    def recognize_with_findtext(self, full_frame: np.ndarray, sx: int, sy: int, dx: int, dy: int) -> str:
        """
        FindText 패턴으로 숫자 인식
        """
        fh, fw = full_frame.shape[:2]
        crop = full_frame[max(0, sy):min(fh, dy), max(0, sx):min(fw, dx)]
        if crop.size == 0:
            return ""
        
        # 그레이스케일 변환
        crop_gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        
        # 모든 숫자 패턴에 대해 검색
        all_matches = []
        
        for digit, (template_bin, threshold) in self.findtext_engine.template_cache.items():
            if template_bin is None:
                continue
            
            # 고정 임계값으로 이진화 (AHK와 동일)
            _, screen_bin = cv2.threshold(crop_gray, threshold, 1, cv2.THRESH_BINARY)
            
            # 매칭 수행
            matches = self.findtext_engine.match_bitmap(screen_bin, template_bin, threshold)
            
            if matches:
                for x, y in matches:
                    all_matches.append((x, y, digit))
        
        if not all_matches:
            return ""
        
        # x 좌표 기준 정렬 (숫자 순서 보정)
        all_matches.sort(key=lambda m: m[0])
        
        # 중복 제거
        final_digits = []
        last_x = -999
        min_gap = 8  # 최소 간격 조정
        
        for x, y, digit in all_matches:
            if x - last_x > min_gap:
                final_digits.append(digit)
                last_x = x
        
        return "".join(final_digits)

    # FindText 래퍼 메서드들
    def findtext_find_pattern(self, pattern_text: str, x1: int = 0, y1: int = 0, 
                               x2: int = 0, y2: int = 0, err1: float = 0.1, 
                               err0: float = 0.1, find_all: bool = True):
        """
        FindText 패턴 찾기
        :param pattern_text: FindText 형식의 패턴 텍스트
        :param x1, y1, x2, y2: 검색 영역
        :param err1, err0: 오차 허용율
        :param find_all: 모든 결과 찾기 여부
        :return: 찾은 결과 리스트
        """
        return self.findtext.find_text(pattern_text, x1, y1, x2, y2, err1, err0, find_all=find_all)
    
    def findtext_find_color(self, color: int, variation: int = 10,
                            x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0):
        """
        특정 색상 찾기
        :param color: RGB 색상 값
        :param variation: 색상 변화 허용 범위
        :param x1, y1, x2, y2: 검색 영역
        :return: 찾은 위치 리스트
        """
        return self.findtext.find_color(color, variation, x1, y1, x2, y2)
    
    def findtext_wait_pattern(self, pattern_text: str, timeout: float = 5.0,
                              appear: bool = True, x1: int = 0, y1: int = 0,
                              x2: int = 0, y2: int = 0, err1: float = 0.1, err0: float = 0.1):
        """
        패턴이 나타나거나 사라질 때까지 대기
        :param pattern_text: FindText 형식의 패턴 텍스트
        :param timeout: 타임아웃 (초)
        :param appear: 나타날 때까지 대기 (False면 사라질 때까지)
        :param x1, y1, x2, y2: 검색 영역
        :param err1, err0: 오차 허용율
        :return: 찾은 결과 또는 None
        """
        return self.findtext.wait_text(pattern_text, timeout, appear, x1, y1, x2, y2, err1, err0)

if __name__ == "__main__":
    ctk.set_appearance_mode("Dark")
    root = ctk.CTk()
    app = OCRTesterApp(root)
    root.mainloop()
