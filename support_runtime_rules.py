SELF_HP_EMERGENCY_THRESHOLD = 50000
SELF_MP_PRIORITY_THRESHOLD = 50000
ZERO_HP_CONFIRMATIONS_REQUIRED = 2


def follow_manhattan_gap(target_x: int, target_y: int, current_x: int, current_y: int) -> int:
    return abs(int(target_x) - int(current_x)) + abs(int(target_y) - int(current_y))


def should_prioritize_follow_distance(gap: int, risk_distance: int = 7) -> bool:
    return max(0, int(gap)) >= max(1, int(risk_distance))


def should_detect_warrior_transition(prev_x: int, prev_y: int, cur_x: int, cur_y: int, jump_distance: int = 12) -> bool:
    return follow_manhattan_gap(prev_x, prev_y, cur_x, cur_y) >= max(1, int(jump_distance))


def should_hold_follow_gap(gap: int, hold_distance: int = 1) -> bool:
    return max(0, int(gap)) <= max(0, int(hold_distance))


def adjust_follow_target_by_axis_gap(
    target_x: int,
    target_y: int,
    current_x: int,
    current_y: int,
    warrior_x: int,
    warrior_y: int,
    min_axis_gap: int = 1,
) -> tuple[int, int]:
    min_gap = max(0, int(min_axis_gap))
    adjusted_x = int(target_x)
    adjusted_y = int(target_y)
    if abs(int(warrior_x) - int(current_x)) <= min_gap:
        adjusted_x = int(current_x)
    if abs(int(warrior_y) - int(current_y)) <= min_gap:
        adjusted_y = int(current_y)
    return adjusted_x, adjusted_y


def should_allow_follow_navigation(
    nav_follow_enabled: bool,
    has_follow_target: bool,
    is_connected: bool = False,
) -> bool:
    return bool(nav_follow_enabled) and (bool(has_follow_target) or bool(is_connected))


def speed_up_delay(delay_sec: float, factor: float = 2.0) -> float:
    factor = max(0.1, float(factor or 1.0))
    return round(max(0.0, float(delay_sec or 0.0)) / factor, 4)


def build_f5_hon_sequence(repeat_count: int = 5) -> tuple[str, ...]:
    sequence: list[str] = []
    for _ in range(max(1, int(repeat_count or 1))):
        sequence.extend(("esc", "6", "up", "enter"))
    sequence.extend(("esc", "tab", "tab"))
    return tuple(sequence)


def should_hold_follow_position(target_x: int, target_y: int, current_x: int, current_y: int) -> bool:
    return should_hold_follow_gap(
        follow_manhattan_gap(target_x, target_y, current_x, current_y),
        hold_distance=1,
    )


def should_trigger_self_hp_emergency(current_hp: int, threshold: int = SELF_HP_EMERGENCY_THRESHOLD) -> bool:
    current_hp = max(0, int(current_hp))
    return 0 < current_hp <= int(threshold)


def should_continue_self_hp_recovery(
    current_hp: int,
    good_hp: int,
    emergency_threshold: int = SELF_HP_EMERGENCY_THRESHOLD,
) -> bool:
    current_hp = max(0, int(current_hp))
    good_hp = max(0, int(good_hp))
    emergency_threshold = max(1, int(emergency_threshold))
    target_hp = good_hp if good_hp > 0 else (emergency_threshold + 7000)
    return 0 < current_hp < target_hp


def should_prioritize_self_mp(current_mp: int, threshold: int = SELF_MP_PRIORITY_THRESHOLD) -> bool:
    current_mp = max(0, int(current_mp))
    return current_mp <= int(threshold)


def should_prioritize_self_mp_over_party_heal(
    current_mp: int,
    warrior_hp: int,
    mp_threshold: int = SELF_MP_PRIORITY_THRESHOLD,
    warrior_critical_hp: int = 30000,
) -> bool:
    return should_prioritize_self_mp(current_mp, mp_threshold)


def should_cast_periodic_heewon(
    elapsed_sec: float,
    interval_sec: float = 16.0,
) -> bool:
    return float(elapsed_sec) >= max(0.0, float(interval_sec))


def should_ignore_monster_combat_for_support_autohunt(
    role: str,
    service_active: bool,
    nav_follow_enabled: bool,
) -> bool:
    normalized_role = str(role or "").strip()
    return normalized_role == "도사" and bool(service_active) and bool(nav_follow_enabled)


def should_defer_stuck_escape_for_support(
    role: str,
    service_active: bool,
    nav_follow_enabled: bool,
) -> bool:
    normalized_role = str(role or "").strip().lower()
    is_support_role = (
        "도사" in normalized_role
        or "dosa" in normalized_role
        or "priest" in normalized_role
    )
    return is_support_role and bool(service_active) and bool(nav_follow_enabled)


def should_retarget_after_support_follow_stuck(
    consecutive_soft_stucks: int,
    max_soft_stucks_before_retarget: int = 1,
) -> bool:
    return int(consecutive_soft_stucks) >= max(0, int(max_soft_stucks_before_retarget))


def should_allow_party_support_cast(red_tab_confirmed: bool) -> bool:
    return bool(red_tab_confirmed)


def should_block_party_heal(
    retarget_active: bool,
    now_sec: float,
    blocked_until_sec: float,
) -> bool:
    return bool(retarget_active) or float(now_sec) < float(blocked_until_sec or 0.0)


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
