"""Tests for the trade-queue informativeness ranking (trade_queue.py).

The queue reorders UNMARKED lost trades so the scarce human marks land where
they teach the Bradley-Terry learner the most. Two signals, combined greedily:

- Fisher information p(1-p) of the comparison the mark would create (max over
  the taken action vs each alternative under the ACTIVE weights). Peaks when the
  model is unsure which action is better; ~0 when one action dominates. A pair
  with NO HEF signal at all (identical terms) scores 0, not 0.25 -- it mirrors
  how the learner drops all-zero-delta rows as pure noise.
- Coverage: a state bucket (lane phase x gold band x taken action) already
  crowded -- by marks already made OR by higher-ranked queue items -- is
  down-weighted 1/(1+n), so marks spread across the state space instead of
  piling up on the decision boundary (the documented uncertainty-sampling bias).

The math is pure and DB-free: items carry pre-resolved terms per action.
"""
from __future__ import annotations

from lol_coach import trade_queue as TQ


# --------------------------------------------------------------- bucket pieces
def test_phase_of_splits_the_laning_window():
    assert TQ.phase_of(120_000) == "early"   # min 2
    assert TQ.phase_of(480_000) == "mid"     # min 8
    assert TQ.phase_of(720_000) == "late"    # min 12


def test_gold_band_of_bins_local_gold_diff():
    assert TQ.gold_band_of(-500) == "behind"
    assert TQ.gold_band_of(0) == "even"
    assert TQ.gold_band_of(500) == "ahead"
    assert TQ.gold_band_of(None) == "even"   # missing gold reads as even, never crashes


def test_bucket_of_is_phase_gold_champion():
    # The taken action is constant for trade_v1 (always fight_entry), so it is a
    # dead coverage axis; champion is what actually varies and what progress is
    # tracked by (backlog #17-S5). Bucket = phase x gold band x champion.
    item = {
        "game_time_ms": 480_000,
        "local_gold_diff": 500,
        "champion": "Sona",
    }
    assert TQ.bucket_of(item) == ("mid", "ahead", "Sona")


# ----------------------------------------------------------------- Fisher math
W_POWER = {"power": 1.0, "info": 0.0, "action_fit": 0.0, "wave": 0.0}


def test_fisher_of_pair_peaks_at_a_tie():
    # z = 0 (a genuine tie with signal present) -> p = 0.5 -> p(1-p) = 0.25
    f = TQ.fisher_of_pair({"power": 0.5, "info": 0.5}, {"power": 0.5, "info": 0.9}, W_POWER)
    assert round(f, 4) == 0.25


def test_fisher_of_pair_falls_as_one_action_pulls_ahead():
    # With normalized weights and terms in [0,1], z stays in ~[-1,1] so Fisher
    # lives in a narrow [~0.196, 0.25] band -- it MONOTONICALLY drops off the
    # tie, and only a large gap drives it near zero. (This narrow range is why
    # the criterion is hybrid: coverage does most of the ordering work.)
    tie = TQ.fisher_of_pair({"power": 0.5, "info": 0.5}, {"power": 0.5, "info": 0.9}, W_POWER)
    lean = TQ.fisher_of_pair({"power": 0.9}, {"power": 0.1}, W_POWER)      # z=0.8
    lopsided = TQ.fisher_of_pair({"power": 3.0}, {"power": 0.0}, W_POWER)  # z=3.0
    assert lopsided < lean < tie
    assert lopsided < 0.10


def test_fisher_of_pair_is_zero_when_there_is_no_signal():
    # identical terms -> all-zero delta -> the learner would drop it; it is NOT
    # a maximally-informative 0.25, it is worthless.
    f = TQ.fisher_of_pair({"power": 0.5, "info": 0.5}, {"power": 0.5, "info": 0.5}, W_POWER)
    assert f == 0.0


def test_fisher_signal_takes_the_closest_contender():
    # two alternatives: one lopsided (low fisher), one tied (fisher 0.25). The
    # decision is as informative as its MOST ambiguous available comparison.
    item = {
        "taken_action_id": "fight_entry",
        "terms_by_action": {
            "fight_entry": {"power": 0.5, "info": 0.5},
            "disengage": {"power": 0.5, "info": 0.9},   # tie -> 0.25
            "all_in": {"power": 0.1},                    # lopsided -> low
        },
    }
    assert round(TQ.fisher_signal(item, W_POWER), 4) == 0.25


