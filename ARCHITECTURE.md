# RiftPattern — Architecture

A behavioral coaching engine for League of Legends. It ingests my own ranked
games, detects the *decision points* inside each one, evaluates the choice that
was actually made against the alternatives, and produces a written argument for
what was optimal and why. The output is not a stat sheet or a list of flags — it
is an **argument** per moment: structured context + options with expected value
(EV) + prose. Criteria are refined by human feedback that trains a preference
model over the evaluation weights.

The premise: climbing out of high elo is a *behavioral* problem (consistency),
not a mechanical one. The system's job is to surface the pattern you repeat while
knowing it is wrong. The dominant measured pattern for this account is
**over-extension while ahead**.

---

## Stack (deliberately minimal)

Python standard library + `requests` (Riot's API rejects urllib's default
User-Agent with a 403). The frontend is React over UMD served as a single
`dashboard.html` — **no build step**. Storage is SQLite in WAL mode. No
FastAPI / Vite / ORM, on purpose: the constraint keeps the surface area small
and the whole system inspectable. `pytest` is a dev-only dependency; the runtime
stays stdlib + `requests`.

The dashboard UI and generated coaching prose are in Spanish (the target user is
the author); the code is in English.

**LLM-powered argument generation.** The written argument attached to each
decision is produced by an LLM layer that consumes the *structured* decision row
(state features, action taken, options with their EVs) and renders it as prose.
The layer is provider-agnostic and strictly downstream: it never decides what the
options are, never computes an EV, and nothing it writes is read back by the HEF
or the learner. If the layer is unavailable the decision still exists — it just
carries no prose. That separation is deliberate: the criterion has to be
inspectable and testable, and prose is not.

---

## Data model: accounts and cohorts

Matches are tagged with a `cohort` (the elo bracket they were played in) so
baselines from very different opponent skill levels are not mixed. A small set
of **reference cohorts** (e.g. a Challenger one-trick) is ingested separately and
kept isolated: stats, patterns, and the learner use only the player's own
accounts unless a reference cohort is explicitly selected. Reference players are
ingested through a dedicated manual script and are never added to `config.toml`,
so the automatic ingest never touches them.

| Account | Cohort | Role |
|---|---|---|
| main account | `master` | primary |
| secondary | `emerald` | secondary |
| reference player | `challenger` | comparison only, never trains the model |

---

## Pipeline

```
CAPTURE
  recorder.py (Live Client API :2999, 1 Hz)      external input recorder
    data/raw/<session>/snapshots.jsonl             video .mp4 + input event log
    (exact HP / mana / gold, own games only)       (keystrokes / clicks per game)
        |                                              |
INGEST  +--------------+--------------------------------+
  auto_ingest.py  (scheduled task, every 30 min, headless)
    -> rotated daily DB backup (7 copies, WAL-safe)
    -> sync_ascent.py:
        0) link recorder sessions  <-> Riot matches
        1) link clips, pull missing matches from Riot
        2) for each PENDING match (analyzed_at_utc IS NULL):
             ingest input events -> input_events
             analyze_match -> detectors -> persist_decisions -> reward -> marker
        3) assign clips to decisions
  ingest_reference.py (manual) -> reference player, isolated cohort
        |
  INGEST GUARD: ranked queues only (420 solo/duo, 440 flex), duration >= 300s.
  Normals / remakes -> skipped_matches (tombstone: never re-fetched).
        |
ANALYSIS  lol_coach/analysis.py
  DETECTOR_FUNCS = [death, trade, tempo, objective_readiness]
        |
DASHBOARD  scripts/dashboard.py (stdlib http.server, :8765) + dashboard.html
  /api/decisions  -> HEF recomputed per request with the active weights
  /api/patterns   -> behavioral-progress series (Wilson CIs, moving windows)
  /api/session    -> "today's session": unmarked moments in an anti-bias mix
  /api/ingest_status -> red banner if the Riot API key has gone dead
        |
FEEDBACK  POST /api/feedback   (per-decision: agree / disagree / note /
          best_option / mark_type)   ·   POST /api/preference  (pairwise)
        |
LEARNER   preference.learn_weights (Bradley-Terry MAP, cross-validated)
          -> hef_weights.json (guardrailed) -> feeds back into /api/decisions
```

