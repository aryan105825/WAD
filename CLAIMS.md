# CLAIMS.md: every claim, the module behind it, and the check that proves it

Run `bash scripts/verify.sh` (quick) or `bash scripts/verify.sh --full`. Each check prints
`PASS <id> <measured>`, `FAIL <id>: <reason>` or `SKIP <id> <reason>`, and the same data is
written to `results/verify.json` with the commit it ran against.

Mode column: **quick** runs it live in under 90 s. **cited** means quick mode re-validates the
committed `results/*.json` (each must cite a commit that exists in git history) and the full run
produces it. **full** means only `--full` (or `--kit-only` / `--run-only`) runs it.

| Claim | Module | Check | Mode |
|---|---|---|---|
| Mandates are generic | `mandate_lint.py`, `mandates/*` | R6: lint clean against every task file and the other track's text | quick |
| The lint can actually detect a leak | `mandate_lint.py`, `factory/selftest/test_lint.py` | K1: leaky fixture fails, clean fixture passes, allowlist guard reports hits | quick |
| Each stage builds and serves clean, with no outbound network at runtime | `sandbox.py`, `gate.py` | R1: per-stage gate at each `green/stage-N` tag | cited (full runs it) |
| The egress probe works | `sandbox.py` | K6: TCP 1.1.1.1:443 and DNS both blocked inside the sandbox | full |
| Later stages don't break earlier ones | `gate.py` (inherit), `ratchet.py check` | R2: inherited acceptance and ratchet green at each green tag, waivers reported | cited (full runs it) |
| Every defect found is permanent and replayable | `ratchet.py` | R3: counterexample VIOLATED at found commit, HELD at fixed commit (1 replay in quick, `--max-replay N` in full) | quick (SKIP without Docker) |
| The ratchet works end to end in isolation | `ratchet.py`, `sandbox.py` | K7: throwaway repo, racy commit then fixed commit, verify-history | full |
| The hammer catches concurrency violations | `hammer.py`, selftest service | K3: racy service caught on at least 9 of 10 seeds, safe twin 0 of 10 false positives | cited (full runs it) |
| Bad work is detected mechanically | `gate.py` | K4: safe service gates green; K5: racy service gates red and writes evidence JSON | full |
| UX checks behave as specified | `htmlcheck.py`, `docs/APP_RUBRIC.md` | K2: a good page passes all six checks and a bad page fails all six | quick |
| Recovery follows the rework budget | `gate.py`, `stage_advance.sh`, `costreport.py` | R9: every item with `max_rework` consecutive gate failures is followed by revert evidence | quick |
| Code is traceable to the room | `trace.py` | R4: every stage commit has trailers and its Item id appears in the room export | quick |
| No human wrote stage code | `trace.py` | R5: zero stage commits with a non-`seat-` author | quick |
| Cost and time are measured | `costreport.py` | R7: `results/cost.json` equals numbers recomputed from `evidence/` | quick |
| FACTORY.md numbers are real | `costreport.py --inject` | R8: FACTORY.md cost block equals the block regenerated from evidence | quick |
| Results are not hand-written | all of `results/*.json` | RES: every file cites a commit that exists in git history | quick |

## Notes

- **Per-item page checks on served pages** (the old R10) were cut in the v2 pack. K2 proves the
  checker itself is correct; whether a given UI item's page passes is recorded in that item's gate
  evidence, because the Planner wires `htmlcheck.check_page` into the item's acceptance check.
- **Docker**: without it, R3 is reported as SKIP in quick mode, and every Docker-backed check
  fails in full mode. Nothing is marked PASS without being measured.
- **Honest limits**: the numbers in FACTORY.md and `results/` are only as good as the run that
  produced them. `verify.sh` checks that they are internally consistent and traceable to commits,
  and re-measures what it can, but it cannot prove a run was fully autonomous beyond the git
  author and room-export checks in R4 and R5.
