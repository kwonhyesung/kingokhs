import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bis_core
from support_runtime_rules import (
    adjust_follow_target_by_axis_gap,
    build_f5_hon_sequence,
    confirm_support_lock_by_hp_gain,
    dir_between_coords,
    infer_dir_from_trail,
    is_confirmed_zero_hp_state,
    next_zero_hp_count,
    normalize_move_dir,
    pick_portal_enter_dir,
    portal_cache_key,
    is_plausible_transition_coord,
    should_allow_party_support_cast,
    should_allow_follow_navigation,
    should_block_party_heal,
    should_cast_periodic_heewon,
    should_clear_target_box_after_support_stuck,
    should_continue_self_hp_recovery,
    should_defer_stuck_escape_for_support,
    should_hold_follow_gap,
    should_hold_follow_position,
    should_ignore_monster_combat_for_support_autohunt,
    should_prioritize_self_mp,
    should_prioritize_self_mp_over_party_heal,
    should_retarget_after_support_follow_stuck,
    should_run_periodic_refresh,
    support_retarget_block_duration,
    should_detect_warrior_transition,
    should_prioritize_follow_distance,
    should_defer_heal_for_follow_distance,
    should_trigger_self_hp_emergency,
    should_accept_hotkey_press,
    classify_map_sync,
    fingerprint_hamming_distance,
    is_implausible_walk_speed,
    classify_peer_connection,
    is_hub_network_role,
    enqueue_hotkey_callback,
    is_peer_role_conflict,
    is_support_role,
    normalize_network_role,
    resolve_runtime_network_role,
    speed_up_delay,
)
from bis_core import BisHardware, GameState
from svc_logic import LogicSvc


def test_debug_slash_hotkey_is_not_registered():
    source = (REPO_ROOT / "svc_worker.py").read_text(encoding="utf-8")
    assert 'HOTKEY_HANDLES["/"]' not in source
    assert 'keyboard.on_press_key("/",' not in source


def test_f2_does_not_run_move_test_automatically():
    source = (REPO_ROOT / "gui_app.py").read_text(encoding="utf-8")
    assert "self.test_move_signal()" not in source


def test_restore_gate_diag_interval_is_three_seconds():
    source = (REPO_ROOT / "svc_logic.py").read_text(encoding="utf-8")
    assert "RESTORE_GATE_DIAG_INTERVAL_SEC = 3.0" in source


def test_self_status_refresh_skips_s_before_f2(monkeypatch):
    state = GameState()
    state.role = "도사1"
    state.service_active = False
    state.nav_follow_enabled = False
    state.auto_hunt = False
    logic = LogicSvc(state)
    pressed = []

    monkeypatch.setattr(logic, "_is_support_lock_search_active", lambda: False)
    monkeypatch.setattr(logic, "_is_hw_ready", lambda: True)
    monkeypatch.setattr(logic, "_is_game_window_active", lambda: True)
    monkeypatch.setattr(logic, "_is_self_status_visible", lambda: False)
    monkeypatch.setattr(logic, "_press_hw_key", lambda key, **kw: pressed.append(key) or True)

    assert logic._refresh_self_status_if_needed(force=True) is False
    assert pressed == []


def test_self_status_refresh_allows_s_after_f2(monkeypatch):
    state = GameState()
    state.role = "도사1"
    state.service_active = True
    state.nav_follow_enabled = True
    state.auto_hunt = True
    logic = LogicSvc(state)
    pressed = []

    monkeypatch.setattr(logic, "_is_support_lock_search_active", lambda: False)
    monkeypatch.setattr(logic, "_is_hw_ready", lambda: True)
    monkeypatch.setattr(logic, "_is_game_window_active", lambda: True)
    monkeypatch.setattr(logic, "_is_self_status_visible", lambda: False)
    monkeypatch.setattr(logic, "_press_hw_key", lambda key, **kw: pressed.append(key) or True)
    monkeypatch.setattr(logic, "_wait_for_user_pattern", lambda *args, **kwargs: True)

    assert logic._refresh_self_status_if_needed(force=True) is True
    assert pressed == ["s"]


