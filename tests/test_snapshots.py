"""Tests del localizador de momento de trade via HP del recorder (snapshots.py).

El detector de trades solo sabe el MINUTO del intercambio (Riot da daño por
minuto); el recorder captura el HP propio a 1Hz. La mayor caida de HP dentro de
la ventana localiza el segundo del intercambio para que el clip/replay abra ahi
en vez de en el inicio del minuto (feedback 2026-07-18: buscar el frame a mano
frenaba el marcado).
"""
from __future__ import annotations

import json

from lol_coach import snapshots as S


def _snap(hp, max_hp=1000.0):
    return {"activePlayer": {"championStats": {"currentHealth": hp, "maxHealth": max_hp}}}


def test_hp_pct_series_filters_window_and_normalizes():
    snaps = [
        (539.0, _snap(800)),   # fuera (antes)
        (540.0, _snap(1000)),
        (570.0, _snap(500)),
        (600.0, _snap(900)),
        (601.0, _snap(100)),   # fuera (despues)
    ]
    serie = S.hp_pct_series(snaps, 540.0, 600.0)
    assert serie == [(540.0, 1.0), (570.0, 0.5), (600.0, 0.9)]


def test_hp_pct_series_skips_snapshots_without_hp():
    snaps = [
        (540.0, {"activePlayer": {}}),          # sin championStats
        (541.0, _snap(None)),                    # currentHealth None
        (542.0, _snap(700)),
    ]
    assert S.hp_pct_series(snaps, 540.0, 600.0) == [(542.0, 0.7)]


def test_biggest_drop_returns_the_peak_time_before_the_trough():
    # HP: lleno, empieza a caer en t=550 (pico), valle en t=565, se recupera.
    serie = [(540.0, 0.9), (550.0, 1.0), (555.0, 0.8), (565.0, 0.55), (580.0, 0.7)]
    # caida max = 1.0 - 0.55 = 0.45, arrancando en el pico t=550
    assert S.biggest_drop_start_s(serie) == 550.0


def test_biggest_drop_below_threshold_is_none():
    # Ruido de regen/poke chico: caida de 5% con umbral de 8% -> no hay momento.
    serie = [(540.0, 1.0), (560.0, 0.95), (580.0, 1.0)]
    assert S.biggest_drop_start_s(serie) is None


def test_biggest_drop_of_flat_or_empty_series_is_none():
    assert S.biggest_drop_start_s([]) is None
    assert S.biggest_drop_start_s([(540.0, 0.8)]) is None
    assert S.biggest_drop_start_s([(540.0, 0.8), (560.0, 0.8)]) is None


def test_biggest_drop_dead_then_respawn_is_not_a_drop():
    # Muerto (0.0) al inicio y respawn a 1.0: subir no es caida; sin caida
    # posterior >= umbral no hay momento (caso real Sona id=24689).
    serie = [(540.0, 0.0), (550.0, 0.0), (555.0, 1.0), (595.0, 0.96)]
    assert S.biggest_drop_start_s(serie) is None


# --------------------------------------------- lector por ventana con corte temprano
def _write_snaps(path, rows):
    """rows: [(game_time_s, hp, max_hp)]; escribe el formato real del recorder."""
    with open(path, "w", encoding="utf-8") as f:
        for gt, hp, mx in rows:
            f.write(json.dumps({"data": {
                "gameData": {"gameTime": gt},
                "activePlayer": {"championStats": {"currentHealth": hp, "maxHealth": mx}},
            }}) + "\n")


def test_windowed_reader_keeps_only_the_window(tmp_path):
    _write_snaps(tmp_path / "snapshots.jsonl", [
        (539.0, 800, 1000),   # antes
        (540.0, 1000, 1000),
        (570.0, 500, 1000),
        (600.0, 900, 1000),
        (601.0, 100, 1000),   # despues
    ])
    assert S.hp_pct_series_windowed(tmp_path, 540.0, 600.0) == [
        (540.0, 1.0), (570.0, 0.5), (600.0, 0.9)]


def test_windowed_reader_stops_at_end_of_window(tmp_path):
    # Corte temprano: una linea CORRUPTA despues de t1 no se lee (ya rompimos).
    # Si se leyera, json.loads reventaria -> la prueba lo delata.
    path = tmp_path / "snapshots.jsonl"
    _write_snaps(path, [(540.0, 1000, 1000), (570.0, 600, 1000)])
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"data": {"gameData": {"gameTime": 620.0}}}\n')  # fuera de ventana
        f.write("{basura no-json que reventaria si se leyera\n")
    assert S.hp_pct_series_windowed(path.parent, 540.0, 600.0) == [
        (540.0, 1.0), (570.0, 0.6)]


def test_windowed_reader_tolerates_half_written_line_in_window(tmp_path):
    # Grabacion en curso: la ultima linea puede estar a medio escribir. Se salta
    # sin romper el resto de la ventana.
    path = tmp_path / "snapshots.jsonl"
    _write_snaps(path, [(540.0, 1000, 1000)])
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"data": {"gameData": {"gameTi')   # linea truncada, sin newline
    assert S.hp_pct_series_windowed(path.parent, 540.0, 600.0) == [(540.0, 1.0)]


def test_windowed_reader_missing_file_is_empty(tmp_path):
    assert S.hp_pct_series_windowed(tmp_path, 0.0, 100.0) == []
