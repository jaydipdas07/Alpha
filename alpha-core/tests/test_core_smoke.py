"""Smoke test: the alpha-core package imports and is typed.

A trivial green test so B0.1's CI (ruff -> mypy --strict -> pytest) has something to run;
real kernel tests land with their modules (B0.7 onward).
"""

import alpha_core


def test_alpha_core_imports() -> None:
    assert alpha_core.__version__ == "0.0.0"
