import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from support_runtime_rules import (
    adjust_follow_target_by_axis_gap,
    build_f5_hon_sequence,
    build_warrior_search_sequence,
    confirm_support_lock_by_hp_gain,
    is_confirmed_zero_hp_state,
    next_zero_hp_count,
    should_allow_party_support_cast,
    should_allow_follow_navigation,
    should_block_party_heal,
    should_cast_periodic_heewon,
    should_continue_self_hp_recovery,
    should_defer_stuck_escape_for_support,
    should_hold_follow_gap,
    should_hold_follow_position,
    should_ignore_monster_combat_for_support_autohunt,
    should_prioritize_self_mp,
    should_prioritize_self_mp_over_party_heal,
    should_retarget_after_support_follow_stuck,
    should_detect_warrior_transition,
    should_prioritize_follow_distance,
    should_trigger_self_hp_emergency,
    speed_up_delay,
)


def test_follow_hold_position_uses_manhattan_distance():
    assert should_hold_follow_position(10, 10, 10, 10) is True
    assert should_hold_follow_position(10, 10, 11, 10) is True
    assert should_hold_follow_position(10, 10, 10, 11) is True
    assert should_hold_follow_position(10, 10, 11, 11) is False


def test_support_follow_hold_gap_uses_one_tile_spacing():
    assert should_hold_follow_gap(1, hold_distance=1) is True
    assert should_hold_follow_gap(2, hold_distance=1) is False


def test_follow_distance_risk_starts_at_seven_tiles():
    assert should_prioritize_follow_distance(6, risk_distance=7) is False
    assert should_prioritize_follow_distance(7, risk_distance=7) is True


def test_warrior_transition_detects_twelve_tile_coordinate_jump():
    assert should_detect_warrior_transition(10, 10, 16, 15, jump_distance=12) is False
    assert should_detect_warrior_transition(10, 10, 22, 10, jump_distance=12) is True
    assert should_detect_warrior_transition(10, 10, 16, 16, jump_distance=12) is True


def test_follow_target_ignores_axis_when_warrior_gap_is_one_or_less():
    assert adjust_follow_target_by_axis_gap(
        target_x=12,
        target_y=15,
        current_x=10,
        current_y=20,
        warrior_x=11,
        warrior_y=18,
    ) == (10, 15)
    assert adjust_follow_target_by_axis_gap(
        target_x=12,
        target_y=15,
        current_x=10,
        current_y=20,
        warrior_x=14,
        warrior_y=21,
    ) == (12, 20)


def test_follow_navigation_can_use_fresh_target_without_connected_flag():
    assert should_allow_follow_navigation(True, True, False) is True
    assert should_allow_follow_navigation(True, False, True) is True
    assert should_allow_follow_navigation(True, False, False) is False
    assert should_allow_follow_navigation(False, True, True) is False


def test_retarget_delay_can_be_tuned_faster_than_double_speed():
    assert speed_up_delay(0.22, factor=3.0) == 0.0733
    assert speed_up_delay(0.46, factor=3.0) == 0.1533


def test_f5_hon_sequence_uses_esc_before_each_cast_and_retargets_after():
    sequence = build_f5_hon_sequence(repeat_count=5)
    assert sequence[:4] == ("esc", "6", "up", "enter")
    assert sequence[-3:] == ("esc", "tab", "tab")
    assert sequence.count("6") == 5
    assert len(sequence) == 23


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


def test_warrior_search_sequence_matches_real_input_flow():
    assert build_warrior_search_sequence("left") == ("esc", "tab", "left", "enter")


def test_support_lock_requires_hp_gain_after_heal():
    assert confirm_support_lock_by_hp_gain(100000, 105000) is True
    assert confirm_support_lock_by_hp_gain(100000, 100000) is False
    assert confirm_support_lock_by_hp_gain(100000, 95000) is False
