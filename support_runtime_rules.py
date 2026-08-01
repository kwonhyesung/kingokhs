SELF_HP_EMERGENCY_THRESHOLD = 50000
SELF_MP_PRIORITY_THRESHOLD = 50000
ZERO_HP_CONFIRMATIONS_REQUIRED = 2


NETWORK_ROLE_ALIASES = {
    "priest": "도사1",
    "priest1 (hub)": "도사1",
    "priest1": "도사1",
    "priest2": "도사2",
    "warrior": "격수",
    "shaman": "술사",
    "도사": "도사1",
    "도사1": "도사1",
    "도사2": "도사2",
    "격수": "격수",
    "술사": "술사",
}


def normalize_network_role(role: object, default: str = "도사1") -> str:
    value = str(role or "").strip()
    return NETWORK_ROLE_ALIASES.get(value.lower(), value or default)


def is_hub_network_role(role: object) -> bool:
    return normalize_network_role(role) == "도사1"


def is_support_role(role: object) -> bool:
    return normalize_network_role(role) in {"도사1", "도사2"}


def classify_peer_connection(
    received_at: float,
    now: float,
    connected_sec: float = 1.5,
    stale_sec: float = 5.0,
) -> str:
    if float(received_at or 0.0) <= 0.0:
        return "DISCONNECTED"
    age = max(0.0, float(now) - float(received_at))
    if age <= max(0.0, float(connected_sec)):
        return "CONNECTED"
    if age <= max(float(connected_sec), float(stale_sec)):
        return "STALE"
    return "DISCONNECTED"


def should_accept_hotkey_press(last_pressed_at: float, now: float, debounce_sec: float = 0.20) -> bool:
    return float(now) - float(last_pressed_at or 0.0) >= max(0.0, float(debounce_sec))


def _normalize_map_info_text(value: object) -> str:
    return "".join(char.lower() for char in str(value or "") if char.isalnum())


def classify_map_sync(
    local: dict | None,
    remote: dict | None,
    now: float,
    freshness_sec: float = 1.0,
) -> str:
    if not isinstance(local, dict) or not isinstance(remote, dict):
        return "pending"
    received_at = float(remote.get("_received_at") or 0.0)
    if received_at > 0.0 and float(now) - received_at > max(0.0, float(freshness_sec)):
        return "pending"

    local_text = _normalize_map_info_text(local.get("map_info_text") or local.get("current_map"))
    remote_text = _normalize_map_info_text(remote.get("map_info_text") or remote.get("current_map"))
    if local_text and remote_text:
        return "same" if local_text == remote_text else "different"

    local_fingerprint = str(local.get("map_info_fingerprint") or "").strip()
    remote_fingerprint = str(remote.get("map_info_fingerprint") or "").strip()
    if local_fingerprint and remote_fingerprint and local_fingerprint == remote_fingerprint:
        return "same"
    return "pending"


def follow_manhattan_gap(target_x: int, target_y: int, current_x: int, current_y: int) -> int:
    return abs(int(target_x) - int(current_x)) + abs(int(target_y) - int(current_y))


def should_prioritize_follow_distance(gap: int, risk_distance: int = 7) -> bool:
    return max(0, int(gap)) >= max(1, int(risk_distance))


WARRIOR_TRANSITION_JUMP_DISTANCE = 4


def should_detect_warrior_transition(
    prev_x: int,
    prev_y: int,
    cur_x: int,
    cur_y: int,
    jump_distance: int = WARRIOR_TRANSITION_JUMP_DISTANCE,
) -> bool:
    """
    Detect map/cave warp by coordinate discontinuity.
    Default threshold is 4 so small portal jumps (e.g. 31,1 -> 27,3 = gap 6) are caught,
    while normal 1~2 tile walking between telemetry ticks is ignored.
    """
    return follow_manhattan_gap(prev_x, prev_y, cur_x, cur_y) >= max(1, int(jump_distance))



PORTAL_DIR_DELTA = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}


def offset_coord_by_dir(x: int, y: int, direction: str | None) -> tuple[int, int]:
    delta = PORTAL_DIR_DELTA.get(str(direction or "").strip().lower())
    if not delta:
        return int(x), int(y)
    return int(x) + int(delta[0]), int(y) + int(delta[1])


