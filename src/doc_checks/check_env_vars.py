"""Env Var Audit — detects mismatches between Pydantic Settings and ``.env.example``.

Parses every configured config module with the AST, finds all Pydantic
``BaseSettings`` subclasses (the single source of truth for configuration),
turns their fields into uppercase env var names, and compares against the
entries in ``.env.example`` (and, informationally, the README).

Policy:
    * Fields **without a default** are *required* — missing them from
      ``.env.example`` is a hard error.
    * Fields **with a default** are *optional* — their absence is reported as
      an informational note, not a failure.
    * ``.env.example`` entries that map to no settings field are surfaced as
      warnings (they may be consumed via ``os.getenv`` / external services);
      add them to ``known_non_config_vars`` to silence.

The check is config-driven and passes trivially when a repo defines no
Pydantic Settings classes, so enabling the hook is safe everywhere.

Usage:
    python -m doc_checks.check_env_vars
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

from pydantic import BaseModel

from doc_checks import CheckResult, get_config, repo_root

REPO_ROOT = repo_root()

_cfg = get_config().get("env_vars", {})
CONFIG_GLOBS: list[str] = _cfg.get("config_globs", ["**/config.py", "**/settings.py", "**/conf.py"])
EXCLUDE_GLOBS: list[str] = _cfg.get(
    "exclude_globs",
    [".venv/**", "node_modules/**", ".pytest_cache/**", ".ruff_cache/**"],
)
SETTINGS_BASE_CLASSES: set[str] = set(_cfg.get("settings_base_classes", ["BaseSettings"]))
IGNORE_FIELDS: set[str] = set(_cfg.get("ignore_fields", ["model_config"]))
KNOWN_NON_CONFIG_VARS: set[str] = set(_cfg.get("known_non_config_vars", []))
ENV_EXAMPLE: Path = REPO_ROOT / _cfg.get("env_example", ".env.example")
README_FILE: Path = REPO_ROOT / _cfg.get("readme_file", "README.md")


class FieldInfo(BaseModel):
    """Metadata about a Pydantic Settings field."""

    name: str  # uppercase env var name (prefix applied)
    has_default: bool  # True if the field has a concrete default value


def _is_ellipsis(node: ast.expr) -> bool:
    """Check whether an AST node is the Ellipsis literal (``...``)."""
    return isinstance(node, ast.Constant) and node.value is ...


def _field_has_default(node: ast.AnnAssign) -> bool:
    """Determine whether an annotated assignment carries a real default value.

    Returns False (= required) for:
      - bare annotations (no value):        ``field: str``
      - Ellipsis literal:                   ``field: str = ...``
      - ``Field(default=...)``:             ``field: str = Field(default=...)``
      - ``Field(...)`` with Ellipsis arg:   ``field: str = Field(..., desc="x")``
      - ``Field()`` with no default at all: ``field: str = Field(description="x")``
    Returns True (= optional) for everything else.
    """
    if node.value is None:
        return False
    if _is_ellipsis(node.value):
        return False
    if isinstance(node.value, ast.Call):
        func = node.value.func
        is_field_call = (isinstance(func, ast.Name) and func.id == "Field") or (
            isinstance(func, ast.Attribute) and func.attr == "Field"
        )
        if is_field_call:
            # ``default_factory=`` always means optional.
            if any(kw.arg == "default_factory" for kw in node.value.keywords):
                return True
            # ``default=`` keyword: optional unless explicitly Ellipsis.
            for kw in node.value.keywords:
                if kw.arg == "default":
                    return not _is_ellipsis(kw.value)
            # No default kwarg — fall back to the first positional arg.
            if node.value.args:
                return not _is_ellipsis(node.value.args[0])
            # ``Field()`` with neither → required.
            return False
    return True


def _extract_env_prefix(class_node: ast.ClassDef) -> str:
    """Return ``env_prefix`` from ``model_config = SettingsConfigDict(...)`` if set."""
    for item in class_node.body:
        if not isinstance(item, ast.Assign):
            continue
        if not (
            len(item.targets) == 1 and isinstance(item.targets[0], ast.Name) and item.targets[0].id == "model_config"
        ):
            continue
        if not isinstance(item.value, ast.Call):
            continue
        func = item.value.func
        is_settings_config = (isinstance(func, ast.Name) and func.id == "SettingsConfigDict") or (
            isinstance(func, ast.Attribute) and func.attr == "SettingsConfigDict"
        )
        if not is_settings_config:
            continue
        for kw in item.value.keywords:
            if kw.arg == "env_prefix" and isinstance(kw.value, ast.Constant):
                return str(kw.value.value)
    return ""


def extract_settings_fields(config_path: Path) -> dict[str, list[FieldInfo]]:
    """Parse a config module, returning ``{ClassName: [FieldInfo, ...]}``.

    Recognizes any class inheriting (transitively, within the file) from a name
    in ``settings_base_classes``. Field names are uppercased and prefixed with
    the class's ``env_prefix`` to match the resulting env var.
    """
    tree = ast.parse(config_path.read_text())
    settings_classes: dict[str, list[FieldInfo]] = {}
    # Local copy so transitive bases discovered here don't leak across files.
    known_bases = set(SETTINGS_BASE_CLASSES)

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        base_names: list[str] = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                base_names.append(base.id)
            elif isinstance(base, ast.Attribute):
                base_names.append(base.attr)

        if not any(b in known_bases for b in base_names):
            continue

        # Track this class as a settings base so subclasses are picked up too.
        known_bases.add(node.name)

        prefix = _extract_env_prefix(node)
        fields: list[FieldInfo] = []
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                field_name = item.target.id
                if field_name not in IGNORE_FIELDS:
                    fields.append(
                        FieldInfo(
                            name=f"{prefix}{field_name}".upper(),
                            has_default=_field_has_default(item),
                        ),
                    )

        if fields:
            settings_classes[node.name] = fields

    return settings_classes


def extract_env_example_vars(env_path: Path) -> set[str]:
    """Parse ``.env.example`` and return the set of declared variable names."""
    if not env_path.exists():
        return set()

    vars_found: set[str] = set()
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Z_][A-Z0-9_]*)\s*=", line)
        if match:
            vars_found.add(match.group(1))
    return vars_found


def extract_readme_env_vars(readme_path: Path) -> set[str]:
    """Extract env var references from a README's *Environment Variables* section."""
    if not readme_path.exists():
        return set()

    vars_found: set[str] = set()
    in_section = False
    for line in readme_path.read_text().splitlines():
        if re.match(r"^#{1,4}\s+.*[Ee]nvironment\s+[Vv]ariable", line):
            in_section = True
            continue
        if in_section and re.match(r"^#{1,3}\s+", line) and "environment" not in line.lower():
            break
        if in_section:
            # Require at least one underscore to avoid matching bare acronyms
            # like API / SSE / MCP.
            for match in re.finditer(r"\b([A-Z][A-Z0-9]*_[A-Z0-9_]+)\b", line):
                vars_found.add(match.group(1))
    return vars_found


