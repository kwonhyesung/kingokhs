# -*- coding: utf-8 -*-
"""Extract missing map digit tokens from debug fail crop and speed-related helpers."""
from pathlib import Path
import json
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
FAIL = ROOT / "temple" / "maps" / "debug" / "map_info_last_fail.png"
TOKEN_JSON = ROOT / "temple" / "maps" / "map_tokens.json"
OUT_SAMPLE = ROOT / "temple" / "maps" / "samples" / "seonbijok_2.png"


def main():
    im = Image.open(FAIL).convert("RGB")
    arr = np.array(im)
    OUT_SAMPLE.parent.mkdir(parents=True, exist_ok=True)
    im.save(OUT_SAMPLE)

    gray = arr.mean(axis=2)
    mask = (gray > 160).astype(np.uint8)
    col = mask.any(axis=0)
    # column segments
    segs = []
    start = None
    for i, v in enumerate(col):
        if v and start is None:
            start = i
        elif not v and start is not None:
            segs.append((start, i - 1))
            start = None
    if start is not None:
        segs.append((start, len(col) - 1))
    print("segs", [(a, b, b - a + 1) for a, b in segs])

    # last substantial segment should be digit 2
    digit_segs = [(a, b) for a, b in segs if (b - a + 1) >= 6]
    print("digit_segs", digit_segs)
    if not digit_segs:
        raise SystemExit("no digit segment")
    x0, x1 = digit_segs[-1]
    # pad a bit like other map digits
    ys = np.where(mask[:, x0 : x1 + 1].any(axis=1))[0]
    y0 = max(0, int(ys[0]) - 2)
    y1 = min(mask.shape[0], int(ys[-1]) + 1 + 2)
    bitmap = mask[y0:y1, x0 : x1 + 1].copy()
    print("digit2", bitmap.shape, "fg", int(bitmap.sum()))

    data = json.loads(TOKEN_JSON.read_text(encoding="utf-8"))
    data.setdefault("tokens", {})
    data["tokens"]["2_map"] = {
        "name": "2",
        "threshold": 160,
        "width": int(bitmap.shape[1]),
        "height": int(bitmap.shape[0]),
        "bitmap": bitmap.tolist(),
    }
    TOKEN_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("updated", TOKEN_JSON)


if __name__ == "__main__":
    main()
