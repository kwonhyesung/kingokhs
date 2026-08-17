"""
findtext_wrapper.py 자체 검증 (assert 기반, pytest 불필요).
게임/화면 캡처 없이 합성 이미지로 매칭 알고리즘과 인코딩 왕복을 확인한다.
실행: py test_findtext_wrapper.py
"""
import json
import numpy as np

from findtext_wrapper import get_findtext


def load_reference_pattern():
    with open("temple/patterns.json", encoding="utf-8") as f:
        data = json.load(f)
    raw = data["0"]
    if isinstance(raw, dict):
        raw = raw["ahk_pattern"]
    if isinstance(raw, list):
        raw = raw[0]
    return raw


def build_synthetic_roi(bitmap: np.ndarray, offset_x: int, offset_y: int, pad: int = 3):
    h, w = bitmap.shape
    roi = np.full((h + 2 * pad, w + 2 * pad, 3), 255, dtype=np.uint8)  # 배경: 흰색 -> bin=0
    fg = bitmap > 0
    roi_region = roi[offset_y:offset_y + h, offset_x:offset_x + w]
    roi_region[fg] = (0, 0, 0)  # 전경: 검정 -> bin=1
    return roi


def build_synthetic_color_roi(bitmap: np.ndarray, offset_x: int, offset_y: int,
                               ink_bgr, bg_bgr=(255, 255, 255), pad: int = 3):
    """color 모드 테스트용: '잉크' 픽셀을 지정한 BGR 색으로 칠한 합성 ROI."""
    h, w = bitmap.shape
    roi = np.full((h + 2 * pad, w + 2 * pad, 3), bg_bgr, dtype=np.uint8)
    fg = bitmap > 0
    roi_region = roi[offset_y:offset_y + h, offset_x:offset_x + w]
    roi_region[fg] = ink_bgr
    return roi


def test_match_finds_exact_position():
    ft = get_findtext()
    raw = load_reference_pattern()
    pat = ft._parse_single_pattern(raw)
    assert pat is not None, "패턴 파싱 실패"

    ox, oy = 4, 2
    roi = build_synthetic_roi(pat["bitmap"], ox, oy)

    results = ft._template_match_native(
        roi, {"bitmap": pat["bitmap"], "color": pat["color"], "comment": "0"},
        err1=0.0, err0=0.0, find_all=True,
    )
    assert len(results) == 1, f"정확히 1개 매치를 기대했으나 {len(results)}개"
    r = results[0]
    assert (r["x"], r["y"], r["w"], r["h"]) == (ox, oy, pat["width"], pat["height"]), r


def test_no_match_when_absent():
    ft = get_findtext()
    raw = load_reference_pattern()
    pat = ft._parse_single_pattern(raw)
    blank = np.full((pat["height"] + 6, pat["width"] + 6, 3), 255, dtype=np.uint8)

    results = ft._template_match_native(
        blank, {"bitmap": pat["bitmap"], "color": pat["color"], "comment": "0"},
        err1=0.0, err0=0.0, find_all=True,
    )
    assert results == [], f"빈 화면인데 매치 발생: {results}"


def test_base64_roundtrip():
    ft = get_findtext()
    bits = "1101001011110000101"
    encoded = ft.bit2base64(bits)
    decoded = ft.base64tobit(encoded)
    # base64tobit은 끝의 정지비트(10*)를 제거하므로, 원본이 '1'로 끝나지 않으면
    # 정확히 같고, '1'로 끝나면 그 비트까지만 남는다. 여기 테스트 비트열은 '0'으로 끝남.
    assert decoded == bits, f"왕복 불일치: {bits} -> {encoded} -> {decoded}"


def test_capture_pattern_roundtrip():
    ft = get_findtext()
    raw = load_reference_pattern()
    pat = ft._parse_single_pattern(raw)

    roi = build_synthetic_roi(pat["bitmap"], 0, 0, pad=0)
    # cut=False: 이 테스트는 인코딩 왕복(bit-fidelity)만 확인. 크롭 동작은
    # test_auto_crop_margins에서 따로 검증한다.
    pattern_str = ft.capture_pattern(roi, threshold=pat["color"], comment="rt", cut=False)
    reparsed = ft._parse_single_pattern(pattern_str)

    assert reparsed["width"] == pat["width"]
    assert reparsed["height"] == pat["height"]
    assert np.array_equal(reparsed["bitmap"], pat["bitmap"]), "캡처 왕복 후 비트맵 불일치"


