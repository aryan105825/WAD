# FACTORY.md: how RATCHET is stood up, why it is built this way, and what it cost

RATCHET is a dark factory that cannot move backwards. Four generic seats exchange
schema'd evidence. Rejection is mechanical, replayable and permanent. The only human
input during a run is the task dispatch.

Items marked **(confirm)** depend on BAND or challenge-spec details that were assumed
at design time (see "Assumptions"). Confirm each in hour 0 and edit this file to match.

## 1. Stand-up steps (fresh clone to first dispatch)

1. Prerequisites: Ubuntu, Docker 24+, Python 3.12, git, BAND Desktop signed in.
2. `cp .env.example .env` and fill `GROQ_API_KEY` and `GROQ_MODEL`.
3. `bash scripts/hour0_smoke.sh`. Every probe must PASS (egress blocked, Python 3.12,
   Groq 200, plus the two manual steps H4 and H5). If H4 fails, move the Breaker to a
   different native model family in `seats.json`.
4. Create the four seats in BAND as custom ACP agents. Command: `seats/seat-<role>.sh`
   (argument `acp`, working directory the repo root). Each launcher sets the seat's git
   identity and starts OpenCode with `seats/seat-<role>.json`, which pins the Groq model and
   loads `mandates/COMMON.md` plus `mandates/<role>.md`. See `seats/README.md`.
5. `python3 factory/selftest/test_lint.py` and `bash scripts/verify.sh --kit-only`.
   Both must be green before the kit is frozen.
6. Paste the task text verbatim into `tasks/stage-N.md` and the other track's text into
   `tasks/_other_track.md` (lint input only). Run `bash scripts/verify.sh --quick`; R6
   (mandate lint) must pass against both.
7. Have a teammate who did not write the mandates review them by hand.
8. Freeze the kit: `git tag factory-start`. Nothing under `factory/`, `mandates/` or
   `seats.json` changes after this tag.
9. Start the room recording (OBS) before the first dispatch.
10. Dispatch the task to the Planner. This is the last human action.

During the run, after each stage: export the room into `room-export/`, and commit nothing
by hand. After the run: `python3 factory/bin/costreport.py --out results/cost.json --inject FACTORY.md`,
then `bash scripts/verify.sh --full`, then commit `results/`.

## 2. Seats and rationale

| Seat | Role | Why it exists | Model |
|---|---|---|---|
| seat-planner | Slices the task into small items, writes falsifiable checks, writes `axes:` into each brief | Work that cannot be falsified is rejected before any code is written | OpenCode, `groq/openai/gpt-oss-120b` |
| seat-builder | Implements one item at a time, runs the gate before handoff | Single writer of product code keeps history legible | OpenCode, `groq/openai/gpt-oss-120b` |
| seat-breaker | Black-box attacker. Never edits product code. Rejects only with a seeded, replayable runner | A reviewer who cannot show a failing run cannot block work, which removes opinion-based rejection | OpenCode, `groq/qwen/qwen3.8-27b` (different family from the Builder) |
| seat-steward | Repo state, size and clarity review, promotion, green tags, revert on budget exhaustion | Someone must own forward motion and the right to go back | OpenCode, `groq/openai/gpt-oss-120b` |

Why the Breaker runs on a different model family: a builder and an attacker with the same
blind spots agree with each other. Different families make shared blind spots less likely.

Why perturbation axes live in the Planner's per-task brief and not in any mandate: mandates
must read the same for any task. Anything task-shaped belongs in the brief, which is
regenerated per task.

## 3. Cost and time (measured)

All numbers below are produced by `factory/bin/costreport.py` from `evidence/`. They are not
typed by hand. `scripts/verify.sh` check R8 fails if this block drifts from the evidence.

<!-- COST:BEGIN -->
_Not measured yet. After the run, execute:_ `python3 factory/bin/costreport.py --out results/cost.json --inject FACTORY.md`
<!-- COST:END -->

Column meanings: wall clock is earliest start to latest finish of any evidence in the stage;
gate failures count `kind=gate` evidence with verdict `fail`; rework cycles sum, per item,
the longest run of consecutive gate failures; LOC counts lines in source-like files under
`stage-N/`. Dollar cost is not tracked (the cost proxy was cut from scope).

## 4. How bad work is detected