Two independent capture layers feed the same DB: a 1 Hz **Live Client poller**
for exact per-second state (HP/mana/gold/position) on the player's own games, and
an external **video + input-event recorder** used for input-based signals and
clip attachment. Riot's Match-V5 timeline backfills everything else and is the
only source for reference players.

---

## Decision detectors

Signature: `analyze_x(conn, match_id) -> list[Decision]`. Each `Decision` carries
`context` (standardized `state_features` + the action taken), `options` (each with
a label, a consequence, and an EV; exactly one is marked as "what you did"), an
`argument` (prose), and optionally a video clip.

Internally every detector separates **I/O from logic**: `_gather_facts(conn, …)
-> *Facts` does all the I/O and returns a flat dataclass (returning `None` when
there is no decision), and `_evaluate(facts) -> Decision` is pure. The
classification / options / argument logic is therefore unit-tested without a
database.

- **death_v1** — every one of the player's own deaths, classified by the real
  action: `calculated_sacrifice` (team took an objective within range — traded
  life for objective), `engage_worth` (own kill/assist in the window),
  `support_lost` (an ally fell first — you walked into a fight already lost),
  `trade_lost`, `disengage_failed` (caught on the way out), `engage_blind`
  (over-extension). A key subtlety: enemy **pre-fight visibility** is measured
  10s *before* the death, not at the moment of death — the kill event itself
  "reveals" the killer, which would otherwise make every gank look "seen".
- **trade_v1 (v3)** — bot-lane damage trades during laning (min 2–14, support/bot
  roles, bottom half of the map). Enemy HP is not observable, so damage taken is
  the proxy; a trade is "bad" if your duo took disproportionately more, and the
  enemy duo must have taken meaningful damage too (a one-sided poke is not a
  bilateral exchange). Won trades are persisted but hidden from the lists,
  surfaced only in the pairwise calibration view.

  Two corrections worth recording, because both were *timing* bugs rather than
  criterion bugs. **Frame index vs clock interval:** Match-V5 frame `m` is a
  snapshot at clock `m*60s` and its `damageStats` are cumulative, so the delta
  `frames[m] − frames[m−1]` describes the interval `[(m−1)*60s, m*60s]`. The
  detector used to seal the decision at `m*60_000` — the *end* of the exchange,
  so a trade at 13:13 was reported "at minute 14". It now seals at the start of
  the interval. Because the stable feedback key is `(detector_id, game_time_ms)`,
  moving *when* a detector fires requires a key migration shipped alongside the
  change (`scripts/migrate_trade_keys.py`), not just a code edit.
  **Skip gate asymmetry:** the interval is skipped only when *the player* dies —
  death_v1 filters on `victimId != us_id`, so intervals where the duo partner
  fell and the player did not had no death_v1 decision either and were falling
  through unanalyzed. A kill on *either* enemy still counts as a won trade.
- **tempo_v1** — fights your team took without you. Does not fire if you were
  dead (estimated respawn + grace) or if the fight was physically unreachable.
- **objective_readiness_v1** — each epic objective taken (dragon/herald/baron).
  Scores `setup` = team vision + numbers near the pit + position + resources.
  Handles must-defend states (conceding is correct), uncontested/ dominating
  states (presence optional), natural despawn (not "stolen"), and objectives
  traded for one another. Options avoid outcome bias and never present twin
  choices: if you did the right thing, one option reads "(what you did) — this
  was correct" against a real low-EV contrast.

Retired detectors (`hesitation`, `awareness`, `vision_prep`) remain in the tree
for reference but are not imported: hesitation from current input data is noise,
"look at the map" proved too subjective, and vision prep was absorbed into
objective readiness.

---

## Options: one decision axis, stated from the prior context

The options attached to a decision are the part users actually read, and they are
easy to get subtly wrong. Three rules now hold across every detector:

1. **One real axis.** A decision offers a genuine binary on a single axis — fight
   or disengage, contest or concede — not a menu of near-duplicates and not two
   phrasings of the same action. Options that differ only in wording teach the
   learner nothing and read as filler.
2. **Stated from the context *before* the play, never from the result.** An
   option describes a choice that was available given what was known at that
   instant. Writing it from the outcome ("you shouldn't have died there") is
   outcome bias dressed as coaching, and it also poisons the preference model,
   which is supposed to learn a *criterion*, not a scoreboard.