def dir_between_coords(from_x: int, from_y: int, to_x: int, to_y: int) -> str | None:
    dx = int(to_x) - int(from_x)
    dy = int(to_y) - int(from_y)
    if dx == 0 and dy == 0:
        return None
    if abs(dx) >= abs(dy):
        return "right" if dx > 0 else "left"
    return "down" if dy > 0 else "up"


def normalize_move_dir(direction: str | None) -> str | None:
    d = str(direction or "").strip().lower()
    return d if d in PORTAL_DIR_DELTA else None


def opposite_move_dir(direction: str | None) -> str | None:
    return {
        "up": "down",
        "down": "up",
        "left": "right",
        "right": "left",
    }.get(normalize_move_dir(direction) or "", None)


def is_plausible_map_coord(x: int, y: int) -> bool:
    """Reject OCR/telemetry noise that breaks portal follow (e.g. x=0 spikes)."""
    try:
        xi, yi = int(x), int(y)
    except Exception:
        return False
    if abs(xi) > 300 or abs(yi) > 300:
        return False
    if xi <= 0 or yi <= 0:
        return False
    return True


def is_plausible_transition_coord(x: int, y: int) -> bool:
    """Allow portal-edge coords like 0,4, but still reject empty (0,0) noise."""
    try:
        xi, yi = int(x), int(y)
    except Exception:
        return False
    if abs(xi) > 300 or abs(yi) > 300:
        return False
    if xi == 0 and yi == 0:
        return False
    return True


def resolve_portal_follow_cells(
    warrior_x: int,
    warrior_y: int,
    enter_dir: str | None,
) -> tuple[tuple[int, int], tuple[int, int]]:
    """
    Return (approach tile, portal tile).
    Approach is the warrior's last visible tile.
    Portal is the adjacent tile in the entry direction.
    """
    approach = (int(warrior_x), int(warrior_y))
    portal = offset_coord_by_dir(approach[0], approach[1], enter_dir)
    return approach, portal


def should_attempt_portal_enter(
    current_x: int,
    current_y: int,
    warrior_x: int,
    warrior_y: int,
    enter_dir: str | None = None,
    portal_x: int | None = None,
    portal_y: int | None = None,
) -> bool:
    """Enter while standing on the warrior's last visible tile or its portal tile."""
    current = (int(current_x), int(current_y))
    warrior = (int(warrior_x), int(warrior_y))
    portal = None
    if portal_x is not None and portal_y is not None:
        portal = (int(portal_x), int(portal_y))
    elif enter_dir:
        portal = offset_coord_by_dir(warrior[0], warrior[1], enter_dir)
    if current == warrior:
        return True
    if portal is not None and current == portal:
        return True
    return False


def infer_dir_from_trail(trail: list) -> str | None:
    """Infer last step direction from recent (x, y[, dir]) samples."""
    if not trail or len(trail) < 2:
        return None
    for i in range(len(trail) - 1, 0, -1):
        a = trail[i - 1]
        b = trail[i]
        try:
            ax, ay = int(a[0]), int(a[1])
            bx, by = int(b[0]), int(b[1])
        except Exception:
            continue
        if follow_manhattan_gap(ax, ay, bx, by) != 1:
            continue
        stepped = dir_between_coords(ax, ay, bx, by)
        if stepped:
            return stepped
        if len(b) >= 3:
            return normalize_move_dir(b[2])
    return None


def pick_portal_enter_dir(
    remote_dir: str | None,
    last_step_dir: str | None,
    trail: list | None = None,
) -> str | None:
    """Prefer local step inference, then trail, and only then remote hints."""
    return (
        normalize_move_dir(last_step_dir)
        or infer_dir_from_trail(trail or [])
        or normalize_move_dir(remote_dir)
    )


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
    # Follow target must stay anchored to the warrior's most recent position.
    # Snapping the target to the local current position causes same-tile loops
    # during delayed OCR / remote sync updates, so keep the intended target.
    _ = current_x, current_y, warrior_x, warrior_y, min_axis_gap
    return int(target_x), int(target_y)


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
    return (
        abs(int(target_x) - int(current_x)) <= 1
        and abs(int(target_y) - int(current_y)) <= 1
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
