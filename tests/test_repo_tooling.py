"""Guard developer tooling against drifting from what CI actually enforces.

Each class pins one claim the repo makes about itself to the file that makes
it. These claims drifted silently before: the devcontainer kept Python 3.10
after the floor moved to 3.11. Parsing is regex-based on purpose, matching the
workflow parsing in tests/tracker/test_generate_coverage.py, so the tests add
no dependency beyond the standard library.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _python_floor_minor() -> int:
    """Minor version of the requires-python floor, e.g. 11 for '>=3.11'."""
    spec = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["requires-python"]
    m = re.fullmatch(r"\s*>=\s*3\.(\d+)\s*", spec)
    assert m, f"requires-python {spec!r} is not a simple '>=3.N' floor; update this parser"
    return int(m.group(1))


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