3. **The taken option is a flag, not prose.** `Option.taken` is an explicit
   boolean set by the detector. It used to be recovered by regex-matching a
   marker inside the label text, which chained the whole feedback chain to the
   exact wording of a sentence. The regex survives only as a read-only fallback
   for rows persisted before the flag existed. Each detector marks exactly one
   option: zero marks push the fingerprint to `t?` and invalidate that detector's
   marks rather than guessing, which is the safe direction to fail in.

Action identifiers (`fight_entry`, `disengage`, `attrition_trade`, `push_wave`,
`vision_setup`, …) are internal ids with a single display-label table
(`action_label_es`); consumers import it rather than each keeping their own map.
Option EVs are derived from the same local-setup blend the HEF uses
(`setup_blend = 0.3·info + 0.7·power`), so an option's EV and the score of the
state it came from cannot drift apart.

---

## HEF — hierarchical evaluation function

`F = Σ ωᵢ·termᵢ − Π` over the terms present.

| Term | Measures | Default weight |
|---|---|---|
| `power` | local advantage (level / gold / HP / numbers) | 0.35 |
| `info` | information safety (enemies seen, jungle, local threat) | 0.30 |
| `action_fit` | how well the action matches the setup | 0.25 |
| `wave` | wave state — **context-sensitive** | 0.10 |

- **`_commit_fit`** blends `setup = 0.3·info + 0.7·power`. The info term was found
  to *overestimate* safety — recalibrating the blend (0.6→0.3 on info) raised
  agreement with human marks from 0.46 to 0.58. Aggressive actions map to
  `setup`, defensive to `1 − setup`, vision to `0.6 + 0.4·(1 − setup)`; a
  `commit_sin_setup` penalty fires on aggression with low info.
- **`wave`** inverts by context: near your own tower during a *death* means safety
  (high); a wave pushed into the enemy during an *objective* means pressure
  (high). The exact same board reads oppositely depending on the moment.
- Active weights live in `hef_weights.json` (learned, reversible to default). The
  HEF is recomputed per request: changing weights never touches the options, EVs,
  or prose (the detectors generate those) — only the score.

---

## Feedback and learning

Two feedback sources, kept separate and balanced to equal mass:

1. **Pairwise calibration** (`preferences`): "which of A or B was played better?"
   over comparable decisions. Carries the weight of the *state* terms.
2. **Per-decision** (`decisions.user_*`): a verdict (`agree`/`disagree`/
   `equivalent`) + the option you consider correct + a note. Isolates
   `action_fit` by comparing two actions on the *same* state.

**Feedback fingerprints (robustness).** `user_best_option` is an *index*, so a
mark is only valid while the option set keeps its meaning. An
`action_fingerprint = v1|detector|action_ids|t{taken_index}` (a semantic
silhouette, ignoring prose with volatile numbers) is stored with each mark, and
the learner validates by *content* rather than by timestamp. A timestamp gate
once invalidated 192/192 marks on a full re-analysis; the fingerprint gate
revives the ones whose options did not actually change.

**Mark type.** `user_mark_type` records *why* a mark was made — `decision`
(a critique of the criterion, the only type that trains the learner),
`execution` (a mechanics complaint, not a decision), `mixed`, `missing_context`,
and `wrong_moment` (the card points at the wrong instant — the mistake was
earlier). The train/skip gate lives in a single pure function; untyped legacy
marks fall back to a noise-note regex so historical behavior is unchanged.

**Durability across re-analysis.** `persist_decisions` preserves feedback by the
stable key `(detector_id, game_time_ms)` across a delete+reinsert; feedback whose
key does not reappear (because a detector changed its triggers) is *reported* as
dropped rather than silently lost.

**Orphan feedback is archived, not discarded.** Marks whose stable key no longer
resolves are moved to an `orphan_feedback` table instead of vanishing with the
row they were attached to. Those notes are months of manual labeling and they are
the field evidence used to diagnose a detector, so losing them costs more than
losing the decision itself. `scripts/orphan_feedback.py` renders the archive back
into a readable report — grouped by detector and by the date of the mark, each
one showing the note verbatim, the moment of the play, the argument that was on
screen at the time, and the option set it was marked against (with the chosen one
flagged). The option set matters: `user_best_option` is an index, so without the
list it is a number with no meaning.

