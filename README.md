# RATCHET: a dark factory that can't move backwards

> **WeAreDevelopers x BAND: Dark Factory (hackathon edition)** · Track: **💸 pocketful** (wallet and payments, a clean-room Venmo-style service)

AI factories grade their own homework, regress in later stages, and leak the task into their mandates.
**RATCHET makes rejection mechanical, replayable and permanent.**

Four generic coding-agent seats (Planner, Builder, Breaker, Steward) work in a BAND Desktop room and
exchange schema'd evidence. The Breaker is black-box, runs on a different model family from the Builder,
and may reject work **only** with a seeded, replayable counterexample. Every counterexample becomes a
permanent test that gates all later stages, so a fixed bug can never quietly come back.

The only human input during a run is the task dispatch for each stage.

| | |
|---|---|
| **Team** | _<team name and members>_ |
| **Demo video (room recording + walkthrough)** | _<link>_ |
| **Slides** | _<link>_ |
| **Factory description** | [FACTORY.md](FACTORY.md) |

---

## 1. Why this design

The challenge asks for a factory that plans, implements, hands off evidence and checks itself, then
survives four stages without breaking what already works. The failure modes we designed against:

| Failure mode | RATCHET's answer |
|---|---|
| Reviewer opinion ("looks fine" / "looks wrong") | The Breaker may reject only with a seeded runner that fails on replay. No replay, no rejection. |
| Builder and reviewer share blind spots | The Breaker runs on a different model family and never reads the Builder's source. |
| Later stages regress earlier ones | Every stage must pass its own checks, all inherited earlier checks, and every recorded counterexample. |
| Fixed bugs return | A counterexample must be `VIOLATED` at the commit where it was found, `HELD` at its fix, and `HELD` in every later stage. |
| Mandates leak the task | `mandate_lint.py` mechanically fails any mandate sharing task vocabulary with either track's text. |
| Human quietly steering the run | Seat-only git identities, commit trailers, and a trace check against the room export. |
| Runaway rework loops | A rework budget of 3, then a revert to the last green tag and a re-slice by the Planner. |

---

## 2. The band (four seats)

Every seat is a coding agent on the BAND SDK/ACP path, launched through OpenCode. Seats share a runtime;
the Breaker uses a different model family.

| Seat | Owns | Never touches | Model (see `seats.json`) |
|---|---|---|---|
| **seat-planner** | `stage-N/brief.md`, `stage-N/service.json`, `stage-N/acceptance/` | Product code | `groq/openai/gpt-oss-120b` |
| **seat-builder** | The rest of `stage-N/` (source, Dockerfile) | Checks, counterexamples, evidence | `groq/openai/gpt-oss-120b` |
| **seat-breaker** | `adversarial/`, `ratchet/` | Product code (black-box only) | `groq/qwen/qwen3.8-27b` |
| **seat-steward** | Tags, stage copy-forward, promotion/revert/review records | Product code, checks, counterexamples | `groq/openai/gpt-oss-120b` |

Model ids are read from `seats.json` and `seats/seat-*.json`; edit those if your Groq account exposes
different ids. You can also point seats at Featherless or any other OpenAI-compatible provider.

### Mandates are generic (disqualification rule)

Mandates live in [`mandates/`](mandates): `COMMON.md` plus one file per seat. They describe *how a seat
works* (ownership, handoff envelope, when to reject) and name nothing about payments, endpoints, field
names or error codes. Anything task-shaped lives in the Planner's per-task `brief.md`, including the
perturbation `axes:` the Breaker must try.

The swap test is automated: `python3 factory/bin/mandate_lint.py` fails if mandates share vocabulary with
any task file in `tasks/` or with the other track's text (`tasks/_other_track.md`, used as lint input only).
Latest result: [`results/mandate_lint.json`](results/mandate_lint.json).

---

## 3. How a run flows

