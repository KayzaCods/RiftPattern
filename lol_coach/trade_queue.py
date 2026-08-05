"""Informativeness ranking for the trade-feedback queue (backlog #5).

trade_v1 is the detector most faithful to the player's criterion and the most
starved of marks. Marking dozens blindly is slow, so the queue should surface
first the unmarked lost trades whose mark teaches the Bradley-Terry learner
(preference.py) the most. The criterion is HYBRID, not pure uncertainty:

- Fisher information ``p*(1-p)`` of the comparison the mark would create. For a
  decision we take the MAX over the taken action vs each alternative under the
  ACTIVE weights: it is as informative as its most ambiguous available
  comparison. ``p = sigma(w . (terms_alt - terms_taken))``. A pair with no HEF
  signal at all (all-zero delta) scores 0 -- it mirrors how the learner drops
  those rows as noise, instead of the degenerate p=0.5 -> 0.25 that pure
  uncertainty would assign it.

- Coverage: a state bucket (lane phase x local-gold band x champion) that is
  already crowded -- by marks ALREADY made or by higher-ranked queue items -- is
  down-weighted ``1/(1+n)``. Greedy selection then spreads the scarce marks over
  the state space instead of piling them on the decision boundary, which is the
  documented failure mode of uncertainty sampling with small n and a possibly
  miscalibrated model (Shen et al. ICML 2025; Fisher-as-Laplacian D-optimal
  design for Bradley-Terry, arXiv 2601.14727).

Pure and DB-free: callers resolve HEF terms per action (via
``features.hef_score``) and pass items carrying ``terms_by_action``. This module
only decides the ORDER; it never touches the detector or the criterion.
"""
from __future__ import annotations

import math

from lol_coach.decisions.features import hef_score
from lol_coach.preference import DEFAULT_WEIGHTS, DIMS

# Lane-phase split of the trade_v1 window (fires ~min 2-14).
_EARLY_MAX_MS = 7 * 60_000    # < min 7  -> early
_MID_MAX_MS = 11 * 60_000     # < min 11 -> mid; else late

# Local gold advantage band. ~350g is roughly a wave of CS: below/above it the
# lane state is meaningfully behind/ahead, in between it is even.
_GOLD_EDGE = 350


def phase_of(game_time_ms) -> str:
    ms = game_time_ms or 0
    if ms < _EARLY_MAX_MS:
        return "early"
    if ms < _MID_MAX_MS:
        return "mid"
    return "late"


def gold_band_of(local_gold_diff) -> str:
    if local_gold_diff is None:
        return "even"
    if local_gold_diff < -_GOLD_EDGE:
        return "behind"
    if local_gold_diff > _GOLD_EDGE:
        return "ahead"
    return "even"


def bucket_of(item) -> tuple:
    """State bucket used for coverage: (lane phase, gold band, champion).

    The taken action is constant for trade_v1 (always fight_entry), so it is a
    dead coverage axis. Champion is what actually varies and what progress is
    tracked by (backlog #17-S5: progress separable per champion), so the scarce
    marks spread across champions instead of piling on the most-played one."""
    return (
        phase_of(item.get("game_time_ms")),
        gold_band_of(item.get("local_gold_diff")),
        item.get("champion"),
    )


def _delta(terms_hi: dict, terms_lo: dict) -> list[float]:
    """terms_hi - terms_lo per HEF dim; a dim absent on EITHER side votes 0 (no
    signal), exactly like preference._delta so the queue and the learner agree
    on what counts as an all-zero, worthless comparison."""
    out = []
    for d in DIMS:
        hi, lo = terms_hi.get(d), terms_lo.get(d)
        out.append((hi - lo) if (hi is not None and lo is not None) else 0.0)
    return out


def fisher_of_pair(terms_a: dict, terms_b: dict, weights: dict) -> float:
    """Fisher information p(1-p) of an a-vs-b Bradley-Terry comparison under
    ``weights``. Symmetric in a/b (p(1-p) is even in z). No-signal pairs -> 0."""
    delta = _delta(terms_a, terms_b)
    if all(v == 0.0 for v in delta):
        return 0.0
    z = sum(weights.get(DIMS[j], 0.0) * delta[j] for j in range(len(DIMS)))
    p = 1.0 / (1.0 + math.exp(-z))
    return p * (1.0 - p)


def fisher_signal(item, weights: dict) -> float:
    """Max Fisher information over the taken action vs each alternative. The
    mark is worth as much as the most ambiguous comparison it could resolve."""
    tba = item.get("terms_by_action") or {}
    taken = item.get("taken_action_id")
    terms_taken = tba.get(taken) or {}
    best = 0.0
    for aid, terms_alt in tba.items():
        if aid == taken:
            continue
        best = max(best, fisher_of_pair(terms_taken, terms_alt, weights))
    return best


def item_from_context(decision_id, game_time_ms, context: dict, champion=None) -> dict:
    """Turn a trade decision's parsed ``context_json`` into a rankable item.

    ``champion`` comes from the DB (not the context) and lands in the coverage
    bucket. Resolves HEF terms for the taken action AND every available
    alternative via ``features.hef_score`` (pure, no DB), so the ranking can
    score the taken-vs-alternative comparisons the mark would create."""
    sf = context.get("state_features") or {}
    action = context.get("action") or {}
    taken = action.get("action_id")
    aids = {taken} | {
        a.get("action_id") for a in (action.get("available_actions") or [])
    }
    aids.discard(None)
    return {
        "id": decision_id,
        "game_time_ms": game_time_ms,
        "local_gold_diff": (sf.get("power") or {}).get("local_gold_diff"),
        "taken_action_id": taken,
        "champion": champion,
        "terms_by_action": {
            aid: (hef_score(sf, aid).get("terms") or {}) for aid in aids
        },
    }


def rank(items, weights: dict, marked_bucket_counts=None) -> list:
    """Order items by Fisher x coverage, greedily. Each returned item is a copy
    annotated with ``fisher`` and ``bucket``.

    Coverage counts start from ``marked_bucket_counts`` (buckets already covered
    by human marks) and grow as items are placed, so the ordering diversifies
    across the state space. Ties break toward the earlier item in ``items``."""
    counts = dict(marked_bucket_counts or {})
    scored = [(fisher_signal(it, weights), bucket_of(it), it) for it in items]
    out = []
    while scored:
        best_i, best_adj = 0, None
        for i, (fisher, bucket, _it) in enumerate(scored):
            adj = fisher / (1 + counts.get(bucket, 0))
            if best_adj is None or adj > best_adj:
                best_adj, best_i = adj, i
        fisher, bucket, it = scored.pop(best_i)
        out.append({**it, "fisher": fisher, "bucket": bucket})
        counts[bucket] = counts.get(bucket, 0) + 1
    return out