**A stale mark says so on the card.** When the option set changes, a mark
survives in the DB but stops training the learner. That used to happen silently —
the card still displayed the old mark as if it were current. The card now renders
a *historical mark* block, resolving the old choice against the action ids stored
in the fingerprint rather than the current list, with the reason it went stale
(`no_fingerprint` for pre-fingerprint marks the timestamp gate declares stale).
A test pins the card's verdict to the learner's own `n_stale` count over the same
rows, so the two can't diverge into telling different stories.

**Learner** (`preference.learn_weights`, Bradley-Terry MAP): an L2 prior toward
the default weights prevents collapse (e.g. info → 0); the two sources are
balanced to equal mass; accuracy is reported in-sample, per-source, and
**cross-validated**, and the `apply_weights` guardrail trusts only the
out-of-sample number.

**Which decision to mark next** (`lol_coach/trade_queue.py`). Marks are the
scarce resource, so the review queue orders unmarked decisions by how much the
mark would teach the learner. The criterion is hybrid, not pure uncertainty:
Fisher information `p·(1−p)` of the comparison the mark would create — taken
action vs each alternative under the *active* weights, taking the max, so a
decision is worth as much as its most ambiguous available comparison — times a
coverage term `1/(1+n)` that down-weights state buckets already crowded by
existing marks or by higher-ranked queue items. Pure uncertainty sampling with
small n and a possibly miscalibrated model piles every mark on the decision
boundary; the coverage factor spreads them over the state space instead. A pair
with no HEF signal at all scores 0 rather than the degenerate 0.25 that pure
uncertainty would give it, mirroring how the learner drops those rows as noise.
The module is pure and DB-free: it decides the *order* only, and never touches
the detector or the criterion.

---

## Behavioral progress

The point of the whole system is to answer "am I actually improving?" without
outcome bias. The progress view tracks the dominant pattern
(`overextension_with_lead`: engage-blind / disengage-failed deaths while ahead)
as a proportion over a moving window, with a **Wilson confidence interval**, a
death-density curve, a provisional target, and a verdict computed over blocks of
games. The honest current read is "interval-overlapping — no distinguishable
improvement yet", which is exactly the kind of claim the CIs are there to keep
the system from overstating.

---

## Read-only studies (falsification first)

Some questions about the criterion are answered by scripts that only *read* the
DB: they persist nothing, add no detector, and ask for no feedback. Keeping them
out of the write path is what makes a study cheap to throw away when it fails —
and several of these were built specifically to try to kill an idea that looked
good.

- **Exposure without death** (`lol_coach/exposure.py`) — every `death_v1`
  decision conditions on *dying*, so the exposed states you survived are
  invisible to the system, and they are the majority (measured: 93% of exposed
  states did not end in a death). That is the survivorship bias this module
  attacks: it detects the exposed *state* per frame-minute, no death required.
  Phase 1 is report-only, so the false-positive rate can be judged before the
  detector is promoted.
- **Blind anchor-set validation** (`lol_coach/anchor_eval.py`,
  `scripts/exposure_anchor_set.py`, `scripts/exposure_validation.py`) — finding
  hundreds of episodes says nothing about whether they are the *right* ones. The
  anchor set is built from deaths the player had already marked in disagreement,
  and the file to label is generated **blind**: it deliberately does not say
  whether the detector fired on that death, because showing it would anchor the
  judgment to the system's own answer (confirmation bias is the classic failure
  mode of validating a detector against its own author). Reported as recall, a
  Mann-Whitney proxy-AUC (ties count 0.5 — deaths where the detector never fired
  score 0, so ties are massive), and a permutation p-value. Stdlib only.
- **Pre-registered attention experiment** (`scripts/attention_experiment.py`) —
  hypothesis, primary signal, window, α, strata and stopping rule are sealed in
  git *before* the script runs, and are not touched afterwards. The confounder —
  deaths happen mostly during fights, where activity is high by construction — is
  controlled by permuting the label only *within* each `fight_ongoing` stratum,
  which is defined from timeline `CHAMPION_KILL` events and is therefore
  independent of the input events that form the signal. Declared asymmetry: these
  are the same data that generated the hypothesis, so this stage **can only
  refute**, never confirm; surviving it merely earns the right to run stage 2 on
  fresh anchors.
