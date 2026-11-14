# Slice notes (H, I, J built in parallel)

Each slice branch writes **only** its own file here (`H.md`, `I.md` or `J.md`) instead of editing
`progress.md`, `explanations.md` or `docs/SPEC.md`. The integrator folds these files into the
shared docs after each merge (CLAUDE.md, "Parallel slices H, I, J", section 6).

Copy this template into your slice's file:

```markdown
# Slice <X> — <name>
Branch: feat/slice-<x>-<name> · Machine owner: <person>

## Status
- Current state, what is left.

## Built (what / why / how)
- ...

## Proof (exact commands and their result)
- `backend` tests: N passed
- `scripts/demo/<demo>.sh`: PASSED (key numbers)

## SPEC rows (status line to paste into docs/SPEC.md)
- <row id>: DONE|PARTIAL (...)

## Shared-file edits (each one under this slice's marker)
- docker-compose.yml: ...
- backend/main.py: ...

## ADRs written
- docs/adr/00NN-....md

## Known gaps
- ...

## Requests to other slices
- (to H/I/J) what is needed, why, agreed? yes/no
```
