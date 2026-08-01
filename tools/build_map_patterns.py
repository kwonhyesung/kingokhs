"""Build FindText-style map tokens from provided sample screenshots."""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "temple" / "maps"
OUT_JSON = OUT_DIR / "map_tokens.json"
SAMPLES_DIR = OUT_DIR / "samples"

ASSETS = [
    (
        "seonbijok_entrance",
        Path(r"C:\Users\kwon\.cursor\projects\c-Users-kwon-Desktop-0416-1\assets")
        / "c__Users_kwon_AppData_Roaming_Cursor_User_workspaceStorage_8cf0939215353b360bd991176e7118fc_images_image-4ec1ff02-6ac5-43dd-b7e3-cef9584787f6.png",
    ),
    (
        "seonbijok_single",
        Path(r"C:\Users\kwon\.cursor\projects\c-Users-kwon-Desktop-0416-1\assets")
        / "c__Users_kwon_AppData_Roaming_Cursor_User_workspaceStorage_8cf0939215353b360bd991176e7118fc_images_image-e40de4ea-a45e-4170-8fc2-3b1fd11c371a.png",
    ),
    (
        "seonbijok_compound",
        Path(r"C:\Users\kwon\.cursor\projects\c-Users-kwon-Desktop-0416-1\assets")
        / "c__Users_kwon_AppData_Roaming_Cursor_User_workspaceStorage_8cf0939215353b360bd991176e7118fc_images_image-1967a0ee-f31d-4d9f-aa12-562e2c19ac98.png",
    ),
]


def to_mask(arr: np.ndarray, thr: int = 160) -> np.ndarray:
    gray = arr.mean(axis=2) if arr.ndim == 3 else arr.astype(np.float32)
    return (gray > thr).astype(np.uint8)


def trim_mask(mask: np.ndarray, pad: int = 1) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    if rows.size == 0 or cols.size == 0:
        return mask, (0, 0, mask.shape[1], mask.shape[0])
    y0 = max(0, int(rows[0]) - pad)
    y1 = min(mask.shape[0], int(rows[-1]) + 1 + pad)
    x0 = max(0, int(cols[0]) - pad)
    x1 = min(mask.shape[1], int(cols[-1]) + 1 + pad)
    return mask[y0:y1, x0:x1].copy(), (x0, y0, x1, y1)


def crop_token(mask: np.ndarray, x0: int, x1: int) -> np.ndarray:
    piece = mask[:, x0:x1]
    trimmed, _ = trim_mask(piece, pad=0)
    return trimmed


def pack_token(name: str, bitmap: np.ndarray, threshold: int = 160) -> dict:
    return {
        "name": name,
        "threshold": int(threshold),
        "width": int(bitmap.shape[1]),
        "height": int(bitmap.shape[0]),
        "bitmap": bitmap.astype(np.uint8).tolist(),
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

    imgs = {}
    masks = {}
    for key, path in ASSETS:
        if not path.exists():
            raise FileNotFoundError(path)
        im = Image.open(path).convert("RGB")
        arr = np.array(im)
        imgs[key] = arr
        masks[key] = to_mask(arr, 160)
        im.save(SAMPLES_DIR / f"{key}.png")

    tokens = {}

    # Image2: 선비족3 -> prefix 선비족 (cols 93..155), digit tail starts ~158
    m2 = masks["seonbijok_single"]
    prefix = crop_token(m2, 93, 156)
    tokens["선비족"] = pack_token("선비족", prefix)

    # Image1: 선비족입구 -> suffix 입구 (cols 131..172)
    m1 = masks["seonbijok_entrance"]
    entrance = crop_token(m1, 131, 172)
    tokens["입구"] = pack_token("입구", entrance)

    # Full fixed form also useful as fast path
    full_entrance = crop_token(m1, 65, 172)
    tokens["선비족입구"] = pack_token("선비족입구", full_entrance)

    # Image3 symbols / digits around compound form
    m3 = masks["seonbijok_compound"]
    # Keep vertical context for thin symbols like '-' so background pixels exist (t0>0).
    def crop_token_padded(mask: np.ndarray, x0: int, x1: int, y_pad: int = 4) -> np.ndarray:
        h = mask.shape[0]
        ys = np.where(mask[:, x0:x1].any(axis=1))[0]
        if ys.size == 0:
            return crop_token(mask, x0, x1)
        y0 = max(0, int(ys[0]) - y_pad)
        y1 = min(h, int(ys[-1]) + 1 + y_pad)
        piece = mask[y0:y1, x0:x1]
        return piece

    tokens["-"] = pack_token("-", crop_token_padded(m3, 120, 130, y_pad=6))
    tokens["("] = pack_token("(", crop_token_padded(m3, 145, 152, y_pad=2))
    tokens[")"] = pack_token(")", crop_token_padded(m3, 168, 174, y_pad=2))

    # Optional local digit overrides for map header font (may differ from HUD digits)
    tokens["3_map"] = pack_token("3", crop_token_padded(m3, 108, 119, y_pad=2))
    tokens["5_map"] = pack_token("5", crop_token_padded(m3, 131, 142, y_pad=2))
    tokens["1_map"] = pack_token("1", crop_token_padded(m3, 155, 165, y_pad=2))
    tokens["3b_map"] = pack_token("3", crop_token_padded(m2, 158, 169, y_pad=2))

    meta = {
        "version": 1,
        "note": "FindText-style binary tokens for map_info (선비족 forms)",
        "forms": ["선비족입구", "선비족{n}", "선비족{n}-{n}({n})"],
        "tokens": tokens,
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"Wrote {OUT_JSON}")
    for k, v in tokens.items():
        print(f"  {k}: {v['width']}x{v['height']} fg={int(np.array(v['bitmap']).sum())}")


if __name__ == "__main__":
    main()
