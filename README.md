# RiftPattern

**A League of Legends coaching engine built for one player: behavioral analysis
of my own ranked games on the climb to Challenger.**

Most stats sites tell you *what* happened (KDA, gold, win rate). RiftPattern tries
to tell you *what you should have decided, and why*. It ingests my ranked games,
detects the individual **decision points** inside each one, evaluates the choice
I actually made against the alternatives, and produces a written argument. That
argument is not a flag or a number: it is context, options with expected value,
and prose. My feedback on those arguments trains a preference model that
recalibrates how the engine weighs each factor.

The bet behind it: climbing out of high elo is a *behavioral* problem
(consistency), not a mechanical one. The engine's job is to surface the mistake
you keep repeating while knowing it's wrong. For my account, the measured
dominant pattern is **over-extending while ahead**.

> Built as a real tool I use on my own account, not a demo. The dashboard and the
> generated coaching prose are in Spanish (I'm the target user). The code and
> docs are in English.

**By the numbers** (measured 2026-09-25; own games only, the reference cohort is
excluded):

| Own ranked games | Decision points | Human marks | Active detectors | Tests |
|---:|---:|---:|---:|---:|
| 310 | 4,407 | 505 | 4 | 650 |

The dashboard's review list shows 3,741 of those decision points: trades the
engine scores as won are still detected and stored, but hidden from review.

---

## Screenshots

> The dashboard UI is in Spanish (I'm the target user). The captions below
> explain each view.

**Recent matches.** The dashboard opens on recent games rather than on a raw
list of decisions. Each row shows the champion portrait, K/D/A, age, duration,
review progress and how the match's decision points split across the four
detectors. A flat list of all decisions stays available as a secondary view,
with its filters.

![Recent matches: portrait, KDA, age, review progress and the detector mix per match](docs/img/partidas.png)

**Each decision becomes an argument, not a flag.** An expanded decision is a
four-stage review: *01 Program's analysis*, *02 Available evidence*, *03 Your
reasoning*, *04 Calibration / result*. The option picker, at the end of stage
02, is neutral: it shows no EV, no bar and no "recommended" badge. The EV
breakdown and the later outcome only appear in stage 04 once an option has been
selected. The picker is only half of the fix, though: for `death` and `tempo`,
the program's own prose in stage 01 still names the highest-EV option (visible
in the screenshot below; see [Known limitations](#known-limitations)).

![An expanded decision: the program's analysis, the evidence, your reasoning and the calibration](docs/img/decision.png)

**What was on screen.** Inside the evidence stage, a strip of minimap crops is
taken from the recording of the game itself, at a few seconds before the
decision. The labels depend on the detector: −30 s, −10 s and the event for a
death or a trade; −90 s, −45 s and −15 s before an objective. The crops show fog
of war, wards and icons that Riot's API does not expose. Each crop can be
enlarged. It shows what was *available* on screen, not what I was looking at.

![Minimap strip: crops of the recorded minimap at fixed offsets before the decision](docs/img/minimapa.png)

**Honest progress measurement.** The dominant pattern is tracked over a moving
window with a Wilson confidence interval. When the intervals overlap, the verdict
is "no distinguishable change yet" rather than a cherry-picked win. Below it, a
criterion that failed its blind validation stays on screen, labelled "criterio
NO validado" with the numbers that sank it, instead of being quietly removed.

![Behavioral-progress card with its Wilson band, and the exposure card labelled as not validated](docs/img/patrones.png)

**A curated review session.** An anti-bias mix of unmarked moments: the dominant
pattern by worst EV gap, an expensive objective, a hard correct call, and
variety. The point is to review without doomscrolling.

![Today's session: a curated set of moments to review](docs/img/sesion.png)

---

## How it works

```
Riot API + Live Client capture + game recordings  ->  4 decision detectors
        ->  evaluation (HEF)  ->  options with EV + written argument
        ->  human feedback  ->  Bradley-Terry preference learner  ->  weights
```

1. **Capture.** A 1 Hz poller reads the in-game Live Client API for exact
   per-second state (HP, mana, gold, position). Riot's Match-V5 timeline
   backfills the rest, at one frame per minute. Game recordings (video plus an
   input-event log) are linked to matches for clips and the minimap strip. Only
   ranked games are ingested. Normals and remakes are tombstoned so they are
   never fetched again.
2. **Detect decisions.** Four detectors turn a match into discrete decision
   points:
   - `death` covers every death, classified by the real action (over-extension,
     failed disengage, calculated sacrifice, …).
   - `trade` covers bilateral bot-lane damage exchanges during laning.
   - `tempo` covers fights the team took without me.
   - `objective_readiness` asks whether the team was actually set up for that
     dragon or baron.
3. **Evaluate.** A hierarchical evaluation function (HEF) scores each state on
   `power`, `info`, `action_fit` and `wave`. Each option gets an expected value.
4. **Argue.** Each decision becomes an argument: what I did, what was optimal,
   and why. The alternative is always spelled out, never just "you played it
   wrong". The prose comes from **deterministic per-detector templates** that
   read the same structured row the EVs come from. There is no generative model
   in the loop, so the same row always yields the same text, the text can be
   tested like any other output, and it cannot claim something the row doesn't
   contain.
5. **Learn.** I mark decisions (agree / equivalent / disagree, which option was
   actually right, and why I'm marking). A Bradley-Terry preference model learns
   HEF weights from those marks. It is cross-validated and sits behind a
   guardrail, so the engine's judgment can track mine over time. So far the
   learned weights have not beaten the defaults out of sample, and the defaults
   stay active.
6. **Measure progress.** A progress view tracks the dominant pattern over a
   moving window with Wilson confidence intervals. That lets me answer "am I
   actually improving?" without fooling myself with outcome bias.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

---

## Design decisions worth calling out

These are the parts I'm most proud of:

- **Stdlib-only, on purpose.** Python standard library + `requests`, React over
  UMD as a single `dashboard.html` with no build step, and SQLite in WAL mode. No
  FastAPI, Vite or ORM. The constraint keeps the whole system small and
  inspectable. I only add a framework when a concrete pain justifies it.
- **Feedback survives re-analysis.** Re-running the detectors deletes and
  reinserts every decision. Human feedback is preserved by a stable key, and
  marks are validated by a semantic *fingerprint* of the option set rather than a
  brittle index or timestamp. Refactoring a detector therefore doesn't silently
  throw away months of labeling.
  - When a detector stops firing where it used to, the orphaned marks are
    *archived* rather than dropped. Those notes are the field evidence I diagnose
    the detectors with.
  - A mark that no longer matches the current options still shows on the card,
    labeled as historical, and it is listed in the Review queue so it can be
    redone.
- **Options are stated from the prior context, never from the result.** A
  decision should offer one real axis (trade or don't, go for this objective or
  take a higher-value play), written from what was knowable at that instant.
  Phrasing an option from the outcome is outcome bias dressed as coaching, and it
  corrupts the preference model. The rule is applied to `trade` and to the
  objective priority axis. The rest is pending (see
  [Known limitations](#known-limitations)).
- **The UI must not dictate the answer.** A read-only check
  (`scripts/anchoring-check.py`) measures how often the option I mark as right
  coincides with the system's max-EV option.
  - With the old picker, which showed EV and the recommendation while I chose,
    the match rate was 85% (273/323 when first measured; the script reproduces
    84.4%, 190/225, over the marks that are still valid). The HEF's own measured
    accuracy is about 52%, so the learner was being trained on a copy of itself.
  - After neutralizing the picker, the first batch (all trades, whose prose
    names no option) matched 3/9 = 33% (Wilson 95% CI 12.1–64.6%). That is below
    the 60% suspicion threshold, but with n = 9 the interval still reaches
    64.6%. It is an indication, not a confirmation.
- **I/O separated from pure logic** in every detector. `_gather_facts` does all
  the database work and `_evaluate` is pure, which makes the criterion branches
  unit-testable without a database.
- **Honest measurement, built to refute.**
  - Progress is reported with confidence intervals.
  - A pre-registered experiment is sealed in git before it runs.
  - A detector was validated against an anchor set that was generated blind.
  - A headline number the project had been quoting ("70% of objectives had no
    vision set up beforehand") was checked against video and
    [withdrawn as a measurement artifact](docs/experiments/vision-proxy-validation.md).
    The real rate is 42.9%.
- **Historical rewrites run only on a registered copy.** Correcting the state
  that past trade decisions were evaluated on is done by a tool that edits a
  closed, digest-checked set of rows, two columns only, and only on a registered
  copy of the database. It has been rehearsed with an exact rollback. It has
  **not** been applied to the production database.
- **650 characterization tests** pin the behavior as a safety net for
  criterion refactors.

---

## Known limitations

- **The recommendation still leaks before the choice for `death` and `tempo`.**
  Their stage 01 prose ends with the highest-EV option and its EV, and the HEF
  verdict ("decisión pobre") is shown in stage 01 and on the collapsed row. The
  neutral picker only removes the recommendation from the picker itself. Marks
  on these two detectors should not be read as independent of the system.
- **The recommendation appears on selection, not on save.** The stage 04
  breakdown renders as soon as an option is *selected*, before the choice is
  saved. A different option can be chosen afterwards, so no independent first
  choice is recorded.
- **Editing a note can revive a legacy mark.** For old marks made before
  fingerprints existed, validity falls back to the mark's timestamp. Editing only
  the note refreshes that timestamp and can make a stale mark count again.
- **Older option vocabulary in `death` and most `objective` branches.** These
  still label one option "(what you did)" and some are phrased from the outcome.
  Migrating them invalidates existing marks, so it is deferred.
- **Possible hindsight in some menus and EVs.** A few branches may condition the
  offered options or their EVs on what happened afterwards. How often this
  happens in the stored data has not been measured yet.
- **The trade PRE regeneration is not applied to production.** Past trade
  decisions are still evaluated on the old, up-to-a-minute-stale state until that
  step is authorized and carried out.
- **Timeline positions are per minute.** Match-V5 gives one position frame per
  minute and no ward positions at all, so any positional inference from the
  timeline is approximate.
- **One player's sample.** Some questions cannot be settled with a single
  player's data. The exposure study was archived after failing its blind
  validation (AUC 0.477). The comparison against the reference player
  ([policy delta](docs/experiments/policy_delta.md), in Spanish) closed as null
  for lack of power.

---

## Setup

Requires Python 3.12+ and a free Riot API key.

```bash
# 1. install
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt   # for tests

# 2. configure
copy config.example.toml config.toml
#   then edit config.toml: add your Riot API key (https://developer.riotgames.com)
#   and your account(s). config.toml is gitignored so your key stays local.

# 3. fetch static game data (champion/item data, patch-versioned)
.venv\Scripts\python.exe scripts\download_game_data.py

# 4. run the pipeline, then open the dashboard
.venv\Scripts\python.exe scripts\sync_ascent.py --all
.venv\Scripts\python.exe scripts\dashboard.py        # http://localhost:8766

# tests
.venv\Scripts\python.exe -m pytest
```

The suite needs no network. 650 tests in total. On a fresh clone 627 pass and 23 are skipped: 18 compare the extracted trade scanner against a commit that only exists in the private working history, 4 need permission to create symlinks (Windows), and 1 needs a populated database. Without a `config.toml`, the test setup points the dashboard at a throwaway synthetic config (`LOL_COACH_CONFIG`) outside the repo, so the dashboard tests still run.

The minimap strip needs a one-time calibration per recording source. You drag a
rectangle over the minimap on a sample frame, and the result is stored in
`data/minimap_crop.json`. An uncalibrated source shows a "not calibrated" notice
instead of a guessed crop.

---

## Disclaimer

RiftPattern isn't endorsed by Riot Games and doesn't reflect the views or opinions
of Riot Games or anyone officially involved in producing or managing Riot Games
properties. Riot Games and all associated properties are trademarks or registered
trademarks of Riot Games, Inc.

## License

[MIT](LICENSE)
