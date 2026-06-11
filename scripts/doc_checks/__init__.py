"""Self-healing documentation checks.

Each check module exposes a `run() -> CheckResult` function.
Exit code 0 = pass, 1 = mismatch found.

Available checks:
    - make_refs:  Stale ``make <target>`` references in markdown
    - cd_refs:    Stale ``cd <path>`` references in markdown
    - py_imports: Stale project-local imports in markdown code fences
    - mermaid:    Mermaid diagram syntax (via ``mmdc``, skipped if missing)

Usage:
    make doc-check                  # Run all checks
    uv run python -m scripts.doc_checks.runner            # Run all
    uv run python -m scripts.doc_checks.runner make_refs  # Run one
    uv run python -m scripts.doc_checks.runner --ci       # CI mode (strict exit code)
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

_CONFIG_PATH = Path(__file__).parent / "config.yaml"


@lru_cache(maxsize=1)
def get_config() -> dict[str, Any]:
    """Load and cache doc_checks configuration from config.yaml."""
    return yaml.safe_load(_CONFIG_PATH.read_text())


class CheckResult(BaseModel):
    """Standard return type for all doc check `run()` functions."""

    passed: bool
    message: str
    details: list[str] = []


__all__ = ["CheckResult", "get_config"]
