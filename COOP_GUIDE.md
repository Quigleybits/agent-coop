# Agent Co-op — how to coordinate

> The agent behavioral contract lives in [`SKILL.md`](SKILL.md). This guide is the command-syntax appendix.
>
> Human operators and interactive debug sessions use this appendix. Autonomous
> bound turns receive exact commands through `next_action.command` and do not
> need this file for the ordinary loop.
>
> **Product mode:** **autonomous** multi-provider coordination — public
> `coop start`, backed by a content-free wake runtime. **Human = Kickoff + end
> Review only** (mid-run human action = stall).

Human operators and interactive debug sessions use the `coop` CLI over a
shared SQLite board. SQLite is canonical. The Markdown inbox files are
disposable projections. **Messages inform. Contracts and decisions bind.
Claims authorize. Handoffs transfer. Receipts evidence. Reviews validate.**
Autonomous turns follow their structured status and never improvise another
transition.

## Launch and interactive attach

The sole public product runner is:

```
coop start --item 24
# Intentional whole-board drain only:
coop start --all
```

Inside Git, the CLI discovers the nearest board only through the nearest
worktree root (a `.git` directory or `.git` file); outside Git it checks only
the working directory. An explicit `--board <db>` or `COOP_DB` is the deliberate
cross-workspace route. Exactly one of `--item <positive-id>` or `--all` is
required. In the dashboard, TASKS Enter creates, highlights, and launches a
new goal as `--item`. No second `/coop` command is required. Dashboard `/coop`
launches an existing highlighted item and refuses when no task is selected.
Dashboard `/coop all` is the explicit board-wide form. Dashboard launches are
headless on Windows and capture ordered stdout/stderr in
`<board-directory>/.coop-runs/run-*.log`. Every run (dashboard or bare
`coop start`) publishes a sibling `run-*.status.json`. The dashboard's
`runner:` line follows the newest sidecar for that board so each new task
refreshes the line instead of sticking on a prior finished run; it
intentionally contains only operational runner state, not the log path or
duplicated board content.

`coop start --herdr` is an advanced, optional observability flag. It opens one
read-only Herdr mirror pane for each persistent provider. A mirror pane renders
that run's turn-trace events. It is never the worker's stdin or stdout, and
agents do not run inside the panes. The dashboard `runner:` line publishes the
mirror pane IDs as a jump target. Quick-goal and dashboard launches do not
expose a Herdr switch; start the item explicitly to request mirrors.

Co-op resolves one absolute Herdr launcher outside the selected workspace and
uses a filtered client environment. Mirror commands start the installed Co-op
package with only the trace/board bindings they need; provider and unrelated
credential values are not mirror inputs.

**Run panes are read-only for the human between the start and finish markers.**
Do not type into a run pane. A keystroke there is human input inside an
autonomous run, and it fails the acceptance gate. Watch the panes, and send
every instruction through the board.

Harness launchers must likewise resolve a concrete item and invoke that same
command. `coop session run` and
`session start` remain interactive/debug attach surfaces, not alternate
product runners. Every launched provider turn is process-tree owned. Identity
arrives through `COOP_AGENT`, `COOP_PROVIDER`, `COOP_SESSION_ID`, and
`COOP_DB`. Targeted runs also export `COOP_ITEM_ID`; status, queue, inbox,
non-binding chatter, wake routing, progress detection, and completion all stay
on that item. Never pass identity flags inside a bound session.

## The loop

Start every turn from the machine-readable boot panel:

```
coop status --json
```

`next_action` is an object carrying the target, exact command, lease, required
judgment inputs, and legal choices. Execute it and repeat until `kind=idle`.
Warnings are facts, never authority to seize an unsafe lane.

Discover and read work (the packet is the complete contract — there is
never a reason to carry a contract by prompt file):

```
coop queue --json
coop item show 7 --packet --json
```

Claim before work; one claim id is your whole protocol handle:

```
coop item claim 7 --intent "implement the parser fix"
```

Checkpoint at reasoning boundaries (`start`, `step`, `risky`, `predone`) —
each returns your packet plus your unread inbox, consumed atomically:

```
coop checkpoint step --claim 12 --note "tests written, red as expected"
```

`blocked` is the guarded stop: the note names the exact stop condition,
your claim closes, the item blocks, and resuming needs a fresh reasoned
claim:

```
coop checkpoint blocked --claim 12 --note "stop condition: touches prod schema"
coop item claim 7 --intent "resuming" --reclaim --reason "credentials arrived"
```

Blocked on a peer agent? Ask the exact question — this closes your execution
claim and routes the item. Autonomous runs reject questions addressed to
`human`:

```
coop needs-input --claim 12 --to grok --question "which region is canonical?"
```

The addressed agent answers through its own claim (only the addressed
agent can):

```
coop question claim 3 --intent "answering"
coop question answer --claim 15 --answer "eu-west-2"
```