def test_cooltime_refresh_skips_s_before_f2(monkeypatch):
    state = GameState()
    state.role = "도사1"
    state.service_active = False
    state.nav_follow_enabled = False
    state.auto_hunt = False
    logic = LogicSvc(state)
    pressed = []

    monkeypatch.setattr(logic, "_is_support_lock_search_active", lambda: False)
    monkeypatch.setattr(logic, "_is_hw_ready", lambda: True)
    monkeypatch.setattr(logic, "_is_game_window_active", lambda: True)
    monkeypatch.setattr(logic, "_press_hw_key", lambda key, **kw: pressed.append(key) or True)

    assert logic._refresh_self_cooltime_view("[test]") is False
    assert pressed == []


def test_network_role_normalization_keeps_priest2_as_udp_client():
    assert normalize_network_role("Priest1 (Hub)") == "도사1"
    assert normalize_network_role("Priest2") == "도사2"
    assert is_hub_network_role("도사1") is True
    assert is_hub_network_role("도사2") is False


def test_runtime_role_uses_one_resolved_value_for_gui_and_udp():
    assert resolve_runtime_network_role("격수") == "격수"
    assert resolve_runtime_network_role("격수", explicit_role="도사2") == "도사2"
    assert resolve_runtime_network_role(
        "격수",
        explicit_role="도사2",
        explicit_network_role="도사1",
    ) == "도사1"


def test_peer_role_conflict_requires_same_pc_with_live_different_session_and_role():
    assert is_peer_role_conflict("old", "도사2", "new", "격수") is True
    assert is_peer_role_conflict("old", "도사2", "new", "도사2") is False
    assert is_peer_role_conflict("old", "도사2", "old", "격수") is False


def test_input_backend_falls_back_to_software_without_esp32():
    controller = BisHardware()
    assert controller.get_input_backend() == "SW"
    assert controller.is_input_ready() is True


def test_software_input_backend_translates_force_key_command(monkeypatch):
    events = []

    class FakeKeyboard:
        @staticmethod
        def press(key):
            events.append(("press", key))

        @staticmethod
        def release(key):
            events.append(("release", key))

    monkeypatch.setattr(bis_core, "direct_software_input", None)
    monkeypatch.setattr(bis_core, "software_keyboard", FakeKeyboard())
    controller = BisHardware()
    controller.send_force("K,3")

    assert events == [("press", "3"), ("release", "3")]


def test_priest2_f2_watchdog_restores_follow_service_mode(monkeypatch):
    state = GameState()
    state.role = "도사2"
    state.network_role = "도사2"
    state.auto_hunt = True
    state.nav_follow_enabled = True
    state.service_active = False

    logic = LogicSvc(state)
    monkeypatch.setattr(logic, "_is_game_window_active", lambda: True)

    assert logic._restore_dosa_service_if_follow_autohunt() is True
    assert state.service_active is True


def test_global_hotkey_queues_callback_without_calling_tk_from_hook_thread():
    state = GameState()
    callback = lambda: None

    assert enqueue_hotkey_callback(state.hotkey_event_queue, "TEST", callback) is True

    hotkey_name, queued_callback = state.hotkey_event_queue.get_nowait()
    assert hotkey_name == "TEST"
    assert queued_callback is callback


def test_both_priests_are_support_roles_for_warrior_follow_and_service():
    assert is_support_role("도사1") is True
    assert is_support_role("도사2") is True
    assert is_support_role("격수") is False


def test_peer_connection_status_is_fixed_by_last_received_time():
    assert classify_peer_connection(0.0, now=10.0) == "DISCONNECTED"
    assert classify_peer_connection(9.4, now=10.0) == "CONNECTED"
    assert classify_peer_connection(7.0, now=10.0) == "STALE"
    assert classify_peer_connection(4.0, now=10.0) == "DISCONNECTED"


def test_network_panel_refresh_runs_independently_on_its_interval():
    assert should_run_periodic_refresh(0.0, now=1.0, interval_sec=0.25) is True
    assert should_run_periodic_refresh(1.0, now=1.10, interval_sec=0.25) is False
    assert should_run_periodic_refresh(1.0, now=1.25, interval_sec=0.25) is True


def test_client_relay_refreshes_peer_received_time_without_echoing_stale_cache():
    state = GameState()
    payload = {
        "sender": "DOSA1-HUB",
        "seq": 1,
        "status": {"role": "도사1", "network_role": "도사1"},
        "peers": {
            "WARRIOR-PC": {
                "role": "격수",
                "network_role": "격수",
                "x": 27,
                "y": 3,
                "_received_at": 1.0,
            },
        },
    }

    before = time.time()
    state.apply_remote_payload(payload, accept_relayed_peers=True)
    relayed = state.other_pc_data["WARRIOR-PC"]
    assert relayed["_source_received_at"] == 1.0
    assert relayed["_received_at"] >= before

    hub_state = GameState()
    hub_state.apply_remote_payload(payload, accept_relayed_peers=False)
    assert "WARRIOR-PC" not in hub_state.other_pc_data


