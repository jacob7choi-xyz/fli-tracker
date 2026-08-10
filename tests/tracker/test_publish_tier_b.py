"""Tests for the single-commit Tier-B publisher.

These run the production implementation in scripts/publish_tier_b.py against
real git repositories. Faults are injected at the process boundary with a git
shim on PATH, so the publisher's own staging, commit, and identity
classification logic is the code under test rather than a reimplementation of
it. That distinction matters here: the bug this module exists to prevent was
invisible to reasoning about the workflow and only appeared when the actual
commit ancestry was built.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.publish_tier_b import (
    ALLOWED_PATHS,
    PublishError,
    PublishResult,
    assert_staged_allowlist,
    main,
)

REAL_GIT = shutil.which("git") or "/usr/bin/git"

SHIM = """#!/bin/sh
case "$1" in
  push)
    if [ -n "$PUSH_COUNTER" ]; then
      pn=$(cat "$PUSH_COUNTER" 2>/dev/null || echo 0)
      echo "$((pn + 1))" > "$PUSH_COUNTER"
    fi
    if [ -n "$FOREIGN_PUSH_DIR" ]; then
      "{git}" -C "$FOREIGN_PUSH_DIR" push -q origin HEAD:data
      exit 1
    fi
    if [ "$FAIL_PUSH" = "1" ]; then exit 1; fi
    if [ "$FAIL_PUSH_AFTER" = "1" ]; then "{git}" "$@"; exit 1; fi
    ;;
  fetch)
    if [ -n "$FAIL_FETCH_FROM" ]; then
      n=$(cat "$FETCH_COUNTER" 2>/dev/null || echo 0)
      n=$((n + 1))
      echo "$n" > "$FETCH_COUNTER"
      if [ "$n" -ge "$FAIL_FETCH_FROM" ]; then exit 1; fi
    fi
    if [ "$FAIL_FETCH" = "1" ]; then exit 1; fi
    ;;
