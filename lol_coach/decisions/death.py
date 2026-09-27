"""Death analyzer: argues about each of the user's deaths in a match.

For each CHAMPION_KILL event where the user is the victim:
  - Reconstruct the world state at the moment of death.
  - Identify nearest ally (proxy for "could anyone help?").
  - Compute how long each enemy had been off the visibility radar
    (events-based lower bound — see context.last_event_appearance).
  - Enumerate options (Engage/Retreat) with heuristic EV scores.
  - Compose an argument citing the facts that drove the EV ranking.

Heuristic sources (Phase 1; will be calibrated later):
  - Ally proximity threshold from common coaching wisdom (~2 screen units
    in lane support discussions). 2500 LoL distance units approximates
    "close enough to peel/follow-up before disengage".
  - Unseen-enemy threshold (20s) approximates one rotation window of a
    jungler from a clear of a buff camp to a lane gank.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from typing import Optional

from ..context import (
    ALLY_NEAR_UNITS,
    UNSEEN_DANGEROUS_S,
    build_world_state,
    distance,
    find_clip_for_event,
    last_event_appearance,
    map_lane,
    map_side,
)
from .base import Decision, Option
from .features import (
    ACTION_DISENGAGE,
    ACTION_FIGHT_ENTRY,
    THREAT_RADIUS_UNITS,
    build_action,
    build_state_features,
    enemy_jungler_id,
    mmss,
    nearest_ally_tower_dist,
    setup_score,
    taken_index,
)


DETECTOR_ID = "death_v1"

FIGHT_RADIUS_UNITS = 4000      # kills beyond this from your death are a different fight (noise)
SACRIFICE_RADIUS_UNITS = 2500  # an objective taken this close = your death bought it
PRE_FIGHT_WINDOW_MS = 10_000   # visibility for the COMMIT decision is measured this long
                               # BEFORE the death: the kill event itself "reveals" the
                               # killer, so measuring at death time made every gank look
                               # "seen" (measured 2026-06-11: 60% of the player's deaths
                               # had an event-unseen killer >= 20s, and none of them
                               # counted in enemies_unseen — info term inflated exactly
                               # where the player marks "debí retirarme").

# EV de las opciones derivado del setup local (#23 Fase C), la misma cuenta que
# trade_v1 (Task 5): el agresivo escala con el setup, el defensivo con su
# complemento. Rango [0.25, 0.85]. Antes cada rama fijaba sus dos EV (0.64/0.72,
# 0.62/0.52, 0.68/0.42, 0.70/0.42, 0.65/0.45): la rama -que sale del desenlace-
# decidia la recomendacion, y el menu por si solo predecia la opcion de max EV en
# 1949 de 1952 decisiones. Ahora la recomendacion depende de si PODIAS
# ganar la pelea, no de como termino.
EV_FLOOR = 0.25
EV_SPAN = 0.60


@dataclass
class DeathFacts:
    """Plain, DB-free snapshot of one death — the boundary between the I/O layer
    (_gather_facts) and the pure decision logic (_evaluate)."""
    match_id: str
    death_ms: int
    death_pos: tuple
    lane: str
    map_side: str
    killer: str
    assisters: list
    first_blood: bool
    our_state: dict
    nearest_ally_champ: Optional[str]
    nearest_ally_dist: Optional[int]
    we_had_help: bool
    fighters_for: int
    fighters_against: int
    score_our_kills: int
    score_enemy_kills: int
    non_combatant_unseen: list
    combatant_unseen: list
    retreating: bool
    got_value: bool
    enemy_deaths: int
    ally_deaths: int
    ally_died_first: bool
    sacrifice_obj: Optional[dict]
    top_damager: Optional[dict]
    state_features: dict
    clip_path: Optional[str]
    clip_offset_s: Optional[float]


_OBJ_NAME = {
    "TOWER_BUILDING": "la torre", "INHIBITOR_BUILDING": "el inhibidor",
    "DRAGON": "el dragón", "BARON_NASHOR": "el barón", "RIFTHERALD": "el heraldo",
}


def _objective_taken_near(conn, match_id, death_ms, death_x, death_y, our_team) -> dict | None:
    """A tower / epic monster our team took within SACRIFICE_RADIUS and ~10s around the
    death. Then the death wasn't a blind feed — it bought that objective (player feedback:
    'tumbé la torre e hice el mayor daño antes de morir, fue buena planeación')."""
    if death_x is None:
        return None
    our_pids = {r["participant_id"] for r in conn.execute(
        "SELECT participant_id FROM participants WHERE match_id=? AND team_id=?", (match_id, our_team))}
    for r in conn.execute(
        "SELECT timestamp_ms, payload_json FROM timeline_events WHERE match_id=? AND "
        "type IN ('BUILDING_KILL','ELITE_MONSTER_KILL') AND timestamp_ms BETWEEN ? AND ?",
        (match_id, death_ms - 10000, death_ms + 4000),
    ):
        ev = json.loads(r["payload_json"])
        if ev.get("killerId") not in our_pids and ev.get("killerTeamId") != our_team:
            continue  # enemy took it — not your sacrifice
        pos = ev.get("position") or {}
        if pos.get("x") is None or death_x is None:
            continue
        if math.hypot(pos["x"] - death_x, pos["y"] - death_y) > SACRIFICE_RADIUS_UNITS:
            continue
        kind = ev.get("buildingType") or ev.get("monsterType")
        return {"name": _OBJ_NAME.get(kind, "el objetivo"),
                "rel_s": round((r["timestamp_ms"] - death_ms) / 1000.0, 1)}
    return None


def analyze_deaths(conn: sqlite3.Connection, match_id: str) -> list[Decision]:
    # Find our participant_id
    match = conn.execute(
        "SELECT our_puuid FROM matches WHERE match_id = ?", (match_id,)
    ).fetchone()
    our_puuid = match["our_puuid"]
    us = conn.execute(
        "SELECT participant_id, team_id FROM participants "
        "WHERE match_id = ? AND puuid = ?",
        (match_id, our_puuid),
    ).fetchone()
    us_id = us["participant_id"]
    our_team = us["team_id"]

    # All deaths where we were the victim
    rows = conn.execute(
        "SELECT timestamp_ms, payload_json FROM timeline_events "
        "WHERE match_id = ? AND type = 'CHAMPION_KILL' ORDER BY timestamp_ms",
        (match_id,),
    ).fetchall()

    decisions: list[Decision] = []
    for row in rows:
        ev = json.loads(row["payload_json"])
        if ev.get("victimId") != us_id:
            continue
        facts = _gather_facts(conn, match_id, row["timestamp_ms"], ev, us_id, our_team)
        decisions.append(_evaluate(facts))
    return decisions


def _gather_facts(
    conn: sqlite3.Connection,
    match_id: str,
    death_ms: int,
    payload: dict,
    us_id: int,
    our_team: int,
) -> DeathFacts:
    """The ONLY I/O for a death: world state, visibility, fight outcome, sacrifice,
    state_features and clip — all read here and returned as plain data."""
    ws = build_world_state(conn, match_id, death_ms)
    us = ws.players[us_id]

    killer_id = payload.get("killerId")
    assister_ids = payload.get("assistingParticipantIds") or []
    pos = payload.get("position") or {}
    death_x, death_y = pos.get("x"), pos.get("y")

    killer = ws.players.get(killer_id) if killer_id else None
    assisters = [ws.players[a].champion for a in assister_ids if a in ws.players]

    # Nearest ally (excluding us)
    nearest_ally = None
    nearest_dist = None
    for p in ws.players.values():
        if p.team_id != our_team or p.is_us:
            continue
        d = distance(us, p)
        if d is None:
            continue
        if nearest_dist is None or d < nearest_dist:
            nearest_dist = d
            nearest_ally = p

    # Enemy visibility analysis — only for enemies NOT in this fight
    combat_pids = {killer_id, *assister_ids}
    enemy_jg = enemy_jungler_id(conn, match_id, our_team)
    non_combatant_unseen: list[tuple[str, float]] = []
    for p in ws.players.values():
        if p.team_id == our_team or p.participant_id in combat_pids:
            continue
        last_ms = last_event_appearance(conn, match_id, p.participant_id, death_ms)
        unseen_s = (death_ms - last_ms) / 1000.0 if last_ms else death_ms / 1000.0
        if unseen_s < UNSEEN_DANGEROUS_S:
            continue
        # Local-threat filter: an unseen enemy matters to THIS death only if it
        # could reach the fight — the jungler always can; a laner only if its last
        # position is near. A far-side laner in its own lane is noise (the user's
        # complaint: top enemy unseen 212s while she dies in bot is irrelevant).
        if p.participant_id == enemy_jg:
            local = True
        elif p.pos_x is not None and death_x is not None:
            local = math.hypot(p.pos_x - death_x, p.pos_y - death_y) <= THREAT_RADIUS_UNITS
        else:
            local = True
        if local:
            non_combatant_unseen.append((p.champion, unseen_s))

    # Visibility of the COMBATANTS (killer + assisters), measured at the moment
    # you could still decide (PRE_FIGHT_WINDOW_MS before the death). They used
    # to be excluded because the kill reveals them — which silently turned
    # ganks-from-fog into "seen" enemies. No local filter needed: they reached
    # you by definition.
    pre_ms = death_ms - PRE_FIGHT_WINDOW_MS
    combatant_unseen: list[tuple[str, float]] = []
    if pre_ms > 0:
        for pid in combat_pids:
            p = ws.players.get(pid)
            if p is None or p.team_id == our_team:
                continue
            last_ms = last_event_appearance(conn, match_id, pid, pre_ms)
            unseen_s = (pre_ms - last_ms) / 1000.0 if last_ms else pre_ms / 1000.0
            if unseen_s >= UNSEEN_DANGEROUS_S:
                combatant_unseen.append((p.champion, unseen_s))

    # Effective fighter count
    we_had_help = nearest_dist is not None and nearest_dist <= ALLY_NEAR_UNITS
    fighters_for = 2 if we_had_help else 1
    fighters_against = 1 + len(assister_ids)

    # Score state at this moment
    our_team_kills = sum(p.kills for p in ws.players.values() if p.team_id == our_team)
    enemy_team_kills = sum(p.kills for p in ws.players.values() if p.team_id != our_team)

    # First blood?
    is_first_blood = payload.get("killType") == "KILL_FIRST_BLOOD"

    lane = map_lane(death_x, death_y)
    side = map_side(death_x, death_y, our_team)

    # Damage breakdown if available
    damage_received = payload.get("victimDamageReceived") or []
    top_damager = None
    if damage_received:
        agg: dict[int, int] = {}
        for d in damage_received:
            spid = d.get("participantId")
            if spid is None:
                continue
            agg[spid] = agg.get(spid, 0) + (d.get("magicDamage", 0) + d.get("physicalDamage", 0) + d.get("trueDamage", 0))
        if agg:
            top_pid = max(agg, key=agg.get)
            if top_pid in ws.players:
                top_damager = {
                    "champion": ws.players[top_pid].champion,
                    "damage": agg[top_pid],
                }

    our_state = {
        "level": us.level,
        "current_gold": us.current_gold,  # spendable at the moment (rule #7)
        "total_gold": us.total_gold,      # lifetime accumulated, stat only
        "cs": us.minions_killed,
        "kills_so_far": us.kills,
        "deaths_so_far": us.deaths,  # includes this death
        "assists_so_far": us.assists,
    }

    # Inferred action inputs (player feedback: the detector assumed "you
    # stayed/engaged" even when you were retreating, or when the engage paid off).
    # retreating = you moved meaningfully toward your own tower before dying
    # (caught while leaving). got_value = you got a kill/assist around the death.
    prev = conn.execute(
        "SELECT position_x, position_y FROM timeline_frames WHERE match_id=? AND "
        "participant_id=? AND timestamp_ms < ? ORDER BY timestamp_ms DESC LIMIT 1",
        (match_id, us_id, death_ms)).fetchone()
    d_now = nearest_ally_tower_dist(death_x, death_y, our_team)
    d_prev = (nearest_ally_tower_dist(prev["position_x"], prev["position_y"], our_team)
              if prev and prev["position_x"] is not None else None)
    retreating = (isinstance(d_now, (int, float)) and isinstance(d_prev, (int, float))
                  and d_now < d_prev - 400)
    got_value = False
    enemy_deaths = ally_deaths = 0   # LOCAL to this death: cross-map kills are a different fight
    ally_died_first = False          # an ally fell well before you = you joined a losing fight
    for r in conn.execute(
        "SELECT timestamp_ms, payload_json FROM timeline_events WHERE match_id=? AND "
        "type='CHAMPION_KILL' AND timestamp_ms BETWEEN ? AND ?",
        (match_id, death_ms - 20000, death_ms + 5000)):
        ev2 = json.loads(r["payload_json"])
        # Personal kill/assist = the play paid off. Window is wide (-20s) and NOT
        # spatially filtered: extended fights drift across the map (player case: 2
        # assists at -16/-20s and ~4100u got dropped, misclassifying a won fight).
        if ev2.get("killerId") == us_id or us_id in (ev2.get("assistingParticipantIds") or []):
            got_value = True
        vid2 = ev2.get("victimId")
        if vid2 == us_id:
            continue
        vp = ws.players.get(vid2)
        if vp is None:
            continue
        kp = ev2.get("position") or {}
        if (death_x is not None and kp.get("x") is not None
                and math.hypot(kp["x"] - death_x, kp["y"] - death_y) > FIGHT_RADIUS_UNITS):
            continue  # a kill across the map — not part of the fight you died in
        rel_ms = r["timestamp_ms"] - death_ms
        if vp.team_id == our_team:
            if -20000 <= rel_ms <= -3000:
                ally_died_first = True   # the fight was already lost/going when you committed
            if rel_ms >= -10000:
                ally_deaths += 1
        elif rel_ms >= -10000:
            enemy_deaths += 1

    # Did our team take an objective right where you died? Then it was a calculated
    # trade of your life for the objective, not a blind feed.
    sacrifice_obj = _objective_taken_near(conn, match_id, death_ms, death_x, death_y, our_team)

    # Standardized state block. enemies_unseen counts everyone you could NOT see at
    # decision time: flankers outside the fight AND combatants that came out of
    # fog (pre-fight cutoff) — a gank you never saw is the textbook low-info state.
    state_features = build_state_features(
        conn, match_id, ws, us_id, our_team,
        enemies_unseen=len(non_combatant_unseen) + len(combatant_unseen),
    )

    clip_path, clip_offset = find_clip_for_event(conn, match_id, death_ms)

    return DeathFacts(
        match_id=match_id, death_ms=death_ms, death_pos=(death_x, death_y),
        lane=lane, map_side=side,
        killer=killer.champion if killer else "unknown",
        assisters=assisters, first_blood=is_first_blood, our_state=our_state,
        nearest_ally_champ=nearest_ally.champion if nearest_ally else None,
        nearest_ally_dist=int(nearest_dist) if nearest_dist is not None else None,
        we_had_help=we_had_help, fighters_for=fighters_for, fighters_against=fighters_against,
        score_our_kills=our_team_kills, score_enemy_kills=enemy_team_kills,
        non_combatant_unseen=non_combatant_unseen, combatant_unseen=combatant_unseen,
        retreating=retreating, got_value=got_value,
        enemy_deaths=enemy_deaths, ally_deaths=ally_deaths, ally_died_first=ally_died_first,
        sacrifice_obj=sacrifice_obj, top_damager=top_damager,
        state_features=state_features, clip_path=clip_path, clip_offset_s=clip_offset,
    )


def _evaluate(facts: DeathFacts) -> Decision:
    """Pure decision logic: classify the action, build options/context/argument,
    assemble the Decision. No conn — everything comes from `facts`."""
    # Classify the REAL action. Priority: bought-an-objective > personally paid off >
    # joined an already-losing fight to support > a 2-sided trade you lost > caught
    # while retreating > a blind over-extension.
    skirmish = (facts.enemy_deaths + facts.ally_deaths) >= 1
    # "Rindio" es una afirmacion sobre el RESULTADO del intercambio, no sobre si
    # la decision fue buena. Antes bastaba `got_value` (una kill o asistencia
    # tuya) y eso cortocircuitaba la comparacion de bajas: una pelea de 1-por-3
    # se etiquetaba "Engage que rindio". Medido sobre 540 decisiones: 73 (14%)
    # tenian MAS bajas aliadas que enemigas, y el desacuerdo del jugador escalaba
    # con lo malo del intercambio — 3% con ratio favorable, 17% en empate, 30%
    # ahi. En sus palabras: "una kill por dos muertes no se me hace que rindio".
    # Ahora la kill personal solo cuenta si el intercambio no sale perdiendo; el
    # empate se conserva porque tu aporte (peel/CC) pudo sostenerlo.
    # Que la decision fuera correcta pese al mal resultado es lo que expresa el
    # eje de opciones, no esta etiqueta.
    exchange_not_behind = facts.enemy_deaths >= facts.ally_deaths
    team_won_fight = facts.enemy_deaths > facts.ally_deaths and facts.enemy_deaths >= 1
    if facts.sacrifice_obj:
        action_class = "calculated_sacrifice"
    elif team_won_fight or (facts.got_value and exchange_not_behind):
        action_class = "engage_worth"
    elif facts.ally_died_first:
        action_class = "support_lost"
    elif skirmish:
        action_class = "trade_lost"
    elif facts.retreating:
        action_class = "disengage_failed"
    else:
        action_class = "engage_blind"

    options = _build_options(
        facts.fighters_for, facts.fighters_against, facts.non_combatant_unseen,
        action_class, facts.sacrifice_obj, setup_score(facts.state_features))

    death_x, death_y = facts.death_pos
    context = {
        "time_mmss": mmss(facts.death_ms),
        "lane": facts.lane,
        "map_side": facts.map_side,
        "death_position": {"x": death_x, "y": death_y},
        "killer": facts.killer,
        "assisters": facts.assisters,
        "first_blood": facts.first_blood,
        "score_at_moment": {"our_kills": facts.score_our_kills, "enemy_kills": facts.score_enemy_kills},
        "our_state": facts.our_state,
        "nearest_ally": {
            "champion": facts.nearest_ally_champ,
            "distance_units": facts.nearest_ally_dist,
            "within_help_range": facts.we_had_help,
        },
        "fight_ratio_apparent": f"{facts.fighters_for}v{facts.fighters_against}",
        "non_combatant_enemies_unseen": [
            {"champion": c, "unseen_s": int(s)} for c, s in facts.non_combatant_unseen
        ],
        # Killer/assisters that came out of fog: unseen_s is measured at the
        # pre-fight cutoff (what you could know when committing), not at death.
        "combatant_enemies_unseen": [
            {"champion": c, "unseen_s": int(s)} for c, s in facts.combatant_unseen
        ],
        "top_damager": facts.top_damager,
        "fight_outcome_around_death": f"{facts.enemy_deaths}-{facts.ally_deaths}",
        "sacrifice_objective": facts.sacrifice_obj,
        "ally_died_first": facts.ally_died_first,
        "inferred_action_class": action_class,
        "state_features": facts.state_features,
    }

    # Mismo orden que las opciones de _build_options. disengage_failed ofrecia
    # [disengage, disengage] -- "desenganchar antes" contra "te retiraste tarde",
    # el mismo eje dicho dos veces -- y pasa al eje luchar/huir; es la unica rama
    # cuyo fingerprint cambia (sus marcas quedan historicas, #23 Fase C).
    if action_class in ("trade_lost", "support_lost"):
        aids = [ACTION_DISENGAGE, ACTION_FIGHT_ENTRY]
    else:  # engage_worth / calculated_sacrifice / engage_blind / disengage_failed
        aids = [ACTION_FIGHT_ENTRY, ACTION_DISENGAGE]
    context["action"] = build_action(DETECTOR_ID, options, aids)

    argument = _compose_argument(context, options)

    return Decision(
        detector_id=DETECTOR_ID,
        match_id=facts.match_id,
        game_time_ms=facts.death_ms,
        moment=f"Muerte en {facts.lane} ({facts.map_side}) a manos de {facts.killer}",
        outcome=(
            f"Moriste {mmss(facts.death_ms)}. "
            f"Pelea efectiva {context['fight_ratio_apparent']}. "
            + ("Primera sangre. " if facts.first_blood else "")
            + (f"Top damage: {facts.top_damager['champion']} ({facts.top_damager['damage']})." if facts.top_damager else "")
        ),
        context=context,
        options=options,
        argument=argument,
        clip_path=facts.clip_path,
        clip_offset_s=facts.clip_offset_s,
    )


def _build_options(
    fighters_for: int,
    fighters_against: int,
    non_combatant_unseen: list[tuple[str, float]],
    action_class: str = "engage_blind",
    sacrifice_obj: dict | None = None,
    setup: float | None = None,
) -> list[Option]:
    """Un eje binario real por situacion (#23 Fase C), formulado desde el contexto
    PREVIO a la jugada: nunca describe el desenlace ni se autoetiqueta "(lo que
    hiciste)" -- eso lo lleva el flag `taken`. Los ejes son los del jugador:

      pelea (engage_blind, engage_worth, trade_lost, disengage_failed): luchar / huir
      apoyo (support_lost): intentar salvar la pelea / no sumar tu muerte
      objetivo (calculated_sacrifice): llevarte el objetivo aunque mueras / salir vivo

    La rama solo decide el EJE y cual se tomo; el EV sale del setup local. El
    ORDEN de cada rama es el que ya tenia, para que el fingerprint (action_ids +
    indice tomado) de las marcas existentes no cambie; la unica excepcion es
    disengage_failed, que ofrecia `disengage` dos veces y no tenia eje."""
    s = 0.5 if setup is None else setup
    ev_fight = round(EV_FLOOR + EV_SPAN * s, 2)
    ev_flee = round(EV_FLOOR + EV_SPAN * (1.0 - s), 2)
    setup_desc = ("sin lectura de setup" if setup is None
                  else f"setup local {round(setup * 100)}%")
    unseen_desc = (
        " Enemigos sin ver cerca: " + ", ".join(c for c, _ in non_combatant_unseen) + "."
        if non_combatant_unseen else ""
    )
    fight_cons = (f"Pelea aparente {fighters_for}v{fighters_against}, {setup_desc}."
                  + unseen_desc)

    if action_class == "calculated_sacrifice":
        obj = (sacrifice_obj or {}).get("name", "el objetivo")
        return [
            Option(
                label=f"Llevarte {obj} aunque te cueste la vida",
                predicted_consequence=(f"Aseguras {obj} y aceptas el tiempo muerto. "
                                       + fight_cons),
                ev_score=ev_fight,
                taken=True,
            ),
            Option(
                label=f"Salir vivo y ceder {obj}",
                predicted_consequence=f"Conservas tu vida y tu presencia en el mapa; {obj} queda para el rival.",
                ev_score=ev_flee,
            ),
        ]

    if action_class == "support_lost":
        return [
            Option(
                label="No entrar: no sumar una segunda muerte",
                predicted_consequence="Cedes la pelea que ya empezó y conservas tu vida para la siguiente jugada.",
                ev_score=ev_flee,
            ),
            Option(
                label="Entrar a salvar la pelea",
                predicted_consequence="Un aliado ya había caído: entras en inferioridad a intentar darle la vuelta. " + fight_cons,
                ev_score=ev_fight,
                taken=True,
            ),
        ]

    fight = Option(
        label="Luchar: quedarte y pelear",
        predicted_consequence=fight_cons,
        ev_score=ev_fight,
        taken=action_class != "disengage_failed",
    )
    flee = Option(
        label="Huir: salir antes de comprometerte",
        predicted_consequence="Sueltas farm, oleada o posición a cambio de salir vivo.",
        ev_score=ev_flee,
        taken=action_class == "disengage_failed",
    )
    if action_class == "trade_lost":
        return [flee, fight]      # orden historico: [disengage, fight_entry]
    return [fight, flee]          # engage_blind / engage_worth / disengage_failed


def _compose_argument(ctx: dict, options: list[Option]) -> str:
    lines: list[str] = []

    headline = (
        f"A los {ctx['time_mmss']} moriste en {ctx['lane']} ({ctx['map_side']}) "
        f"a manos de {ctx['killer']}"
    )
    if ctx["assisters"]:
        headline += f" con asistencia de {', '.join(ctx['assisters'])}"
    headline += "."
    if ctx["first_blood"]:
        headline += " (Primera sangre.)"
    lines.append(headline)

    s = ctx["our_state"]
    lines.append(
        f"Tu estado: nivel {s['level']}, oro disponible {s['current_gold']}, CS {s['cs']}, "
        f"KDA acumulado {s['kills_so_far']}/{s['deaths_so_far']}/{s['assists_so_far']}."
    )

    na = ctx["nearest_ally"]
    if na["champion"] is None:
        lines.append("No hay info de aliados cercanos (frame inicial del juego).")
    else:
        proximity = (
            "cerca y útil"
            if na["within_help_range"]
            else "demasiado lejos como para ayudar"
        )
        lines.append(
            f"Aliado más cercano: {na['champion']} a {na['distance_units']} unidades ({proximity})."
        )

    lines.append(f"Pelea efectiva al momento: {ctx['fight_ratio_apparent']}.")

    if ctx.get("sacrifice_objective"):
        so = ctx["sacrifice_objective"]
        lines.append(
            # Hecho, no veredicto: si la vida valia el objetivo es justo lo que
            # pregunta el eje de opciones (#23 Fase C). Antes cerraba con "tu
            # muerte compró ese objetivo, no fue una entrega en seco".
            f"Tu equipo se llevó {so['name']} justo aquí ({so['rel_s']:+.0f}s respecto a tu "
            f"muerte)."
        )

    if ctx.get("combatant_enemies_unseen"):
        unseen = ", ".join(
            f"{e['champion']} ({e['unseen_s']}s sin verse)"
            for e in ctx["combatant_enemies_unseen"]
        )
        lines.append(
            f"Te mataron llegando desde la niebla: {unseen} no había aparecido "
            f"en visión antes de la pelea. Con esa información pendiente, el "
            f"compromiso era a ciegas aunque el resto pareciera controlado."
        )

    if ctx["non_combatant_enemies_unseen"]:
        unseen = ", ".join(
            f"{e['champion']} ({e['unseen_s']}s sin verse)"
            for e in ctx["non_combatant_enemies_unseen"]
        )
        lines.append(
            f"Enemigos fuera del fight pero sin visibilidad reciente: {unseen}. "
            f"Si alguno estaba rotando hacia ti, la situación era peor que el "
            f"{ctx['fight_ratio_apparent']} aparente."
        )

    if ctx.get("top_damager"):
        td = ctx["top_damager"]
        lines.append(f"Quien más daño te hizo: {td['champion']} ({td['damage']} de daño).")

    if ctx.get("inferred_action_class") == "disengage_failed":
        # El eje luchar/huir no expresa CUANDO saliste, y en esta clase es la mitad
        # de la leccion (la vieja opcion "desenganchar antes" era eso). Va como
        # lectura de la situacion, sin decir cual de los dos lados era el bueno.
        lines.append(
            "Ibas de salida cuando te alcanzaron: además de luchar o huir, cuenta "
            "en qué momento sales."
        )

    # Compare what you did with the recommended (highest EV). La opcion tomada sale
    # del flag explicito (features.taken_index), no de buscar "hiciste" en el label:
    # ese lookup por texto ataba la prosa del argumento a la redaccion de la opcion.
    _ti = taken_index(options)
    taken = options[_ti] if _ti is not None else None
    best = max(options, key=lambda o: o.ev_score) if options else None
    # Empate: max() se queda con la PRIMERA opcion, asi que sin esta guarda el
    # veredicto de un eje parejo dependia del orden de la rama ("fue acertada" si
    # la tomada iba primero, "no marcan alternativa" si iba segunda).
    tied = (taken is not None and best is not None
            and sum(1 for o in options if o.ev_score == best.ev_score) > 1
            and taken.ev_score == best.ev_score)
    if tied:
        lines.append(
            f"Las heurísticas no marcan una alternativa claramente superior "
            f"(mejor EV {best.ev_score:.2f} vs lo que hiciste {taken.ev_score:.2f})."
        )
    elif taken and best and best.label != taken.label and best.ev_score > taken.ev_score:
        lines.append(
            f"Opción de mayor EV: \"{best.label}\" (EV {best.ev_score:.2f}) "
            f"vs lo que hiciste (EV {taken.ev_score:.2f}). "
            f"Diferencial {best.ev_score - taken.ev_score:+.2f}."
        )
    elif taken and best and best.label == taken.label:
        lines.append(
            f"Lo que hiciste ES la opción de mayor EV ({taken.ev_score:.2f}): la "
            f"decisión fue acertada; el resultado se explica por otros factores."
        )
    elif taken and best:
        lines.append(
            f"Las heurísticas no marcan una alternativa claramente superior "
            f"(mejor EV {best.ev_score:.2f} vs lo que hiciste {taken.ev_score:.2f})."
        )

    return " ".join(lines)
