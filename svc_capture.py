"""svc_capture.py - 화면/카메라 캡처 스레드 (CaptureSvc)."""

import time
import threading
import cv2
from svc_kernel import GameState
from svc_monitor_common import _monitor_log


def get_camera():
    """OBS 가상 카메라 인덱스를 자동 감지해 연결한다."""
    _monitor_log("[Camera] Attempting to connect to camera...")
    for index in range(3):
        _monitor_log(f"[Camera] Trying index {index}...")
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        _monitor_log(f"[Camera] VideoCapture created for index {index}, isOpened={cap.isOpened()}")
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
            cap.set(cv2.CAP_PROP_FPS, 60)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            _monitor_log(f"[Camera] OBS 가상 카메라 연결 성공 (index: {index})")
            return cap, index
        cap.release()
    _monitor_log("[Camera] OBS 가상 카메라 연결 실패")
    return None, -1


class CaptureSvc(threading.Thread):
    """
    OBS 가상 카메라로부터 프레임을 획득하여 GameState에 공유하는 전담 스레드.
    60 FPS 이상의 속도로 최신 화면을 유지하여 전체 시스템 지연을 최소화함.
    """
    def __init__(self, state: GameState):
        super().__init__(name="CaptureSvc", daemon=True)
        self.state = state
        self.cap = None
        self._running = True

    def run(self):
        _monitor_log("[Capture] Frame capture thread start (60+ FPS target)")
        while self._running and self.state.running:
            # 카메라 연결 확인
            if self.cap is None or not self.cap.isOpened():
                self.cap, _ = get_camera()
                if self.cap is None:
                    time.sleep(1.0)
                    continue
            
            # 버퍼 비우기 및 최신 프레임 획득
            ok = True
            for _ in range(3):
                if not self.cap.grab():
                    ok = False
                    break
            if not ok:
                try:
                    self.cap.release()
                except Exception:
                    pass
                self.cap = None
                continue
            
            ret, frame = self.cap.retrieve()
            if not ret or frame is None:
                self.cap.release()
                self.cap = None
                continue

            # BGR -> RGB 미리 변환 (다른 스레드 부하 경감)
            img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            
            # 전역 공유
            self.state.last_frame = img_rgb
            self.state.last_frame_time = time.time()
            
            # 약 100~120 FPS 제한으로 CPU 부하 조절
            time.sleep(0.015)

    def stop(self):
        self._running = False
        if self.cap:
            self.cap.release()
