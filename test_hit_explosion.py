"""히트가 폭발한 프레임에서만 오차 0으로 다시 거른다 (평소엔 건드리지 않는다).

폭발하지 않는 프레임에서까지 작은 패턴을 미리 엄격하게 만들면, 격수가 자기
위치를 찾는 점프_right/top 같은 패턴을 멀쩡한 상황에서도 죽이게 된다.
"""
import numpy as np
from findtext_wrapper import get_findtext

ft = get_findtext()


def _scene(pattern_bits, w, h, extra_noise):
    """패턴을 한 번 심고, 잡음을 원하는 만큼 깐 흑백 화면."""
    canvas = np.full((300, 400), 255, np.uint8)
    if extra_noise:
        rng = np.random.default_rng(0)
        canvas = rng.choice([0, 255], size=canvas.shape, p=[0.5, 0.5]).astype(np.uint8)
    img = np.where(np.array(pattern_bits).reshape(h, w) == 1, 0, 255).astype(np.uint8)
    canvas[20:20 + h, 20:20 + w] = img
    return np.dstack([canvas] * 3)


def demo():
    # 3x3 전부 잉크인 아주 작은 패턴 = 잡음 화면에서 폭발한다
    bits = [1] * 9
    text = "|<tiny>*128$3." + ft.bit2base64("".join(map(str, bits)))

    quiet = _scene(bits, 3, 3, extra_noise=False)
    hits = ft.find_text(text, screenshot=quiet, err1=0.10, err0=0.10, find_all=True)
    assert len(hits) == 1, f"조용한 화면에서는 그대로 찾아야 한다: {len(hits)}"

    noisy = _scene(bits, 3, 3, extra_noise=True)
    hits2 = ft.find_text(text, screenshot=noisy, err1=0.10, err0=0.10, find_all=True)
    # 폭발한 프레임: 오차 0 재시도로 줄거나, 그래도 많으면 버린다.
    assert len(hits2) <= ft.MAX_RAW_HITS, f"폭발을 그대로 흘려보냈다: {len(hits2)}"
    print(f"ok (조용={len(hits)}건, 잡음={len(hits2)}건, 상한={ft.MAX_RAW_HITS})")


if __name__ == "__main__":
    demo()