def _iter_config_files() -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for glob in CONFIG_GLOBS:
        for p in REPO_ROOT.glob(glob):
            if not p.is_file() or p in seen:
                continue
            rel = p.relative_to(REPO_ROOT)
            if any(rel.full_match(pat) for pat in EXCLUDE_GLOBS):
                continue
            seen.add(p)
            out.append(p)
    return sorted(out)


def collect_settings(files: list[Path] | None = None) -> tuple[dict[str, list[FieldInfo]], list[str]]:
    """Merge settings classes across all config files. Returns ``(classes, warnings)``."""
    files = files if files is not None else _iter_config_files()
    classes: dict[str, list[FieldInfo]] = {}
    warnings: list[str] = []
    for path in files:
        try:
            found = extract_settings_fields(path)
        except SyntaxError as exc:
            rel = path.relative_to(REPO_ROOT)
            warnings.append(f"⚠ {rel}: SyntaxError — {exc.msg} (line {exc.lineno})")
            continue
        for cls_name, fields in found.items():
            # Disambiguate identically-named classes from different modules.
            key = cls_name if cls_name not in classes else f"{path.stem}.{cls_name}"
            classes[key] = fields
    return classes, warnings


def run(files: list[str] | None = None) -> CheckResult:  # noqa: ARG001 (whole-repo audit)
    """Run the env var audit. Required settings fields missing from
    ``.env.example`` are hard errors; everything else is informational.
    """
    settings_classes, details = collect_settings()

    env_vars = extract_env_example_vars(ENV_EXAMPLE)
    if not settings_classes:
        # Nothing to validate against — pass quietly so the hook is safe to
        # enable in repos that don't (yet) use Pydantic Settings.
        msg = "No Pydantic Settings classes found — nothing to check."
        return CheckResult(passed=True, message=msg, details=details)

    issues: list[str] = []
    all_config_vars: set[str] = set()
    required_vars: set[str] = set()
    optional_vars: set[str] = set()

    for cls_name, fields in settings_classes.items():
        field_names = sorted(f.name for f in fields)
        details.append(f"  [{cls_name}] {len(fields)} field(s): {', '.join(field_names)}")
        for f in fields:
            all_config_vars.add(f.name)
            (optional_vars if f.has_default else required_vars).add(f.name)

    details.append(
        f"\n  Total settings fields: {len(all_config_vars)} "
        f"({len(required_vars)} required, {len(optional_vars)} optional)",
    )

    if not env_vars:
        issues.append(f"{ENV_EXAMPLE.name} is missing or empty")
    else:
        details.append(f"  Total vars in {ENV_EXAMPLE.name}: {len(env_vars)}")

        for var in sorted(required_vars - env_vars):
            issues.append(f"Required field {var} (no default) is NOT in {ENV_EXAMPLE.name}")

        for var in sorted(optional_vars - env_vars):
            details.append(f"  ℹ Optional field {var} (has default) is not in {ENV_EXAMPLE.name}")

        extra_in_env = (env_vars - all_config_vars) - KNOWN_NON_CONFIG_VARS
        for var in sorted(extra_in_env):
            details.append(
                f"  ⚠ {ENV_EXAMPLE.name} has {var} which is not a Pydantic Settings field "
                f"(may be used via os.getenv)",
            )

    readme_vars = extract_readme_env_vars(README_FILE)
    if readme_vars:
        details.append(f"  Total vars referenced in README: {len(readme_vars)}")
        undocumented = all_config_vars - readme_vars
        if undocumented:
            details.append(f"  ⚠ {len(undocumented)} config field(s) not explicitly mentioned in README env section")

    passed = not issues
    if passed:
        msg = f"All required config fields are present in {ENV_EXAMPLE.name}."
    else:
        msg = f"Found {len(issues)} required env var(s) missing from {ENV_EXAMPLE.name}."

    return CheckResult(
        passed=passed,
        message=msg,
        details=[*details, "", *[f"  ❌ {i}" for i in issues]] if issues else details,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit Pydantic Settings fields against .env.example.")
    parser.add_argument("files", nargs="*", help="(ignored — the audit always scans the whole repo)")
    parser.parse_args()

    result = run()
    status = "PASS ✅" if result.passed else "FAIL ❌"
    print(f"[Env Var Audit] {status}: {result.message}")
    for line in result.details:
        print(line)
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
