"""격수 이름표는 배경에 흔들리지 않는 '색상 패턴'이어야 한다.

실측 회귀 둘:
  1) 밝기 패턴 점프_name/점프_name_green이 졈프(도사) 글자를 오탐해서
     도사가 자기 자신을 클릭했다.
  2) 그 패턴의 60~70%는 '글자 주변 바닥'이다(글자가 밝고 잉크는 어두운 쪽).
     격수가 불 이팩트 위로 들어가자 진짜 위치에서도 25~50% 어긋나 실패했다.
색상 모드는 '1' 픽셀의 색만 보고 배경은 아예 안 본다 - 바닥이 불꽃이든
바닥이든 무관하고, 초록(파티원)/흰색(선택됨) 두 색을 OR로 담는다.
"""
import io, json

def demo():
    party = json.load(io.open("findtext_patterns.json", encoding="utf-8"))["party"]
    from svc_logic import LogicSvc
    tags = [n for n in party if n.startswith(LogicSvc._WARRIOR_NAME_PREFIX)]
    assert tags, "격수 이름표 패턴이 없다"
    for n in tags:
        assert "@" in party[n].split("$")[0], (
            f"{n}은 밝기 패턴이다 - 배경(바닥/불꽃)이 바뀌면 못 찾는다")

    print(f"ok ({tags} 전부 색상 패턴)")


if __name__ == "__main__":
    demo()
