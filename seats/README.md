# Seat launchers

One launcher per seat. Each sets the seat's git author/committer identity, loads
`mandates/COMMON.md` plus the seat's own mandate through an OpenCode config, and
starts `opencode acp` from the repo root. Paths are resolved from the script's own
location, so they work from any clone.

Prerequisites: `opencode` installed, `opencode auth login` done (Groq), and
`chmod +x seats/*.sh`.

## BAND form values (ACP agent tab, Starting point: Custom command)

| Name and Handle | Command | Description |
|---|---|---|
| `seat-planner` | `/home/aryan-rajput/PROJ/WAD/seats/seat-planner.sh` | Planner seat of the RATCHET factory |
| `seat-builder` | `/home/aryan-rajput/PROJ/WAD/seats/seat-builder.sh` | Builder seat of the RATCHET factory |
| `seat-breaker` | `/home/aryan-rajput/PROJ/WAD/seats/seat-breaker.sh` | Breaker seat of the RATCHET factory (black-box) |
| `seat-steward` | `/home/aryan-rajput/PROJ/WAD/seats/seat-steward.sh` | Steward seat of the RATCHET factory |

Same for all four seats:

- Arguments (one per line): `acp`
- Working directory: `/home/aryan-rajput/PROJ/WAD` (the repo root)
- Approval policy: Allow automatically
- Tags: leave blank
- Model: a Groq model from the list after the runtime check passes. Give
  `seat-breaker` a different model family from `seat-builder`.

Replace `/home/aryan-rajput/PROJ/WAD` with your own checkout path if it differs.
