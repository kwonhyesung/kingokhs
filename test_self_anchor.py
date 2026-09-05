"""자기 위치(my_screen_pos)는 격수만 필요하다는 계약을 못 박는다.

이 좌표는 몬스터/아이템의 월드 좌표를 역계산하는 데만 쓰이고, 그건 격수의
사냥/줍기 사이클(_run_warrior_attack_loot_cycle) 전용이다. 도사는 격수를
원격 텔레메트리 좌표로 따라가고 힐은 이름표를 화면에서 직접 클릭한다.

회귀: 표에 도사(졈프)를 넣어놨더니 존재하지도 않는 졈프_left/right/back/top을
매 프레임 뒤지며 "캐릭터 패턴 2초 이상 미검출"만 쌓았다.
"""
import io, json

ROLES = json.load(io.open("pc_roles.json", encoding="utf-8"))
PARTY = set(json.load(io.open("findtext_patterns.json", encoding="utf-8"))["party"])
DIRS = ("left", "right", "back", "top")


def demo():
    table = ROLES["self_pattern_by_role"]
    # 자기 위치를 쓰는 역할은 격수뿐이다
    assert set(table) == {"격수"}, table

    # 표에 올린 역할은 방향 패턴이 실제로 저장돼 있어야 한다 -
    # 없으면 조용히 (0,0)으로 남아 사냥/줍기가 통째로 멈춘다.
    for role, pattern in table.items():
        have = {f"{pattern}_{d}" for d in DIRS} & PARTY
        assert have == {f"{pattern}_{d}" for d in DIRS}, (role, pattern, sorted(have))

    # 도사 패턴은 방향 패턴이 없다 - 그래서 표에 넣으면 안 된다는 게 요점이다
    assert not {f"졈프_{d}" for d in DIRS} & PARTY
    print("ok")


if __name__ == "__main__":
    demo()