```
task ──> Planner ──> brief: items, falsifiable checks, "axes:" (perturbations for the Breaker)
                        │
                        ▼
                     Builder ──> commit (trailers Item / Seat / Room-Ref) ──> HANDOFF envelope
                        │
                        ▼
  gate.py: git-archive snapshot ─> docker build ─> run --network none ─> healthy
           ─> egress probe ─> stage-N acceptance ─> inherited stage-k ─> ratchet ─> evidence JSON
                        │ pass
                        ▼
                     Breaker (black-box, other model family) ── hammer ──> counterexample?
                        │ yes: ratchet add (red at found_at) ──> Builder fixes ──> ratchet fixed
                        │ no : adversarial evidence (pass)
                        ▼
                     Steward ──> promotable? ──> tag green/stage-N ──> seed stage-(N+1)
                        (3 failed reworks on an item: revert to green tag, Planner re-slices)
```

Every room handoff starts with one machine-readable line:

```
HANDOFF item=<id> from=<seat> to=<seat> stage=<n> commit=<sha> evidence=<path|-> verdict=<pass|fail|n/a> spec=<path> next=<sentence>
```

Full contracts, evidence schema and the reason the test driver shares the service's network namespace
are in [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 4. The product being built: Pocketful

Task text is pasted verbatim into [`tasks/`](tasks) and never authored by the kit.

| Stage | Task file | Scope |
|---|---|---|
| 1 | `tasks/stage-1.md` | Payments and settlements: send money by handle, requests, bill splits, activity feed with public/private visibility, operator settlements. HTTP API only. |
| 2 | `tasks/stage-2.md` | Wallet screens in the browser (routes by URL) and payment authorizations with one or more captures. |
| 3 | `tasks/stage-3.md` | Statements, historical balances (effective-date and as-known-at queries), payment corrections that preserve the original receipt. |
| 4 | `tasks/stage-4.md` | Refunds and batch corrections; ten idempotent write paths in total. |

Hard part of the track: **money is never created, destroyed or spent twice**, under concurrent transfers,
retries and rounding. The hammer (concurrent barrages, duplicate submissions, abandoned requests) and the
Breaker's differential reference models target exactly this.

### Stage deliverables

Each completed stage is a complete, buildable service in its own folder:

```
stage-1/   stage-2/   stage-3/   stage-4/
  Dockerfile          builds from a clean checkout
  service.json        port, health path, start timeout, caps, env, build_network, waivers
  brief.md            Planner's task copy, items, checks, axes
  acceptance/         Planner's falsifiable check_*.py
  ...                 Builder-owned source
```

> **Status note for judges:** this repository snapshot contains the complete factory kit, mandates, seat
> launchers, task texts and verification harness. Per-stage service folders, `evidence/`, `ratchet/`
> counterexamples and the full room export are produced by the run itself and committed afterwards. Submit
> only the stages you completed; a complete stage 1 is the minimum for eligibility.

---

## 5. Quickstart

Requirements: Python >= 3.12 (standard library only, nothing to `pip install`), Docker >= 24, git, bash, tar.
Seats additionally need [OpenCode](https://opencode.ai) (`npm install -g opencode-ai`), a model-provider key,
and BAND Desktop signed in.

```bash
cp .env.example .env                 # fill GROQ_API_KEY and GROQ_MODEL
bash scripts/hour0_smoke.sh          # egress, Python, provider, agent-drive, room-export probes
bash scripts/verify.sh               # quick mode, under 90 s
```

### Stand up the band in BAND Desktop

1. `chmod +x seats/*.sh` and run `opencode auth login` for your provider.
2. In BAND Desktop, create four **custom ACP agents** (Starting point: Custom command):

   | Name / handle | Command | Arguments | Working directory |
   |---|---|---|---|
   | `seat-planner` | `<repo>/seats/seat-planner.sh` | `acp` | `<repo>` |
   | `seat-builder` | `<repo>/seats/seat-builder.sh` | `acp` | `<repo>` |
   | `seat-breaker` | `<repo>/seats/seat-breaker.sh` | `acp` | `<repo>` |
   | `seat-steward` | `<repo>/seats/seat-steward.sh` | `acp` | `<repo>` |

   Approval policy: *Allow automatically*. Give `seat-breaker` a different model family from `seat-builder`.
   Each launcher sets the seat's git identity and starts OpenCode with `seats/seat-<role>.json`, which
   loads `mandates/COMMON.md` plus the seat's own mandate. See [seats/README.md](seats/README.md).
3. Paste the task text into `tasks/stage-N.md` and run `bash scripts/verify.sh --quick` (R6 mandate lint must pass).
4. Freeze the kit: `git tag factory-start`. Nothing under `factory/`, `mandates/` or `seats.json` changes afterwards.
5. Start the screen recording of the BAND Desktop room **before** the first dispatch (required for the video).
6. Dispatch the stage task to the Planner. That is the last human action for the stage.
7. After each stage, export the room into `room-export/`.

After the run:

```bash
python3 factory/bin/costreport.py --out results/cost.json --inject FACTORY.md
bash scripts/verify.sh --full         # 10+ min: cold builds, 20 hammer runs, replays
```

### Configuration (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `GROQ_API_KEY`, `GROQ_BASE_URL`, `GROQ_MODEL` | – | Provider used for seats; read by `hour0_smoke.sh` only |
| `FACTORY_DRIVER_IMAGE` | `python:3.12.7-slim` | Pinned image for the test-driver container |
| `FACTORY_CPUS` / `FACTORY_MEMORY` | `1` / `512m` | Resource caps for the service under test |
| `FACTORY_START_TIMEOUT` | `30` | Seconds to become healthy |
| `FACTORY_MAX_REWORK` | `3` | Failed cycles before revert and re-slice |
| `FACTORY_BUILD_CACHE` | `0` | Keep off for clean-room fidelity |

Set the caps, concurrency and timeouts to the values published in the challenge spec before the run.

---

## 6. Command reference

| Purpose | Command |
|---|---|
| Gate a commit (clean build, no network, checks, evidence) | `python3 factory/bin/gate.py --stage N --item ID --seat seat-builder` |
| Breaker's adversarial gate | `python3 factory/bin/gate.py --stage N --item ID --seat seat-breaker --kind adversarial --acceptance adversarial/stage-N --no-inherit --no-ratchet --repeat 10` |
| Is the stage promotable? | `python3 factory/bin/gate.py --promotable N` |
| Register a counterexample | `python3 factory/bin/ratchet.py add --id CX-0001 --stage N --found-at SHA --title T --invariant TEXT --runner ratchet/cx/CX-0001.py --seed S --seat seat-breaker` |
| Replay a counterexample | `python3 factory/bin/ratchet.py replay --id CX-0001 --commit SHA` |
| Record the fix | `python3 factory/bin/ratchet.py fixed --id CX-0001 --fixed-at SHA` |
| Prove red-then-green history | `python3 factory/bin/ratchet.py verify-history` |
| Promote a stage | `bash factory/bin/stage_advance.sh N` |
| Lint mandates | `python3 factory/bin/mandate_lint.py --task tasks/stage-1.md tasks/_other_track.md` |
| Traceability | `python3 factory/bin/trace.py --since factory-start --stages 1 2 3 4 --room-export room-export/` |
| Cost and time | `python3 factory/bin/costreport.py --out results/cost.json --inject FACTORY.md` |

Gate exit codes: `0` pass, `1` fail, `2` infrastructure problem (retry; never counts toward the rework budget).

---

## 7. How bad work is caught and recovered

- **Clean-room gate:** builds from a `git archive` snapshot of the commit, never the working tree. Runtime is
  `--network none` with CPU and memory caps. The driver joins the service's network namespace, so checks reach it
  with no outbound path.
- **Egress probe:** every gate run confirms TCP to `1.1.1.1:443` and DNS both fail. A service that can reach out fails.
- **Acceptance plus inheritance:** stage N must pass its own checks, every earlier stage's checks (minus recorded waivers), and the ratchet.
- **Ratchet:** counterexamples are seeded runners with a one-sentence invariant that does not mention the implementation.
- **Hammer** (`factory/lib/hammer.py`): barriers, seeded jitter, duplicate and abandoned requests.
- **Page checks** (`factory/lib/htmlcheck.py`): viewport meta, title, `lang`, labelled inputs, landmark, no fixed widths over 480 px, driven by [docs/APP_RUBRIC.md](docs/APP_RUBRIC.md) for stage 2 UI work.
- **Rework budget and revert:** after 3 consecutive failures on one item the Steward reverts with new commits (never history rewrites), writes `revert` evidence, and the Planner re-slices the item smaller.
- **Waivers:** only through `service.json`, with an id and a reason, and counted per stage in the cost block.

---

## 8. How this maps to the judging criteria

| Criterion (weight) | Where to look |
|---|---|
| **Factory (50%)**: generic, effective, reusable; how far through the four stages; FACTORY.md sufficient to stand it up | `mandates/` and `results/mandate_lint.json` (generic); `stage-*/` and `results/stages.json` (progress); [FACTORY.md](FACTORY.md) (setup, rationale, measured costs, detection and recovery) |
| **App (25%)**: coherent, presentation-ready, responsive UI over maintainable code | `stage-2/` onward; `docs/APP_RUBRIC.md`; Steward `review` evidence under `evidence/` |
| **Agent Teamwork (25%)**: real collaboration and autonomy | `room-export/`, `results/trace.json` (R4/R5), `ratchet/` (review changed outcomes), `git log` showing only `seat-*` authors |

### Verifiable claims

[CLAIMS.md](CLAIMS.md) maps every claim to the check that proves it. Checks print `PASS`, `FAIL` or `SKIP` with
the measured value, and `results/verify.json` records the commit each run used. Nothing is marked PASS without being measured.

Kit self-tests already committed (`results/verify.json`, kit-only mode): lint fixtures, page checker (good page 6/6 pass, bad page 6/6 caught),
hammer (racy service caught 10/10 seeds, safe twin 0/10 false positives), safe service gates green, racy service gates red,
egress probe blocks TCP and DNS, ratchet red-then-green in a throwaway repo.

---

## 9. Submission checklist

- [ ] Public GitHub repo, clonable without BAND Desktop membership
- [ ] `stage-1/` (minimum) through `stage-4/`, each a complete buildable service, only for stages actually completed
- [ ] `mandates/`, `FACTORY.md` (with the cost block injected), and `room-export/` committed
- [ ] Each stage builds and serves from a clean container with **no outbound network** (test with `bash factory/bin/gate.py`)
- [ ] Mandates pass `mandate_lint.py` against all task texts and the other track
- [ ] Video includes the **BAND Desktop room recording** plus a walkthrough
- [ ] All stage commits authored by `seat-*` identities with `Item`/`Seat`/`Room-Ref` trailers
- [ ] Cover image, slides and lablab.ai form filled

---

## 10. Repository map

| Path | What it is |
|---|---|
| `factory/lib/` | `common.py`, `evidence.py`, `sandbox.py`, `hammer.py`, `htmlcheck.py` |
| `factory/bin/` | `gate.py`, `ratchet.py`, `mandate_lint.py`, `trace.py`, `costreport.py`, `stage_advance.sh` |
| `factory/selftest/` | A real probe service (safe and racy twins) and tests proving the kit works |
| `mandates/` | Generic seat mandates (no task vocabulary) |
| `seats/`, `seats.json` | Seat launchers, OpenCode configs, models and git identities |
| `scripts/` | `hour0_smoke.sh`, `verify.sh` |
| `docs/APP_RUBRIC.md` | Generic UX checklist the Planner turns into observable checks |
| `tasks/` | Task text pasted verbatim, never authored by the kit |
| `evidence/`, `ratchet/`, `results/`, `room-export/` | Produced by the real run and committed |
| `ARCHITECTURE.md`, `FACTORY.md`, `CLAIMS.md` | Design, stand-up and cost, claim-to-check map |

## 11. Known limits

- Dollar cost tracking, a live dashboard and browser-driven UI checks were cut; UX is checked statically through `htmlcheck.py` plus the Planner's observable checks.
- Without Docker, `verify.sh --quick` skips the replay and `--full` fails; nothing is faked.
- `service.json` `build_network` defaults to `default`; set it to `none` (and vendor dependencies) if builds must be offline.
- Autonomy is evidenced by git authorship and the room export; it cannot be proven beyond those checks.

## License

MIT (the hackathon requires MIT-compliant, original submissions).
