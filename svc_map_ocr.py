# -*- coding: utf-8 -*-
"""
map_info FindText-style recognizer for 선비족 map name forms.

Supported forms:
  - 선비족입구
  - 선비족{n}
  - 선비족{n}-{n}({n})
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from bis_core import split_map_name_floor

TOKEN_FILE_DEFAULT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "temple", "maps", "map_tokens.json"
)

SEONBIJOK_REGEXES = (
    re.compile(r"^선비족입구$"),
    re.compile(r"^선비족\d+$"),
    re.compile(r"^선비족\d+-\d+\(\d+\)$"),
)


def is_valid_seonbijok_map(text: str) -> bool:
    t = str(text or "").strip()
    return any(rx.match(t) for rx in SEONBIJOK_REGEXES)


class MapNameRecognizer:
    """Prefix + token FindText matcher for map_info ROI."""

    def __init__(self, token_file: Optional[str] = None, digit_patterns: Optional[dict] = None):
        self.token_file = token_file or TOKEN_FILE_DEFAULT
        self.tokens: Dict[str, List[dict]] = {}
        self.last_score: float = 0.0
        self.last_raw: str = ""
        self.last_debug: dict = {}
        self._last_thr: int = 160
        self._load_tokens()
        if digit_patterns:
            self._ingest_ahk_digits(digit_patterns)

    def reload(self, token_file: Optional[str] = None):
        if token_file:
            self.token_file = token_file
        self.tokens = {}
        self._load_tokens()

    def _load_tokens(self):
        if not os.path.exists(self.token_file):
            print(f"[MapOCR] token file missing: {self.token_file}")
            return
        try:
            with open(self.token_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[MapOCR] token load failed: {e}")
            return

        raw_tokens = data.get("tokens", {})
        for key, meta in raw_tokens.items():
            bitmap = np.asarray(meta.get("bitmap", []), dtype=np.uint8)
            if bitmap.ndim != 2 or bitmap.size == 0:
                continue
            name = str(meta.get("name") or key)
            if key.endswith("_map") and len(name) == 1 and name.isdigit():
                label = name
            else:
                label = key if key in ("선비족", "입구", "선비족입구", "-", "(", ")") else name

            entry = {
                "name": label,
                "key": key,
                "threshold": int(meta.get("threshold", 160)),
                "bitmap": (bitmap > 0).astype(np.uint8),
                "width": int(bitmap.shape[1]),
                "height": int(bitmap.shape[0]),
            }
            self.tokens.setdefault(label, []).append(entry)

        print(f"[MapOCR] loaded labels={list(self.tokens.keys())} from {self.token_file}")

    def _ingest_ahk_digits(self, digit_patterns: dict):
        """Optional: merge HUD AHK digit bitmaps as extra digit templates."""
        if not digit_patterns:
            return
        chars = "0123456789+/ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        char_map = {c: i for i, c in enumerate(chars)}

        def decode(ahk_string: str):
            try:
                star_idx = ahk_string.find("*")
                dollar_idx = ahk_string.find("$")
                threshold = 127
                if star_idx != -1 and dollar_idx != -1:
                    threshold = int(ahk_string[star_idx + 1 : dollar_idx])
                width_part = ahk_string[dollar_idx + 1 :]
                dot_idx = width_part.find(".")
                width = int(width_part[:dot_idx])
                data_str = width_part[dot_idx + 1 :]
                bits = ""
                for ch in data_str:
                    if ch not in char_map:
                        continue
                    val = char_map[ch]
                    bits += f"{val:06b}"
                if width <= 0:
                    return None
                height = max(1, len(bits) // width)
                bitmap = np.zeros((height, width), dtype=np.uint8)
                for i, b in enumerate(bits):
                    if i >= height * width:
                        break
                    if b == "1":
                        y, x = divmod(i, width)
                        bitmap[y, x] = 1
                return {"threshold": threshold, "bitmap": bitmap, "width": width}
            except Exception:
                return None

        for digit, pattern in digit_patterns.items():
            items = pattern if isinstance(pattern, list) else [pattern]
            for i, p in enumerate(items):
                decoded = decode(p)
                if not decoded:
                    continue
                bmp = decoded["bitmap"]
                entry = {
                    "name": str(digit),
                    "key": f"ahk_{digit}_{i}",
                    "threshold": int(decoded["threshold"]),
                    "bitmap": (bmp > 0).astype(np.uint8),
                    "width": int(decoded["width"]),
                    "height": int(bmp.shape[0]),
                }
                self.tokens.setdefault(str(digit), []).append(entry)

    @staticmethod
    def _gray_u8(crop_rgb: np.ndarray) -> np.ndarray:
        # Token builder used simple RGB mean > thr.
        return crop_rgb.mean(axis=2).astype(np.uint8)

    @staticmethod
    def _binarize_u8(gray_u8: np.ndarray, thr: int) -> np.ndarray:
        return (gray_u8 >= int(thr)).astype(np.uint8)

    def _best_match(
        self,
        screen_bin: np.ndarray,
        templates: List[dict],
        x_min: int = 0,
        x_max: Optional[int] = None,
        min_score: float = 0.72,
    ) -> Optional[Tuple[dict, int, int, float]]:
        if not templates:
            return None
        h, w = screen_bin.shape
        if x_max is None:
            x_max = w
        x0 = max(0, int(x_min))
        x1 = min(w, int(x_max))
        if x1 <= x0:
            return None

        roi = screen_bin[:, x0:x1]
        roi_f = roi.astype(np.float32)
        inv_f = (1 - roi).astype(np.float32)
        best = None

        for entry in templates:
            tmpl = entry["bitmap"]
            th, tw = tmpl.shape
            if th > roi.shape[0] or tw > roi.shape[1]:
                continue
            t1 = float(np.sum(tmpl))
            t0 = float(th * tw - t1)
            if t1 <= 0:
                continue
            s1 = cv2.matchTemplate(roi_f, tmpl.astype(np.float32), cv2.TM_CCORR) / max(1.0, t1)
            if t0 > 0:
                s0 = cv2.matchTemplate(inv_f, (1 - tmpl).astype(np.float32), cv2.TM_CCORR) / t0
                score_map = np.minimum(s1, s0)
            else:
                score_map = s1
            _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(score_map)
            sc = float(max_val)
            if sc < min_score:
                continue
            mx = x0 + int(max_loc[0])
            my = int(max_loc[1])
            if best is None or sc > best[3]:
                best = (entry, mx, my, sc)
        return best

    def _best_match_scaled(
        self,
        screen_bin: np.ndarray,
        templates: List[dict],
        scales: Tuple[float, ...] = (0.90, 0.95, 1.0, 1.05, 1.10, 1.15),
        min_score: float = 0.70,
    ) -> Optional[Tuple[dict, int, int, float, float]]:
        """Return (entry, x, y, score, scale)."""
        best = None
        h, w = screen_bin.shape
        for scale in scales:
            scaled_templates = []
            for entry in templates:
                bmp = entry["bitmap"]
                nh = max(1, int(round(bmp.shape[0] * scale)))
                nw = max(1, int(round(bmp.shape[1] * scale)))
                if nh >= h or nw >= w:
                    continue
                if scale == 1.0:
                    scaled = bmp
                else:
                    scaled = cv2.resize(bmp, (nw, nh), interpolation=cv2.INTER_NEAREST)
                    scaled = (scaled > 0).astype(np.uint8)
                scaled_templates.append(
                    {**entry, "bitmap": scaled, "width": nw, "height": nh, "_scale": scale}
                )
            hit = self._best_match(screen_bin, scaled_templates, min_score=min_score)
            if hit is None:
                continue
            entry, x, y, sc = hit
            scale_used = float(entry.get("_scale", scale))
            if best is None or sc > best[3]:
                best = (entry, x, y, sc, scale_used)
        return best

    def _digit_template_groups(self) -> Tuple[List[dict], List[dict]]:
        """Map-native digits first, AHK HUD digits as fallback."""
        map_digits: List[dict] = []
        ahk_digits: List[dict] = []
        for lab in (str(d) for d in range(10)):
            for e in self.tokens.get(lab, []):
                key = str(e.get("key", ""))
                if key.startswith("ahk_"):
                    ahk_digits.append(e)
                else:
                    map_digits.append(e)
        return map_digits, ahk_digits

    def _greedy_scan_suffix(self, screen_bin: np.ndarray, x_start: int) -> Tuple[str, float]:
        """Scan remaining area for 입구 / digits / - ( )."""
        h, w = screen_bin.shape
        x = max(0, x_start)
        out = []
        scores = []

        entrance = self._best_match(
            screen_bin,
            self.tokens.get("입구", []),
            x_min=x,
            x_max=min(w, x + 80),
            min_score=0.75,
        )
        if entrance and entrance[1] <= x + 12:
            _entry, _mx, _my, sc = entrance
            return "입구", sc

        symbol_templates = [e for lab in ("-", "(", ")") for e in self.tokens.get(lab, [])]
        map_digits, ahk_digits = self._digit_template_groups()

        guard = 0
        while x < w - 2 and guard < 24:
            guard += 1
            col = screen_bin[:, x]
            if int(col.sum()) == 0:
                x += 1
                continue

            x0 = max(0, x - 1)
            x1 = min(w, x + 20)

            # 1) map digits + symbols (native scale, fast)
            local = self._best_match(
                screen_bin,
                symbol_templates + map_digits,
                x_min=x0,
                x_max=x1,
                min_score=0.62,
            )
            # 2) AHK digits native
            if local is None and ahk_digits:
                local = self._best_match(
                    screen_bin,
                    ahk_digits,
                    x_min=x0,
                    x_max=x1,
                    min_score=0.62,
                )
            # 3) small scale sweep only as last resort (map digits)
            if local is None and map_digits:
                strip = screen_bin[:, x0 : min(w, x + 24)]
                scaled = self._best_match_scaled(
                    strip,
                    map_digits,
                    scales=(1.0, 1.05, 0.95),
                    min_score=0.58,
                )
                if scaled is not None:
                    entry, mx_rel, my, sc, _scale = scaled
                    local = (entry, x0 + mx_rel, my, sc)
            if local is None:
                local = self._best_match(
                    screen_bin,
                    symbol_templates,
                    x_min=x0,
                    x_max=min(w, x + 14),
                    min_score=0.50,
                )
            if local is None:
                x += 1
                continue
            entry, mx, _my, sc = local
            if mx > x + 10:
                x += 1
                continue
            out.append(entry["name"])
            scores.append(sc)
            x = mx + max(2, entry["width"] - 1)

        text = "".join(out)
        avg = float(sum(scores) / len(scores)) if scores else 0.0
        return text, avg

    def recognize(self, crop_rgb: np.ndarray) -> str:
        self.last_score = 0.0
        self.last_raw = ""
        self.last_debug = {}
        if crop_rgb is None or getattr(crop_rgb, "size", 0) == 0:
            return ""
        if crop_rgb.ndim != 3 or crop_rgb.shape[2] < 3:
            return ""

        gray = self._gray_u8(crop_rgb)
        # Prefer last successful threshold first, then nearby values only.
        base = int(getattr(self, "_last_thr", 160) or 160)
        thr_list = [base, base - 10, base + 10, 160, 150, 170]
        seen = set()
        thr_ordered = []
        for thr in thr_list:
            if thr < 100 or thr > 210 or thr in seen:
                continue
            seen.add(thr)
            thr_ordered.append(thr)

        best_text = ""
        best_score = -1.0
        best_prefix_score = 0.0
        best_fg = 0
        best_thr = base

        for thr_i, thr in enumerate(thr_ordered):
            screen_bin = self._binarize_u8(gray, thr)
            fg = int(screen_bin.sum())
            if fg < 20:
                continue
            if fg > best_fg:
                best_fg = fg

            # First threshold: native scale only. Expand scales only if needed.
            scales = (1.0,) if thr_i == 0 else (1.0, 1.05, 0.95)

            # Fast path: full entrance
            full = self._best_match_scaled(
                screen_bin,
                self.tokens.get("선비족입구", []),
                scales=scales,
                min_score=0.80,
            )
            if full and full[3] >= 0.90:
                self._last_thr = thr
                self.last_raw = "선비족입구"
                self.last_score = float(full[3])
                self.last_debug = {
                    "mean": float(gray.mean()),
                    "max": int(gray.max()),
                    "fg": fg,
                    "prefix_score": float(full[3]),
                    "shape": (int(crop_rgb.shape[0]), int(crop_rgb.shape[1])),
                    "thr": thr,
                }
                return "선비족입구"

            prefix_hit = self._best_match_scaled(
                screen_bin,
                self.tokens.get("선비족", []),
                scales=scales,
                min_score=0.72,
            )
            if not prefix_hit and thr_i == 0:
                # retry same thr with slight scale sweep before next thr
                prefix_hit = self._best_match_scaled(
                    screen_bin,
                    self.tokens.get("선비족", []),
                    scales=(1.0, 1.05, 0.95),
                    min_score=0.72,
                )
            if not prefix_hit:
                weak = self._best_match(
                    screen_bin,
                    self.tokens.get("선비족", []),
                    min_score=0.40,
                )
                if weak:
                    best_prefix_score = max(best_prefix_score, weak[3])
                continue

            entry, px, _py, psc, _scale = prefix_hit
            best_prefix_score = max(best_prefix_score, psc)
            suffix, ssc = self._greedy_scan_suffix(screen_bin, px + entry["width"] - 2)
            text = "선비족" + suffix
            score = (psc * 0.6) + (ssc * 0.4) if suffix else psc * 0.5
            if is_valid_seonbijok_map(text) and score > best_score:
                best_text, best_score, best_thr = text, score, thr
                # High-confidence early exit
                if score >= 0.90 and psc >= 0.90:
                    break

        if best_text:
            self._last_thr = best_thr

        self.last_raw = best_text
        self.last_score = float(best_score if best_score > 0 else 0.0)
        self.last_debug = {
            "mean": float(gray.mean()),
            "max": int(gray.max()),
            "fg": best_fg,
            "prefix_score": float(best_prefix_score),
            "shape": (int(crop_rgb.shape[0]), int(crop_rgb.shape[1])),
            "thr": int(self._last_thr),
            "suffix_raw": best_text[3:] if best_text.startswith("선비족") else "",
        }
        return best_text

    def recognize_with_state(self, crop_rgb: np.ndarray) -> dict:
        text = self.recognize(crop_rgb)
        name, floor = split_map_name_floor(text)
        return {
            "map_info_text": text,
            "current_map": text,
            "map_name": name,
            "map_floor": floor,
            "current_floor": floor,
            "score": self.last_score,
        }


def append_token_from_image(
    name: str,
    pil_image,
    token_file: Optional[str] = None,
    threshold: int = 160,
) -> str:
    """
    Save/overwrite a token bitmap into map_tokens.json from a PIL crop.
    Returns absolute path of token file.
    """
    path = token_file or TOKEN_FILE_DEFAULT
    os.makedirs(os.path.dirname(path), exist_ok=True)
    arr = np.array(pil_image.convert("RGB"))
    gray = arr.mean(axis=2)
    mask = (gray > threshold).astype(np.uint8)
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    if rows.size == 0 or cols.size == 0:
        raise ValueError("no bright pixels for token")
    bitmap = mask[rows[0] : rows[-1] + 1, cols[0] : cols[-1] + 1]

    data = {"version": 1, "tokens": {}}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("tokens", {})

    data["tokens"][name] = {
        "name": name,
        "threshold": int(threshold),
        "width": int(bitmap.shape[1]),
        "height": int(bitmap.shape[0]),
        "bitmap": bitmap.tolist(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path
