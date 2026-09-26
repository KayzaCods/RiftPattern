# RiftPattern: Architecture

A behavioral coaching engine for League of Legends. It ingests my own ranked
games, detects the *decision points* inside each one, evaluates the choice that
was actually made against the alternatives, and produces a written argument for
what was optimal and why. The output is not a stat sheet or a list of flags. It
is an **argument** per moment: structured context, options with expected value
(EV), and prose. Criteria are refined by human feedback that trains a preference
model over the evaluation weights.

The premise: climbing out of high elo is a *behavioral* problem (consistency),
not a mechanical one. The system's job is to surface the pattern you repeat while
knowing it is wrong. The dominant measured pattern for this account is
**over-extension while ahead**.

Scale as of 2026-09-25, counting own games only: 310 ranked games, 4,407
decision points, 505 human marks, 4 active detectors and 650 tests.

---

## Stack (deliberately minimal)

Python standard library + `requests` (Riot's API rejects urllib's default
User-Agent with a 403). The frontend is React over UMD served as a single
`dashboard.html`, with **no build step**. Storage is SQLite in WAL mode. There is
no FastAPI, Vite or ORM, on purpose: the constraint keeps the surface area small
and the whole system inspectable. `pytest` is a dev-only dependency; the runtime
stays stdlib + `requests`.

The dashboard UI and generated coaching prose are in Spanish (the target user is
the author). The code is in English.

**The argument is templated, not generated.** Each detector builds its prose
deterministically from the same structured row that carries the EVs:

- `_compose_argument(ctx, options)` in `death.py`, `tempo.py` and
  `objective_readiness.py`;
- inline in `_evaluate` in `trade.py`.

The templates read the state features, the action taken and the options with
their EVs, and fill in the numbers. Nothing in the system calls a generative
model. This is a deliberate choice, for three reasons:

- the same row always produces the same text;
- the prose sits behind the same pure `_evaluate` boundary as the criterion, so
  it can be tested like any other output;
- the text can only state what the row contains.

The cost is that the prose is plainer and more repetitive than generated text
would be.

---

## Data model: accounts and cohorts

Matches are tagged with a `cohort` (the elo bracket they were played in) so
baselines from very different opponent skill levels are not mixed. A **reference
cohort** (a Challenger reference player) is ingested separately and kept
isolated. Stats and patterns use only the player's own accounts unless a
reference cohort is explicitly selected. Reference decisions are never marked, so
they produce no training pairs for the learner. Reference players are ingested
through a dedicated manual script and are never added to `config.toml`, so the
automatic ingest never touches them.

| Account | Cohort | Role |
|---|---|---|
| main account | `master` | primary |
| secondary | `emerald` | secondary |
| reference player | `challenger` | comparison only, never marked |

---

## Pipeline

```
CAPTURE
  recorder.py (Live Client API :2999, 1 Hz)      external game recorder
    data/raw/<session>/snapshots.jsonl             video .mp4 + input event log
    (exact HP / mana / gold, own games only)       (keystrokes / clicks per game)
        |                                              |
INGEST  +--------------+--------------------------------+
  auto_ingest.py  (scheduled task, every 30 min, headless)
    -> daily DB backup (WAL-safe; only daily backups rotate, keep 7)
    -> sync_ascent.run_sync() -> {state, ingested, decisions, errors[]}:
        0) link recorder sessions  <-> Riot matches
        1) link clips, pull missing matches from Riot
        2) for each PENDING match (analyzed_at_utc IS NULL):
             ingest input events -> input_events
             analyze_match -> detectors -> persist_decisions -> reward -> marker
        3) assign clips to decisions
    -> data/ingest_status.json  (state: completed | partial | failed, key_alive)
  ingest_reference.py (manual) -> reference player, isolated cohort
        |
  INGEST GUARD: ranked queues only (420 solo/duo, 440 flex), duration >= 300s.
  Normals / remakes -> skipped_matches (tombstone: never re-fetched).
        |
ANALYSIS  lol_coach/analysis.py
  DETECTOR_FUNCS = [death, trade, tempo, objective_readiness]
        |
DASHBOARD  scripts/dashboard.py (stdlib http.server, :8766) + dashboard.html
  /api/matches     -> recent matches: K/D/A, role, per-detector counts
  /api/decisions   -> HEF recomputed per request with the active weights
  /api/patterns    -> behavioral-progress series (Wilson CIs, moving windows)
  /api/session     -> "today's session": unmarked moments in an anti-bias mix
  /api/review_queue-> marks that contradict the formula + stale marks to redo
  /api/trade_moment-> the exact second of a trade, from the recorder's HP series
  /api/minimap_crop-> per-recording-source minimap crop (GET / POST calibrate)
  /clip            -> the recording, served with 206 Partial Content
  /api/ingest_status -> banner for partial / failed runs and a dead key
        |
FEEDBACK  POST /api/feedback   (per-decision: verdict / note /
          best_option / mark_type)   ·   POST /api/preference  (pairwise)
        |
LEARNER   preference.learn_weights (Bradley-Terry MAP, cross-validated)
          -> hef_weights.json (guardrailed) -> feeds back into /api/decisions
```

