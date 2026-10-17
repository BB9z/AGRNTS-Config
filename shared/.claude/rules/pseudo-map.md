---
paths:
  - "**/*.pseudo.coffee"
---
# Pseudo-code map

A `*.pseudo.coffee` file maps where a feature's code lives, for the next agent about to change it. Never compiled, never imported, no part of the build — CoffeeScript is only the carrier, chosen because indentation alone expresses nesting; keep it syntactically valid so editor folding/highlighting still work. Pairs with the feature's `README.md`: map answers "which code do I touch", README answers "why, and what breaks".

## Write

- `# MARK:` splits the file into pipeline stages, named after what runs at that stage, never after a file. `# MARK: shared` is last.
- A top-level block that corresponds to a real symbol carries that symbol's name, spelled exactly as in source; rename in code → update the map in the same change.
- Statement order = execution order; `if`/`switch`/`for` are real branches, not prose about them.
- A branch that runs deep becomes its own top-level block; mark the call site `# ↓` ("expanded below").
- Code pulled out of an inline stretch has no symbol to carry: give it a descriptive name and mark its definition line with the symbol it's inlined in, so a reader won't grep for it. Skip the mark for placeholder names inside a statement — those already read as pseudo-code.
- A symbol reached from more than one block is defined once, at whichever stage it most naturally belongs to — never duplicated inline. Reserve `# MARK: shared` for symbols with no single natural stage (pure utilities, cross-cutting helpers).
- Reference by symbol, not path — a path is one grep away and the first thing to go stale on rename or split. A call leaving the feature names the module it lands in, since that crossing isn't visible from the symbol.
- A comment adds only what the shape can't show: the condition a branch turns on, what a step produces, which downstream step a value feeds.

## Never

- Why, rationale, trade-offs — README's job.
- Type definitions and field lists. A data shape appears only where a step returns it or a later step reads it, and only the fields that matter there; shapes internal to the feature don't appear at all.
- File or directory inventories.
- Real implementation — stop where the next step is a named call.
