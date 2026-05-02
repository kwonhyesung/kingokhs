"""
FindText 파이썬 클론
ft.ahk (AutoHotkey FindText 라이브러리)의 파이썬 구현

주요 기능:
1. 화면 캡쳐 (win32api)
2. 이미지를 0/1 비트맵으로 변환
3. 비트맵 문자열로 패턴 찾기
4. CustomTkinter GUI
"""

import win32gui
import win32api
import win32con
import win32ui
import ctypes
import numpy as np
from PIL import Image, ImageTk
from typing import Optional, List, Dict, Tuple, Union
import time
import customtkinter as ctk
from tkinter import messagebox


class FindText:
    """ft.ahk FindText 라이브러리의 파이썬 클론"""
    
    def __init__(self):
        self.last_capture = None
        self.last_capture_time = 0
        
    def capture_screen(self, x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0) -> Optional[Image.Image]:
        """
        화면 캡쳐 (win32api 기반)
        
        Args:
            x1, y1: 캡쳐 영역 좌상단 (0,0이면 전체 화면)
            x2, y2: 캡쳐 영역 우하단 (0,0이면 전체 화면)
        
        Returns:
            PIL Image 객체
        """
        try:
            # 전체 화면 크기 가져오기
            if x1 == 0 and y1 == 0 and x2 == 0 and y2 == 0:
                x1, y1 = 0, 0
                x2, y2 = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
            
            width = x2 - x1
            height = y2 - y1
            
            # 화면 DC 생성
            hwndDC = win32gui.GetWindowDC(0)
            mfcDC = win32ui.CreateDCFromHandle(hwndDC)
            saveDC = mfcDC.CreateCompatibleDC()
            saveBitMap = win32ui.CreateBitmap()
            saveBitMap.CreateCompatibleBitmap(mfcDC, width, height)
            saveDC.SelectObject(saveBitMap)
            
            # 화면 캡쳐
            result = ctypes.windll.user32.PrintWindow(0, saveDC.GetSafeHdc(), 0)
            if result == 0:
                # PrintWindow 실패 시 BitBlt로 폴백
                saveDC.BitBlt((0, 0), (width, height), mfcDC, (x1, y1), win32con.SRCCOPY)
            
            # 비트맵 데이터 읽기
            bmpinfo = saveBitMap.GetInfo()
            bmpstr = saveBitMap.GetBitmapBits(True)
            img = Image.frombuffer('RGB',
                                   (bmpinfo['bmWidth'], bmpinfo['bmHeight']),
                                   bmpstr, 'raw', 'BGRX', 0, 1)
            
            # 리소스 정리
            mfcDC.DeleteDC()
            saveDC.DeleteDC()
            win32gui.ReleaseDC(0, hwndDC)
            win32gui.DeleteObject(saveBitMap.GetHandle())
            
            self.last_capture = img
            self.last_capture_time = time.time()
            return img
            
        except Exception as e:
            print(f"[FindText] 캡쳐 실패: {e}")
            return None
    
    def image_to_binary_string(self, img: Image.Image, threshold: int = 128) -> str:
        """
        이미지를 0/1 비트맵 문자열로 변환 (ft.ahk FindText 방식)
        
        Args:
            img: PIL Image 객체
            threshold: 이진화 임계값 (기본값: 128)
        
        Returns:
            0/1 비트맵 문자열
        """
        # 그레이스케일 변환
        gray = img.convert('L')
        # 이진화
        binary = gray.point(lambda x: 0 if x < threshold else 1, '1')
        # 문자열로 변환
        width, height = binary.size
        binary_str = ""
        for y in range(height):
            for x in range(width):
                binary_str += str(binary.getpixel((x, y)))
            binary_str += "\n"
        return binary_str
    
    def binary_string_to_image(self, binary_str: str) -> Image.Image:
        """
        0/1 비트맵 문자열을 이미지로 변환
        
        Args:
            binary_str: 0/1 비트맵 문자열
        
        Returns:
            PIL Image 객체
        """
        lines = binary_str.strip().split('\n')
        height = len(lines)
        width = len(lines[0]) if lines else 0
        
        # numpy 배열 생성
        arr = np.zeros((height, width), dtype=np.uint8)
        for y, line in enumerate(lines):
            for x, char in enumerate(line):
                if char == '1':
                    arr[y, x] = 255
        
        return Image.fromarray(arr, mode='L')
    
    def find_text(self, 
                  text: str,
                  x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0,
                  err1: float = 0.1, err0: float = 0.1,
                  screenshot: bool = True,
                  find_all: bool = True) -> List[Dict]:
        """
        비트맵 문자열로 패턴 찾기 (FindText 모드)
        
        Args:
            text: 0/1 비트맵 문자열
            x1, y1, x2, y2: 검색 영역
            err1: 텍스트 오차 허용율 (0.1 = 10%)
            err0: 배경 오차 허용율 (0.1 = 10%)
            screenshot: 화면 캡쳐 여부
            find_all: 모든 결과 찾기 여부
        
        Returns:
            결과 리스트 [{x, y, w, h, id}, ...]
        """
        # 화면 캡쳐
        if screenshot:
            screen_img = self.capture_screen(x1, y1, x2, y2)
        else:
            screen_img = self.last_capture
        
        if screen_img is None:
            return []
        
        # 비트맵 문자열을 이미지로 변환
        pattern_img = self.binary_string_to_image(text)
        pattern_w, pattern_h = pattern_img.size
        screen_w, screen_h = screen_img.size
        
        # 화면 이미지를 그레이스케일로 변환
        screen_gray = screen_img.convert('L')
        
        # 패턴 매칭
        results = []
        threshold = int(255 * (1 - err1))
        
        # 슬라이딩 윈도우 방식으로 패턴 찾기
        for y in range(screen_h - pattern_h + 1):
            for x in range(screen_w - pattern_w + 1):
                # 현재 영역 추출
                crop = screen_gray.crop((x, y, x + pattern_w, y + pattern_h))
                crop_arr = np.array(crop)
                pattern_arr = np.array(pattern_img)
                
                # 일치율 계산
                diff = np.abs(crop_arr.astype(np.int16) - pattern_arr.astype(np.int16))
                match_count = np.sum(diff < threshold)
                total_pixels = pattern_w * pattern_h
                match_ratio = match_count / total_pixels
                
                if match_ratio >= (1 - err1):
                    results.append({
                        'x': x + x1,
                        'y': y + y1,
                        'w': pattern_w,
                        'h': pattern_h,
                        'score': match_ratio
                    })
                    
                    if not find_all:
                        return results
        
        return results
    
    def find_color(self,
                   color: str,
                   x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0,
                   variation: int = 10,
                   screenshot: bool = True) -> List[Dict]:
        """
        특정 색상 찾기 (FindColor 모드)
        
        Args:
            color: RGB 색상 (예: "FF0000" 또는 "Red")
            x1, y1, x2, y2: 검색 영역
            variation: 색상 변동 허용 범위
            screenshot: 화면 캡쳐 여부
        
        Returns:
            결과 리스트 [{x, y}, ...]
        """
        # 화면 캡쳐
        if screenshot:
            screen_img = self.capture_screen(x1, y1, x2, y2)
        else:
            screen_img = self.last_capture
        
        if screen_img is None:
            return []
        
        # 색상 파싱
        target_color = self._parse_color(color)
        if target_color is None:
            return []
        
        # 픽셀 단위로 색상 검색
        screen_arr = np.array(screen_img)
        results = []
        
        for y in range(screen_arr.shape[0]):
            for x in range(screen_arr.shape[1]):
                pixel = screen_arr[y, x]
                if self._color_match(pixel, target_color, variation):
                    results.append({
                        'x': x + x1,
                        'y': y + y1
                    })
        
        return results
    
    def _parse_color(self, color: str) -> Optional[Tuple[int, int, int]]:
        """색상 문자열 파싱"""
        color_map = {
            'Black': (0, 0, 0),
            'White': (255, 255, 255),
            'Red': (255, 0, 0),
            'Green': (0, 255, 0),
            'Blue': (0, 0, 255),
            'Yellow': (255, 255, 0)
        }
        
        if color in color_map:
            return color_map[color]
        
        # 16진수 RGB 파싱
        if len(color) == 6:
            try:
                r = int(color[0:2], 16)
                g = int(color[2:4], 16)
                b = int(color[4:6], 16)
                return (r, g, b)
            except ValueError:
                pass
        
        return None
    
    def _color_match(self, pixel: Tuple[int, int, int], 
                    target: Tuple[int, int, int], variation: int) -> bool:
        """색상 일치 여부 확인"""
        return (abs(pixel[0] - target[0]) <= variation and
                abs(pixel[1] - target[1]) <= variation and
                abs(pixel[2] - target[2]) <= variation)
    
    def find_pic(self,
                 image_path: str,
                 x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0,
                 variation: int = 10,
                 screenshot: bool = True) -> List[Dict]:
        """
        이미지 파일로 패턴 찾기 (FindPic 모드)
        
        Args:
            image_path: 이미지 파일 경로
            x1, y1, x2, y2: 검색 영역
            variation: 색상 변동 허용 범위
            screenshot: 화면 캡쳐 여부
        
        Returns:
            결과 리스트 [{x, y, w, h, score}, ...]
        """
        try:
            # 패턴 이미지 로드
            pattern_img = Image.open(image_path).convert('RGB')
        except Exception as e:
            print(f"[FindText] 이미지 로드 실패: {e}")
            return []
        
        # 화면 캡쳐
        if screenshot:
            screen_img = self.capture_screen(x1, y1, x2, y2)
        else:
            screen_img = self.last_capture
        
        if screen_img is None:
            return []
        
        # 이미지 매칭 (템플릿 매칭)
        return self._template_match(screen_img, pattern_img, variation)
    
    def find_shape(self,
                  shape_str: str,
                  x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0,
                  variation: int = 10,
                  screenshot: bool = True) -> List[Dict]:
        """
        모양 찾기 (FindShape 모드)
        
        Args:
            shape_str: 모양 정의 문자열
            x1, y1, x2, y2: 검색 영역
            variation: 색상 변동 허용 범위
            screenshot: 화면 캡쳐 여부
        
        Returns:
            결과 리스트 [{x, y, w, h, score}, ...]
        """
        # 화면 캡쳐
        if screenshot:
            screen_img = self.capture_screen(x1, y1, x2, y2)
        else:
            screen_img = self.last_capture
        
        if screen_img is None:
            return []
        
        # 모양 문자열 파싱 (ft.ahk 형식)
        shape_points = self._parse_shape_string(shape_str)
        if not shape_points:
            return []
        
        # 모양 매칭
        results = []
        screen_arr = np.array(screen_img)
        
        # 각 위치에서 모양 검사
        for y in range(screen_arr.shape[0] - 10):
            for x in range(screen_arr.shape[1] - 10):
                if self._match_shape_at(screen_arr, x, y, shape_points, variation):
                    results.append({
                        'x': x + x1,
                        'y': y + y1,
                        'w': 10,
                        'h': 10,
                        'score': 1.0
                    })
        
        return results
    
    def _template_match(self, screen_img: Image.Image, pattern_img: Image.Image, variation: int) -> List[Dict]:
        """템플릿 매칭 (numpy 활용)"""
        screen_arr = np.array(screen_img)
        pattern_arr = np.array(pattern_img)
        
        pattern_h, pattern_w = pattern_arr.shape[:2]
        screen_h, screen_w = screen_arr.shape[:2]
        
        results = []
        
        # 슬라이딩 윈도우 방식으로 매칭
        for y in range(screen_h - pattern_h + 1):
            for x in range(screen_w - pattern_w + 1):
                # 현재 영역 추출
                crop = screen_arr[y:y+pattern_h, x:x+pattern_w]
                
                # 색상 차이 계산
                diff = np.abs(crop.astype(np.int16) - pattern_arr.astype(np.int16))
                match_pixels = np.sum(np.all(diff <= variation, axis=2))
                total_pixels = pattern_w * pattern_h
                
                match_ratio = match_pixels / total_pixels
                
                if match_ratio >= 0.8:  # 80% 이상 일치
                    results.append({
                        'x': x,
                        'y': y,
                        'w': pattern_w,
                        'h': pattern_h,
                        'score': match_ratio
                    })
        
        return results
    
    def _parse_shape_string(self, shape_str: str) -> List[Tuple[int, int, int]]:
        """모양 문자열 파싱"""
        # ft.ahk 형식: "0/0/1, x1/y1/0, x2/y2/1, ..."
        points = []
        parts = shape_str.split(',')
        
        for part in parts:
            coords = part.strip().split('/')
            if len(coords) >= 3:
                try:
                    x = int(coords[0])
                    y = int(coords[1])
                    is_match = int(coords[2])
                    points.append((x, y, is_match))
                except ValueError:
                    pass
        
        return points
    
    def _match_shape_at(self, screen_arr: np.ndarray, x: int, y: int, 
                       points: List[Tuple[int, int, int]], variation: int) -> bool:
        """특정 위치에서 모양 일치 여부 확인"""
        base_color = screen_arr[y, x]
        
        for px, py, is_match in points:
            target_x = x + px
            target_y = y + py
            
            if target_y >= screen_arr.shape[0] or target_x >= screen_arr.shape[1]:
                return False
            
            pixel_color = screen_arr[target_y, target_x]
            
            if is_match == 1:
                # 색상이 비슷해야 함
                if not self._color_match(pixel_color, base_color, variation):
                    return False
            else:
                # 색상이 달라야 함
                if self._color_match(pixel_color, base_color, variation):
                    return False
        
        return True
    
    def find_multi_color(self,
                       color_str: str,
                       x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0,
                       variation: int = 10,
                       screenshot: bool = True) -> List[Dict]:
        """
        다중 색상 찾기 (FindMultiColor 모드)
        
        Args:
            color_str: 색상 정의 문자열 (ft.ahk 형식: "##variation $ 0/0/RRGGBB, x1/y1/-RRGGBB1/RRGGBB2, ...")
            x1, y1, x2, y2: 검색 영역
            variation: 색상 변동 허용 범위
            screenshot: 화면 캡쳐 여부
        
        Returns:
            결과 리스트 [{x, y}, ...]
        """
        # 화면 캡쳐
        if screenshot:
            screen_img = self.capture_screen(x1, y1, x2, y2)
        else:
            screen_img = self.last_capture
        
        if screen_img is None:
            return []
        
        # 색상 문자열 파싱
        color_points = self._parse_multi_color_string(color_str, variation)
        if not color_points:
            return []
        
        # 다중 색상 매칭
        results = []
        screen_arr = np.array(screen_img)
        
        # 각 위치에서 색상 패턴 검사
        for y in range(screen_arr.shape[0] - 10):
            for x in range(screen_arr.shape[1] - 10):
                if self._match_multi_color_at(screen_arr, x, y, color_points):
                    results.append({
                        'x': x + x1,
                        'y': y + y1
                    })
        
        return results
    
    def _parse_multi_color_string(self, color_str: str, default_variation: int) -> List[Dict]:
        """다중 색상 문자열 파싱"""
        # ft.ahk 형식: "##variation $ 0/0/RRGGBB1-RRGGBB2, x1/y1/-RRGGBB3/RRGGBB4, ..."
        points = []
        
        try:
            # 기본 변동값 추출
            if '##' in color_str:
                parts = color_str.split('##')
                if len(parts) > 1:
                    try:
                        default_variation = int(parts[1].split()[0])
                    except ValueError:
                        pass
            
            # 색상 포인트 추출
            if '$' in color_str:
                color_str = color_str.split('$')[1]
            
            segments = color_str.split(',')
            
            for segment in segments:
                segment = segment.strip()
                if not segment:
                    continue
                
                coords = segment.split('/')
                if len(coords) >= 3:
                    try:
                        x = int(coords[0])
                        y = int(coords[1])
                        colors = coords[2].split('-')
                        
                        include_colors = []
                        exclude_colors = []
                        
                        for color in colors:
                            if color.startswith('-'):
                                exclude_color = self._parse_color(color[1:])
                                if exclude_color:
                                    exclude_colors.append(exclude_color)
                            else:
                                include_color = self._parse_color(color)
                                if include_color:
                                    include_colors.append(include_color)
                        
                        if include_colors:
                            points.append({
                                'x': x,
                                'y': y,
                                'include': include_colors,
                                'exclude': exclude_colors,
                                'variation': default_variation
                            })
                    except ValueError:
                        pass
        except Exception as e:
            print(f"[FindText] 다중 색상 문자열 파싱 실패: {e}")
        
        return points
    
    def _match_multi_color_at(self, screen_arr: np.ndarray, x: int, y: int, 
                             color_points: List[Dict]) -> bool:
        """특정 위치에서 다중 색상 패턴 일치 여부 확인"""
        for point in color_points:
            target_x = x + point['x']
            target_y = y + point['y']
            
            if target_y >= screen_arr.shape[0] or target_x >= screen_arr.shape[1]:
                return False
            
            pixel_color = screen_arr[target_y, target_x]
            
            # 포함 색상 확인 (적어도 하나와 일치해야 함)
            if point['include']:
                match_found = False
                for include_color in point['include']:
                    if self._color_match(pixel_color, include_color, point['variation']):
                        match_found = True
                        break
                if not match_found:
                    return False
            
            # 제외 색상 확인 (모든 제외 색상과 일치하면 안 됨)
            for exclude_color in point['exclude']:
                if self._color_match(pixel_color, exclude_color, point['variation']):
                    return False
        
        return True


