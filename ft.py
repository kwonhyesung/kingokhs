"""
FindText 패턴 캡처/테스트 GUI.

ft.ahk의 캡처 도구(Gui "Show")를 대체하는 Tkinter 버전. 매칭 엔진은 직접
재구현하지 않고 findtext_wrapper.FindText(이미 ft.ahk와 호환되는 정확한 엔진)를
그대로 사용한다 — 화면 선택 → 패턴 문자열 생성 → 같은 엔진으로 즉시 테스트.

사용법: py ft.py
(구 파일명: find_text.py — 아무 곳에서도 import되지 않아 ft.py로 이름만 변경, 내용은 동일)
설계 문서: FINDTEXT_PORT_DESIGN.md
"""
import json
import os
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import cv2
import numpy as np
from PIL import Image, ImageTk
import mss

from findtext_wrapper import get_findtext

try:
    import pyperclip
except ImportError:
    pyperclip = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PATTERNS_FILE = os.path.join(SCRIPT_DIR, "findtext_patterns.json")
PATTERN_CATEGORIES = ("map", "item", "monster", "party")


def grab_full_screen_bgr() -> np.ndarray:
    """가상 화면 전체(모든 모니터)를 BGR numpy 배열로 캡처."""
    with mss.mss() as sct:
        raw = sct.grab(sct.monitors[0])
        arr = np.array(raw)  # BGRA
        return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)


def get_virtual_screen_rect():
    """가상 화면 전체의 (left, top, width, height). 왼쪽/위에 있는 모니터가
    있으면 left/top이 음수일 수 있음 — 오버레이 창을 실제 화면 위에 정확히
    겹치려면 이 offset이 필요하다."""
    with mss.mss() as sct:
        mon = sct.monitors[0]
        return mon["left"], mon["top"], mon["width"], mon["height"]


# 오버레이 창에서 "투명"으로 취급할 색상 키 (Windows -transparentcolor).
# 매칭 박스(빨강)나 안내문(노랑)과 겹치지 않는 흔치 않은 색을 사용.
OVERLAY_TRANSPARENT_KEY = "#123456"


def pick_region(parent: tk.Tk, image_bgr: np.ndarray):
    """
    전체 이미지를 풀스크린으로 띄우고 드래그로 사각형을 선택하게 한 뒤
    (x1, y1, x2, y2) 를 반환. 취소 시 None.
    """
    h, w = image_bgr.shape[:2]
    rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(rgb)

    top = tk.Toplevel(parent)
    top.attributes("-fullscreen", True)
    top.attributes("-topmost", True)
    top.configure(cursor="crosshair")

    canvas = tk.Canvas(top, width=w, height=h, highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    photo = ImageTk.PhotoImage(pil_img)
    canvas.create_image(0, 0, image=photo, anchor="nw")
    hint = canvas.create_text(
        w // 2, 24, text="드래그로 영역 선택 · Esc 취소",
        fill="yellow", font=("맑은 고딕", 14, "bold"),
    )

    state = {"start": None, "rect": None, "result": None}

    def on_down(ev):
        state["start"] = (ev.x, ev.y)
        if state["rect"]:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(ev.x, ev.y, ev.x, ev.y, outline="red", width=2)

    def on_drag(ev):
        if state["start"] and state["rect"]:
            x0, y0 = state["start"]
            canvas.coords(state["rect"], x0, y0, ev.x, ev.y)

    def on_up(ev):
        if not state["start"]:
            return
        x0, y0 = state["start"]
        x1, y1 = min(x0, ev.x), min(y0, ev.y)
        x2, y2 = max(x0, ev.x), max(y0, ev.y)
        if x2 - x1 >= 2 and y2 - y1 >= 2:
            state["result"] = (x1, y1, x2, y2)
            top.destroy()

    def on_escape(_ev):
        state["result"] = None
        top.destroy()

    canvas.bind("<ButtonPress-1>", on_down)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_up)
    top.bind("<Escape>", on_escape)

    top.grab_set()
    parent.wait_window(top)
    return state["result"]


class FindTextGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("FindText 캡처/테스트")
        self.ft = get_findtext()

        self.source_bgr: np.ndarray | None = None   # 캡처/로드한 전체 이미지
        self.roi_bgr: np.ndarray | None = None       # 선택된 영역
        self.roi_offset = (0, 0)
        self._orig_region = None                     # 드래그로 처음 선택한 (x1,y1,x2,y2)
        self._adj = {"left": 0, "top": 0, "right": 0, "bottom": 0}  # 1px 미세조정 누적량

        self.color_candidates: list = []   # [(r,g,b,tol), ...] 현재 패턴 항목의 색상 후보(OR)
        self.pattern_list: list = []       # 결합 대기 중인 완성된 패턴 문자열들

        self._build_ui()

    # ---------------------------------------------------------- UI 구성
    def _build_ui(self):
        top_bar = ttk.Frame(self.root, padding=8)
        top_bar.pack(fill="x")
        ttk.Button(top_bar, text="화면에서 캡처", command=self.on_capture_screen).pack(side="left", padx=4)
        ttk.Button(top_bar, text="이미지 파일 열기", command=self.on_open_image).pack(side="left", padx=4)

        body = ttk.Frame(self.root, padding=8)
        body.pack(fill="both", expand=True)

        # 미리보기 (원본 / 이진화)
        preview = ttk.LabelFrame(body, text="선택 영역 미리보기 (원본 / 이진화)", padding=8)
        preview.pack(fill="x")
        self.canvas_orig = tk.Canvas(preview, width=260, height=120, bg="#222")
        self.canvas_orig.pack(side="left", padx=4)
        self.canvas_bin = tk.Canvas(preview, width=260, height=120, bg="#222")
        self.canvas_bin.pack(side="left", padx=4)

        # 임계값 조절
        thr_frame = ttk.Frame(body, padding=(0, 8))
        thr_frame.pack(fill="x")
        ttk.Label(thr_frame, text="임계값(threshold):").pack(side="left")
        self.thr_var = tk.IntVar(value=127)
        self.thr_scale = ttk.Scale(thr_frame, from_=0, to=255, variable=self.thr_var,
                                    orient="horizontal", command=lambda _v: self._refresh_binary_preview())
        self.thr_scale.pack(side="left", fill="x", expand=True, padx=8)
        self.thr_label = ttk.Label(thr_frame, text="127")
        self.thr_label.pack(side="left", padx=4)
        ttk.Button(thr_frame, text="자동", command=self.on_auto_threshold).pack(side="left", padx=4)

        # 1px 정밀 조절 (ft.ahk 캡처 도구의 미세조정과 동일)
        adj_frame = ttk.Frame(body, padding=(0, 4))
        adj_frame.pack(fill="x")
        ttk.Label(adj_frame, text="1px 정밀 조절:").pack(side="left")
        for label, edge in (("왼쪽", "left"), ("위", "top"), ("오른쪽", "right"), ("아래", "bottom")):
            sub = ttk.Frame(adj_frame)
            sub.pack(side="left", padx=6)
            ttk.Label(sub, text=label).pack()
            row = ttk.Frame(sub)
            row.pack()
            ttk.Button(row, text="-", width=2, command=lambda e=edge: self.on_nudge(e, -1)).pack(side="left")
            ttk.Button(row, text="+", width=2, command=lambda e=edge: self.on_nudge(e, 1)).pack(side="left")
        ttk.Button(adj_frame, text="리셋", command=self.on_reset_adjustment).pack(side="left", padx=10)

        # 매칭 모드: 흑백(임계값, mode2) / 색상(모양+RGB, 이 엔진의 확장 문법)
        mode_frame = ttk.Frame(body, padding=(0, 4))
        mode_frame.pack(fill="x")
        ttk.Label(mode_frame, text="매칭 모드:").pack(side="left")
        self.mode_var = tk.StringVar(value="흑백")
        ttk.Radiobutton(mode_frame, text="흑백(임계값)", variable=self.mode_var, value="흑백",
                         command=self._on_mode_change).pack(side="left", padx=4)
        ttk.Radiobutton(mode_frame, text="색상(모양+RGB)", variable=self.mode_var, value="색상",
                         command=self._on_mode_change).pack(side="left", padx=4)

        # 색상 후보 목록 (색상 모드일 때만 표시) — 같은 모양에 색상만 여러 개
        # 붙일 수 있음(OR 매칭): "동일 패턴 + 다른 색상" 요구사항이 이 목록.
        self.color_frame = ttk.LabelFrame(
            body, text="색상 후보 (여러 개 추가하면 그중 하나만 맞아도 매치 — OR)", padding=8)
        color_add_row = ttk.Frame(self.color_frame)
        color_add_row.pack(fill="x")
        ttk.Label(color_add_row, text="HEX:").pack(side="left")
        self.color_hex_var = tk.StringVar(value="FF0000")
        ttk.Entry(color_add_row, textvariable=self.color_hex_var, width=8).pack(side="left", padx=4)
        self.color_swatch = tk.Canvas(color_add_row, width=22, height=22, bg="#FF0000", highlightthickness=1)
        self.color_swatch.pack(side="left", padx=4)
        ttk.Label(color_add_row, text="허용오차 ±").pack(side="left")
        self.color_tol_var = tk.IntVar(value=20)
        ttk.Entry(color_add_row, textvariable=self.color_tol_var, width=5).pack(side="left", padx=4)
        ttk.Button(color_add_row, text="ROI에서 색 추출", command=self.on_sample_color).pack(side="left", padx=4)
        ttk.Button(color_add_row, text="+ 후보 추가", command=self.on_add_color_candidate).pack(side="left", padx=4)
        self.color_hex_var.trace_add("write", lambda *_: self._update_color_swatch())

        self.color_listbox = tk.Listbox(self.color_frame, height=3)
        self.color_listbox.pack(fill="x", pady=4)
        ttk.Button(self.color_frame, text="선택 후보 삭제", command=self.on_remove_color_candidate).pack(anchor="w")
        # 초기 상태(흑백)에서는 숨김 — _on_mode_change가 pack/pack_forget으로 토글

        # comment + 생성
        gen_frame = ttk.Frame(body, padding=(0, 4))
        gen_frame.pack(fill="x")
        self._gen_frame = gen_frame  # 색상 후보 프레임을 이 위에 끼워넣기 위한 앵커
        ttk.Label(gen_frame, text="comment:").pack(side="left")
        self.comment_var = tk.StringVar(value="")
        ttk.Entry(gen_frame, textvariable=self.comment_var, width=16).pack(side="left", padx=6)
        self.cut_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(gen_frame, text="여백 자동 크롭", variable=self.cut_var).pack(side="left", padx=6)
        ttk.Button(gen_frame, text="패턴 생성", command=self.on_generate_pattern).pack(side="left", padx=6)
        ttk.Button(gen_frame, text="복사", command=self.on_copy_pattern).pack(side="left")
        self.category_var = tk.StringVar(value=PATTERN_CATEGORIES[0])
        ttk.Combobox(gen_frame, textvariable=self.category_var, values=PATTERN_CATEGORIES,
                     state="readonly", width=8).pack(side="left", padx=(12, 6))
        ttk.Button(gen_frame, text="저장", command=self.on_save_pattern).pack(side="left")

        # 패턴 문자열
        pat_frame = ttk.LabelFrame(body, text="패턴 문자열 (여기에 직접 붙여넣어도 됨)", padding=8)
        pat_frame.pack(fill="x", pady=6)
        self.pattern_text = tk.Text(pat_frame, height=4, wrap="char")
        self.pattern_text.pack(fill="x")

        # 패턴 목록 빌더 — 여러 패턴(모양이 다르든, 색상만 다르든)을 모아서
        # '|'로 결합해 한 번에 검색. "동일 패턴+다른색 멀티서치", "여러 흑백
        # 패턴 동시 검색" 둘 다 이 메커니즘 하나로 처리.
        list_frame = ttk.LabelFrame(
            body, text="패턴 목록 (모아서 한 번에 검색 — 모양/색상 섞어도 됨)", padding=8)
        list_frame.pack(fill="x", pady=6)
        list_btn_row = ttk.Frame(list_frame)
        list_btn_row.pack(fill="x")
        ttk.Button(list_btn_row, text="+ 현재 패턴을 목록에 추가", command=self.on_add_to_pattern_list).pack(side="left", padx=4)
        ttk.Button(list_btn_row, text="선택 삭제", command=self.on_remove_from_pattern_list).pack(side="left", padx=4)
        ttk.Button(list_btn_row, text="전체 삭제", command=self.on_clear_pattern_list).pack(side="left", padx=4)
        ttk.Button(list_btn_row, text="목록 결합 → 패턴창에 적용", command=self.on_combine_pattern_list).pack(side="left", padx=10)
        self.pattern_listbox = tk.Listbox(list_frame, height=4)
        self.pattern_listbox.pack(fill="x", pady=4)

        # 저장된 패턴 관리 (findtext_patterns.json 조회/불러오기/삭제)
        saved_frame = ttk.LabelFrame(body, text="저장된 패턴 (findtext_patterns.json)", padding=8)
        saved_frame.pack(fill="x", pady=6)
        saved_btn_row = ttk.Frame(saved_frame)
        saved_btn_row.pack(fill="x")
        ttk.Button(saved_btn_row, text="새로고침", command=self.on_refresh_saved_patterns).pack(side="left", padx=4)
        ttk.Button(saved_btn_row, text="선택 불러오기", command=self.on_load_saved_pattern).pack(side="left", padx=4)
        ttk.Button(saved_btn_row, text="선택 삭제", command=self.on_delete_saved_pattern).pack(side="left", padx=4)
        ttk.Button(saved_btn_row, text="Git 저장(commit+push)", command=self.on_git_sync_patterns).pack(side="left", padx=10)
        self.saved_listbox = tk.Listbox(saved_frame, height=8)
        self.saved_listbox.pack(fill="x", pady=4)
        self._saved_pattern_index: list = []  # listbox row -> (category, name)
        self.on_refresh_saved_patterns()

        # 매칭 테스트
        test_frame = ttk.LabelFrame(body, text="매칭 테스트", padding=8)
        test_frame.pack(fill="both", expand=True, pady=6)

        opt_row = ttk.Frame(test_frame)
        opt_row.pack(fill="x")
        ttk.Label(opt_row, text="err1:").pack(side="left")
        self.err1_var = tk.DoubleVar(value=0.1)
        ttk.Entry(opt_row, textvariable=self.err1_var, width=6).pack(side="left", padx=4)
        ttk.Label(opt_row, text="err0:").pack(side="left")
        self.err0_var = tk.DoubleVar(value=0.1)
        ttk.Entry(opt_row, textvariable=self.err0_var, width=6).pack(side="left", padx=4)
        ttk.Button(opt_row, text="현재 화면 전체에서 찾기", command=self.on_test_match_screen).pack(side="left", padx=10)
        ttk.Button(opt_row, text="로드된 이미지에서 찾기", command=self.on_test_match_source).pack(side="left")

        self.result_label = ttk.Label(test_frame, text="결과: -")
        self.result_label.pack(anchor="w", pady=(6, 2))
        self.canvas_result = tk.Canvas(test_frame, width=640, height=300, bg="#222")
        self.canvas_result.pack(fill="both", expand=True)

        self._on_mode_change()  # 색상 후보 프레임 초기 표시상태(흑백=숨김) 반영

    # ---------------------------------------------------------- 캡처/로드
    def on_capture_screen(self):
        self.root.withdraw()
        self.root.after(150, self._do_capture_screen)

    def _do_capture_screen(self):
        try:
            img = grab_full_screen_bgr()
        finally:
            self.root.deiconify()
        self.source_bgr = img
        region = pick_region(self.root, img)
        if region:
            self._set_roi_from_source(region)

    def on_open_image(self):
        path = filedialog.askopenfilename(
            title="이미지 파일 선택",
            filetypes=[("Image files", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")],
        )
        if not path:
            return
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            messagebox.showerror("오류", f"이미지를 열 수 없습니다: {path}")
            return
        self.source_bgr = img
        region = pick_region(self.root, img)
        if region:
            self._set_roi_from_source(region)

    def _set_roi_from_source(self, region):
        """새 영역을 선택했을 때: 조정값 초기화 + 자동 임계값 재산출."""
        self._orig_region = region
        self._adj = {"left": 0, "top": 0, "right": 0, "bottom": 0}
        x1, y1, x2, y2 = region
        self.roi_bgr = self.source_bgr[y1:y2, x1:x2].copy()
        self.roi_offset = (x1, y1)
        thr = self.ft.auto_threshold(self.roi_bgr)
        self.thr_var.set(thr)
        self.thr_label.config(text=str(thr))
        self._refresh_previews()

    def on_nudge(self, edge: str, delta: int):
        """선택 영역의 한 변을 1px 늘리거나(delta=-1) 줄인다(delta=1)."""
        if self._orig_region is None or self.source_bgr is None:
            return
        self._adj[edge] += delta
        x1, y1, x2, y2 = self._orig_region
        sh, sw = self.source_bgr.shape[:2]
        nx1 = x1 + self._adj["left"]
        ny1 = y1 + self._adj["top"]
        nx2 = x2 - self._adj["right"]
        ny2 = y2 - self._adj["bottom"]
        if nx1 < 0 or ny1 < 0 or nx2 > sw or ny2 > sh or nx2 - nx1 < 2 or ny2 - ny1 < 2:
            self._adj[edge] -= delta  # 화면 경계를 벗어나거나 너무 작아지면 되돌림
            return
        self.roi_bgr = self.source_bgr[ny1:ny2, nx1:nx2].copy()
        self.roi_offset = (nx1, ny1)
        self._refresh_previews()  # 임계값은 유지 (재자동산출 안 함)

    def on_reset_adjustment(self):
        if self._orig_region is not None:
            self._set_roi_from_source(self._orig_region)

    # ---------------------------------------------------------- 미리보기
    def _refresh_previews(self):
        if self.roi_bgr is None:
            return
        self._show_on_canvas(self.canvas_orig, cv2.cvtColor(self.roi_bgr, cv2.COLOR_BGR2RGB))
        self._refresh_binary_preview()

    def _refresh_binary_preview(self):
        if self.roi_bgr is None:
            return
        thr = int(self.thr_var.get())
        self.thr_label.config(text=str(thr))
        gray = (self.roi_bgr[:, :, 2].astype(np.int64) * 38
                + self.roi_bgr[:, :, 1].astype(np.int64) * 75
                + self.roi_bgr[:, :, 0].astype(np.int64) * 15)
        c = (thr + 1) << 7
        binary = (gray < c).astype(np.uint8) * 255
        rgb = np.stack([binary, binary, binary], axis=-1)
        self._show_on_canvas(self.canvas_bin, rgb)

    def _show_on_canvas(self, canvas: tk.Canvas, rgb_array: np.ndarray):
        cw, ch = int(canvas["width"]), int(canvas["height"])
        h, w = rgb_array.shape[:2]
        scale = min(cw / max(w, 1), ch / max(h, 1), 8.0)
        scale = max(scale, 0.05)
        disp = cv2.resize(rgb_array, (max(1, int(w * scale)), max(1, int(h * scale))),
                           interpolation=cv2.INTER_NEAREST)
        photo = ImageTk.PhotoImage(Image.fromarray(disp))
        canvas.delete("all")
        canvas.image = photo  # 참조 유지 (GC 방지)
        canvas.create_image(cw // 2, ch // 2, image=photo, anchor="center")

    # ---------------------------------------------------------- 패턴 생성
    def on_auto_threshold(self):
        if self.roi_bgr is None:
            messagebox.showwarning("안내", "먼저 영역을 캡처/선택하세요")
            return
        thr = self.ft.auto_threshold(self.roi_bgr)
        self.thr_var.set(thr)
        self._refresh_binary_preview()

    def on_generate_pattern(self):
        if self.roi_bgr is None:
            messagebox.showwarning("안내", "먼저 영역을 캡처/선택하세요")
            return
        thr = int(self.thr_var.get())
        comment = self.comment_var.get().strip()

        if self.mode_var.get() == "색상":
            if not self.color_candidates:
                messagebox.showwarning("안내", "색상 후보를 최소 1개 추가하세요 ('+ 후보 추가')")
                return
            pattern = self.ft.capture_pattern_color(
                self.roi_bgr, colors=list(self.color_candidates),
                threshold=thr, comment=comment, cut=self.cut_var.get())
        else:
            pattern = self.ft.capture_pattern(self.roi_bgr, threshold=thr, comment=comment,
                                               cut=self.cut_var.get())

        self.pattern_text.delete("1.0", "end")
        self.pattern_text.insert("1.0", pattern)

    def on_copy_pattern(self):
        text = self.pattern_text.get("1.0", "end").strip()
        if not text:
            return
        if pyperclip:
            pyperclip.copy(text)
            self.result_label.config(text="패턴을 클립보드에 복사했습니다")
        else:
            messagebox.showinfo("안내", "pyperclip이 없어 자동 복사는 안 되지만, 텍스트 상자에서 직접 복사하세요")

    def on_save_pattern(self):
        """패턴창의 내용을 findtext_patterns.json에 저장.
        이름은 별도로 받지 않고 패턴에 이미 박혀있는 <comment>를 그대로 키로
        쓴다 — 이름을 따로 받으면 comment와 어긋나서 find_text() 결과의 id와
        저장된 이름이 달라지는 버그가 생기기 쉽다."""
        text = self.pattern_text.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("안내", "저장할 패턴이 없습니다")
            return
        parsed = self.ft._parse_text_pattern(text)
        first = parsed[0] if isinstance(parsed, list) else parsed
        name = (first.get("comment") or "").strip() if first else ""
        if not name:
            messagebox.showwarning("안내", "comment를 입력하고 '패턴 생성'을 눌러 저장 이름을 지정하세요")
            return
        category = self.category_var.get()

        data = {}
        if os.path.exists(PATTERNS_FILE):
            with open(PATTERNS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        data.setdefault(category, {})[name] = text
        with open(PATTERNS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        self.result_label.config(text=f"저장됨: [{category}] '{name}' → {os.path.basename(PATTERNS_FILE)}")
        self.on_refresh_saved_patterns()

    def _load_patterns_file(self) -> dict:
        if not os.path.exists(PATTERNS_FILE):
            return {}
        with open(PATTERNS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    def on_refresh_saved_patterns(self):
        self.saved_listbox.delete(0, "end")
        self._saved_pattern_index = []
        data = self._load_patterns_file()
        for category in PATTERN_CATEGORIES:
            for name in sorted(data.get(category, {}).keys()):
                self.saved_listbox.insert("end", f"[{category}] {name}")
                self._saved_pattern_index.append((category, name))

    def _get_selected_saved(self):
        sel = self.saved_listbox.curselection()
        if not sel:
            messagebox.showinfo("안내", "목록에서 패턴을 선택하세요")
            return None
        return self._saved_pattern_index[sel[0]]

    def on_load_saved_pattern(self):
        picked = self._get_selected_saved()
        if not picked:
            return
        category, name = picked
        text = self._load_patterns_file().get(category, {}).get(name, "")
        self.pattern_text.delete("1.0", "end")
        self.pattern_text.insert("1.0", text)
        self.category_var.set(category)
        self.comment_var.set(name)
        self.result_label.config(text=f"불러옴: [{category}] '{name}' (아래 '매칭 테스트'로 바로 확인 가능)")

    def on_delete_saved_pattern(self):
        picked = self._get_selected_saved()
        if not picked:
            return
        category, name = picked
        if not messagebox.askyesno("확인", f"[{category}] '{name}' 패턴을 삭제할까요?"):
            return
        data = self._load_patterns_file()
        if category in data and name in data[category]:
            del data[category][name]
            with open(PATTERNS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        self.on_refresh_saved_patterns()
        self.result_label.config(text=f"삭제됨: [{category}] '{name}'")

    def on_git_sync_patterns(self):
        """findtext_patterns.json만 골라서 commit + push.
        여러 PC가 각자 패턴을 추가하는 구조라 pull(rebase)로 먼저 남의 패턴을
        받아온 뒤 올려야 서로 덮어쓰지 않는다."""
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain", "findtext_patterns.json"],
                cwd=SCRIPT_DIR, capture_output=True, text=True, check=True,
            )
            if not status.stdout.strip():
                self.result_label.config(text="Git: 패턴 변경사항 없음 (이미 최신)")
                return
            subprocess.run(["git", "add", "findtext_patterns.json"], cwd=SCRIPT_DIR, check=True)
            subprocess.run(["git", "commit", "-m", "Update findtext patterns"], cwd=SCRIPT_DIR, check=True)
            subprocess.run(["git", "pull", "--rebase"], cwd=SCRIPT_DIR, check=True)
            subprocess.run(["git", "push"], cwd=SCRIPT_DIR, check=True)
        except subprocess.CalledProcessError as e:
            messagebox.showerror("Git 오류", f"패턴 저장(git) 실패:\n{e}\n\n직접 git으로 충돌을 해결해야 할 수 있습니다.")
            return
        self.result_label.config(text="Git: 패턴 commit + push 완료")

    # ---------------------------------------------------------- 매칭 모드 / 색상 후보
    def _on_mode_change(self):
        if self.mode_var.get() == "색상":
            self.color_frame.pack(fill="x", pady=(0, 4), before=self._gen_frame)
        else:
            self.color_frame.pack_forget()

    def _update_color_swatch(self):
        hexcode = self.color_hex_var.get().strip().lstrip("#")
        if len(hexcode) == 6:
            try:
                int(hexcode, 16)
                self.color_swatch.config(bg=f"#{hexcode}")
            except ValueError:
                pass

    def on_sample_color(self):
        if self.roi_bgr is None:
            messagebox.showwarning("안내", "먼저 영역을 캡처/선택하세요")
            return
        r, g, b = self.ft.sample_ink_color(self.roi_bgr, threshold=int(self.thr_var.get()))
        self.color_hex_var.set(f"{r:02X}{g:02X}{b:02X}")

    def on_add_color_candidate(self):
        hexcode = self.color_hex_var.get().strip().lstrip("#")
        if len(hexcode) != 6:
            messagebox.showerror("오류", "HEX는 RRGGBB 6자리로 입력하세요 (예: FF3B30)")
            return
        try:
            r, g, b = int(hexcode[0:2], 16), int(hexcode[2:4], 16), int(hexcode[4:6], 16)
            tol = int(self.color_tol_var.get())
        except ValueError:
            messagebox.showerror("오류", "색상 또는 허용오차 값이 올바르지 않습니다")
            return
        self.color_candidates.append((r, g, b, tol))
        self.color_listbox.insert("end", f"#{hexcode.upper()}  ±{tol}")

    def on_remove_color_candidate(self):
        sel = self.color_listbox.curselection()
        for i in reversed(sel):
            self.color_listbox.delete(i)
            del self.color_candidates[i]

    # ---------------------------------------------------------- 패턴 목록 빌더
    def _summarize_pattern(self, text: str) -> str:
        import re
        m = re.match(r'\|?<([^>]*)>([@*#]{1,2}?)', text)
        tag = m.group(1) if m else "?"
        marker = m.group(2) if m else ""
        kind = "색상" if marker.startswith("@") else "흑백"
        return f"[{kind}] {tag or '(무명)'}"

    def on_add_to_pattern_list(self):
        text = self.pattern_text.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("안내", "먼저 패턴을 생성하세요")
            return
        self.pattern_list.append(text)
        self.pattern_listbox.insert("end", self._summarize_pattern(text))

    def on_remove_from_pattern_list(self):
        sel = self.pattern_listbox.curselection()
        for i in reversed(sel):
            self.pattern_listbox.delete(i)
            del self.pattern_list[i]

    def on_clear_pattern_list(self):
        self.pattern_listbox.delete(0, "end")
        self.pattern_list.clear()

    def on_combine_pattern_list(self):
        if not self.pattern_list:
            messagebox.showwarning("안내", "목록이 비어 있습니다")
            return
        # 각 패턴이 이미 자기 앞에 '|'를 달고 있으므로 그냥 이어붙이면
        # ft.ahk의 다중 패턴 결합(Text.="|<comment>...")과 동일한 결과가 된다.
        combined = "".join(self.pattern_list)
        self.pattern_text.delete("1.0", "end")
        self.pattern_text.insert("1.0", combined)

    # ---------------------------------------------------------- 매칭 테스트
    def on_test_match_screen(self):
        self.root.withdraw()
        self.root.after(150, lambda: self._run_match(grab_full_screen_bgr(), live=True))

    def on_test_match_source(self):
        if self.source_bgr is None:
            messagebox.showwarning("안내", "먼저 화면 캡처 또는 이미지 파일을 여세요")
            return
        self._run_match(self.source_bgr, live=False)

    def _run_match(self, target_bgr: np.ndarray, live: bool):
        self.root.deiconify()
        pattern = self.pattern_text.get("1.0", "end").strip()
        if not pattern:
            messagebox.showwarning("안내", "패턴 문자열이 없습니다 (먼저 패턴 생성)")
            return
        try:
            err1 = float(self.err1_var.get())
            err0 = float(self.err0_var.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("오류", "err1/err0 값이 올바르지 않습니다")
            return

        results = self.ft.find_text(pattern, 0, 0, 0, 0, err1, err0,
                                     screenshot=target_bgr, find_all=True)
        self.result_label.config(text=f"결과: {len(results)}개 매치")

        if live:
            # ft.ahk 방식: 실제 화면 위에 빨간 박스를 직접 그려서 보여줌
            self.show_screen_overlay(results)
        else:
            # 이미지 파일 테스트: 화면에 없는 이미지이므로 앱 내 미리보기로 표시
            vis = target_bgr.copy()
            for r in results:
                x, y, w, h = r["x"], r["y"], r["w"], r["h"]
                cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 0, 255), 2)
                if r.get("id"):
                    cv2.putText(vis, r["id"], (x, max(0, y - 4)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)
            self._show_on_canvas(self.canvas_result, cv2.cvtColor(vis, cv2.COLOR_BGR2RGB))

    def show_screen_overlay(self, boxes, hold_ms: int = 4000):
        """
        ft.ahk 캡처 도구처럼 매칭된 위치에 실제 화면 위에 빨간 박스를 직접
        그려서 보여준다 (반투명 풀스크린 오버레이, 클릭하거나 hold_ms 후 자동 닫힘).
        """
        ox, oy, ow, oh = get_virtual_screen_rect()

        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        try:
            top.attributes("-transparentcolor", OVERLAY_TRANSPARENT_KEY)
        except tk.TclError:
            pass  # Windows 외 환경: 완전 투명은 안 되지만 박스는 그대로 보임
        top.geometry(f"{ow}x{oh}+{ox}+{oy}")

        canvas = tk.Canvas(top, width=ow, height=oh, bg=OVERLAY_TRANSPARENT_KEY, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        pad = 4  # 매칭 영역에 딱 붙지 않도록 시각적 여백만 추가 (실제 좌표엔 영향 없음)
        for r in boxes:
            x, y, w, h = r["x"], r["y"], r["w"], r["h"]
            canvas.create_rectangle(x - pad, y - pad, x + w + pad, y + h + pad,
                                     outline="red", width=3)
            if r.get("id"):
                # 패턴 목록을 결합해서 여러 항목을 동시에 찾을 때, 어떤 항목이
                # 매치됐는지 박스 위에 태그로 표시 (색상/모양별 구분용)
                canvas.create_text(x - pad, y - pad - 4, anchor="sw", fill="#00FF88",
                                    font=("맑은 고딕", 10, "bold"), text=r["id"])
        canvas.create_text(24, 24, anchor="nw", fill="yellow",
                            font=("맑은 고딕", 14, "bold"),
                            text=f"{len(boxes)}개 매치 · 클릭하거나 Esc로 닫기")

        def close(_ev=None):
            if top.winfo_exists():
                top.destroy()

        canvas.bind("<Button-1>", close)
        top.bind("<Escape>", close)
        top.after(hold_ms, close)
        top.focus_force()


def main():
    root = tk.Tk()
    FindTextGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
