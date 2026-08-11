"""Publish Tier-B artifacts to the data branch in a single commit.

Tier B is tracker.db (operational state) and coverage.csv (derived rollup).
Both are published by ONE commit and ONE push after the archive shard and its
manifest are already durable on the remote.

Why one commit rather than two independent publishers
-----------------------------------------------------
Both artifacts live on one linear git ref, so two sequential publishers share
mutable local branch state even when the workflow draws them as siblings. A
publisher whose push fails leaves an unpushed commit that the next publisher
silently parents on, which means the second push can publish the first one's
"failed" commit, and the first one's commit becomes a bogus BASE_SHA that makes
the identity check misclassify an unmoved remote as a violated writer contract.

Publishing once removes that entire class: there is exactly one post-archive
mutation, so the expected parent is the verified archive tip by construction.

The accepted trade is that a push failure withholds both artifacts together.
That is deliberate, not incidental. Two sequential publishers really could
land one and fail the other; we give that up because both artifacts are
reconstructible (tracker.db is rewritten by the next sweep, coverage.csv
regenerates from the archive) while Tier A is already durable.

Failure semantics, in this order
--------------------------------
An artifact that failed preparation is withheld, never staged, and never
inferred from what happens to be on disk: a crashed generator can leave a
perfectly plausible partial file. Withholding one artifact must not withhold
the other, so publication of everything valid happens FIRST and the non-zero
exit reporting withheld artifacts happens LAST.

Eligibility is enforced at two independent levels because they answer
different questions. ALLOWED_PATHS asks whether an artifact may EVER be
published by this script; the per-invocation eligible set asks whether it is
authorized to enter THIS commit. Checking only the former lets a withheld
artifact be published whenever something upstream happens to have staged it,
which reports the artifact as withheld and publishes it in the same run. The
publisher also refuses to start on a dirty index, so authorization cannot be
smuggled in as inherited state.

Remote identity is verified before staging, not discovered after committing.
The archive push proved the remote tip at that moment; this is a later moment,
and an out-of-band writer between the two steps must fail closed rather than
be reconciled. Unknown remote state is never permission to write.

Environment variables:
    DATA_DIR         -- data-branch checkout (default: cwd)
    COVERAGE_READY   -- "true" only if generation SUCCEEDED (step outcome,
                        never file existence)
    COMMIT_LABEL     -- sweep group name used in the commit message
    DB_WARN_MIB      -- warning threshold, default 50
    DB_HARD_MIB      -- refusal threshold, default 90
    PUBLISH_RETRY_SLEEP -- seconds between push retries, default 5
    SIMULATE_PUBLISH_FAILURE -- fault injection: fail the push after commit
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

# Nothing outside this set may ever enter a Tier-B commit. Asserted against the
# real staged tree before committing, so a future workflow edit that stages
# something else fails loudly instead of quietly publishing it.
ALLOWED_PATHS = frozenset({"tracker.db", "coverage.csv"})

MIB = 1024 * 1024


class PublishResult(StrEnum):
    """Outcome of one push attempt, classified by remote identity only."""

    PUBLISHED = "published"
    REMOTE_UNCHANGED = "remote_unchanged"
    UNEXPECTED_REMOTE = "unexpected_remote"
    UNVERIFIABLE = "unverifiable"


class PublishError(RuntimeError):
    """Publication could not proceed safely; the caller must fail closed."""


def git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run one git command with no shell, in an explicit working directory."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, shell=False, explicit cwd
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise PublishError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def head_sha(cwd: Path) -> str:
    """Return the current local HEAD."""
    return git(["rev-parse", "HEAD"], cwd).stdout.strip()


def verify_remote_parent(cwd: Path, expected: str) -> str:
    """Confirm origin/data is still exactly the tip we intend to parent on.

    Returns the remote SHA. Raises PublishError when the remote moved or when
    its state cannot be established, because both mean this run no longer knows
    what it would be writing on top of.
    """
    fetch = git(["fetch", "origin", "data"], cwd, check=False)
    if fetch.returncode != 0:
        raise PublishError(
            "Cannot verify the data branch tip before publication "
            f"(git fetch failed: {fetch.stderr.strip()}). Refusing to write "
            "with unverifiable remote state."
        )
    remote = git(["rev-parse", "FETCH_HEAD"], cwd).stdout.strip()
    if remote != expected:
        raise PublishError(
            f"Data branch tip {remote} is not this run's verified archive tip "
            f"{expected}. Another writer moved the branch between the archive "
            "push and Tier-B publication; the serialized-writer contract was "
            "violated. Refusing automated reconciliation."
        )
    return remote


def assess_db(cwd: Path, warn_mib: int, hard_mib: int) -> tuple[bool, str | None, str | None]:
    """Decide whether tracker.db may be published.

    Returns (eligible, withheld_reason, warning). The hard gate exists so DB
    size can never again threaten publication: GitHub rejects at 100 MiB.
    """
    db = cwd / "tracker.db"
    if not db.exists():
        return False, "tracker.db is missing from the data checkout", None
    size = db.stat().st_size
    if size >= hard_mib * MIB:
        return (
            False,
            f"tracker.db is {size} bytes (>= {hard_mib} MiB safety limit); "
            "snapshot withheld, archive already durable",
            None,
        )
    warning = None
    if size >= warn_mib * MIB:
        warning = (
            f"tracker.db is {size} bytes (>= {warn_mib} MiB); "
            f"investigate growth before the {hard_mib} MiB gate"
        )
    return True, None, warning


def stage(cwd: Path, paths: list[str]) -> bool:
    """Stage an explicit path list. Never `git add -A`, never `git add .`."""
    for path in paths:
        git(["add", "--", path], cwd)
    return bool(git(["diff", "--cached", "--name-only"], cwd).stdout.strip())


def require_clean_index(cwd: Path) -> None:
    """Refuse to inherit index state this invocation did not create.

    Structural precondition rather than a convention upstream steps must
    remember forever: anything already staged when the publisher starts would
    otherwise ride into the Tier-B commit without ever being declared eligible.
    """
    staged = git(["diff", "--cached", "--name-only"], cwd).stdout.split()
    if staged:
        raise PublishError(
            f"Refusing to publish: the index already contains {sorted(set(staged))} "
            "before staging. Tier-B publication requires a clean index so that "
            "only artifacts declared eligible by this run can be committed."
        )
    unmerged = git(["diff", "--name-only", "--diff-filter=U"], cwd).stdout.split()
    if unmerged:
        raise PublishError(f"Refusing to publish with unresolved merge paths: {sorted(unmerged)}")


def assert_staged_allowlist(cwd: Path, eligible: list[str]) -> list[str]:
    """Fail closed unless every staged path is eligible in this invocation.

    Subset, not equality: an eligible artifact whose content did not change
    stages nothing, which is normal and must not fail the publication.

    Two distinct authorities, both required. ALLOWED_PATHS is the global
    capability, meaning an artifact may EVER be published by this script.
    `eligible` is the per-invocation capability, meaning it is authorized to
    enter THIS commit because its preparation succeeded. Checking only the
    global one lets a withheld artifact be published whenever something else
    put it in the index, which reports withheld and publishes anyway.
    """
    staged = [p for p in git(["diff", "--cached", "--name-only"], cwd).stdout.splitlines() if p]
    forbidden = sorted(set(staged) - ALLOWED_PATHS)
    if forbidden:
        raise PublishError(
            f"Refusing to commit: staged paths outside the Tier-B allowlist: {forbidden}"
        )
    unauthorized = sorted(set(staged) - set(eligible))
    if unauthorized:
        raise PublishError(
            f"Refusing to commit: {unauthorized} staged but not eligible in this run. "
            "An artifact whose preparation failed must never reach the commit."
        )
    return sorted(staged)


def classify_push(cwd: Path, base_sha: str, local_sha: str) -> PublishResult:
    """Classify a failed push by remote identity alone.

    Never rebases, merges, or otherwise acquires history. remote == our commit
    means the push landed and the response was lost; remote == the parent we
    started from means a transient failure worth retrying; anything else, or an
    unverifiable remote, fails closed.
    """
    if git(["fetch", "origin", "data"], cwd, check=False).returncode != 0:
        return PublishResult.UNVERIFIABLE
    remote = git(["rev-parse", "FETCH_HEAD"], cwd).stdout.strip()
    if remote == local_sha:
        return PublishResult.PUBLISHED
    if remote == base_sha:
        return PublishResult.REMOTE_UNCHANGED
    return PublishResult.UNEXPECTED_REMOTE


def push_with_identity(
    cwd: Path, base_sha: str, local_sha: str, attempts: int = 3, sleep_seconds: float = 5.0
) -> PublishResult:
    """Push, classifying every failure by remote identity.

    Returns PUBLISHED on success. REMOTE_UNCHANGED is the ONLY classification
    that authorizes another attempt; returning it means the retries were
    exhausted with the remote still unmoved, which is a failed publication,
    not a success.

    UNVERIFIABLE stops immediately rather than retrying. A non-force push
    could not corrupt an advanced remote, so retrying would be safe in
    practice, but "unknown is its own state" is the rule this project runs on
    and a state machine that says one thing while doing another is the defect
    class that costs the most here.
    """
    result = PublishResult.REMOTE_UNCHANGED
    for attempt in range(1, attempts + 1):
        if git(["push", "origin", "HEAD:data"], cwd, check=False).returncode == 0:
            return PublishResult.PUBLISHED
        result = classify_push(cwd, base_sha, local_sha)
        if result is not PublishResult.REMOTE_UNCHANGED:
            return result
        if attempt < attempts:
            time.sleep(sleep_seconds * attempt)
    return result


def emit(line: str) -> None:
    """Write one line to the GitHub step summary when running in Actions."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    print(line)