def test_new_network_session_accepts_sequence_reset_after_client_restart():
    state = GameState()
    first = {
        "sender": "DOSA2-PC",
        "session_id": "session-before-restart",
        "seq": 1780,
        "status": {"role": "도사2", "network_role": "도사2", "x": 10, "y": 10},
    }
    restarted = {
        "sender": "DOSA2-PC",
        "session_id": "session-after-restart",
        "seq": 1,
        "status": {"role": "도사2", "network_role": "도사2", "x": 20, "y": 20},
    }

    state.apply_remote_payload(first)
    state.apply_remote_payload(restarted)

    assert state.other_pc_data["DOSA2-PC"]["x"] == 20
    assert state.remote_sequences["DOSA2-PC"] == 1


def test_follow_hold_position_allows_one_tile_axis_error():
    assert should_hold_follow_position(10, 10, 10, 10) is True
    assert should_hold_follow_position(10, 10, 11, 10) is True
    assert should_hold_follow_position(10, 10, 10, 11) is True
    assert should_hold_follow_position(10, 10, 11, 11) is True


def test_portal_cache_key_discriminates_nearby_but_different_portals():
    map_sig = ("map", "1", "floor")
    assert portal_cache_key(map_sig, 30, 48) == portal_cache_key(map_sig, 31, 49)
    assert portal_cache_key(map_sig, 30, 48) != portal_cache_key(map_sig, 30, 80)


def test_follow_hold_position_max_gap_zero_forces_exact_tile():
    # Portal-follow passes max_gap=0 so movement doesn't stop 1 tile short
    # of the warrior's exact last tile.
    assert should_hold_follow_position(10, 10, 11, 10, max_gap=0) is False
    assert should_hold_follow_position(10, 10, 10, 10, max_gap=0) is True


def test_support_follow_hold_gap_uses_one_tile_spacing():
    assert should_hold_follow_gap(1, hold_distance=1) is True
    assert should_hold_follow_gap(2, hold_distance=1) is False


def test_follow_distance_risk_starts_at_seven_tiles():
    assert should_prioritize_follow_distance(6, risk_distance=7) is False
    assert should_prioritize_follow_distance(7, risk_distance=7) is True


def test_heal_defer_skips_only_when_retarget_would_be_needed():
    # already-verified target: cast is a free tap, never defer regardless of distance
    assert should_defer_heal_for_follow_distance(True, True) is False
    # unverified + far: would need a fresh esc>tab>tab, defer regardless of HP
    assert should_defer_heal_for_follow_distance(True, False) is True
    # no distance risk at all: never defer
    assert should_defer_heal_for_follow_distance(False, False) is False


def test_pause_hotkey_rejects_duplicate_event_but_accepts_later_press():
    assert should_accept_hotkey_press(0.0, 1.0, debounce_sec=0.20) is True
    assert should_accept_hotkey_press(1.0, 1.05, debounce_sec=0.20) is False
    assert should_accept_hotkey_press(1.0, 1.25, debounce_sec=0.20) is True


def test_map_sync_prefers_normalized_text_then_roi_fingerprint():
    local = {"map_info_text": "Map A - 1F", "map_info_fingerprint": "0101", "_received_at": 10.0}
    same_text = {"map_info_text": "mapa1f", "map_info_fingerprint": "1111", "_received_at": 10.1}
    same_roi = {"map_info_text": "", "map_info_fingerprint": "0101", "_received_at": 10.1}
    different = {"map_info_text": "Map B - 1F", "map_info_fingerprint": "1100", "_received_at": 10.1}

    assert classify_map_sync(local, same_text, now=10.2) == "same"
    assert classify_map_sync(local, same_roi, now=10.2) == "same"
    assert classify_map_sync(local, different, now=10.2) == "different"
    assert classify_map_sync(local, None, now=10.2) == "pending"


