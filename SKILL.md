---
name: coop
description: Start or join autonomous Claude, Codex, and Grok coordination on the shared SQLite board. Use for an explicit Co-op request or action, including /coop, or when COOP_SESSION_ID is set. Do not activate for ordinary single-agent work without either trigger.
---

# Agent Co-op launch and behavioral policy

This file is the single behavioral policy reference. Autonomous bound turns
receive a self-contained runner prompt. They do not need this file or
`COOP_GUIDE.md` for the ordinary `next_action` loop. `COOP_GUIDE.md` is the
syntax appendix for human operators and interactive debugging.

## Launch model and product boundary

Loading a skill does not start a run. A run starts provider processes and can
spend quota. Launch only after an explicit user request names Co-op and a goal,
an item, or the whole board. Never launch because an ordinary task appears
suitable for multiple agents.

The skill description helps a harness discover these instructions. Discovery
is not launch authorization. Every launch surface calls the same public runner:

| Surface | Use |
|---|---|
| `coop start --item <id>` | canonical default for one existing item |
| dashboard TASKS Enter | create and launch a new goal from `coop monitor` |
| dashboard `/coop` | start an existing highlighted item |
| harness `/coop <target>` or an explicit “Use Agent Co-op…” request | resolve the target, then call `coop start` |
| `coop start --all` or dashboard `/coop all` | drain the board only after an explicit whole-board request |

Run `coop init --workspace .` once in each working directory or worktree. After
that, the CLI discovers the nearest board from the current directory. Omit
`--board` unless the user targets a different board.

A harness wrapper resolves requests as follows:

1. For an existing item, run `coop start --item <id>`.
2. For an explicit whole-board request, run `coop start --all`.
3. For a new goal, run
   `coop item create --title "<goal>" --objective "<goal>"`. Read the new item
   with `coop item show <id> --packet`, then run `coop start --item <id>`.
   Agents complete and peer-accept the draft contract before substantive work.
4. Without a goal or item, run `coop tasks` and ask the user to select one. Do
   not guess and do not substitute `--all`.

Dashboard TASKS Enter creates and launches a new goal. It needs no second
`/coop` command. Dashboard `/coop` launches an existing highlighted item and is
different from a harness skill invocation. `/coop stop` stops after the current
owned turn. `/coop join` attaches a harness for interactive or debug
coordination.

`coop start` requires an explicit target and all three core providers at recipe
preflight. Use it as the only product runner. Do not call
`agent_coop/coop_autonomous.py` directly. Do not build another orchestrator
inside a harness.

SQLite is the only operative channel. The human supplies the kickoff goal or
contract and starts the run. The human reviews the result after the run ends.
A human board write between the run markers fails the product gate. Agents must
handle questions and recovery during the run.

For logs, status sidecars, dashboard behavior, provider launchers, and worker
transports, read `docs/manual.md`.

## Soft provider profiles

The profiles only bias the initial participant order:

- **Claude:** orchestration.
- **Codex:** code review and deep dives.
- **Grok:** fast tasks and research.

Availability and board-derived `next_action` remain authoritative. The runner
never reroutes directed work. Every provider can perform any legal action. Use
a profile only when multiple legal handoffs or work divisions are equally
valid.

## Workflow recipes and sparse scheduling

Code freezes one deterministic recipe before it creates sessions or the first
run marker. `standard_three` is the default. It activates Claude, Codex, and
Grok in soft-profile order.

`quick_two` is an explicit low-risk optimization, not an availability
fallback. It requires `review_quorum=1` and every exact `allowed_actions` tag
below:

```text
recipe:quick-two
quick:bounded
quick:low-risk
quick:reversible
quick:no-research
quick:no-three-party
quick:no-high-authority
```

Missing or malformed tags fail closed to `standard_three`. Task prose never
selects a recipe. `quick_two` activates the first two soft-ordered providers
and keeps the third as a dormant reserve.

Any of these board events promotes the recipe once to `standard_three`:

- a new needs-input event.
- a `changes` review.
- a contract-version increase.
- a legal action directed to the reserve provider.

The board records `workflow_recipe_promoted`. Agents must not edit recipes.

The runner can overlap at most three turns. Each turn must use a distinct
allowlisted lane:

- separate huddle posts.
- question responses.
- review records.
- handoff responses on distinct items.

The runner serializes these actions:

- implementation work.
- same-item handoffs.
- huddle close.
- review request.
- completion.
- recovery.
- incomplete targets.
- unknown actions.

