# .claude Directory Rules

- Shared config is prefixed `aky-`; `local-` prefixed items are local-only (already gitignored)
- When adding a new `aky-` agent/command/skill, update README.md
- Each file under `rules/` is a scoped CLAUDE.md that loads automatically when working on matching files.
