from __future__ import annotations

import os
import re
import subprocess
from typing import Callable, Iterable, Optional

import numpy as np


OBS_DEVICE_PATTERNS = (
    "OBS Virtual Camera",
    "OBS",
    "Virtual Camera",
)

_DSHOW_VIDEO_RE = re.compile(r'"([^"]+)"\s+\(video\)', re.IGNORECASE)


def _default_log(*args, **kwargs) -> None:
    return None


def _ffmpeg_cmd() -> str:
    return os.environ.get("SVC_FFMPEG_BIN", "ffmpeg")


def list_dshow_video_devices(ffmpeg_bin: Optional[str] = None) -> list[str]:
    cmd = [
        ffmpeg_bin or _ffmpeg_cmd(),
        "-hide_banner",
        "-list_devices",
        "true",
        "-f",
        "dshow",
        "-i",
        "dummy",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
    except FileNotFoundError:
        return []

    output = (proc.stderr or "") + "\n" + (proc.stdout or "")
    devices = [m.group(1) for m in _DSHOW_VIDEO_RE.finditer(output)]
    seen: set[str] = set()
    ordered: list[str] = []
    for name in devices:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def pick_obs_device_name(devices: Iterable[str]) -> Optional[str]:
    device_list = list(devices)
    if not device_list:
        return None

    lowered = [(name, name.lower()) for name in device_list]
    for pattern in OBS_DEVICE_PATTERNS:
        needle = pattern.lower()
        for original, text in lowered:
            if needle in text:
                return original
    return None


class FfmpegDShowCapture:
    def __init__(
        self,
        device_name: str,
        width: int = 1920,
        height: int = 1080,
        fps: int = 60,
        ffmpeg_bin: Optional[str] = None,
        log: Optional[Callable[..., None]] = None,
    ) -> None:
        self.device_name = device_name
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.ffmpeg_bin = ffmpeg_bin or _ffmpeg_cmd()
        self.log = log or _default_log
        self.frame_size = self.width * self.height * 3
        self.proc: Optional[subprocess.Popen] = None
        self._last_frame: Optional[bytes] = None
        self._opened = False
        self._start()

    def _start(self) -> None:
        cmd = [
            self.ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "dshow",
            "-rtbufsize",
            "512M",
            "-thread_queue_size",
            "1024",
            "-video_size",
            f"{self.width}x{self.height}",
            "-framerate",
            str(self.fps),
            "-i",
            f"video={self.device_name}",
            "-an",
            "-sn",
            "-pix_fmt",
            "bgr24",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            self._opened = self.proc.poll() is None and self.proc.stdout is not None
        except Exception as exc:
            self.log(f"[Camera] ffmpeg open failed: {exc}")
            self.proc = None
            self._opened = False

    def isOpened(self) -> bool:
        return bool(self._opened and self.proc and self.proc.poll() is None and self.proc.stdout)

    def _read_exact(self, size: int) -> Optional[bytes]:
        if not self.isOpened():
            return None
        stdout = self.proc.stdout if self.proc else None
        if stdout is None:
            return None
        buf = bytearray()
        while len(buf) < size:
            chunk = stdout.read(size - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def grab(self) -> bool:
        if not self.isOpened():
            return False
        data = self._read_exact(self.frame_size)
        if data is None or len(data) != self.frame_size:
            self.release()
            return False
        self._last_frame = data
        return True

    def retrieve(self):
        if self._last_frame is None:
            return False, None
        frame = np.frombuffer(self._last_frame, dtype=np.uint8)
        if frame.size != self.frame_size:
            return False, None
        try:
            frame = frame.reshape((self.height, self.width, 3))
        except Exception:
            return False, None
        return True, frame

    def set(self, *_args, **_kwargs) -> bool:
        return True

    def get(self, *_args, **_kwargs):
        return 0

    def release(self) -> None:
        self._opened = False
        self._last_frame = None
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            if proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=1.5)
            except Exception:
                proc.kill()
        except Exception:
            pass


def open_preferred_obs_capture(
    log: Optional[Callable[..., None]] = None,
    width: int = 1920,
    height: int = 1080,
    fps: int = 60,
    ffmpeg_bin: Optional[str] = None,
):
    logger = log or _default_log
    devices = list_dshow_video_devices(ffmpeg_bin=ffmpeg_bin)
    obs_name = pick_obs_device_name(devices)
    if not obs_name:
        logger(f"[Camera] OBS virtual camera not found. devices={devices}")
        return None, -1, devices

    logger(f"[Camera] Using OBS device: {obs_name}")
    cap = FfmpegDShowCapture(
        obs_name,
        width=width,
        height=height,
        fps=fps,
        ffmpeg_bin=ffmpeg_bin,
        log=logger,
    )
    if not cap.isOpened():
        cap.release()
        logger("[Camera] OBS capture open failed")
        return None, -1, devices
    return cap, 0, devices
