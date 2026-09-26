"""Engine and invariant tests for the selective trade_v1 PRE regeneration.

Everything here is synthetic: DBs under tmp_path built with the production
schema (`db.connect`), timelines/snapshots/game_data written by the test, and
no config, real DB, raw data, session dir or network.

How a LEGACY row is crafted: exactly what the pre-827bd07 detector persisted —
the scanner's candidate, `_gather_trade_facts` with the world state at the
NOMINAL minute N (not the block opening B), `_evaluate`, and no `pre_temporal`
key. Frames sit at i*60000+190 ms, so B = N+190 and the legacy anchor differs.
Enemy spendable gold flips between consecutive frames, so the PRE at B reads a
genuinely different power than the one at N and the EVs move.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from conftest import damage_frame, insert_event, set_timeline_json, ten_player_match
from lol_coach import db as dbmod
from lol_coach import game_data as GD
from lol_coach import trade_pre_regeneration as trp
from lol_coach.analysis import insert_decision
from lol_coach.context import build_world_state
from lol_coach.decisions import trade as TR

OPEN_OFFSET_MS = 190
FRAMES = 16
MARK = dict(user_feedback="disagree", user_feedback_note="no era un trade",
            user_feedback_at_utc="2026-08-10T00:00:00+00:00", user_best_option=0,
            user_feedback_fingerprint="fp", user_mark_type="decision")


def _frame_ts(i):
    return i * 60_000 + (OPEN_OFFSET_MS if i else 0)


def _has_resource(champion):
    return True


@dataclass
class World:
    root: Path
    source: Path
    game_data: Path
    keys_s: list
    keyset: str
    ids: dict = field(default_factory=dict)          # key -> decision id
    marked: list = field(default_factory=list)       # ids of marked trade rows
    sessions: dict = field(default_factory=dict)     # match_id -> session dir


def _match(conn, root, mid, deltas, *, flip=True):
    """A bot-lane match whose only exchanges are `deltas` {frame: (us, enemy)}.

    Enemy economy alternates between odd and even frames, so the PRE at N (odd
    frame i-1) and at B (even frame i) read different power. With `flip` the
    even frames also give us a level lead: the recommendation itself changes.
    """
    ten_player_match(conn, mid, frame_times=[(i, _frame_ts(i)) for i in range(FRAMES)])
    for pid, x in ((1, 10000.0), (5, 11000.0)):
        conn.execute("UPDATE timeline_frames SET position_x=?, position_y=? "
                     "WHERE match_id=? AND participant_id=?", (x, 4000.0, mid, pid))
    conn.execute("UPDATE timeline_frames SET current_gold=2000 WHERE match_id=? "
                 "AND participant_id <= 5", (mid,))
    conn.execute("UPDATE timeline_frames SET current_gold = CASE WHEN frame_index % 2 = 1 "
                 "THEN 3500 ELSE ? END WHERE match_id=? AND participant_id > 5",
                 (0 if flip else 1500, mid))
    if flip:
        conn.execute("UPDATE timeline_frames SET level = 9 WHERE match_id=? "
                     "AND participant_id > 5 AND frame_index % 2 = 0", (mid,))
    tdir = root / "raw"
    tdir.mkdir(exist_ok=True)
    set_timeline_json(conn, mid, tdir / f"{mid}.json", damage_frames(deltas))


def damage_frames(deltas):
    frames, us, en = [], 0, 0
    for i in range(FRAMES):
        d_us, d_en = deltas.get(i, (0, 0))
        us, en = us + d_us, en + d_en
        frames.append(damage_frame(timestamp=_frame_ts(i), **{"1": us, "6": en}))
    return frames


def rewrite_timeline(world, mid, deltas):
    path = world.root / "raw" / f"{mid}.json"
    path.write_text(json.dumps({"info": {"frames": damage_frames(deltas)}}), encoding="utf-8")


def _session(conn, root, mid, lines):
    sdir = root / "sessions" / mid
    sdir.mkdir(parents=True)
    (sdir / "snapshots.jsonl").write_text("".join(lines), encoding="utf-8")
    conn.execute("INSERT INTO sessions (match_id, session_dir, start_utc, ingested_at_utc) "
                 "VALUES (?,?,?,?)", (mid, str(sdir), "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
    return sdir


def _snap(gt, hp, mana, **extra):
    return json.dumps({"data": {"gameData": {"gameTime": gt, "mapName": "SR"},
                                "activePlayer": {"championStats": {
                                    "currentHealth": hp, "maxHealth": 1000,
                                    "resourceValue": mana, "resourceMax": 1000,
                                    "attackDamage": 60}, "level": 9},
                                "allPlayers": [{"items": [1, 2]}], **extra}}) + "\n"


def legacy_decisions(conn, mid, *, reward=True):
    """What the pre-827bd07 detector would have persisted for this match."""
    inv = TR._collect_trade_candidates(conn, mid, has_resource=_has_resource)
    out = []
    for c in inv.candidates:
        ws_old = build_world_state(conn, mid, (c.m_start - 1) * 60_000)
        facts = TR._gather_trade_facts(conn, mid, inv.us_id, inv.our_team, inv.parts,
                                       inv.my_duo, c.m_start, c.m_end, c.kind, c.d_us, c.d_en,
                                       c.stats, TR.TradePre("available", state=ws_old))
        d = TR._evaluate(facts)
        del d.context["pre_temporal"]
        if reward:
            d.context["reward"] = {"reward_score": 0.25, "window_s": 60, "note": "ñ"}
        out.append(d)
    return out


def current_decisions(conn, mid):
    """What the current (versioned) detector emits, without the checkout game data."""
    inv = TR._collect_trade_candidates(conn, mid, has_resource=_has_resource)
    out = []
    for c in inv.candidates:
        pre = TR._resolve_trade_pre(conn, mid, inv.frames, c.m_start, c.m_end)
        facts = TR._gather_trade_facts(conn, mid, inv.us_id, inv.our_team, inv.parts,
                                       inv.my_duo, c.m_start, c.m_end, c.kind, c.d_us, c.d_en,
                                       c.stats, pre)
        out.append(TR._evaluate(facts))
    return out


def _key(d):
    return (d.match_id, d.detector_id, d.game_time_ms)


def build_world(root: Path, *, shared=1) -> World:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    source = root / "source.db"
    gd = root / "game_data"
    gd.mkdir()
    (gd / "champion_stats_meraki.json").write_text(
        json.dumps({"champions": {"Jinx": {"resource": "MANA"}}}), encoding="utf-8")
    conn = dbmod.connect(source)
    world = World(root, source, gd, [], "")
    try:
        # M_A: one authorized lost trade in a match without feedback, with snapshots.
        _match(conn, root, "M_A", {9: (600, 200)})
        world.sessions["M_A"] = _session(conn, root, "M_A", [
            _snap(530.0, 700, 900), _snap(540.0, 400, 600),
            json.dumps({"data": {"gameData": {"gameTime": "late"}}}) + "\n",
            json.dumps({"data": {"gameData": {"gameTime": True}, "activePlayer": None}}) + "\n",
            json.dumps({"data": {}}) + "\n"])
        # Partner death right after the run: a stored POST consequence to preserve.
        insert_event(conn, "M_A", 560_000, "CHAMPION_KILL", {"victimId": 5, "killerId": 6})
        # Shared matches: an authorized run and a MARKED run of the same match.
        for n in range(1, shared + 1):
            _match(conn, root, f"M_S{n}", {5: (650, 200), 9: (700, 250)}, flip=n % 2 == 1)
        # M_V: already versioned by the current detector -> never in S.
        _match(conn, root, "M_V", {9: (600, 200)})
        conn.commit()

        for mid in ["M_A"] + [f"M_S{n}" for n in range(1, shared + 1)]:
            for d in legacy_decisions(conn, mid):
                world.ids[_key(d)] = insert_decision(conn, d)
        for d in current_decisions(conn, "M_V"):
            world.ids[_key(d)] = insert_decision(conn, d)
        for n in range(1, shared + 1):
            rid = world.ids[(f"M_S{n}", "trade_v1", 240_000)]
            conn.execute(
                "UPDATE decisions SET " + ", ".join(f"{c}=?" for c in MARK) + " WHERE id=?",
                (*MARK.values(), rid))
            world.marked.append(rid)
        # A non-trade decision with hand-formatted JSON, clip and feedback.
        cur = conn.execute(
            "INSERT INTO decisions (match_id, detector_id, game_time_ms, moment, outcome, "
            "context_json, options_json, argument, clip_path, clip_offset_s, user_feedback, "
            "created_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("M_A", "death_v1", 300_000, "Muerte", "", '{"x":1,  "y":[1.50]}', "[]", "arg",
             "C:/clips/a.mp4", 12.5, "agree", "2026-07-01T00:00:00+00:00"))
        world.ids[("M_A", "death_v1", 300_000)] = cur.lastrowid
        # Human signal and labels elsewhere; one preference with PRE-EXISTING orphans.
        marked_key = f"M_S1|trade_v1|240000"
        conn.execute("INSERT INTO preferences (detector_id, decision_a_id, decision_b_id, winner, "
                     "note, created_at_utc, a_key, b_key) VALUES (?,?,?,?,?,?,?,?)",
                     ("trade_v1", world.marked[0], 999_999, "a", "", "2026-08-01", marked_key,
                      "GONE|trade_v1|60000"))
        conn.execute("INSERT INTO orphan_feedback (match_id, detector_id, game_time_ms, "
                     "user_feedback, dropped_at_utc, reason) VALUES (?,?,?,?,?,?)",
                     ("M_A", "trade_v1", 60_000, "disagree", "2026-06-01", "key_not_reemitted"))
        conn.execute("INSERT INTO exposure_anchors VALUES (?,?,?)",
                     ("M_A|death_v1|300000", "yes", "2026-07-22"))
        conn.execute("INSERT INTO clips (path, source, match_id, in_match_start_s, "
                     "discovered_at_utc) VALUES (?,?,?,?,?)",
                     ("C:/clips/a.mp4", "ascent", "M_A", 3.25, "2026-07-01"))
        conn.commit()
    finally:
        conn.close()
    world.keys_s = sorted([("M_A", "trade_v1", 480_000)]
                          + [(f"M_S{n}", "trade_v1", 480_000) for n in range(1, shared + 1)])
    world.keyset = trp.keyset_digest(world.keys_s)
    return world


def prepare_world(world: World, name="ev", **kw) -> trp.PrepareResult:
    return trp.prepare(world.source, world.root / name, expected_keyset_sha256=world.keyset,
                       game_data_dir=world.game_data, **kw)


# ----------------------------------------------------------------- helpers

def full_rows(path) -> tuple[list, dict]:
    c = trp.connect_ro(Path(path))
    try:
        cols = trp.table_columns(c, "decisions")
        return cols, {r["id"]: tuple(r) for r in c.execute("SELECT * FROM decisions")}
    finally:
        c.close()


def digest_of(path) -> str:
    c = trp.connect_ro(Path(path))
    try:
        return trp.state_digest(trp.logical_state(c))
    finally:
        c.close()


def load(res) -> trp.Bundle:
    return trp.load_bundle(res.bundle_dir, res.manifest_sha256, res.bundle_sha256)


def run_engine(res, engine=trp.apply_batch, **kw):
    bundle = load(res)
    conn = trp.connect_rw_existing(res.trial_path)
    try:
        return engine(conn, bundle, **kw)
    finally:
        conn.close()


def edit(path, sql, params=()):
    c = sqlite3.connect(path)
    try:
        c.execute(sql, params)
        c.commit()
    finally:
        c.close()


def audit_file(world, keys, name="audit.json"):
    records = [{"key": list(k), "provenance": "legacy_reconstructed", "feedback": False,
                "anchor": "different", "baseline_verified": True} for k in keys]
    records.append({"key": ["M_V", "trade_v1", 480000], "provenance": "versioned",
                    "feedback": False, "anchor": "different", "baseline_verified": True})
    path = world.root / name
    path.write_bytes(json.dumps({"decisions": records}).encode("utf-8"))
    return path, trp.sha256_hex(path.read_bytes())


def prepare_audit(world, keys, name="ev", **kw):
    path, sha = audit_file(world, keys)
    return trp.prepare(world.source, world.root / name,
                       expected_keyset_sha256=trp.keyset_digest(keys),
                       game_data_dir=world.game_data, audit_results=path,
                       audit_results_sha256=sha, **kw)


def abort_code(excinfo):
    return excinfo.value.code


@pytest.fixture
def world(tmp_path):
    return build_world(tmp_path / "w")


@pytest.fixture
def prepared(world):
    return world, prepare_world(world)


# ============================================================ pure encodings

def test_keyset_digest_matches_the_documented_encoding():
    keys = [("M_B", "trade_v1", 60000), ("M_A", "trade_v1", 120000), ("M_A", "trade_v1", 9000)]
    expected = trp.sha256_hex(json.dumps(
        [["M_A", "trade_v1", 9000], ["M_A", "trade_v1", 120000], ["M_B", "trade_v1", 60000]],
        ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert trp.keyset_digest(keys) == expected
    assert trp.keyset_digest(list(reversed(keys))) == expected     # never by input/ID order
    for bad in ([keys[0], keys[0]], [("M", "death_v1", 1)], [("M", "trade_v1", True)],
                [("M", "trade_v1", 1.0)], [("", "trade_v1", 1)]):
        with pytest.raises(trp.ValidationAbort):
            trp.keyset_digest(bad)


def test_typed_row_hash_distinguishes_storage_classes_and_keeps_multiplicity():
    tags = {trp.row_digest((v,)) for v in (None, "", 0, 0.0, b"", "0", 1, 1.0, "N")}
    assert len(tags) == 9
    a, b = trp.row_digest(("a",)), trp.row_digest(("b",))
    assert trp.rows_digest([a, a, b]) != trp.rows_digest([a, b, b])
    assert trp.rows_digest([a, b, a]) == trp.rows_digest([a, a, b])     # order-insensitive
    assert trp.rows_digest([a, a])[0] == 2


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', '{"a":NaN}', "[Infinity]", "[-Infinity]",
                                  '[{"x":{"y":1,"y":1}}]', "{bad"])
def test_strict_json_rejects_duplicates_and_non_finite(text):
    with pytest.raises(trp.ValidationAbort) as ei:
        trp.strict_loads(text)
    assert abort_code(ei) == "invalid_json"


def test_snapshot_projection_is_equivalent_for_our_stats(tmp_path):
    lines = [
        _snap(10.0, 900, 800),
        json.dumps({"data": {"gameData": {"gameTime": 20}, "activePlayer": {"championStats": {
            "currentHealth": 1, "maxHealth": 3, "resourceValue": None, "resourceMax": 400}}}}) + "\n",
        json.dumps({"data": {"gameData": {"gameTime": 30.5}, "activePlayer": {}}}) + "\n",
        json.dumps({"data": {"gameData": {"gameTime": 40.0}, "activePlayer": {"championStats": {
            "currentHealth": 5, "maxHealth": 0}}}}) + "\n",
        json.dumps({"data": {"gameData": {"gameTime": True}}}) + "\n",   # bool: kept, like the reader
        json.dumps({"data": {"gameData": {"gameTime": "x"}}}) + "\n",    # dropped by both
        json.dumps({"data": None}) + "\n",
        json.dumps({"other": 1}) + "\n",
        json.dumps({"data": {"gameData": {"gameTime": 15.0}, "activePlayer": {"championStats": {
            "currentHealth": 250, "maxHealth": 1000, "resourceValue": 3, "resourceMax": 12,
            "armor": 1}}, "events": [1]}}) + "\n",
    ]
    orig = tmp_path / "orig"
    orig.mkdir()
    (orig / "snapshots.jsonl").write_text("".join(lines), encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()
    projected = [p for p in (trp._project_snapshot_line(x) for x in lines) if p is not None]
    (proj / "snapshots.jsonl").write_text("".join(p + "\n" for p in projected), encoding="utf-8")
    a = trp.load_snapshots(orig)
    b = trp.load_snapshots(proj)
    assert [t for t, _ in a] == [t for t, _ in b] and len(a) == 6
    for t in [0, 1, 9.9, 10, 14, 15, 17.4, 20, 25, 30.5, 36, 40, 100, 130, 131]:
        for res in (True, False):
            assert TR._our_stats_at(a, t, res) == TR._our_stats_at(b, t, res)


@pytest.mark.parametrize("line", ["{broken\n", "[1, 2]\n",
                                  json.dumps({"data": {"gameData": {"gameTime": 1},
                                                       "activePlayer": "x"}}) + "\n",
                                  json.dumps({"data": {"gameData": {"gameTime": 1},
                                                       "activePlayer": {"championStats": [1]}}}) + "\n"])
def test_snapshot_projection_never_hides_a_reader_crash(line):
    with pytest.raises(trp.ValidationAbort):
        trp._project_snapshot_line(line)


# ======================================================= prepare and payloads

def test_prepare_selects_exactly_the_legacy_unmarked_rows(prepared):
    world, res = prepared
    bundle = load(res)
    assert [tuple(e["key"]) for e in bundle.entries] == world.keys_s
    assert [e["id"] for e in bundle.entries] == [world.ids[k] for k in world.keys_s]
    m = bundle.manifest
    assert m["selection"]["criteria_counts"] == {
        "excluded_human_signal": 1, "excluded_versioned": 1, "selected": 2, "trade_rows": 4}
    assert m["contract_sha"] == trp.CONTRACT_SHA and m["keyset_sha256"] == world.keyset
    assert m["implementation"]["files_sha256"]["lol_coach/trade_pre_regeneration.py"]
    assert m["runtime"]["sqlite"] == sqlite3.sqlite_version
    assert m["source_db"]["logical_sha256"] == m["before_state_sha256"]
    assert m["sources"]["game_data"]["files"]["champions_summary.json"]["status"] == "absent"
    assert m["sources"]["snapshots"]["M_S1"]["status"] == "no_session"
    assert m["sources"]["snapshots"]["M_A"]["projected_lines"] == 3
    analysis = json.loads((res.bundle_dir / "analysis.json").read_text(encoding="utf-8"))
    assert (analysis["pre_different"], analysis["recommendation_changed"]) == (2, 2)
    one = analysis["per_key"][0]
    assert one["old_pre"] == {"target_ms": 480000, "source_frame_ms": [420190]}
    assert one["new_pre"] == {"target_ms": 480190, "opening_frame_index": 8}
    assert one["old"]["argmax"] != one["new"]["argmax"]
    assert {c["path"] for c in one["feature_changes"]} >= {"state_features.power.local_gold_diff"}
    # The evidence dir: immutable bundle plus operational siblings.
    names = sorted(p.name for p in res.evidence_dir.iterdir())
    assert names == ["approval.txt", "bundle", "receipts", "trial-registry.json", "trial.sqlite"]
    assert digest_of(res.trial_path) == bundle.before_digest


def test_authorized_row_changes_only_two_columns_and_allowed_paths(prepared):
    """authorized row in a match without feedback."""
    world, res = prepared
    cols, before = full_rows(world.source)
    assert run_engine(res) == "applied"
    _, after = full_rows(res.trial_path)
    assert before.keys() == after.keys()
    s_ids = {world.ids[k] for k in world.keys_s}
    ci, oi = cols.index("context_json"), cols.index("options_json")
    for rid, row in before.items():
        if rid not in s_ids:
            assert after[rid] == row
            continue
        changed = [c for c, x, y in zip(cols, row, after[rid]) if x != y]
        assert changed == ["context_json", "options_json"]
        old_ctx, new_ctx = json.loads(row[ci]), json.loads(after[rid][ci])
        old_opt, new_opt = json.loads(row[oi]), json.loads(after[rid][oi])
        trp.check_allowed_paths(old_ctx, new_ctx, old_opt, new_opt)
        assert new_ctx["reward"] == old_ctx["reward"] == {"reward_score": 0.25, "window_s": 60,
                                                          "note": "ñ"}
        assert new_ctx["pre_temporal"]["target_ms"] == 480190
        assert list(new_ctx)[list(new_ctx).index("state_features") + 1] == "pre_temporal"
        assert [o["label"] for o in new_opt] == [o["label"] for o in old_opt]
        assert [o["taken"] for o in new_opt] == [o["taken"] for o in old_opt]
        assert new_ctx["action"]["action_id"] == old_ctx["action"]["action_id"]
    ma = json.loads(after[world.ids[("M_A", "trade_v1", 480000)]][ci])
    assert ma["consequence"].startswith("muerte de Thresh")      # POST fact kept


def test_marked_row_in_same_match_is_identical_and_never_evaluated(world, monkeypatch):
    """authorized and marked in the same match."""
    seen = []
    real = TR._evaluate

    def spy(facts):
        seen.append((facts.match_id, facts.t_ms))
        return real(facts)
    monkeypatch.setattr(TR, "_evaluate", spy)
    res = prepare_world(world)
    marked_key = ("M_S1", "trade_v1", 240_000)
    assert (marked_key[0], marked_key[2]) not in seen
    assert set(seen) == {(k[0], k[2]) for k in world.keys_s}
    _, before = full_rows(world.source)
    run_engine(res)
    _, after = full_rows(res.trial_path)
    assert after[world.marked[0]] == before[world.marked[0]]


def test_shared_matches_batch_keeps_every_excluded_row(tmp_path):
    """a batch of authorized rows spread over shared matches."""
    world = build_world(tmp_path / "w", shared=4)
    res = prepare_world(world)
    bundle = load(res)
    ex = bundle.manifest["excluded"]
    # M_A shares with a marked death row; M_S1..4 with a marked trade row each.
    assert ex["counts"]["shared_matches"] == 5
    assert ex["counts"]["authorized_in_shared_matches"] == 5
    assert ex["counts"]["marked_trade"] == 4
    assert ex["counts"]["matches_shared_with_trade_marks"] == 4          # the audited 38-style count
    assert ex["counts"]["authorized_in_matches_with_trade_marks"] == 4   # the audited 99-style count
    assert {m["match_id"] for m in ex["shared_matches"]} == {"M_A", "M_S1", "M_S2", "M_S3", "M_S4"}
    for m in ex["shared_matches"]:
        assert all(tuple(k) in world.keys_s for _, k in m["authorized"])
        assert all(tuple(k) not in world.keys_s for _, k in m["excluded"])
    _, before = full_rows(world.source)
    run_engine(res)
    _, after = full_rows(res.trial_path)
    s_ids = {world.ids[k] for k in world.keys_s}
    assert {r: v for r, v in after.items() if r not in s_ids} == \
           {r: v for r, v in before.items() if r not in s_ids}
    c = trp.connect_ro(res.trial_path)
    try:
        assert trp.excluded_snapshot(c, bundle.entries)["e_sha256"] == ex["e_sha256"]
    finally:
        c.close()


def test_missing_authorized_key_aborts_without_neighbour(world):
    """K missing (no nearest-row substitution)."""
    keys = world.keys_s + [("M_A", "trade_v1", 420_000)]       # the minute before a real row
    with pytest.raises(trp.ValidationAbort) as ei:
        prepare_audit(world, keys)
    assert abort_code(ei) == "authorized_key_missing"
    assert not (world.root / "ev" / "bundle" / "manifest.json").exists()


def test_substituted_id_is_detected(prepared):
    """ID substituted after prepare."""
    world, res = prepared
    rid = world.ids[world.keys_s[0]]
    edit(res.trial_path, "UPDATE decisions SET id=? WHERE id=?", (rid + 1000, rid))
    tampered = digest_of(res.trial_path)
    with pytest.raises(trp.StateMismatch):
        run_engine(res)
    assert digest_of(res.trial_path) == tampered
    bundle = load(res)
    c = trp.connect_ro(res.trial_path)
    try:
        with pytest.raises(trp.ValidationAbort) as ei:
            trp.compute_plan(c, bundle.sources(), world.keys_s,
                             expected_ids={tuple(e["key"]): e["id"] for e in bundle.entries})
    finally:
        c.close()
    assert abort_code(ei) == "id_changed"


def test_duplicate_rows_with_same_key_are_rejected(world):
    """two rows with the same K."""
    rid = world.ids[world.keys_s[0]]
    edit(world.source, "INSERT INTO decisions (match_id, detector_id, game_time_ms, moment, "
         "outcome, context_json, options_json, argument, created_at_utc) SELECT match_id, "
         "detector_id, game_time_ms, moment, outcome, context_json, options_json, argument, "
         "created_at_utc FROM decisions WHERE id=?", (rid,))
    with pytest.raises(trp.ValidationAbort) as ei:
        prepare_world(world)
    assert abort_code(ei) == "duplicate_decision_key"
    with pytest.raises(trp.ValidationAbort) as ei:
        prepare_audit(world, world.keys_s, name="ev2")
    assert abort_code(ei) == "duplicate_decision_key"


def test_duplicate_candidates_with_same_key_are_rejected(world, monkeypatch):
    """two candidates with the same K (before any map is built)."""
    real = TR._collect_trade_candidates

    def doubled(*a, **k):
        inv = real(*a, **k)
        if inv is not None and inv.candidates:
            inv.candidates = inv.candidates + [inv.candidates[-1]]
        return inv
    monkeypatch.setattr(TR, "_collect_trade_candidates", doubled)
    with pytest.raises(trp.ValidationAbort) as ei:
        prepare_world(world)
    assert abort_code(ei) == "duplicate_candidate_key"


def _orphan_count(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT count(*) FROM orphan_feedback").fetchone()[0]
    finally:
        c.close()


def test_vanished_candidate_aborts_and_keeps_the_row(world):
    """an authorized candidate disappears from the inventory."""
    before = digest_of(world.source)
    rewrite_timeline(world, "M_A", {9: (120, 100)})      # below the exchange threshold now
    with pytest.raises(trp.ValidationAbort) as ei:
        prepare_world(world)
    assert abort_code(ei) == "candidate_missing"
    assert digest_of(world.source) == before
    assert _orphan_count(world.source) == 1


def test_pre_abstention_aborts_before_evaluate(world, monkeypatch):
    """an authorized candidate loses its exact PRE."""
    edit(world.source, "DELETE FROM timeline_frames WHERE match_id='M_A' AND frame_index=8 "
         "AND participant_id=3")

    def boom(facts):
        raise AssertionError("_evaluate reached with an unavailable PRE")
    monkeypatch.setattr(TR, "_evaluate", boom)
    with pytest.raises(trp.ValidationAbort) as ei:
        prepare_world(world)
    assert abort_code(ei) == "pre_unavailable"
    assert ei.value.details == {"M_A|trade_v1|480000": "missing_opening_frame"}


def test_new_candidate_in_affected_match_aborts(world):
    """a new unexpected K in an affected match."""
    rewrite_timeline(world, "M_A", {9: (600, 200), 12: (900, 300)})
    with pytest.raises(trp.ValidationAbort) as ei:
        prepare_world(world)
    assert abort_code(ei) == "candidate_new"
    assert ei.value.details == [["M_A", "trade_v1", 660000]]


@pytest.mark.parametrize("change", ["damage", "hp", "snapshots_absent", "merge"])
def test_descriptor_change_aborts(world, change):
    """span, class, damage or final HP/mana of the descriptor changes."""
    if change == "damage":
        rewrite_timeline(world, "M_A", {9: (650, 200)})
    elif change == "hp":
        (world.sessions["M_A"] / "snapshots.jsonl").write_text(
            _snap(540.0, 450, 600), encoding="utf-8")
    elif change == "snapshots_absent":
        (world.sessions["M_A"] / "snapshots.jsonl").unlink()   # never backfilled from the row
    elif change == "merge":
        rewrite_timeline(world, "M_A", {9: (600, 200), 10: (600, 200)})
    with pytest.raises(trp.ValidationAbort) as ei:
        prepare_world(world)
    assert abort_code(ei) == "descriptor_changed"


@pytest.mark.parametrize("sql", [
    "UPDATE decisions SET user_feedback_note='' WHERE id=:id",
    "UPDATE decisions SET user_feedback_at_utc='2026-09-01' WHERE id=:id",
    "UPDATE decisions SET user_mark_type='execution' WHERE id=:id",
    "UPDATE decisions SET user_best_option=0 WHERE id=:id",
    "INSERT INTO preferences (decision_a_id, decision_b_id, winner, created_at_utc) "
    "VALUES (1, :id, 'tie', 'x')",
    "INSERT INTO preferences (decision_a_id, decision_b_id, winner, created_at_utc, a_key) "
    "VALUES (0, 0, 'a', 'x', :key)",
    "INSERT INTO exposure_anchors VALUES (:key, 'yes', 'x')",
    "INSERT INTO vision_proxy_labels VALUES (:key, 'near', 'x')",
    "INSERT INTO vision_sample VALUES (:key, 1, 'x')",
    "INSERT INTO orphan_feedback (match_id, detector_id, game_time_ms, dropped_at_utc, reason) "
    "VALUES ('M_A', 'trade_v1', 480000, 'x', 'y')",
])
def test_new_mark_or_reference_after_prepare_blocks_apply(prepared, sql):
    """new mark/reference after prepare, including an empty note or isolated metadata."""
    world, res = prepared
    k = ("M_A", "trade_v1", 480000)
    c = sqlite3.connect(res.trial_path)
    c.execute(sql, {"id": world.ids[k], "key": trp.key_string(k)})
    c.commit()
    c.close()
    marked = digest_of(res.trial_path)
    with pytest.raises(trp.StateMismatch):
        run_engine(res)
    assert digest_of(res.trial_path) == marked       # S not reduced, signal not moved


def test_mark_present_at_prepare_blocks_the_batch_in_both_routes(world):
    """a mark with an empty note blocks; S is never silently reduced."""
    rid = world.ids[("M_A", "trade_v1", 480000)]
    edit(world.source, "UPDATE decisions SET user_feedback_note='' WHERE id=?", (rid,))
    with pytest.raises(trp.ValidationAbort) as ei:
        prepare_audit(world, world.keys_s)
    assert abort_code(ei) == "human_signal_or_reference"
    with pytest.raises(trp.ValidationAbort) as ei:
        prepare_world(world, name="ev2")                    # reconstructed: 1 key short
    assert abort_code(ei) == "keyset_mismatch"
    rejection = json.loads((world.root / "ev2" / "rejection.json").read_text(encoding="utf-8"))
    assert rejection["found_count"] == len(world.keys_s) - 1
    assert rejection["criteria_counts"]["excluded_human_signal"] == 2
    assert not (world.root / "ev2" / "bundle" / "manifest.json").exists()


def test_keyset_digest_must_match_exactly(world):
    other = trp.keyset_digest([("X", "trade_v1", 1)])
    with pytest.raises(trp.ValidationAbort) as ei:
        trp.prepare(world.source, world.root / "ev", expected_keyset_sha256=other,
                    game_data_dir=world.game_data)
    assert abort_code(ei) == "keyset_mismatch"
    assert (world.root / "ev" / "rejection.json").is_file()


def test_audit_results_route_is_hash_pinned(world):
    res = prepare_audit(world, world.keys_s)
    assert load(res).manifest["selection"]["mode"] == "audit_results"
    path, _ = audit_file(world, world.keys_s, name="a2.json")
    with pytest.raises(trp.ValidationAbort) as ei:
        trp.prepare(world.source, world.root / "ev3", expected_keyset_sha256=world.keyset,
                    game_data_dir=world.game_data, audit_results=path)   # default = real constant
    assert abort_code(ei) == "audit_results_hash_mismatch"


# ============================================================ frozen sources

@pytest.mark.parametrize("tamper", ["timeline", "snapshot", "game_data", "before", "manifest",
                                    "extra_file", "missing_file", "proposals", "empty_dir"])
def test_bundle_tamper_is_rejected(prepared, tamper):
    """a change to any captured source (or bundle member) invalidates it."""
    world, res = prepared
    b = res.bundle_dir
    target = {"timeline": b / "sources/timelines/M_A.json",
              "snapshot": b / "sources/snapshots/M_A/snapshots.jsonl",
              "game_data": b / "sources/game_data/champion_stats_meraki.json",
              "before": b / "before.sqlite", "manifest": b / "manifest.json",
              "proposals": b / "proposals.json"}.get(tamper)
    if tamper == "extra_file":
        (b / "sources/game_data/champions_summary.json").write_text("{}", encoding="utf-8")
    elif tamper == "missing_file":
        (b / "sources/timelines/M_S1.json").unlink()
    elif tamper == "empty_dir":
        (b / "sources" / "hidden").mkdir()
    else:
        data = target.read_bytes()
        target.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    with pytest.raises(trp.ValidationAbort):
        load(res)


def test_capture_detects_a_source_changing_during_capture(world, monkeypatch):
    real = trp._write_new
    victim = world.root / "raw" / "M_A.json"

    def racing(path, data):
        real(path, data)
        if path.name == "M_A.json":
            victim.write_text(victim.read_text(encoding="utf-8") + " ", encoding="utf-8")
    monkeypatch.setattr(trp, "_write_new", racing)
    with pytest.raises(trp.ValidationAbort) as ei:
        prepare_world(world)
    assert abort_code(ei) == "source_changed_during_capture"


def test_capture_detects_an_absent_source_appearing(world, monkeypatch):
    real = trp._write_new

    def appearing(path, data):
        real(path, data)
        if path.name == "champion_stats_meraki.json":
            (world.game_data / "champions_summary.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(trp, "_write_new", appearing)
    with pytest.raises(trp.ValidationAbort) as ei:
        prepare_world(world)
    assert abort_code(ei) == "source_appeared_during_capture"


def test_rehearsal_reads_only_the_bundle(prepared, monkeypatch):
    """after prepare the original sources may change or vanish; the rehearsal
    never falls back to them, nor to the checkout's game data or its cache."""
    world, res = prepared
    shutil.rmtree(world.root / "raw")
    shutil.rmtree(world.root / "sessions")
    shutil.rmtree(world.game_data)
    nowhere = world.root / "nowhere"
    monkeypatch.setattr(GD, "_GAME_DATA_DIR", nowhere)
    poisoned = {"Jinx": {"resource": "NONE"}}                # a stale cache must be ignored
    monkeypatch.setattr(GD, "_meraki", poisoned)
    bundle = load(res)
    c = trp.connect_ro(res.trial_path)
    try:
        plan = trp.compute_plan(c, bundle.sources(), world.keys_s)
    finally:
        c.close()
    assert trp.canonical_json({"keyset_sha256": world.keyset, "proposals": plan.entries}) == \
        (res.bundle_dir / "proposals.json").read_bytes()
    assert GD._GAME_DATA_DIR == nowhere and GD._meraki is poisoned      # restored afterwards