Do not start parallel work unless `next_action` directs it.

`coop start --herdr` creates read-only mirrors for human observability. Never
use pane text as coordination input. Never type in a run pane between the start
and finish markers. The board remains the only authority.

## A bound agent's loop

If `COOP_SESSION_ID` is set, do not bind again and never pass `--as`.
When `COOP_ITEM_ID` is set, status, queue, inbox, chatter, wake routing, progress,
and completion are scoped to that item. Do not act on another item's work.

1. Run `coop status --json`.
2. Inspect `next_action`, whose stable fields are `kind`, target ids,
   `lease_seconds`, `command`, `required_inputs`, and `choices`.
3. Read the named object when judgment is required (`item show --packet` or
   `huddle show`). Execute `command`; if it is a template, supply only the
   named `required_inputs`. If `command` is null, select one legal `choice`.
4. Consume `coop inbox` when addressed entries are present, then repeat. In an
   item-scoped run this is a filtered peek and intentionally does not advance
   the board-global inbox cursor.
5. Stop the turn when `next_action.kind` is `idle`.

Code—not this skill—selects targets, legal transitions, reclaim flags, leases,
review routing, and completion readiness. Never replace the returned command
with a guessed protocol sequence.

## Rejection recovery reflex

A refused command is coordination feedback, not a dead end. Every rejection
names a stable `reason_code`, machine-checkable `evidence`, and — when the
board can derive one — the canonical repair in `legal_next_actions` (the same
grammar as `next_action`). Recover in this order:

1. Read `reason_code` and `evidence`. Do not re-run the refused command
   unchanged and do not guess a repair sequence.
2. If `legal_next_actions` offers an action, take it: execute its `command`,
   supplying only the named `required_inputs`; if `command` is null, select
   one legal `choice`.
3. An offered `idle` action (typically `awaiting_peer`) means waiting is the
   legal response — a peer already owns the routed work. End the turn; the
   watcher wakes you when the board changes.
4. After the repair commits, retry the original intent once.
5. If the action list is empty, re-run `coop status --json` and follow the
   board's current `next_action`.
6. If the same `reason_code` repeats after its offered repair, stop repairing
   mechanically and escalate with coop's own primitives: route an exact
   `needs-input` question to the peer the evidence names, or open the opt-in
   plan huddle for a multi-writer plan dispute. Never ask `human`, never use
   `admin` mid-run, and never invent a transition the machine did not offer.

Runner stall codes (`no_actionable_participant`,
`actionable_no_board_progress`, `turn_budget_exhausted`) are operator
diagnostics, not agent actions.

## Where agent judgment is expected

- doing the contract's substantive work and producing honest evidence — a
  receipt summary names what was **not** done and where work stopped (its
  stop boundaries), never only successes;
- filling an agent-authored goal contract;
- a huddle proposal, concern, revision, or support statement;
- a review verdict and a concrete reason when requesting changes;
- accepting or declining a handoff;
- wording a peer question or a decision.

Everything else should follow `next_action` mechanically.

## Work whose author identity matters

If a contract requires named peers to independently author, decide, or perform
work, the current owner must not do it on their behalf and must not rely on
`coop say` (which is passive and never creates actionable work). Transfer the
implementation lane with a structured handoff. After the peer accepts, that
peer owns a fresh fenced claim and its authored board writes carry its own
identity.

For mutual peer questions, use this existing route:

1. The current owner hands the item to the first required question author.
2. That peer accepts, then uses `needs-input` to route its exact question to
   another peer.
3. The addressed peer claims and answers; the question author resumes its
   implementation lane.
4. The author hands the item to the next required author, or back to the final
   synthesizer after every named obligation is represented on the board.

An autonomous synthetic session is not unavailable merely because its
`last_seen_at` is old. Idle providers sleep until the runner gives them an
actionable handoff, question, huddle, or review. During a live run, a
`status='running'` peer remains routable; never trigger a partial-output stop
condition from heartbeat age alone.

## Contracts, critique, and review

- The normal kickoff path is a complete human-authored contract. An incomplete
  contract that would force a mid-run human judgment is a kickoff defect.
- A one-line goal is a convenience adapter. Its owner may fill only empty
  fields. The resulting contract is non-executable until a bounded peer huddle
  (maximum two rounds) critiques and a different provider accepts it. A concern
  routes to guarded `item refine`, followed by fresh peer acceptance.
