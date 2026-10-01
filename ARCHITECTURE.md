# ARCHITECTURE

## Flow

```
task ──> Planner ──> task brief: items, checks, "axes:" (perturbations for the Breaker)
                         │
                         ▼
                      Builder ──> commit (trailers Item/Seat/Room-Ref) ──> HANDOFF envelope
                         │
                         ▼
   gate.py:  git-archive snapshot ─> docker build ─> run --network none ─> healthy
             ─> egress probe ─> stage-N acceptance ─> inherited stage-k ─> ratchet ─> evidence JSON
                         │ pass
                         ▼
                      Breaker (black-box, other model family) ── hammer ──> counterexample?
                         │ yes: ratchet add (red at found_at) ──> Builder fixes ──> ratchet fixed
                         │ no : adversarial evidence (pass)
                         ▼
                      Steward ──> promotable? ──> tag green/stage-N ──> copy to stage-(N+1)
                         (3 failed reworks on an item: revert to green, Planner re-slices)
```

## Seats

Planner, Builder, Breaker, Steward. Each seat has its own git identity (`seat-*`), so a commit by
anyone else is detectable. Mandates live in `mandates/` and contain no task vocabulary.

## Evidence layout

```
evidence/stage-{N}/{item}/{seq:03d}-{seat}-{kind}.json    kind: gate|adversarial|review|promotion|revert
ratchet/{CX-NNNN}.json   ratchet/cx/{CX-NNNN}.py   ratchet/models/{CX-NNNN}_model.py (optional)
results/*.json
```

Evidence JSON (schema 1): `schema, stage, item, seat, kind, commit, started_at, finished_at, checks[],
verdict (pass|fail|infra), counterexample_id, resources, rework_cycle, budget_exhausted, waivers, room_ref`.
Each check is `{name, cmd, exit, duration_s, ok, stdout_tail}` with the tail capped at 2000 characters.
Files are written atomically with sorted keys, so reruns diff cleanly.

## Library layer (written)

- `factory/lib/common.py`: dataclasses, `load_config()` (the only reader of `FACTORY_*`), atomic JSON, git helpers.
- `factory/lib/evidence.py`: evidence I/O, rework counter, `promotable()`.
- `factory/lib/sandbox.py`: clean-room build and run, shared-namespace driver, egress probe, `Sandbox` context manager.
- `factory/lib/hammer.py`: barrage, duplicate, abandon, `check`/`finish` for check scripts.

Modules import each other as `factory.lib.<module>`; the repo root must be on `PYTHONPATH`
(the gate sets it for drivers via `PYTHONPATH=/repo`).

## Rules the code enforces

- Rework counting: consecutive failing `gate`/`adversarial` evidence for an item, newest first. A pass,
  a `promotion` or a `revert` ends the streak; `infra` verdicts neither count nor reset.
  The budget is exhausted at `FACTORY_MAX_REWORK` failures.
- Promotion: a passing `gate` AND a passing `adversarial` record for the same commit in that stage.
- Build network: `build_network` in `service.json` is `none` or `default`; runtime is always `--network none`.
- Check scripts exit 0 (held), 1 (violated) or 2 (infrastructure error, set by `hammer`'s excepthook),
  and print `{"violated": bool, "detail": str}` as the last stdout line.

## Why the gate shares a network namespace with the service

The service runs with `--network none`, so it has no interface except loopback and cannot be reached
from the host. The test driver is a second container started with `--network container:<service>`,
so it lives in the same namespace and reaches the service on `127.0.0.1:<port>`. This gives us both
properties at once: the service provably has no egress, and the hammer can still talk to it. The same
namespace is used for the egress probe, so the probe tests exactly what the service sees.

## Determinism

Randomness (jitter) comes from `random.Random(seed)`. Evidence is ordered by stage, item and sequence
number. Config errors, missing files and Docker problems raise with a message that says what failed
and how to fix it; nothing is silently swallowed.
