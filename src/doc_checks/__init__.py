"""Self-healing documentation checks, distributed as pre-commit hooks.

Each check module exposes a `run() -> CheckResult` function.
Exit code 0 = pass, 1 = mismatch found.

Available checks:
    - make_refs:  Stale ``make <target>`` references in markdown
    - cd_refs:    Stale ``cd <path>`` references in markdown
    - py_imports: Stale project-local imports in markdown code fences
    - mermaid:    Mermaid diagram syntax (via ``mmdc``, skipped if missing)

Usage (console scripts installed with the package):
    doc-check                  # Run all checks
    doc-check make_refs        # Run one check via the runner
    doc-check --ci             # CI mode (strict exit code)
    doc-check-make-refs        # Run one check directly (pre-commit entry point)
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

_DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"

# Consumer-side override file, looked up in the root of the repo under check.
CONFIG_FILENAME = ".doc-checks.yaml"


@lru_cache(maxsize=1)
def repo_root() -> Path:
    """Root of the repository being checked — NOT of this package.

    When installed as a pre-commit hook this package lives in pre-commit's
    cached venv, so ``__file__`` says nothing about the project under check.
    Pre-commit runs hooks with cwd at the consumer repo root; ``git rev-parse``
    makes that explicit and also covers manual runs from a subdirectory.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(out.stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return Path.cwd()


@lru_cache(maxsize=1)
def get_config() -> dict[str, Any]:
    """Packaged defaults merged with the consumer repo's ``.doc-checks.yaml``.

    The merge is per check section: keys set in the override file replace the
    corresponding default keys, while unspecified keys keep their defaults.
    This lets a consumer add e.g. ``make_refs.ignore_targets`` without having
    to restate the default scan globs.
    """
    config: dict[str, Any] = yaml.safe_load(_DEFAULT_CONFIG_PATH.read_text())
    override_path = repo_root() / CONFIG_FILENAME
    if override_path.exists():
        override: dict[str, Any] = yaml.safe_load(override_path.read_text()) or {}
        for section, values in override.items():
            if isinstance(values, dict) and isinstance(config.get(section), dict):
                config[section] = {**config[section], **values}
            else:
                config[section] = values
    return config


class CheckResult(BaseModel):
    """Standard return type for all doc check `run()` functions."""

    passed: bool
    message: str
    details: list[str] = []


__all__ = ["CheckResult", "get_config", "repo_root"]
