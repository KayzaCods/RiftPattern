"""Patrones de derrota desde hechos Riot: criterio y estadistica sin I/O.

Fuente de hipotesis con n declarado; correlacion, no causa. No es detector,
no produce decisiones ni feedback. El script loss_patterns.py carga los datos.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import random


@dataclass
class KillEvent:
    t_ms: int
    victim_id: int
    killer_id: int
    assist_ids: tuple
    x: int | None
    y: int | None


@dataclass
class BuildingEvent:
    t_ms: int
    owner_team: int
    building_type: str


@dataclass
class EliteEvent:
    t_ms: int
    killer_team: int
    monster_type: str
    x: int | None
    y: int | None


@dataclass
class GameView:
    match_id: str
    win: bool
    duration_s: int
    our_id: int
    team_of: dict[int, int]
    gold_by_min: list[dict[int, int]]
    level_by_min: list[dict[int, int]]
    kills: list[KillEvent]
    buildings: list[BuildingEvent]
    elites: list[EliteEvent]


def _team_diff(game, frames, minute) -> int:
    if not frames:
        raise ValueError("La partida no tiene frames")
    if minute < 0:
        raise ValueError("El minuto no puede ser negativo")
    frame = frames[min(minute, len(frames) - 1)]
    ours = game.team_of[game.our_id]
    return sum(value if game.team_of[pid] == ours else -value
               for pid, value in frame.items())


def team_gold_diff(game, minute) -> int:
    """Oro propio menos rival; despues del ultimo frame usa ese ultimo."""
    return _team_diff(game, game.gold_by_min, minute)


def team_level_diff(game, minute) -> int:
    """Suma de niveles propios menos rivales, con el mismo corte de frames."""
    return _team_diff(game, game.level_by_min, minute)


def kill_diff(game, t_ms) -> int:
    """Kills hasta t inclusive; un killer ajeno a participantes no cuenta."""
    ours = game.team_of[game.our_id]
    return sum(1 if game.team_of[k.killer_id] == ours else -1
               for k in game.kills
               if k.t_ms <= t_ms and k.killer_id in game.team_of)


def tower_diff(game, t_ms) -> int:
    """Torres rivales caidas menos propias; teamId es el DUENO."""
    ours = game.team_of[game.our_id]
    return sum(-1 if b.owner_team == ours else 1
               for b in game.buildings
               if b.t_ms <= t_ms and b.building_type == "TOWER_BUILDING"
               and b.owner_team in (100, 200))


def our_deaths(game) -> list[KillEvent]:
    """Muertes propias ordenadas por timestamp, sin cambiar la partida."""
    return sorted((k for k in game.kills if k.victim_id == game.our_id),
                  key=lambda k: k.t_ms)


EARLY_CUTOFF_MS = 840_000   # minuto 14: fin de la fase temprana
CHAIN_GAP_MS = 96_000       # 1.6 min: muerte "encadenada" a la anterior


def early_deaths(game, cutoff_ms=EARLY_CUTOFF_MS) -> int:
    """Antes del corte estricto: una muerte en 14:00 ya es tardia."""
    return sum(k.t_ms < cutoff_ms for k in our_deaths(game))


def chained_deaths(game, cutoff_ms=EARLY_CUTOFF_MS, gap_ms=CHAIN_GAP_MS) -> int:
    """Cada muerte temprana a <= gap de la anterior propia cuenta una vez."""
    deaths = our_deaths(game)
    return sum(current.t_ms < cutoff_ms
               and current.t_ms - previous.t_ms <= gap_ms
               for previous, current in zip(deaths, deaths[1:]))


def wilson_ci(k, n, z=1.96) -> tuple[float, float]:
    """IC de Wilson binomial; sin muestra devuelve el intervalo completo."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return (0.0 if k == 0 else max(0.0, center - half),
            1.0 if k == n else min(1.0, center + half))


def win_rate_by_bucket(games, key, buckets) -> list[dict]:
    """Evalua key(game) contra cada predicado; tasas en fraccion, n=0 -> None."""
    values = [(key(game), game.win) for game in games]
    rows = []
    for label, predicate in buckets:
        selected = [win for value, win in values if predicate(value)]
        n, wins = len(selected), sum(selected)
        lo, hi = wilson_ci(wins, n)
        rows.append(dict(label=label, wins=wins, n=n,
                         rate=wins / n if n else None, lo=lo, hi=hi))
    return rows


def _death_exposure(game, predicate, window) -> tuple[int, float]:
    lo, hi = window
    minutes = max(0.0, min(game.duration_s / 60, hi) - lo)
    if minutes <= 0:
        return (0, 0.0)
    count = sum(lo * 60_000 <= k.t_ms < hi * 60_000
                and k.t_ms <= game.duration_s * 1000 and predicate(game, k)
                for k in our_deaths(game))
    return count, minutes


def _rate(samples) -> float | None:
    count = sum(pair[0] for pair in samples)
    minutes = sum(pair[1] for pair in samples)
    return 10 * count / minutes if minutes else None