def test_map_sync_fingerprint_tolerates_a_few_flipped_bits():
    # 16-bit fingerprints, no OCR text (forces the fingerprint path).
    base = {"map_info_fingerprint": "0101010101010101", "_received_at": 10.0}
    near = {"map_info_fingerprint": "1001010101010101", "_received_at": 10.1}  # 2 bits flipped
    far = {"map_info_fingerprint": "1010101010100101", "_received_at": 10.1}  # 12 bits flipped

    assert fingerprint_hamming_distance(base["map_info_fingerprint"], near["map_info_fingerprint"]) == 2
    assert classify_map_sync(base, near, now=10.2) == "same"
    assert classify_map_sync(base, far, now=10.2) == "pending"


def test_implausible_walk_speed_separates_teleport_from_delayed_telemetry():
    # 7 tiles in 0.2s = 35 tiles/s: no normal walk speed explains this -> real teleport.
    assert is_implausible_walk_speed(distance=7, elapsed_sec=0.2) is True
    # Same 7 tiles but the reading was 2s late: 3.5 tiles/s is ordinary walking.
    assert is_implausible_walk_speed(distance=7, elapsed_sec=2.0) is False
    # Unknown timing stays fail-safe (treated as implausible, same as before this existed).
    assert is_implausible_walk_speed(distance=7, elapsed_sec=0.0) is True


def test_warrior_transition_detects_small_portal_jumps():
    # Default threshold is 4: ignore normal walking, catch small cave warps.
    assert should_detect_warrior_transition(10, 10, 11, 10) is False  # 1
    assert should_detect_warrior_transition(10, 10, 12, 11) is False  # 3
    assert should_detect_warrior_transition(10, 10, 14, 10) is True   # 4
    # User case: 31,01 -> 27,03 (manhattan 6)
    assert should_detect_warrior_transition(31, 1, 27, 3) is True
    # Legacy large jump still works
    assert should_detect_warrior_transition(10, 10, 22, 10, jump_distance=12) is True
    assert should_detect_warrior_transition(10, 10, 16, 15, jump_distance=12) is False


def test_portal_follow_uses_exact_warrior_last_tile():
    # Nav target is the warrior's own last tile - no invented offset tile.
    assert dir_between_coords(17, 11, 18, 11) == "right"
    assert normalize_move_dir("RIGHT") == "right"
    assert infer_dir_from_trail([(16, 11, None), (17, 11, "right")]) == "right"


def test_portal_enter_direction_prefers_transition_key_over_prior_step():
    assert pick_portal_enter_dir("up", "right", [(30, 1, "right"), (31, 1, "right")]) == "right"


def test_game_state_shares_coord_transition_without_map_ocr():
    state = GameState()
    state.x = 31
    state.y = 1
    state.last_move_dir = "up"
    first = state.build_share_payload()["status"]
    assert first["coord_transition"] is None

    state.x = 27
    state.y = 3
    second = state.build_share_payload()["status"]
    assert second["coord_transition"]["from"] == [31, 1]
    assert second["coord_transition"]["to"] == [27, 3]
    assert second["coord_transition"]["dir"] == "up"


def test_game_state_prefers_fresh_physical_key_over_stale_bot_move_dir():
    # A manually-piloted warrior's real arrow-key press should win over this
    # PC's own last bot-issued auto-nav direction, which can be stale by the
    # time a human steps through a portal.
    state = GameState()
    state.x = 31
    state.y = 1
    state.last_move_dir = "left"
    state.physical_last_move_dir = "up"
    state.physical_last_move_dir_ts = time.time()
    state.build_share_payload()

    state.x = 27
    state.y = 3
    payload = state.build_share_payload()["status"]
    assert payload["coord_transition"]["input_dir"] == "up"


def test_game_state_ignores_stale_physical_key():
    state = GameState()
    state.x = 31
    state.y = 1
    state.last_move_dir = "left"
    state.physical_last_move_dir = "up"
    state.physical_last_move_dir_ts = time.time() - 10.0
    state.build_share_payload()

    state.x = 27
    state.y = 3
    payload = state.build_share_payload()["status"]
    assert payload["coord_transition"]["input_dir"] == "left"


def test_game_state_marks_from_confidence_high_only_with_fresh_key_press():
    state = GameState()
    state.x = 31
    state.y = 1
    state.physical_last_move_dir = "up"
    state.physical_last_move_dir_ts = time.time()
    state.build_share_payload()

    state.x = 27
    state.y = 3
    payload = state.build_share_payload()["status"]
    assert payload["coord_transition"]["from_confidence"] == "high"


def test_game_state_marks_from_confidence_low_without_key_press():
    state = GameState()
    state.x = 31
    state.y = 1
    state.build_share_payload()

    state.x = 27
    state.y = 3
    payload = state.build_share_payload()["status"]
    assert payload["coord_transition"]["from_confidence"] == "low"


