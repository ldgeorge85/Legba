# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure-kernel tests for the P-10 coalescing-trigger decision logic.

These exercise ``decide`` / ``apply_dirty`` / ``apply_fire`` /
``policy_from_descriptor`` directly — no I/O, no rig. They pin the gate
ORDERING + the cooldown clamp + the LLM per-signal guard precisely, so the
integration tests can trust the kernel and focus on the durable/NATS plumbing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from legba.runtime.triggers.policy import (
    TriggerAccumulator,
    TriggerPolicy,
    TriggerReason,
    apply_dirty,
    apply_fire,
    decide,
    policy_from_descriptor,
    severity_rank,
)

T0 = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)


def _dirty(acc, sid, sev_rank):
    acc, counted = apply_dirty(acc, signal_id=sid, severity_rank_value=sev_rank)
    return acc, counted


# ---------------------------------------------------------------------------
# severity ranking
# ---------------------------------------------------------------------------


def test_severity_rank_ladder():
    assert severity_rank("info") == 0
    assert severity_rank("CRITICAL") == 4  # case-insensitive
    assert severity_rank("nonsense") == -1
    assert severity_rank(None) == -1


# ---------------------------------------------------------------------------
# accumulation (batch) gate
# ---------------------------------------------------------------------------


def test_accumulation_holds_then_fires_at_threshold():
    p = TriggerPolicy(accumulation_threshold=3)
    acc = TriggerAccumulator()
    acc, _ = _dirty(acc, "a", 1)
    assert decide(acc, p, now=T0).reason is TriggerReason.BELOW_THRESHOLD
    acc, _ = _dirty(acc, "b", 1)
    assert decide(acc, p, now=T0).reason is TriggerReason.BELOW_THRESHOLD
    acc, _ = _dirty(acc, "c", 1)
    d = decide(acc, p, now=T0)
    assert d.should_fire and d.reason is TriggerReason.ACCUMULATION
    assert d.pending_count == 3 and not d.severity_wake


def test_not_dirty_never_fires():
    p = TriggerPolicy(accumulation_threshold=1, cadence_seconds=60)
    acc = TriggerAccumulator()
    d = decide(acc, p, now=T0 + timedelta(days=1))
    assert not d.should_fire and d.reason is TriggerReason.NOT_DIRTY


# ---------------------------------------------------------------------------
# severity (immediate) gate — beats a half-full batch
# ---------------------------------------------------------------------------


def test_severity_wake_beats_unfilled_batch():
    p = TriggerPolicy(accumulation_threshold=10, severity_gate="critical")
    acc = TriggerAccumulator()
    acc, _ = _dirty(acc, "a", 2)  # medium — holds
    assert decide(acc, p, now=T0).reason is TriggerReason.BELOW_THRESHOLD
    acc, _ = _dirty(acc, "b", 4)  # critical — wakes NOW even at pending 2 < 10
    d = decide(acc, p, now=T0)
    assert d.should_fire and d.reason is TriggerReason.SEVERITY and d.severity_wake


def test_below_severity_gate_does_not_wake():
    p = TriggerPolicy(accumulation_threshold=10, severity_gate="critical")
    acc = TriggerAccumulator()
    acc, _ = _dirty(acc, "a", 3)  # high < critical
    d = decide(acc, p, now=T0)
    assert not d.should_fire and d.reason is TriggerReason.BELOW_THRESHOLD


# ---------------------------------------------------------------------------
# cooldown clamp — thrash protection
# ---------------------------------------------------------------------------


def test_cooldown_clamps_even_a_critical_signal():
    p = TriggerPolicy(accumulation_threshold=1, severity_gate="critical", cooldown_seconds=300)
    acc = TriggerAccumulator()
    acc, _ = _dirty(acc, "a", 4)  # critical
    d = decide(acc, p, now=T0)
    assert d.should_fire and d.reason is TriggerReason.SEVERITY

    # Fire → cooldown opens.
    acc = apply_fire(acc, fired_at=T0)
    # A fresh critical 1s later is CLAMPED (cooldown, not severity).
    acc, _ = _dirty(acc, "b", 4)
    d2 = decide(acc, p, now=T0 + timedelta(seconds=1))
    assert not d2.should_fire and d2.reason is TriggerReason.COOLDOWN

    # After the cooldown lapses the still-pending critical fires.
    d3 = decide(acc, p, now=T0 + timedelta(seconds=301))
    assert d3.should_fire and d3.reason is TriggerReason.SEVERITY


