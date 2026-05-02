import numpy as np
import cv2
import os
import time
from typing import Dict, List, Tuple, Optional
import csv
from datetime import datetime

# Debug image dumps are expensive and create lots of files; keep them off by default.
SAVE_DEBUG_IMAGES = os.environ.get("SVC_SAVE_DEBUG_IMAGES", "0") == "1"

class FindTextEngine:
    def __init__(self, digit_patterns: Dict[str, str] = None):
        self.template_cache: Dict[str, Tuple[np.ndarray, int]] = {}
        self.ahk_chars = "0123456789+/ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        self.char_to_val = {c: i for i, c in enumerate(self.ahk_chars)}
        self.last_thr = {} # 필드별 성공 임계값 저장
        self.debug_log_file = "debug_report.csv"
        self.test_num = 120  # TestNum 120부터 시작
        # 너무 많은 print로 성능/가독성이 떨어져 기본은 비활성화.
        # 필요 시 외부에서 fte.verbose = True로 켜서 상세 로그를 볼 수 있음.
        self.verbose = False
        
        if digit_patterns:
            for digit, pattern_str in digit_patterns.items():
                # 다중 템플릿 처리 (리스트 형식)
                if isinstance(pattern_str, list):
                    if self.verbose:
                        print(f"[DEBUG] 숫자 {digit} 다중 템플릿 로딩: {len(pattern_str)}개")
                    for i, pattern in enumerate(pattern_str):
                        bitmap, threshold = self.decode(pattern)
                        if bitmap is not None:
                            cache_key = f"{digit}_{i}"
                            self.template_cache[cache_key] = (bitmap, threshold)
                            if self.verbose:
                                print(f"[DEBUG] 숫자 {digit} 템플릿 {i} 로딩 성공: 크기 {bitmap.shape}")
                        else:
                            if self.verbose:
                                print(f"[DEBUG] 숫자 {digit} 템플릿 {i} 로딩 실패")
                else:
                    # 단일 템플릿 처리
                    bitmap, threshold = self.decode(pattern_str)
                    if bitmap is not None:
                        self.template_cache[digit] = (bitmap, threshold)
                        if self.verbose:
                            print(f"[DEBUG] 숫자 {digit} 단일 템플릿 로딩 성공: 크기 {bitmap.shape}")
                    else:
                        if self.verbose:
                            print(f"[DEBUG] 숫자 {digit} 단일 템플릿 로딩 실패")
    
    def _median(self, nums: List[float]) -> Optional[float]:
        if not nums:
            return None
        s = sorted(nums)
        mid = len(s) // 2
        if len(s) % 2 == 1:
            return float(s[mid])
        return float((s[mid - 1] + s[mid]) / 2.0)

    def _compose_xy_4slots(self, final_matches: List[Tuple[int, int, str, float, int]], crop_w: int) -> str:
        """
        x/y 좌표는 화면에 항상 4자리 고정으로 표시됨.
        검출된 숫자들의 x좌표(중심) 간격을 추정해 4개의 슬롯에 매핑하고,
        비는 슬롯은 'x'로 채운다. (선행 0 포함 그대로 유지)
        """
        if not final_matches:
            return "xxxx"

        centers = [(m[0] + (m[4] / 2.0), m) for m in final_matches]  # (center_x, match)
        # 4자리 고정 필드이므로, ROI 폭을 4등분한 "슬롯 중심"을 기본 기준으로 사용한다.
        slot_centers = [((i + 0.5) * crop_w / 4.0) for i in range(4)]

        # 슬롯별 최선 매치 선택 (err 작을수록 좋음). 슬롯 중심에 가장 가까운 매치를 우선 고려.
        slots: List[Optional[Tuple[str, float]]] = [None, None, None, None]
        max_dist = max(3.0, crop_w / 8.0)  # 슬롯 중심에서 너무 멀면 잡음으로 간주
        for cx, m in centers:
            # 가장 가까운 슬롯
            idx = min(range(4), key=lambda i: abs(cx - slot_centers[i]))
            if abs(cx - slot_centers[idx]) > max_dist:
                continue
            digit = m[2]
            err = float(m[3])
            cur = slots[idx]
            if cur is None or err < cur[1]:
                slots[idx] = (digit, err)

        return "".join([(s[0] if s else "x") for s in slots])

    def _xy_slot_centers(self, crop_w: int) -> List[float]:
        return [((i + 0.5) * crop_w / 4.0) for i in range(4)]

    def _recognize_xy_4digit(self, crop, debug_name: str) -> str:
        """
        x/y는 항상 4자리 고정이라는 전제를 적극 활용:
        전역 threshold로 후보를 잔뜩 모은 뒤 NMS 하는 대신,
        4개 슬롯 주변에서만 템플릿별 최고 점수를 비교해 각 슬롯의 숫자를 결정한다.
        """
        dprint = print if getattr(self, "verbose", False) else (lambda *a, **k: None)

        if crop is None or getattr(crop, "size", 0) == 0:
            return "xxxx"

        # Input is RGB (CaptureSvc provides RGB frames). Apply the same weighted grayscale as ft.ahk.
        gray = crop[:, :, 0].astype(np.uint32) * 38 + crop[:, :, 1].astype(np.uint32) * 75 + crop[:, :, 2].astype(np.uint32) * 15
        base = self.last_thr.get(debug_name, 50)
        scan_range = [base, base - 10, base + 10, 50, 114, 150]

        best_res = "xxxx"
        best_score_sum = -1.0

        for thr in scan_range:
            if thr < 0 or thr > 255:
                continue
            c = (thr + 1) << 7
            s_bin = (gray < c).astype(np.uint8)
            s_bin_f = s_bin.astype(np.float32)

            h, w = s_bin.shape[:2]
            slot_centers = self._xy_slot_centers(w)
            # slot -> (score, digit)
            slot_best = [(0.0, "x") for _ in range(4)]

            for digit, (t1, _) in self.template_cache.items():
                th, tw = t1.shape
                if th > h or tw > w:
                    continue

                s1_v = cv2.matchTemplate(s_bin_f, t1.astype(np.float32), cv2.TM_CCORR)
                s0_v = cv2.matchTemplate(1.0 - s_bin_f, (1 - t1).astype(np.float32), cv2.TM_CCORR)
                sim1 = s1_v / max(1.0, float(np.sum(t1)))
                sim0 = s0_v / max(1.0, float(th * tw - np.sum(t1)))
                score_map = np.minimum(sim1, sim0)

                digit_base = digit.split('_')[0] if '_' in digit else digit

                # 각 슬롯 중심 주변에서만 최고점 탐색
                for si, scx in enumerate(slot_centers):
                    # score_map의 x는 "템플릿 top-left"이므로 center를 top-left로 변환
                    nominal_x = int(round(scx - (tw / 2.0)))
                    x0 = max(0, nominal_x - tw)
                    x1 = min(score_map.shape[1] - 1, nominal_x + tw)
                    if x1 < x0:
                        continue

                    roi = score_map[:, x0:x1 + 1]
                    if roi.size == 0:
                        continue

                    max_idx = int(np.argmax(roi))
                    ry, rx = np.unravel_index(max_idx, roi.shape)
                    score = float(roi[ry, rx])

                    # 슬롯 최고점 갱신
                    if score > slot_best[si][0]:
                        slot_best[si] = (score, digit_base)

            # 신뢰도 낮은 슬롯은 x로 처리
            # 고정 임계값 대신, 해당 thr에서의 평균 최고점을 기준으로 상대 임계값을 둔다.
            scores = [s for s, _d in slot_best]
            avg = sum(scores) / 4.0
            slot_thr = max(0.50, avg * 0.85)
            res = "".join([(d if s >= slot_thr else "x") for s, d in slot_best])
            score_sum = sum([s for s, _d in slot_best])

            dprint(f"[XY] thr={thr} res={res} score_sum={score_sum:.3f} avg={avg:.3f} slot_thr={slot_thr:.3f}")

            if score_sum > best_score_sum:
                best_score_sum = score_sum
                best_res = res

        return best_res

    def log_debug(self, field: str, value: str, note: str = ""):
        """디버깅 기록을 debug_report.csv에 저장 (비활성화)"""
        try:
            file_exists = os.path.exists(self.debug_log_file)
            with open(self.debug_log_file, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(['Time', 'TestNum', 'Field', 'Value', 'Note'])
                writer.writerow([datetime.now().strftime('%Y-%m-%d %H:%M:%S'), self.test_num, field, value, note])
            # print(f"[FindTextEngine] Logged: {field}={value}, TestNum={self.test_num}")  # 비활성화
        except Exception as e:
            # print(f"[FindTextEngine] Failed to log debug: {e}")  # 비활성화
            pass

    def decode(self, ahk_str: str) -> Tuple[np.ndarray, int]:
        try:
            star_pos, dollar_pos, dot_pos = ahk_str.find('*'), ahk_str.find('$'), ahk_str.find('.')
            if -1 in (star_pos, dollar_pos, dot_pos): return None, 127
            threshold = int(ahk_str[star_pos + 1:dollar_pos])
            width = int(ahk_str[dollar_pos + 1:dot_pos])
            bits_list = []
            for char in ahk_str[dot_pos + 1:]:
                val = self.char_to_val.get(char, 0)
                for j in range(5, -1, -1): bits_list.append((val >> j) & 1)
            while bits_list and bits_list[-1] == 0: bits_list.pop()
            if bits_list and bits_list[-1] == 1: bits_list.pop()
            height = len(bits_list) // width
            if height == 0: return None, threshold
            return np.array(bits_list[:height*width], dtype=np.uint8).reshape(height, width), threshold
        except: return None, 127

    def analyze_template_mismatch(self, crop, debug_name="exp"):
        """템플릿과 화면 숫자의 픽셀 차이 분석"""
        if (not SAVE_DEBUG_IMAGES) or debug_name != "exp":
            return
            
        import os
        debug_dir = "debug_analysis"
        os.makedirs(debug_dir, exist_ok=True)
        
        # 실제 exp ROI 영역 추출 (config.json의 ROI 사용)
        try:
            print(f"[Analysis] 전체 화면 크기: {crop.shape}")
            # config.json의 exp ROI 사용: sx: 1380, sy: 965, dx: 1680, dy: 993
            # 하지만 crop은 이미 exp 영역일 수 있으므로 전체 영역 사용
            if crop.shape[0] < 965 or crop.shape[1] < 1380:
                print(f"[Analysis] crop이 전체 화면이 아닙니다. crop 크기: {crop.shape}")
                exp_region = crop  # crop 자체가 exp 영역일 수 있음
            else:
                exp_region = crop[965:993, 1380:1680]
            print(f"[Analysis] exp ROI 영역 크기: {exp_region.shape}")
            if exp_region.size == 0:
                print("[Analysis] exp ROI 영역이 비어있습니다.")
                return
        except Exception as e:
            print(f"[Analysis] exp ROI 영역 추출 실패: {e}")
            return
        
        # ft.ahk 방식으로 그레이스케일 및 이진화
        gray = exp_region[:,:,0].astype(np.uint32)*38 + exp_region[:,:,1].astype(np.uint32)*75 + exp_region[:,:,2].astype(np.uint32)*15
        c = (50 + 1) << 7  # 기본 임계값
        s_bin = (gray < c).astype(np.uint8)
        
        # 전체 이진화된 영역 저장
        if s_bin.size > 0:
            cv2.imwrite(f"{debug_dir}/exp_binary.png", s_bin * 255)
        
        # 실제 화면에서 5와 9 숫자 위치 찾기 (혼동 문제 분석)
        for digit_name in ['5', '9', '2']:
            if digit_name in self.template_cache:
                template, _ = self.template_cache[digit_name]
                
                # 템플릿 매칭으로 위치 찾기
                result = cv2.matchTemplate(s_bin.astype(np.float32), template.astype(np.float32), cv2.TM_CCORR_NORMED)
                min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)
                
                print(f"[Analysis] 숫자 {digit_name} 매칭 점수: {max_val:.3f}")
                
                if max_val > 0.2:  # 낮은 임계값으로 일단 위치 찾기
                    y, x = max_loc
                    h, w = template.shape
                    
                    # 화면에서 해당 숫자 영역 추출
                    if y+h <= s_bin.shape[0] and x+w <= s_bin.shape[1]:
                        screen_digit = s_bin[y:y+h, x:x+w]
                        
                        # 템플릿과 화면 숫자 저장
                        cv2.imwrite(f"{debug_dir}/template_{digit_name}.png", template * 255)
                        cv2.imwrite(f"{debug_dir}/screen_{digit_name}.png", screen_digit * 255)
                        
                        # 차이 계산
                        diff = cv2.absdiff(template, screen_digit)
                        cv2.imwrite(f"{debug_dir}/diff_{digit_name}.png", diff * 255)
                        
                        # 픽셀 차이 통계
                        total_pixels = template.size
                        different_pixels = np.sum(diff)
                        similarity = (total_pixels - different_pixels) / total_pixels * 100
                        
                        print(f"[Analysis] 숫자 {digit_name}:")
                        print(f"  - 전체 픽셀: {total_pixels}")
                        print(f"  - 다른 픽셀: {different_pixels}")
                        print(f"  - 유사도: {similarity:.2f}%")
                        print(f"  - 위치: ({x}, {y})")
                        print(f"  - 매칭 점수: {max_val:.3f}")
                        
                        # 다른 숫자들과도 비교
                        for other_digit in ['0', '1', '3', '4', '6', '7', '8', '9']:
                            if other_digit in self.template_cache:
                                other_template, _ = self.template_cache[other_digit]
                                # 템플릿 크기 확인
                                if (screen_digit.shape[0] <= other_template.shape[0] and 
                                    screen_digit.shape[1] <= other_template.shape[1]):
                                    other_result = cv2.matchTemplate(screen_digit.astype(np.float32), other_template.astype(np.float32), cv2.TM_CCORR_NORMED)
                                    other_max = cv2.minMaxLoc(other_result)[1]
                                    if other_max > max_val * 0.9:  # 비슷한 점수가 있는지 확인
                                        print(f"  - 경고: {other_digit}와 유사함 (점수: {other_max:.3f})")
                                else:
                                    print(f"  - {other_digit} 템플릿 크기 초과로 비교 생략")
                    else:
                        print(f"[Analysis] 숫자 {digit_name}: 영역 범위 초과 ({x}, {y}, {w}, {h})")
                        # 경계 조정으로 다시 시도
                        if y+h > s_bin.shape[0]:
                            h = s_bin.shape[0] - y
                        if x+w > s_bin.shape[1]:
                            w = s_bin.shape[1] - x
                        if h > 0 and w > 0:
                            screen_digit = s_bin[y:y+h, x:x+w]
                            template_cropped = template[:h, :w]
                            
                            # 템플릿과 화면 숫자 저장
                            cv2.imwrite(f"{debug_dir}/template_{digit_name}_cropped.png", template_cropped * 255)
                            cv2.imwrite(f"{debug_dir}/screen_{digit_name}_cropped.png", screen_digit * 255)
                            
                            # 차이 계산
                            diff = cv2.absdiff(template_cropped, screen_digit)
                            cv2.imwrite(f"{debug_dir}/diff_{digit_name}_cropped.png", diff * 255)
                            
                            # 픽셀 차이 통계
                            total_pixels = template_cropped.size
                            different_pixels = np.sum(diff)
                            similarity = (total_pixels - different_pixels) / total_pixels * 100
                            
                            print(f"[Analysis] 숫자 {digit_name} (크롭된):")
                            print(f"  - 전체 픽셀: {total_pixels}")
                            print(f"  - 다른 픽셀: {different_pixels}")
                            print(f"  - 유사도: {similarity:.2f}%")
                            print(f"  - 크롭된 크기: {w}x{h}")
                else:
                    print(f"[Analysis] 숫자 {digit_name}: 매칭 실패 (점수: {max_val:.3f})")

    def validate_exp_value(self, recognized_exp, debug_name="exp"):
        """인식된 exp 값의 합리성 검증"""
        if debug_name != "exp":
            return True
        
        try:
            exp_num = int(recognized_exp)
            
            # 자릿수 검증 (2-12자리로 완화)
            if len(recognized_exp) < 2 or len(recognized_exp) > 12:
                return False
            
            # 게임 상식적 범위 검증 (0부터 허용)
            if exp_num < 0 or exp_num > 99999999999:
                return False
            
            # 이전 값과의 급격한 변화 검증 (완화)
            if hasattr(self, 'last_exp_value'):
                if self.last_exp_value > 0:
                    change_ratio = abs(exp_num - self.last_exp_value) / max(self.last_exp_value, 1)
                    # 500% 이상 변화만 비정상으로 간주
                    if change_ratio > 5.0:
                        return False
            
            # 숫자 패턴 검증 (완화)
            if len(set(recognized_exp)) == 1 and len(recognized_exp) > 5:
                return False  # 5자리 이상의 같은 숫자만 비정상
            
            return True
            
        except ValueError:
            return False

    def extract_and_update_templates(self, screen_roi, debug_name="exp"):
        """현재 화면에서 숫자들을 추출하여 템플릿 업데이트"""
        if debug_name == "exp":
            # exp 영역 추출
            exp_region = screen_roi[965:993, 1419:1648]  # 현재 exp ROI
            
            # 숫자 분리를 위한 간단한 전처리
            gray = cv2.cvtColor(exp_region, cv2.COLOR_BGR2GRAY)
            _, binary = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY_INV)
            
            # 윤곽선 찾기
            contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            # 윤곽선을 x좌표로 정렬
            digit_contours = sorted(contours, key=lambda c: cv2.boundingRect(c)[0])
            
            updated_count = 0
            for i, contour in enumerate(digit_contours[:10]):  # 최대 10개 숫자
                x, y, w, h = cv2.boundingRect(contour)
                if w < 10 or h < 15:  # 너무 작은 윤곽선은 무시
                    continue
                
                # 숫자 영역 추출
                digit_img = binary[y:y+h, x:x+w]
                
                # 템플릿 크기로 리사이즈 (18x24)
                digit_resized = cv2.resize(digit_img, (18, 24), interpolation=cv2.INTER_NEAREST)
                
                # AHK 패턴으로 변환하여 저장
                ahk_pattern = self.bin_to_ahk(digit_resized, 50)
                if ahk_pattern:
                    # 파일로 저장
                    with open(f"temple/digits/{i}_extracted.txt", 'w') as f:
                        f.write(ahk_pattern)
                    updated_count += 1
            
            print(f"[FindTextEngine] {debug_name} 템플릿 업데이트 완료: {updated_count}개 숫자")
            return updated_count
        
        return 0

    def get_adaptive_threshold(self, digit, base_threshold):
        """숫자별 최적 임계값 동적 조정"""
        # 숫자별 최적 임계값 (경험적 데이터 기반)
        digit_adjustments = {
            '0': +5,   # 0은 약간 더 높은 임계값
            '2': -3,   # 2는 약간 더 낮은 임계값
            '3': -12,  # 3은 훨씬 더 낮은 임계값 (9로 인식 방지)
            '4': -15,  # 4는 매우 낮은 임계값 (0.75→0.60)
            '5': -12,  # 5는 훨씬 더 낮은 임계값 (6으로 인식 방지)
            '7': -2,
            '8': +3,
        }
        return base_threshold + digit_adjustments.get(digit, 0)

    def pixel_perfect_match(self, screen_bin, template_bin, err1=0.05, err0=0.05):
        """ft.ahk 방식의 픽셀 단위 정확한 매칭"""
        h, w = template_bin.shape
        sh, sw = screen_bin.shape
        
        # ft.ahk의 err1/err0 계산 방식 적용 (정수 타입으로 변환)
        template_ones = int(np.sum(template_bin == 1))
        template_zeros = w*h - template_ones
        
        max_err1 = max(2, int(template_ones * 0.1))  # 10% 오차 허용, 최소 2픽셀
        max_err0 = max(2, int(template_zeros * 0.1))
        
        matches = []
        for y in range(sh - h + 1):
            for x in range(sw - w + 1):
                roi = screen_bin[y:y+h, x:x+w]
                
                # 픽셀 단위 비교
                match1 = np.sum((roi == 1) & (template_bin == 1))
                match0 = np.sum((roi == 0) & (template_bin == 0))
                err1_count = np.sum((roi == 1) & (template_bin == 0))
                err0_count = np.sum((roi == 0) & (template_bin == 1))
                
                # ft.ahk 스타일 오차 검증
                if err1_count <= max_err1 and err0_count <= max_err0:
                    # 정확도 점수 계산 (높을수록 좋음)
                    accuracy = (match1 + match0) / (w * h)
                    matches.append((x, y, accuracy))
        
        return matches

    def recognize_with_findtext(self, crop, override_threshold: Optional[int] = None, debug_name: str = "unknown"):
        dprint = print if getattr(self, "verbose", False) else (lambda *a, **k: None)
        # 템플릿 차이 분석 (exp 필드에 대해서만 한 번 실행)
        if SAVE_DEBUG_IMAGES and debug_name == "exp" and not hasattr(self, '_analysis_done'):
            self.analyze_template_mismatch(crop, debug_name)
            self._analysis_done = True
        
        # ft.ahk mode=2 Gray Threshold Mode: weighted BGR grayscale
        # BGR 순서: B=0, G=1, R=2
        # gray = R*38 + G*75 + B*15
        # Input is RGB (CaptureSvc provides RGB frames). Apply the same weighted grayscale as ft.ahk.
        gray = crop[:,:,0].astype(np.uint32)*38 + crop[:,:,1].astype(np.uint32)*75 + crop[:,:,2].astype(np.uint32)*15

        # x/y는 4자리 고정 슬롯 기반 인식을 우선 사용
        if debug_name in ("x", "y"):
            return self._recognize_xy_4digit(crop, debug_name)
        
        all_matches = []
        # 필드별 최적 임계값 주변 탐색
        base = self.last_thr.get(debug_name, 50 if debug_name != "money" else 114)
        scan_range = [base, base-10, base+10, 50, 114, 150]
        
        for thr in scan_range:
            if thr < 0 or thr > 255: continue
            # ft.ahk: c=(c+1)<<7, screen_bin = (gray < c)
            c = (thr + 1) << 7
            s_bin = (gray < c).astype(np.uint8)
            s_bin_f = s_bin.astype(np.float32)
            
            matches_this_thr = []
            for digit, (t1, _) in self.template_cache.items():
                th, tw = t1.shape
                if th > s_bin.shape[0] or tw > s_bin.shape[1]: continue
                
                # 원래의 순수 OpenCV 매칭으로 복귀
                s1_v = cv2.matchTemplate(s_bin_f, t1.astype(np.float32), cv2.TM_CCORR)
                s0_v = cv2.matchTemplate(1.0 - s_bin_f, (1-t1).astype(np.float32), cv2.TM_CCORR)
                sim1, sim0 = s1_v / np.sum(t1), s0_v / (th*tw - np.sum(t1))
                
                score_map = np.minimum(sim1, sim0)
                
                # 숫자 4 템플릿별 매칭 점수 디버깅 - 모든 필드 확장 + 강제 출력
                dprint(f"[DEBUG] 모든 숫자 템플릿 확인 - {debug_name} 필드: 숫자 {digit} 처리 중...")
                # 다중 템플릿 키 처리 (예: "4_0", "4_1" 등)
                digit_base = digit.split('_')[0]  # "4_0" -> "4"
                if digit_base == '4':
                    dprint(f"[DEBUG] 숫자 4 템플릿 발견! - {debug_name} 필드 처리 중...")
                    # 최고 점수 찾기
                    max_score = np.max(score_map)
                    max_pos = np.unravel_index(np.argmax(score_map), score_map.shape)
                    max_sim1 = sim1[max_pos]
                    max_sim0 = sim0[max_pos]
                    dprint(f"[DEBUG] 숫자 4 템플릿 - {debug_name} 필드: 최고 점수: {max_score:.3f}, sim1: {max_sim1:.3f}, sim0: {max_sim0:.3f}, 위치: {max_pos}")
                    
                    # x,y 필드는 항상 상세 출력 + 강제 출력
                    if debug_name in ['x', 'y']:
                        dprint(f"[DEBUG] 숫자 4 템플릿 - {debug_name} 필드 강제 디버그 시작")
                        dprint(f"[DEBUG] 실제 값: x:0144, y:0044 (사용자 제공)")
                        dprint(f"[DEBUG] 시스템 인식: x:1, y:3 (문제 발생)")
                        # 상위 5개 매칭 점수 출력
                        top_indices = np.unravel_index(np.argpartition(score_map.flatten(), -5)[-5:], score_map.shape)
                        dprint(f"[DEBUG] 숫자 4 템플릿 - {debug_name} 필드: 상위 5개 점수:")
                        for i in range(5):
                            y_pos, x_pos = top_indices[0][i], top_indices[1][i]
                            score = score_map[y_pos, x_pos]
                            sim1_val = sim1[y_pos, x_pos]
                            sim0_val = sim0[y_pos, x_pos]
                            dprint(f"  {i+1}: 위치({x_pos}, {y_pos}) 점수:{score:.3f} sim1:{sim1_val:.3f} sim0:{sim0_val:.3f}")
                        
                        # 임계값 통과 여부 확인
                        threshold_mask = (sim1 >= 0.75) & (sim0 >= 0.7)
                        if np.any(threshold_mask):
                            threshold_positions = np.where(threshold_mask)
                            dprint(f"[DEBUG] 숫자 4 템플릿 - {debug_name} 필드: 임계값 통과 위치 수: {len(threshold_positions[0])}")
                            if len(threshold_positions[0]) > 0:
                                # 임계값 통과한 최고 점수
                                threshold_scores = score_map[threshold_mask]
                                max_threshold_idx = np.argmax(threshold_scores)
                                ty, tx = threshold_positions[0][max_threshold_idx], threshold_positions[1][max_threshold_idx]
                                dprint(f"[DEBUG] 임계값 통과 최고 점수: {threshold_scores[max_threshold_idx]:.3f} at ({tx}, {ty})")
                        else:
                            dprint(f"[DEBUG] 숫자 4 템플릿 - {debug_name} 필드: 임계값 미통과")
                        
                        # 추가 정보: 템플릿과 화면 크기 비교
                        dprint(f"[DEBUG] 숫자 4 템플릿 - {debug_name} 필드: 템플릿 크기: {t1.shape}, 화면 크기: {s_bin.shape}")
                        
                        # 최저 점수도 확인
                        min_score = np.min(score_map)
                        min_pos = np.unravel_index(np.argmin(score_map), score_map.shape)
                        min_sim1 = sim1[min_pos]
                        min_sim0 = sim0[min_pos]
                        dprint(f"[DEBUG] 숫자 4 템플릿 - {debug_name} 필드: 최저 점수: {min_score:.3f}, sim1: {min_sim1:.3f}, sim0: {min_sim0:.3f}, 위치: {min_pos}")
                        dprint(f"[DEBUG] 숫자 4 템플릿 - {debug_name} 필드 강제 디버그 종료")
                else:
                    # 다른 숫자들도 x,y 필드에서는 출력
                    if debug_name in ['x', 'y']:
                        dprint(f"[DEBUG] 다른 숫자({digit}) - {debug_name} 필드: 최고 점수: {np.max(score_map):.3f}")
                
                # 숫자별 개별 임계값 적용
                # 템플릿 키가 "4_0"처럼 올 수 있으므로 digit_base 기준으로 적용한다.
                if digit_base in ['5']:  # 5만 높은 임계값 유지
                    mask = (sim1 >= 0.80) & (sim0 >= 0.7)  # sim1 높게, sim0 통일
                elif digit_base in ['9']:  # 9는 약간 낮춤
                    mask = (sim1 >= 0.75) & (sim0 >= 0.7)  # sim1 조정, sim0 통일
                elif digit_base in ['2']:  # 2 개별 조절
                    mask = (sim1 >= 0.75) & (sim0 >= 0.7)  # sim1 조정, sim0 통일
                elif digit_base in ['4']:  # 4 개별 조절 (1로 인식 문제 해결)
                    if debug_name in ['x', 'y']:
                        # x/y ROI에서는 글자 폭이 작고 안티앨리어싱 영향으로 sim1이 낮게 나오는 케이스가 많음.
                        # score_map(=min(sim1,sim0)) 기준으로 완화하되, 과검출을 막기 위해
                        # (1) 해당 템플릿의 최고점 주변만, (2) 배경 일치(sim0)도 충분히 높은 곳만 통과시킨다.
                        max_s = float(np.max(score_map))
                        # y ROI에서는 마지막 자리의 대비가 약해 max_s가 낮을 수 있어 약간 더 완화
                        thr4 = max(0.50, max_s * 0.92)
                        if debug_name == 'y':
                            mask = (score_map >= thr4) & (sim0 >= 0.60) & (sim1 >= 0.55)
                        else:
                            mask = (score_map >= thr4) & (sim0 >= 0.90)
                    else:
                        mask = (sim1 >= 0.75) & (sim0 >= 0.7)  # sim1 조정, sim0 통일
                elif digit_base in ['6']:  # 6 개별 조절
                    mask = (sim1 >= 0.72) & (sim0 >= 0.7)  # sim1 조정, sim0 통일
                elif digit_base in ['8']:  # 8 개별 조절
                    mask = (sim1 >= 0.72) & (sim0 >= 0.7)  # sim1 조정, sim0 통일
                elif digit_base in ['0']:  # 0 개별 조절
                    mask = (sim1 >= 0.70) & (sim0 >= 0.7)  # sim1 조정, sim0 통일
                elif digit_base in ['1']:  # 1 개별 조절
                    mask = (sim1 >= 0.75) & (sim0 >= 0.7)  # sim1 조정, sim0 통일
                elif digit_base in ['3']:  # 3 개별 조절
                    mask = (sim1 >= 0.70) & (sim0 >= 0.7)  # sim1 조정, sim0 통일
                elif digit_base in ['7']:  # 7 개별 조절
                    mask = (sim1 >= 0.70) & (sim0 >= 0.7)  # sim1 조정, sim0 통일
                
                for y, x in zip(*np.where(mask)):
                    # 5와 9 혼동 방지: 추가 픽셀 레벨 검증
                    if digit in ['5', '9']:
                        # 해당 위치에서 실제 픽셀 패턴 추출
                        if y+th <= s_bin.shape[0] and x+tw <= s_bin.shape[1]:
                            screen_patch = s_bin[y:y+th, x:x+tw]
                            # 5와 9 구분을 위한 특정 픽셀 패턴 검증
                            if digit == '5':
                                # 5 숫자 특징: 상단 중앙에 픽셀이 있어야 함
                                if th > 10 and tw > 10:
                                    center_top = screen_patch[2:th//3, tw//3:2*tw//3]
                                    if np.sum(center_top) < center_top.size * 0.3:  # 픽셀이 부족하면 5가 아님
                                        continue  # 이 위치는 5가 아님
                            elif digit == '9':
                                # 9 숫자 특징: 하단 중앙에 픽셀이 있어야 함
                                if th > 10 and tw > 10:
                                    center_bottom = screen_patch[2*th//3:th-2, tw//3:2*tw//3]
                                    if np.sum(center_bottom) < center_bottom.size * 0.3:  # 픽셀이 부족하면 9가 아님
                                        continue  # 이 위치는 9가 아님
                    
                    # 좌표 범위 확인 후 추가
                    if 0 <= y < score_map.shape[0] and 0 <= x < score_map.shape[1]:
                        matches_this_thr.append((int(x), int(y), digit, 1.0 - score_map[y, x]))
                    else:
                        dprint(f"[DEBUG] 좌표 범위 초과: ({x}, {y}), score_map 크기: {score_map.shape}")
            
            # 디버그: exp 필드 모든 시도 로그 (비활성화)
            # if debug_name == "exp":
            #     print(f"[FindTextEngine] {debug_name} thr={thr}, matches={len(matches_this_thr)}")

            if matches_this_thr:
                self.last_thr[debug_name] = thr # 성공한 임계값 기억
                all_matches.extend(matches_this_thr)
                # 전체 매치 수를 기준으로 조기 종료
                # 고정 조기 종료(3)는 긴 숫자에서 앞자리 누락을 유발하므로 완화
                early_exit_threshold = 10 if debug_name == "exp" else 20
                if len(all_matches) >= early_exit_threshold:
                    break

        if not all_matches: return ""
        
        # 4번 우선순위 및 NMS
        all_matches.sort(key=lambda m: (1.0 - m[3]) * (2.0 if m[2].startswith('4') else 1.0), reverse=True)
        final = []
        # exp 필드는 중복 허용 범위를 0.2로 설정
        # x/y는 글자 간격이 매우 좁아 NMS 겹침 제거가 과하게 작동할 수 있어 더 완화
        overlap_threshold = 0.0 if debug_name in ("x", "y") else (0.2 if debug_name == "exp" else 0.3)
        for x, y, digit, err in all_matches:
            # 다중 템플릿 키 처리 (예: "4_0" -> "4")
            digit_base = digit.split('_')[0] if '_' in digit else digit
            # 템플릿 너비 가져오기
            if digit in self.template_cache:
                w = self.template_cache[digit][0].shape[1]
            else:
                # 다중 템플릿인 경우 첫 번째 템플릿의 너비 사용
                base_keys = [k for k in self.template_cache.keys() if k.startswith(digit_base + '_')]
                if base_keys:
                    w = self.template_cache[base_keys[0]][0].shape[1]
                else:
                    continue  # 템플릿을 찾을 수 없으면 건너뛰기
            
            # 중복 확인 시에도 다중 템플릿 키 처리
            def get_template_width(d):
                if d in self.template_cache:
                    return self.template_cache[d][0].shape[1]
                else:
                    # 다중 템플릿인 경우 첫 번째 템플릿의 너비 사용
                    base_keys = [k for k in self.template_cache.keys() if k.startswith(d.split('_')[0] + '_')]
                    return self.template_cache[base_keys[0]][0].shape[1] if base_keys else 0
            
            if any(max(0, min(x+w, fx+get_template_width(fd)) - max(x, fx)) > w*overlap_threshold for fx, fy, fd, fe, fw in final):
                continue
            final.append((x, y, digit_base, err, w))
            
        final.sort(key=lambda m: m[0])
        if debug_name in ("x", "y"):
            # x/y는 항상 4자리 고정 + 누락은 'x'
            res = self._compose_xy_4slots(final, crop_w=s_bin.shape[1])
        else:
            # 선행 0 포함 그대로 유지
            res = "".join([m[2] for m in final])
        
        # 디버깅 기록 저장 (1분에 1번만 저장하여 로그 과다 생성 방지)
        if not hasattr(self, '_last_log_time'):
            self._last_log_time = {}
        if debug_name not in self._last_log_time:
            self._last_log_time[debug_name] = 0
        current_time = time.time()
        if current_time - self._last_log_time[debug_name] > 60:  # 60초마다 1번
            self.log_debug(debug_name, res, "ft.ahk weighted BGR grayscale")
            self._last_log_time[debug_name] = current_time
        
        return res
