---
paths:
  - "**/*.decision.md"
---
# Decision docs

A `*.decision.md` file records decisions reached through discussion between Claude and the user, to guide implementation.

When discussing a design or making a decision with the user:

- Discuss step by step, build consensus incrementally; make sure the user fully understands the design
- The user's ideas may be wrong or incomplete — that's normal; use your expertise to help refine the design
- Use this document to communicate; don't restate it in chat, don't use the `AskUserQuestion` tool
- Ask one question at a time; follow up based on the answer until you're 95% confident
- Record settled decisions and open ones needing user confirmation in the decision doc
- When you notice a pending item's description needs updating during discussion, update it directly — no need to ask first
- The user may also edit the file directly, avoid re-reading the whole file. Respect their changes but flag anything that looks unreasonable

## Content requirements

- Label decisions with IDs like A1, B2, Nx for later reference; avoid cross-references between entries
- Each entry must mark who decided: the user or Claude
- The doc records only core decisions; no technical details, no factual descriptions
- Rationale only where its absence would mislead: an entry that reads as redundant without it, or one resting on an assumption that must not be lost. A sentence, not a derivation
- No bold text, no horizontal rules

## Example format

Use this format for new decision docs; don't copy the style of existing project docs

```md
# ... Decisions

## <organize by section>

A1. ...

- Decided by: <user/Claude>
- <note>
- ...

## Open questions

Q1. ...

...

Q2. ...

## Pending

W1. ...

## Future work

- N1. ...
```

## Cleanup principles

When the user asks to clean up a decision doc, consider:

- For implemented items, trim implementation detail
- Merge duplicate entries describing the same thing
- Other simplifications you judge appropriate