def test_cooldown_caps_fire_rate_over_a_burst():
    # 100 signals across 50s with a 10s cooldown + accumulation=1 → far fewer
    # than 100 fires (the cooldown-cap behaviour, in kernel form).
    p = TriggerPolicy(accumulation_threshold=1, cooldown_seconds=10)
    acc = TriggerAccumulator()
    fires = 0
    for i in range(100):
        now = T0 + timedelta(seconds=i * 0.5)  # 0,0.5,...49.5s
        acc, _ = _dirty(acc, f"s{i}", 1)
        d = decide(acc, p, now=now)
        if d.should_fire:
            acc = apply_fire(acc, fired_at=now)
            fires += 1
    # 50s / 10s cooldown ⇒ at most ~6 fires (t=0,10,20,30,40, +the first).
    assert 1 <= fires <= 7, fires


# ---------------------------------------------------------------------------
# cadence gate
# ---------------------------------------------------------------------------


def test_cadence_fires_only_with_pending_and_only_after_period():
    p = TriggerPolicy(accumulation_threshold=100, cadence_seconds=60)
    acc = TriggerAccumulator(last_fired_at=T0)  # last fired at T0
    # One sub-threshold signal; cadence not yet due.
    acc, _ = _dirty(acc, "a", 1)
    assert decide(acc, p, now=T0 + timedelta(seconds=30)).reason is TriggerReason.BELOW_THRESHOLD
    # Period elapsed → cadence fires the small batch.
    d = decide(acc, p, now=T0 + timedelta(seconds=61))
    assert d.should_fire and d.reason is TriggerReason.CADENCE and d.pending_count == 1


def test_cadence_tick_over_empty_window_is_noop():
    p = TriggerPolicy(accumulation_threshold=100, cadence_seconds=60)
    acc = TriggerAccumulator(last_fired_at=T0)  # no pending
    d = decide(acc, p, now=T0 + timedelta(seconds=600))
    assert not d.should_fire and d.reason is TriggerReason.NOT_DIRTY


def test_cadence_anchors_on_first_dirty_for_never_fired_pair():
    # A never-fired pair must NOT fire on cadence on its first signal — the
    # period is measured from when the window opened (first_dirty_at).
    p = TriggerPolicy(accumulation_threshold=100, cadence_seconds=60)
    acc = TriggerAccumulator()  # never fired
    acc, _ = apply_dirty(acc, signal_id="a", severity_rank_value=1, now=T0)
    assert acc.first_dirty_at == T0
    # Same instant + 30s: not yet due.
    assert not decide(acc, p, now=T0).should_fire
    assert not decide(acc, p, now=T0 + timedelta(seconds=30)).should_fire
    # A full period after the window opened: cadence fires.
    d = decide(acc, p, now=T0 + timedelta(seconds=61))
    assert d.should_fire and d.reason is TriggerReason.CADENCE


def test_first_dirty_anchor_resets_on_fire():
    p = TriggerPolicy(accumulation_threshold=1)
    acc = TriggerAccumulator()
    acc, _ = apply_dirty(acc, signal_id="a", severity_rank_value=1, now=T0)
    assert acc.first_dirty_at == T0
    acc = apply_fire(acc, fired_at=T0)
    assert acc.first_dirty_at is None  # window closed
    # The next window re-anchors.
    acc, _ = apply_dirty(acc, signal_id="b", severity_rank_value=1, now=T0 + timedelta(seconds=5))
    assert acc.first_dirty_at == T0 + timedelta(seconds=5)


