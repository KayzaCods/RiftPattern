"""CLI tests for scripts/regenerate_trade_pre.py: copy-only policy, external
hashes, dry-run without writes, aliases/links and recovery after a lost result.

Synthetic only (worlds built by test_trade_pre_regeneration.build_world under
tmp_path). The full flow and the alias rejection also run through the real
entrypoint in a subprocess; the rest call main(argv) in-process.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from lol_coach import trade_pre_regeneration as trp
from test_trade_pre_regeneration import build_world, digest_of, edit, prepare_world

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "regenerate_trade_pre.py"

_spec = importlib.util.spec_from_file_location("regenerate_trade_pre_cli", SCRIPT)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


@pytest.fixture
def prepared(tmp_path):
    world = build_world(tmp_path / "w")
    return world, prepare_world(world)


def hashes(res):
    return ["--manifest-sha256", res.manifest_sha256, "--bundle-sha256", res.bundle_sha256,
            "--trial-registry-sha256", res.trial_registry_sha256]


def target(res, db=None):
    return ["--db", str(db or res.trial_path), "--bundle", str(res.bundle_dir), *hashes(res)]


def run(argv, capsys):
    code = cli.main(argv)
    out = capsys.readouterr().out
    try:
        return code, json.loads(out)
    except ValueError:
        return code, out


def subprocess_run(args):
    proc = subprocess.run([sys.executable, "-B", str(SCRIPT), *args], cwd=REPO,
                          capture_output=True, text=True, encoding="utf-8")
    try:
        body = json.loads(proc.stdout)
    except ValueError:
        body = proc.stdout + proc.stderr
    return proc.returncode, body


def file_fingerprint(path):
    st = os.stat(path)
    return st.st_mtime_ns, st.st_size, trp.file_sha256(Path(path))[1]


# ============================================================ real entrypoint

def test_full_flow_through_the_real_entrypoint(tmp_path):
    world = build_world(tmp_path / "w")
    evidence = tmp_path / "evidence"
    code, out = subprocess_run(["prepare", "--source", str(world.source), "--no-audit-results",
                                "--expected-keyset-sha256", world.keyset,
                                "--game-data-dir", str(world.game_data), "--output", str(evidence)])
    assert code == 0, out
    assert out["counts"]["authorized"] == 2
    # Depends on the git state of whichever repo runs the suite (dirty, clean or
    # not a repo at all); its semantics are covered with a throwaway repo in
    # test_implementation_committed_requires_every_file_clean_and_at_head.
    assert isinstance(out["implementation_committed"], bool)
    h = ["--manifest-sha256", out["manifest_sha256"], "--bundle-sha256", out["bundle_sha256"],
         "--trial-registry-sha256", out["trial_registry_sha256"]]
    approval = (evidence / "approval.txt").read_text(encoding="utf-8")
    assert f"trial_registry_sha256={out['trial_registry_sha256']}" in approval
    t = ["--db", str(evidence / "trial.sqlite"), "--bundle", str(evidence / "bundle"), *h]
    assert subprocess_run(["dry-run", *t])[0] == 0
    assert subprocess_run(["verify", *t, "--expect", "before"])[0] == 0
    code, out2 = subprocess_run(["apply-copy", *t])
    assert (code, out2["result"]) == (0, "applied")
    assert subprocess_run(["verify", *t, "--expect", "after"])[0] == 0
    assert subprocess_run(["verify", *t, "--expect", "before"])[0] == 3
    code, out3 = subprocess_run(["apply-copy", *t])
    assert (code, out3["result"]) == (0, "already_applied")
    code, out4 = subprocess_run(["rollback-copy", *t])
    assert (code, out4["result"]) == (0, "rolled_back")
    assert subprocess_run(["verify", *t, "--expect", "before"])[0] == 0
    assert subprocess_run(["rollback-copy", *t])[1]["result"] == "already_before"
    receipts = sorted(p.name.split("-")[0] for p in (evidence / "receipts").iterdir())
    assert receipts == ["apply", "apply", "prepared.json", "rollback", "rollback"]
    # Alias rejection through the real entrypoint: the source DB as destination.
    before = file_fingerprint(world.source)
    code, out5 = subprocess_run(["apply-copy", "--db", str(world.source),
                                 "--bundle", str(evidence / "bundle"), *h])
    assert (code, out5["error"]) == (2, "destination_not_registered_trial")
    assert file_fingerprint(world.source) == before


def test_entrypoint_imports_no_config_db_or_analyze():
    code = ("import runpy, sys; sys.argv=['x', '--help']\n"
            "try:\n    runpy.run_path(r'%s', run_name='__main__')\n"
            "except SystemExit:\n    pass\n"
            "bad = [m for m in ('lol_coach.config', 'lol_coach.db', 'analyze', 'lol_coach.analysis')"
            " if m in sys.modules]\n"
            "print('BAD' if bad else 'OK', bad)" % SCRIPT)
    proc = subprocess.run([sys.executable, "-B", "-c", code], cwd=REPO, capture_output=True,
                          text=True, encoding="utf-8")
    assert proc.stdout.strip().splitlines()[-1] == "OK []", proc.stdout + proc.stderr


# ================================================================== dry-run

def test_dry_run_makes_zero_writes_and_is_deterministic(prepared, capsys):
    world, res = prepared
    fp, logical = file_fingerprint(res.trial_path), digest_of(res.trial_path)
    code, out = run(["dry-run", *target(res)], capsys)
    assert code == 0, out
    assert out["proposals_byte_equal"] and out["analysis_byte_equal"]
    assert out["inventory_equal"] and out["excluded_equal"] and out["target_state"] == "before"
    assert file_fingerprint(res.trial_path) == fp and digest_of(res.trial_path) == logical
    assert not Path(str(res.trial_path) + "-wal").exists()
    report = (res.evidence_dir / "reports" / "dry-run.json").read_bytes()
    assert run(["dry-run", *target(res)], capsys)[0] == 0
    assert (res.evidence_dir / "reports" / "dry-run.json").read_bytes() == report


def test_dry_run_refuses_a_target_that_is_not_before(prepared, capsys):
    world, res = prepared
    assert run(["apply-copy", *target(res)], capsys)[0] == 0
    code, out = run(["dry-run", *target(res)], capsys)
    assert (code, out["state"]) == (3, "after")


def test_dry_run_detects_scoring_drift_in_the_recompute(prepared, capsys, monkeypatch):
    world, res = prepared
    monkeypatch.setattr(trp._trade, "EV_ATTRITION_NO_PLAY", 0.21)
    code, out = run(["dry-run", *target(res)], capsys)
    assert code == 2


# =========================================================== external hashes

@pytest.mark.parametrize("which", ["manifest", "bundle", "malformed"])
def test_commands_require_the_external_hashes(prepared, capsys, which):
    world, res = prepared
    m, b = res.manifest_sha256, res.bundle_sha256
    if which == "manifest":
        m = "0" * 64
    elif which == "bundle":
        b = "0" * 64
    else:
        m = "XYZ"
    argv = ["apply-copy", "--db", str(res.trial_path), "--bundle", str(res.bundle_dir),
            "--manifest-sha256", m, "--bundle-sha256", b,
            "--trial-registry-sha256", res.trial_registry_sha256]
    before = file_fingerprint(res.trial_path)
    assert run(argv, capsys)[0] == 2
    assert file_fingerprint(res.trial_path) == before


def test_manifest_hash_is_not_derived_from_the_file_itself(prepared, capsys):
    """Rewriting the manifest (e.g. widening S) cannot be self-certified."""
    world, res = prepared
    path = res.bundle_dir / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["entries"] = manifest["entries"][:1]
    path.write_bytes(trp.canonical_json(manifest))
    new_sha = trp.sha256_hex(path.read_bytes())
    argv = ["apply-copy", "--db", str(res.trial_path), "--bundle", str(res.bundle_dir),
            "--manifest-sha256", new_sha, "--bundle-sha256", res.bundle_sha256,
            "--trial-registry-sha256", res.trial_registry_sha256]
    assert run(argv, capsys)[0] == 2


# ========================================================== copy-only policy

def test_live_db_after_prepare_from_snapshot_is_rejected(tmp_path, capsys):
    """prepare from a snapshot, then try apply-copy against the live DB."""
    world = build_world(tmp_path / "w")
    snapshot = tmp_path / "snapshot.db"
    shutil.copy2(world.source, snapshot)
    res = trp.prepare(snapshot, tmp_path / "ev", expected_keyset_sha256=world.keyset,
                      game_data_dir=world.game_data)
    live = world.source
    before = file_fingerprint(live)
    for cmd in ("apply-copy", "rollback-copy", "dry-run"):
        code, out = run([cmd, *target(res, db=live)], capsys)
        assert (code, out["error"]) == (2, "destination_not_registered_trial")
    code, out = run(["verify", *target(res, db=live), "--expect", "before"], capsys)
    assert code == 2
    assert file_fingerprint(live) == before


def test_foreign_path_is_rejected_before_it_is_touched(prepared, capsys):
    world, res = prepared
    fake_real = world.root / "data" / "lol_coach.db"          # never created, never opened
    code, out = run(["apply-copy", *target(res, db=fake_real)], capsys)
    assert (code, out["error"]) == (2, "destination_not_registered_trial")
    assert not fake_real.parent.exists()


def test_copy_of_the_trial_elsewhere_is_rejected(prepared, capsys):
    world, res = prepared
    other = world.root / "copy.sqlite"
    shutil.copy2(res.trial_path, other)
    assert run(["apply-copy", *target(res, db=other)], capsys)[0] == 2
    assert digest_of(other) == digest_of(res.trial_path)


def test_substituted_trial_is_rejected(prepared, capsys):
    world, res = prepared
    tmp = res.evidence_dir / "sub.sqlite"
    shutil.copy2(res.trial_path, tmp)
    os.replace(tmp, res.trial_path)                          # same bytes, other file
    code, out = run(["apply-copy", *target(res)], capsys)
    assert (code, out["error"]) == (2, "trial_substituted")
    assert digest_of(res.trial_path) == trp.load_bundle(
        res.bundle_dir, res.manifest_sha256, res.bundle_sha256).before_digest


def test_hardlinked_source_as_trial_is_rejected(prepared, capsys):
    world, res = prepared
    res.trial_path.unlink()
    os.link(world.source, res.trial_path)
    before = file_fingerprint(world.source)
    code, out = run(["apply-copy", *target(res)], capsys)
    assert code == 2 and out["error"] in ("trial_substituted", "destination_alias_rejected")
    assert file_fingerprint(world.source) == before


def test_trial_with_a_second_hardlink_is_rejected(prepared, capsys):
    world, res = prepared
    os.link(res.trial_path, world.root / "alias.sqlite")
    code, out = run(["apply-copy", *target(res)], capsys)
    assert (code, out["error"]) == (2, "destination_alias_rejected")


def test_symlinked_trial_is_rejected(prepared, capsys):
    world, res = prepared
    real = world.root / "elsewhere.sqlite"
    shutil.copy2(res.trial_path, real)
    res.trial_path.unlink()
    try:
        os.symlink(real, res.trial_path)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks no disponibles en esta plataforma: {exc}")
    code, out = run(["apply-copy", *target(res)], capsys)
    assert (code, out["error"]) == (2, "destination_alias_rejected")


@pytest.mark.skipif(os.name != "nt", reason="junctions are an NTFS feature")
def test_junction_alias_is_rejected(prepared, capsys):
    world, res = prepared
    junction = world.root / "junction"
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(res.evidence_dir)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"mklink /J unavailable: {proc.stdout}{proc.stderr}")
    try:
        argv = ["apply-copy", "--db", str(junction / "trial.sqlite"),
                "--bundle", str(junction / "bundle"), *hashes(res)]
        code, out = run(argv, capsys)
        assert (code, out["error"]) == (2, "destination_alias_rejected")
    finally:
        os.rmdir(junction)                                   # removes the link, not the target
    assert res.trial_path.is_file()


@pytest.mark.parametrize("damage,code", [("missing", "trial_not_registered"),
                                         ("edited", "trial_registry_hash_mismatch"),
                                         ("other_bundle", "trial_registered_for_other_bundle")])
def test_unregistered_trial_is_rejected(prepared, capsys, damage, code):
    world, res = prepared
    registry = res.evidence_dir / "trial-registry.json"
    reg_sha = res.trial_registry_sha256
    if damage == "missing":
        registry.unlink()
    else:
        reg = json.loads(registry.read_text(encoding="utf-8"))
        reg["bundle_sha256"] = "1" * 64
        registry.write_bytes(trp.canonical_json(reg))
        if damage == "other_bundle":             # even with a re-hashed registry
            reg_sha = trp.sha256_hex(registry.read_bytes())
    argv = ["apply-copy", "--db", str(res.trial_path), "--bundle", str(res.bundle_dir),
            "--manifest-sha256", res.manifest_sha256, "--bundle-sha256", res.bundle_sha256,
            "--trial-registry-sha256", reg_sha]
    code_, out = run(argv, capsys)
    assert (code_, out["error"]) == (2, code)


def test_forged_evidence_dir_needs_a_forged_registry_hash(prepared, capsys):
    """Documented limit: the policy stops mistakes, not deliberate forgery. A
    forged evidence dir (bundle copy + registry around another DB) is rejected
    as long as the approver's pinned registry hash is used; whoever computes and
    passes the forged registry's hash deliberately is outside the threat model."""
    world, res = prepared
    ev2 = world.root / "forged"
    ev2.mkdir()
    shutil.copytree(res.bundle_dir, ev2 / "bundle")
    (ev2 / "receipts").mkdir()
    shutil.copy2(res.evidence_dir / "receipts" / "prepared.json", ev2 / "receipts")
    victim = ev2 / "trial.sqlite"
    shutil.copy2(world.source, victim)
    st = os.stat(victim)
    (ev2 / "trial-registry.json").write_bytes(trp.canonical_json({
        "path": os.path.normcase(os.path.realpath(victim)), "st_dev": st.st_dev,
        "st_ino": st.st_ino, "st_nlink": 1, "manifest_sha256": res.manifest_sha256,
        "bundle_sha256": res.bundle_sha256}))
    before = digest_of(victim)
    argv = ["apply-copy", "--db", str(victim), "--bundle", str(ev2 / "bundle"), *hashes(res)]
    code, out = run(argv, capsys)
    assert (code, out["error"]) == (2, "trial_registry_hash_mismatch")
    assert digest_of(victim) == before