The answer opens your fifteen-minute resume window; resume with a fresh
claim (`--reclaim --reason`), or after expiry another agent may take over
with a reason. Your old claim id is dead forever — a delayed write through
it is refused.

If a contract requires each peer to **author** a question (or any other board
object whose author identity matters), hand off the implementation lane before
that peer authors it. The accepted peer asks through its own claim, resumes
after the answer, then hands the lane to the next required author or final
synthesizer:

```
coop handoff create --claim 12 --to codex --reason "codex must author its own question" --summary "contract accepted; mutual Q&A pending" --completed "claude question recorded" --remaining "codex and grok must each author a question" --risks "do not proxy peer authorship" --next-action "accept, ask grok, then hand off to the next author" --proof-ref event:41
coop handoff accept --id 2 --intent "author my required peer question"
coop needs-input --claim 15 --to grok --question "which recovery invariant matters most?"
```

In an autonomous run, an old `last_seen_at` on an otherwise `running` synthetic
session means the provider has not been woken recently; it is not proof that
the peer is dead. Route actionable work to it instead of declaring a partial
stop from heartbeat age alone.

Non-binding chatter rides `say`; addressed messages land in the
recipient's inbox, broadcasts don't:

```
coop say "heads up: schema v4 landed" --to codex
coop inbox --peek --json
coop inbox
```

`--peek` never advances your cursor; a bare `coop inbox` consumes in ordinary
board-wide/interactive use. Under `COOP_ITEM_ID`, inbox reads are filtered to
that item and always peek-only because the inbox offset is board-global;
advancing a filtered cursor could skip unrelated delivery. Only you consume
your own cursor — human reads are peek-only.

## Goal-tasks — define, huddle, peer-accept, execute

A goal-task is the convenience path for a one-line human goal. Claim it and
fill only its empty fields. The generated contract is not executable yet:
status next routes a bounded contract huddle, peer critique, and acceptance.

```
coop item claim 7 --intent "take the goal task" --lease-seconds 7200
coop item define --claim 12 --scope "one short markdown doc" --done-when "the doc exists and a peer approved" --output-contract "SUMMARY.md" --allowed-action "write files" --stop-condition "do not touch code"
coop huddle open --claim 12
coop huddle show 4 --json
coop huddle post 4 --stance proposal --body "one bounded document; no code"
# a different provider critiques, then posts support and closes:
coop huddle post 4 --stance support --body "scope and proof are testable"
coop huddle close 4 --outcome accepted --summary "peer accepts the contract"
```

Huddles allow at most one post per participant per round and at most two
rounds. The contract author cannot accept their own contract. A peer concern
closes with `changes`; status then routes the owner to `item refine`, which can
change only agent-authored fields and requires a fresh huddle:

```
coop huddle close 4 --outcome changes --summary "name the intended audience"
coop item refine --claim 12 --scope "one short document for a new user"
coop item refine --claim 12 --contract contract-patch.json
```

Complete human-authored kickoff contracts skip this extra acceptance gate.
That is the minimal happy path; goal expansion is the flexible adapter.

## Critique and binding review

The contract huddle critiques an agent-authored plan before execution. The
review gate critiques the resulting work and evidence after execution. These
are separate signals; huddle participation never silently increases the
binding review count.

Review quorum is explicit per item. The stable default is one independent
provider; use two only when the kickoff contract marks the work high risk:

```
coop item create --title "Rotate credentials" --objective "replace the exposed key" --scope "staging secret only" --done-when "old key rejected; new key verified" --output-contract "receipt with secret-store reference and test result" --context "security-sensitive change" --allowed-action "update staging secret" --stop-condition "stop before production" --owner codex --review-quorum 2
# or offline before a run:
coop item revise 7 --reason "high-risk boundary" --review-quorum 2
```

Approvals must still come from distinct providers and never from the owner.
Provider count on the board does not alter quorum. Status opens each required
review and returns the exact claim/request/complete commands; idle agents do
not manufacture advisory chatter.

## Evidence — receipts and the linter

Done work is evidenced, never asserted. Write your results file, then
submit the receipt through your claim — the board hashes the file and
mechanically checks every typed reference (`file:` paths re-hash;
`decision:`/`event:`/`debate:` ids must exist on your item):

```
coop receipt submit --claim 12 --path results/parser-fix.md --summary "parser handles nested escapes" --proof "pytest: 495 passed" --proof-ref decision:4 --proof-ref file:results/parser-fix.md
```

A second submission supersedes the first. Replacing your receipt while a
review is open kills that review — deliberately: reviews bind to exact
evidence.

## Reviews — request, claim, verdict

Review is required by default; completion needs the item's review quorum:
one independent approval by default, two when the contract sets
`--review-quorum 2`. Request through your claim once a current receipt
exists (name a reviewer or leave it queue-discoverable). When the quorum is
two, request again after the first approve for the second independent
review:

```
coop review request --claim 12
coop review request --claim 12 --reviewer grok
```

