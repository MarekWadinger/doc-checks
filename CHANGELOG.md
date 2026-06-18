## v0.3.0 (2026-06-18)

### Feat

- add doc-check-trees hook for stale ASCII project-trees (#4)

## v0.2.1 (2026-06-18)

### Fix

- don't flag clone-then-cd install idiom in cd-refs check
- pass make-refs check when no refs exist and no Makefile

## v0.2.0 (2026-06-11)

### Feat

- support [tool.doc-checks] in pyproject.toml as config source

## v0.1.1 (2026-06-11)

### Fix

- **ci**: extract lychee binary from nested tarball directory
- **ci**: python 3.13 for consumer job, lychee 0.24.2 to match config

## v0.1.0 (2026-06-11)

### Feat

- doc-check pre-commit hooks (make refs, cd refs, py imports, mermaid, lychee)

### Refactor

- convert to consumable pre-commit hook repo
