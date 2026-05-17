import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from support_runtime_rules import (
    build_warrior_search_sequence,
    confirm_support_lock_by_hp_gain,
    is_confirmed_zero_hp_state,
    next_zero_hp_count,
    should_allow_party_support_cast,
    should_hold_follow_position,
    should_prioritize_self_mp,
    should_trigger_self_hp_emergency,
)


def test_follow_hold_position_uses_manhattan_distance():
    assert should_hold_follow_position(10, 10, 10, 10) is True
    assert should_hold_follow_position(10, 10, 11, 10) is True
    assert should_hold_follow_position(10, 10, 10, 11) is True
    assert should_hold_follow_position(10, 10, 11, 11) is False


def test_self_hp_emergency_threshold_is_fifty_thousand():
    assert should_trigger_self_hp_emergency(50000) is True
    assert should_trigger_self_hp_emergency(1) is True
    assert should_trigger_self_hp_emergency(50001) is False
    assert should_trigger_self_hp_emergency(0) is False


def test_self_mp_priority_threshold_is_below_forty_thousand():
    assert should_prioritize_self_mp(39999) is True
    assert should_prioritize_self_mp(0) is True
    assert should_prioritize_self_mp(40000) is False
    assert should_prioritize_self_mp(45000) is False


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