The reviewer claims the review lane (never the owner — self-review is
refused at claim, verdict, and completion), reads the returned packet, and
submits a verdict. `changes` supersedes the receipt and returns the item
to `working`; `approve` clears the path to completion:

```
coop review claim 3 --intent "reviewing the parser evidence" --lease-seconds 7200
coop review submit --claim 18 --verdict changes --body "missing the utf-16 case"
coop review submit --claim 18 --verdict approve
```

The structured status action supplies the runner-sized lease and any safe
reclaim flags. When using the syntax manually, keep the lease longer than the
turn; an expired lease cannot be revived by renewal.

A decision recorded after your approval kills it by event order — the
owner replaces the receipt, re-requests, and a fresh verdict rides the
fresh evidence.

## Decisions — binding, append-only

Record a binding decision through your live implementation claim; it
delivers to the current reviewer when one exists and can never be edited
or retracted:

```
coop decision record --claim 12 --text "kept the v1 wire format" --rationale "migration cost outweighs the cleanup"
```

## Completion — the guarded gate

Completion re-validates everything at once: complete contract, current
receipt at the current contract version, evidence files re-hashed, board
references re-checked, qualifying approval (or the recorded waiver from
`working`), no open question, no pending handoff:

```
coop item complete --claim 12
```

If your evidence changed on disk after approval, completion refuses,
supersedes the receipt, and records every failure — resubmit honest
evidence and earn a fresh verdict.

## Handoffs — transfer only on acceptance

Hand unfinished work to a named agent with the full structured transfer —
six fields and at least one verifiable proof reference. The item freezes
while the handoff is pending; ownership moves only on acceptance:

```
coop handoff create --claim 12 --to codex --reason "context limit reached" --summary "parser fixed, emitter half-ported" --completed "lexer + parser" --remaining "emitter port" --risks "fixture drift" --next-action "port emit_v2 first" --proof-ref event:41
coop handoff accept --id 2 --intent "taking the emitter port"
coop handoff decline --id 2 --reason "wrong specialty — parser work"
```

A decline returns the item to its owner under the resume grace; an
unanswered handoff is a visible wedge, never a silent timeout. After the
owner session exceeds the abandon horizon, a peer releases its stale claim
through `coop recover wedge` and follows the returned action.

## The human operator boundary

Outside an autonomous run, the trusted-local human seeds complete kickoff
contracts and may use audited admin tooling for offline/end recovery. These
commands are not the product's mid-run path; any human board mutation between
the run markers fails the autonomous acceptance gate:

```
coop item create --title "Fix parser" --objective "o" --scope "s" --done-when "d" --output-contract "oc" --context "c" --allowed-action "edit src/" --stop-condition "stop if prod config changes" --owner codex
coop admin answer 3 --answer "offline adjudication after stopped run" --reason "run ended unresolved"
coop admin release 12 --reason "offline cleanup after stopped run" --confirm-process-stopped
```

`item create` also accepts `--contract contract.json` (same fields, flags
override, unknown keys refused) and `--review-waiver "reason"` to waive
the default review requirement. Human contract revision is sessionless,
reasoned, and confined to kickoff or after the run stops; the merged result
must be complete:

```
coop item revise 7 --reason "scope cut to parser only" --scope "parser only" --review-waiver "mechanical rename"
coop item revise 9 --reason "fill the migrated contract" --objective "o" --done-when "d" --output-contract "oc"
```

## Prohibited actions — the board is the channel

- **Never write SQLite directly.** Every mutation goes through `coop`.
- **Never edit a projection** (`inbox/*.md`) to change state — they are
  disposable renders, never read back.
- **Never run an agent mutation outside `coop session run`.** No session,
  no authority.
- **Never continue under a stale, released, completed, or closed claim.**
  Acquire a fresh claim with a reason; late writes are fenced and refused.
- **Never use prompt files, stdin contracts, external chat, or an
  informational message in place of a protocol object.** A task contract
  lives in `items`; a blocking question in `questions`; an autonomous answer
  through the addressed peer's claim. If a message would
  change state, it belongs in a typed object instead.
- **Never type into a Herdr run pane between the start and finish markers.**
  Mirror panes are read-only. A keystroke there is human input inside an
  autonomous run, and the existing human-write gate records the violation.
- **Commands from pre-release builds (`item done`, `item update`, `assign`,
  `debate`, `watch`) are refused.**

## Recovery, briefly

A crashed session stops renewing; its claims go visibly stale. Reclaim
(`--reclaim --reason`) works once exit is confirmed. If the supervisor vanished
unconfirmed, a peer waits for the abandon horizon, then runs:

```
coop recover wedge 12 --reason "peer abandoned after horizon"
```

This may mark the zombie session abandoned and release its stale claim.
`admin answer` and `admin release` remain offline/end tools, not autonomous
run recovery. A visible wedge beats two writers in one workspace.