- **Policy delta** (`lol_coach/policy_delta.py`, `scripts/policy_delta.py`) —
  *which action* gets chosen in objective setup, own accounts vs the reference
  cohort, since the positional hypothesis was already falsified. With small n per
  bucket Wald intervals are unreliable, so it uses Jeffreys credible intervals
  (Beta(½,½) per category) with the Beta quantile computed by hand (regularized
  incomplete beta + bisection, stdlib). An action a player never chose is count 0
  over *their own* total, not a degenerate [0,0] — conflating the two was what
  manufactured false divergences. A source of hypotheses with n stated, not of
  verdicts.

---

## Testing

294 characterization tests (they pin current behavior as a safety net for
criterion refactors), run with `pytest`:

- `test_features.py` — pure evaluation: HEF + penalties, `_commit_fit`,
  `setup_blend`, `_wave_term` (the exact death↔objective inversion),
  `power_index`, `action_fingerprint`.
- `test_preference.py` — the Bradley-Terry learner: source balancing, no-collapse
  under the prior, deterministic cross-validation.
- `test_death.py` / `test_tempo.py` / `test_objective_readiness.py` /
  `test_trade.py` — end-to-end integration of all four detectors over a synthetic
  ten-player match, asserting the key guard of each.
- `test_*_logic.py` — the pure `_evaluate` branches per detector, no database.
- `test_feedback.py`, `test_session.py`, `test_progress*.py` — the feedback gate,
  the session mix, and the progress math.
- `test_options_taken.py` — the taken-option contract: the explicit flag wins over
  the label prose, and zero or two marked options degrade predictably.
- `test_trade_moment.py` — that a trade decision is sealed at the start of the
  exchange, not at the end of it.
- `test_trade_queue.py` — the informativeness ranking: Fisher term, coverage
  down-weighting, no-signal pairs scoring 0.
- `test_orphan_feedback.py` — the orphan archive round-trip (note, moment,
  argument and option set survive intact).
- `test_historical_mark.py` — a stale mark renders as historical and the card's
  verdict matches the learner's own stale count.
- `test_exposure.py`, `test_anchor_eval.py`, `test_policy_delta.py` — the
  read-only studies: exposed-state criterion, proxy-AUC/permutation, and the
  Jeffreys interval (including the absent-action case that used to manufacture
  false divergences).

A fresh clone runs the suite with no setup, but 27 of these tests need a local
`config.toml` (importing `scripts/dashboard.py` loads the config at import time)
and one needs a populated database, so they report as *skipped* until the project
is configured. Nothing in the suite requires network access.

---

## Safety invariants

1. `config.toml` (the Riot API key) is never committed. Raw gameplay JSON and the
   SQLite DB are never committed. `git` is the safety net.
2. A daily DB backup runs even when the API key is dead — user feedback lives only
   in the DB.
3. Re-analysis goes only through `analyze.py` / `sync_ascent` (`persist_decisions`).
   Never `DELETE FROM decisions` by hand.
4. Fixed order: detectors → reward → clips. Skipping clips orphans them.
5. `analyzed_at_utc` is the "done" marker.
6. Ranked only; normals / remakes are tombstoned.
7. `current_gold` = spendable gold (readiness/power); `total_gold` = stat only.

---

## Entry points

| Command | Purpose |
|---|---|
| `scripts/sync_ascent.py [--all]` | full pipeline (capture + analysis + clips) |
| `scripts/analyze.py [match \| --all]` | re-analyze, preserving feedback |
| `scripts/dashboard.py [--port 8765]` | dashboard server |
| `scripts/ingest_reference.py "Name#TAG" --champion X` | ingest a reference player |
| `scripts/download_game_data.py` | fetch static game data (patch-versioned) |
| `scripts/trade_feedback_queue.py` | trade marks to make next, ranked by informativeness |
| `scripts/orphan_feedback.py [--json]` | render the orphan-feedback archive |
| `scripts/migrate_trade_keys.py [--apply]` | key migration for the trade_v1 timing fix (dry-run by default) |
| `scripts/exposure_anchor_set.py` | build the blind anchor set to label |
| `scripts/exposure_validation.py` | validate exposure_v1 against the labeled anchors |
| `scripts/attention_experiment.py` | pre-registered attention experiment, stage 1 |
| `scripts/policy_delta.py` | action distribution, own accounts vs reference cohort |
| `.venv\Scripts\python.exe -m pytest` | run the test suite |

Everything below `sync_ascent.py` in that list is read-only except
`migrate_trade_keys.py --apply`, which backs the database up before writing and
refuses to run twice (it records itself in `applied_migrations`).
