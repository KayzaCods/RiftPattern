"""Policy delta vs el jugador de referencia (#14, redirigido): ¿en QUE decision divergimos?

exposure_v1 falsifico la hipotesis POSICIONAL (mis muertes de exposicion pasan
en mi propia mitad). Un estudio de 560k partidas dice por que: el combate
individual es plano entre tiers; lo que separa Master de Challenger es la vision
y la conducta forward-looking. Asi que #14 se compara en el eje que SI mueve el
tier: la distribucion de la ACCION elegida en setup de objetivo, mis cuentas vs
el jugador de referencia (one-trick), no la profundidad en el mapa.

Con n chico por bucket (~118 decisiones de objetivo del jugador de referencia), Wald da
intervalos poco fiables; se usa Jeffreys (Beta(1/2,1/2) por categoria), cuyo
cuantil se computa a mano (beta incompleta regularizada + biseccion, stdlib).
Es fuente de HIPOTESIS con n declarado, no de verdad.

Modulo puro (criterio + estadistica). El script scripts/policy_delta.py hace la
I/O y el reporte; NO se escribe nada en la DB.
"""
from __future__ import annotations

import math

# Bandas de ventaja de EQUIPO. Mismos cortes que objective_readiness (+/-3000):
# consistencia con el detector del que salen las decisiones.
TEAM_GOLD_EDGE = 3000
LATE_FROM_MS = 20 * 60_000     # setup de objetivo: temprano vs tardio (souls/elder)


# --- beta incompleta regularizada I_x(a,b) (Numerical Recipes) --------------
def _betacf(a, b, x, itmax: int = 300, eps: float = 3e-12) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """I_x(a,b): CDF regularizada de la Beta(a,b) en x."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_bt = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log(1.0 - x))
    bt = math.exp(ln_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _beta_quantile(p: float, a: float, b: float) -> float:
    """Inversa de _betai por biseccion (monotona en x)."""
    lo, hi = 0.0, 1.0
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if _betai(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def jeffreys_ci(count: int, total: int, alpha: float = 0.05) -> tuple:
    """Intervalo creible de Jeffreys para una proporcion count/total.

    Posterior Beta(count+1/2, total-count+1/2). Convencion Brown-Cai-DasGupta:
    lo=0 cuando count=0 y hi=1 cuando count=total (si no, el intervalo daria un
    borde diminuto no-cero/no-uno que confunde). total=0 -> intervalo completo."""
    if total <= 0:
        return (0.0, 1.0)
    a = count + 0.5
    b = total - count + 0.5
    lo = 0.0 if count == 0 else _beta_quantile(alpha / 2.0, a, b)
    hi = 1.0 if count == total else _beta_quantile(1.0 - alpha / 2.0, a, b)
    return (round(lo, 3), round(hi, 3))


def action_distribution(action_ids, alpha: float = 0.05) -> dict:
    """{action_id: (count, prop, ic_lo, ic_hi)} ordenado por count desc. El IC es
    Jeffreys marginal por categoria (cada accion como binomial count/total)."""
    total = len(action_ids)
    counts: dict = {}
    for a in action_ids:
        counts[a] = counts.get(a, 0) + 1
    out = {}
    for a, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        lo, hi = jeffreys_ci(n, total, alpha)
        out[a] = (n, n / total, lo, hi)
    return out


def prop_ci(dist: dict, total: int, aid: str) -> tuple:
    """(prop, ic_lo, ic_hi) de una accion en una distribucion. Si la accion esta
    AUSENTE es count 0 sobre el total de esa cohorte -> Jeffreys(0, total), NO
    [0,0]: una accion que un jugador nunca eligio sigue teniendo incertidumbre
    hacia arriba con n chico. Confundir eso con [0,0] fabrica divergencias falsas."""
    if aid in dist:
        _, prop, lo, hi = dist[aid]
        return (prop, lo, hi)
    lo, hi = jeffreys_ci(0, total)
    return (0.0, lo, hi)


# --- buckets ----------------------------------------------------------------
def gold_band_of(team_gold_diff) -> str:
    if team_gold_diff is None:
        return "even"
    if team_gold_diff <= -TEAM_GOLD_EDGE:
        return "behind"
    if team_gold_diff >= TEAM_GOLD_EDGE:
        return "ahead"
    return "even"


def phase_of(game_time_ms) -> str:
    return "tardio" if (game_time_ms or 0) >= LATE_FROM_MS else "temprano"


def bucket_of(game_time_ms, team_gold_diff) -> tuple:
    return (phase_of(game_time_ms), gold_band_of(team_gold_diff))