# ---------------------------------------------------------------------------
# alias / duplicate — no double count
# ---------------------------------------------------------------------------


def test_duplicate_canonical_id_counts_once():
    p = TriggerPolicy(accumulation_threshold=2)
    acc = TriggerAccumulator()
    acc, c1 = _dirty(acc, "canon-1", 1)
    acc, c2 = _dirty(acc, "canon-1", 1)  # same canonical (alias) — must NOT count
    assert c1 is True and c2 is False and acc.pending_count == 1
    assert not decide(acc, p, now=T0).should_fire
    acc, c3 = _dirty(acc, "canon-2", 1)  # a genuinely new observation
    assert c3 is True and acc.pending_count == 2
    assert decide(acc, p, now=T0).reason is TriggerReason.ACCUMULATION


def test_seen_set_resets_after_fire():
    acc = TriggerAccumulator()
    acc, _ = _dirty(acc, "x", 1)
    acc = apply_fire(acc, fired_at=T0)
    assert acc.pending_count == 0 and acc.seen_signal_ids == set()
    # Post-fire the same id can contribute to the NEW window.
    acc, counted = _dirty(acc, "x", 1)
    assert counted and acc.pending_count == 1


# ---------------------------------------------------------------------------
# LLM per-signal guard — the hard rule
# ---------------------------------------------------------------------------


def test_llm_accumulation_floored_so_never_per_signal():
    # An LLM analyst with a (mis)configured accumulation=1 is floored to the
    # min batch, so a single signal NEVER fires it.
    p = TriggerPolicy(accumulation_threshold=1, is_llm=True, min_llm_batch=3)
    assert p.effective_accumulation == 3
    acc = TriggerAccumulator()
    acc, _ = _dirty(acc, "a", 1)
    assert not decide(acc, p, now=T0).should_fire  # 1 < 3


def test_llm_severity_wake_disabled_by_default():
    # A critical signal does NOT wake an LLM analyst unless explicitly allowed.
    p = TriggerPolicy(severity_gate="critical", accumulation_threshold=5, is_llm=True)
    assert p.severity_gate_rank == 99  # unreachable
    acc = TriggerAccumulator()
    acc, _ = _dirty(acc, "a", 4)  # critical
    assert not decide(acc, p, now=T0).should_fire

    p2 = TriggerPolicy(
        severity_gate="critical", accumulation_threshold=5, is_llm=True,
        allow_llm_severity_wake=True,
    )
    assert p2.severity_gate_rank == 4
    assert decide(acc, p2, now=T0).reason is TriggerReason.SEVERITY


def test_deterministic_keeps_per_signal_accumulation():
    p = TriggerPolicy(accumulation_threshold=1, is_llm=False)
    assert p.effective_accumulation == 1


# ---------------------------------------------------------------------------
# descriptor → policy mapping
# ---------------------------------------------------------------------------


class _Cadence:
    def __init__(self, cooldown_seconds=0, fallback_schedule=None):
        self.cooldown_seconds = cooldown_seconds
        self.fallback_schedule = fallback_schedule


def test_policy_from_descriptor_deterministic():
    p = policy_from_descriptor(
        cadence=_Cadence(cooldown_seconds=120, fallback_schedule="*/5 * * * *"),
        method_kind="deterministic",
        accumulation_threshold=4,
        severity_gate="critical",
    )
    assert p.cooldown_seconds == 120
    assert p.cadence_seconds == 300  # */5 → 5min
    assert p.accumulation_threshold == 4
    assert p.severity_gate == "critical"
    assert p.is_llm is False
    assert p.severity_gate_rank == 4


def test_policy_from_descriptor_llm_is_guarded():
    p = policy_from_descriptor(
        cadence=_Cadence(cooldown_seconds=0),
        method_kind="llm_single_turn",
        accumulation_threshold=1,  # operator tried per-signal
        severity_gate="critical",
    )
    assert p.is_llm is True
    assert p.effective_accumulation >= 2  # floored — never per-signal
    assert p.severity_gate_rank == 99     # severity wake off by default
