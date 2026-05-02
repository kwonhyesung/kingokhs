# -*- coding: utf-8 -*-
"""
숫자 필드 GUI 업데이트 속도 테스트
다른 기능 없이 숫자 라벨만 업데이트하여 병목 현상 확인
"""
import time
import threading
import tkinter as tk
from tkinter import ttk
import customtkinter as ctk

# CustomTkinter 설정
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

class NumericTestGUI:
    def __init__(self):
        self.root = ctk.CTk()
        self.root.title("숫자 필드 업데이트 속도 테스트")
        self.root.geometry("400x300")
        
        # 상태 변수
        self.hp = 1000
        self.mp = 500
        self.exp = 0
        self.money = 10000
        self.x = 0
        self.y = 0
        self.frame_count = 0
        self.running = True
        
        # GUI 생성
        self.create_widgets()
        
        # 업데이트 시작
        self.start_update()
    
    def create_widgets(self):
        """숫자 라벨만 생성"""
        # 메인 프레임
        main_frame = ctk.CTkFrame(self.root)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        # HP
        hp_frame = ctk.CTkFrame(main_frame)
        hp_frame.pack(fill="x", pady=5)
        ctk.CTkLabel(hp_frame, text="HP:", width=50).pack(side="left", padx=5)
        self.lbl_hp = ctk.CTkLabel(hp_frame, text="1000", font=("Inter", 16, "bold"))
        self.lbl_hp.pack(side="left", padx=5)
        
        # MP
        mp_frame = ctk.CTkFrame(main_frame)
        mp_frame.pack(fill="x", pady=5)
        ctk.CTkLabel(mp_frame, text="MP:", width=50).pack(side="left", padx=5)
        self.lbl_mp = ctk.CTkLabel(mp_frame, text="500", font=("Inter", 16, "bold"))
        self.lbl_mp.pack(side="left", padx=5)
        
        # EXP
        exp_frame = ctk.CTkFrame(main_frame)
        exp_frame.pack(fill="x", pady=5)
        ctk.CTkLabel(exp_frame, text="EXP:", width=50).pack(side="left", padx=5)
        self.lbl_exp = ctk.CTkLabel(exp_frame, text="0", font=("Inter", 16, "bold"))
        self.lbl_exp.pack(side="left", padx=5)
        
        # Money
        money_frame = ctk.CTkFrame(main_frame)
        money_frame.pack(fill="x", pady=5)
        ctk.CTkLabel(money_frame, text="Money:", width=50).pack(side="left", padx=5)
        self.lbl_money = ctk.CTkLabel(money_frame, text="10000", font=("Inter", 16, "bold"))
        self.lbl_money.pack(side="left", padx=5)
        
        # Position
        pos_frame = ctk.CTkFrame(main_frame)
        pos_frame.pack(fill="x", pady=5)
        ctk.CTkLabel(pos_frame, text="POS:", width=50).pack(side="left", padx=5)
        self.lbl_pos = ctk.CTkLabel(pos_frame, text="0,0", font=("Inter", 16, "bold"))
        self.lbl_pos.pack(side="left", padx=5)
        
        # 성능 표시
        self.lbl_perf = ctk.CTkLabel(main_frame, text="FPS: 0", font=("Inter", 12))
        self.lbl_perf.pack(pady=10)
    
    def update_values(self):
        """값 업데이트 (시뮬레이션)"""
        self.hp = max(1, self.hp - 1) if self.hp > 1 else 1000
        self.mp = max(1, self.mp - 1) if self.mp > 1 else 500
        self.exp += 10
        self.money += 100
        self.x = (self.x + 1) % 100
        self.y = (self.y + 1) % 100
    
    def update_gui(self):
        """GUI 업데이트 (16ms = 60 FPS)"""
        if not self.running:
            return
        
        perf_start = time.perf_counter()
        
        try:
            # 값 업데이트
            self.update_values()
            
            # 라벨 업데이트
            self.lbl_hp.configure(text=str(self.hp))
            self.lbl_mp.configure(text=str(self.mp))
            self.lbl_exp.configure(text=str(self.exp))
            self.lbl_money.configure(text=str(self.money))
            self.lbl_pos.configure(text=f"{self.x},{self.y}")
            
            # 성능 측정
            self.frame_count += 1
            if self.frame_count % 60 == 0:
                perf_end = time.perf_counter()
                perf_ms = (perf_end - perf_start) * 1000
                fps = 1000 / max(0.1, perf_ms)
                self.lbl_perf.configure(text=f"Update: {perf_ms:.2f}ms | FPS: {fps:.1f}")
                print(f"[PERF] GUI Update: {perf_ms:.2f}ms | FPS: {fps:.1f}")
        
        except Exception as e:
            print(f"[ERROR] {e}")
        
        finally:
            # 16ms 후 다시 호출 (60 FPS)
            self.root.after(16, self.update_gui)
    
    def start_update(self):
        """업데이트 시작"""
        print("[TEST] 숫자 필드 GUI 업데이트 테스트 시작...")
        self.update_gui()
    
    def run(self):
        """GUI 실행"""
        self.root.mainloop()
        self.running = False

if __name__ == "__main__":
    app = NumericTestGUI()
    app.run()
