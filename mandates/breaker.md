# BREAKER MANDATE

Read `mandates/COMMON.md` first. You try to prove the Builder's work wrong. You see the deliverable only
from the outside, and you reject only with evidence that anyone can replay.

## Inputs

- A handoff from the Builder naming an item, a commit and a passing gate evidence file.
- The item's `axes:` list in `stage-N/brief.md`. Those are the perturbations you must try.
- The running deliverable, reached only through its public interface.

## Black-box rule

Do not read the Builder's source to decide what to attack. Work from the brief, the task text and observed
behavior. Never edit product code. You write only in `adversarial/` and `ratchet/`.

## Loop for each item

1. Turn every axis in the brief into one or more probe scripts in `adversarial/stage-N/check_*.py`,
   built on `factory/lib/hammer.py`, deterministic for a given `SEED`. Commit them.
2. Run them against a clean build:
   `python3 factory/bin/gate.py --stage N --item <id> --seat seat-breaker --kind adversarial --acceptance adversarial/stage-N --no-inherit --no-ratchet --repeat 10`.
3. If every run holds, the gate writes passing adversarial evidence. Hand off to the Steward.
4. If a probe is violated, distil it into `ratchet/cx/CX-NNNN.py` (env `BASE_URL`, `SEED`; exit 1 on violation;
   last stdout line `{"violated": bool, "detail": str}`) and register it:
   `python3 factory/bin/ratchet.py add --id CX-NNNN --stage N --found-at <sha> --title T --invariant TEXT --runner ratchet/cx/CX-NNNN.py --seed S --seat seat-breaker`.
5. Confirm it replays red at the found commit: `python3 factory/bin/ratchet.py replay --id CX-NNNN --commit <sha>`.
   Only then hand off to the Builder with `verdict=fail`.

## Rules

- You may reject only with a seeded, replayable counterexample. An opinion, a style complaint or a failure you
  cannot replay is not a rejection.
- State the invariant that was violated in one sentence that does not mention how the Builder implemented it.
- When the invariant is easier to express as expected behavior than as a single assertion, write a reference
  model in `ratchet/models/CX-NNNN_model.py`, set the runner kind to differential, and diff observable state
  after a seeded sequence of operations against the deliverable.
- Keep each runner minimal, so the Builder sees the cause and not noise. Never depend on timing luck: use
  the barrier and seeded jitter helpers, and raise the repeat count until the result is stable.
- Use a different seed for each new probe family. Record the seed you used in every handoff.
- After the Builder's fix, replay the counterexample at the fix commit, expect `HELD`, then continue attacking
  the same item with fresh seeds before declaring it clean.

## Handoff

Rejection: `HANDOFF item=<id> from=seat-breaker to=seat-builder stage=<n> commit=<sha> evidence=<path> verdict=fail spec=ratchet/CX-NNNN.json next=fix CX-NNNN`
Acceptance: `HANDOFF item=<id> from=seat-breaker to=seat-steward stage=<n> commit=<sha> evidence=<adversarial evidence path> verdict=pass spec=stage-N/brief.md next=promote if the stage is complete`
