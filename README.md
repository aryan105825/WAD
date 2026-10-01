# RATCHET: a dark factory that can't move backwards

AI factories grade their own homework, regress in later stages, and leak the task into their
mandates. RATCHET makes rejection mechanical, replayable and permanent.

Four generic seats (Planner, Builder, Breaker, Steward) exchange schema'd evidence. The Breaker is
black-box, runs on a different model family, and may reject only with a seeded, replayable
counterexample. Every counterexample becomes a permanent test that gates all later stages.

## Quickstart (3 commands)

```bash
cp .env.example .env                 # then fill in the Featherless values
bash scripts/hour0_smoke.sh          # egress, Python, Featherless, agent-drive, room-export probes
bash scripts/verify.sh               # quick mode, under 90 s (script is written in Milestone 3)
```

Requirements: Python >= 3.12 (stdlib only), Docker >= 24, git, bash, tar.

## Repo map

| Path | What it is |
|---|---|
| `factory/lib/` | `common.py`, `evidence.py`, `sandbox.py`, `hammer.py`, `htmlcheck.py` |
| `factory/bin/` | `gate.py`, `ratchet.py`, `mandate_lint.py`, `trace.py`, `costreport.py`, `stage_advance.sh` |
| `factory/selftest/` | A real probe service (safe/racy) and tests that prove the kit works |
| `mandates/` | Generic seat mandates (no task vocabulary) |
| `scripts/` | `hour0_smoke.sh`, `verify.sh` |
| `docs/APP_RUBRIC.md` | Generic UX checklist the Planner turns into observable checks |
| `evidence/`, `ratchet/`, `results/` | Produced by the real run and committed |
| `tasks/` | Task text pasted verbatim, never authored by the kit |

## Documents

- [ARCHITECTURE.md](ARCHITECTURE.md): flow, contracts, evidence schema, why the gate shares a network namespace
- FACTORY.md: stand-up steps, rationale, measured costs, detection and recovery (Milestone 3)
- CLAIMS.md: each claim mapped to the check that verifies it (Milestone 3)

## Status

Milestone 1 is in progress: the library layer and hour-0 smoke script are written first; seats,
selftest service and gate follow. Files not yet present are listed in the build manifest.