def test_game_state_shares_map_info_text_for_cross_pc_map_compare():
    state = GameState()
    state.map_info_text = "선비족입구"
    state.map_name = "선비족"
    state.map_floor = "입구"
    state.map_info_fingerprint = "a1b2c3d4"
    payload = state.build_share_payload()["status"]
    assert payload["map_info_text"] == "선비족입구"
    assert payload["map_name"] == "선비족"
    assert payload["map_floor"] == "입구"
    assert payload["map_info_fingerprint"] == "a1b2c3d4"


def test_game_state_emits_portal_edge_transition_at_zero_coord():
    state = GameState()
    state.x = 1
    state.y = 4
    state.build_share_payload()

    state.x = 0
    state.y = 4
    payload = state.build_share_payload()["status"]
    assert is_plausible_transition_coord(0, 4) is True
    assert payload["coord_transition"]["from"] == [1, 4]
    assert payload["coord_transition"]["to"] == [0, 4]
    assert payload["coord_transition"]["dir"] == "left"
    assert payload["coord_transition"]["input_dir"] is None


def test_follow_target_stays_anchored_to_intended_target():
    assert adjust_follow_target_by_axis_gap(
        target_x=12,
        target_y=15,
        current_x=10,
        current_y=20,
        warrior_x=11,
        warrior_y=18,
    ) == (12, 15)
    assert adjust_follow_target_by_axis_gap(
        target_x=12,
        target_y=15,
        current_x=10,
        current_y=20,
        warrior_x=14,
        warrior_y=21,
    ) == (12, 15)


def test_follow_navigation_can_use_fresh_target_without_connected_flag():
    assert should_allow_follow_navigation(True, True, False) is True
    assert should_allow_follow_navigation(True, False, True) is True
    assert should_allow_follow_navigation(True, False, False) is False
    assert should_allow_follow_navigation(False, True, True) is False


def test_retarget_delay_can_be_tuned_faster_than_double_speed():
    assert speed_up_delay(0.22, factor=3.0) == 0.0733
    assert speed_up_delay(0.46, factor=3.0) == 0.1533


def test_f5_hon_sequence_uses_esc_before_each_cast():
    sequence = build_f5_hon_sequence(repeat_count=5)
    assert sequence[:4] == ("esc", "6", "up", "enter")
    assert sequence[-4:] == ("esc", "6", "up", "enter")
    assert sequence.count("6") == 5
    assert len(sequence) == 20


def test_party_heal_is_blocked_until_retarget_finishes():
    assert should_block_party_heal(True, now_sec=10.0, blocked_until_sec=0.0) is True
    assert should_block_party_heal(False, now_sec=10.0, blocked_until_sec=10.1) is True
    assert should_block_party_heal(False, now_sec=10.0, blocked_until_sec=9.9) is False


def test_self_hp_emergency_threshold_is_fifty_thousand():
    assert should_trigger_self_hp_emergency(50000) is True
    assert should_trigger_self_hp_emergency(1) is True
    assert should_trigger_self_hp_emergency(50001) is False
    assert should_trigger_self_hp_emergency(0) is False


def test_self_mp_priority_threshold_is_below_fifty_thousand():
    assert should_prioritize_self_mp(49999) is True
    assert should_prioritize_self_mp(50000) is True
    assert should_prioritize_self_mp(0) is True
    assert should_prioritize_self_mp(50001) is False


def test_self_mp_priority_over_party_heal_ignores_warrior_critical_hp():
    assert should_prioritize_self_mp_over_party_heal(49999, 30001) is True
    assert should_prioritize_self_mp_over_party_heal(50000, 30001) is True
    assert should_prioritize_self_mp_over_party_heal(49999, 30000) is True
    assert should_prioritize_self_mp_over_party_heal(50001, 30001) is False


def test_periodic_heewon_uses_sixteen_second_interval():
    assert should_cast_periodic_heewon(15.99) is False
    assert should_cast_periodic_heewon(16.0) is True


def test_periodic_heewoncheom_uses_twenty_five_second_interval():
    assert should_cast_periodic_heewon(24.99, interval_sec=25.0) is False
    assert should_cast_periodic_heewon(25.0, interval_sec=25.0) is True


