---
name: coop
description: Route explicit Agent Co-op launch or join requests to the canonical CLI. Use when the user asks Codex or Grok to use Agent Co-op, invokes the coop skill, joins a live Co-op, or when COOP_SESSION_ID already binds the harness. Never activate only because an ordinary task appears suitable for multiple agents.
---

# Agent Co-op — shared Codex/Grok launch entrypoint

This skill is a convenience wrapper over `coop start`. It is not a separate
runner. Loading this skill does not start provider processes.

## Launch rule

A run can spend provider quota. Launch only after the user explicitly invokes
the coop skill or asks to use Agent Co-op. The request must identify an
existing item, a new goal, or the whole board. Never infer launch authorization
from task suitability.

Route an unbound request as follows:

1. For a named existing item, run `coop start --item <id>`.
2. For an explicit whole-board request, run `coop start --all`. Never
   substitute `--all` for a missing target.
3. For an explicit new goal, run
   `coop item create --title "<goal>" --objective "<goal>"`. Read the resulting
   item with `coop item show <id> --packet`, then run
   `coop start --item <id>`. Agents complete and peer-accept the draft contract
   before substantive work.
4. Without a target, run `coop tasks` and ask the user to select an item. Do
   not guess.

If the workspace has no board, run `coop init --workspace .` before launch.
The CLI discovers the nearest board from the current directory. Use `--board`
only when the user explicitly targets a different board. Inside `coop monitor`,
TASKS Enter creates and launches a new goal. Dashboard `/coop` starts an
existing highlighted item.

## Bound turn

If `COOP_SESSION_ID` is set, do not start or bind another session. Never pass
`--as`. Follow the hydrated `next_action`. If no snapshot is present, run
`coop status --json` and follow the returned command. Stop when
`next_action.kind` is `idle`.

The autonomous runner prompt is self-contained. A bound turn does not need
`SKILL.md` or `COOP_GUIDE.md` for the ordinary loop.

## Interactive attach

If the user explicitly asks to join or debug instead of launching a product
run, bind before any agent mutation with
`coop session start --as <claude|codex|grok> --export`.

Use `coop --help` for command flags. Keep every operative
contract, question, answer, handoff, decision, receipt, review, and completion
on the board.
