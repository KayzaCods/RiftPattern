# Changelog

Published versions of RiftPattern, newest first. Each entry is one public
release; numbers are measured at release time.

## 2026-10-27 — Loss patterns

- New read-only analysis `lol_coach/loss_patterns.py` + `scripts/loss_patterns.py`
  (SQLite opened with `mode=ro` and `PRAGMA query_only`; not a detector, writes
  no decisions). It measures win rate by team gold at minute 14, by own deaths
  before minute 14 and by chained early deaths (Wilson CIs); own-death rates per
  10 minutes in losses vs wins with a per-game bootstrap; death risk near
  objective pits by team-gold band; and how well visible in-game signals (kills,
  towers, summed levels, "2 of 3") flag a −3000 team-gold deficit.
- Experiment report, in Spanish:
  [docs/experiments/2026-10-27-patrones-de-derrota.md](docs/experiments/2026-10-27-patrones-de-derrota.md).
  Over 291 games, what held up is the link between the result and team gold at
  minute 14, own deaths before minute 14 and chained early deaths. Causes read
  from video (early enemy roams, late objective deaths) did not replicate with a
  second independent rater (55–65% agreement) nor with Riot event data, and the
  data does not show a higher risk of dying at the pit when behind.
- Tests: 690 total; on a fresh clone 667 pass and 23 are skipped.

## 2026-09-27 — Context-first options

- `death_v1` and `objective_readiness_v1`: one decision axis per situation, with
  EV derived from the local setup instead of fixed per-branch values.
- Dashboard: the comparison with the highest-EV option and the HEF verdict stay
  hidden until an option is saved.
- In-place re-analysis of a single detector: key-set equality, column
  allowlist, dry run by default, backup before writing.
- Configuration via `LOL_COACH_CONFIG`. Tests: 678 total; on a fresh clone 655
  pass and 23 are skipped.

## 2026-09-26 — Minimap evidence and vision-proxy study

- Minimap strip: crops of the recorded minimap at fixed offsets before each
  decision, with per-source calibration.
- Vision pre-objective proxy validated against video: the "70% without vision"
  figure was a measurement artifact; the real rate is 42.9% [39.1–47.2]
  ([report](docs/experiments/vision-proxy-validation.md)).
- `trade_v1`: PRE state anchored to the real start of the damage block; duo
  co-presence guard.
- Ingestion reports completed / partial / failed independently of key health.
- Docs: new "Known limitations" section; the argument prose is documented as
  produced by deterministic templates.

## 2026-08-06 — RiftPattern: match-first review

- The dashboard opens on recent matches; picking one shows a persistent match
  summary before its decisions.
- The option picker no longer reveals the system's recommendation while you
  choose.
- Champion, item and rune icons are downloaded once, verified by SHA-256 and
  served locally.

## 2026-08-05 — Exposure study and trade timing fix

- Read-only studies: exposure states, blind anchor-set validation, a
  pre-registered attention experiment and the policy delta vs a reference
  cohort ([report](docs/experiments/policy_delta.md)).
- `trade_v1`: decisions sealed at the start of the damage interval, and the
  skip gate excludes only the player's own death.
- Options carry an explicit `taken` flag and state one axis written from the
  context before the play; orphaned marks are archived instead of dropped.

## 2026-07-07 — First public version

- Behavioral coaching engine for League of Legends: ingestion from the Riot API,
  detectors that argue decisions with EV options, and a local review dashboard.
