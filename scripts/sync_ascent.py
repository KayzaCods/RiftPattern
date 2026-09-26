"""One command for the whole Ascent flow.

Replaces the manual chain:
  ingest_ascent.py -> ingest_ascent_events.py -> analyze.py <m>
  -> compute_reward.py <m> -> complete_clips.py

Steps:
  1. Ingest Ascent recordings: link clips, and pull any match not yet in the DB
     straight from Riot (no recorder session needed).
  2. For each Ascent match (new ones by default, or all with --all): parse its
     input log into input_events, run all detectors, persist, compute rewards.
  3. Normalize full replays and (re)assign clips to decisions.

Usage:
    .venv\\Scripts\\python.exe scripts\\sync_ascent.py          # new Ascent matches
    .venv\\Scripts\\python.exe scripts\\sync_ascent.py --all    # reprocess all
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root
sys.path.insert(0, str(Path(__file__).resolve().parent))          # sibling scripts

from lol_coach.accounts import load_accounts
from lol_coach.config import load_config
from lol_coach.db import connect
from lol_coach.riot_api import RiotAPI

import analyze
import complete_clips
import ingest_ascent
import ingest_ascent_events

ASCENT_SOURCE = "ascent_full"
log = logging.getLogger("sync_ascent")


def _ascent_match_ids(conn, only_new: bool) -> list[str]:
    """Ascent matches to process. "New" = the analyzed_at_utc marker is unset,
    which (unlike the old "has no decisions" test) repairs matches left half-done
    by a crash and doesn't reprocess 0-decision matches forever."""
    q = ("SELECT DISTINCT c.match_id FROM clips c JOIN matches m ON m.match_id = c.match_id "
         "WHERE c.source = ? AND c.match_id IS NOT NULL")
    if only_new:
        q += " AND m.analyzed_at_utc IS NULL"
    return [r["match_id"] for r in conn.execute(q, (ASCENT_SOURCE,))]


