"""Validacion de exposure_v1 contra el anchor set (#17).

El detector encuentra 478 episodios, pero eso no dice si encuentra los CORRECTOS.
El anchor set son las muertes que el jugador marco en desacuerdo y etiqueto como
"el error real fue estar EXPUESTO antes de morir" (si) o no. Aqui vive la
matematica pura que responde: la senal del detector, ¿separa unas de otras?

Dos medidas, ambas stdlib:

- `auc`: proxy-AUC (Mann-Whitney). Probabilidad de que una muerte etiquetada "si"
  puntue mas alto que una etiquetada "no". 0.5 = el detector no distingue nada.
  Los EMPATES cuentan 0.5 — esencial aqui, porque toda muerte donde el detector
  no disparo puntua 0.0 y los empates son masivos.
- `permutation_p`: el filtro honesto. Con n=48 un AUC alto puede salir por azar;
  se baraja la etiqueta muchas veces y se mide que fraccion de barajadas iguala o
  supera el AUC observado. No asume normalidad ni independencia entre grupos, que
  es justo lo que no se puede asumir con este n.
"""
from __future__ import annotations

import random
from typing import Optional

PERM_SEED = 13        # misma disciplina que preference.CV_SEED: reproducible
PERM_N = 10_000


def auc(pos_scores, neg_scores) -> Optional[float]:
    """Proxy-AUC de Mann-Whitney: P(pos > neg) + 0.5 * P(pos == neg).

    None si falta alguno de los dos grupos (no hay nada que separar)."""
    if not pos_scores or not neg_scores:
        return None
    wins = 0.0
    for p in pos_scores:
        for n in neg_scores:
            if p > n:
                wins += 1.0
            elif p == n:
                wins += 0.5
    return wins / (len(pos_scores) * len(neg_scores))


def permutation_p_stratified(pos_scores, neg_scores, pos_strata, neg_strata,
                             n_perm: int = PERM_N,
                             seed: int = PERM_SEED) -> Optional[float]:
    """Como `permutation_p`, pero barajando la etiqueta SOLO DENTRO de cada
    estrato. Controla un confusor por construccion: si la separacion se explica
    por el estrato (p. ej. "habia pelea", donde el APM es alto por definicion),
    permutar dentro del estrato la destruye y el p sale alto. Una separacion que
    vive DENTRO de los estratos sobrevive.

    El estadistico sigue siendo el AUC GLOBAL (comparable con `auc`), pero su
    distribucion nula se construye respetando los estratos."""
    obs = auc(pos_scores, neg_scores)
    if obs is None:
        return None
    # {estrato: [valores...]} con cuantos positivos aporta cada uno
    buckets: dict = {}
    n_pos_by: dict = {}
    for v, s in zip(pos_scores, pos_strata):
        buckets.setdefault(s, []).append(v)
        n_pos_by[s] = n_pos_by.get(s, 0) + 1
    for v, s in zip(neg_scores, neg_strata):
        buckets.setdefault(s, []).append(v)
        n_pos_by.setdefault(s, 0)

    rng = random.Random(seed)
    hits = 0
    for _ in range(n_perm):
        p_side, n_side = [], []
        for s, vals in buckets.items():
            rng.shuffle(vals)
            k = n_pos_by[s]
            p_side.extend(vals[:k])
            n_side.extend(vals[k:])
        a = auc(p_side, n_side)
        if a is not None and a >= obs:
            hits += 1
    return (hits + 1) / (n_perm + 1)


def permutation_p(pos_scores, neg_scores, n_perm: int = PERM_N,
                  seed: int = PERM_SEED) -> Optional[float]:
    """p-valor de una cola: fraccion de barajadas de la ETIQUETA cuyo AUC iguala
    o supera al observado. Correccion +1 en numerador y denominador (con n_perm
    finito no se puede afirmar p=0). Determinista por la semilla."""
    obs = auc(pos_scores, neg_scores)
    if obs is None:
        return None
    pool = list(pos_scores) + list(neg_scores)
    n_pos = len(pos_scores)
    rng = random.Random(seed)
    hits = 0
    for _ in range(n_perm):
        rng.shuffle(pool)
        if auc(pool[:n_pos], pool[n_pos:]) >= obs:
            hits += 1
    return (hits + 1) / (n_perm + 1)