def test_find_text_public_api_with_real_pattern():
    """
    실제 temple/patterns.json의 원본 문자열(선행 '|' 포함, ft.ahk 표준 형식)을
    공개 API find_text()에 그대로 넣었을 때 threshold/comment가 깨지지 않고
    올바른 위치를 찾는지 확인 (leading-pipe 파싱 버그 회귀 테스트).
    """
    ft = get_findtext()
    raw = load_reference_pattern()  # 예: "|<0>*117$18.zzzz..." (선행 파이프 포함)
    pat = ft._parse_single_pattern(raw[1:])
    assert pat["color"] == 117, f"임계값 파싱이 깨짐: {pat['color']}"
    assert pat["comment"] == "0", f"comment 파싱이 깨짐: {pat['comment']!r}"

    roi = build_synthetic_roi(pat["bitmap"], 5, 3)
    results = ft.find_text(raw, 0, 0, 0, 0, err1=0.0, err0=0.0, screenshot=roi, find_all=True)
    assert len(results) == 1, f"find_text 결과 개수 불일치: {results}"
    assert (results[0]["x"], results[0]["y"]) == (5, 3), results[0]
    assert results[0]["id"] == "0", results[0]


def test_auto_crop_margins():
    """ft.ahk GetTextFromScreen()의 CutUp/CutDown 여백 자동 크롭 이식 검증."""
    ft = get_findtext()
    w = 5
    rows = ["00000", "00000", "01110", "01110", "11111", "11111"]
    bits = "".join(rows)
    cropped, cut_up, cut_down = ft.auto_crop_margins(bits, w)
    # 위에서 균일한(전부 0) 2줄, 아래에서 균일한(전부 1) 2줄이 잘려나가야 함
    assert cut_up == 2, cut_up
    assert cut_down == 2, cut_down
    assert cropped == "0111001110", cropped

    # 완전 단색 ROI는 전부 잘려서 빈 문자열이 됨 -> capture_pattern은 원본 유지
    blank_roi = np.full((6, 6, 3), 255, dtype=np.uint8)
    pattern_str = ft.capture_pattern(blank_roi, threshold=127, comment="blank", cut=True)
    reparsed = ft._parse_single_pattern(pattern_str)
    assert reparsed["height"] == 6, "완전 단색 ROI는 크롭을 포기하고 원본 높이를 유지해야 함"


def test_color_pattern_match_correct_color():
    """모양 + 지정 색상이 둘 다 맞으면 매치되어야 함 (mode='color')."""
    ft = get_findtext()
    raw = load_reference_pattern()
    pat = ft._parse_single_pattern(raw[1:])  # 모양(shape)만 재사용

    blue_bgr = (235, 64, 52)  # BGR -> R=52,G=64,B=235 (파란 캐릭터라고 가정)
    roi = build_synthetic_color_roi(pat["bitmap"], 6, 4, ink_bgr=blue_bgr)

    # capture_pattern_color는 ROI에서 모양(shape)을 threshold로 새로 추출하므로,
    # 이미 알고 있는 원본 숫자 모양을 그대로 쓰기 위해 여기선 패턴 문자열을
    # 직접 조립해서 파싱/매칭 경로를 검증한다.
    shape_bits = "".join("1" if v else "0" for v in pat["bitmap"].flatten())
    pattern_str = f"|<blue>@3440EB~10${pat['width']}.{ft.bit2base64(shape_bits)}"

    parsed = ft._parse_single_pattern(pattern_str[1:])
    assert parsed["mode"] == "color"
    assert parsed["colors"] == [(0x34, 0x40, 0xEB, 10)]

    results = ft.find_text(pattern_str, 0, 0, 0, 0, err1=0.0, err0=0.0,
                            screenshot=roi, find_all=True)
    assert len(results) == 1, results
    assert (results[0]["x"], results[0]["y"]) == (6, 4), results[0]
    assert results[0]["id"] == "blue"