def test_prepared_receipt_is_required_before_any_update(prepared, capsys):
    world, res = prepared
    (res.evidence_dir / "receipts" / "prepared.json").unlink()
    code, out = run(["apply-copy", *target(res)], capsys)
    assert (code, out["error"]) == (2, "prepared_receipt_missing")


def test_identity_is_rechecked_just_before_commit(prepared, capsys, monkeypatch):
    world, res = prepared
    real = trp.apply_batch

    def swapped_identity(conn, bundle, *, pre_commit_check=None, **kw):
        def check():
            raise trp.ValidationAbort("trial_substituted", "simulated swap")
        return real(conn, bundle, pre_commit_check=check, **kw)
    monkeypatch.setattr(trp, "apply_batch", swapped_identity)
    code, out = run(["apply-copy", *target(res)], capsys)
    assert code == 2
    assert digest_of(res.trial_path) == trp.load_bundle(
        res.bundle_dir, res.manifest_sha256, res.bundle_sha256).before_digest


# =================================================================== recovery

def test_receipt_failure_after_commit_is_recoverable(prepared, capsys, monkeypatch):
    """the final receipt cannot be written after COMMIT: verify still tells
    the state and a re-run does not reapply."""
    world, res = prepared

    def broken(path, data):
        raise OSError("disk full")
    monkeypatch.setattr(cli, "write_receipt", broken)
    assert cli.main(["apply-copy", *target(res)]) == 1
    capsys.readouterr()
    monkeypatch.undo()
    code, out = run(["verify", *target(res), "--expect", "after"], capsys)
    assert (code, out["result"]) == (0, "after")
    fp = file_fingerprint(res.trial_path)
    code, out = run(["apply-copy", *target(res)], capsys)
    assert (code, out["result"]) == (0, "already_applied")
    assert file_fingerprint(res.trial_path)[1:] == fp[1:]


