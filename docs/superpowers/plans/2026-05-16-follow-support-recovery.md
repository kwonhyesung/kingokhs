# Follow Support Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve dosa follow/service behavior so stuck avoidance preserves useful memory, follow prioritizes closing distance to the warrior, and self emergency recovery handles low HP, low MP, and zero-HP death recovery.

**Architecture:** Add a small pure-rules helper for thresholds and follow-distance decisions, then wire those rules into the existing `LogicSvc` and `RecoveryManager` flow. Keep the current threading model, but elevate self emergency handling to the top of the dosa service loop and make stuck escape follow-aware.

**Tech Stack:** Python, pytest, existing `bis_logic.py`/`bis_spell.py` runtime

---

### Task 1: Add regression rules and tests

**Files:**
- Create: `tests/test_support_runtime_rules.py`
- Create: `support_runtime_rules.py`

- [ ] Add tests for follow hold distance, self HP emergency threshold, self MP priority threshold, and zero-HP confirmation logic.
- [ ] Run the targeted pytest command and confirm it fails before production code exists.
- [ ] Add the minimal helper module to satisfy the tests.

### Task 2: Wire follow/stuck behavior

**Files:**
- Modify: `bis_logic.py`

- [ ] Use the pure helper for follow hold distance (`|dx| + |dy| <= 1`).
- [ ] Make follow escape and step choice prefer distance reduction toward the warrior target.
- [ ] Stop eagerly clearing blocked-memory on minor forward progress; let TTL and repeated progress determine reuse instead.

### Task 3: Wire self emergency behavior

**Files:**
- Modify: `bis_logic.py`
- Modify: `bis_spell.py`

- [ ] Add top-priority self HP emergency handling for `hp <= 50000`.
- [ ] Add self MP priority handling for `mp < 40000`.
- [ ] Ensure self MP recovery clears target/red-tab state before self-cast.

### Task 4: Add zero-HP recovery flow

**Files:**
- Modify: `bis_logic.py`

- [ ] Add a guarded zero-HP confirmation counter.
- [ ] Implement the user-specified recovery sequence: stop support -> `esc` -> `4` -> `home` -> `enter` -> repeat `3` -> `home` -> `enter` -> `s` -> BM/GG recheck -> resume service/follow.

### Task 5: Verify

**Files:**
- Modify: `tests/test_support_runtime_rules.py`
- Modify: `support_runtime_rules.py`
- Modify: `bis_logic.py`
- Modify: `bis_spell.py`

- [ ] Run the targeted pytest command and confirm all new regression tests pass.
- [ ] Run a syntax check for the touched Python files.