- `coop item create --expand` drafts the missing contract fields from the
  goal with a fast model AT KICKOFF (before run markers), so the item enters
  as a complete human contract and skips the acceptance huddle. The human
  owns the draft exactly as if typed; use it when kickoff speed matters more
  than peer critique of the contract.
- `coop item create --template <name>` starts from a built-in complete
  contract template — zero model calls, deterministic, and cheaper than
  `--expand`; explicit flags and `--contract` still override. Templates
  carry the one-turn routing discipline: the owner routes ALL outbound
  questions in its first turn and each peer answers all questions addressed
  to it in one turn (`coop_templates.ONE_TURN_ROUTING_LINE`; the `--expand`
  drafting prompt embeds the same line).
- Peer review is the binding work-quality critique. One independent provider is
  the default. Use explicit `review_quorum=2` only for a high-risk item; the
  number of registered providers never changes policy silently. A receipt whose
  summary omits a known limitation or stop boundary is grounds for `changes`.
- Huddle participation and binding review quorum are separate. Huddles discuss
  the plan/contract; reviews judge the produced evidence.

## Hard invariants

- Board objects carry contracts, claims, questions, handoffs, huddles,
  decisions, receipts, reviews, and completion. `coop say` is non-binding.
- Never edit projected inbox Markdown or carry operative content in prompt
  files/chat.
- Claim before work. Reviewer and contract acceptor must be independent of the
  owner/author as enforced by code.
- Complete only through the receipt/review/completion gates.
- Ask peers, never `human`, during an autonomous run. Agent wedge recovery is
  the product path; admin answer/release are offline end tools.
- **Expired resume grace:** The preferred owner can use `resume_task` after
  expiry. Other live peers can use the reasoned `claim_task` from status. Do
  not treat the item as stranded.
- **Contract huddle is one-shot.** Once acceptance is accepted, open is refused.
  Post-acceptance multi-writer plan critique uses a plan huddle
  (`implementation_plan` / `huddle open-plan`), never reopens
  `contract_acceptance`. Plan huddle is **opt-in**, never forced globally —
  complete human contracts retain claim → work → receipt → review without it.
- **Lane-scoped claims** (not item mutex) are the keystone for multi-kind
  concurrency — implementation, question_response, and review lanes fence
  independently.
- **Crash durability** is at protocol commit boundaries only. Workspace files
  need a committed checkpoint or receipt to be board-known.
- **Dashboard UI:** the working-task blink (item status indicator) fires only
  while the owning session is live; a claim held by a dead or exited session
  is a zombie and renders static, never as active work.
- The OS/process sandbox remains the security boundary. Co-op protocol purity
  does not make an unsafe task safe.

## Invoking the CLI

| Form | When |
|---|---|
| `coop …` | Installed human-facing command and the shell fallback used in skills and prompts. |
| `<absolute-python> -m agent_coop …` | Machine-routed form in `next_action.command`. Co-op pins the interpreter that loaded the package; it never assumes a bare `python` alias. |
| `python coop.py …` | Source-checkout alias for human operators working in the Co-op repository. |

Execute `next_action.command` verbatim. Do not replace its pinned interpreter
with `python`, `python3`, or another environment.

Board: the nearest board walking up from the cwd, unless `COOP_DB` is set —
`<workspace>/.coop/board.db` first, then the two older layouts
(`<repo>/board.db`, `<repo>/coop/board.db`) for boards created by pre-release
builds, nearest ancestor winning. One
board per working directory, so one per git worktree. Create one for a repo
with `coop init --workspace <path>`. This command installs both harness adapters
and hides them with `.coop/` through the repository's shared
`.git/info/exclude`. It never changes the tracked `.gitignore`. A bound agent
does not need `COOP_GUIDE.md` because `next_action.command` contains the exact
command. Human operators and interactive debug sessions can use the guide for
additional flags.

## Wedge recovery (autonomous path)

When `coop start` detects an expired-but-still-active claim on the selected
item whose owning session has exited, structured status returns a
**recover wedge** command. A bound
agent should:

1. Run the command as-is: `coop recover wedge <claim_id> --reason "<reason>"`
2. Reclaim the orphaned item (your agent re-asserts ownership)
3. File any existing receipt (work on disk, pre-committed to the repo)
4. Request review if needed and complete through the normal gates

This preserves the autonomous acceptance record: recovery and completion are
bound operations, not human admin interventions. Only use `coop admin release`
when the runner is truly hung (not responding, no graceful recovery available).
Do not skip this path for convenience.
