"""Reader for the recorder's per-second Live Client snapshots.

recorder.py writes snapshots.jsonl (one JSON object per line, one snapshot per
second). Detectors that want the player's exact HP/mana/items at a moment read
it back through here — a single loader instead of a near-identical copy in each
detector (trade and objective_readiness had diverged only in name).
"""
from __future__ import annotations

import json
from pathlib import Path


def load_snapshots(session_dir: Path) -> list[tuple[float, dict]]:
    """[(game_time_s, snapshot_data)] from session_dir/snapshots.jsonl, sorted by
    game time. Empty list if the file is absent — the recorder is optional, and
    damage/timeline deltas drive the detectors without it."""
    path = session_dir / "snapshots.jsonl"
    if not path.exists():
        return []
    out: list[tuple[float, dict]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            data = obj.get("data") or {}
            gt = (data.get("gameData") or {}).get("gameTime")
            if isinstance(gt, (int, float)):
                out.append((float(gt), data))
    out.sort(key=lambda t: t[0])
    return out


# ------------------------------------------- localizador de momento por caida de HP
# El detector de trades solo conoce el MINUTO (Riot da danio por minuto); el HP
# propio a 1Hz localiza el SEGUNDO del intercambio para abrir el clip ahi.
# Umbral: por debajo de esto es regen/poke chico, no un intercambio — mejor
# caer al inicio del minuto (comportamiento previo) que inventar un momento.
MIN_TRADE_DROP = 0.08


def _hp_pct_of(data: dict):
    cs = (data.get("activePlayer") or {}).get("championStats") or {}
    hp, mx = cs.get("currentHealth"), cs.get("maxHealth")
    return (hp / mx) if (hp is not None and mx) else None


def hp_pct_series(snapshots: list[tuple[float, dict]], t0_s: float, t1_s: float) -> list[tuple[float, float]]:
    """[(game_time_s, hp_pct)] del jugador dentro de [t0_s, t1_s], desde los
    snapshots de load_snapshots. Salta snapshots sin championStats/HP."""
    out: list[tuple[float, float]] = []
    for gt, data in snapshots:
        if not t0_s <= gt <= t1_s:
            continue
        pct = _hp_pct_of(data)
        if pct is not None:
            out.append((gt, pct))
    return out


def hp_pct_series_windowed(session_dir: Path, t0_s: float, t1_s: float) -> list[tuple[float, float]]:
    """[(game_time_s, hp_pct)] dentro de [t0_s, t1_s] leyendo snapshots.jsonl por
    STREAMING con corte temprano: el recorder escribe 1 snapshot/seg en orden
    temporal, asi que en cuanto una linea supera t1_s dejamos de leer. Evita
    cargar el archivo entero (los .jsonl del Live Client pesan 50-100 MB) cuando
    solo se quiere una ventana de ~1-2 min. Vacio si no hay archivo/sesion."""
    path = session_dir / "snapshots.jsonl"
    if not path.exists():
        return []
    out: list[tuple[float, float]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                data = (json.loads(line).get("data")) or {}
            except json.JSONDecodeError:
                continue                       # linea a medio escribir (grabacion en curso)
            gt = (data.get("gameData") or {}).get("gameTime")
            if not isinstance(gt, (int, float)):
                continue
            if gt > t1_s:
                break                          # orden temporal: ya pasamos la ventana
            if gt >= t0_s:
                pct = _hp_pct_of(data)
                if pct is not None:
                    out.append((float(gt), pct))
    return out


def biggest_drop_start_s(series: list[tuple[float, float]], min_drop: float = MIN_TRADE_DROP):
    """game_time_s donde ARRANCA la mayor caida de HP de la serie (el pico antes
    del valle — ahi empieza el intercambio), o None si ninguna caida alcanza
    min_drop. Subidas (respawn, curacion) nunca cuentan como caida."""
    best = 0.0
    best_start = None
    peak_t = peak_v = None
    for gt, hp in series:
        if peak_v is None or hp > peak_v:
            peak_t, peak_v = gt, hp
        drop = peak_v - hp
        if drop > best:
            best, best_start = drop, peak_t
    return best_start if best >= min_drop else None