def test_support_autohunt_ignores_monster_combat_for_dosa_follow_service():
    assert should_ignore_monster_combat_for_support_autohunt("도사", True, True) is True
    assert should_ignore_monster_combat_for_support_autohunt("도사1", True, True) is True
    assert should_ignore_monster_combat_for_support_autohunt("도사2", True, True) is True
    assert should_ignore_monster_combat_for_support_autohunt("술사", True, True) is True
    assert should_ignore_monster_combat_for_support_autohunt("술사", True, False) is False
    assert should_ignore_monster_combat_for_support_autohunt("도사", True, False) is False
    assert should_ignore_monster_combat_for_support_autohunt("격수", True, True) is False


def test_dosa_f2_support_defers_route_stuck_escape():
    assert should_defer_stuck_escape_for_support("도사", True, True) is True
    assert should_defer_stuck_escape_for_support("도사", True, False) is False
    assert should_defer_stuck_escape_for_support("격수", True, True) is False


def test_support_follow_stuck_retargets_after_soft_escape_budget():
    assert should_retarget_after_support_follow_stuck(0, max_soft_stucks_before_retarget=1) is False
    assert should_retarget_after_support_follow_stuck(1, max_soft_stucks_before_retarget=1) is True
    assert should_retarget_after_support_follow_stuck(2, max_soft_stucks_before_retarget=1) is True
    assert should_retarget_after_support_follow_stuck(1, max_soft_stucks_before_retarget=2) is False
    assert should_retarget_after_support_follow_stuck(2, max_soft_stucks_before_retarget=2) is True


def test_self_hp_recovery_continues_until_good_hp():
    assert should_continue_self_hp_recovery(49000, 100000) is True
    assert should_continue_self_hp_recovery(99999, 100000) is True
    assert should_continue_self_hp_recovery(100000, 100000) is False


def test_self_hp_recovery_uses_emergency_fallback_when_good_hp_missing():
    assert should_continue_self_hp_recovery(56000, 0) is True
    assert should_continue_self_hp_recovery(57000, 0) is False


def test_zero_hp_confirmation_requires_two_consecutive_reads():
    zero_count = 0
    zero_count = next_zero_hp_count(zero_count, 0)
    assert is_confirmed_zero_hp_state(zero_count) is False

    zero_count = next_zero_hp_count(zero_count, 0)
    assert is_confirmed_zero_hp_state(zero_count) is True

    zero_count = next_zero_hp_count(zero_count, 12000)
    assert zero_count == 0


def test_party_support_requires_confirmed_red_tab():
    assert should_allow_party_support_cast(True) is True
    assert should_allow_party_support_cast(False) is False
    assert should_allow_party_support_cast(True, target_prepared=False) is False


def test_self_recovery_retarget_keeps_follow_block_short():
    assert support_retarget_block_duration(True) == 0.18
    assert support_retarget_block_duration(False) == 2.4


def test_redtab_click_fallback_does_not_stop_follow_for_seconds():
    assert LogicSvc._REDTAB_HOLD_SEC <= 0.3


def test_redtab_click_path_uses_short_waits():
    source = (REPO_ROOT / "svc_logic.py").read_text(encoding="utf-8")
    assert LogicSvc._NAMETAG_WAIT_SEC <= 0.5
    assert "self._sleep_ui_gap(0.01)" in source
    assert source.count("self._sleep_ui_gap(0.03)") >= 4
    assert "_wait_for_red_tab_lock(timeout=0.20, min_hits=1)" in source


def test_target_box_clear_waits_for_support_targeting_to_finish():
    assert should_clear_target_box_after_support_stuck(True, False, False) is True
    assert should_clear_target_box_after_support_stuck(True, True, False) is False
    assert should_clear_target_box_after_support_stuck(True, False, True) is False
    assert should_clear_target_box_after_support_stuck(False, False, False) is False


def test_support_lock_requires_hp_gain_after_heal():
    assert confirm_support_lock_by_hp_gain(100000, 105000) is True
    assert confirm_support_lock_by_hp_gain(100000, 100000) is False
    assert confirm_support_lock_by_hp_gain(100000, 95000) is False


