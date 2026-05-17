SELF_HP_EMERGENCY_THRESHOLD = 50000
SELF_MP_PRIORITY_THRESHOLD = 40000
ZERO_HP_CONFIRMATIONS_REQUIRED = 2


def follow_manhattan_gap(target_x: int, target_y: int, current_x: int, current_y: int) -> int:
    return abs(int(target_x) - int(current_x)) + abs(int(target_y) - int(current_y))


def should_hold_follow_position(target_x: int, target_y: int, current_x: int, current_y: int) -> bool:
    return follow_manhattan_gap(target_x, target_y, current_x, current_y) <= 1


def should_trigger_self_hp_emergency(current_hp: int, threshold: int = SELF_HP_EMERGENCY_THRESHOLD) -> bool:
    current_hp = max(0, int(current_hp))
    return 0 < current_hp <= int(threshold)


def should_prioritize_self_mp(current_mp: int, threshold: int = SELF_MP_PRIORITY_THRESHOLD) -> bool:
    current_mp = max(0, int(current_mp))
    return current_mp < int(threshold)


def should_allow_party_support_cast(red_tab_confirmed: bool) -> bool:
    return bool(red_tab_confirmed)


def build_warrior_search_sequence(direction: str) -> tuple[str, str, str, str]:
    return ("esc", "tab", str(direction), "enter")


def confirm_support_lock_by_hp_gain(before_hp: int, after_hp: int) -> bool:
    return int(after_hp) > int(before_hp)


def next_zero_hp_count(previous_count: int, current_hp: int) -> int:
    current_hp = int(current_hp)
    if current_hp <= 0:
        return max(0, int(previous_count)) + 1
    return 0


def is_confirmed_zero_hp_state(
    zero_hp_count: int,
    required_count: int = ZERO_HP_CONFIRMATIONS_REQUIRED,
) -> bool:
    return int(zero_hp_count) >= max(1, int(required_count))