def run_sync(*, all_flag: bool = False) -> dict:
    """Run the pipeline and return counts plus sanitized stage failures.

    ``ingested`` counts successfully analyzed matches (the legacy status meaning).
    Recoverable stage errors produce ``partial``; an interrupted flow is ``failed``.
    Exception text and Riot samples are deliberately excluded from this result.
    """
    result = {"state": "failed", "ingested": 0, "decisions": 0, "errors": []}
    conn = None
    stage = "config"
    try:
        cfg = load_config()
        paths = cfg["paths"]
        if not paths.get("ascent_recordings_db"):
            print("config.toml [paths].ascent_recordings_db no está configurado.")
            result["errors"].append({"stage": "config", "code": "ascent_not_configured", "count": 1})
            return result
        ascent_db = Path(paths["ascent_recordings_db"])
        ascent_dir = Path(paths["ascent_dir"]) if paths.get("ascent_dir") else None
        raw_dir = Path(paths["data_raw"])
        stage = "database"
        conn = connect(paths["sqlite_db"])

        # Riot API lets step 1 pull matches with no recorder session; optional.
        stage = "riot_setup"
        api = accounts = None
        try:
            api = RiotAPI(cfg["riot"]["api_key"], cfg["riot"]["routing"], cfg["riot"]["platform"])
            accounts = load_accounts(cfg, api, raw_dir.parent)
        except Exception as e:
            api = accounts = None
            log.warning("Sin Riot API (%s): solo proceso partidas ya en la DB.", e)

        if not api or not accounts:
            result["errors"].append({"stage": "riot_setup", "code": "riot_unavailable", "count": 1})

        stage = "recorder"
        print("=" * 70)
        print("0) Ingest del recorder (sesiones Live Client 1Hz -> snapshots HP/maná)")
        # Reconnected 2026-06-09: the recorder kept recording but nothing ingested its
        # sessions since sync_ascent replaced analyze_recent — trade_v1 and HP/mana were
        # silently frozen at the 27 old matches. A session newly linked to an already-
        # analyzed match clears its marker so the analysis step re-runs it (trade_v1
        # fires now that snapshots exist; persist_decisions keeps the feedback).
        if api and accounts:
            from lol_coach.ingest import run_ingest
            linked_before = {r["match_id"] for r in conn.execute(
                "SELECT match_id FROM sessions WHERE match_id IS NOT NULL")}
            replays_dirs = [Path(p) for p in
                            (paths.get("replays_lol_dirs") or [paths.get("replays_lol_dir")]) if p]
            outplayed = Path(paths["outplayed_dir"]) if paths.get("outplayed_dir") else None
            try:
                s0 = run_ingest(conn, api, raw_dir, accounts, replays_dirs, outplayed)
                print(f"   sesiones={s0['sessions_seen']}  nuevas={s0['sessions_new']}  "
                      f"partidas añadidas={s0['matches_added']}")
                if s0.get("errors"):
                    result["errors"].append({"stage": "recorder", "code": "recorder_errors",
                                             "count": len(s0["errors"])})
                newly_linked = {r["match_id"] for r in conn.execute(
                    "SELECT match_id FROM sessions WHERE match_id IS NOT NULL")} - linked_before
                if newly_linked:
                    qmarks = ",".join("?" * len(newly_linked))
                    conn.execute(f"UPDATE matches SET analyzed_at_utc=NULL WHERE match_id IN ({qmarks})",
                                 tuple(newly_linked))
                    conn.commit()
                    print(f"   {len(newly_linked)} partida(s) re-marcadas para análisis (ya tienen snapshots 1Hz)")
            except Exception as e:
                result["errors"].append({"stage": "recorder", "code": "recorder_failed", "count": 1})
                log.warning("run_ingest fallo (%s): sigo con Ascent.", e)
        else:
            print("   (sin Riot API: se salta; las sesiones quedan para la próxima corrida)")

        print()
        print("1) Ingest de grabaciones Ascent (vincula clips + trae partidas de Riot)")
        stage = "ascent"
        s1 = ingest_ascent.ingest_ascent(conn, ascent_db, ascent_dir, api, accounts, raw_dir)
        print(f"   grabaciones={s1['recordings']}  traídas de Riot={s1['matches_pulled']}  "
              f"vinculadas={s1['linked']}  sin match={s1['unlinked']}")
        if s1.get("source_error"):
            result["errors"].append({"stage": "ascent", "code": s1["source_error"], "count": 1})
        # Un fallo de Riot tiene que verse DONDE se mira. Sin esto, nueve dias de
        # ingesta rota se leian como "traídas de Riot=0", que parece "no jugaste".
        if s1.get("riot_errors"):
            result["errors"].append({"stage": "ascent", "code": "riot_errors",
                                     "count": s1["riot_errors"]})
            print(f"   *** {s1['riot_errors']} llamada(s) a Riot fallaron al importar "
                  "desde Ascent; la sincronización tiene errores parciales")
            print(f"       {s1.get('riot_error_sample')}")

        # Analysis covers EVERY pending match (recorder-only, Ascent, reference) — the
        # analyzed_at marker decides, not the clip source. Ascent matches get their
        # input events ingested first.
        stage = "analysis"
        if all_flag:
            targets = [r["match_id"] for r in conn.execute(
                "SELECT match_id FROM matches ORDER BY game_start_ms")]
        else:
            targets = [r["match_id"] for r in conn.execute(
                "SELECT match_id FROM matches WHERE analyzed_at_utc IS NULL ORDER BY game_start_ms")]
        ascent_mids = set(_ascent_match_ids(conn, only_new=False))
        print()
        print(f"2) Input events + análisis — {len(targets)} partida(s) "
              f"({'todas' if all_flag else 'pendientes'})")
        if not targets:
            print("   Nada pendiente (usa --all para reprocesar todo).")
        for mid in targets:
            if mid in ascent_mids:
                ingest_ascent_events.ingest_events_for_db(conn, ascent_db, only_match=mid)
            n = analyze.analyze_match(conn, mid)  # detectors + reward + analyzed_at marker
            result["ingested"] += 1
            result["decisions"] += n
            print(f"   {mid}: {n} decisiones + reward")

        print()
        print("3) Clips (normaliza full replays + asigna a decisiones)")
        stage = "clips"
        nfull = complete_clips.normalize_full_replays(conn)
        res = complete_clips.complete_clips_for_decisions(conn)
        print(f"   full normalizados={nfull}  |  {res['short']} por jugada / "
              f"{res['full']} por replay / {res['none']} sin clip")

        print()
        result["state"] = "partial" if result["errors"] else "completed"
    except Exception as exc:
        result["errors"].append({"stage": stage, "code": "stage_failed", "count": 1})
        print(f"FALLO en etapa {stage} ({type(exc).__name__}); la sincronización no se completó.")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                result["state"] = "failed"
                result["errors"].append({"stage": "database", "code": "close_failed", "count": 1})
    label = {"completed": "LISTO", "partial": "PARCIAL", "failed": "FALLO"}[result["state"]]
    print(f"{label}. {result['ingested']} partida(s), {result['decisions']} decisiones.")
    return result


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    result = run_sync(all_flag="--all" in sys.argv)
    return {"completed": 0, "partial": 2, "failed": 1}[result["state"]]


if __name__ == "__main__":
    sys.exit(main())
