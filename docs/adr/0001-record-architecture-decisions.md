# ADR 0001 — Record architecture decisions

Date: 2026-08-26
Status: Accepted

## Context

This project makes a large number of non-obvious choices in ten days: which solver, which
model family, which data source, what to cut when time runs short. Three weeks from now
the reasoning behind any one of them will not be reconstructable from the code alone, and
the selection process for this buildathon includes a panel interview where each choice has
to be defended.

## Decision

Every architecturally significant decision gets a numbered file in `docs/adr/`, stating
the context, the decision, the alternatives rejected, and the consequences — including
what would force a revisit.

"Architecturally significant" means: it constrains later work, it would be expensive to
reverse, or a reasonable engineer would ask "why did you do it that way?"

## Consequences

- The answer to "why is this like this?" is a file, not a memory.
- Rejected alternatives are recorded, so a later reader does not re-litigate a settled
  question or "fix" something that was chosen deliberately.
- Small cost: a few minutes per decision, and the discipline to write it at the time
  rather than afterwards.
