#!/usr/bin/env bash
# PostToolUse (Write|Edit|MultiEdit): check the markdown file Claude just wrote.
#   1. markdownlint (MD013 line length off), when npx is available;
#   2. doc-check-links and doc-check-code-refs on that file, from this plugin's own source (needs uv).
# The checks may rewrite the file (links, moved line anchors), like a formatter. Anything rewritten or
# still wrong goes back to Claude (exit 2 + stderr); the write itself already happened, nothing is blocked.
# A tool that is missing is skipped, never an error: the hook must not get in the way of an install without it.
set -u
f=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))' 2>/dev/null) || exit 0
case "$f" in *.md|*.markdown) ;; *) exit 0 ;; esac
[ -f "$f" ] || exit 0
dir=$(cd "$(dirname "$f")" && pwd) && f="$dir/$(basename "$f")"
report=""

if command -v npx >/dev/null 2>&1; then
  out=$(npx --yes markdownlint-cli@0.48.0 --disable MD013 -- "$f" 2>&1) || report+=$'markdownlint:\n'"$out"$'\n'
fi

if command -v uv >/dev/null 2>&1; then
  # Run from the file's folder so the checks take the file's own git repository as the root.
  checks="doc-check-code-refs"
  git -C "$dir" rev-parse --show-toplevel >/dev/null 2>&1 && checks="doc-check-links doc-check-code-refs"
  for check in $checks; do
    out=$(cd "$dir" && uv run --quiet --project "${CLAUDE_PLUGIN_ROOT}" "$check" "$f" 2>&1) || report+="$out"$'\n'
  done
fi

[ -z "$report" ] && exit 0
{ echo "doc-checks on $f (fix what is still wrong; rewrites were made on disk, re-read before editing):"; echo "$report"; } >&2
exit 2