def test_failed_self_mp_attempt_does_not_reacquire_warrior_redtab(monkeypatch):
    state = GameState()
    state.role = "도사"
    state.network_role = "도사"
    state.service_active = True
    state.nav_follow_enabled = True
    state.mp = 30000
    state.hp = 200000
    state.good_hp = 100000

    logic = LogicSvc(state)
    logic._party_direct_heal_target_prepared = True
    logic._party_direct_heal_verified = True
    calls = []

    monkeypatch.setattr("svc_logic.humanized_sleep", lambda *args, **kwargs: None)
    monkeypatch.setattr(logic, "_set_support_phase", lambda *args, **kwargs: None)
    monkeypatch.setattr(logic, "_clear_support_phase", lambda *args, **kwargs: None)
    monkeypatch.setattr(logic, "_cast_self_mp_recovery_fast", lambda trigger_mp=None: True)
    monkeypatch.setattr(
        logic,
        "_reacquire_warrior_red_tab_after_emergency",
        lambda moving_follow=False: calls.append(moving_follow) or True,
    )

    assert logic._execute_self_mp_recovery_and_retarget(
        {"hp": 100000, "good_hp": 1100000},
        trigger_mp=30000,
    ) is True
    assert calls == []
    assert logic._party_direct_heal_target_prepared is True
    assert logic._party_direct_heal_verified is True


def test_successful_self_mp_reacquires_redtab_then_heals_warrior(monkeypatch):
    state = GameState()
    state.role = "도사"
    state.network_role = "도사"
    state.service_active = True
    state.nav_follow_enabled = True
    state.mp = 30000
    state.hp = 200000
    state.good_hp = 100000

    logic = LogicSvc(state)
    calls = []

    def cast_mp(trigger_mp=None):
        state.mp = 80000
        return True

    monkeypatch.setattr("svc_logic.humanized_sleep", lambda *args, **kwargs: None)
    monkeypatch.setattr(logic, "_set_support_phase", lambda *args, **kwargs: None)
    monkeypatch.setattr(logic, "_clear_support_phase", lambda *args, **kwargs: None)
    monkeypatch.setattr(logic, "_cast_self_mp_recovery_fast", cast_mp)
    monkeypatch.setattr(
        logic,
        "_reacquire_warrior_red_tab_after_emergency",
        lambda moving_follow=False: calls.append("retarget") or True,
    )
    monkeypatch.setattr(
        logic,
        "_recover_party_hp",
        lambda snapshot: calls.append(("heal", snapshot["hp"])) or True,
    )

    assert logic._execute_self_mp_recovery_and_retarget(
        {"hp": 100000, "good_hp": 1100000},
        trigger_mp=30000,
    ) is True
    assert calls == ["retarget", ("heal", 100000)]


def test_warrior_body_pattern_is_not_clicked_when_name_tag_is_missing(monkeypatch):
    state = GameState()
    state.pattern_matcher = type(
        "Matcher",
        (),
        {"find_text_scan": lambda self, crop, category, ent_type: [
            {"name": "점프_right", "cx": 100, "cy": 200, "score": 1.0}
        ]},
    )()
    state.last_frame = type("Frame", (), {"size": 1})()
    logic = LogicSvc(state)
    clicked = []

    monkeypatch.setattr("svc_logic.humanized_sleep", lambda *args, **kwargs: None)
    monkeypatch.setattr("svc_logic.hw.move_cursor", lambda *args, **kwargs: True)
    monkeypatch.setattr("svc_logic.hw.click_pixel", lambda x, y: clicked.append((x, y)) or True)
    monkeypatch.setattr(logic, "_press_hw_key", lambda *args, **kwargs: True)
    monkeypatch.setattr(logic, "_get_play_area_crop", lambda frame: ("crop", 10, 20))
    monkeypatch.setattr(logic, "_wait_for_red_tab_lock", lambda timeout=None, min_hits=None: True)
    monkeypatch.setattr(logic, "_is_monster_target_selected", lambda: False)

    assert logic._click_warrior_marker_and_lock() is None
    assert clicked == []