def main() -> int:
    """Publish eligible Tier-B artifacts, then report withheld ones as failure."""
    cwd = Path(os.environ.get("DATA_DIR", ".")).resolve()
    coverage_ready = os.environ.get("COVERAGE_READY", "").lower() == "true"
    label = os.environ.get("COMMIT_LABEL", "sweep")
    warn_mib = int(os.environ.get("DB_WARN_MIB", "50"))
    hard_mib = int(os.environ.get("DB_HARD_MIB", "90"))
    sleep_seconds = float(os.environ.get("PUBLISH_RETRY_SLEEP", "5"))

    withheld: list[str] = []
    paths: list[str] = []
    attribution_degraded = False

    # Coverage eligibility comes from the generation step's outcome. A crashed
    # generator can leave a complete-looking partial file, so disk state is not
    # evidence of success and is deliberately not consulted.
    if coverage_ready:
        # Declared ready but absent means the preparation contract is
        # internally inconsistent, so no claim about this run's state can be
        # trusted. That fails the whole publication rather than degrading to
        # a partial one; it is a bug in the caller, not an artifact-local
        # failure the publisher should route around.
        if not (cwd / "coverage.csv").exists():
            print(
                "::error::COVERAGE_READY=true but coverage.csv does not exist. "
                "Tier-B preparation contract violated; refusing to publish."
            )
            emit("TIER B: FAILED CLOSED: preparation contract violated")
            return 1
        paths.append("coverage.csv")
    else:
        withheld.append("coverage rollup regeneration failed")

    db_ok, db_reason, db_warning = assess_db(cwd, warn_mib, hard_mib)
    if db_warning:
        print(f"::warning::{db_warning}")
        emit(f"WARNING: {db_warning}")
    if db_ok:
        paths.append("tracker.db")
    else:
        withheld.append(db_reason or "tracker.db withheld")

    try:
        base_sha = head_sha(cwd)
        # Preconditions, established BEFORE anything is staged or committed:
        # the index carries nothing this run did not put there, and the remote
        # is still the tip we intend to parent on.
        require_clean_index(cwd)
        verify_remote_parent(cwd, base_sha)

        if not paths or not stage(cwd, paths):
            emit(f"Tier B: nothing to publish ({'; '.join(withheld) or 'no changes'})")
        else:
            staged = assert_staged_allowlist(cwd, paths)
            stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
            # The attempt id makes Tier-B state attributable to the run that
            # produced it. Without it, "did this run publish tracker.db?" can
            # only be answered by inspecting the branch tip before another of
            # the 12 daily sweeps lands, which is a timing argument. Archive
            # commits already carry per-attempt manifests; this gives Tier B
            # the same property structurally.
            attempt = os.environ.get("ATTEMPT_ID", "")
            if not attempt:
                # The attribution system being absent, not an optional
                # descriptor going unset. Publication proceeds, because
                # Tier B is reconstructible and provenance must never gate
                # durability, but the run does NOT get to look healthy: a
                # green run asserts that everything important succeeded, and
                # losing attribution means future verification cannot say
                # which run published this state. Same shape as a withheld
                # artifact, so it is reported the same way, after publishing.
                attempt = "unknown"
                attribution_degraded = True
                print("::error::ATTEMPT_ID absent; Tier-B commit attribution degraded")
            git(
                [
                    "commit",
                    "-m",
                    f"Update {label} Tier B ({', '.join(staged)}) attempt {attempt} {stamp}",
                ],
                cwd,
            )
            local_sha = head_sha(cwd)

            if os.environ.get("SIMULATE_PUBLISH_FAILURE", "").lower() == "true":
                print("::error::SYNTHETIC FAULT INJECTION: Tier-B publication failed deliberately")
                emit("## SYNTHETIC FAULT INJECTION: Tier-B publication failure")
                emit("ARCHIVE: already durable on the remote")
                emit(f"TIER B: withheld ({', '.join(staged)})")
                return 1

            result = push_with_identity(cwd, base_sha, local_sha, sleep_seconds=sleep_seconds)
            if result is not PublishResult.PUBLISHED:
                print(f"::error::Tier-B publication failed: {result}")
                emit(f"TIER B: NOT PUBLISHED ({result})")
                return 1
            emit(f"TIER B: published {', '.join(staged)}")
    except PublishError as exc:
        print(f"::error::{exc}")
        emit(f"TIER B: FAILED CLOSED: {exc}")
        return 1

    if attribution_degraded:
        emit("TIER B: published, but attribution degraded (ATTEMPT_ID absent)")
    if withheld:
        for reason in withheld:
            print(f"::error::Tier-B artifact withheld: {reason}")
        emit("TIER B: withheld -> " + "; ".join(withheld))
    return 1 if (withheld or attribution_degraded) else 0


if __name__ == "__main__":
    sys.exit(main())
