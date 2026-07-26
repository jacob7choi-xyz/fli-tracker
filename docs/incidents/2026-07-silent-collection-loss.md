# Postmortem: Seven Weeks of Silent Collection Loss

**Status:** Resolved 2026-07-25
**Impact:** About 55% of June and 76% of July price observations permanently lost
**Detection:** 51 days after the first failure, during manual inspection. Failure emails were delivered throughout and ignored
**Author:** Jacob Choi
**Document status:** Draft. Sections 4 and 7 are not yet written.

## 1. Summary

A scheduled price collection pipeline stopped persisting data on 2026-06-02 and
failed completely from 2026-07-17. It appeared healthy the entire time. Sweeps
ran on schedule, prices were collected, and deal alert emails arrived in my
inbox every six hours. The only broken step was the last one, where results were
written back to storage.

The pipeline collects data into a SQLite database and an append-only CSV
archive, then commits both to a git branch. A logging table inside that database
was storing the full HTML body of every alert email ever sent. Nothing pruned
it, and it grew to 100.6 MB of a 104.7 MB file. GitHub rejects any file over
100 MiB, so every push was refused. The archive shard and the database were
committed together, so each refused push destroyed that sweep's observations
along with it, and the runner holding them was destroyed minutes later.

Failure notifications did exist. GitHub emails the owner of a scheduled workflow
when a run fails, and it did so every time. I received them and stopped reading
them. See section 4.

## 2. Timeline

| Date (UTC) | Event |
|---|---|
| 2026-04-12 | Collection begins. `notification_log` starts accumulating email bodies |
| 2026-06-01 | Last date with full collection coverage |
| 2026-06-02 | First push rejections. The database crosses 100 MiB, and `VACUUM` intermittently pulls it back under, so failures are sporadic |
| 2026-06 | Roughly half of sweeps fail. Deal emails continue uninterrupted |
| 2026-07-17 04:09 | Last successful push. The database can no longer be squeezed under the limit |
| 2026-07-17 to 07-23 | Total failure. 99 of the last 100 runs red |
| 2026-07-23 | Discovered during manual inspection. All three workflows disabled |
| 2026-07-23 | Root cause identified. Database remediated from 104.7 MB to 1.17 MB |
| 2026-07-24 | Fixes merged, fault injection performed, collection restored |
| 2026-07-25 | Credential history purged. Incident closed after 14 consecutive green cycles |

## 3. Root cause

Two independent defects combined to produce data loss.

**Unbounded logging in a size constrained store.** The notification
deduplication table recorded a `message` column alongside its structural
columns. The digest path wrote the entire rendered HTML email, averaging 42 KB
and peaking at 203 KB, once per triggered alert per sweep. Deduplication never
read that column. It matched only on `(alert_id, departure_date, return_date,
price)`, so the stored text was pure overhead. Nothing pruned it. At detection
the table held 100.6 MB against 1.4 MB of actual price snapshots.

**Coupled persistence of unequal value data.** The authoritative archive shard
and the operational database were staged and committed in a single git
transaction. A push rejected because of database size therefore discarded the
archive shard as well. These two artifacts have very different loss tolerance.
The archive is irreplaceable observation data and the database is rebuildable
operational state, but the pipeline treated them as one unit.

An aggravating factor, and the reason this lasted seven weeks rather than a day,
was that the failure signal reached me and I ignored it. That is covered in
section 4.

## 4. Why it went undetected for seven weeks

*Not yet written.*

## 5. What changed

**Failure domain separation.** Subsystems are now classified by loss tolerance.
Failure may propagate upward in visibility but never in destructive authority.

* Tier A, the archive shards, holds authoritative observations. Immutable,
  append-only, and published before anything else.
* Tier B, `tracker.db`, holds operational state such as routes, alerts, and
  suppression history. Losing it is recoverable inconvenience.
* Tier C, notifications, are side effects and may never gate A or B.

