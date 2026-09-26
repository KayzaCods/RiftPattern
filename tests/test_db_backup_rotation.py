"""Caracterizacion de la rotacion de respaldos de `auto_ingest`.

Regla: solo rotan los respaldos DIARIOS (`lol_coach_YYYYMMDD.db`). Los puntos de
control manuales (`lol_coach_pre*.db`, `lol_coach.db.pre22-*.db`) son evidencia
previa a trabajo riesgoso y NO se tocan: antes entraban al glob
`lol_coach_*.db`, ordenan despues de las fechas y le comian la ventana a la
historia diaria.
"""
from __future__ import annotations

import importlib
import io
import sqlite3
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def auto(monkeypatch, tmp_path):
    """`auto_ingest` importado con DATA/LOG/STATUS en un temporal."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    # El import de los scripts solo define funciones; calla su reconfigure de stdout.
    with monkeypatch.context() as imports:
        imports.setattr(sys, "stdout", io.StringIO())
        module = importlib.import_module("auto_ingest")
    monkeypatch.setattr(module, "DATA", tmp_path)
    monkeypatch.setattr(module, "LOG", tmp_path / "ingest.log")
    monkeypatch.setattr(module, "STATUS", tmp_path / "ingest_status.json")
    return module


def _source_db(tmp_path: Path) -> Path:
    src = tmp_path / "source.db"
    with sqlite3.connect(src) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
    return src


def _touch(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_bytes(b"not-a-real-db")
    return path


def test_only_eight_digit_names_are_daily(auto, tmp_path):
    assert auto._is_daily_backup(tmp_path / "lol_coach_20260102.db")
    for name in ("lol_coach_pre-eje-prioridad.db",
                 "lol_coach_pre_trade_fix_2026-07-13.db",
                 "lol_coach_pre_trade_v1_key_shift_minus_60s_20260804_211447.db",
                 "lol_coach.db.pre22-20260826.db",
                 "lol_coach_2026-01-02.db",
                 "otro_20260102.db",
                 "lol_coach_20260102.bak"):
        assert not auto._is_daily_backup(tmp_path / name), name


def test_rotation_only_touches_daily_backups(auto, tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    days = ("20260102", "20260103", "20260104", "20260105",
            "20260106", "20260107", "20260108", "20260109")
    dailies = [_touch(backups, f"lol_coach_{day}.db") for day in days]
    manuals = [_touch(backups, name) for name in (
        "lol_coach_pre-eje-prioridad.db",
        "lol_coach_pre-engage-worth.db",
        "lol_coach_pre_trade_fix_2026-07-13.db",
        "lol_coach_pre_trade_v1_key_shift_minus_60s_20260804_211447.db",
        "lol_coach.db.pre22-20260826.db",
    )]
    cfg = {"paths": {"sqlite_db": str(_source_db(tmp_path))}}

    written = auto._daily_db_backup(cfg)

    assert written is not None and Path(written).exists()
    # Los manuales sobreviven, aunque ordenan despues de las fechas.
    assert all(path.exists() for path in manuals)
    # Rota la cola diaria: los dos mas viejos se van, el resto queda.
    assert not dailies[0].exists() and not dailies[1].exists()
    assert all(path.exists() for path in dailies[2:])
    live = [p for p in backups.glob("lol_coach_*.db") if auto._is_daily_backup(p)]
    assert len(live) == auto.BACKUP_KEEP  # los 6 diarios que quedan + el de hoy
    # La copia de hoy es una DB valida, no un archivo vacio.
    with sqlite3.connect(written) as conn:
        assert conn.execute("SELECT x FROM t").fetchone() == (1,)


def test_second_run_same_day_does_not_rotate_again(auto, tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    manual = _touch(backups, "lol_coach_pre-engage-worth.db")
    cfg = {"paths": {"sqlite_db": str(_source_db(tmp_path))}}

    assert auto._daily_db_backup(cfg) is not None
    after_first = sorted(p.name for p in backups.iterdir())

    assert auto._daily_db_backup(cfg) is None
    assert sorted(p.name for p in backups.iterdir()) == after_first
    assert manual.exists()
