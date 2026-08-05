"""Lector del archivo de feedback huerfano (#19).

`persist_decisions` (lol_coach/analysis.py) borra y reinserta las decisiones de
una partida al re-analizarla, y solo reasocia el feedback cuya clave estable
(detector_id, game_time_ms) vuelve a emitirse. Cuando un detector cambia lo que
dispara — o cuando disparaba — las marcas que ya no encuentran destino se vuelcan
a la tabla `orphan_feedback` en vez de desaparecer. Esas notas son trabajo manual
de semanas: son la evidencia con la que se diagnostican los detectores.

Este script las devuelve a un formato legible. Agrupa por detector y por fecha de
la marca, y para cada una imprime la nota INTEGRA, el momento de la jugada, el
argumento que se mostro entonces y las opciones contra las que se marco (con la
que el usuario eligio senalada) — sin las opciones, `user_best_option` es un
indice sin significado.

Solo lecturas. Escribe data/orphan_feedback.md junto a la DB.

    .venv\\Scripts\\python.exe scripts\\orphan_feedback.py
    .venv\\Scripts\\python.exe scripts\\orphan_feedback.py --json      # crudo, para reprocesar
    .venv\\Scripts\\python.exe scripts\\orphan_feedback.py --db ruta\\a\\copia.db
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lol_coach.config import load_config

COLS = (
    "id", "match_id", "detector_id", "game_time_ms", "user_feedback",
    "user_feedback_note", "user_feedback_at_utc", "user_feedback_fingerprint",
    "user_mark_type", "user_best_option", "options_json", "moment", "argument",
    "dropped_at_utc", "reason",
)


def load_rows(conn: sqlite3.Connection) -> list[dict]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orphan_feedback'"
    ).fetchone()
    if not exists:
        return []
    return [
        dict(r) for r in conn.execute(
            f"SELECT {', '.join(COLS)} FROM orphan_feedback "
            "ORDER BY detector_id, user_feedback_at_utc, match_id, game_time_ms"
        )
    ]


def _mmss(ms: int | None) -> str:
    if not isinstance(ms, int):
        return "?"
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


def _day(row: dict) -> str:
    stamp = row.get("user_feedback_at_utc") or row.get("dropped_at_utc") or ""
    return stamp[:10] or "sin fecha"


def _options(row: dict) -> list[dict]:
    try:
        opts = json.loads(row.get("options_json") or "[]")
    except (ValueError, TypeError):
        return []
    return opts if isinstance(opts, list) else []


def render_md(rows: list[dict]) -> str:
    lines: list[str] = []
    w = lines.append
    w("# Feedback huerfano archivado")
    w("")
    if not rows:
        w("No hay filas en `orphan_feedback`: ningun re-analisis ha dejado marcas "
          "sin destino desde que existe el archivo.")
        w("")
        return "\n".join(lines)

    by_detector = collections.Counter(r["detector_id"] for r in rows)
    w(f"**{len(rows)} marcas** rescatadas de re-analisis que dejaron su clave "
      f"estable sin reemitir. Estas notas ya NO estan en `decisions`: esta tabla "
      f"es su unica copia.")
    w("")
    w("| detector | marcas |")
    w("|---|---|")
    for det, n in by_detector.most_common():
        w(f"| `{det}` | {n} |")
    w("")
    w("Motivos: " + ", ".join(
        f"`{k}` ({n})" for k, n in
        collections.Counter(r["reason"] for r in rows).most_common()))
    w("")

    grouped: dict[str, dict[str, list[dict]]] = collections.defaultdict(
        lambda: collections.defaultdict(list))
    for r in rows:
        grouped[r["detector_id"]][_day(r)].append(r)

    for detector in sorted(grouped):
        w(f"## {detector}")
        w("")
        for day in sorted(grouped[detector]):
            day_rows = grouped[detector][day]
            w(f"### Marcadas el {day} ({len(day_rows)})")
            w("")
            for r in day_rows:
                w(f"#### {r['match_id']} @ {_mmss(r['game_time_ms'])} "
                  f"(`{r['game_time_ms']}` ms)")
                w("")
                meta = [f"marca: **{r['user_feedback'] or '—'}**"]
                if r["user_mark_type"]:
                    meta.append(f"tipo: `{r['user_mark_type']}`")
                if r["user_feedback_at_utc"]:
                    meta.append(f"fecha: {r['user_feedback_at_utc'][:19]}")
                meta.append(f"archivada: {(r['dropped_at_utc'] or '')[:19]}")
                w("- " + " · ".join(meta))
                w(f"- Momento: {r['moment'] or '—'}")
                w("")
                note = (r["user_feedback_note"] or "").strip()
                if note:
                    w("**Nota:**")
                    w("")
                    for para in note.split("\n"):
                        w(f"> {para}" if para.strip() else ">")
                    w("")
                opts = _options(r)
                if opts:
                    w("**Opciones que se marcaron:**")
                    w("")
                    for i, o in enumerate(opts):
                        chosen = " **← tu mejor opcion**" if i == r["user_best_option"] else ""
                        ev = o.get("ev_score")
                        ev_s = f" (EV {ev:.2f})" if isinstance(ev, (int, float)) else ""
                        w(f"{i}. {o.get('label', '—')}{ev_s}{chosen}")
                        cons = (o.get("predicted_consequence") or "").strip()
                        if cons:
                            w(f"   - {cons}")
                    w("")
                elif r["user_best_option"] is not None:
                    w(f"**Opcion elegida:** indice {r['user_best_option']} "
                      f"(opciones no archivadas)")
                    w("")
                arg = (r["argument"] or "").strip()
                if arg:
                    w("<details><summary>Argumento que se te mostro</summary>")
                    w("")
                    w(arg)
                    w("")
                    w("</details>")
                    w("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Vuelca el feedback huerfano archivado a un .md legible.")
    ap.add_argument("--db", default=None, help="ruta de la DB (por defecto: config.toml)")
    ap.add_argument("--json", action="store_true",
                    help="imprime las filas en crudo (JSON) por stdout en vez del informe")
    args = ap.parse_args()

    cfg = load_config()
    db_path = Path(args.db) if args.db else Path(cfg["paths"]["sqlite_db"])
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = load_rows(conn)
    conn.close()

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    out = render_md(rows) + "\n"
    md_path = db_path.parent / "orphan_feedback.md"
    md_path.write_text(out, encoding="utf-8")

    if not rows:
        print("orphan_feedback esta vacia: ningun re-analisis ha perdido marcas.")
    else:
        counts = collections.Counter(r["detector_id"] for r in rows)
        print(f"Marcas huerfanas archivadas: {len(rows)}")
        for det, n in counts.most_common():
            print(f"  {det}: {n}")
    print(f"\nInforme: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