esac
exec "{git}" "$@"
"""


def _run(args: list[str], cwd: Path) -> str:
    proc = subprocess.run([REAL_GIT, *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a data-branch checkout whose remote tip is the verified archive commit."""
    remote = tmp_path / "remote.git"
    subprocess.run([REAL_GIT, "init", "-q", "--bare", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run([REAL_GIT, "clone", "-q", str(remote), str(work)], check=True)
    _run(["config", "user.email", "t@example.com"], work)
    _run(["config", "user.name", "test"], work)
    (work / "archive").mkdir()
    (work / "archive" / "shard.csv.gz").write_text("shard")
    (work / "tracker.db").write_text("db-v1")
    (work / "coverage.csv").write_text("coverage-v1")
    _run(["add", "-A"], work)
    _run(["commit", "-qm", "A: archive"], work)
    _run(["push", "-q", "origin", "HEAD:data"], work)
    _run(["branch", "-q", "-M", "data"], work)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "git"
    shim.write_text(SHIM.format(git=REAL_GIT))
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FETCH_COUNTER", str(tmp_path / "fetch_count"))
    monkeypatch.setenv("PUSH_COUNTER", str(tmp_path / "push_count"))
    monkeypatch.setenv("DATA_DIR", str(work))
    monkeypatch.setenv("PUBLISH_RETRY_SLEEP", "0")
    monkeypatch.setenv("COMMIT_LABEL", "domestic")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("SIMULATE_PUBLISH_FAILURE", raising=False)
    return work


def _remote_file(work: Path, name: str) -> str | None:
    proc = subprocess.run(
        [REAL_GIT, "--git-dir", str(work.parent / "remote.git"), "show", f"data:{name}"],
        capture_output=True,
        text=True,
    )
    return proc.stdout if proc.returncode == 0 else None


def _remote_sha(work: Path) -> str:
    return _run(["--git-dir", str(work.parent / "remote.git"), "rev-parse", "data"], work)


def _push_attempts(tmp: Path) -> int:
    counter = tmp / "push_count"
    return int(counter.read_text().strip()) if counter.exists() else 0


def _dirty(work: Path, name: str, content: str) -> None:
    (work / name).write_text(content)


class TestBothArtifactsValid:
    def test_single_commit_contains_both(self, repo: Path, monkeypatch: pytest.MonkeyPatch):
        base = _run(["rev-parse", "HEAD"], repo)
        _dirty(repo, "tracker.db", "db-v2")
        _dirty(repo, "coverage.csv", "coverage-v2")
        monkeypatch.setenv("COVERAGE_READY", "true")

        assert main() == 0

        assert _remote_file(repo, "tracker.db") == "db-v2"
        assert _remote_file(repo, "coverage.csv") == "coverage-v2"
        # Exactly one post-archive mutation, parented on the verified tip.
        assert _run(["rev-list", "--count", f"{base}..data"], repo) == "1"


class TestWithholdingDoesNotBlockTheSibling:
    def test_coverage_failure_still_publishes_db(self, repo: Path, monkeypatch: pytest.MonkeyPatch):
        _dirty(repo, "tracker.db", "db-v2")
        monkeypatch.setenv("COVERAGE_READY", "false")

        assert main() == 1  # red for the withheld artifact

        assert _remote_file(repo, "tracker.db") == "db-v2"
        assert _remote_file(repo, "coverage.csv") == "coverage-v1"

    def test_partial_coverage_file_never_hitchhikes(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A crashed generator leaves a plausible file; outcome, not disk, decides."""
        _dirty(repo, "tracker.db", "db-v2")
        _dirty(repo, "coverage.csv", "coverage-HALF-WRITTEN")
        monkeypatch.setenv("COVERAGE_READY", "false")

        assert main() == 1

        assert _remote_file(repo, "tracker.db") == "db-v2"
        assert _remote_file(repo, "coverage.csv") == "coverage-v1"

    def test_db_size_gate_still_publishes_coverage(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        (repo / "tracker.db").write_bytes(b"x" * (2 * 1024 * 1024))
        _dirty(repo, "coverage.csv", "coverage-v2")
        monkeypatch.setenv("COVERAGE_READY", "true")
        monkeypatch.setenv("DB_HARD_MIB", "1")

        assert main() == 1

        assert _remote_file(repo, "coverage.csv") == "coverage-v2"
        assert _remote_file(repo, "tracker.db") == "db-v1"

    def test_both_withheld_makes_no_commit_and_no_push(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        base = _run(["rev-parse", "HEAD"], repo)
        (repo / "tracker.db").write_bytes(b"x" * (2 * 1024 * 1024))
        _dirty(repo, "coverage.csv", "coverage-HALF")
        monkeypatch.setenv("COVERAGE_READY", "false")
        monkeypatch.setenv("DB_HARD_MIB", "1")

        assert main() == 1

        assert _run(["rev-parse", "HEAD"], repo) == base
        assert _remote_sha(repo) == base


class TestPushIdentityClassification:
    def test_lost_response_counts_as_published(self, repo: Path, monkeypatch: pytest.MonkeyPatch):
        """Push mutates the remote then reports failure; remote == our commit."""
        _dirty(repo, "tracker.db", "db-v2")
        monkeypatch.setenv("COVERAGE_READY", "true")
        monkeypatch.setenv("FAIL_PUSH_AFTER", "1")

        assert main() == 0

        assert _remote_file(repo, "tracker.db") == "db-v2"

    def test_unmoved_remote_retries_then_fails_without_publishing(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """REMOTE_UNCHANGED drives retries; exhausting them is a failure."""
        base = _run(["rev-parse", "HEAD"], repo)
        _dirty(repo, "tracker.db", "db-v2")
        monkeypatch.setenv("COVERAGE_READY", "true")
        monkeypatch.setenv("FAIL_PUSH", "1")

        assert main() == 1
        assert PublishResult.REMOTE_UNCHANGED.value in capsys.readouterr().out

        assert _remote_sha(repo) == base
        assert _remote_file(repo, "tracker.db") == "db-v1"

    def test_unexpected_remote_fails_closed(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """A foreign writer that lands BETWEEN our verify and our push.

        The window this covers is narrow and specific: the starting-parent
        check passed, so publication proceeded, and only the push discovered
        the branch had moved. Pushing the foreign commit before main() would
        instead be caught by the starting-parent check, which is a different
        guard tested elsewhere.
        """
        other = repo.parent / "other"
        subprocess.run(
            [REAL_GIT, "clone", "-q", "-b", "data", str(repo.parent / "remote.git"), str(other)],
            check=True,
        )
        _run(["config", "user.email", "o@example.com"], other)
        _run(["config", "user.name", "other"], other)
        (other / "foreign.txt").write_text("foreign")
        _run(["add", "-A"], other)
        _run(["commit", "-qm", "foreign writer"], other)

        _dirty(repo, "tracker.db", "db-v2")
        monkeypatch.setenv("COVERAGE_READY", "true")
        monkeypatch.setenv("FOREIGN_PUSH_DIR", str(other))

        assert main() == 1
        # Assert the classification, not just the exit code: this test would
        # otherwise pass identically via the starting-parent guard.
        assert PublishResult.UNEXPECTED_REMOTE.value in capsys.readouterr().out

        # The foreign commit is the tip and ours was never reconciled onto it.
        assert _remote_file(repo, "foreign.txt") == "foreign"
        assert _remote_file(repo, "tracker.db") == "db-v1"

    def test_unverifiable_remote_fails_closed(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """Push fails and the remote cannot be read, so its state is unknown.

        The starting-parent fetch must succeed here, otherwise this would be
        the start guard again rather than push classification.
        """
        base = _run(["rev-parse", "HEAD"], repo)
        _dirty(repo, "tracker.db", "db-v2")
        monkeypatch.setenv("COVERAGE_READY", "true")
        monkeypatch.setenv("FAIL_PUSH", "1")
        monkeypatch.setenv("FAIL_FETCH_FROM", "2")

        assert main() == 1
        assert PublishResult.UNVERIFIABLE.value in capsys.readouterr().out

        # The load-bearing assertion: unknown remote state authorizes no
        # further writes. Retrying would reach the same classification, so
        # only the attempt count distinguishes the two policies.
        assert _push_attempts(repo.parent) == 1

        assert _remote_sha(repo) == base


class TestStartingParentVerification:
    def test_remote_moved_before_publication_fails_before_committing(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Identity is established before staging, not discovered after commit."""
        base = _run(["rev-parse", "HEAD"], repo)
        other = repo.parent / "other"
        subprocess.run(
            [REAL_GIT, "clone", "-q", "-b", "data", str(repo.parent / "remote.git"), str(other)],
            check=True,
        )
        _run(["config", "user.email", "o@example.com"], other)
        _run(["config", "user.name", "other"], other)
        (other / "foreign.txt").write_text("foreign")
        _run(["add", "-A"], other)
        _run(["commit", "-qm", "out-of-band writer"], other)
        _run(["push", "-q", "origin", "HEAD:data"], other)
        foreign_tip = _remote_sha(repo)

        _dirty(repo, "tracker.db", "db-v2")
        monkeypatch.setenv("COVERAGE_READY", "true")

        assert main() == 1

        assert _run(["rev-parse", "HEAD"], repo) == base  # no local commit was created
        assert _remote_sha(repo) == foreign_tip

    def test_unverifiable_start_fails_closed(self, repo: Path, monkeypatch: pytest.MonkeyPatch):
        base = _run(["rev-parse", "HEAD"], repo)
        _dirty(repo, "tracker.db", "db-v2")
        monkeypatch.setenv("COVERAGE_READY", "true")
        monkeypatch.setenv("FAIL_FETCH", "1")

        assert main() == 1

        assert _run(["rev-parse", "HEAD"], repo) == base


class TestStagedPathAllowlist:
    def test_forbidden_staged_path_is_refused(self, repo: Path):
        (repo / "secrets.env").write_text("NOTIFY_URL=mailto://user:pw@example.com")
        _run(["add", "--", "secrets.env"], repo)

        with pytest.raises(PublishError, match="outside the Tier-B allowlist"):
            assert_staged_allowlist(repo, ["tracker.db", "coverage.csv"])

    def test_allowlist_is_exactly_the_two_tier_b_artifacts(self):
        assert ALLOWED_PATHS == {"tracker.db", "coverage.csv"}


class TestFaultInjection:
    def test_simulated_publish_failure_leaves_remote_untouched(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        base = _run(["rev-parse", "HEAD"], repo)
        _dirty(repo, "tracker.db", "db-v2")
        monkeypatch.setenv("COVERAGE_READY", "true")
        monkeypatch.setenv("SIMULATE_PUBLISH_FAILURE", "true")

        assert main() == 1

        assert _remote_sha(repo) == base
        assert _remote_file(repo, "tracker.db") == "db-v1"


class TestPublishResultVocabulary:
    def test_closed_vocabulary(self):
        assert {r.value for r in PublishResult} == {
            "published",
            "remote_unchanged",
            "unexpected_remote",
            "unverifiable",
        }


class TestInvocationStagingAuthority:
    """Eligibility is per invocation, not merely per script.

    Membership in ALLOWED_PATHS says an artifact may EVER be published. It
    says nothing about whether it is authorized to enter THIS commit, which
    is decided by this invocation's preparation outcomes.
    """

    def test_prestaged_ineligible_artifact_is_never_published(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _dirty(repo, "coverage.csv", "coverage-UNAUTHORIZED")
        _run(["add", "--", "coverage.csv"], repo)  # staged by something upstream
        _dirty(repo, "tracker.db", "db-v2")
        monkeypatch.setenv("COVERAGE_READY", "false")

        assert main() == 1
        assert _remote_file(repo, "coverage.csv") == "coverage-v1"

    def test_prestaged_state_is_refused_before_any_commit(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The clean-index precondition, independent of eligibility."""
        base = _run(["rev-parse", "HEAD"], repo)
        _dirty(repo, "coverage.csv", "coverage-v2")
        _run(["add", "--", "coverage.csv"], repo)
        _dirty(repo, "tracker.db", "db-v2")
        monkeypatch.setenv("COVERAGE_READY", "true")

        assert main() == 1

        assert _run(["rev-parse", "HEAD"], repo) == base
        assert _remote_sha(repo) == base

    def test_eligible_subset_is_enforced_not_just_the_global_allowlist(self, repo: Path):
        """coverage.csv is globally allowed yet unauthorized when ineligible."""
        _dirty(repo, "coverage.csv", "coverage-v2")
        _run(["add", "--", "coverage.csv"], repo)

        with pytest.raises(PublishError, match="staged but not eligible"):
            assert_staged_allowlist(repo, ["tracker.db"])


class TestPreparationContract:
    def test_coverage_ready_but_missing_file_fails_whole_publication(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """An inconsistent contract invalidates every claim about this run."""
        base = _run(["rev-parse", "HEAD"], repo)
        (repo / "coverage.csv").unlink()
        _dirty(repo, "tracker.db", "db-v2")
        monkeypatch.setenv("COVERAGE_READY", "true")

        assert main() == 1

        assert _remote_sha(repo) == base
        assert _remote_file(repo, "tracker.db") == "db-v1"
