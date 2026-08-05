"""Cola de feedback de trades: los trades PERDIDOS sin marcar, con sus hechos.

El detector de trades (trade_v1) es el más fiel al criterio del jugador y el más
huérfano de marcas (backlog #5: prioritario para el learner). Marcar decenas a
ciegas en el dashboard es lento; esta cola saca, por partida, cada trade perdido
sin marcar con lo necesario para juzgarlo (minuto, daño recibido tú vs duo enemigo,
estado al cierre, consecuencia) para decidir rápido y marcarlo en Decisiones
(filtro Trade). Re-corre el script para ver lo que queda.

Trades GANADOS quedan ocultos a propósito (solo Calibrar), así que no aparecen aquí.
Solo se miran cuentas propias (master/emerald); la referencia (challenger) se excluye.

Solo lee la DB. Escribe data/trade_feedback_queue.md.

    .venv\\Scripts\\python.exe scripts\\trade_feedback_queue.py
    .venv\\Scripts\\python.exe scripts\\trade_feedback_queue.py --db ruta\\a\\copia.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lol_coach import trade_queue as TQ
from lol_coach.config import load_config
from lol_coach.preference import DEFAULT_WEIGHTS, load_weights

WON = '%"positive_trade": true%'        # trades ganados: ocultos de Decisiones
OWN = ("master", "emerald")             # cuentas propias; excluye la referencia


def _date(ms) -> str:
    if not ms:
        return "?"
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def _unmarked_rows(conn: sqlite3.Connection) -> list:
    return conn.execute(
        f"""
        SELECT d.id, d.match_id, d.game_time_ms, d.context_json, m.our_cohort,
               m.game_start_ms,
               (SELECT p.champion_name FROM participants p
                 WHERE p.match_id = d.match_id AND p.puuid = m.our_puuid) AS champ
        FROM decisions d JOIN matches m ON m.match_id = d.match_id
        WHERE d.detector_id = 'trade_v1'
          AND m.our_cohort IN ({",".join("?" * len(OWN))})
          AND d.context_json NOT LIKE ?
          AND d.user_feedback IS NULL AND d.user_best_option IS NULL
        """,
        (*OWN, WON),
    ).fetchall()


def _marked_bucket_counts(conn: sqlite3.Connection) -> dict:
    """Buckets already covered by human marks on lost trades -- coverage seed so
    the ranking steers scarce new marks toward under-sampled state buckets."""
    rows = conn.execute(
        f"""
        SELECT d.id, d.game_time_ms, d.context_json,
               (SELECT p.champion_name FROM participants p
                 WHERE p.match_id = d.match_id AND p.puuid = m.our_puuid) AS champ
        FROM decisions d JOIN matches m ON m.match_id = d.match_id
        WHERE d.detector_id = 'trade_v1'
          AND m.our_cohort IN ({",".join("?" * len(OWN))})
          AND d.context_json NOT LIKE ?
          AND (d.user_feedback IS NOT NULL OR d.user_best_option IS NOT NULL)
        """,
        (*OWN, WON),
    ).fetchall()
    counts: dict = {}
    for r in rows:
        item = TQ.item_from_context(
            r["id"], r["game_time_ms"], json.loads(r["context_json"]), champion=r["champ"]
        )
        b = TQ.bucket_of(item)
        counts[b] = counts.get(b, 0) + 1
    return counts


def _why(item: dict) -> str:
    """Human-readable 'why is this near the top': Fisher + state bucket."""
    phase, gold, champion = item["bucket"]
    return f"Fisher {item['fisher']:.3f} · {phase}/{gold}/{champion}"


def build(conn: sqlite3.Connection, db_path: str) -> tuple[str, int, int]:
    """(markdown, n_trades, n_matches) de los trades perdidos sin marcar,
    ORDENADOS por informatividad para el learner (Fisher x cobertura). Los pesos
    activos (`hef_weights.json`) se leen junto a `db_path`."""
    rows = _unmarked_rows(conn)
    weights = load_weights(db_path) or DEFAULT_WEIGHTS

    items = []
    for r in rows:
        ctx = json.loads(r["context_json"])
        item = TQ.item_from_context(r["id"], r["game_time_ms"], ctx, champion=r["champ"])
        item.update(
            match_id=r["match_id"], cohort=r["our_cohort"],
            date=_date(r["game_start_ms"]),
            damage=(ctx.get("duo_damage_taken"), ctx.get("enemy_duo_damage_taken")),
            hp=ctx.get("our_hp_pct_end"), mana=ctx.get("our_mana_pct_end"),
            consequence=ctx.get("consequence") or "—",
            span=ctx.get("minute_span") or ctx.get("time_mmss") or "?",
        )
        items.append(item)

    ranked = TQ.rank(items, weights, _marked_bucket_counts(conn))
    n_matches = len({it["match_id"] for it in ranked})

    out = ["# Cola de feedback de trades (perdidos sin marcar, por informatividad)", ""]
    out.append(
        f"> {len(ranked)} trades perdidos sin marcar en {n_matches} partidas (cuentas "
        f"propias), ordenados de mayor a menor informatividad para el learner. "
        f"Márcalos en el dashboard: pestaña **Decisiones**, filtro **Trade** (busca el "
        f"`id`). Re-corre el script para ver lo que queda."
    )
    out.append("")
    out.append(
        "El criterio no es no tradear: es cambiar daño solo con ventana (maná/cooldowns/"
        "posición); si no, respetar y farmear. `daño` = lo que recibió tu duo vs el duo "
        "enemigo en la ventana (proxy de quién ganó el intercambio)."
    )
    out.append("")
    out.append(
        "**Orden = Fisher `p(1-p)` de la comparación (acción tomada vs su alternativa "
        "más cercana, bajo los pesos activos) × cobertura del bucket (fase × banda de "
        "oro × campeón). `por qué` declara ambos.** NOTA: en trade_v1 el Fisher es casi "
        "plano (la acción es siempre `fight_entry` y las dos opciones difieren solo en "
        "`action_fit` por un ~constante), así que el orden lo domina la COBERTURA: "
        "reparte las marcas escasas entre campeones/fases/bandas de oro en vez de "
        "apelotonarlas donde ya marcaste."
    )
    out.append("")
    out.append("| # | id | campeón · fecha | min | daño (tú vs enemigo) | estado | consecuencia | por qué está arriba |")
    out.append("|---|---|---|---|---|---|---|---|")
    for i, it in enumerate(ranked, 1):
        dus, den = it["damage"]
        state = " · ".join(
            x for x in (
                f"HP {int(it['hp'] * 100)}%" if it["hp"] is not None else None,
                f"maná {int(it['mana'] * 100)}%" if it["mana"] is not None else None,
            ) if x
        ) or "—"
        who = f"{it['champion'] or '?'} · {it['date']} ({it['cohort']})"
        out.append(
            f"| {i} | {it['id']} | {who} | {it['span']} | {dus} vs {den} | "
            f"{state} | {it['consequence']} | {_why(it)} |"
        )
    out.append("")

    return "\n".join(out), len(ranked), n_matches


def main() -> int:
    ap = argparse.ArgumentParser(description="Cola de feedback de trades sin marcar")
    ap.add_argument("--db", help="ruta a la DB (por defecto, la de config.toml)")
    args = ap.parse_args()

    db = args.db or load_config()["paths"]["sqlite_db"]
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    text, n, nmatches = build(conn, db)
    conn.close()

    out_path = Path(db).parent / "trade_feedback_queue.md"
    out_path.write_text(text, encoding="utf-8")
    print(f"{n} trades perdidos sin marcar en {nmatches} partidas.")
    print(f"Escrito: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
