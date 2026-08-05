"""Sobreextension EXPUESTA sin muerte (#17, Fase 1: report-only).

Todo `death_v1` condiciona a MORIR, asi que las exposiciones que sobreviviste son
invisibles para el sistema — y son la mayoria (medido: 93% de los estados expuestos
no terminaron en muerte). Ese es el sesgo de supervivencia que este modulo ataca:
detecta el ESTADO expuesto por frame-minuto, sin requerir muerte.

Fase 1 es REPORT-ONLY: no persiste decisiones, no pide feedback, no emite argumento.
Solo lee, agrega y lista para que el jugador mida la tasa de falsos positivos antes
de promover el detector.

El criterio vive SOLO aqui (misma disciplina que `progress.py`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from lol_coach.context import build_world_state, get_match_meta
from lol_coach.decisions.features import build_state_features
from lol_coach.decisions.respawn import player_dead_at

# --- Knobs del criterio (Fase 1 los calibra mirando la lista de FP) ---------
MAX_SAFETY_RATIO = 0.35     # <= esto = profundo (1 = tu torre, 0 = torre enemiga)
MIN_ENEMIES_UNSEEN = 2      # MIAs locales minimas (jungla enemiga siempre cuenta)
DEATH_WINDOW_MS = 15_000    # una muerte dentro de esta ventana se atribuye a la exposicion
SKIP_BEFORE_MS = 90_000     # antes de 1:30 no hay decision real (spawn/camino a linea)


@dataclass
class ExposureFacts:
    """Un frame-minuto en el que el jugador estaba expuesto. Plano y serializable:
    la capa de reporte no vuelve a tocar la DB para nada de esto."""
    match_id: str
    game_time_ms: int
    champion: Optional[str]
    safety_ratio: Optional[float]
    enemies_unseen: Optional[int]
    local_threat: Optional[float]
    local_gold_diff: Optional[int]
    died_within_s: Optional[int] = None   # None = sobrevivio (la senal nueva)


def is_exposed(safety_ratio, enemies_unseen) -> bool:
    """Profundo Y a ciegas. Datos faltantes -> False: nunca inventamos exposicion."""
    if safety_ratio is None or enemies_unseen is None:
        return False
    return safety_ratio <= MAX_SAFETY_RATIO and enemies_unseen >= MIN_ENEMIES_UNSEEN


def severity(local_threat) -> float:
    """Severidad continua 0..1 (profundidad + enemigos cerca, ya combinados por
    `features.build_state_features`). Ordena la lista revisable; NO decide si cuenta."""
    if local_threat is None:
        return 0.0
    return max(0.0, min(1.0, float(local_threat)))


# --- Buckets del reporte ----------------------------------------------------
# Cortes de FASE que cubren la partida entera. Deliberadamente distintos de
# `trade_queue.phase_of`, que solo cubre la ventana de lineas (min 2-14): la
# exposicion vive sobre todo en late (medido: 440 de 586).
EARLY_MAX_MS = 7 * 60_000
MID_MAX_MS = 15 * 60_000

GOLD_EDGE = 350   # ~una oleada de CS; mismos cortes que trade_queue.gold_band_of


def phase_of(game_time_ms) -> str:
    ms = game_time_ms or 0
    if ms < EARLY_MAX_MS:
        return "early"
    if ms < MID_MAX_MS:
        return "mid"
    return "late"


def gold_band_of(local_gold_diff) -> str:
    """La ventaja NO filtra si algo cuenta como exposicion (la evidencia medida
    dice que varias exposiciones reales fueron EN DESVENTAJA, en setup de vision);
    solo se ANOTA para el agregado."""
    if local_gold_diff is None:
        return "even"
    if local_gold_diff < -GOLD_EDGE:
        return "behind"
    if local_gold_diff > GOLD_EDGE:
        return "ahead"
    return "even"


def _count(pairs) -> dict:
    out: dict = {}
    for k in pairs:
        out[k] = out.get(k, 0) + 1
    return out


# --- Episodios: la unidad correcta de conteo --------------------------------
# scan_match emite un frame-minuto por cada minuto expuesto y vivo, sin dedup.
# Una sobreextension sostenida de min 18->20 son 3 frames autocorrelados (60s de
# separacion en el timeline de Match-V5), casi el mismo estado. Contar frames
# infla el N efectivo (design effect / ESS) y vuelve OPTIMISTA cualquier cota de
# FP futura. La unidad correcta es el EPISODIO: un near-miss discreto (la decision
# de quedarte expuesto), no uno por minuto que dura. El merge es un umbral de gap.
EPISODE_GAP_MS = 90_000   # <= 1.5 frames une; hueco mayor corta en episodios distintos


@dataclass
class ExposureEpisode:
    """Una sobreextension SOSTENIDA (uno o mas frames-minuto consecutivos). La
    muerte pertenece al episodio (cualquier frame), no al minuto."""
    match_id: str
    start_ms: int
    end_ms: int
    n_frames: int
    champion: Optional[str]
    peak_severity: float
    worst_safety_ratio: Optional[float]
    max_enemies_unseen: Optional[int]
    gold_band: str
    died: bool


def _episode_of(frames) -> ExposureEpisode:
    f0 = frames[0]
    srs = [f.safety_ratio for f in frames if f.safety_ratio is not None]
    eus = [f.enemies_unseen for f in frames if f.enemies_unseen is not None]
    return ExposureEpisode(
        match_id=f0.match_id,
        start_ms=f0.game_time_ms,
        end_ms=frames[-1].game_time_ms,
        n_frames=len(frames),
        champion=f0.champion,
        peak_severity=max(severity(f.local_threat) for f in frames),
        worst_safety_ratio=min(srs) if srs else None,
        max_enemies_unseen=max(eus) if eus else None,
        gold_band=gold_band_of(f0.local_gold_diff),
        died=any(f.died_within_s is not None for f in frames),
    )


def segment_episodes(exposures, gap_ms=EPISODE_GAP_MS) -> list:
    """Colapsa los frames-minuto expuestos CONSECUTIVOS de UN match (hueco entre
    frames <= gap_ms) en un episodio; un hueco mayor corta. Puro; se ordena por
    game_time_ms por si la entrada no venia ordenada."""
    if not exposures:
        return []
    frames = sorted(exposures, key=lambda e: e.game_time_ms)
    episodes, run = [], [frames[0]]
    for f in frames[1:]:
        if f.game_time_ms - run[-1].game_time_ms <= gap_ms:
            run.append(f)
        else:
            episodes.append(_episode_of(run))
            run = [f]
    episodes.append(_episode_of(run))
    return episodes


def aggregate(episodes, n_matches=None) -> dict:
    """Resumen report-only sobre EPISODIOS (la unidad correcta; ver
    segment_episodes). Mantiene las mismas claves de antes pero contando
    EPISODIOS, y anade el N crudo de frames + el tamano medio de cluster como
    recordatorio explicito de por que la FP se mide sobre episodios, no frames
    autocorrelados. `n_matches` = partidas escaneadas (para densidad)."""
    n = len(episodes)
    n_died = sum(1 for e in episodes if e.died)
    n_survived = n - n_died
    n_frames = sum(e.n_frames for e in episodes)
    return {
        "n": n,
        "n_died": n_died,
        "n_survived": n_survived,
        "pct_survived": round(100.0 * n_survived / n, 1) if n else 0.0,
        "per_match": round(n / n_matches, 2) if n_matches else 0.0,
        "by_phase": _count(phase_of(e.start_ms) for e in episodes),
        "by_champion": _count(e.champion or "?" for e in episodes),
        "by_gold_band": _count(e.gold_band for e in episodes),
        "n_frames": n_frames,
        "frames_per_episode": round(n_frames / n, 1) if n else 0.0,
    }


# --- Escaneo (unica capa con I/O) -------------------------------------------
def _own_champion(conn, match_id) -> Optional[str]:
    row = conn.execute(
        "SELECT p.champion_name FROM participants p JOIN matches m "
        "ON m.match_id = p.match_id AND p.puuid = m.our_puuid WHERE p.match_id = ?",
        (match_id,)).fetchone()
    return row["champion_name"] if row else None


def _own_death_times(conn, match_id, us_id) -> list:
    return [r["timestamp_ms"] for r in conn.execute(
        "SELECT timestamp_ms FROM timeline_events WHERE match_id = ? "
        "AND type = 'CHAMPION_KILL' AND json_extract(payload_json,'$.victimId') = ? "
        "ORDER BY timestamp_ms", (match_id, us_id))]


def scan_match(conn, match_id) -> list:
    """Todos los frames-minuto en que el jugador estaba EXPUESTO y VIVO.

    Unica funcion con I/O: reusa los mismos primitivos que death_v1 usa al morir
    (`build_world_state` + `build_state_features`), con wards/CS apagados porque
    el criterio no los mira y son lo caro del escaneo."""
    meta = get_match_meta(conn, match_id)
    us_id, our_team = meta["us_id"], meta["our_team"]
    champion = _own_champion(conn, match_id)
    deaths = _own_death_times(conn, match_id, us_id)

    out = []
    for row in conn.execute(
        "SELECT DISTINCT timestamp_ms FROM timeline_frames "
        "WHERE match_id = ? AND participant_id = ? ORDER BY timestamp_ms",
        (match_id, us_id),
    ):
        ts = row["timestamp_ms"]
        if ts < SKIP_BEFORE_MS:
            continue
        if player_dead_at(conn, match_id, us_id, ts):
            continue
        ws = build_world_state(conn, match_id, ts)
        sf = build_state_features(conn, match_id, ws, us_id, our_team,
                                  include_wards=False, include_cs=False)
        info, power = sf["info_risk"], sf["power"]
        if not is_exposed(info.get("safety_ratio"), info.get("enemies_unseen")):
            continue
        died = next((int((d - ts) / 1000) for d in deaths
                     if ts <= d <= ts + DEATH_WINDOW_MS), None)
        out.append(ExposureFacts(
            match_id=match_id,
            game_time_ms=ts,
            champion=champion,
            safety_ratio=info.get("safety_ratio"),
            enemies_unseen=info.get("enemies_unseen"),
            local_threat=info.get("local_threat"),
            local_gold_diff=power.get("local_gold_diff"),
            died_within_s=died,
        ))
    return out
