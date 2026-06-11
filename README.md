# doc-checks

Self-healing documentation checks, distributed as [pre-commit](https://pre-commit.com)
hooks. They fail the commit when markdown docs drift from reality:

| Hook | What it catches |
|------|-----------------|
| `doc-check-make-refs` | `make <target>` references in markdown that no longer exist in the Makefile |
| `doc-check-cd-refs` | `cd <path>` references pointing at directories that no longer exist |
| `doc-check-py-imports` | Project-local imports in markdown code fences that no longer resolve |
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
      - id: doc-check-mermaid
```

Pick only the hooks that fit: `doc-check-make-refs` fails when it finds no
Makefile targets to validate against, so skip it in repos without a Makefile.

To bypass a hook for one commit (e.g. a false positive you'll fix separately):

```bash
SKIP=doc-check-mermaid git commit -m "..."
```

### Hooks

#### `doc-check-make-refs`

Fails when markdown mentions `make <target>` and the target is not defined in
any configured Makefile. Config keys (`make_refs:`): `makefiles`,
`ignore_targets`, `scan_globs`, `exclude_globs`.

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
