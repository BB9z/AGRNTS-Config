---
name: "aky-review-resolve"
description: "Analyze unresolved GitHub PR review comments; fix and resolve after confirmation."
---

# Resolve PR review comments

1. Run `scripts/review_threads.py list` to fetch unresolved threads of the current branch's PR.
   - If it prints "No unresolved review comments found.", tell the user and stop.
   - Each line is one thread as JSON: `id`, `path`, `line`, `isOutdated`, `hunk` (last lines of the diff ending at the commented line), `comments` (first is the review, the rest are replies).
2. Analyze each thread:
   - Read the referenced file and examine related definitions or usages to fully understand the scope and intent of the feedback. Do not limit your analysis to just the specific line number.
   - An outdated thread may already be addressed by later commits; verify against the current code.
   - Decide FIX (actionable) or SKIP (conflict, already resolved, or invalid).
3. Report to the user in their language, one entry per thread: `path:line`, the point of the comment, FIX with the planned change or SKIP with the reason. Then stop and wait for confirmation, unless the invocation explicitly authorizes acting directly.
4. For the threads the user confirmed (possibly a subset; leave the rest untouched):
   - FIX: apply the change, then `scripts/review_threads.py resolve THREAD_ID --reply "✅ {description of changes made}"`
   - SKIP: `scripts/review_threads.py resolve THREAD_ID --reply "❌ {reason for rejection}"`