**Archive first publication.** The shard and its provenance record commit and
push in their own transaction before the database is touched, so a database
failure can no longer cost observations.

**Size gates.** A warning fires at 50 MiB. At 90 MiB the database snapshot is
refused outright while the archive stays durable and the run goes red. The limit
can no longer be discovered by hitting it.

**Bounded logging.** The `message` column now stores a constant marker.
Deduplication reads only structural columns, so nothing is lost. Pruning removes
rows that are provably dead for deduplication, meaning departed trips and rows
with a NULL departure date, which SQL equality can never match.

**Delivery truthful suppression.** Deduplication rows are written only after
confirmed delivery. Previously a failed send still recorded "already notified,"
which would have permanently silenced that fare. The governing bias is false
duplicate over false suppression.

**Fail closed publication.** Push failures are classified by SHA identity rather
than exit code. Remote equal to local means the push landed and the response was
lost. Remote equal to the starting tip means a transient failure. Anything else
means the single writer contract was violated, and the run fails closed for a
human to investigate. No automated reconciliation and no history rewriting
exists on any path.

**Per attempt provenance.** Every run writes an immutable record containing its
scheduled slot, sweep window, shard checksum, and an explicit
`collection_status`. Completeness comes from comparing expected against
completed collection units, never from the process exit code, because the
scanner deliberately swallows individual search failures and exit 0 therefore
proves nothing. The existence of a shard never implies a complete observation
window.

**Loud failure.** Runs go red on any failure and send email. The failure path
was verified by deliberate fault injection rather than assumed.

**Runtime only credentials.** The notification URL resolves from the environment
at send time and fails closed when absent. No credential is persisted in
application state, displayed by the CLI, or written to logs. Tests assert its
absence from database bytes, stdout, stderr, log records, and exception text.

**CI merge gate.** 526 deterministic tests plus lint run on every pull request
in about 40 seconds, with pinned action SHAs, a pinned uv binary, a verified
lockfile, read only tokens, and a check that the production dependency set is
independently sufficient.

### Verification rather than assertion

Both destructive paths were deliberately triggered in production.

Sabotaging notification produced a completed collection, a published archive and
database, a red run, and zero deduplication rows written for 47 triggered
alerts, proving those fares stayed eligible for retry.

Sabotaging the database push left the archive and provenance durable on the
remote, wrote no database commit, turned the run red, and delivered the alarm.
That is the exact June failure recreated, now costing one red badge instead of a
sweep.

## 6. Data loss and residual risks

The following observations are permanently unrecoverable, because they existed
only on ephemeral CI runners that were destroyed after each rejected push.

| Period | Slots observed | Slots missing | Coverage |
|---|---|---|---|
| 2026-04-12 to 2026-05-07 | 78 (backfill) | 0 | complete |
| 2026-05 | 282 | 6 | 97.9% |
| 2026-06 | 181 | 179 | 50.3% |
| 2026-07-01 to 07-17 | 38 | 120 | 24.1% |

The gap is documented per slot in a machine readable coverage file, so future
analysis can distinguish "no observation" from "no deal," and can weight windows
by trustworthiness instead of assuming uniform quality.

Accepted residual risks, each naming its mechanism and bound. None is claimed
impossible.

* **Runner loss before publication** costs at most one sweep. Closing it would
  require incremental external checkpointing during collection.
* **Delivery and deduplication race**, where submission succeeds and the runner
  dies before the row commits, may cause the next sweep to resend. Accepted
  under the false duplicate bias.
* **Downstream email bounce**, where SMTP accepts the message, the row is
  written, and the message later bounces. Unobserved by design.
* **Operator collision**, where a manual push during a live sweep fails that
  sweep closed. Bounded to one sweep and visible.
* **Concurrency queue saturation**, where the shared writer group holds up to
  100 pending runs and rejects work beyond that.

## 7. Lessons

*Not yet written.*