def test_bundle_loader_refuses_paths_outside_the_recorded_ones(prepared):
    world, res = prepared
    tl, sn = load(res).sources().loaders_for("M_A")
    with pytest.raises(trp.ValidationAbort):
        tl(str(world.root / "raw" / "other.json"))
    with pytest.raises(trp.ValidationAbort):
        sn(str(world.root / "elsewhere"))


# ======================================================= transaction behaviour

def test_second_writer_holding_the_lock_means_zero_changes(prepared):
    """interference of a second writer (busy)."""
    world, res = prepared
    other = sqlite3.connect(res.trial_path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(trp.StateMismatch) as ei:
            run_engine(res)
        assert abort_code(ei) == "target_busy"
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert digest_of(res.trial_path) == load(res).before_digest


def test_second_writer_edit_is_detected_and_not_overwritten(prepared):
    """interference of a second writer (baseline differs)."""
    world, res = prepared
    edit(res.trial_path, "UPDATE clips SET in_match_start_s=4.0")
    edited = digest_of(res.trial_path)
    with pytest.raises(trp.StateMismatch) as ei:
        run_engine(res)
    assert abort_code(ei) == "state_mismatch"
    assert ei.value.details["tables_differing_from_before"] == ["clips"]
    assert digest_of(res.trial_path) == edited


@pytest.mark.parametrize("stage,index", [("after_update", 0), ("after_update", 1),
                                         ("before_commit", None)])
def test_fault_rolls_back_the_whole_batch(tmp_path, stage, index):
    """failure after UPDATE #1, mid-batch and right before COMMIT."""
    world = build_world(tmp_path / "w", shared=2)
    res = prepare_world(world)
    before = load(res).before_digest

    def fault(s, i):
        if (s, i) == (stage, index):
            raise RuntimeError("injected")
    with pytest.raises(RuntimeError):
        run_engine(res, fault=fault)
    assert digest_of(res.trial_path) == before          # fresh read-only connection


def test_lost_commit_result_is_resolved_by_verification(prepared):
    """process death around COMMIT: verification tells before from after,
    and a blind re-run is a no-op."""
    world, res = prepared
    bundle = load(res)

    def die(s, i):
        if s == "before_commit":
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        run_engine(res, fault=die)
    c = trp.connect_ro(res.trial_path)
    assert trp.verify_state(c, bundle)[0] == "before"
    c.close()
    assert run_engine(res) == "applied"
    c = trp.connect_ro(res.trial_path)
    assert trp.verify_state(c, bundle)[0] == "after"
    c.close()
    conn = trp.connect_rw_existing(res.trial_path)
    try:
        changes = conn.total_changes
        assert trp.apply_batch(conn, bundle) == "already_applied"
        assert conn.total_changes == changes
    finally:
        conn.close()


@pytest.mark.parametrize("ddl,code", [
    ("CREATE TRIGGER t AFTER UPDATE ON decisions BEGIN SELECT 1; END", "trigger_present"),
    ("ALTER TABLE decisions ADD COLUMN user_extra TEXT", "unknown_user_columns"),
    ("CREATE TABLE notes (decision_id INTEGER, txt TEXT)", "unknown_reference_column"),
    ("CREATE TABLE labels2 (label_key TEXT)", "unknown_reference_column"),
    ("CREATE TABLE copy_fb (match_id TEXT, detector_id TEXT, game_time_ms INTEGER)",
     "unknown_reference_column"),
])
def test_preflight_rejects_unknown_schema_at_prepare(world, ddl, code):
    """trigger, schema or unknown column -> preflight fails."""
    edit(world.source, ddl)
    with pytest.raises(trp.ValidationAbort) as ei:
        prepare_world(world)
    assert abort_code(ei) == code


@pytest.mark.parametrize("ddl", [
    "CREATE TRIGGER t AFTER UPDATE ON decisions BEGIN UPDATE clips SET source='x'; END",
    "ALTER TABLE decisions ADD COLUMN extra TEXT",
    "CREATE INDEX ix ON decisions(argument)",
])
def test_schema_change_after_prepare_blocks_apply(prepared, ddl):
    world, res = prepared
    edit(res.trial_path, ddl)
    changed = digest_of(res.trial_path)
    with pytest.raises(trp.StateMismatch):
        run_engine(res)
    assert digest_of(res.trial_path) == changed


@pytest.mark.parametrize("stmt", [
    "INSERT INTO decisions (match_id, detector_id, game_time_ms, context_json, options_json, "
    "argument, created_at_utc) VALUES ('X','trade_v1',1,'{}','[]','a','t')",
    "DELETE FROM decisions WHERE detector_id='death_v1'",
    "REPLACE INTO clips (path, source, discovered_at_utc) VALUES ('C:/clips/a.mp4','x','y')",
    "UPDATE decisions SET argument='x'",
    "UPDATE decisions SET user_feedback='agree'",
    "UPDATE matches SET analyzed_at_utc='now'",
    "CREATE TABLE ledger (a)",
    "DROP INDEX idx_decisions_match",
    "PRAGMA user_version = 7",
])
def test_authorizer_blocks_any_other_write(prepared, stmt):
    """INSERT/DELETE/REPLACE/foreign UPDATE by implementation error."""
    world, res = prepared
    bundle = load(res)
    conn = trp.connect_rw_existing(res.trial_path)

    def rogue(stage, index):
        if stage == "after_update":
            conn.execute(stmt)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            trp.apply_batch(conn, bundle, fault=rogue)
    finally:
        conn.close()
    assert digest_of(res.trial_path) == bundle.before_digest


# ============================================================ payload guards

@pytest.mark.parametrize("mutation,code", [
    (lambda ctx: ctx.replace('"minute_span"', '"minute_span": "x",\n  "minute_span"', 1),
     "invalid_json"),
    (lambda ctx: ctx.replace('"reward": {', '"reward": {"bad": NaN, ', 1), "invalid_json"),
    (lambda ctx: ctx.replace('"state_features": {', '"state_features": {"custom": 1, ', 1),
     "unknown_state_features_extension"),
])
def test_payload_guards_abort_and_lose_nothing(world, mutation, code):
    """duplicate keys/NaN, unknown state_features extension."""
    rid = world.ids[("M_A", "trade_v1", 480000)]
    c = sqlite3.connect(world.source)
    ctx = c.execute("SELECT context_json FROM decisions WHERE id=?", (rid,)).fetchone()[0]
    c.execute("UPDATE decisions SET context_json=? WHERE id=?", (mutation(ctx), rid))
    c.commit()
    c.close()
    before = digest_of(world.source)
    with pytest.raises(trp.ValidationAbort) as ei:
        prepare_audit(world, world.keys_s)
    assert abort_code(ei) == code
    assert digest_of(world.source) == before


@pytest.mark.parametrize("path,value", [
    (("options", 0, "label"), "otra"),
    (("options", 1, "predicted_consequence"), "otra"),
    (("context", "reward", "reward_score"), 0.3),
    (("context", "duo_damage_taken"), 600.0),                 # type change counts
    (("context", "action", "action_id"), "disengage"),
    (("context", "positive_trade"), 0),
])
def test_path_allowlist_rejects_foreign_deltas(prepared, path, value):
    """a delta outside the allowlist aborts."""
    world, res = prepared
    e = load(res).entries[0]
    old_ctx = json.loads(e["before"]["context_json"])
    old_opts = json.loads(e["before"]["options_json"])
    new_ctx = json.loads(e["after"]["context_json"])
    new_opts = json.loads(e["after"]["options_json"])
    trp.check_allowed_paths(old_ctx, new_ctx, old_opts, new_opts)
    target = {"context": new_ctx, "options": new_opts}[path[0]]
    for p in path[1:-1]:
        target = target[p]
    target[path[-1]] = value
    with pytest.raises(trp.ValidationAbort) as ei:
        trp.check_allowed_paths(old_ctx, new_ctx, old_opts, new_opts)
    assert abort_code(ei) == "path_not_allowed"


def test_scoring_evolution_is_not_attributed_to_pre(world, monkeypatch):
    """A stored EV that no longer reproduces aborts instead of being 'fixed'."""
    monkeypatch.setattr(TR, "EV_SPAN", 0.61)
    with pytest.raises(trp.ValidationAbort) as ei:
        prepare_world(world)
    assert abort_code(ei) == "baseline_not_reproducible"


# ===================================================== references and tables

def test_references_orphans_and_other_tables_are_untouched(prepared):
    """references by id/key, pre-existing orphans, clips, sessions, sequences."""
    world, res = prepared
    bundle = load(res)
    m = bundle.manifest
    assert m["after_state"]["references_sha256"] == m["before_state"]["references_sha256"]
    run_engine(res)
    c = trp.connect_ro(res.trial_path)
    try:
        state = trp.logical_state(c)
        refs = trp.build_reference_index(c)
    finally:
        c.close()
    assert state["references_sha256"] == m["before_state"]["references_sha256"]
    for t in ("clips", "sessions", "sqlite_sequence", "preferences", "orphan_feedback",
              "exposure_anchors", "matches", "timeline_frames"):
        assert state["tables"][t] == m["before_state"]["tables"][t]
    pref = refs.vectors["preferences"][0]
    # The pre-existing orphan endpoints stay unresolved (no fallback resurrection).
    assert ["decision_b_id", "I:999999", 0] in pref[1:]
    assert ["b_key", "T:19:GONE|trade_v1|60000", 0] in pref[1:]
    assert ["a_key", "T:20:M_S1|trade_v1|240000", 1] in pref[1:]
    assert refs.vectors["orphan_feedback:triple"] == [["I:1", 0]]


# ============================================================ repeat/rollback

def test_repeat_rollback_and_new_mark_after_commit(prepared):
    """repetition without writes, valid inverse restores before, a new
    mark after the commit blocks the inverse."""
    world, res = prepared
    bundle = load(res)
    assert run_engine(res) == "applied"
    assert run_engine(res) == "already_applied"
    assert run_engine(res, trp.rollback_batch) == "rolled_back"
    assert digest_of(res.trial_path) == bundle.before_digest
    assert run_engine(res, trp.rollback_batch) == "already_before"
    assert run_engine(res) == "applied"
    edit(res.trial_path, "UPDATE decisions SET user_feedback='agree' WHERE id=?",
         (world.ids[world.keys_s[0]],))
    marked = digest_of(res.trial_path)
    with pytest.raises(trp.StateMismatch):
        run_engine(res, trp.rollback_batch)
    assert digest_of(res.trial_path) == marked


def test_rollback_fault_keeps_after(prepared):
    world, res = prepared
    bundle = load(res)
    run_engine(res)

    def fault(s, i):
        if s == "before_commit":
            raise RuntimeError("x")
    with pytest.raises(RuntimeError):
        run_engine(res, trp.rollback_batch, fault=fault)
    assert digest_of(res.trial_path) == bundle.after_digest


# ================================================================== prepare IO

def test_prepare_refuses_existing_output_and_source_inside_output(world):
    (world.root / "exists").mkdir()
    with pytest.raises(trp.ValidationAbort) as ei:
        trp.prepare(world.source, world.root / "exists", expected_keyset_sha256=world.keyset,
                    game_data_dir=world.game_data)
    assert abort_code(ei) == "output_exists"
    with pytest.raises(trp.ValidationAbort) as ei:          # the source's own directory
        trp.prepare(world.source, world.root, expected_keyset_sha256=world.keyset,
                    game_data_dir=world.game_data)
    assert abort_code(ei) == "output_exists"


def test_prepare_never_writes_the_source(world):
    before = world.source.read_bytes()
    logical = digest_of(world.source)
    prepare_world(world)
    assert world.source.read_bytes() == before
    assert digest_of(world.source) == logical


def test_prepare_snapshot_pins_one_view_during_concurrent_writes(world, monkeypatch):
    """The hash and the backup come from the same read transaction: a commit that
    lands between them is not in before.sqlite (and the consistency check agrees)."""
    real = trp.logical_state
    calls = []

    def racing(conn):
        state = real(conn)
        if not calls:
            calls.append(1)
            edit(world.source, "UPDATE clips SET source='late'")
        return state
    monkeypatch.setattr(trp, "logical_state", racing)
    res = prepare_world(world)
    c = trp.connect_ro(res.bundle_dir / "before.sqlite", immutable=True)
    try:
        assert c.execute("SELECT source FROM clips").fetchone()[0] == "ascent"
    finally:
        c.close()


# ====================================================== verifier fix round

def test_implementation_identity_covers_every_loaded_module():
    files = trp.loaded_implementation_files()
    for rel in ("lol_coach/context.py", "lol_coach/decisions/features.py",
                "lol_coach/decisions/base.py", "lol_coach/game_data.py", "lol_coach/snapshots.py",
                "lol_coach/decisions/trade.py", "lol_coach/trade_pre_regeneration.py",
                "scripts/regenerate_trade_pre.py"):
        assert rel in files
    assert files == sorted(files) and all("\\" not in f for f in files)


def _git(repo, *args):
    import subprocess
    subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
                   cwd=repo, check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not available")
def test_implementation_committed_requires_every_file_clean_and_at_head(tmp_path):
    """A throwaway git repo under tmp_path (never the project repo)."""
    repo = tmp_path / "repo"
    (repo / "lol_coach").mkdir(parents=True)
    (repo / "lol_coach" / "helper.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "lol_coach" / "main.py").write_text("y = 2\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "lol_coach")
    _git(repo, "commit", "-q", "-m", "init")
    files = ["lol_coach/helper.py", "lol_coach/main.py"]
    ident = trp.implementation_identity(repo, files)
    assert ident["implementation_committed"] is True and ident["not_at_head"] == []
    (repo / "lol_coach" / "helper.py").write_text("x = 3\n", encoding="utf-8")   # dirty helper
    assert trp.implementation_identity(repo, files)["implementation_committed"] is False
    _git(repo, "checkout", "--", "lol_coach/helper.py")
    (repo / "lol_coach" / "new.py").write_text("", encoding="utf-8")            # untracked
    ident = trp.implementation_identity(repo, files + ["lol_coach/new.py"])
    assert ident["implementation_committed"] is False
    assert ident["not_at_head"] == ["lol_coach/new.py"]
    assert trp.implementation_identity(repo, files)["implementation_committed"] is True


@pytest.mark.parametrize("member", ["before.sqlite", "sources/timelines/M_A.json"])
def test_hard_linked_bundle_member_is_rejected(prepared, member):
    world, res = prepared
    os.link(res.bundle_dir / member, world.root / "outside-link")
    with pytest.raises(trp.ValidationAbort) as ei:
        load(res)
    assert abort_code(ei) == "bundle_link_rejected"


@pytest.mark.parametrize("bad", ["../escape.json", "sources/timelines/nope.json",
                                 "C:/abs.json", "/abs.json", "sources\\timelines\\M_A.json",
                                 "sources/./timelines/M_A.json"])
def test_source_member_paths_must_be_declared_and_contained(prepared, bad):
    """Even a manifest re-hashed by whoever edited it cannot point a loader
    outside the declared bundle members."""
    world, res = prepared
    path = res.bundle_dir / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["sources"]["timelines"]["M_A"]["member"] = bad
    path.write_bytes(trp.canonical_json(manifest))
    m_sha = trp.sha256_hex(path.read_bytes())
    b_sha = trp.bundle_digest(trp._bundle_members(res.bundle_dir))
    with pytest.raises(trp.ValidationAbort) as ei:
        trp.load_bundle(res.bundle_dir, m_sha, b_sha)
    assert abort_code(ei) in ("bundle_member_path_rejected", "bundle_member_mismatch")


def test_approval_pins_the_trial_registry(prepared):
    world, res = prepared
    text = (res.evidence_dir / "approval.txt").read_text(encoding="utf-8")
    assert f"trial_registry_sha256={res.trial_registry_sha256}" in text
    assert res.trial_registry_sha256 == trp.sha256_hex(
        (res.evidence_dir / "trial-registry.json").read_bytes())
