"""Trade analyzer v2 — lane-phase damage exchanges in the bot lane.

v1 triggered on every death with snapshots, so each death produced a death_v1
AND a trade_v1 decision (player 2026-06-10: "me está duplicando las muertes").
v2 implements the player's own criterion for a BAD lane trade:

  Exchanging damage (mostly basic abilities) while the ADC farms is normal;
  the detector must flag when the exchange backfires: we lost significantly
  more HP than the enemy duo — or dumped our mana — so the NEXT exchange
  starts in deficit; aggravated when the following ~90s bring a duo death.

Data sources (honest about what each side exposes):
  - Per-minute damageStats from the RAW match timeline (enemy HP is not
    observable; who absorbed more damage is the proxy for who won the trade).
  - The recorder's 1Hz snapshots for OUR exact HP%/mana% when available.

Scope: lane phase only (minutes 2-14), only when we play bot (UTILITY/BOTTOM),
and only while we are physically in the bot half (roam fights belong to
death_v1/tempo_v1). A minute where WE die is skipped: death_v1 already argues
that play (this kills the duplication). Only our own death — death_v1 filters on
`victimId != us_id`, so an interval where the duo partner dies and we do not has
no death_v1 decision and used to fall through the cracks. A kill on either enemy
is a won trade.

WON trades are persisted flagged (`context.positive_trade = true`) so Calibrar
can pair them against lost ones ("¿cuál se jugó mejor?") but they are hidden
from the decision list — the player should not have to re-judge what went well.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..context import build_world_state, map_lane, map_side
from .base import Decision, Option
from .features import (
    ACTION_ATTRITION,
    ACTION_DISENGAGE,
    ACTION_FIGHT_ENTRY,
    build_action,
    build_state_features,
    mmss,
    setup_score,
)
from ..game_data import has_meaningful_resource
from ..snapshots import load_snapshots


DETECTOR_ID = "trade_v1"

LANE_PHASE_FIRST_MIN = 2     # ignore the walk-to-lane minute
LANE_PHASE_LAST_MIN = 14     # after this, fights are skirmishes, not lane trades
MIN_EXCHANGE_DMG = 250       # combined duo damage in a minute to call it a trade
MIN_ENEMY_DMG = 150          # enemy duo must have taken meaningful damage too:
                             # d_en~0 is unilateral poke, not a bilateral exchange
BAD_MIN_DIFF = 300           # we took at least this much MORE than the enemy duo
BAD_RATIO = 1.5              # and at least 1.5x theirs
LOW_MANA_PCT = 0.25          # ended the exchange nearly oom = next fight in deficit
CONSEQUENCE_WINDOW_MS = 90_000
NEXT_PLAY_OBJ_WINDOW_S = 90  # an objective this close is a play the attrition can buy

# EV de las opciones derivado del setup local (#23, task 5). Rango [0.25, 0.85]:
# ni una opcion es imposible ni ninguna es gratis. El factor del desgaste lo pone
# por debajo de la agresion pura con el mismo setup — desgastar es tradear
# aceptando quedar peor, asi que solo compensa cuando la jugada que lo cobra existe.
EV_FLOOR = 0.25
EV_SPAN = 0.60
EV_ATTRITION_FACTOR = 0.8
EV_ATTRITION_NO_PLAY = 0.20  # sin jugada proxima, el desgaste no es una opcion real


def _next_play_available(state_features: dict) -> bool:
    """¿Hay una jugada proxima que pague el HP perdido en un desgaste?

    El criterio es del jugador: "te lo marco valido si hay una jugada proxima,
    crashear una wave e ir a base, dejarlos bajos de vida para hacer un dragon".
    Dos formas de que exista, ambas con datos que el detector YA tiene:
      - la oleada empujada hacia el enemigo: esta por crashear, el reset es gratis;
      - un objetivo mayor a menos de NEXT_PLAY_OBJ_WINDOW_S.
    Hoy `objectives` viene sin poblar en trade_v1 (no le pasamos objetivo a
    build_state_features), asi que en la practica manda la oleada; la rama del
    objetivo esta escrita para que funcione sola el dia que se pueble, sin
    consultas nuevas a la DB.
    """
    wave = (state_features.get("wave_tempo") or {}).get("wave_state")
    if wave == "pushed_into_enemy":
        return True
    objectives = state_features.get("objectives") or {}
    t_obj = objectives.get("time_to_obj_s")
    return bool(objectives.get("next_major_obj")) and isinstance(t_obj, (int, float)) \
        and t_obj <= NEXT_PLAY_OBJ_WINDOW_S


def _our_stats_at(snapshots, t_s, has_resource) -> dict:
    """Our exact HP%/mana% at ~t_s from the closest snapshot (None without recorder)."""
    if not snapshots:
        return {"hp_pct": None, "mana_pct": None}
    snap = min(snapshots, key=lambda s: abs(s[0] - t_s))
    if abs(snap[0] - t_s) > 90:
        return {"hp_pct": None, "mana_pct": None}
    cs = (snap[1].get("activePlayer") or {}).get("championStats") or {}
    hp = (cs["currentHealth"] / cs["maxHealth"]
          if cs.get("currentHealth") is not None and cs.get("maxHealth") else None)
    mp = (cs["resourceValue"] / cs["resourceMax"]
          if has_resource and cs.get("resourceValue") is not None and cs.get("resourceMax") else None)
    return {"hp_pct": round(hp, 2) if hp is not None else None,
            "mana_pct": round(mp, 2) if mp is not None else None}


def _taken_total(pf: dict) -> float:
    d = pf.get("damageStats") or {}
    return (d.get("physicalDamageTaken") or 0) + (d.get("magicDamageTaken") or 0) \
        + (d.get("trueDamageTaken") or 0)


def analyze_trades(conn: sqlite3.Connection, match_id: str) -> list[Decision]:
    match = conn.execute(
        "SELECT our_puuid, timeline_json_path FROM matches WHERE match_id = ?", (match_id,)
    ).fetchone()
    if not match or not match["timeline_json_path"]:
        return []
    us = conn.execute(
        "SELECT participant_id, champion_name, team_id, team_position FROM participants "
        "WHERE match_id = ? AND puuid = ?", (match_id, match["our_puuid"]),
    ).fetchone()
    if us is None or us["team_position"] not in ("UTILITY", "BOTTOM"):
        return []  # lane-trade analysis is a bot-lane concept (player's own framing)
    us_id, our_team = us["participant_id"], us["team_id"]
    _has_resource = has_meaningful_resource(us["champion_name"] or "")

    parts = {r["participant_id"]: r for r in conn.execute(
        "SELECT participant_id, team_id, team_position, champion_name FROM participants "
        "WHERE match_id = ?", (match_id,))}
    my_duo = [pid for pid, p in parts.items()
              if p["team_id"] == our_team and p["team_position"] in ("UTILITY", "BOTTOM")]
    enemy_duo = [pid for pid, p in parts.items()
                 if p["team_id"] != our_team and p["team_position"] in ("UTILITY", "BOTTOM")]
    if len(my_duo) < 2 or len(enemy_duo) < 2:
        return []

    try:
        timeline = json.loads(Path(match["timeline_json_path"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    frames = (timeline.get("info") or {}).get("frames") or []
    if len(frames) < LANE_PHASE_FIRST_MIN + 2:
        return []

    # Cumulative damage-taken per duo per frame -> per-minute deltas.
    def duo_taken(frame, duo):
        pfs = frame.get("participantFrames") or {}
        return sum(_taken_total(pfs.get(str(pid)) or {}) for pid in duo)

    # PLAYER deaths / enemy duo deaths, indexed by FRAME INDEX (skip = death_v1's
    # turf / a won kill). See the frame-index note above: an event at clock t
    # belongs to the interval [(m-1)*60s, m*60s] with m = t//60_000 + 1, so a
    # death at 7:30 is indexed 8 (the interval [7, 8]), NOT 7. Indexing it by
    # t//60_000 was the original off-by-one: the gate then skipped the interval
    # AFTER the one the death actually happened in.
    #
    # ASYMMETRY (deliberate): the exclusion set holds only OUR OWN deaths, while
    # the won set holds ANY enemy duo death. death_v1 skips every victim that is
    # not us (`victimId != us_id`), so an interval where the duo PARTNER dies and
    # we do not has no death_v1 decision — excluding it left it unanalyzed. A kill
    # on either enemy is a won trade regardless of who fell, so there the full duo
    # is right.
    player_death_frame, enemy_death_frame = set(), set()
    for r in conn.execute(
        "SELECT timestamp_ms, payload_json FROM timeline_events WHERE match_id=? AND type='CHAMPION_KILL'",
        (match_id,),
    ):
        ev = json.loads(r["payload_json"])
        m_frame = r["timestamp_ms"] // 60_000 + 1
        if ev.get("victimId") == us_id:
            player_death_frame.add(m_frame)
        elif ev.get("victimId") in enemy_duo:
            enemy_death_frame.add(m_frame)

    # Recorder snapshots (our exact HP/mana). Optional: damage deltas alone suffice.
    session_row = conn.execute(
        "SELECT session_dir FROM sessions WHERE match_id = ?", (match_id,)).fetchone()
    snapshots = (load_snapshots(Path(session_row["session_dir"]))
                 if session_row and session_row["session_dir"] else [])

    # FRAME INDEX vs CLOCK INTERVAL — the distinction the original code lost.
    # Match-V5 frame `m` is a snapshot at clock m*60s (verified: exact multiples
    # of 60s, ~4ms/min of drift). Its damageStats are cumulative, so the delta
    # frames[m] - frames[m-1] describes the interval [(m-1)*60s, m*60s]. Below,
    # `m` is ALWAYS a frame index; the exchange it describes happened during the
    # minute BEFORE that frame.
    last_min = min(LANE_PHASE_LAST_MIN, len(frames) - 1)
    minutes = []   # (frame_index, kind, d_us, d_enemy, our_stats)
    for m in range(LANE_PHASE_FIRST_MIN, last_min + 1):
        d_us = duo_taken(frames[m], my_duo) - duo_taken(frames[m - 1], my_duo)
        d_en = duo_taken(frames[m], enemy_duo) - duo_taken(frames[m - 1], enemy_duo)
        if d_us + d_en < MIN_EXCHANGE_DMG:
            continue  # no real exchange this minute
        if d_en < MIN_ENEMY_DMG:
            continue  # unilateral poke: enemy duo took ~0, not a bilateral exchange
        # Were WE in the bot half? Roams/objective fights belong to other detectors.
        # Sample the MIDPOINT of the interval, not its closing edge: at m*60_000
        # the exchange is already over and we may have walked somewhere else.
        # The tiebreak on timestamp_ms matters because timeline_frames also comes
        # from Match-V5 (one row per minute), so the midpoint is usually exactly
        # equidistant from both bounding frames — we then prefer the earlier one,
        # i.e. where we stood when the exchange began.
        fr = conn.execute(
            "SELECT position_x x, position_y y FROM timeline_frames WHERE match_id=? AND "
            "participant_id=? ORDER BY ABS(timestamp_ms-?), timestamp_ms LIMIT 1",
            (match_id, us_id, int((m - 0.5) * 60_000))).fetchone()
        if fr and fr["x"] is not None:
            in_bot_lane = map_lane(fr["x"], fr["y"]) == "bot lane"
            in_bot_quadrant_our_side = (
                fr["x"] > 7500 and fr["y"] < 7500
                and map_side(fr["x"], fr["y"], our_team) == "nuestro lado"
            )
            if not (in_bot_lane or in_bot_quadrant_our_side):
                continue
        if m in player_death_frame:
            continue  # WE died here: death_v1 argues that play (no duplicates).
                      # Only our own death — death_v1 covers no other victim.
        # HP/mana at the CLOSING edge of this interval (m*60s) is the intended
        # reading: "how we came out of the exchange". Unlike the position read
        # above, m*60_000 is the right instant here, not the off-by-one.
        stats = _our_stats_at(snapshots, m * 60.0, _has_resource)
        lost = (d_us - d_en >= BAD_MIN_DIFF and d_us >= BAD_RATIO * max(d_en, 1)
                and d_en >= MIN_ENEMY_DMG)
        oom = (stats["mana_pct"] is not None and stats["mana_pct"] < LOW_MANA_PCT
               and d_us >= d_en)  # dumped mana without winning the exchange
        won = (m in enemy_death_frame) or (d_en - d_us >= BAD_MIN_DIFF and d_en >= BAD_RATIO * max(d_us, 1))
        if lost or oom:
            minutes.append((m, "bad", d_us, d_en, stats))
        elif won:
            minutes.append((m, "good", d_us, d_en, stats))

    # Merge consecutive bad minutes: one sustained losing pattern = one decision.
    decisions: list[Decision] = []
    i = 0
    while i < len(minutes):
        m, kind, d_us, d_en, stats = minutes[i]
        j = i
        while (kind == "bad" and j + 1 < len(minutes)
               and minutes[j + 1][1] == "bad" and minutes[j + 1][0] == minutes[j][0] + 1):
            j += 1
            d_us += minutes[j][2]
            d_en += minutes[j][3]
            stats = minutes[j][4]  # state at the END of the run (edge of the last interval)
        facts = _gather_trade_facts(conn, match_id, us_id, our_team, parts, my_duo,
                                    m, minutes[j][0], kind, d_us, d_en, stats)
        decisions.append(_evaluate(facts))
        i = j + 1
    return decisions


@dataclass
class TradeFacts:
    """Plain, DB-free snapshot of a bot-lane trade run — the boundary between
    _gather_trade_facts (consequence query + state_features) and _evaluate (pure)."""
    match_id: str
    t_ms: int
    m_start: int
    m_end: int
    positive: bool
    d_us: int
    d_en: int
    hp_pct: Optional[float]
    mana_pct: Optional[float]
    consequence: Optional[str]
    state_features: dict


def _gather_trade_facts(conn, match_id, us_id, our_team, parts, my_duo,
                        m_start, m_end, kind, d_us, d_en, stats) -> TradeFacts:
    """The I/O layer for one trade run: the lost-trade consequence query and the
    state_features at the run's start.

    `m_start`/`m_end` are FRAME INDICES; the run covers the clock interval
    [(m_start-1)*60s, m_end*60s]. The decision's timestamp is that interval's
    START — the clip has to open before the exchange, not after it ended.
    """
    t_ms = (m_start - 1) * 60_000
    positive = kind == "good"

    # Consequence after a lost trade: a duo death in the next ~90s confirms the
    # deficit got cashed in (the player's own chain: bad trade -> kill/freeze/gank).
    # This window DOES start at m_end*60_000: that is the closing edge of the last
    # interval of the run, i.e. the moment the trade finished. "After the trade"
    # is exactly what it should mean, so it is not part of the off-by-one.
    consequence = None
    if not positive:
        for r in conn.execute(
            "SELECT payload_json FROM timeline_events WHERE match_id=? AND type='CHAMPION_KILL' "
            "AND timestamp_ms BETWEEN ? AND ?",
            (match_id, m_end * 60_000, m_end * 60_000 + CONSEQUENCE_WINDOW_MS),
        ):
            ev = json.loads(r["payload_json"])
            if ev.get("victimId") in my_duo:
                victim = parts.get(ev.get("victimId"))
                consequence = f"muerte de {victim['champion_name'] if victim else 'tu duo'} en los 90s siguientes"
                break

    ws = build_world_state(conn, match_id, t_ms)
    sf = build_state_features(conn, match_id, ws, us_id, our_team)

    return TradeFacts(
        match_id=match_id, t_ms=t_ms, m_start=m_start, m_end=m_end, positive=positive,
        d_us=d_us, d_en=d_en, hp_pct=stats["hp_pct"], mana_pct=stats["mana_pct"],
        consequence=consequence, state_features=sf,
    )


def _evaluate(facts: TradeFacts) -> Decision:
    """Pure decision logic: options, argument, context, Decision. No conn."""
    m_start, m_end = facts.m_start, facts.m_end
    d_us, d_en = facts.d_us, facts.d_en
    consequence = facts.consequence
    positive = facts.positive

    # The run's frames [m_start, m_end] cover the clock window [m_start-1, m_end].
    # Always show that RANGE: a per-minute delta cannot locate the exchange at an
    # instant, and calling it "min 14" when it happened at 13:13 is a lie the
    # player already caught.
    span = f"min {m_start - 1}-{m_end}"
    span_prose = f"el minuto {m_start - 1} y el {m_end}"
    ratio_txt = f"recibieron {int(d_us)} de daño vs {int(d_en)} del duo enemigo"
    hp_txt = (f"HP {int(facts.hp_pct*100)}%" if facts.hp_pct is not None else None)
    mana_txt = (f"maná {int(facts.mana_pct*100)}%" if facts.mana_pct is not None else None)
    state_txt = " · ".join(x for x in (hp_txt, mana_txt) if x)

    # --- La recomendacion sale del calculo de ventaja, no del desenlace (#23).
    # setup = 0.3*info + 0.7*power: la misma cuenta que _commit_fit, aqui aplicada
    # al EV de cada opcion para que la puntuacion responda a si PODIAS ganar el
    # intercambio (vida/mana, numeros locales, oro/nivel, vision) y no a si lo
    # ganaste. Sin ninguna de las dos senales, 0.5 = parejo: las tres opciones
    # quedan practicamente empatadas, que es la respuesta honesta.
    setup = setup_score(facts.state_features)
    setup = 0.5 if setup is None else setup
    next_play = _next_play_available(facts.state_features)
    ev_fight = round(EV_FLOOR + EV_SPAN * setup, 2)
    ev_wait = round(EV_FLOOR + EV_SPAN * (1.0 - setup), 2)
    ev_attrition = (round(EV_FLOOR + EV_SPAN * setup * EV_ATTRITION_FACTOR, 2)
                    if next_play else EV_ATTRITION_NO_PLAY)

    cons_wait = (
        "Cedes la ventana pero llegas a la siguiente con maná, cooldowns y posición: "
        "el intercambio del support solo paga cuando desgasta más de lo que cuesta."
    )
    # El EV del desgaste es binario (arriba), pero la PROSA no puede afirmar lo que
    # no sabemos: en ~38% de las decisiones no hay lectura de oleada, y ahi "no hay
    # jugada proxima" seria un invento. Tres textos, dos EV.
    if next_play:
        cons_attrition = (
            "Pierdes HP a propósito y lo cobras en la jugada siguiente: crashear la "
            "oleada y resetear barato, o dejarlos bajos para el objetivo."
        )
    elif (facts.state_features.get("wave_tempo") or {}).get("wave_state") is not None:
        cons_attrition = (
            "No se ve la jugada próxima que cobre el HP perdido (la oleada no está "
            "por crashear ni hay objetivo cerca), así que aquí el desgaste es "
            "tradear en desventaja con otro nombre."
        )
    else:
        cons_attrition = (
            "Sin lectura de la oleada no se puede confirmar una jugada próxima que "
            "cobre el HP; si la había (crashear y resetear, o dejarlos bajos para el "
            "objetivo), esta era la opción — márcala y el EV se corrige."
        )

    if positive:
        cons_fight = f"Cambias daño ahora: {ratio_txt}."
        moment = f"Trade ganado en bot ({span})"
        outcome = f"Intercambio a favor: {ratio_txt}." + (f" Tu estado al cierre: {state_txt}." if state_txt else "")
        argument = (
            f"Entre {span_prose} el intercambio de daño en bot salió a favor ({ratio_txt}). "
            f"Registrado para comparación (Calibrar), no para revisión."
        )
    else:
        why = []
        if d_us - d_en >= BAD_MIN_DIFF:
            why.append("perdieron el intercambio de vida")
        if facts.mana_pct is not None and facts.mana_pct < LOW_MANA_PCT:
            why.append("quedaste casi sin maná para la siguiente pelea")
        why_txt = " y ".join(why) or "el intercambio salió en contra"
        cons_fight = (
            f"Cambias daño ahora: {why_txt} ({ratio_txt})."
            + (f" {consequence.capitalize()}." if consequence else "")
        )
        moment = f"Trade perdido en bot ({span})"
        outcome = (f"{why_txt.capitalize()}: {ratio_txt}."
                   + (f" Estado al cierre: {state_txt}." if state_txt else "")
                   + (f" Consecuencia: {consequence}." if consequence else ""))
        argument = (
            f"Entre {span_prose} el duo recibió {int(d_us)} de daño contra {int(d_en)} del enemigo: {why_txt}. "
            + (f"Tu estado al cierre ({state_txt}) confirma el déficit para la siguiente ventana. " if state_txt else "")
            + (f"La consecuencia llegó: {consequence}. " if consequence else "")
            + "El criterio no es no tradear: es cambiar daño solo cuando maná/cooldowns/posición están a favor; "
              "si no, respetar, farmear y esperar tu ventana. "
            + "Nota: el daño recibido por minuto es un proxy (Match-V5 no expone el HP/daño enemigo "
              "evento a evento) y solo ubica el intercambio dentro de esa ventana de un minuto, "
              "no en un instante exacto."
        )

    # --- Un solo eje, gane o pierda el intercambio (#23, task 4).
    # Lo que se juzga es la decision de entrar, no el desenlace. Antes el MENU lo
    # elegia el resultado: 2 opciones si ganaste ("Trade ganado (lo que hiciste)"
    # contra "no intercambiar", que no es una eleccion) y 3 si perdiste. Un menu
    # que ya sabe como termino no es una decision.
    #
    # ORDEN CONGELADO [disengage, fight_entry, attrition_trade]: es el que ya
    # tienen las decisiones perdidas marcadas, asi que su action_fingerprint queda
    # identico y las marcas sobreviven. Cualquier otro orden las rompe
    # (user_best_option es un INDICE posicional).
    #
    # La opcion tomada es siempre fight_entry: el detector solo emite cuando hubo
    # intercambio bilateral (MIN_ENEMY_DMG), ganado o perdido.
    taken_id = ACTION_FIGHT_ENTRY
    options = [
        Option(
            label="No tradear: respetar y farmear hasta tener ventana "
                  "(mana, cooldowns, posicion)",
            predicted_consequence=cons_wait,
            ev_score=ev_wait,
            taken=(taken_id == ACTION_DISENGAGE),
        ),
        Option(
            label="Tradear: cambiar dano ahora",
            predicted_consequence=cons_fight,
            ev_score=ev_fight,
            taken=(taken_id == ACTION_FIGHT_ENTRY),
        ),
        Option(
            label="Desgaste intencional: perder HP a cambio de una jugada proxima "
                  "(crashear la oleada y resetear, o dejarlos bajos para el dragon)",
            predicted_consequence=cons_attrition,
            ev_score=ev_attrition,
            taken=(taken_id == ACTION_ATTRITION),
        ),
    ]
    action_ids = [ACTION_DISENGAGE, ACTION_FIGHT_ENTRY, ACTION_ATTRITION]

    context = {
        "time_mmss": mmss(facts.t_ms),
        "minute_span": span,
        "span_minutes": m_end - (m_start - 1),
        "duo_damage_taken": int(d_us),
        "enemy_duo_damage_taken": int(d_en),
        "our_hp_pct_end": facts.hp_pct,
        "our_mana_pct_end": facts.mana_pct,
        "consequence": consequence,
        "positive_trade": positive,
        # Por que el desgaste puntua lo que puntua: auditable sin releer el codigo.
        "next_play_available": next_play,
        "state_features": facts.state_features,
    }
    context["action"] = build_action(DETECTOR_ID, options, action_ids)

    return Decision(
        detector_id=DETECTOR_ID,
        match_id=facts.match_id,
        game_time_ms=facts.t_ms,
        moment=moment,
        outcome=outcome,
        context=context,
        options=options,
        argument=argument,
    )
