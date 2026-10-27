"""Patrones de derrota desde datos Riot ya ingeridos, sin escribir en SQLite.

Fuente de hipotesis con n declarado; no es detector ni solicita feedback.
La I/O vive aqui; criterio y estadistica en lol_coach.loss_patterns.

    .venv\\Scripts\\python.exe scripts\\loss_patterns.py
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lol_coach import loss_patterns as LP
from lol_coach.config import load_config

OWN = ("master", "emerald")
LIMIT = "Limites: correlacion, no causa; fuente de hipotesis, no veredicto."


def load_games(conn, cohorts=OWN) -> list[LP.GameView]:
    """Lee partidas completas de las cohortes indicadas, sin cambiar conexion.

    Los indices de frame son los minutos de la timeline. Datos faltantes o
    ambiguos abortan con match_id: no se inventan oro, niveles ni participante.
    Una posicion ausente queda como None, nunca como la esquina del mapa.
    """
    if not cohorts:
        return []
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in cohorts)
    matches = cursor.execute(
        "SELECT match_id, our_puuid, game_duration_s FROM matches "
        f"WHERE queue_id=420 AND game_duration_s>=300 AND our_cohort IN ({placeholders}) "
        "ORDER BY match_id", tuple(cohorts),
    ).fetchall()
    games = []
    for match in matches:
        match_id = match["match_id"]
        participants = cursor.execute(
            "SELECT participant_id, puuid, team_id, win FROM participants "
            "WHERE match_id=? ORDER BY participant_id", (match_id,),
        ).fetchall()
        ours = [p for p in participants if p["puuid"] == match["our_puuid"]]
        if len(ours) != 1:
            raise ValueError(f"{match_id}: participante propio ausente o ambiguo")
        team_of = {p["participant_id"]: p["team_id"] for p in participants}
        if set(team_of.values()) != {100, 200}:
            raise ValueError(f"{match_id}: equipos incompletos")
        frames = cursor.execute(
            "SELECT frame_index, participant_id, total_gold, level FROM timeline_frames "
            "WHERE match_id=? ORDER BY frame_index, participant_id", (match_id,),
        ).fetchall()
        gold, levels = {}, {}
        for frame in frames:
            minute, pid = frame["frame_index"], frame["participant_id"]
            if frame["total_gold"] is None or frame["level"] is None:
                raise ValueError(f"{match_id}: oro o nivel faltante en frame {minute}")
            gold.setdefault(minute, {})[pid] = frame["total_gold"]
            levels.setdefault(minute, {})[pid] = frame["level"]
        if not gold or sorted(gold) != list(range(max(gold) + 1)):
            raise ValueError(f"{match_id}: frames ausentes o indices no contiguos")
        if any(set(frame) != set(team_of) for frame in gold.values()):
            raise ValueError(f"{match_id}: participantes faltantes en frames")
        kills, buildings, elites = [], [], []
        events = cursor.execute(
            "SELECT timestamp_ms, type, payload_json FROM timeline_events "
            "WHERE match_id=? AND type IN "
            "('CHAMPION_KILL','BUILDING_KILL','ELITE_MONSTER_KILL') "
            "ORDER BY timestamp_ms, id", (match_id,),
        ).fetchall()
        for row in events:
            event = json.loads(row["payload_json"])
            t_ms = row["timestamp_ms"]
            position = event.get("position") or {}
            if row["type"] == "CHAMPION_KILL":
                kills.append(LP.KillEvent(
                    t_ms, event["victimId"], event.get("killerId", 0),
                    tuple(event.get("assistingParticipantIds") or ()),
                    position.get("x"), position.get("y"),
                ))
            elif row["type"] == "BUILDING_KILL":
                # teamId es el dueno de la estructura, no quien la destruyo.
                buildings.append(LP.BuildingEvent(t_ms, event["teamId"], event["buildingType"]))
            else:
                killer_team = event.get("killerTeamId") or team_of.get(event.get("killerId"), 0)
                elites.append(LP.EliteEvent(
                    t_ms, killer_team, event["monsterType"],
                    position.get("x"), position.get("y"),
                ))
        games.append(LP.GameView(
            match_id, bool(ours[0]["win"]), match["game_duration_s"],
            ours[0]["participant_id"], team_of,
            [gold[m] for m in range(len(gold))],
            [levels[m] for m in range(len(levels))], kills, buildings, elites,
        ))
    cursor.close()
    return games


def _percent(value):
    return "sin datos" if value is None else f"{100 * value:.1f}%"


def _number(value):
    return "sin datos" if value is None else f"{value:.3f}"


def _bucket_table(title, games, key, buckets):
    rows = LP.win_rate_by_bucket(games, key, buckets)
    lines = [f"## {title}", "", f"n = {len(games)} partidas. IC Wilson 95%.", "",
             "| Grupo | Victorias | n | Victoria | IC 95% |",
             "|---|---:|---:|---:|---:|"]
    for row in rows:
        lines.append(f"| {row['label']} | {row['wins']} | {row['n']} | "
                     f"{_percent(row['rate'])} | {_percent(row['lo'])} a {_percent(row['hi'])} |")
    return lines + ["", LIMIT, ""]


def _chained(game, kill):
    """La segunda muerte de cada par cercano, solo antes del minuto 14."""
    deaths = LP.our_deaths(game)
    return any(current is kill and current.t_ms < LP.EARLY_CUTOFF_MS
               and current.t_ms - previous.t_ms <= LP.CHAIN_GAP_MS
               for previous, current in zip(deaths, deaths[1:]))


def _own_half(game, kill):
    if kill.x is None or kill.y is None:
        return False
    diagonal = kill.x + kill.y
    return diagonal < 15_000 if game.team_of[game.our_id] == 100 else diagonal > 15_000


def _exposure(games, window):
    lo, hi = window
    return sum(max(0, min(game.duration_s / 60, hi) - lo) for game in games)


def _death_count(games, predicate, window):
    lo, hi = window
    return sum(lo * 60_000 <= kill.t_ms < hi * 60_000
               and kill.t_ms <= game.duration_s * 1000 and predicate(game, kill)
               for game in games if max(0, min(game.duration_s / 60, hi) - lo) > 0
               for kill in LP.our_deaths(game))


def build(conn) -> tuple[str, dict]:
    """Construye Markdown reproducible y resumen, sin escribir archivos."""
    by_cohort = {cohort: load_games(conn, (cohort,)) for cohort in OWN}
    games = [game for cohort in OWN for game in by_cohort[cohort]]
    losses = [game for game in games if not game.win]
    wins = [game for game in games if game.win]
    summary = {"n": len(games), "losses": len(losses), "wins": len(wins), "cohorts": {}}
    lines = ["# Patrones de derrota", "",
             "Solo q (queue_id=420), cohortes propias master y emerald, duracion >=300 s.",
             "SQLite se abre en solo lectura. No es un detector, no escribe decisions ni solicita feedback.", "",
             "## Muestra", "", "| Cohorte | n partidas | Derrotas | Victorias |",
             "|---|---:|---:|---:|"]
    for cohort, group in by_cohort.items():
        n_wins = sum(game.win for game in group)
        summary["cohorts"][cohort] = {"n": len(group), "wins": n_wins, "losses": len(group) - n_wins}
        lines.append(f"| {cohort} | {len(group)} | {len(group) - n_wins} | {n_wins} |")
    lines += [f"| Total | {len(games)} | {len(losses)} | {len(wins)} |", "", LIMIT, ""]
    short = sum(game.duration_s < 840 or len(game.gold_by_min) <= 14 for game in games)
    lines += [f"Partidas sin frame 14 o terminadas antes del 14: {short}. "
              "Para oro al 14 se usa el ultimo frame disponible; no es una observacion real al 14.", ""]
    gold_buckets = [
        ("+2000+", lambda v: v >= 2000),
        ("0..+2000 (sin +2000)", lambda v: 0 <= v < 2000),
        ("-2000..0 (sin 0)", lambda v: -2000 <= v < 0),
        ("-4000..-2000 (sin -2000)", lambda v: -4000 <= v < -2000),
        ("< -4000", lambda v: v < -4000),
    ]
    lines += _bucket_table("Victoria por oro de equipo al minuto 14", games,
                           lambda game: LP.team_gold_diff(game, 14), gold_buckets)
    summary["gold14"] = LP.win_rate_by_bucket(games, lambda g: LP.team_gold_diff(g, 14), gold_buckets)
    lines += _bucket_table("Victoria por muertes propias antes del minuto 14", games,
                           LP.early_deaths, [("0-1", lambda v: v <= 1), ("2", lambda v: v == 2),
                           ("3", lambda v: v == 3), ("4", lambda v: v == 4), ("5+", lambda v: v >= 5)])
    lines += _bucket_table("Victoria por muertes encadenadas antes del minuto 14", games,
                           LP.chained_deaths, [("0", lambda v: v == 0), ("1", lambda v: v == 1),
                           ("2+", lambda v: v >= 2)])
    lines += ["Encadenada: muerte propia a <=96 s de la muerte propia anterior; corte estricto antes del 14.", "",
              "## Tasas de muertes propias por 10 minutos", "",
              f"n = {len(losses)} derrotas y {len(wins)} victorias. Ventanas [inicio, fin). "
              "IC bootstrap percentil 95%, 2000 remuestreos POR PARTIDA (derrotas y victorias por separado, sin estratificar por cohorte), semilla 1. "
              "Cada ventana remuestrea solo partidas con exposicion positiva.", "",
              "| Senal | n muertes D/V | Minutos D/V | Tasa D | Tasa V | D - V | IC 95% diferencia |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    rate_specs = [
        ("Antes del 14", lambda g, k: True, (0, 14)),
        ("Encadenadas antes del 14", _chained, (0, 14)),
        ("Desde el 14", lambda g, k: True, (14, math.inf)),
        ("Desde el 14 en mitad propia", _own_half, (14, math.inf)),
    ]
    summary["rates"] = []
    for label, predicate, window in rate_specs:
        loss_rate = LP.death_rate_per_10min(losses, predicate, window)
        win_rate = LP.death_rate_per_10min(wins, predicate, window)
        diff, lo, hi = LP.bootstrap_rate_diff(losses, wins, predicate, window)
        loss_minutes, win_minutes = _exposure(losses, window), _exposure(wins, window)
        lines.append(f"| {label} | {_death_count(losses, predicate, window)}/{_death_count(wins, predicate, window)} | "
                     f"{loss_minutes:.1f}/{win_minutes:.1f} | "
                     f"{_number(loss_rate) if loss_minutes else 'sin datos'} | "
                     f"{_number(win_rate) if win_minutes else 'sin datos'} | "
                     f"{_number(diff)} | {_number(lo)} a {_number(hi)} |")
        summary["rates"].append({"label": label, "loss_rate": loss_rate, "win_rate": win_rate,
                                 "diff": diff, "lo": lo, "hi": hi})
    missing_positions = sum(k.x is None or k.y is None for g in games for k in LP.our_deaths(g) if k.t_ms >= 840_000)
    lines += ["", "Mitad propia: x+y<15000 en equipo 100, x+y>15000 en equipo 200. "
              f"La diagonal se excluye. Muertes tardias sin posicion, excluidas del numerador posicional: {missing_positions}.",
              "El denominador es tiempo jugado en la ventana, no tiempo pasado en esa mitad.", LIMIT, "",
              "## Riesgo en el foso por banda de oro", "",
              "Unidad n: eventos de dragon/baron/heraldo desde el minuto 14. Oro en el frame del minuto del evento.",
              "Muerte propia a <=3000 unidades de la posicion ELITE, entre 60 s antes y 30 s despues (bordes incluidos).", "",
              "| Oro de equipo | n objetivos | Tomo el rival | Muerte cerca del foso |",
              "|---|---:|---:|---:|"]
    pit = LP.objective_pit_risk(games)
    summary["pit"] = pit
    for row in pit:
        lines.append(f"| {row['label']} | {row['n']} | {_percent(row['enemy_taken_rate'])} | {_percent(row['death_rate'])} |")
    missing_pit_positions = sum(
        event.x is None or event.y is None for game in games for event in game.elites
        if event.monster_type in ("DRAGON", "BARON_NASHOR", "RIFTHERALD")
        and 840_000 <= event.t_ms <= game.duration_s * 1000
        and event.killer_team in (100, 200))
    missing_death_positions = sum(
        kill.x is None or kill.y is None for game in games for kill in LP.our_deaths(game))
    lines += ["", f"Objetivos evaluados sin coordenadas: {missing_pit_positions}; "
              f"muertes propias sin coordenadas en toda la muestra: {missing_death_positions}.",
              "El valor exacto -6000 pertenece a <=-6000; la banda intermedia es (-6000,-3000]. "
              "Un evento cuenta como maximo una muerte, aunque haya varias. Sin posicion no se imputa cercania; "
              "las posiciones ausentes pueden subestimar esta proporcion. Los eventos de una partida no son independientes. "
              "La ventana incluye informacion posterior: este informe no demuestra prediccion en vivo.",
              LIMIT, "", "## Reglas visibles de deficit de oro", ""]
    samples = sum(max(0, min(int(g.duration_s // 60), len(g.gold_by_min) - 1,
                            len(g.level_by_min) - 1) - 14 + 1) for g in games)
    lines += [f"n = {samples} observaciones partida-minuto desde el 14 (hasta ultimo minuto entero con frames). "
              "Objetivo: oro de equipo <=-3000; precision = verdaderos positivos / marcados, "
              "recall = verdaderos positivos / minutos con deficit.", "",
              "| Regla | Precision | Recall | n minutos marcados |", "|---|---:|---:|---:|"]
    rules = LP.visible_deficit_rules(games)
    summary["rules"] = rules
    summary["minute_samples"] = samples
    for row in rules:
        lines.append(f"| {row['rule']} | {_percent(row['precision'])} | {_percent(row['recall'])} | {row['n_flagged']} |")
    lines += ["", "Las partidas largas aportan mas minutos; las observaciones no son independientes. "
              "Sin denominador se muestra sin datos. No hay validacion fuera de muestra ni control por parche, campeon o cohorte.",
              LIMIT, ""]
    return "\n".join(lines), summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Patrones de derrota, SQLite en solo lectura")
    ap.add_argument("--out", type=Path, help="informe Markdown de salida")
    args = ap.parse_args()
    db_path = Path(load_config()["paths"]["sqlite_db"]).resolve()
    out_path = (args.out or db_path.parent / "loss_patterns.md").resolve()
    if (out_path.suffix.lower() != ".md" or out_path == db_path
            or (out_path.exists() and out_path.samefile(db_path))):
        ap.error("--out debe ser un archivo .md distinto de la DB")
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        report, summary = build(conn)
    # Reemplazar el nombre evita truncar un destino convertido en enlace a la DB.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=out_path.parent,
                prefix=f".{out_path.name}.", suffix=".tmp", delete=False) as output:
            temp_path = Path(output.name)
            output.write(report)
        temp_path.replace(out_path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    print(f"Partidas: {summary['n']} | derrotas: {summary['losses']} | victorias: {summary['wins']}")
    for cohort, row in summary["cohorts"].items():
        print(f"{cohort}: n={row['n']}, D={row['losses']}, V={row['wins']}")
    for row in summary["rates"]:
        print(f"{row['label']}: D/V={_number(row['loss_rate'])}/{_number(row['win_rate'])} por 10 min; "
              f"D-V={_number(row['diff'])}, IC95 [{_number(row['lo'])}, {_number(row['hi'])}]")
    print(f"Objetivos desde el 14: {sum(row['n'] for row in summary['pit'])}; "
          f"observaciones minuto: {summary['minute_samples']}")
    print(f"Escrito: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
