"""Anchor set para validar exposure_v1 (#17): las muertes que TU marcaste en
desacuerdo, para juzgar a ciegas si el error real fue exposicion previa.

exposure_v1 detecta estados expuestos, pero nadie ha medido si detecta los
CORRECTOS. El jugador ya escribio decenas de veces "el error fue la posicion/
vision ~30s antes, no el retiro" al discrepar de death_v1: esas notas son verdad
de campo que ya existe. Este script las saca a un .md para marcarlas.

DELIBERADAMENTE A CIEGAS: el .md NO dice si exposure_v1 disparo en esa muerte.
Mostrarlo anclaria el juicio en la respuesta del sistema y la validacion no
valdria nada (el sesgo de confirmacion es el modo de fallo clasico de validar
un detector contra su propio autor).

Solo lee la DB. Escribe data/exposure_anchor_set.md; si el archivo ya existe
CONSERVA las marcas hechas y solo anade los casos nuevos.

    .venv\\Scripts\\python.exe scripts\\exposure_anchor_set.py
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lol_coach.config import load_config

OWN = ("master", "emerald")          # cuentas propias; excluye la referencia
MARK_RE = re.compile(r"^- \[([ x?X])\] \*\*id=(\d+)\*\*", re.M)


def _date(ms) -> str:
    if not ms:
        return "?"
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def _mmss(ms) -> str:
    s = int((ms or 0) / 1000)
    return f"{s // 60}:{s % 60:02d}"


def existing_marks(path: Path) -> dict:
    """{decision_id: 'x'|'?'|' '} de un .md ya marcado, para no perder trabajo."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    return {int(mid): mark.lower() for mark, mid in MARK_RE.findall(text)}


def build(conn: sqlite3.Connection, marks: dict) -> tuple[str, int, int]:
    rows = conn.execute(
        f"""
        SELECT d.id, d.game_time_ms, d.moment, d.user_feedback_note,
               m.game_start_ms, m.our_cohort,
               (SELECT p.champion_name FROM participants p
                 WHERE p.match_id = d.match_id AND p.puuid = m.our_puuid) AS champ
        FROM decisions d JOIN matches m ON m.match_id = d.match_id
        WHERE d.detector_id = 'death_v1'
          AND d.user_feedback = 'disagree'
          AND d.user_feedback_note IS NOT NULL AND TRIM(d.user_feedback_note) <> ''
          AND m.our_cohort IN ({",".join("?" * len(OWN))})
        ORDER BY m.game_start_ms DESC, d.game_time_ms ASC
        """,
        OWN,
    ).fetchall()

    out = ["# Anchor set — ¿el error real fue EXPOSICIÓN previa?", ""]
    out.append(
        "> Clave de respuestas para validar `exposure_v1` (#17). Son TUS muertes marcadas "
        "en desacuerdo, con TU nota. Va **a ciegas**: no dice si el detector disparó ahí — "
        "si lo dijera, tu juicio se anclaría en la respuesta del sistema y la validación "
        "no valdría nada."
    )
    out.append("")
    out.append("## Cómo marcar")
    out.append("")
    out.append(
        "Lee tu nota y pregúntate: **¿el error real fue estar expuesto (posición/visión) "
        "ANTES de morir?**"
    )
    out.append("")
    out.append("- `[x]` — **sí**: la nota dice que el problema fue la posición, la visión, "
               "adelantarse, entrar sin ver, quedarse expuesto.")
    out.append("- `[ ]` — **no**: fue otra cosa (mecánica/ejecución, el equipo no siguió, "
               "mala suerte, forzado, no había alternativa).")
    out.append("- `[?]` — **dudoso**: no logras decidir. Se excluyen del cálculo, no pasa nada.")
    out.append("")
    out.append(
        "Guarda el archivo y avísame. Re-correr el script **conserva** lo que ya marcaste."
    )
    out.append("")
    n_marked = sum(1 for m in marks.values() if m in ("x", "?"))
    out.append(f"**{len(rows)} casos · {n_marked} ya marcados**")
    out.append("")
    out.append("---")
    out.append("")

    for r in rows:
        mark = marks.get(r["id"], " ")
        note = " ".join((r["user_feedback_note"] or "").split())
        out.append(
            f"- [{mark}] **id={r['id']}** · {r['champ'] or '?'} · {_date(r['game_start_ms'])} "
            f"· {_mmss(r['game_time_ms'])} — {r['moment'] or ''}"
        )
        out.append(f"  > {note}")
        out.append("")

    return "\n".join(out), len(rows), n_marked


def main() -> int:
    ap = argparse.ArgumentParser(description="Anchor set de exposicion (muertes en desacuerdo)")
    ap.add_argument("--db", help="ruta a la DB (por defecto, la de config.toml)")
    args = ap.parse_args()

    db = args.db or load_config()["paths"]["sqlite_db"]
    out_path = Path(db).parent / "exposure_anchor_set.md"
    marks = existing_marks(out_path)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    text, n, n_marked = build(conn, marks)
    conn.close()

    out_path.write_text(text, encoding="utf-8")
    print(f"{n} casos ({n_marked} ya marcados).")
    print(f"Escrito: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
