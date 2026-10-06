"""Guard developer tooling against drifting from what CI actually enforces.

Each class pins one claim the repo makes about itself (which commands hit the
live API, which Python versions are supported, which CI jobs `make ci` runs)
to the file that makes it. These claims drifted silently before: `make test`
ran live-API tests while CI excluded them, and the devcontainer kept Python
3.10 after the floor moved to 3.11. Parsing is regex-based on purpose, matching
the workflow parsing in tests/tracker/test_generate_coverage.py, so the tests
add no dependency beyond the standard library.
"""

import configparser
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Make targets that deliberately drive the live Google Flights API. Every
# other target that invokes pytest must exclude live_api, matching the
# required CI gate. test-fuzz is live by construction: every fuzz-marked test
# also carries live_api, so filtering it would run zero tests.
LIVE_MAKE_TARGETS = {"test-fuzz", "test-all", "test-live"}
LIVE_TOX_ENVS = {"testenv:live"}
HERMETIC_FILTER = '-m "not live_api"'


def _make_recipes() -> dict[str, list[str]]:
    """Map each Makefile target to its recipe lines (tab-indented)."""
    recipes: dict[str, list[str]] = {}
    current = None
    for line in (ROOT / "Makefile").read_text().splitlines():
        target = re.match(r"^([a-z][\w-]*):", line)
        if target:
            current = target.group(1)
            recipes[current] = []
        elif line.startswith("\t") and current is not None:
            recipes[current].append(line.strip())
        elif line.strip() and not line.startswith("#"):
            current = None
    return recipes


def _make_help_lines() -> dict[str, str]:
    """Map each target to its `make help` description line."""
    help_lines = {}
    for line in _make_recipes().get("help", []):
        m = re.match(r'@echo\s+"\s+make\s+([\w-]+)\s+-\s+(.*)"$', line)
        if m:
            help_lines[m.group(1)] = m.group(2)
    return help_lines


def _pytest_make_targets() -> list[str]:
    return sorted(t for t, lines in _make_recipes().items() if any("pytest" in x for x in lines))


def _pytest_tox_envs() -> list[str]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ROOT / "tox.ini")
    return sorted(
        s for s in parser.sections() if "pytest" in parser.get(s, "commands", fallback="")
    )


def _python_floor_minor() -> int:
    """Minor version of the requires-python floor, e.g. 11 for '>=3.11'."""
    spec = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["requires-python"]
    m = re.fullmatch(r"\s*>=\s*3\.(\d+)\s*", spec)
    assert m, f"requires-python {spec!r} is not a simple '>=3.N' floor; update this parser"
    return int(m.group(1))


class TestLiveApiClassification:
    """Every pytest entry point is either hermetic or explicitly named live.

    Scope: this checks the Makefile and tox.ini, the two places a developer
    runs tests from by name. It does not prove a hermetic test cannot reach the
    network; that is the job of the live_api marker audit, not of this file.
    """

    def test_discovery_is_not_vacuous(self):
        # A parser that finds nothing would let every parametrized case below
        # pass by not existing. Pin the floor of what must be found.
        assert {"test", "test-mcp", "test-fuzz", "test-all"} <= set(_pytest_make_targets())
        assert "testenv" in _pytest_tox_envs()

    @pytest.mark.parametrize("target", _pytest_make_targets())
    def test_make_target_is_hermetic_or_declared_live(self, target):
        recipe = " ".join(_make_recipes()[target])
        assert target in LIVE_MAKE_TARGETS or HERMETIC_FILTER in recipe, (
            f"make {target} runs pytest without {HERMETIC_FILTER} and is not declared live"
        )

    @pytest.mark.parametrize("target", sorted(LIVE_MAKE_TARGETS))
    def test_live_make_target_exists_and_says_live(self, target):
        assert target in _pytest_make_targets(), f"declared live target {target} is missing"
        assert "live" in _make_help_lines().get(target, "").lower(), (
            f"`make help` must tell the reader that make {target} hits the live API"
        )

    @pytest.mark.parametrize("env", _pytest_tox_envs())
    def test_tox_env_is_hermetic_or_declared_live(self, env):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(ROOT / "tox.ini")
        commands = parser.get(env, "commands")
        assert env in LIVE_TOX_ENVS or HERMETIC_FILTER in commands, (
            f"tox [{env}] runs pytest without {HERMETIC_FILTER} and is not declared live"
        )


class TestPythonVersionClaims:
    """Files that state a Python version must agree with requires-python."""

    def test_devcontainer_image_meets_floor(self):
        text = (ROOT / ".devcontainer" / "Dockerfile").read_text()
        m = re.search(r"^FROM\s+python:3\.(\d+)", text, re.MULTILINE)
        assert m, "devcontainer base image is no longer python:3.N; update this test"
        assert int(m.group(1)) >= _python_floor_minor()

    def test_skill_doc_states_the_floor(self):
        text = (ROOT / "skills" / "fli" / "SKILL.md").read_text()
        claims = re.findall(r"Python 3\.(\d+) or newer", text)
        assert claims, "SKILL.md no longer states a Python requirement; update this test"
        assert {int(c) for c in claims} == {_python_floor_minor()}


class TestMakeCiTargetsRealJobs:
    """`make ci` must name jobs that exist in the workflow files it points at."""

    def _act_invocations(self) -> list[tuple[list[str], str]]:
        out = []
        for line in _make_recipes()["ci"]:
            if not line.startswith("act "):
                continue
            jobs = re.findall(r"-j\s+(\S+)", line)
            wf = re.search(r"(?:-W|--workflows)\s+(\S+)", line)
            assert wf, f"act invocation without an explicit workflow file: {line}"
            out.append((jobs, wf.group(1)))
        return out

    def test_ci_runs_lint_and_test(self):
        named = {job for jobs, _ in self._act_invocations() for job in jobs}
        assert {"lint", "test"} <= named

    def test_every_named_job_exists_in_its_workflow(self):
        for jobs, wf in self._act_invocations():
            text = (ROOT / wf).read_text()
            for job in jobs:
                assert re.search(rf"^  {re.escape(job)}:\s*$", text, re.MULTILINE), (
                    f"make ci asks for job {job!r} but {wf} defines no such job"
                )
