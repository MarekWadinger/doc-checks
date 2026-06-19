# doc-checks

<p align="center">
  <img src="assets/logo.png" alt="doc-checks logo" width="160">
</p>

Self-healing documentation checks, distributed as [pre-commit](https://pre-commit.com)
hooks. They fail the commit when markdown docs drift from reality:

| Hook | What it catches |
|------|-----------------|
| `doc-check-make-refs` | `make <target>` references in markdown that no longer exist in the Makefile |
| `doc-check-cd-refs` | `cd <path>` references pointing at directories that no longer exist |
| `doc-check-py-imports` | Project-local imports in markdown code fences that no longer resolve |
| `doc-check-trees` | ASCII project-tree entries in markdown pointing at files that no longer exist |
| `doc-check-env-vars` | Required Pydantic `BaseSettings` fields missing from `.env.example` |
| `doc-check-endpoints` | Endpoints documented in a README that no longer exist as router decorators in code |
| `doc-check-mermaid` | Broken Mermaid diagram syntax (via `mmdc`, skipped if not installed) |

## Use in your project

Add the hooks to your repo's `.pre-commit-config.yaml` — pre-commit installs
this package into an isolated environment, no manual setup needed
(hook definitions: [`.pre-commit-hooks.yaml`](.pre-commit-hooks.yaml)):

```yaml
repos:
  - repo: https://github.com/MarekWadinger/doc-checks
    rev: v0.1.0  # use the latest tag
    hooks:
      - id: doc-check-make-refs   # requires a Makefile at your repo root
      - id: doc-check-cd-refs
      - id: doc-check-py-imports
      - id: doc-check-trees
      - id: doc-check-env-vars     # for Pydantic-Settings projects
      - id: doc-check-endpoints    # needs services configured (see below)
      - id: doc-check-mermaid
```

Pick only the hooks that fit: `doc-check-make-refs` fails when it finds no
Makefile targets to validate against, so skip it in repos without a Makefile.
`doc-check-env-vars` and `doc-check-endpoints` pass trivially until they find
something to validate (a Pydantic `BaseSettings` class / a configured service),
so they're safe to leave enabled even where they don't yet apply.

To bypass a hook for one commit (e.g. a false positive you'll fix separately):

```bash
SKIP=doc-check-mermaid git commit -m "..."
```

### Hooks

#### `doc-check-make-refs`

Fails when markdown mentions `make <target>` and the target is not defined in
any configured Makefile. Makefiles are discovered recursively: a reference is
validated against the configured root Makefile(s) **plus** any Makefile in the
doc's own directory or sub-directories, so a monorepo sub-project
(`server/README.md` → `server/Makefile`) works without per-section config. A
target defined only in a *sibling* sub-project stays flagged. Config keys
(`make_refs:`): `makefiles`, `ignore_targets`, `scan_globs`, `exclude_globs`.

#### `doc-check-cd-refs`

Fails when markdown contains `cd <path>` and the directory does not exist in
the repo. Paths under `ignore_prefixes` (default: `/Users/`, `/home/`) are
skipped as machine-specific. Config keys (`cd_refs:`): `ignore_prefixes`,
`scan_globs`, `exclude_globs`.

#### `doc-check-py-imports`

Fails when a Python code fence in markdown imports a project-local module
that no longer resolves. Only top-level packages listed in `project_packages`
(default: `src`, `lib`, `scripts`) are validated — third-party and stdlib
imports are ignored. Config keys (`py_imports:`): `project_packages`,
`scan_globs`, `exclude_globs`.

#### `doc-check-trees`

Fails when an ASCII project-tree in markdown lists a file or directory whose
basename no longer exists anywhere in the repo. Detects box-drawing trees
(`├──`, `└──`, `│`) and the common ASCII renderings (`|--`, `` `-- ``, `+--`).
Only branch lines are validated, and matching is by basename — a renamed
parent directory won't trip the check, a deleted leaf will. Placeholders
(`...`, `etc.`), wildcards (`*.py`), and `..` parents are skipped, as are
`ls -F` / `tree -F` type indicators. To opt a conceptual box-drawn diagram
(decision tree, struct/field listing) out of validation, tag the fence
info-string (` ```text no-trees `) or precede it with
`<!-- doc-checks: ignore-trees -->`. Config keys (`trees:`): `ignore_names`,
`prune_dirs`, `scan_globs`, `exclude_globs`.

#### `doc-check-env-vars`

Fails when a *required* Pydantic Settings field (one without a default) is
missing from `.env.example`. AST-parses every module matched by `config_globs`
(default: `**/config.py`, `**/settings.py`, `**/conf.py`), finds classes
inheriting from a name in `settings_base_classes` (default: `BaseSettings`),
applies each class's `env_prefix`, and uppercases the field name to get the
expected env var. Fields *with* a default, stale `.env.example` entries, and
README coverage are reported informationally — only missing required fields
fail. Passes trivially when no settings classes are found. Config keys
(`env_vars:`): `config_globs`, `exclude_globs`, `env_example`, `readme_file`,
`settings_base_classes`, `ignore_fields`, `known_non_config_vars`.

#### `doc-check-endpoints`

Fails when a README documents an endpoint that no longer exists in code
(*code is the source of truth*; undocumented routes are only informational).
AST-parses router decorators (`@router.get("/x")`, `@app.post(...)`) in each
configured service's `endpoints_dir` and compares normalized paths against the
endpoints referenced in its `readme_files`. Internal routes (`/health`,
`/docs`, …) are skipped, and `strip_path_prefixes` (default: `^/api/v\d+`)
removes versioned roots before matching. Empty `services` by default, so the
check passes until you configure at least one. Config keys (`endpoints:`):
`services`, `internal`, `router_names`, `methods`, `strip_path_prefixes`.

```yaml
# .doc-checks.yaml — configure a service for doc-check-endpoints
endpoints:
  services:
    api:
      endpoints_dir: src/api/endpoints   # dir of *.py with router decorators
      readme_files:
        - README.md
```

#### `doc-check-mermaid`

Validates ` ```mermaid ` fences with `mmdc`
(`npm install -g @mermaid-js/mermaid-cli`). Silently passes when `mmdc` is
not installed, so contributors without a Node toolchain aren't blocked; set
`require_mmdc: true` or `DOC_CHECK_MERMAID_REQUIRE=1` (e.g. in CI) to fail
instead. Config keys (`mermaid:`): `require_mmdc`, `scan_globs`,
`exclude_globs`.

All hooks share `scan_globs` / `exclude_globs`; `CHANGELOG.md` is excluded by
default because its entries are historical and may legitimately reference
things that no longer exist.

#### Pair with: `lychee` (broken links)

Link checking is out of scope here — pair the hooks above with the official
[lychee](https://lychee.cli.rs) hook or a local one. See this repo's
[`.pre-commit-config.yaml`](.pre-commit-config.yaml) and
[`.lychee.toml`](.lychee.toml) for a working offline-mode setup (external
URLs skipped locally, validated in CI).

#### Pair with: `commitizen` (changelog)

There is deliberately no `doc-check-changelog` hook. Enforce
[conventional commits](https://www.conventionalcommits.org) with
[commitizen](https://commitizen-tools.github.io/commitizen/), then *generate*
`CHANGELOG.md` from the commit history — nobody hand-edits it, so there is
nothing to drift:

```yaml
  - repo: https://github.com/commitizen-tools/commitizen
    rev: v4.8.3
    hooks:
      - id: commitizen          # rejects non-conventional commit messages
        stages: [commit-msg]
```

```toml
# pyproject.toml
[tool.commitizen]
version_provider = "pep621"   # read/bump version from [project].version
update_changelog_on_bump = true
```

`cz bump --changelog` then bumps the version, rewrites `CHANGELOG.md`, and
tags in one step — this repo automates exactly that on push to main (see
[`.github/workflows/bump.yml`](.github/workflows/bump.yml)).

### Per-repo configuration

Defaults live in the packaged [`src/doc_checks/config.yaml`](src/doc_checks/config.yaml).
To override, either drop a `.doc-checks.yaml` at your repo root or add a
`[tool.doc-checks]` table to `pyproject.toml` — sections are merged key-by-key
over the defaults, so you only state what differs:

```yaml
# .doc-checks.yaml
make_refs:
  ignore_targets: [deploy]   # documented but defined in another repo's Makefile
py_imports:
  project_packages: [myapp]  # which top-level imports count as project-local
mermaid:
  require_mmdc: true         # fail instead of skip when mmdc is missing
```

```toml
# pyproject.toml — same keys, TOML syntax
[tool.doc-checks.make_refs]
ignore_targets = ["deploy"]

[tool.doc-checks.py_imports]
project_packages = ["myapp"]
```

If both are present, `.doc-checks.yaml` wins on conflicting keys. This repo's
own [`.doc-checks.yaml`](.doc-checks.yaml) is a working example.

### CI

Run the same hooks in CI so drift can't merge:

```yaml
# .github/workflows/doc-checks.yml
jobs:
  doc-checks:
    runs-on: ubuntu-latest
    env:
      DOC_CHECK_MERMAID_REQUIRE: "1"
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - run: npm install -g @mermaid-js/mermaid-cli
      - uses: pre-commit/action@v3.0.1
```

## Developing this repo

Requires [uv](https://docs.astral.sh/uv/). Optional: [lychee](https://lychee.cli.rs)
(`brew install lychee`) and `mmdc` (`npm install -g @mermaid-js/mermaid-cli`).

```bash
make install   # uv sync + pre-commit install
```

Hooks run automatically on `git commit`. To run manually:

```bash
make doc-check             # run all checks via the runner
make doc-check-make-refs   # run a single check
make pre-commit            # run every hook against all files
```

### Adding a check

Add a `check_<name>.py` module in [`src/doc_checks/`](src/doc_checks/)
exposing a `run` function — the runner auto-discovers it:

```python
from doc_checks import CheckResult

def run(files: list[str] | None = None) -> CheckResult:
    return CheckResult(passed=True, message="...")
```

Then register a console script in [`pyproject.toml`](pyproject.toml) and a
hook entry in [`.pre-commit-hooks.yaml`](.pre-commit-hooks.yaml) to make it
available to consumers.
