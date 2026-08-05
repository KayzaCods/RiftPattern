"""Desplaza la clave estable de trade_v1 un minuto hacia atras (off-by-one del detector).

trade_v1 mide el intercambio como el delta de damageStats entre los frames m-1 y
m de Match-V5, o sea el intervalo de reloj [(m-1)*60s, m*60s]. El detector
sellaba la decision en `m*60_000` — el FINAL del intervalo, cuando el
intercambio ya habia terminado ("dice que es al min 14" para un trade de las
13:13). El fix la sella en `(m-1)*60_000`, el inicio del intervalo.

`persist_decisions` (lol_coach/analysis.py) preserva el feedback del usuario
emparejando por la clave estable (detector_id, game_time_ms) y su docstring es
explicito: si cambias CUANDO dispara un detector, o bumpeas el detector_id o
mandas una migracion de clave junto al cambio. Esto es esa migracion: mueve las
marcas existentes de trade_v1 de `t` a `t - 60_000` para que reaparezcan en la
clave que el detector arreglado va a emitir.

Lo que esta migracion NO puede salvar: el fix tambien toca el gate de muertes por
partida doble. (a) Se corrigio el indice: una muerte a las 7:30 ahora excluye el
intervalo [7, 8], antes excluia el [8, 9]. (b) Se ESTRECHO: excluye solo si murio
el JUGADOR, no si murio su companero de duo — death_v1 filtra por
`victimId != us_id`, asi que los intervalos donde cae el ADC y tu no no los
argumentaba nadie. El detector arreglado emite entonces un CONJUNTO distinto de
decisiones (mas grandes: tambien fusiona rachas que el gate ancho partia en dos).
Algunas marcas caen inevitablemente al re-analizar. El dry-run lo mide
re-corriendo el detector en memoria (solo lecturas) y reporta cuantas sobreviven
de verdad, no cuantas nos gustaria.

Dry-run por defecto; --apply escribe (con backup previo de la DB). Idempotente:
registra la migracion en `applied_migrations` y se niega a correr dos veces.

    .venv\\Scripts\\python.exe scripts\\migrate_trade_keys.py            # informe
    .venv\\Scripts\\python.exe scripts\\migrate_trade_keys.py --apply    # escribe
"""
from __future__ import annotations

import argparse
import collections
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lol_coach.config import load_config
from lol_coach.db import connect
from lol_coach.decisions.trade import analyze_trades

DETECTOR_ID = "trade_v1"
SHIFT_MS = -60_000
MIGRATION_ID = "trade_v1_key_shift_minus_60s"

MARK_COLS = (
    "user_feedback", "user_feedback_note", "user_feedback_at_utc",
    "user_best_option", "user_mark_type", "user_feedback_fingerprint",
)
_HAS_MARK = " OR ".join(f"{c} IS NOT NULL" for c in MARK_COLS)


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS applied_migrations ("
        "  migration_id TEXT PRIMARY KEY,"
        "  applied_at_utc TEXT NOT NULL,"
        "  note TEXT)"
    )
    conn.commit()