def death_rate_per_10min(games, predicate, window) -> float:
    """Muertes por 10 minutos jugados en [lo, hi); duraciones en minutos.

    Sin exposicion devuelve cero; el informe debe declarar esa ausencia.
    """
    rate = _rate([_death_exposure(game, predicate, window) for game in games])
    return rate if rate is not None else 0.0


def _percentile(values, p) -> float:
    index = (len(values) - 1) * p
    lower = math.floor(index)
    upper = math.ceil(index)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def bootstrap_rate_diff(losses, wins, predicate, window, iters=2000, seed=1):
    """Derrotas menos victorias, bootstrap por PARTIDA e IC percentil 95%.

    El universo son partidas con tiempo jugado en la ventana. Cada muestra
    conserva juntas sus muertes y exposicion; sin una cohorte no se estima.
    """
    if iters <= 0:
        raise ValueError("iters debe ser positivo")
    loss_pairs = [pair for g in losses
                  if (pair := _death_exposure(g, predicate, window))[1] > 0]
    win_pairs = [pair for g in wins
                 if (pair := _death_exposure(g, predicate, window))[1] > 0]
    loss_rate, win_rate = _rate(loss_pairs), _rate(win_pairs)
    if loss_rate is None or win_rate is None:
        return (None, None, None)
    rng = random.Random(seed)
    differences = []
    for _ in range(iters):
        left = _rate([rng.choice(loss_pairs) for _ in loss_pairs])
        right = _rate([rng.choice(win_pairs) for _ in win_pairs])
        differences.append(left - right)
    differences.sort()
    return (loss_rate - win_rate, _percentile(differences, .025),
            _percentile(differences, .975))


# -6000 pertenece solo a la ultima banda: las bandas no se solapan.
DEFAULT_GOLD_BANDS = (
    (">= +3000", lambda gold: gold >= 3000),
    ("(-3000, +3000)", lambda gold: -3000 < gold < 3000),
    ("(-6000, -3000]", lambda gold: -6000 < gold <= -3000),
    ("<= -6000", lambda gold: gold <= -6000),
)


def objective_pit_risk(games, min_minute=14, radius=3000,
                       before_ms=60_000, after_ms=30_000,
                       bands=DEFAULT_GOLD_BANDS) -> list[dict]:
    """Riesgo por objetivo, no por muerte; radio y ventana son inclusivos.

    La posicion del ELITE es el centro. Sin coordenadas no se imputa cercania.
    Tasas en fraccion; varias muertes cercanas cuentan un solo objetivo.
    """
    rows = [dict(label=label, n=0, enemy_taken=0, deaths=0)
            for label, _ in bands]
    for game in games:
        ours = game.team_of[game.our_id]
        deaths = our_deaths(game)
        for event in game.elites:
            if (event.monster_type not in ("DRAGON", "BARON_NASHOR", "RIFTHERALD")
                    or not min_minute * 60_000 <= event.t_ms <= game.duration_s * 1000
                    or event.killer_team not in (100, 200)):
                continue
            gold = team_gold_diff(game, event.t_ms // 60_000)
            nearby_death = any(
                event.t_ms - before_ms <= death.t_ms <= event.t_ms + after_ms
                and death.t_ms <= game.duration_s * 1000
                and None not in (event.x, event.y, death.x, death.y)
                and (death.x - event.x) ** 2 + (death.y - event.y) ** 2 <= radius ** 2
                for death in deaths)
            for row, (_, predicate) in zip(rows, bands):
                if predicate(gold):
                    row["n"] += 1
                    row["enemy_taken"] += event.killer_team != ours
                    row["deaths"] += nearby_death
    return [dict(label=row["label"], n=row["n"],
                 enemy_taken_rate=row["enemy_taken"] / row["n"] if row["n"] else None,
                 death_rate=row["deaths"] / row["n"] if row["n"] else None)
            for row in rows]


def visible_deficit_rules(games, min_minute=14, threshold=-3000) -> list[dict]:
    """Precision/recall sobre observaciones partida-minuto, no independientes.

    Solo minutos enteros jugados con frames de oro Y nivel disponibles. Los
    denominadores vacios dan None, para no presentar certeza sin observaciones.
    """
    labels = ("kills abajo >=5", "torres abajo >=2", "niveles abajo >=3", "2 de 3")
    flagged, hits = [0] * 4, [0] * 4
    positives = 0
    for game in games:
        last = min(game.duration_s // 60, len(game.gold_by_min) - 1,
                   len(game.level_by_min) - 1)
        for minute in range(min_minute, last + 1):
            target = team_gold_diff(game, minute) <= threshold
            positives += target
            signals = [kill_diff(game, minute * 60_000) <= -5,
                       tower_diff(game, minute * 60_000) <= -2,
                       team_level_diff(game, minute) <= -3]
            signals.append(sum(signals) >= 2)
            for index, signal in enumerate(signals):
                flagged[index] += signal
                hits[index] += signal and target
    return [dict(rule=label,
                 precision=hits[i] / flagged[i] if flagged[i] else None,
                 recall=hits[i] / positives if positives else None,
                 n_flagged=flagged[i])
            for i, label in enumerate(labels)]