def test_mixed_state_is_reported_and_not_touched(prepared, capsys):
    world, res = prepared
    assert run(["apply-copy", *target(res)], capsys)[0] == 0
    k = world.keys_s[0]
    edit(res.trial_path, "UPDATE decisions SET context_json='{}' WHERE id=?", (world.ids[k],))
    mixed = digest_of(res.trial_path)
    code, out = run(["verify", *target(res), "--expect", "after"], capsys)
    assert (code, out["result"]) == (3, "mixed")
    assert out["details"]["authorized_neither"] == [world.ids[k]]
    assert run(["rollback-copy", *target(res)], capsys)[0] == 3
    assert run(["apply-copy", *target(res)], capsys)[0] == 3
    assert digest_of(res.trial_path) == mixed


# =================================================================== prepare

def test_prepare_cli_requires_an_explicit_selection_route(tmp_path, capsys):
    world = build_world(tmp_path / "w")
    code, out = run(["prepare", "--source", str(world.source), "--expected-keyset-sha256",
                     world.keyset, "--game-data-dir", str(world.game_data),
                     "--output", str(tmp_path / "ev")], capsys)
    assert (code, out["error"]) == (2, "selection_route_required")
    assert not (tmp_path / "ev").exists()


def test_prepare_cli_rejects_a_different_keyset(tmp_path, capsys):
    world = build_world(tmp_path / "w")
    code, out = run(["prepare", "--source", str(world.source), "--no-audit-results",
                     "--expected-keyset-sha256", trp.AUDITED_KEYSET_SHA256,
                     "--game-data-dir", str(world.game_data),
                     "--output", str(tmp_path / "ev")], capsys)
    assert (code, out["error"]) == (2, "keyset_mismatch")
    assert not (tmp_path / "ev" / "trial.sqlite").exists()


