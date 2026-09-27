"""Re-analiza UN detector en el sitio (#23 fases C y D). Ver lol_coach/inplace_reanalysis.py.

Por que no scripts/analyze.py: re-ejecuta TODOS los detectores con borrar +
reinsertar, lo que aplicaria el cambio de PRE de trade (no autorizado),
cambiaria todos los ids y refrescaria created_at.

Uso:
    python scripts/reanalyze_inplace.py death_v1                 # dry-run (read-only)
    python scripts/reanalyze_inplace.py death_v1 --apply         # backup + escritura
    python scripts/reanalyze_inplace.py death_v1 --db COPIA.db --apply --no-backup

`--apply` sin `--no-backup` copia antes la DB entera a data/backups/ con la API de
backup de SQLite. No toca analyzed_at_utc, ni marcas, ni clips, ni ids.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lol_coach import inplace_reanalysis as R  # noqa: E402


def _default_db() -> str:
    from lol_coach.config import load_config
    return load_config()["paths"]["sqlite_db"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("detector", choices=sorted(R.DETECTORS))
    ap.add_argument("--db", help="ruta de la DB (por defecto, la de config.toml)")
    ap.add_argument("--apply", action="store_true", help="escribir (por defecto: dry-run)")
    ap.add_argument("--no-backup", action="store_true",
                    help="no copiar antes (solo para ensayos sobre una copia)")
    ap.add_argument("--json", action="store_true", help="resumen en JSON")
    args = ap.parse_args(argv)
    db = args.db or _default_db()
    if not Path(db).exists():
        print(f"No existe la DB: {db}", file=sys.stderr)
        return 2

    if not args.apply:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            plan = R.build_plan(conn, args.detector)
        finally:
            conn.close()
        out = {"mode": "dry-run", **plan.summary(),
               "label_shift_examples": plan.label_shift_valid[:10]}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1 if plan.mismatches else 0

    backup_path = None
    if not args.no_backup:
        bdir = Path(db).parent / "backups"
        bdir.mkdir(exist_ok=True)
        backup_path = str(bdir / f"lol_coach_pre-inplace-{args.detector}-{R.now_tag()}.db")
        R.backup(db, backup_path)
        # A stderr: stdout es solo el JSON del resumen (lo leen otros scripts).
        print(f"backup: {backup_path}", file=sys.stderr)
    conn = sqlite3.connect(db, timeout=60, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        plan = R.apply_plan(conn, args.detector)
        again = R.build_plan(conn, args.detector)
    except R.KeyMismatch as e:
        print(f"ABORTADO sin escribir: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    out = {"mode": "apply", "backup": backup_path, **plan.summary(),
           "label_shift_examples": plan.label_shift_valid[:10],
           "idempotence_rows_still_changing": len(again.changed)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if not again.changed else 3


if __name__ == "__main__":
    raise SystemExit(main())