def _already_applied(conn: sqlite3.Connection) -> str | None:
    """Tolera que `applied_migrations` no exista todavia: el dry-run no la crea."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='applied_migrations'"
    ).fetchone()
    if not exists:
        return None
    row = conn.execute(
        "SELECT applied_at_utc FROM applied_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone()
    return row["applied_at_utc"] if row else None


def _backup_db(db_path: Path) -> Path:
    """Backup con la API online de sqlite (WAL-safe), como auto_ingest._daily_db_backup.

    Va a data/backups/ con un sufijo propio para no pisar ni rotar los backups
    diarios: esta copia es el punto de retorno de la migracion, no un diario.
    """
    bdir = db_path.parent / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    dest = bdir / f"{db_path.stem}_pre_{MIGRATION_ID}_{datetime.now():%Y%m%d_%H%M%S}.db"
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(dest))
        with dst:
            src.backup(dst)
        dst.close()
    finally:
        src.close()
    return dest


def _trade_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, match_id, game_time_ms, "
        f"({_HAS_MARK}) AS has_mark "
        "FROM decisions WHERE detector_id=? ORDER BY match_id, game_time_ms",
        (DETECTOR_ID,),
    ).fetchall()


def _collisions(rows) -> list[tuple[str, int]]:
    """Claves (match_id, game_time_ms) que quedarian duplicadas tras el desplazamiento.

    El desplazamiento es uniforme y por tanto inyectivo dentro de trade_v1, asi
    que en teoria no puede colisionar; se comprueba igual porque `decisions` no
    tiene UNIQUE sobre la clave estable y una colision silenciosa haria que
    persist_decisions se quedara con una sola de las dos marcas.
    """
    after = collections.Counter(
        (r["match_id"], r["game_time_ms"] + SHIFT_MS) for r in rows)
    return sorted(k for k, n in after.items() if n > 1)


def _survivors(conn: sqlite3.Connection, marked_rows) -> tuple[int, int, int]:
    """(sobreviven, se_pierden, sin_timeline) re-corriendo el detector arreglado.

    Solo lecturas: analyze_trades no escribe. Responde la pregunta real —
    cuantas marcas vuelven a encontrar una decision en su clave desplazada— en
    vez de la pregunta facil (cuantas se pueden mover).
    """
    by_match = collections.defaultdict(list)
    for r in marked_rows:
        by_match[r["match_id"]].append(r["game_time_ms"] + SHIFT_MS)
    ok = lost = no_timeline = 0
    for match_id, targets in sorted(by_match.items()):
        try:
            emitted = {d.game_time_ms for d in analyze_trades(conn, match_id)}
        except Exception as exc:                      # timeline ilegible / borrado
            print(f"  aviso: no se pudo re-analizar {match_id}: {exc}")
            no_timeline += len(targets)
            continue
        if not emitted:
            # Sin timeline JSON en disco el detector devuelve [] sin distinguirse
            # de "no hay trades": no se puede afirmar que la marca se pierda.
            path = conn.execute(
                "SELECT timeline_json_path p FROM matches WHERE match_id=?",
                (match_id,)).fetchone()
            if not path or not path["p"] or not Path(path["p"]).exists():
                no_timeline += len(targets)
                continue
        for t in targets:
            if t in emitted:
                ok += 1
            else:
                lost += 1
    return ok, lost, no_timeline


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Migracion de clave de trade_v1: game_time_ms -> game_time_ms - 60000.")
    ap.add_argument("--apply", action="store_true",
                    help="escribe en la DB (por defecto: solo informe)")
    ap.add_argument("--db", default=None,
                    help="ruta de la DB (por defecto: config.toml)")
    args = ap.parse_args()

    cfg = load_config()
    db_path = Path(args.db) if args.db else Path(cfg["paths"]["sqlite_db"])
    conn = connect(db_path)

    applied_at = _already_applied(conn)
    if applied_at:
        print(f"La migracion '{MIGRATION_ID}' ya se aplico el {applied_at[:19]}.")
        print("No se vuelve a correr: un segundo desplazamiento moveria las claves "
              "otro minuto y ahi si se perderia el feedback.")
        return 1

    rows = _trade_rows(conn)
    marked = [r for r in rows if r["has_mark"]]
    print(f"DB: {db_path}")
    print(f"Decisiones de {DETECTOR_ID}: {len(rows)}  ·  con marca del usuario: {len(marked)}")
    if not rows:
        print("Nada que migrar.")
        return 0

    minimum = min(r["game_time_ms"] for r in rows)
    print(f"game_time_ms minimo actual: {minimum} -> {minimum + SHIFT_MS} tras el desplazamiento")
    if minimum + SHIFT_MS < 0:
        print("ABORTA: el desplazamiento dejaria claves negativas.")
        return 1

    cols = _collisions(rows)
    if cols:
        print(f"\nCOLISIONES DE CLAVE ({len(cols)}): tras mover -60s estas claves quedan duplicadas")
        for match_id, t in cols[:10]:
            print(f"  {match_id} @ {t} ms")
        print("ABORTA: persist_decisions se quedaria con una sola marca por clave duplicada.")
        return 1
    print("Colisiones de clave tras el desplazamiento: ninguna.")

    print("\nRe-analizando en memoria (solo lecturas) para medir supervivencia real...")
    ok, lost, unknown = _survivors(conn, marked)
    print(f"  marcas que el detector arreglado vuelve a emitir en su clave nueva: {ok}")
    print(f"  marcas que se pierden igual (gate de muertes corregido/estrechado): {lost}")
    if unknown:
        print(f"  indeterminadas (sin timeline JSON en disco):                       {unknown}")
    print("  Nota: las que se pierden NO las salva ninguna migracion de clave — el "
          "detector arreglado simplemente ya no considera esos intercambios.")

    if not args.apply:
        print(f"\nDRY-RUN: no se escribio nada. Se moverian {len(rows)} decisiones "
              f"({len(marked)} con marca). Corre con --apply para ejecutar.")
        return 0

    backup = _backup_db(db_path)
    print(f"\nBackup previo: {backup}")

    _ensure_migrations_table(conn)
    conn.execute(
        "UPDATE decisions SET game_time_ms = game_time_ms + ? WHERE detector_id = ?",
        (SHIFT_MS, DETECTOR_ID),
    )
    conn.execute(
        "INSERT INTO applied_migrations (migration_id, applied_at_utc, note) VALUES (?,?,?)",
        (MIGRATION_ID, datetime.now(timezone.utc).isoformat(),
         f"{len(rows)} decisiones movidas, {len(marked)} con marca; backup {backup.name}"),
    )
    conn.commit()

    still = conn.execute(
        f"SELECT COUNT(*) n FROM decisions WHERE detector_id=? AND ({_HAS_MARK})",
        (DETECTOR_ID,)).fetchone()["n"]
    print(f"Movidas {len(rows)} decisiones de {DETECTOR_ID} un minuto hacia atras.")
    print(f"Marcas preservadas en la DB: {still} (eran {len(marked)}).")
    print("Siguiente paso, cuando tu quieras: re-analizar los partidos "
          "(scripts/analyze.py) para que el detector arreglado reemita las decisiones.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