# ------------------------------------------------------------- greedy ordering
def _tie_item(id, gtm, gold, champion):
    """An item whose taken vs alt is a signal-present tie -> fisher 0.25."""
    return {
        "id": id, "game_time_ms": gtm, "local_gold_diff": gold,
        "champion": champion, "taken_action_id": "fight_entry",
        "terms_by_action": {
            "fight_entry": {"power": 0.5, "info": 0.5},
            "other": {"power": 0.5, "info": 0.9},
        },
    }


def _skewed_item(id, gtm, gold, champion):
    """Taken vs alt is a moderate skew -> fisher ~0.24 (just under a tie)."""
    return {
        "id": id, "game_time_ms": gtm, "local_gold_diff": gold,
        "champion": champion, "taken_action_id": "fight_entry",
        "terms_by_action": {
            "fight_entry": {"power": 0.2},
            "other": {"power": 0.6},   # z = 0.4 -> fisher ~0.240
        },
    }


def test_rank_orders_by_fisher_when_buckets_differ():
    hi = _tie_item(1, 120_000, 0, "Sona")          # fisher 0.25, bucket A
    lo = _skewed_item(2, 720_000, 500, "Ashe")     # fisher ~0.24, bucket B
    ranked = TQ.rank([lo, hi], W_POWER)
    assert [it["id"] for it in ranked] == [1, 2]
    assert ranked[0]["fisher"] > ranked[1]["fisher"]


def test_rank_diversifies_away_from_a_crowded_bucket():
    # A and B share a bucket (both fisher 0.25); C is a fresh bucket at lower
    # fisher. Greedy coverage pulls C ahead of the second same-bucket item.
    a = _tie_item(1, 120_000, 0, "Sona")           # bucket X, 0.25
    b = _tie_item(2, 120_000, 0, "Sona")           # bucket X, 0.25
    c = _skewed_item(3, 720_000, 500, "Ashe")      # bucket Y, 0.24
    ranked = TQ.rank([a, b, c], W_POWER)
    assert [it["id"] for it in ranked] == [1, 3, 2]


def test_rank_penalizes_already_marked_buckets():
    a = _tie_item(1, 120_000, 0, "Sona")           # bucket X, 0.25
    c = _skewed_item(2, 720_000, 500, "Ashe")      # bucket Y, 0.24
    marked = {("early", "even", "Sona"): 3}        # X is already well covered
    ranked = TQ.rank([a, c], W_POWER, marked)
    assert [it["id"] for it in ranked] == [2, 1]


def test_rank_annotates_fisher_and_bucket():
    a = _tie_item(1, 120_000, 0, "Sona")
    ranked = TQ.rank([a], W_POWER)
    assert ranked[0]["bucket"] == ("early", "even", "Sona")
    assert round(ranked[0]["fisher"], 4) == 0.25


# ------------------------------------------ decision context -> rankable item
def test_item_from_context_pulls_action_gold_and_terms():
    ctx = {
        "state_features": {
            "power": {"local_gold_diff": 260, "power_index": 0.52},
            "info_risk": {"safety_ratio": 0.9},
            "wave_tempo": {"wave_state": "frozen_near_tower"},
        },
        "action": {
            "action_id": "fight_entry",
            "available_actions": [
                {"action_id": "disengage", "ev_model": 0.72},
                {"action_id": "fight_entry", "ev_model": 0.42},
            ],
        },
    }
    item = TQ.item_from_context(24689, 540_000, ctx, champion="Sona")
    assert item["id"] == 24689
    assert item["game_time_ms"] == 540_000
    assert item["local_gold_diff"] == 260
    assert item["taken_action_id"] == "fight_entry"
    assert item["champion"] == "Sona"
    # every available action (taken + alternatives) resolves to a terms dict
    assert set(item["terms_by_action"]) == {"fight_entry", "disengage"}
    assert isinstance(item["terms_by_action"]["disengage"], dict)
    # champion lands in the coverage bucket; end-to-end rankable
    assert TQ.bucket_of(item) == ("mid", "even", "Sona")
    assert TQ.fisher_signal(item, TQ.DEFAULT_WEIGHTS) >= 0.0