def test_network_is_blocked_inside_the_cli(prepared, capsys, monkeypatch):
    world, res = prepared
    saved = (socket.socket.connect, socket.create_connection)

    def phone_home(conn, bundle):
        socket.create_connection(("127.0.0.1", 9))
    monkeypatch.setattr(trp, "verify_state", phone_home)
    assert cli.main(["verify", *target(res), "--expect", "before"]) == 1
    assert "network access is blocked" in capsys.readouterr().err
    assert (socket.socket.connect, socket.create_connection) == saved


@pytest.mark.skipif(os.name != "nt", reason="relies on Windows file-sharing semantics")
def test_open_write_connection_pins_the_trial_file(prepared):
    """Why path-based identity checks hold for the whole write connection on
    Windows: while SQLite has the trial open it cannot be swapped underneath."""
    world, res = prepared
    other = world.root / "other.sqlite"
    shutil.copy2(world.source, other)
    conn = trp.connect_rw_existing(res.trial_path)
    try:
        conn.execute("SELECT count(*) FROM decisions").fetchone()
        with pytest.raises(PermissionError):
            os.replace(other, res.trial_path)
        with pytest.raises(PermissionError):
            os.rename(res.trial_path, world.root / "moved.sqlite")
    finally:
        conn.close()


# ================================================================ hard crash

