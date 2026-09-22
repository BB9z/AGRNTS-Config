---
name: "aky-review-staged-commit"
description: "Review staged changes; commit if no issues."
---

# Review staged changes and commit

1. Run `scripts/review_diff.py` to get the staged file list and a diff dump path. Skip `git status`.
   - If it reports no staged changes, tell the user and stop.
   - stdout lists each file with its status and line range (`L<start>-<end>`) in the diff file, for quick navigation.
   - Diff file format (one section per changed file, separated by a blank line):
     - Header line: file path
     - Two-char line prefix: `+` plus a space = added line, `-` plus a space = removed line, two spaces = unchanged context
     - `renamed from <old path>` marks a rename (may have no further content if nothing else changed)
     - `binary file` marks a binary file with no content shown
     - `...` marks a gap where unchanged lines were omitted between two changed regions
2. Read the diff file and review it for issues.
   - Do not fix problems — only report them.
   - Do not run tsc / lint / test; they should have passed already.
3. If issues found: report them in the user's language, then stop. Do not commit.
4. If no issues: commit the staged changes.
   - Commit exactly the files that are already staged. Never `git add` a file that wasn't staged, never unstage, and never use `git commit -a`.
   - Message: Conventional Commits format, one line, no body.
   - When staged changes span multiple unrelated edits, find their common intent (e.g. "improve code quality" / "fix minor issues" / "clean up config") — do not enumerate which functions or variables changed.
5. If a git hook blocks the commit with biome errors: run `./node_modules/.bin/biome check --fix --unsafe`, re-stage, then retry the commit once.