- **Clean-room gate** (`factory/bin/gate.py`): builds from a `git archive` snapshot of the
  commit, never the working tree. Runtime is `--network none` with CPU and memory caps.
  The driver shares the service's network namespace, so checks reach it without any
  outbound path.
- **Egress probe**: every gate run confirms TCP to 1.1.1.1:443 and DNS both fail. A service
  that can reach out fails the gate. `verify.sh` K6 exercises the probe itself.
- **Acceptance plus inheritance**: stage N must pass its own checks, every earlier stage's
  checks (minus recorded waivers), and the ratchet.
- **Ratchet** (`factory/bin/ratchet.py`): every Breaker rejection is a seeded runner.
  A counterexample must be VIOLATED at the commit where it was found, HELD at its fix, and
  HELD in every later stage. `ratchet.py verify-history` proves the first two live.
- **Hammer** (`factory/lib/hammer.py`): concurrent barrages, duplicate submissions and
  abandoned requests. `verify.sh` K3 requires a known-racy service to be caught on at least
  9 of 10 seeds with zero false positives on the safe twin.
- **Page checks** (`factory/lib/htmlcheck.py`): viewport meta, title, labelled inputs,
  no fixed widths over 480px, `lang`, landmark. Planners cite it in UI acceptance checks.
- **Mandate lint** (`factory/bin/mandate_lint.py`): mandates must share no task-specific
  vocabulary with any track text. This automates the swap test.
- **Trace** (`factory/bin/trace.py`): every stage commit carries Item, Seat and Room-Ref
  trailers, its Item id appears in the room export, and its author is a `seat-*` identity.
  Any human-authored stage commit fails R5.

## 5. Recovery

- **Rework budget**: `max_rework` (3, from `seats.json`). Each failed gate or adversarial
  verdict sends the item back to the Builder with the evidence path in the handoff.
- **Revert**: after the budget is exhausted the Steward reverts to the last `green/stage-N`
  tag and writes `kind=revert` evidence. The Planner then re-slices the item smaller.
  `verify.sh` R9 fails if any item hit the budget without a later revert.
- **Infra failures** (gate exit 2) are not verdicts. They are retried and never count toward
  the rework budget. A persistent infra failure is a kit bug: stop, fix the kit outside the
  run, and record it honestly rather than editing evidence.
- **Waivers**: a check may be waived only through `service.json` with an id and a reason.
  Waivers are counted per stage in the cost block.
- **Promotion**: `bash factory/bin/stage_advance.sh N` requires a passing gate and adversarial
  evidence for the same commit, then tags `green/stage-N` and seeds stage N+1 as `seat-steward`.

## 6. Autonomy boundary

The human dispatches the task and nothing else. Seat-only git identities make this checkable
(R5). The kit is frozen at tag `factory-start`, so any later change to it shows in `git diff
factory-start..HEAD -- factory mandates seats.json`.

## 7. Verifying this document's claims

| Command | Time | What it proves |
|---|---|---|
| `bash scripts/verify.sh` | under 90 s target, no cold builds | Lint, trace, cost, results provenance, one live red-to-green replay |
| `bash scripts/verify.sh --full` | 10+ min | Everything above from scratch, with cold builds, 20 hammer runs and replays |

Each check prints `PASS`, `FAIL` or `SKIP` and the measured value. `results/verify.json` keeps
the same data with the commit it ran against. CLAIMS.md maps each claim to its check.

## 8. Assumptions and known limits

- Assumed at design time: 3 people, about 48 hours, one coding-agent subscription, Groq API
  key, the Pocketful track. **(confirm)** all of these in hour 0.
- **(confirm)** the mandate file format, the room-export format, how a seat obtains a message
  reference, whether a fresh clean run counts as a rerun, whether builds must be offline, and the
  caps and timeouts from the challenge spec.
- `service.json` `build_network` defaults to `default`. Set it to `none` if builds must be
  offline, and vendor or pin dependencies so that works.
- Without Docker, `verify.sh --quick` SKIPs the replay and `--full` fails. Nothing is faked.
- Dollar cost, a live dashboard, and browser-driven UI checks were cut. UX is checked
  statically through `htmlcheck.py` and the Planner's per-item observable checks
  (`docs/APP_RUBRIC.md`).