def test_hard_kill_mid_apply_is_recovered_by_verify_only(tmp_path, capsys):
    """the process dies inside the transaction (os._exit, cache spilled
    to the file). verify restores the trial through SQLite's hot-journal
    recovery, classifies it as before without re-applying, and apply still works."""
    world = build_world(tmp_path / "w", shared=6)
    res = prepare_world(world)
    child = (
        "import os, sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from pathlib import Path\n"
        "from lol_coach import trade_pre_regeneration as trp\n"
        f"b = trp.load_bundle({str(res.bundle_dir)!r}, {res.manifest_sha256!r}, "
        f"{res.bundle_sha256!r})\n"
        f"c = trp.connect_rw_existing(Path({str(res.trial_path)!r}))\n"
        "c.execute('PRAGMA cache_size=1')\n"
        "c.execute('PRAGMA cache_spill=1')\n"
        "def die(stage, i):\n"
        "    if stage == 'before_commit':\n"
        "        os._exit(9)\n"
        "trp.apply_batch(c, b, fault=die)\n")
    proc = subprocess.run([sys.executable, "-B", "-c", child], cwd=REPO, capture_output=True,
                          text=True)
    assert proc.returncode == 9, proc.stderr
    journal = Path(str(res.trial_path) + "-journal")
    assert journal.is_file() and journal.stat().st_size > 0          # really hot
    # dry-run promises zero writes, so it refuses rather than recover.
    assert run(["dry-run", *target(res)], capsys)[0] == 3
    assert trp.hot_journal(res.trial_path) is not None
    code, out = subprocess_run(["verify", *target(res), "--expect", "before"])
    assert (code, out["result"], out["hot_journal_recovered"]) == (0, "before", True), out
    assert trp.hot_journal(res.trial_path) is None
    assert Path(out["receipt"]).is_file()
    code, out = subprocess_run(["verify", *target(res), "--expect", "before"])
    assert (code, out["hot_journal_recovered"]) == (0, False)
    code, out = subprocess_run(["apply-copy", *target(res)])
    assert (code, out["result"]) == (0, "applied")


def test_hot_journal_recovery_never_runs_on_the_source(tmp_path, capsys):
    """A foreign --db is rejected by path before recovery could ever touch it."""
    world = build_world(tmp_path / "w")
    res = prepare_world(world)
    fake_journal = Path(str(world.source) + "-journal")
    fake_journal.write_bytes(b"x" * 64)
    before = file_fingerprint(world.source)
    code, out = run(["verify", *target(res, db=world.source), "--expect", "before"], capsys)
    assert (code, out["error"]) == (2, "destination_not_registered_trial")
    assert fake_journal.read_bytes() == b"x" * 64 and file_fingerprint(world.source) == before
