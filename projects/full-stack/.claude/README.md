# Custom Agents & Commands

Project-specific agents (invoke with `@`) and slash commands (invoke with `/`) available in this repo.

## `@aky-explorer`

Finds where things live in the codebase — best for "where is X / what handles Y". Low cost, and gets faster the more you use it.

## `/aky-plan-rethinking`

Pressure-test the current plan or approach — surface problems and propose a simpler one. Use before committing to a design.

## `/aky-coding-post-simplify`

Call after writing code — proposes simplifications. Suggests only, does not edit.

## `/aky-rule-update-suggestion`

At the end of a session, get suggestions on which docs to update based on what came up. Lists only substantive changes.

## `/aky-handoff-before-compact`

Manually dump the current context to a handoff.md before compaction.

## `/aky-model-routing`

Break a task into subtasks and route each to the cheapest capable model, to cut cost.

## `/aky-review-resolve`

Fetch code review comments from a PR and have the agent resolve them directly, without confirmation. Best run in the session already associated with that PR.

## `/aky-review-staged-commit`

Review the changes already in the git staging area; commit if no issues are found. Requires `difftastic` (`brew install difftastic`). You must stage your changes yourself — the skill never touches the staging area.