def test_color_pattern_no_match_wrong_color():
    """모양은 같아도 색상이 허용오차 밖이면 매치되면 안 됨."""
    ft = get_findtext()
    raw = load_reference_pattern()
    pat = ft._parse_single_pattern(raw[1:])

    red_bgr = (30, 30, 220)  # 실제 색은 빨강(R=220)
    roi = build_synthetic_color_roi(pat["bitmap"], 6, 4, ink_bgr=red_bgr)

    shape_bits = "".join("1" if v else "0" for v in pat["bitmap"].flatten())
    # 패턴은 파랑(3440EB)을 찾도록 만듦 -> 빨간 화면에서는 안 나와야 함
    pattern_str = f"|<blue>@3440EB~10${pat['width']}.{ft.bit2base64(shape_bits)}"

    results = ft.find_text(pattern_str, 0, 0, 0, 0, err1=0.0, err0=0.0,
                            screenshot=roi, find_all=True)
    assert results == [], results


def test_color_pattern_multi_color_or():
    """한 항목에 색상 후보 여러 개(OR) -> 그중 하나만 있어도 매치."""
    ft = get_findtext()
    raw = load_reference_pattern()
    pat = ft._parse_single_pattern(raw[1:])
    shape_bits = "".join("1" if v else "0" for v in pat["bitmap"].flatten())

    green_bgr = (60, 200, 60)  # R=60,G=200,B=60
    roi = build_synthetic_color_roi(pat["bitmap"], 2, 2, ink_bgr=green_bgr)

    # 파랑 또는 초록 중 하나면 매치 (OR)
    pattern_str = f"|<multi>@3440EB~10,3CC83C~10${pat['width']}.{ft.bit2base64(shape_bits)}"
    results = ft.find_text(pattern_str, 0, 0, 0, 0, err1=0.0, err0=0.0,
                            screenshot=roi, find_all=True)
    assert len(results) == 1, results


def test_capture_pattern_color_roundtrip():
    """capture_pattern_color()로 만든 패턴이 원래 위치를 정확히 다시 찾아야 함."""
    ft = get_findtext()
    raw = load_reference_pattern()
    pat = ft._parse_single_pattern(raw[1:])

    orange_bgr = (10, 140, 235)  # R=235,G=140,B=10
    roi = build_synthetic_color_roi(pat["bitmap"], 0, 0, ink_bgr=orange_bgr, pad=0)

    # threshold는 auto(None)로 둔다: pat["color"](117)는 원본 흑백 숫자 패턴 기준
    # 임계값이라 이 합성 주황색 ROI의 실제 밝기와는 무관 — 모양 추출은 자동 임계값이
    # 담당하고, 색상 판정만 지정한 (235,140,10)이 담당하는 구조를 그대로 검증한다.
    pattern_str = ft.capture_pattern_color(
        roi, colors=[(235, 140, 10, 8)], comment="orange", cut=False,
    )
    assert pattern_str.startswith("|<orange>@EB8C0A~8$") or "~8$" in pattern_str

    results = ft.find_text(pattern_str, 0, 0, 0, 0, err1=0.0, err0=0.0,
                            screenshot=roi, find_all=True)
    assert len(results) == 1, results
    assert results[0]["id"] == "orange"


def test_multi_pattern_color_tagged_search():
    """동일 모양 + 서로 다른 색 두 개를 '|'로 묶어 한 번에 검색 -> id로 구분."""
    ft = get_findtext()
    raw = load_reference_pattern()
    pat = ft._parse_single_pattern(raw[1:])
    shape_bits = "".join("1" if v else "0" for v in pat["bitmap"].flatten())
    b64 = ft.bit2base64(shape_bits)

    blue_roi = build_synthetic_color_roi(pat["bitmap"], 5, 5, ink_bgr=(235, 64, 52), pad=10)
    combined = (
        f"|<blue>@3440EB~10${pat['width']}.{b64}"
        f"|<red>@E8342C~10${pat['width']}.{b64}"
    )

    results = ft.find_text(combined, 0, 0, 0, 0, err1=0.0, err0=0.0,
                            screenshot=blue_roi, find_all=True)
    ids = {r["id"] for r in results}
    assert ids == {"blue"}, f"파란 화면인데 {ids}가 매치됨"


def test_auto_threshold_reasonable():
    ft = get_findtext()
    raw = load_reference_pattern()
    pat = ft._parse_single_pattern(raw)
    roi = build_synthetic_roi(pat["bitmap"], 0, 0, pad=0)
    thr = ft.auto_threshold(roi)
    assert 0 <= thr <= 255, thr


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK  {t.__name__}")
    print(f"\n{len(tests)}개 테스트 통과")