Two independent capture layers feed the same DB:

- a 1 Hz **Live Client poller** for exact per-second state
  (HP/mana/gold/position) on the player's own games;
- an external **video + input-event recorder**, used for input-based signals,
  clip attachment and the minimap strip.

Riot's Match-V5 timeline backfills everything else and is the only source for
reference players. It has two hard limits that shape the design: position frames
are **one per minute**, and `WARD_PLACED` events carry **no position at all**
(measured: 0 of 3,000 sampled, against 3,000 of 3,000 `CHAMPION_KILL` events with
coordinates).

### Ingestion health

A run's outcome is a structured result, not something parsed from console output.
`sync_ascent.run_sync()` returns a `state` of `completed` (including "no new
games"), `partial` (the flow finished with recoverable errors) or `failed` (the
flow was interrupted). It also returns an `errors` list of stage, code and count,
with no private error samples. `key_alive` is kept as a separate field: it is
the result of a pre-check, not a diagnosis of the run. The dashboard banner shows
partial and failed runs even when the key is alive.

This exists because of a silent failure. **Riot encrypts PUUIDs per API key.**
After a key rotation, the cached PUUIDs could no longer be decrypted, and
match-v5 answered `400 Exception decrypting`. The account registry only called
the API for accounts missing from its cache, so the cache survived exactly the
event that invalidated it. Ingestion stopped for 9 days, and nothing flagged it:

- the key check tested a call that needs no PUUID, the one capability that
  survives a rotation;
- Riot errors went only to a log line, so the summary read "0 fetched from Riot",
  which looks like "you didn't play".

The fix has three parts:

- PUUIDs are re-resolved on every run, with the cache kept only as a fallback
  when the network is down;
- the key check probes `matches/by-puuid` with the cached PUUID, which is what
  ingestion actually needs, and treats a 400 as dead;
- Riot errors are counted into the run result.

---

## Decision detectors

Signature: `analyze_x(conn, match_id) -> list[Decision]`. Each `Decision`
carries:

- `context`: standardized `state_features` plus the action taken;
- `options`: each with a label, a consequence, an EV and a `taken` flag, with
  exactly one option taken;
- `argument`: the templated prose;
- optionally, a video clip.

Internally every detector separates **I/O from logic**. `_gather_facts(conn, …)
-> *Facts` does all the I/O and returns a flat dataclass (or `None` when there is
no decision), and `_evaluate(facts) -> Decision` is pure. The classification,
options and argument logic is therefore unit-tested without a database.

### death_v1

Every one of the player's own deaths, classified by the real action:

- `calculated_sacrifice`: the team took an objective within range, trading a
  life for an objective;
- `engage_worth`: the engage paid off;
- `support_lost`: an ally fell first, so you walked into a fight already lost;
- `trade_lost`;
- `disengage_failed`: caught on the way out;
- `engage_blind`: over-extension.

"Paid off" is a claim about the exchange, so the player's own kill or assist only
counts if the exchange was not lost: either the team won the fight, or the player
scored and allied deaths did not exceed enemy deaths. Before this fix a 1-for-3
fight was labelled an engage that paid off. Measured over the 540 decisions with
that label, 73 (14%) had more allied than enemy deaths, and the player's
disagreement rose with how bad the exchange was: 3% when favourable, 17% when
even, 30% when behind. Those cases now fall into `support_lost` or `trade_lost`,
which describe what happened.

Enemy **pre-fight visibility** is measured 10 s *before* the death, not at the
moment of death. The kill event itself "reveals" the killer, which would
otherwise make every gank look "seen".

### trade_v1

Bot-lane damage exchanges during laning (minutes 2–14, support/bot roles, bottom
half of the map).

- **Damage as the proxy.** Enemy HP is not observable, so damage taken is the
  proxy. The enemy duo must have taken meaningful damage too: a one-sided poke is
  not a bilateral exchange.
- **Duo co-presence.** The trigger sums damage taken by *both* members of the
  duo, but it originally checked only the player's position. A partner taking
  damage in another lane counted as a duo trade. The partner must now also be in
  a valid bot-side zone and within 2,500 units of the player.
- **Won and lost trades.** Both are persisted. Won trades are hidden from the
  lists and surfaced only in the pairwise calibration view.

Three corrections worth recording. All three are *timing* bugs rather than
criterion bugs.

- **Frame index vs clock interval.** Match-V5 frame `m` is a snapshot at clock
  `m*60s`, and its `damageStats` are cumulative. The delta
  `frames[m] − frames[m−1]` therefore describes the interval
  `[(m−1)*60s, m*60s]`. The detector used to seal the decision at `m*60_000`,
  the *end* of the exchange, so a trade at 13:13 was reported "at minute 14". It
  now seals at the start of the interval. The stable feedback key is
  `(detector_id, game_time_ms)`, so moving *when* a detector fires required a
  key migration shipped alongside the change (`scripts/migrate_trade_keys.py`),
  not just a code edit.
