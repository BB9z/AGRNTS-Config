---
name: "aky-model-routing"
description: "Decompose a request and route each subtask to the cheapest capable model tier."
---

# Model tier routing

Decompose the request, route each subtask to the cheapest tier that handles it, then validate before accepting.

1. Label each subtask, one line each:
   - `aky-haiku-drone` - Describing the pattern is cheaper than doing every edit yourself (judge by repetition/volume, not file count).
   - `aky-opus-arbiter` - Only when being wrong costs more than asking, not for trivia.
   - `self` — everything else.
2. Batch same-tier subtasks into one dispatch; run dependent ones in order, dispatch independent ones together.
3. Passing context to arbiter — decide mode: give precise pointers (file:line- range/symbol), not pasted content, so Opus reads the exact slice instead of exploring blindly. Advise mode: paste your proposal and rationale — that's what it judges.
4. Reject and retry a `drone` result that misses the acceptance criteria; flag an `arbiter` decision that contradicts this project's CLAUDE.md conventions.
