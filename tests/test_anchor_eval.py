"""Tests de la validacion del anchor set (#17): AUC + prueba de permutacion.

Mide si la senal de exposure_v1 separa las muertes que el jugador atribuye a
EXPOSICION de las que atribuye a otra cosa. Con n chico (48) y muchos empates
(las muertes donde el detector no disparo puntuan 0), los dos puntos faciles de
equivocar son el manejo de EMPATES en el AUC y la correccion +1 del p-valor.
"""
from __future__ import annotations

from lol_coach import anchor_eval as AE


def test_auc_perfect_separation_is_one():
    assert AE.auc([0.9, 0.8, 0.7], [0.3, 0.2]) == 1.0


def test_auc_reversed_separation_is_zero():
    assert AE.auc([0.1, 0.2], [0.8, 0.9]) == 0.0


def test_auc_counts_ties_as_half():
    # Todos empatados: el detector no distingue nada -> 0.5 exacto. Este es el
    # caso REAL cuando el detector no disparo en ningun ancla (todos 0.0).
    assert AE.auc([0.0, 0.0], [0.0, 0.0]) == 0.5
    # Un empate parcial: pos>neg en 1 de 2 pares, empate en el otro -> 0.75
    assert AE.auc([0.5], [0.5, 0.1]) == 0.75


def test_auc_without_one_of_the_groups_is_none():
    assert AE.auc([], [0.1]) is None
    assert AE.auc([0.1], []) is None


def test_permutation_p_is_high_when_groups_are_indistinguishable():
    # Mismos valores en ambos grupos: barajar no cambia nada -> p cercano a 1.
    p = AE.permutation_p([0.5, 0.5, 0.5], [0.5, 0.5, 0.5], n_perm=200, seed=13)
    assert p > 0.9


def test_permutation_p_is_low_under_clean_separation():
    p = AE.permutation_p([0.9, 0.8, 0.85, 0.95], [0.1, 0.2, 0.15, 0.05],
                         n_perm=500, seed=13)
    assert p < 0.05


def test_stratified_permutation_kills_a_confounded_separation():
    """El caso que motiva la estratificacion: la separacion es PURO estrato.

    Estrato A (sin pelea) tiene valores bajos, estrato B (con pelea) altos, y
    resulta que casi todos los 'pos' cayeron en A y los 'neg' en B. Sin
    estratificar parece separacion perfecta; permutando DENTRO de cada estrato
    no queda nada que permutar y el p debe ser alto (no refutable como senal)."""
    pos = [1.0, 1.1, 1.2]          # todos del estrato A
    neg = [9.0, 9.1, 9.2]          # todos del estrato B
    strata_pos = ["A", "A", "A"]
    strata_neg = ["B", "B", "B"]
    assert AE.auc(pos, neg) == 0.0                      # separacion total (invertida)
    p = AE.permutation_p_stratified([-x for x in pos], [-x for x in neg],
                                    strata_pos, strata_neg, n_perm=200, seed=13)
    assert p > 0.9   # dentro de estrato no hay mezcla posible -> sin evidencia


def test_stratified_permutation_keeps_a_real_within_stratum_signal():
    # Aqui la separacion vive DENTRO de cada estrato, no entre estratos:
    # el control no debe borrarla.
    pos = [9.0, 9.1, 8.9, 9.2, 19.0, 19.1, 18.9, 19.2]
    neg = [1.0, 1.1, 0.9, 1.2, 11.0, 11.1, 10.9, 11.2]
    sp = ["A"] * 4 + ["B"] * 4
    sn = ["A"] * 4 + ["B"] * 4
    p = AE.permutation_p_stratified(pos, neg, sp, sn, n_perm=500, seed=13)
    assert p < 0.05


def test_stratified_permutation_ignores_strata_missing_a_group():
    # Un estrato con solo un grupo no aporta comparaciones; no debe romper.
    p = AE.permutation_p_stratified([1.0, 2.0], [0.5], ["A", "B"], ["A"],
                                    n_perm=100, seed=13)
    assert p is not None and 0.0 < p <= 1.0


def test_permutation_p_is_deterministic_and_never_zero():
    # La correccion +1 evita p=0 (con n_perm finito no se puede afirmar p=0), y
    # la semilla fija hace el resultado reproducible entre corridas.
    a = AE.permutation_p([0.9, 0.8], [0.1, 0.2], n_perm=100, seed=13)
    b = AE.permutation_p([0.9, 0.8], [0.1, 0.2], n_perm=100, seed=13)
    assert a == b
    assert a >= 1.0 / (100 + 1)