- **Skip gate asymmetry.** The interval is skipped only when *the player* dies.
  death_v1 filters on `victimId != us_id`, so intervals where the partner fell
  and the player did not had no death_v1 decision either, and were falling
  through unanalyzed. A kill on *either* enemy still counts as a won trade.
- **The PRE state is anchored to the real start of the block.** The state a
  trade is evaluated on (the PRE) used to be read from "the last frame at or
  before the nominal minute". Frames land shortly after each nominal minute
  (within about a second), so that lookup returned the *previous* frame, almost a
  minute stale.
  Before the fix, 882 of 888 trades had a PRE more than 50 s old (median
  59.9 s). The PRE is now anchored to `B`, the actual timestamp of the frame
  that opens the first delta of the block. The resolver requires exact,
  coherent frame boundaries for all ten participants. It uses no nearest-frame
  or interpolation fallback: if the opening is missing or inconsistent, the
  candidate is dropped before evaluation. New decisions carry
  `context.pre_temporal` with a versioned contract (`trade_block_open_v1`). That
  contract certifies only the block start (`block_start_only`); it does not
  claim precedence over the player's action (`action_precedence: unverified`).
  The decision's key, clip and options are unchanged. Rows persisted earlier
  keep the old PRE; see
  [selective regeneration](#selective-regeneration-of-historical-trade-pre).

The clip and the minimap strip open at the exact second of the exchange,
recovered by `/api/trade_moment` from the largest HP drop in the recorder's
series inside the trade window. Without recorder data, no second is claimed.

### tempo_v1

Fights your team took without you. It does not fire if you were dead (estimated
respawn plus a grace period) or if the fight was physically unreachable.

### objective_readiness_v1

Each epic objective taken (dragon, herald, baron). It scores `setup` from team
vision, numbers near the pit, position and resources. It handles:

- must-defend states, where conceding is correct;
- uncontested or dominating states, where presence is optional;
- natural despawn, which is not a "steal".

When the team took another objective or structure around the same time, the
detector no longer *asserts* a trade ("you traded X for Y"). The counterpart
matching turned out to be loose: 64% of detected counterparts were towers, which
fall constantly, and the assertion drew 33% disagreement. No measured signal
separated incidental towers from deliberate ones with the marks available. So
the detector offers a **priority axis** of three parallel actions instead: go
for this objective, make a higher-value play such as the counterpart, or concede
and regroup. None is labelled as what you did. EVs come from the prior setup,
never from the outcome. That branch covers 134 decisions.

The pre-objective vision count in this detector was
[validated against video and found broken](docs/experiments/vision-proxy-validation.md):
63.3% of its zeros were false. Its "70% of objectives with no vision" figure has
been withdrawn, and the real rate is 42.9% [39.1, 47.2]. The proxy itself is
still in place and documented as open.

### Retired detectors

`hesitation`, `awareness` and `vision_prep` remain in the tree for reference but
are not imported:

- hesitation from current input data is noise;
- "look at the map" proved too subjective;
- vision prep was absorbed into objective readiness.

---

## Options: one decision axis, stated from the prior context

The options attached to a decision are the part users actually read, and they are
easy to get subtly wrong. There are three rules. The first two are the target
state and are applied to `trade_v1` and to the objective priority axis; `death_v1`
and the remaining objective branches still use the older vocabulary (see
[Known limitations](README.md#known-limitations)). The third holds in every
detector.

1. **One real axis.** A decision offers parallel actions a player could choose
   before knowing how it ends: trade or don't, this objective or a better play.
   It should not be a menu of near-duplicates or two phrasings of the same
   action. Options that differ only in wording teach the learner nothing.
2. **Stated from the context *before* the play, never from the result.** Writing
   an option from the outcome ("you shouldn't have died there") is outcome bias
   dressed as coaching. It also poisons the preference model, which is supposed
   to learn a *criterion*, not a scoreboard. The corollary: **the outcome must
   not choose the menu**. `trade_v1` used to offer two options when the trade was
   won and three when it was lost. Nobody marks a menu whose options already know
   the result, and none of the 583 won-trade decisions carried a mark. Every
   trade now offers the same axis in a frozen order: don't trade, trade now, or
   take intentional attrition in exchange for an upcoming play. That order is
   the one the already-marked lost trades had, so all 26 existing marks
   survived. The outcome still lives where it belongs: in `moment`, `outcome`,
   `argument` and each option's predicted consequence.
3. **The taken option is a flag, not prose.** `Option.taken` is an explicit
   boolean set by the detector. It used to be recovered by regex-matching a
   marker inside the label text, which chained the whole feedback path to the
   exact wording of a sentence. The regex survives only as a read-only fallback
   for rows persisted before the flag existed. Each detector marks exactly one
   option. Zero marks push the fingerprint to `t?` and invalidate that detector's
   marks rather than guessing, which is the safe direction to fail in.

Action identifiers (`fight_entry`, `disengage`, `attrition_trade`, `push_wave`,
`vision_setup`, …) are internal ids with a single display-label table
(`action_label_es`). Consumers import it rather than each keeping their own map.

In `trade_v1`, option EVs derive from the same local-setup blend the HEF uses
(`setup_blend = 0.3·info + 0.7·power`). An option's EV and the score of the state
it came from therefore cannot drift apart, and the recommendation depends on
whether you *could* win the trade, not on whether you did. Intentional attrition
only scores as a real option when an upcoming play can pay for the HP, such as a
wave to crash before resetting. Where the wave state is unknown, the prose says
it cannot confirm one instead of asserting there was none.

---

## HEF: hierarchical evaluation function

`F = Σ ωᵢ·termᵢ − Π` over the terms present.

| Term | Measures | Default weight |
|---|---|---|
| `power` | local advantage (level / gold / HP / numbers) | 0.35 |
| `info` | information safety (enemies seen, jungle, local threat) | 0.30 |
| `action_fit` | how well the action matches the setup | 0.25 |
| `wave` | wave state, **context-sensitive** | 0.10 |

- **`_commit_fit`** blends `setup = 0.3·info + 0.7·power`. The info term was
  found to *overestimate* safety. Recalibrating the blend (0.6→0.3 on info)
  raised agreement with human marks from 0.46 to 0.58.
  - Aggressive actions map to `setup`.
  - Defensive actions map to `1 − setup`.
  - Vision maps to `0.6 + 0.4·(1 − setup)`.
  - A `commit_sin_setup` penalty fires on aggression with low info.
- **`wave`** inverts by context. Near your own tower during a *death* means
  safety (high). A wave pushed into the enemy during an *objective* means
  pressure (high). The same board reads oppositely depending on the moment.
- Active weights would live in `hef_weights.json` (learned, reversible to the
  defaults). The HEF is recomputed per request. Changing weights never touches
  the options, EVs or prose, which the detectors generate; it changes only the
  score.

---

## Feedback and learning

There are two feedback sources, kept separate and balanced to equal mass:

1. **Pairwise calibration** (`preferences`): "which of A or B was played
   better?" over comparable decisions. It carries the weight of the *state*
   terms.
2. **Per-decision** (`decisions.user_*`): a verdict (`agree` / `disagree` /
   `equivalent`), the option you consider correct, and a note. It isolates
   `action_fit` by comparing two actions on the *same* state.

**Feedback fingerprints (robustness).** `user_best_option` is an *index*, so a
mark is only valid while the option set keeps its meaning. Each mark is stored
with an `action_fingerprint = v1|detector|action_ids|t{taken_index}`: a semantic
silhouette that ignores prose with volatile numbers. The learner validates by
*content* rather than by timestamp. A timestamp gate once invalidated 192/192
marks on a full re-analysis; the fingerprint gate revives the ones whose options
did not actually change. Validity is a single function (`features.mark_validity`)
with four verdicts: `valid`, `revived`, `options_changed` and `no_fingerprint`.
The learner, the review queue and the card all call it, so they cannot disagree
about whether a mark counts. Marks made before fingerprints existed still fall
back to the timestamp (see [Known limitations](README.md#known-limitations)).

**Mark type.** `user_mark_type` records *why* a mark was made:

- `decision`: a critique of the criterion, and the only type that trains the
  learner;
- `execution`: a mechanics complaint, not a decision;
- `mixed`;
- `missing_context`;
- `wrong_moment`: the card points at the wrong instant, and the mistake was
  earlier.

The train/skip gate lives in a single pure function. Untyped legacy marks fall
back to a noise-note regex, so historical behavior is unchanged.

**Durability across re-analysis.** `persist_decisions` preserves feedback by the
stable key `(detector_id, game_time_ms)` across a delete and reinsert. Feedback
whose key does not reappear, because a detector changed its triggers, is
*reported* as dropped rather than silently lost.

**Orphan feedback is archived, not discarded.** Marks whose stable key no longer
resolves are moved to an `orphan_feedback` table instead of vanishing with the
row they were attached to. Those notes are months of manual labeling and the
field evidence used to diagnose a detector, so losing them costs more than
losing the decision itself. `scripts/orphan_feedback.py` renders the archive as a
readable report, grouped by detector and by the date of the mark. Each entry
shows the note verbatim, the moment of the play, the argument that was on screen
at the time, and the option set it was marked against, with the chosen option
flagged.

**A stale mark says so, and can be found.** When the option set changes, a mark
survives in the DB but stops training the learner.

- **On the card**, it renders as a *historical mark*. The old choice is resolved
  against the action ids stored in the fingerprint rather than the current list,
  and the card states why the mark went stale. A test pins the card's verdict to
  the learner's own stale count over the same rows.
- **In the Review queue**, stale marks worth redoing are listed in their own
  panel, with a one-click jump to each decision. Before that, the only way to
  find them was a match id from the console.

**Learner** (`preference.learn_weights`, Bradley-Terry MAP).

- An L2 prior toward the default weights prevents collapse (e.g. info → 0).
- The two sources are balanced to equal mass.
- Accuracy is reported in-sample, per source and **cross-validated**. The
  `apply_weights` guardrail trusts only the out-of-sample number.

Measured three times in August on 110–116 pairs, the learned weights reached
0.527 cross-validated accuracy against 0.518 for the defaults, with a standard
error of about 0.047. That is noise, so the defaults remain active.

**Is the interface dictating the answer?** (`scripts/anchoring-check.py`,
read-only). If marks were independent judgments, the option marked as right
would match the system's max-EV option about as often as the HEF is right, which
is around 52%. A rate far above that means the player is copying a visible
recommendation, and the learner trains on a copy of itself.

- The check counts only currently valid marks, via the same `mark_validity`. It
  counts EV ties as matches and reports Wilson intervals.
- **Old picker.** It showed EV, a bar and the recommendation while the player
  chose: 85% (273/323) at the original measurement, reproduced as 84.4%
  (190/225) over valid marks.
- **Neutral picker.** The first batch matched 3/9 = 33.3%, Wilson 95% CI
  [12.1, 64.6]. That is below the 60% suspicion threshold, but with n = 9 the
  interval still reaches 64.6%. It is an indication in favour of the fix, not a
  confirmation. More marks are needed.
- **What the picker hides.** It shows no `optimal` flag, EV, bar or colour. While
  no option is selected, the calibration stage (04) shows only a line saying it
  becomes available after you register your decision; the EV breakdown and the
  recommendation are absent from the DOM, not blurred or silhouetted. They do
  appear as soon as an option is *selected*, before the choice is saved. That
  gap is a known limitation.
- **What the picker does not hide.** For `death` and `tempo`, the argument shown
  in stage 01 ends by naming the highest-EV option and its EV, and the HEF
  verdict is visible in stage 01 and on the collapsed row. The picker fix
  removes the recommendation from the picker, not from the page, so marks on
  those two detectors are not independent of the system. The first neutral
  batch above was all trades, whose prose names no option.

**Which decision to mark next** (`lol_coach/trade_queue.py`). Marks are the
scarce resource, so the review queue orders unmarked decisions by how much the
mark would teach the learner. The criterion is a product of two terms:

- **Fisher information** `p·(1−p)` of the comparison the mark would create:
  taken action against each alternative under the *active* weights, taking the
  max. A decision is worth as much as its most ambiguous available comparison.
- **A coverage term** `1/(1+n)` that down-weights state buckets already crowded
  by existing marks or by higher-ranked queue items.

Pure uncertainty sampling, with small n and a possibly miscalibrated model,
piles every mark on the decision boundary; the coverage factor spreads them over
the state space instead. A pair with no HEF signal scores 0 rather than the
degenerate 0.25 that pure uncertainty would give it. The module is pure and
DB-free, and decides the *order* only.

---

## Minimap strip: evidence from the video

The timeline cannot say where wards were or what was in fog. The recording can.
When this was built, 84% of decisions had a local recording with an exact
clip offset. Inside the evidence stage of a decision, `MinimapStrip` shows crops
of the recorded minimap at detector-specific offsets before the event:

| Detector | Offsets |
|---|---|
| death, trade | −30 s, −10 s, event |
| tempo | −45 s, −20 s, event |
| objective | −90 s, −45 s, −15 s |

For deaths and objectives the stored time is the *outcome*, such as the kill.
The objective window closes at −15 s so that every frame shows something that
could still be decided. The strip shows what was available on screen, never what
the player was looking at: it is observation, not reproach.

How it works:

- **No ffmpeg, no server-side decoding.** One hidden `<video>` element seeks
  through the clip, served by `/clip` with 206 Partial Content, and each frame
  is cropped through a same-origin `<canvas>`.
- **Seeks are fast enough.** Measured on a 2,574 s recording, 9/9 seeks
  completed with a median of 21.3 ms, a p95 of 32.3 ms and a maximum of 68.6 ms.
  A seek that does not answer within 2 s is dropped rather than hanging the card.
- **Crops are calibrated per recording source, never guessed.** Minimap position
  and scale depend on the recorder and its resolution. `clip_source` identifies
  the recorder for each decision. The user calibrates each source once by
  dragging a rectangle over a mid-game sample frame (the source's median clip
  offset). The crop is stored as fractions in `data/minimap_crop.json` with an
  atomic write. A missing, corrupt or invalid entry means "uncalibrated", and the
  strip says so and offers to calibrate. A guessed crop would fail silently.
- **Trades anchor on the exact second.** The strip anchors on the second from
  `/api/trade_moment`. If the recorder has no data for that game, the strip is
  not drawn: falling back to the minute boundary would misalign it by up to 60 s
  against the clip beneath it.
- **Offsets outside the recording are omitted**, never replaced by the first or
  last frame.

The strip has a declared ceiling. The crop is 121 px wide in 95% of clips, and a
ward icon is 4–5 px at that size. It supports human reading, not computer vision.

---

## Selective regeneration of historical trade PRE

The PRE fix above applies to newly analyzed decisions. Correcting rows already
in the database cannot go through the normal pipeline, which deletes and
reinserts a whole match (new ids, internal commits) and has no allowlist.
`lol_coach/trade_pre_regeneration.py` and `scripts/regenerate_trade_pre.py`
(`prepare | dry-run | apply-copy | verify | rollback-copy`) rewrite **in place**
only `context_json` and `options_json` of a closed set of rows, and prove that
everything else stays logically identical.

- **Closed key set.** The keys are `[match_id, "trade_v1", game_time_ms]`. The
  digest of the sorted keys must equal an expected hash passed as an argument.
  Any human signal on any key aborts the whole batch: a verdict, note, chosen
  option or mark type (even an empty one), a preference, an archived orphan mark,
  or an anchor or study label.
- **Immutable evidence bundle.** It contains a `before` backup taken from a
  read-only view, the exact timeline bytes, the static game data, a manifest and
  the proposals. The manifest and bundle hashes are not self-referential and are
  supplied by argument. Recorder snapshots enter as a *projection* (game time plus
  the four HP/mana fields the evaluator reads), because the originals total about
  11 GB. The projection is tested equivalent for that reader.
- **Proposals.** For each key, the legacy features and EVs are first
  reconstructed and must reproduce what is stored; otherwise the batch aborts,
  since the difference would come from something other than the PRE. The
  evaluator is re-run with the stored post-trade facts and the features at `B`.
  The overlay is limited to an allowlist of paths. `reward` and every other field
  keep their value and type.
- **Inventory check.** For each affected match, the scanner's candidates must
  match the persisted trade rows exactly. A missing, new, duplicated or changed
  row aborts.
- **Engine.**
  - The write runs under `BEGIN IMMEDIATE`, and the full state is first
    classified: before (apply), after (`already_applied`) or anything else
    (abort).
  - A SQLite authorizer allows only `UPDATE` of the two columns.
  - Each update is predicated on id, key, a NULL-safe pre-image and all `user_*`
    columns being NULL, and requires `rowcount == 1`. `total_changes` must be
    exact.
  - The full state is re-read and compared with the approved after-state, then
    `integrity_check` runs, then a single `COMMIT`. Any exception rolls back.
  - `rollback-copy` inverts in place from the before-state, and `verify`
    classifies a database as before, after or mixed without reapplying.
- **Copy-only policy.** The CLI writes only to the trial copy that `prepare`
  created and registered. It checks the path, device and inode, a single link, no
  reparse points and that the copy differs from the source, both on open and just
  before commit. This protects against mistakes (wrong path, alias, link,
  substitution), not against someone deliberately forging an evidence directory.
- **Code identity.** The manifest records the git HEAD and the hash of every
  loaded module. It is marked committed only if all of them are clean at HEAD.

**Rehearsal on a copy (2026-09-25), with the production database opened
read-only.**

- The key-set digest was reproduced.
- 840 rows were authorized, and all 840 get a different PRE.
- The system's recommended option changes in 458 of them and stays the same in
  382.
- The dry-run was byte-identical, apply produced the after-state, a second apply
  reported `already_applied`, and rollback returned to the before-state.
- An external comparison found exactly 840 changed rows, only in those two
  columns. The 505 marked rows, every `reward`, the other 16 tables and the
  schema were identical.

**It has not been applied to the production database.** There is deliberately
no command that writes to it. Applying it would require:

- explicit authorization;
- a window with no ingestion or marking;
- a fresh `prepare` on the state at that moment;
- a new, real-apply entry point.

The 54 existing trade marks judged the old PRE. They are not regenerated or
revalidated by this tool.

---

## Behavioral progress

The point of the whole system is to answer "am I actually improving?" without
outcome bias. The progress view tracks the dominant pattern
(over-extension with a lead: `engage_blind` or `disengage_failed` deaths while
ahead)
as a proportion over a moving window. It shows:

- a **Wilson confidence interval**;
- a death-density curve;
- a provisional target;
- a verdict computed over blocks of games.

The honest current read is "interval-overlapping: no distinguishable improvement
yet". Keeping the system from overstating that is exactly what the CIs are for.

---

## Read-only studies (falsification first)

Some questions about the criterion are answered by scripts that only *read* the
DB: they persist nothing, add no detector and ask for no feedback. Keeping them
out of the write path makes a study cheap to throw away when it fails. Several of
these were built specifically to try to kill an idea that looked good.

- **Pre-objective vision proxy vs video**
  ([write-up](docs/experiments/vision-proxy-validation.md)). This study tested
  whether "0 own wards near the pit" described a habit or a measurement error. A
  frozen sample of 100 was labelled against the recording: 62 of 98 readable
  zeros were false (63.3%, 95% CI [53.4, 72.1]), above the p ≥ 0.30 break
  threshold across the whole interval. The published 70% dropped to 42.9%
  [39.1, 47.2]; about 301 of 1,098 objectives had been miscounted. Two causes
  were found: ward positions proxied from per-minute frames, up to 60 s off, and
  a "near the pit" radius equal to 23% of the map's width. The study's labels
  are kept in their own tables and never reach the learner.
- **Exposure without death** (`lol_coach/exposure.py`). Every `death_v1`
  decision conditions on *dying*, so the exposed states you survived are
  invisible to the system, and they are the majority. This module detects the
  exposed *state* per frame-minute, no death required, and collapses consecutive
  frames into episodes so that autocorrelation doesn't inflate n. **Archived in
  negative:** against the blind anchor set below it reached 3% recall, AUC 0.477
  and permutation p = 0.725. In the deaths the player called exposure, the player
  was usually on their own half of the map. The criterion measured depth into enemy
  territory, which is a different construct. Six alternative signals scored AUC
  0.35–0.52. The card stays visible, labelled "not validated".
- **Blind anchor-set validation** (`lol_coach/anchor_eval.py`,
  `scripts/exposure_anchor_set.py`, `scripts/exposure_validation.py`).
  - Finding hundreds of episodes says nothing about whether they are the *right*
    ones. The anchor set is built from deaths the player had already marked in
    disagreement.
  - The file to label is generated **blind**: it does not say whether the
    detector fired, because showing that would anchor the judgment to the
    system's answer.
  - Results are reported as recall, a Mann-Whitney proxy-AUC with ties counted as
    0.5, and a permutation p-value. With n = 34/13, the minimum detectable AUC
    was 0.658, which rules out a large effect but not a small one.
- **Pre-registered attention experiment** (`scripts/attention_experiment.py`).
  - The hypothesis, primary signal, window, α, strata and stopping rule are
    sealed in git *before* the script runs.
  - The confounder is that deaths happen mostly during fights, where activity is
    high by construction. It is controlled by permuting the label only within
    each `fight_ongoing` stratum. Those strata are defined from timeline kill
    events and are independent of the input events that form the signal.
  - This stage **can only refute**, never confirm. It did not refute. Stage 2
    needs at least 25 fresh anchors *of the minority class*, and there are 2
    today, so it has not been run.
- **Policy delta** (`lol_coach/policy_delta.py`, `scripts/policy_delta.py`,
  [report](docs/experiments/policy_delta.md), in Spanish). It compares *which
  action* gets chosen in objective setup, own accounts against the reference
  cohort.
  - It uses Jeffreys credible intervals (Beta(½,½) per category), with the Beta
    quantile computed by hand in the standard library.
  - An action a player never chose counts as 0 over *their own* total, not as a
    degenerate [0,0] interval.
  - **Closed as null for lack of power.** No bucket with n ≥ 8 in both cohorts
    has non-overlapping intervals. With about 20 reference games, this is a data
    limit, not evidence that the two play alike.

---

## Frontend

`scripts/dashboard.html` is a single file with React 18 over UMD and no build
step.

- **Views.** The dashboard opens on *recent matches*. A flat list of all
  decisions remains as a secondary view with filters.
- **Styling.** A token layer uses OKLCH colours and three tonal surface levels.
  Colour carries fixed semantic roles (death, trade, tempo, objective, status,
  navigation) and is never the only signal.
- **Four-stage review.** Each decision reads as *program's analysis → available
  evidence → your reasoning → calibration / result*. The option picker closes
  stage 02 and is neutral, with the limits described above.
- **Deep links and state.** `?dec=`, `?match=` and `?tab=` deep-link into the
  dashboard. Filters, the selected match and scroll position persist in
  `sessionStorage`; no Riot IDs or note text are stored there.
- **No network requests in normal use.** Champion, item and rune icons are
  downloaded once and verified against a manifest (URL, size, dimensions,
  SHA-256), then served read-only from `/assets/` with path containment and no
  symlinks.

---

## Testing

650 characterization tests, run with `pytest`. They pin current behavior as
a safety net for criterion refactors.

- `test_features.py`: pure evaluation. HEF and penalties, `_commit_fit`,
  `setup_blend`, `_wave_term` (the exact death↔objective inversion),
  `power_index`, `action_fingerprint`.
- `test_preference.py`: the Bradley-Terry learner. Source balancing, no collapse
  under the prior, deterministic cross-validation.
- `test_death.py` / `test_tempo.py` / `test_objective_readiness.py` /
  `test_trade.py`: end-to-end integration of all four detectors over a synthetic
  ten-player match, asserting the key guard of each. `test_trade.py` also pins
  the frame-interval semantics, the player-only death gate and the scanner's
  equivalence after it was extracted for regeneration.
- `test_*_logic.py`: the pure `_evaluate` branches per detector, no database.
  Trade covers the single option axis and the setup-derived recommendation.
- `test_feedback.py`, `test_session.py`, `test_progress*.py`: the feedback gate,
  the session mix and the progress math.
- `test_options_taken.py`: the taken-option contract. The explicit flag wins over
  the label prose, and zero or two marked options degrade predictably.
- `test_trade_moment.py`: a trade is sealed at the start of the exchange, and
  its exact second comes from the recorder.
- `test_minimap_strip_backend.py`: clip source resolution and per-source crop
  calibration, including "missing or invalid means uncalibrated".
- `test_vision_proxy_study.py`: the sampling frame of the vision study (correct
  zeros and ambiguous attributions excluded).
- `test_trade_queue.py`: the informativeness ranking. Fisher term, coverage
  down-weighting, no-signal pairs scoring 0.
- `test_orphan_feedback.py`, `test_historical_mark.py`: the orphan archive round
  trip, and a stale mark rendering as historical in agreement with the learner.
- `test_trade_pre_regeneration*.py`: the selective regeneration. Closed key set
  and digest, abort on any human signal, bundle integrity, authorizer, rollback,
  verify and the copy-only policy.
- `test_ingest_status.py`, `test_auto_ingest_status.py`,
  `test_account_puuid_refresh.py`, `test_db_backup_rotation.py`: run states
  independent of key health, PUUID re-resolution with the cache as fallback, and
  backup rotation that never touches manual backups.
- `test_download_champion_icons.py`: the icon downloader. Catalogue filtering,
  rejection of malicious ids, path containment, symlinks, PNG header validation,
  atomic writes and idempotence.
- `test_exposure.py`, `test_anchor_eval.py`, `test_policy_delta.py`: the
  read-only studies.

Nothing in the suite requires network access. On a fresh clone 627 pass and 23 are skipped: 18 compare the extracted trade scanner against a commit that only exists in the private working history, 4 need permission to create symlinks (Windows), and 1 needs a populated database. Without a `config.toml`, the test setup points the dashboard at a throwaway synthetic config (`LOL_COACH_CONFIG`) outside the repo, so the dashboard tests still run.

---

## Safety invariants

1. `config.toml` (the Riot API key) is never committed. Raw gameplay JSON and the
   SQLite DB are never committed. `git` is the safety net.
2. A daily DB backup runs even when the API key is dead, because user feedback
   lives only in the DB. Rotation keeps 7 and considers only daily backups
   (`lol_coach_YYYYMMDD.db`). Manual checkpoints taken before risky operations
   are evidence and are never rotated away.
3. Re-analysis goes only through `analyze.py` / `sync_ascent`
   (`persist_decisions`). Never `DELETE FROM decisions` by hand. Historical rows
   are rewritten in place only by the selective regeneration tool, and only on a
   registered copy.
4. Fixed order: detectors → reward → clips. Skipping clips orphans them.
5. `analyzed_at_utc` is the "done" marker.
6. Ranked only; normals and remakes are tombstoned.
7. `current_gold` is spendable gold (readiness/power); `total_gold` is a stat
   only.
8. Ward events with `wardType UNDEFINED` are not wards and are always filtered.

---

## Entry points

| Command | Purpose |
|---|---|
| `scripts/sync_ascent.py [--all]` | full pipeline (capture + analysis + clips) |
| `scripts/analyze.py [match \| --all]` | re-analyze, preserving feedback |
| `scripts/dashboard.py [--port 8766]` | dashboard server (default port 8766) |
| `scripts/ingest_reference.py "Name#TAG" --champion X` | ingest a reference player |
| `scripts/download_game_data.py` | fetch static game data (patch-versioned) |
| `scripts/trade_feedback_queue.py` | trade marks to make next, ranked by informativeness |
| `scripts/orphan_feedback.py [--json]` | render the orphan-feedback archive |
| `scripts/anchoring-check.py [--since DATE] [--detector ID]` | does the UI dictate the answer? (read-only) |
| `scripts/migrate_trade_keys.py [--apply]` | key migration for the trade_v1 timing fix (dry-run by default) |
| `scripts/regenerate_trade_pre.py prepare \| dry-run \| apply-copy \| verify \| rollback-copy` | selective PRE regeneration, **registered copy only** |
| `scripts/exposure_anchor_set.py` | build the blind anchor set to label |
| `scripts/exposure_validation.py` | validate exposure_v1 against the labeled anchors |
| `scripts/attention_experiment.py` | pre-registered attention experiment, stage 1 |
| `scripts/policy_delta.py` | action distribution, own accounts vs reference cohort |
| `.venv\Scripts\python.exe -m pytest` | run the test suite |

Everything below `sync_ascent.py` and `analyze.py` in that list is read-only,
with two exceptions:

- `migrate_trade_keys.py --apply` backs the database up before writing and
  refuses to run twice (it records itself in `applied_migrations`).
- `regenerate_trade_pre.py` writes only to the trial copy that its own `prepare`
  step created.
