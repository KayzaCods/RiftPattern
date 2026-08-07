"""Tests del policy delta vs el jugador de referencia (#14): IC de Jeffreys + distribucion de accion.

El eje es comparar QUE accion elijo yo vs el jugador de referencia en setup de objetivo, con n
chico por bucket (la referencia ~118 decisiones). Wald falla en ese regimen; se usa
Jeffreys (Beta(1/2,1/2) por categoria). La pieza que hay que clavar es el cuantil
de la Beta sin scipy: beta incompleta regularizada + biseccion.
"""
from __future__ import annotations

from lol_coach import policy_delta as PD


# --- beta incompleta regularizada (bloque del IC) ---------------------------
def test_betai_matches_textbook_value():
    # I_0.5(2,3) = 0.6875 exacto (CDF de Beta(2,3) en 0.5).
    assert abs(PD._betai(2.0, 3.0, 0.5) - 0.6875) < 1e-6


def test_betai_boundaries():
    assert PD._betai(2.0, 3.0, 0.0) == 0.0
    assert PD._betai(2.0, 3.0, 1.0) == 1.0


def test_beta_quantile_inverts_betai():
    # El cuantil q en p debe cumplir betai(a,b,q) == p (auto-consistencia).
    for p in (0.025, 0.5, 0.975):
        q = PD._beta_quantile(p, 3.5, 8.5)
        assert abs(PD._betai(3.5, 8.5, q) - p) < 1e-4


# --- IC de Jeffreys ---------------------------------------------------------
def test_jeffreys_ci_contains_the_proportion():
    lo, hi = PD.jeffreys_ci(3, 10)
    assert lo < 0.3 < hi
    assert 0.0 <= lo < hi <= 1.0


def test_jeffreys_ci_is_symmetric_under_success_failure_swap():
    # Jeffreys(k,n) y Jeffreys(n-k,n) son espejo: (lo,hi) <-> (1-hi,1-lo).
    lo, hi = PD.jeffreys_ci(2, 10)
    lo2, hi2 = PD.jeffreys_ci(8, 10)
    assert abs(lo - (1 - hi2)) < 1e-3
    assert abs(hi - (1 - lo2)) < 1e-3


def test_jeffreys_ci_zero_count_pins_lower_to_zero():
    # Convencion Brown-Cai-DasGupta: x=0 -> lo=0 ; x=n -> hi=1.
    lo, hi = PD.jeffreys_ci(0, 10)
    assert lo == 0.0 and 0.0 < hi < 0.4
    lo, hi = PD.jeffreys_ci(10, 10)
    assert hi == 1.0 and 0.6 < lo < 1.0


def test_jeffreys_ci_no_data_is_the_full_interval():
    assert PD.jeffreys_ci(0, 0) == (0.0, 1.0)


# --- distribucion de accion -------------------------------------------------
def test_action_distribution_props_sum_to_one():
    d = PD.action_distribution(["fight_entry", "vision_setup", "vision_setup", "ignore_objective"])
    assert abs(sum(v[1] for v in d.values()) - 1.0) < 1e-9
    assert d["vision_setup"][0] == 2               # count
    assert abs(d["vision_setup"][1] - 0.5) < 1e-9  # prop


def test_action_distribution_single_action_is_prop_one_but_ci_not_degenerate():
    # Una sola accion en n=6: prop 1.0, pero el IC de Jeffreys NO colapsa a [1,1]
    # (esa es la razon de usar Jeffreys: n chico no certeza total).
    d = PD.action_distribution(["vision_setup"] * 6)
    cnt, prop, lo, hi = d["vision_setup"]
    assert cnt == 6 and prop == 1.0
    assert hi == 1.0 and lo < 0.7    # sigue habiendo incertidumbre hacia abajo


def test_action_distribution_empty_is_empty():
    assert PD.action_distribution([]) == {}


def test_prop_ci_absent_action_is_zero_over_total_not_degenerate():
    # Una accion que la referencia nunca eligio es count 0 sobre SU total, no [0,0]:
    # con n=11 el IC superior es ~24%, asi que NO es una divergencia frente a un
    # 3% mio. Este era el bug que fabricaba divergencias falsas.
    dist = PD.action_distribution(["ignore_objective"] * 6 + ["fight_entry"] * 5)  # sin vision
    prop, lo, hi = PD.prop_ci(dist, 11, "vision_setup")
    assert prop == 0.0 and lo == 0.0 and hi > 0.15


def test_prop_ci_present_action_matches_distribution():
    dist = PD.action_distribution(["a", "a", "b"])
    prop, lo, hi = PD.prop_ci(dist, 3, "a")
    assert prop == dist["a"][1] and (lo, hi) == (dist["a"][2], dist["a"][3])


def test_action_distribution_is_deterministic():
    a = PD.action_distribution(["a", "b", "a"])
    b = PD.action_distribution(["a", "b", "a"])
    assert a == b


# --- buckets ----------------------------------------------------------------
def test_gold_band_uses_team_gold_thresholds():
    assert PD.gold_band_of(-4000) == "behind"    # <= -3000, mismo corte del detector
    assert PD.gold_band_of(0) == "even"
    assert PD.gold_band_of(4000) == "ahead"      # >= 3000
    assert PD.gold_band_of(None) == "even"


def test_phase_splits_early_late_for_objectives():
    assert PD.phase_of(10 * 60_000) == "temprano"
    assert PD.phase_of(25 * 60_000) == "tardio"