def test_warrior_click_uses_jeompeu_name_not_jyeompeu_name(monkeypatch):
    """점프_name*(격수)만 클릭 대상이다 - 졈프_name*(도사 자신)은 후보에서
    빠져야 한다. 이게 반대로 되면 도사가 자기 자신을 클릭해 red_tab을
    스스로에게 건다(실측 로그로 확인된 회귀: character click: name=졈프_name)."""
    state = GameState()
    state.pattern_matcher = type(
        "Matcher",
        (),
        {"find_text_scan": lambda self, crop, category, ent_type: [
            {"name": "졈프_name", "cx": 300, "cy": 200, "score": 999.0},
            {"name": "점프_name_c", "cx": 100, "cy": 100, "score": 1.0},
        ]},
    )()
    state.last_frame = type("Frame", (), {"size": 1})()
    logic = LogicSvc(state)
    clicked = []

    monkeypatch.setattr("svc_logic.humanized_sleep", lambda *args, **kwargs: None)
    monkeypatch.setattr("svc_logic.hw.move_cursor", lambda *args, **kwargs: True)
    monkeypatch.setattr("svc_logic.hw.click_pixel", lambda x, y: clicked.append((x, y)) or True)
    monkeypatch.setattr("svc_logic.click_offset_y", lambda: 87)
    monkeypatch.setattr(logic, "_press_hw_key", lambda *args, **kwargs: True)
    monkeypatch.setattr(logic, "_get_play_area_crop", lambda frame: ("crop", 10, 20))
    monkeypatch.setattr(logic, "_wait_for_red_tab_lock", lambda timeout=None, min_hits=None: True)
    monkeypatch.setattr(logic, "_is_monster_target_selected", lambda: False)

    assert logic._click_warrior_marker_and_lock() is True
    assert clicked == [(110, 207)]


def test_warrior_click_accepts_jeompeu_name_variant_suffix(monkeypatch):
    """점프_name_ 로 시작하는 색 변형(예: 점프_name_c)도 전부 후보다."""
    state = GameState()
    state.pattern_matcher = type(
        "Matcher",
        (),
        {"find_text_scan": lambda self, crop, category, ent_type: [
            {"name": "점프_name_c", "cx": 250, "cy": 150, "score": 1.0},
        ]},
    )()
    state.last_frame = type("Frame", (), {"size": 1})()
    logic = LogicSvc(state)
    clicked = []

    monkeypatch.setattr("svc_logic.humanized_sleep", lambda *args, **kwargs: None)
    monkeypatch.setattr("svc_logic.hw.move_cursor", lambda *args, **kwargs: True)
    monkeypatch.setattr("svc_logic.hw.click_pixel", lambda x, y: clicked.append((x, y)) or True)
    monkeypatch.setattr("svc_logic.click_offset_y", lambda: 87)
    monkeypatch.setattr(logic, "_press_hw_key", lambda *args, **kwargs: True)
    monkeypatch.setattr(logic, "_get_play_area_crop", lambda frame: ("crop", 10, 20))
    monkeypatch.setattr(logic, "_wait_for_red_tab_lock", lambda timeout=None, min_hits=None: True)
    monkeypatch.setattr(logic, "_is_monster_target_selected", lambda: False)

    assert logic._click_warrior_marker_and_lock() is True
    assert clicked == [(260, 257)]


def test_logger_handles_game_log_permission_error_with_retry():
    source = (REPO_ROOT / "background_threads.py").read_text(encoding="utf-8")
    assert "except PermissionError" in source
    assert "_append_csv_row_with_retry(LOG_FILE, row)" in source
    assert "_append_csv_row_with_retry(LOG_FILE_STR, row_str)" in source


def test_warrior_name_click_does_not_create_magenta_marker(monkeypatch):
    state = GameState()
    state.x = 10
    state.y = 10
    state.my_screen_pos = (500, 500)
    state.other_pc_data["warrior"] = {
        "role": "격수",
        "x": 11,
        "y": 10,
        "_received_at": time.time(),
    }
    state.pattern_matcher = type(
        "Matcher",
        (),
        {"find_text_scan": lambda self, crop, category, ent_type: [
            {"name": "점프_name_c", "cx": 538, "cy": 393, "score": 1.0},
        ]},
    )()
    state.last_frame = type("Frame", (), {"size": 1})()
    logic = LogicSvc(state)

    monkeypatch.setattr("svc_logic.humanized_sleep", lambda *args, **kwargs: None)
    monkeypatch.setattr("svc_logic.hw.move_cursor", lambda *args, **kwargs: True)
    monkeypatch.setattr("svc_logic.hw.click_pixel", lambda x, y: True)
    monkeypatch.setattr("svc_logic.click_offset_y", lambda: 87)
    monkeypatch.setattr(logic, "_press_hw_key", lambda *args, **kwargs: True)
    monkeypatch.setattr(logic, "_get_play_area_crop", lambda frame: ("crop", 0, 0))
    monkeypatch.setattr(logic, "_wait_for_red_tab_lock", lambda timeout=None, min_hits=None: True)
    monkeypatch.setattr(logic, "_is_monster_target_selected", lambda: False)

    assert logic._click_warrior_marker_and_lock() is True
    assert [m for m in state.visual_markers if m.get("color") == "magenta"] == []
