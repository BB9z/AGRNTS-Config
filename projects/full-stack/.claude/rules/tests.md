---
paths:
  - "**/*.test.ts"
---
# Tests

Tests catch real regressions; that is the only thing that justifies their cost. Code is a burden — reason from the principles below to judge whether a test earns its keep and whether its assertions bite. This is a lens, not a checklist.

## Assertions bite

The test for any assertion: if the code under test broke, would this go red? If not, it asserts nothing — rewrite or drop it.

Pin the contract, not an incidental value — match granularity to the real invariant on two axes:

- Logic axis: a regression in the code under test must turn it red.
- Data axis: a legitimate data refresh must not falsely break it, and wrong data must not slip through.

Too loose (existence-only checks like `toBeDefined`, `.length > 0`) and too tight (a literal count or snapshot pinned to generated TEMPLATE/CT data) are the same mistake: neither is fastened to the actual invariant. Assert what the code guarantees — derive an expected count from the same source, assert the structural relation, name the discriminator when two paths share an outcome.

## A test pays its cost with the regression it uniquely catches

- Strengthen an existing test before adding a parallel one.
- Add a test only for a real, uncovered branch — never for coverage's sake.
- Delete a test that catches nothing others don't. But check whether it's the sole guardian of a specific behavior — if so, move it to the right place instead of dropping it.
