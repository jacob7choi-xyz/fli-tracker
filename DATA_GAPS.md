# Data Collection Gaps and Provenance

This document explains known gaps in the price-snapshot archive. The
authoritative evidence is the archive shards and the immutable `runs/`
manifests. `coverage.csv` is a deterministic rollup derived from them, and it
is the file training code should consult rather than this prose, which is a
narrative snapshot. The rollup regenerates after every sweep and can be
rebuilt from the evidence at any time.

Regenerate at any time from a checkout of the `data` branch:

    ARCHIVE_DIR=data/archive RUNS_DIR=data/runs COVERAGE_OUT=coverage.csv \
        uv run python scripts/generate_coverage.py

## Failure-domain hierarchy

Every design decision in the collection pipeline follows this hierarchy.
Failure propagates upward in visibility, never in destructive authority: a
lower tier failing may turn a workflow red, but may never block or destroy
a higher tier.

- **Tier A - archive shards** (`archive/date=.../group=.../run=...csv.gz`):
  the authoritative ML dataset. Immutable, append-only, published before
  any other persistence.
- **Tier B - tracker.db**: important operational state. Routes, alerts,
  and notification-suppression history exist only here; loss does not
  erase observations but requires operational restoration and alters
  alerting behavior until restored.
- **Tier C - notifications**: side effects. Configured at runtime via the
  NOTIFY_URL environment variable; failure is surfaced, never allowed to
  gate Tier A or B.

## Accepted residual risks

Explicitly accepted, none labeled impossible. Each names its mechanism and
bound; none may be silently "fixed" into a stronger claim than the
mechanism provides.

- **Runner loss before publication**: if the CI runner dies before the
  archive push, that sweep's observations are lost. Bounded to one sweep;
  identical to the pre-incident exposure. Closing it requires incremental
  external checkpointing during collection (deliberate non-goal).
- **Delivery/dedup race**: notification submission succeeds, runner dies
  before the dedup row commits; the next sweep may resend the same fare.
  Accepted under the governing bias: false duplicate over false
  suppression.
- **Downstream email bounce**: the SMTP transaction is accepted, the
  suppression row is written, and the message later bounces. The
  application never learns; that exact fare event stays suppressed.
  Accepted as low-risk and unobserved by design; closing it would require
  bounce processing (webhook or mailbox monitoring), disproportionate for
  this system.
- **Concurrency queue saturation**: the shared writer group can queue up
  to the platform cap of 100 pending runs; work beyond the cap may be
  rejected. The 20-minute job timeouts bound individual executing sweeps
  but do NOT bound platform queue delay: saturation requires sustained
  inability to drain the queue relative to the sweep arrival rate
  (derived: at the current 12 scheduled runs/day, filling the cap implies
  roughly 8 days of zero drainage) and remains an accepted
  platform-availability residual.
- **Operator collision fails a sweep closed**: the data branch has one
  serialized automated writer (the shared concurrency group); the push
  loops classify failed pushes by SHA identity and treat a remote tip
  differing from both the run's starting tip and its local commit as a
  violated writer contract - red run, no automated reconciliation, that
  sweep's observations lost (bounded to one sweep, visible). A manual
  push during a live sweep window triggers this by design; re-dispatch
  the sweep after investigating.
- **Platform pre-execution loss.** A scheduled run can produce no data before
  any of this repository's code executes, in two distinct ways observed on
  2026-08-06. In the first, the run was created but no hosted runner was ever
  acquired, so it was cancelled after 15 minutes with zero steps recorded; the
  in-job failure email cannot fire in that state because no step runs, leaving
  a red run as the only signal. In the second, no workflow run was created at
  all for the expected slot, which leaves no red run, no notification, and no
  manifest. GitHub documents that scheduled events may be delayed or dropped
  under high Actions load, which is a plausible explanation, but repository
  evidence cannot prove the cause for a specific slot. Both reduce to the same
  repository-visible fact, an expected observation that never arrived, and both
  are detectable only by comparing the expected-slot grid against observed
  attempts. Accepted platform-availability residual; the coverage rollup is the
  detector.
- **Parse failure aborts the sweep**: a search response that returns but
  fails parsing stops the sweep loudly (unexpected upstream contract
  drift deserves visibility, not per-unit recovery). Already-collected
  routes stay persisted, the attempt publishes as partial, the run goes
  red. Downgrading specific parse failures to per-unit recovery is a
  follow-up decision, not incident scope.

## The 2026-06/07 collection outage

Root cause: `notification_log.message` stored the full digest email HTML
(~42 KB average) once per triggered alert per sweep, was never pruned, and
grew to 100.6 MB of a 104.7 MB tracker.db. GitHub rejects files over
100 MiB, and the shard and DB were committed together on an ephemeral
runner, so each rejected push destroyed that sweep's shard as well. The
failure was intermittent from 2026-06-02 (whenever VACUUM happened to
squeeze the file under the limit, the push succeeded) and total from
2026-07-17 04:09 UTC. Remediated 2026-07-23; the fixes are the archive-
first push, the constant-marker notification log, notification-log
pruning, DB size gates, and runtime-only credentials.

## Coverage by period (from coverage.csv, 2026-07-23)

A slot is one scheduled sweep: 4 per day per group (domestic, coastal,
longhaul), 12 per day total once all groups were live.

| Period | Slots observed | Slots missing | Notes |
|---|---|---|---|
| 2026-04-12 to 2026-05-07 | 78 day-level rows | 0 | Backfill import, day granularity, status `backfill` |
| 2026-05 (live sweeps) | 282 | 6 | 97.9% coverage |
| 2026-06 | 181 | 179 | 50.3% coverage; intermittent push failures |
| 2026-07-01 to 2026-07-17 | 38 | 120 (whole month) | 24.1% coverage; total failure after 07-17 04:09 UTC |
| 2026-07-17 to 2026-07-23 | 0 | (in July count) | Workflows disabled 07-23 for remediation |

Since restoration on 2026-07-24T13:05Z: 206 slots collected, 2 missing. Both
fell on 2026-08-06 and both were platform pre-execution losses rather than code
failures. The coastal 14:00Z run was created but never acquired a runner. No run
was created at all for the domestic 18:00Z slot. Slots before 13:05Z on 07-24
are labeled `maintenance` rather than `missing` because collection was
deliberately disabled during remediation.

The missing slots are permanently unrecoverable: the data existed only on
destroyed CI runners.

## Interpreting provenance status

- `complete`: a `runs/` record exists and every intended collection unit
  returned (a search returning zero fares is a completed observation; a
  network/API/parser failure is not).
- `partial`: a record exists but some units did not complete, or the sweep
  died before writing its explicit result. The shard's rows are valid
  observations; the slot is just not a full sweep.
- `pre-manifest`: shard predates per-attempt provenance (before
  2026-07-23). Completeness is unknown and deliberately not guessed;
  historically some "successful" sweeps silently collected fewer units
  than intended, because the scanner swallows per-search failures.
- `missing`: no evidence for a slot inside the group's live range, and the
  slot is not inside a declared maintenance window. Covers both a failed run
  and a run that was never created; the rollup records the absence and does
  not guess the cause.
- `maintenance`: collection was deliberately disabled for this slot, so the
  absence is planned. Kept distinct from `missing` so a reader of the rollup
  alone can tell an intentional pause from an unexplained loss.
- `backfill`: day-granularity import rows from before live sweeps.

Multiple attempts for one slot (re-runs, manual dispatches) remain
separately visible in `runs/` and are extra observations, not corruption.