class FindTextGUI:
    """FindText GUI 클래스 (CustomTkinter 기반)"""
    
    def __init__(self, find_text: FindText):
        self.find_text = find_text
        self.root = None
        self.sample_img = None
        self.original_img = None  # 원본 이미지 저장
        self.sample_binary = None
        self.tk_img = None
        self.selection_start = None
        self.selection_end = None
        self.selection_rect = None
        
    def show(self):
        """GUI 표시"""
        # CustomTkinter 설정
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        
        self.root = ctk.CTk()
        self.root.title("FindText 파이썬 클론")
        self.root.geometry("1000x800")
        self.root.attributes("-topmost", True)  # 최상단 표시
        
        # 메인 프레임
        main_frame = ctk.CTkFrame(self.root)
        main_frame.pack(fill="both", expand=True, padx=20, pady=20)
        
        # 제목
        title_label = ctk.CTkLabel(main_frame, text="FindText 파이썬 클론", 
                                   font=ctk.CTkFont(size=24, weight="bold"))
        title_label.pack(pady=(0, 20))
        
        # 버튼 프레임
        button_frame = ctk.CTkFrame(main_frame)
        button_frame.pack(fill="x", pady=(0, 20))
        
        # 캡쳐 버튼
        capture_btn = ctk.CTkButton(button_frame, text="화면 캡쳐", command=self.capture_and_show,
                                   fg_color="#2E8B57", hover_color="#3CB371",
                                   font=ctk.CTkFont(size=14, weight="bold"))
        capture_btn.pack(side="left", padx=(0, 10), expand=True, fill="x")
        
        # 영역 선택 버튼
        select_btn = ctk.CTkButton(button_frame, text="영역 선택", command=self.select_region,
                                  fg_color="#1E90FF", hover_color="#4169E1",
                                  font=ctk.CTkFont(size=14, weight="bold"))
        select_btn.pack(side="left", padx=(0, 10), expand=True, fill="x")
        
        # 저장 버튼
        save_btn = ctk.CTkButton(button_frame, text="비트맵 저장", command=self.save_binary_string,
                               fg_color="#FF8C00", hover_color="#FFA500",
                               font=ctk.CTkFont(size=14, weight="bold"))
        save_btn.pack(side="left", padx=(0, 10), expand=True, fill="x")
        
        # 테스트 버튼
        test_btn = ctk.CTkButton(button_frame, text="패턴 찾기", command=self.test_find,
                               fg_color="#8A2BE2", hover_color="#9370DB",
                               font=ctk.CTkFont(size=14, weight="bold"))
        test_btn.pack(side="left", expand=True, fill="x")
        
        # 정밀 조절 프레임
        adjust_frame = ctk.CTkFrame(main_frame)
        adjust_frame.pack(fill="x", pady=(0, 20))
        
        adjust_label = ctk.CTkLabel(adjust_frame, text="1px 정밀 조절:",
                                    font=ctk.CTkFont(size=12, weight="bold"))
        adjust_label.pack(side="left", padx=(0, 10))
        
        # 상하좌우 조절 버튼
        ctk.CTkButton(adjust_frame, text="▲", width=40, command=lambda: self.adjust_crop(0, -1),
                     fg_color="#4169E1", hover_color="#6495ED").pack(side="left", padx=2)
        ctk.CTkButton(adjust_frame, text="▼", width=40, command=lambda: self.adjust_crop(0, 1),
                     fg_color="#4169E1", hover_color="#6495ED").pack(side="left", padx=2)
        ctk.CTkButton(adjust_frame, text="◀", width=40, command=lambda: self.adjust_crop(-1, 0),
                     fg_color="#4169E1", hover_color="#6495ED").pack(side="left", padx=2)
        ctk.CTkButton(adjust_frame, text="▶", width=40, command=lambda: self.adjust_crop(1, 0),
                     fg_color="#4169E1", hover_color="#6495ED").pack(side="left", padx=2)
        
        # 리셋 버튼
        ctk.CTkButton(adjust_frame, text="리셋", width=60, command=self.reset_crop,
                     fg_color="#DC143C", hover_color="#FF6347").pack(side="left", padx=(10, 0))
        
        # 캔버스 프레임
        canvas_frame = ctk.CTkFrame(main_frame)
        canvas_frame.pack(fill="both", expand=True, pady=(0, 20))
        
        # 캔버스 (미리보기)
        self.canvas = ctk.CTkCanvas(canvas_frame, bg="gray", width=960, height=500)
        self.canvas.pack(fill="both", expand=True, padx=10, pady=10)
        
        # 비트맵 문자열 표시
        text_frame = ctk.CTkFrame(main_frame)
        text_frame.pack(fill="both", expand=True)
        
        text_label = ctk.CTkLabel(text_frame, text="비트맵 문자열:", 
                                 font=ctk.CTkFont(size=12, weight="bold"))
        text_label.pack(anchor="w", padx=10, pady=(10, 5))
        
        self.text_area = ctk.CTkTextbox(text_frame, height=100)
        self.text_area.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        
        self.root.mainloop()
    
    def capture_and_show(self):
        """화면 캡쳐 및 표시"""
        img = self.find_text.capture_screen()
        if img:
            self.sample_img = img
            self.original_img = img  # 원본 이미지 저장
            self.sample_binary = self.find_text.image_to_binary_string(img)
            self.show_image(img)
            self.text_area.delete("0.0", "end")
            self.text_area.insert("0.0", self.sample_binary[:500] + "...")
            print("[FindTextGUI] 캡쳐 완료")
        else:
            messagebox.showerror("오류", "화면 캡쳐 실패")
    
    def show_image(self, img):
        """이미지 캔버스에 표시"""
        try:
            # 캔버스 크기에 맞게 리사이즈
            canvas_width = self.canvas.winfo_width()
            canvas_height = self.canvas.winfo_height()
            if canvas_width < 100:
                canvas_width = 960
            if canvas_height < 100:
                canvas_height = 500
            
            img_ratio = img.width / img.height
            canvas_ratio = canvas_width / canvas_height
            
            if img_ratio > canvas_ratio:
                new_width = canvas_width
                new_height = int(canvas_width / img_ratio)
            else:
                new_height = canvas_height
                new_width = int(canvas_height * img_ratio)
            
            resized = img.resize((new_width, new_height))
            self.tk_img = ImageTk.PhotoImage(resized)
            
            self.canvas.delete("all")
            self.canvas.create_image(canvas_width//2, canvas_height//2, 
                                    image=self.tk_img, anchor="center")
        except Exception as e:
            print(f"[FindTextGUI] 이미지 표시 실패: {e}")
    
    def select_region(self):
        """영역 선택 (샘플링)"""
        # 먼저 화면 캡쳐
        img = self.find_text.capture_screen()
        if not img:
            ctk.CTkMessageBox.showerror("오류", "화면 캡쳐 실패")
            return
        
        self.sample_img = img
        self.show_image(img)
        
        # 마우스 이벤트 바인딩
        self.canvas.bind("<ButtonPress-1>", self.on_mouse_down)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_mouse_up)
        
        messagebox.showinfo("안내", "이미지에서 마우스로 드래그하여 영역을 선택하세요.")
    
    def on_mouse_down(self, event):
        """마우스 누름"""
        self.selection_start = (event.x, event.y)
        self.selection_end = (event.x, event.y)
        
        # 선택 사각형 초기화
        if self.selection_rect:
            self.canvas.delete(self.selection_rect)
        self.selection_rect = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline="red", width=2, dash=(5, 5)
        )
    
    def on_mouse_drag(self, event):
        """마우스 드래그"""
        if self.selection_start:
            self.selection_end = (event.x, event.y)
            
            # 선택 사각형 업데이트
            if self.selection_rect:
                self.canvas.delete(self.selection_rect)
            self.selection_rect = self.canvas.create_rectangle(
                self.selection_start[0], self.selection_start[1],
                event.x, event.y,
                outline="red", width=2, dash=(5, 5)
            )
    
    def on_mouse_up(self, event):
        """마우스 떼기"""
        if self.selection_start and self.selection_end:
            # 선택 영역 계산
            x1 = min(self.selection_start[0], self.selection_end[0])
            y1 = min(self.selection_start[1], self.selection_end[1])
            x2 = max(self.selection_start[0], self.selection_end[0])
            y2 = max(self.selection_start[1], self.selection_end[1])
            
            # 캔버스 크기 비율 계산
            canvas_width = self.canvas.winfo_width()
            canvas_height = self.canvas.winfo_height()
            if canvas_width < 100:
                canvas_width = 960
            if canvas_height < 100:
                canvas_height = 500
            
            # 이미지 크기 비율 계산
            img_ratio = self.sample_img.width / self.sample_img.height
            canvas_ratio = canvas_width / canvas_height
            
            if img_ratio > canvas_ratio:
                scale = canvas_width / self.sample_img.width
                offset_y = (canvas_height - int(self.sample_img.height * scale)) // 2
                offset_x = 0
            else:
                scale = canvas_height / self.sample_img.height
                offset_x = (canvas_width - int(self.sample_img.width * scale)) // 2
                offset_y = 0
            
            # 실제 이미지 좌표로 변환
            real_x1 = int((x1 - offset_x) / scale)
            real_y1 = int((y1 - offset_y) / scale)
            real_x2 = int((x2 - offset_x) / scale)
            real_y2 = int((y2 - offset_y) / scale)
            
            # 유효한 영역 확인
            if real_x1 < 0:
                real_x1 = 0
            if real_y1 < 0:
                real_y1 = 0
            if real_x2 > self.sample_img.width:
                real_x2 = self.sample_img.width
            if real_y2 > self.sample_img.height:
                real_y2 = self.sample_img.height
            
            # 영역이 너무 작으면 무시
            if real_x2 - real_x1 < 5 or real_y2 - real_y1 < 5:
                messagebox.showwarning("경고", "선택 영역이 너무 작습니다.")
                self.canvas.delete(self.selection_rect)
                self.selection_rect = None
                return
            
            # 선택 영역 크롭
            cropped = self.sample_img.crop((real_x1, real_y1, real_x2, real_y2))
            self.sample_img = cropped
            self.sample_binary = self.find_text.image_to_binary_string(cropped)
            
            # 크롭된 이미지 표시
            self.show_image(cropped)
            self.text_area.delete("0.0", "end")
            self.text_area.insert("0.0", self.sample_binary[:500] + "...")
            
            # 선택 사각형 삭제
            self.canvas.delete(self.selection_rect)
            self.selection_rect = None
            
            # 마우스 이벤트 언바인딩
            self.canvas.unbind("<ButtonPress-1>")
            self.canvas.unbind("<B1-Motion>")
            self.canvas.unbind("<ButtonRelease-1>")
            
            print(f"[FindTextGUI] 영역 선택 완료: ({real_x1}, {real_y1}) -> ({real_x2}, {real_y2})")
    
    def adjust_crop(self, dx: int, dy: int):
        """1px 단위 정밀 조절"""
        if not self.sample_img:
            messagebox.showwarning("경고", "먼저 화면 캡쳐를 수행하세요")
            return
        
        width, height = self.sample_img.size
        
        # 현재 이미지 크롭
        if width < 3 or height < 3:
            messagebox.showwarning("경고", "이미지가 너무 작습니다.")
            return
        
        # 1px 조절 (상하좌우)
        new_x1 = max(0, -dx if dx < 0 else 0)
        new_y1 = max(0, -dy if dy < 0 else 0)
        new_x2 = width - (dx if dx > 0 else 0)
        new_y2 = height - (dy if dy > 0 else 0)
        
        # 유효한 영역 확인
        if new_x2 - new_x1 < 2 or new_y2 - new_y1 < 2:
            messagebox.showwarning("경고", "더 이상 잘라낼 수 없습니다.")
            return
        
        # 크롭 적용
        cropped = self.sample_img.crop((new_x1, new_y1, new_x2, new_y2))
        self.sample_img = cropped
        self.sample_binary = self.find_text.image_to_binary_string(cropped)
        
        # 크롭된 이미지 표시
        self.show_image(cropped)
        self.text_area.delete("0.0", "end")
        self.text_area.insert("0.0", self.sample_binary[:500] + "...")
        
        print(f"[FindTextGUI] 정밀 조절: dx={dx}, dy={dy}, 새 크기: {cropped.size}")
    
    def reset_crop(self):
        """원본 이미지로 리셋"""
        if not self.original_img:
            messagebox.showwarning("경고", "원본 이미지가 없습니다.")
            return
        
        self.sample_img = self.original_img
        self.sample_binary = self.find_text.image_to_binary_string(self.original_img)
        
        # 원본 이미지 표시
        self.show_image(self.original_img)
        self.text_area.delete("0.0", "end")
        self.text_area.insert("0.0", self.sample_binary[:500] + "...")
        
        print("[FindTextGUI] 원본 이미지로 리셋")
    
    def save_binary_string(self):
        """비트맵 문자열 저장"""
        if not self.sample_binary:
            messagebox.showwarning("경고", "먼저 화면 캡쳐를 수행하세요")
            return
        
        try:
            from tkinter import filedialog
            file_path = filedialog.asksaveasfilename(
                defaultextension=".txt",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
            )
            
            if file_path:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(self.sample_binary)
                messagebox.showinfo("성공", f"비트맵 문자열 저장 완료: {file_path}")
        except Exception as e:
            messagebox.showerror("오류", f"저장 실패: {e}")
    
    def test_find(self):
        """패턴 찾기 테스트"""
        if not self.sample_binary:
            messagebox.showwarning("경고", "먼저 화면 캡쳐를 수행하세요")
            return
        
        try:
            results = self.find_text.find_text(self.sample_binary, err1=0.1, err0=0.1)
            if results:
                msg = f"{len(results)}개의 패턴 발견:\n"
                for i, r in enumerate(results[:5]):  # 처음 5개만 표시
                    msg += f"  {i+1}. x={r['x']}, y={r['y']}, score={r['score']:.2f}\n"
                if len(results) > 5:
                    msg += f"  ... (총 {len(results)}개)"
                messagebox.showinfo("결과", msg)
            else:
                messagebox.showinfo("결과", "패턴을 찾지 못했습니다")
        except Exception as e:
            messagebox.showerror("오류", f"테스트 실패: {e}")


# 테스트 코드
if __name__ == "__main__":
    # 기본적으로 GUI 모드 실행
    ft = FindText()
    gui = FindTextGUI(ft)
    gui.show()
