"""DISPOSABLE: proves the test-versions gate blocks a merge. Delete with the branch.

PEP 695 type-alias syntax is 3.12+, so this file is a SyntaxError on 3.11
and parses fine on 3.12 and 3.13. That makes it the precise shape of drift
the old single-version gate could not see: `test` runs 3.12 and goes green
while the declared floor is broken.
"""

type DisposableAlias = int


def test_placeholder():
    assert DisposableAlias is not None
