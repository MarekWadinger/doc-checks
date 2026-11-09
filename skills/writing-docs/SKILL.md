---
name: writing-docs
description: How to write markdown docs that stay true as the code changes (plans, READMEs, reports, design notes, MR descriptions). Use whenever you write or edit a .md file - link every file and code location, point at code by symbol (file.py::function) instead of line number, link between sections, and start long documents with an overview.
---

# Writing docs that stay true

The plugin's hook checks every markdown file you write: markdownlint, then
`doc-check-links` and `doc-check-code-refs`. They rewrite what they can fix
(file references into links, stale line anchors) and report the rest back to
you. Write so there is little left for them to report.

## Link everything

- Every file, folder or code location you mention is a relative link:
  `[settings.py](../src/app/settings.py)`, not a bare path in backticks.
  A path you leave in backticks gets linked by the hook when it resolves to
  exactly one file; one it cannot resolve is reported.
- Tickets, merge requests, pipelines, dashboards and URLs are links too.
- Refer to other sections of the same document with an in-page link
  (`see [Rollout](#rollout)`), never "see below".

## Point at code by symbol, not by line

A line number is wrong after the next edit above it. Name the symbol after
`::` and the hook keeps the anchor on its definition:

```markdown
[validate.py::process_validation](../src/validator/validate.py#L33)
[`client.py::Validator.run`](../src/client.py#L120)
```

- Write `` `path/to/file.py::symbol` `` and the hook turns it into such a link.
- Use `Class.method` for methods; module and class constants work too.
- Use a bare line (`<file>.py:42`) only when there is no symbol to name, such
  as a line inside a long function. It is checked to exist, not kept true.
- For evidence ("this is the code we saw"), link a commit permalink instead:
  it should not move.

## Overview

A document with more than four `##` sections starts with a short overview:
one line per section saying where it stands, each linking to the section.
Keep it in step when a section's status changes.

## Lint

markdownlint must pass, line length (MD013) excepted. The hook reports
findings in the file you just wrote; fix them before moving on. A file you
wrote another way (a script, a shell redirect) skips the hook: check it with
`npx --yes markdownlint-cli@0.48.0 --disable MD013 -- <file>`.
