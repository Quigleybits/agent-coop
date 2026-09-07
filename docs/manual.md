# Agent Co-op — manual

**Version:** v0.1.0 · **Date:** 2026-09-05 · **License:** MIT

This manual is the full product write-up. The [README](../README.md) is the short
orientation. [`SKILL.md`](../SKILL.md) is the launch and behavioral policy reference.
Autonomous turns receive a self-contained runner prompt instead of that file.
[`COOP_GUIDE.md`](../COOP_GUIDE.md) is the operator and debug flag appendix.

## Contents

1. [What Co-op is](#1-what-co-op-is)
2. [Positioning](#2-positioning)
3. [Intended users](#3-intended-users)
4. [Concepts](#4-concepts)
5. [Manual](#5-manual)
6. [Tests](#6-tests)
7. [Features](#7-features)
8. [Contribution to the space](#8-contribution-to-the-space)
9. [Limitations](#9-limitations)
10. [Security](#10-security)
11. [FAQ](#11-faq)

---

## 1. What Co-op is

| | |
|---|---|
| **Name** | Agent Co-op |
| **Category** | Multi-agent **coordination protocol** / local coordination framework |
| **One-liner** | Co-op is the **shared memory + rules of engagement between coding agents**. |
| **Market-facing** | **Local multi-agent coordination protocol for terminal coding agents.** |
| **Package** | `agent-coop-cli` on PyPI · commands `coop` and `agent-coop` · module entrypoint `python -m agent_coop` |

Agent Co-op is a local-first SQLite coordination protocol for supervised terminal coding
agents. The reference providers are Claude Code, Codex and Grok. The product is the
protocol: claims, contracts, questions, handoffs, hashed receipts, independent review and
decisions. It is not a harness, a model router, a SaaS or a web application.

Harnesses do the thinking and the editing. **Co-op decides who owns what, what counts as
done, and how proof travels** — without chat or a terminal poke as the coordination
channel.

The human role is **kickoff and end review only**. A human writes the contract and starts
the run. Agents then coordinate through the board. A human board write between the run's
start and finish markers fails the product gate.

---

## 2. Positioning

### 2.1 What Co-op is not

| Label | Why not |
|---|---|
| Agent **harness** | Claude Code, Codex and Grok *are* harnesses. Co-op sits *across* them. It does not own tools, context windows or the coding loop. |
| **CLI product** | `coop` is the delivery surface. The product is the protocol and the board. |
| **Model Fusion system** | Fusion ensembles many views of *one* question. Co-op divides labour across *different* owners with independent review. |
| **Agentic engineering workflow** | You *run* workflows on Co-op. Co-op does not prescribe the development lifecycle. |

### 2.2 The three reference products

Co-op was designed against three named systems.

- **Open Engine** (Nate B Jones) is a product thesis. Work leaves chat. A shared ticket
  queue becomes the system of record. Tickets carry scope, definition of done, owner,
  handoffs and receipts.
- **FirstMate** (Kun Chen) is an agent distro. You talk to one first mate. It spawns and
  supervises crewmates in visible session panes, each on a clean git worktree.
- **Model Fusion** (the fusion-harness pattern) is a multi-model ensemble. Parallel models
  answer the same prompt, and a fusion step consolidates consensus and divergence.

### 2.3 Comparison

| Dimension | Agent Co-op | Open Engine | FirstMate | Model Fusion |
|---|---|---|---|---|
| **Category** | Local coordination **protocol** (board + CLI views) | Multi-agent **queue** operating model | **Agent distro** for a hierarchical crew | **Ensemble** harness pattern |
| **Independence unit** | **Provider** (Claude ≠ Codex ≠ Grok) on one board | Agent identity on a shared queue | Crewmate session + worktree | Model instance under one harness |
| **Canonical state** | **SQLite board**; Markdown inbox is projection only | Ticket system | Disk + session-backend state | Prompt and files in the harness workspace |
| **Who drives turns** | Content-free watcher + autonomous runner | Continuous poll on the queue | Zero-token bash watcher | Orchestrator, always |
| **How agents relate** | **Peers** with exclusive claims and cross-provider review | Peers on tickets | **Hierarchy:** captain → first mate → crew | Temporary ensemble on the same task |
| **Work split** | Different items, different owners | Ticket backlog pickup | Parallel tasks, one worktree each | **No split** — same question |
| **What "done" proves** | Receipt paths + required independent reviews | Ticket closed with outcome evidence | Landed pull request or scout report | Gate green + fused decision |
| **Dependencies** | Python 3 stdlib + SQLite | Queue vendor + agent skills | Bash + git + gh + session backend | Host harness + multi-model access |

You can run a fusion pass *inside* a Co-op item. You can adopt FirstMate-style wake absorb
in the runner. You can keep Open Engine's queue vocabulary in contracts. None of that
collapses Co-op into those products.

### 2.4 Against vendor-native agent teams — the neutrality argument

Every platform vendor now ships some form of multi-agent feature. Each one coordinates that
vendor's own agents. The scheduler, the state store and the review model all sit inside one
vendor's product.

Co-op takes the opposite position. **The board is vendor-neutral and lives on your disk.**
Three consequences follow.

1. **Heterogeneity is the design target, not a compatibility mode.** Claude, Codex and Grok
   are peers on the same board with the same authority rules. A review by a different
   provider is the default quality gate, so no single vendor's model marks its own work.
2. **The state store is yours.** The board is a SQLite file in your repo. You can read it,
   copy it, diff it and reconstruct any run from its append-only event log. Nothing is
   held in a vendor account.
3. **Provider churn is contained.** A vendor that changes its CLI breaks one adapter, not
   the protocol. The protocol keeps working with whatever else is on PATH.

The trade-off is honest: a vendor-native team knows its own agent's internals and can
schedule more cleverly inside them. Co-op deliberately knows nothing about model internals.
It buys neutrality and auditability, and it pays in depth of integration.

---

## 3. Intended users

**Primary:** developers who run two or more terminal coding agents on one repo and want
ownership, review and completion to be protocol-enforced instead of chat-coordinated.

**Secondary:** agent-harness researchers and plugin authors.

You are the intended user if you already pay for two or more agent CLIs, you already run
them in separate terminals on the same repository, and you are tired of being the hallway
between them.

You are **not** the intended user if you run one agent, or if you want a hosted team
product. Co-op adds protocol overhead that only pays off with independent peers.

---

## 4. Concepts

### 4.1 Board

The board is one SQLite file. On a normal repo it lives at `<repo>/.coop/board.db`. **The
board is canonical.** Every mutation goes through the `coop` CLI. The Markdown files under
`inbox/` are disposable projections; Co-op never reads them back.

One board serves one working directory, so one board serves one git worktree. Two
worktrees of the same repository hold two boards.

The board holds an append-only event log. You can reconstruct a whole run from the board
alone.

### 4.2 Items and goal contracts

An **item** is one unit of work. Its **contract** is the set of fields that make the work
executable without a mid-run human decision:

| Field | Meaning |
|---|---|
| `title` | Short name |
| `objective` | What the work must achieve |
| `scope` | The boundary of the change |
| `done_when` | The machine-checkable acceptance predicate |
| `output_contract` | What evidence the receipt must carry |
| `context` | What the agent needs to know before it starts |
| `allowed_actions` | The permitted actions, enumerated |
| `stop_conditions` | The points at which the agent must stop and ask |

A contract with every field filled is a **complete contract**. That is the normal kickoff.
An incomplete contract that forces a mid-run human judgment is a kickoff defect.

A one-line goal is a convenience adapter. Its owner fills only the empty fields with
`coop item define`. The result is not executable until a bounded peer huddle critiques it
and a *different* provider accepts it.

### 4.3 Claims and lanes

A **claim** is an exclusive, leased, fenced authorization to act. Claim before work. The
claim id is your whole protocol handle: every later write quotes it.

Claims are **lane-scoped**, not an item mutex. The implementation lane, the
`question_response` lane and the review lane fence independently. Two agents can therefore
work the same item on different lanes without a race.

A claim carries a lease. A session that stops renewing lets its claims go stale. A peer may
then reclaim with `--reclaim --reason`. A write through a dead claim id is refused forever.

### 4.4 Sessions

A **session** binds a process to an agent identity. Without a bound session, a board write
is attributed to the trusted-local `human`, and you cannot claim, receipt or review.

The autonomous runner mints sessions itself. A foreign harness attaches with
`coop session start --as <provider> --export`. Identity travels through `COOP_AGENT`,
`COOP_PROVIDER`, `COOP_SESSION_ID` and `COOP_DB`. Targeted runs also export
`COOP_ITEM_ID`.

**If `COOP_SESSION_ID` is already set, do not bind again.**

### 4.5 Hashed receipts

A **receipt** is the evidence object. You write a results file, then submit the receipt
through your claim. The board hashes the file and mechanically checks every typed
reference: `file:` paths re-hash, and `decision:`, `event:` and `debate:` ids must exist on
your item.

A receipt summary must name what was **not** done and where the work stopped. A summary
that omits a known limitation is grounds for `changes`.

A second submission supersedes the first. Replacing a receipt while a review is open kills
that review, deliberately: a review binds to exact evidence.

### 4.6 Reviews and quorum

Review is the binding work-quality critique. **One independent approval is the default.**
Use `--review-quorum 2` only when the item is high risk. The number of registered providers
never changes the policy silently.

Code enforces independence. The owner cannot review their own item. Self-review is refused
at claim, at verdict and at completion. Approvals must come from distinct providers.

A `changes` verdict supersedes the receipt and returns the item to `working`. A decision
recorded after an approval kills that approval by event order.

### 4.7 Handoffs

A **handoff** transfers an unfinished lane to a named agent. It carries six structured
fields and at least one verifiable proof reference. The item freezes while the handoff is
pending. Ownership moves only on acceptance.

A decline returns the item to its owner under the resume grace. An unanswered handoff is a
visible wedge, never a silent timeout.

Use a handoff when a contract requires a **named peer to author** something. The current
owner must not author it on the peer's behalf.

### 4.8 Questions

`coop needs-input` routes an exact question to a peer. It closes the asker's execution
claim and routes the item. Only the addressed agent may claim and answer.

The answer opens a fifteen-minute resume window for the asker. **Autonomous runs reject
questions addressed to `human`.** During a run, agents ask peers, never the operator.

### 4.9 Decisions

A **decision** is binding and append-only. You record it through a live implementation
claim. It delivers to the current reviewer when one exists. It can never be edited or
retracted. Reverse a decision with a new decision.

### 4.10 Huddles

A **huddle** is bounded peer deliberation. Each participant may post at most once per
round, and a huddle runs at most two rounds. Stances are `proposal`, `concern`, `support`
and `revision`.

The **contract huddle** is one-shot: it critiques an agent-authored contract before
execution, and the author cannot accept their own contract. Once acceptance is recorded, it
cannot be reopened.

The **plan huddle** (`coop huddle open-plan`) is opt-in and advisory. It handles a
post-acceptance multi-writer plan dispute. It never reopens contract finality.

### 4.11 Envelopes

The **communication envelope** is a read-only projection over the event stream
(`coop envelope list`, `coop envelope show`). It renders board traffic as content-free
envelope lines with responses correlated to their root requests. It adds no schema and
replaces no typed object. It wraps them for reading.

### 4.12 Content-free wake

Co-op's watcher wakes an agent with a **category only**. The wake carries no task content.
The runner prompt contains the stable operating loop and can append a hydrated snapshot
copied from the board. The snapshot is a cache, not a second source of authority.

This is the anti-side-channel rule. No wake can introduce an instruction that is absent
from the board. After a refusal, missing snapshot or truncation note, the agent reads the
board again. Continuous polling inside an agent is a non-goal. The wake is external and
deterministic.

### 4.13 Autonomous runtime

`coop start` is the single public runner. It freezes one deterministic **workflow recipe**
before it creates sessions or the first run marker.

- **`standard_three`** is the default. It activates Claude, Codex and Grok in soft-profile
  order.

Soft provider profiles bias the initial order only: Claude for orchestration, Codex for
code review and deep dives, Grok for fast tasks and research. They are not capability
boundaries. Board-derived routing stays authoritative, and every provider may perform any
legal action.

The runner overlaps at most three provider turns, and only on distinct allowlisted lanes.
Implementation work, same-item handoffs, huddle close, review requests, completion and
recovery all serialize. Each concurrent turn owns its process tree and its SQLite
connection.

On Windows, provider resolution selects an executable PATHEXT launcher such as
`codex.cmd`. It never selects an extensionless POSIX shim. The runner supplies only the
run-local `COOP_*` bindings as explicit shell environment overrides for Codex turns. A
user-level `inherit = "core"` policy cannot erase the session identity or selected item.

---

## 5. Manual

### 5.1 Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
uv tool install agent-coop-cli
```

uv creates an isolated tool environment and can download a suitable Python if needed.
If you already use [pipx](https://pipx.pypa.io/stable/installation/),
`pipx install agent-coop-cli` is equally supported. Both install the same PyPI package.

If `coop` is not found, run `uv tool update-shell` (or `pipx ensurepath` for a pipx
installation), then restart your terminal. Upgrade with `uv tool upgrade agent-coop-cli`
or `pipx upgrade agent-coop-cli`, using the tool that installed it.

To test an unreleased commit, install from a clone:

```bash
git clone https://github.com/Quigleybits/agent-coop
cd agent-coop
uv tool install .
```

The pipx equivalent is `pipx install .`. For editable development, see
[Contributing](https://github.com/Quigleybits/agent-coop/blob/main/CONTRIBUTING.md).

Requirements: CPython 3.10–3.14 on Windows or Linux, and the three stock provider CLIs
below installed, on `PATH`, and signed in (`coop start` launches all three). The runtime
is stdlib-only, with no third-party runtime dependencies.

| Provider | Official install and authentication | Confirm the executable |
|---|---|---|
| Claude Code | [Setup](https://docs.anthropic.com/en/docs/claude-code/setup) · [quickstart and sign-in](https://docs.anthropic.com/en/docs/claude-code/quickstart) | `claude --version` |
| OpenAI Codex | [CLI install](https://developers.openai.com/codex/cli) · [authentication](https://developers.openai.com/codex/auth) | `codex --version` |
| xAI Grok | [Official CLI and released installers](https://github.com/xai-org/grok-build) · [authentication and device-code flow](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/02-authentication.md) | `grok --version` |

All three are public stock tools; Agent Co-op does not distribute them. Provider CLIs
change independently, so executable preflight and a real run in your own environment
remain the practical compatibility check. Offline tests do not make a live-provider claim.

Both installers provide two console scripts plus the module form for the same `main()`:

| Form | When to use it |
|---|---|
| `coop …` | Short human-facing command and shell fallback. |
| `agent-coop …` | Collision-safe alias for environments that already provide `coop`. |
| `python -m agent_coop …` | Supported module entrypoint for the current Python environment. |

`next_action.command` uses a third, machine-routed form: the absolute Python
interpreter that loaded Agent Co-op, followed by `-m agent_coop`. This works
inside uv and pipx tool environments and does not require Linux to provide a bare
`python` alias.

Verify the install:

```bash
coop smoke --offline
```

`smoke --offline` opens a board in a private temp directory, runs a claim cycle and
launches an owned process tree. It probes no provider CLI and spends no quota. Drop
`--offline` to also probe the provider CLIs. A successful executable probe does not prove
login, a paid turn, or the full persistent-provider transport.

### 5.2 Quick start

#### The golden path

One command to install, one action to run. Three entry points, same result:

```bash
uv tool install agent-coop-cli
cd your-repo
coop "add a unit test for greet()"
# created task #1 (draft) · runner: starting · watch with: coop
```

`coop "<goal>"` creates the draft item and launches the run, exactly as TASKS Enter does,
on every supported OS. Co-op recognises a goal only as one quoted argument that contains
whitespace, or as everything after `--` (`coop -- add a unit test`). The quoted form
works even when the sentence starts with a command word. Unquoted words or a single word
are an error (exit 2) with the hint `to run a goal, quote it: coop "…"  (or: coop -- …)`,
so a typo such as `coop assign 1 claude` never launches a run. Add `--json` for
`{"item_id": N, "run": "<run id>", "launched": true}`.

The second entry point is the dashboard: bare `coop` opens it; type what you want and
press Enter. The third is the agent: `/coop <goal>` in Claude Code; "use Agent Co-op to
<goal>" in Codex or Grok.

The first run does two things. It creates `.coop/board.db` and the repo adapters in the
current repo, and it installs the `/coop` launch skill user-globally for the three
harnesses (locations in §5.10). Both are announced once: the dashboard notice line reads
`created board in ~/projects/app` and `installed /coop skill for Claude Code, Codex and
Grok`; `coop init` prints the same. Opt out of the global install with
`--no-global-skills` or `COOP_NO_GLOBAL_SKILLS=1`.

The dashboard is interactive on Windows only. On Linux it is a read-only redraw loop, so
launch with `coop "<goal>"` or the harness path there and watch with bare `coop`.

#### Five-minute local setup, then one real task

This is a five-minute **preparation** path, not a promise about provider run time. Creating
the toy repository, running its standard-library test, and running `coop smoke --offline`
make no provider calls. The final goal command launches Claude Code, Codex and Grok and can
spend their quota.

Create a disposable Git repository with this `greeting.py`:

```python
def greet(name):
    return f"Hello, {name}!"
```

Add `test_greeting.py`:

```python
import unittest

from greeting import greet


class GreetingTest(unittest.TestCase):
    def test_named_greeting(self):
        self.assertEqual(greet("Ada"), "Hello, Ada!")
```

Run the zero-provider preparation checks:

```bash
git init
python -m unittest -q
coop smoke --offline
```

Stop here if you do not intend to launch providers. When quota use is acceptable, start
the real task:

```bash
coop "make greet() reject blank names with ValueError; add a regression test; acceptance: python -m unittest -q"
```

Watch with bare `coop`. The run should move the task through a contract, an exclusive
claim, implementation, review by a different provider, receipt re-check, and `all_done`.
Afterward, run `python -m unittest -q` yourself and inspect `git diff`. The real run is the
stock-provider acceptance proof; the setup and offline smoke are not.

#### Advanced: contract-first

Write the whole contract yourself before any agent runs. Every field is then a human
decision, and the agents skip the acceptance huddle.

1. **Give the repo a board.** `coop init --workspace .` (details under *Boards* below).
2. **Seed one item with a complete contract.** The nine-flag `coop item create` example
   in §5.3. The command prints the new item id; read it back with
   `coop item show 1 --packet`.
3. **Start the run.** `coop start --item 1`. Add `--dry-run` first to resolve participants
   and the recipe without provider quota (§5.4).
4. **Watch the board.** `coop` in a second terminal in the same repo (§5.5). Do not write
   to the board while the run is active.
5. **Confirm the result.** When the run reports `all_done`, run the repo's own acceptance
   command — for the §5.3 contract, `python -m pytest tests/` exits 0 and
   `git status --porcelain=v1` lists only the allowed path. The item reached `done` only
   after an independent provider reviewed the receipt and the completion gate re-hashed
   every evidence file (§5.6).

#### Boards

```bash
cd /path/to/your-repo
coop init --workspace .
```

This creates `<repo>/.coop/board.db`. It also installs standalone adapters under
`.claude/skills/coop/` and `.agents/skills/coop/`. Co-op records managed hashes under
`.coop/` so an upgrade can replace unchanged adapters and preserve local edits. It hides
all three local paths through the repository's shared `.git/info/exclude`, never its
tracked `.gitignore`. Opening the dashboard (`coop` or `coop monitor`) in a folder with no
board does the same, and names the created board in its notice line.

An explicit `--board`/`--db` option or `COOP_DB` wins over discovery. Inside Git,
implicit discovery starts at the working directory and walks only through the nearest
worktree root (marked by a `.git` directory or a linked-worktree/submodule `.git` file).
It checks `<workspace>/.coop/board.db` first, then the two older layouts
(`<workspace>/board.db`, `<workspace>/coop/board.db`) at each eligible directory; the
nearest match wins. It never adopts a board above that Git boundary. Outside Git,
implicit discovery checks only the working directory. Use an explicit board/database
path for deliberate cross-workspace coordination.

When no board resolves, the command decides what happens. A read command (`status`,
`inbox`, `tasks`, `queue`, `board`, `agents`, `item show`, `envelope list`, `envelope show`,
`huddle show`) writes nothing: it prints `no board here (run coop init
--workspace . to create one)` and exits non-zero. A write command (`say`, `item create`,
and every other command that changes the board) creates the board and the adapters in the
working directory, and prints the file list to stderr before the first write. The
dashboard (`coop`, `coop monitor`) creates the board too, because opening it in a folder
is an explicit user action. `coop init`, auto-provision and the dashboard all refuse the
home directory, any directory above it and a filesystem root, because every repository
below such a board would discover it. Auto-provision and the dashboard also refuse the
operating system's temp directory itself; an explicit `coop init --workspace` may target
it, because naming the workspace is the caller's decision. A directory *under* the temp
directory is an ordinary workspace.

### 5.3 Seed an item with a complete contract

```bash
coop item create \
  --title "Add a greeting test" \
  --objective "cover app.py with one unit test" \
  --scope "tests/test_app.py only" \
  --done-when "python -m pytest tests/ exits 0 and git status --porcelain=v1 lists only tests/test_app.py" \
  --output-contract "a receipt naming the new test file and the pytest summary line" \
  --context "single-file toy repo; app.py exposes greet()" \
  --allowed-action "create tests/test_app.py" \
  --stop-condition "stop before you edit app.py" \
  --owner claude
```

The command prints the new item id.

Rules for a good contract:

- Make `done_when` a **command**, not a description. A reviewer must be able to run it.
- Prefer `git status --porcelain=v1` to `git diff --name-only` in a scope predicate.
  `git diff` passes untracked files, so it silently accepts an out-of-scope new file.
- Repeat `--allowed-action` and `--stop-condition` once per entry.
- Add `--review-quorum 2` only for high-risk work.

Three alternatives to typing every field:

| Form | Effect |
|---|---|
| `coop item create --contract contract.json` | Read the fields from JSON. Explicit flags override. Unknown keys are refused. |
| `coop item create --template mesh-v2 …` | Start from a built-in complete template. Zero model calls, deterministic. Skips the acceptance huddle. The built-in `mesh` / `mesh-v2` templates run a three-provider board-routing check and write one report; use them to verify a fresh install. |
| `coop item create --expand …` | Draft the missing fields from the goal with a fast model **at kickoff**, before any run marker. The item then enters as a complete human contract and skips the acceptance huddle. |

Read the contract back:

```bash
coop item show 1 --packet
coop tasks
coop queue --json
```

### 5.4 Start the run

Co-op uses explicit launch only. Skill discovery does not authorize a run. A user must
name Co-op and supply an existing item, a new goal or the whole board. This rule prevents
an ordinary task description from starting provider processes or spending quota.

All surfaces call the same public runner:

| Surface | User action | Result |
|---|---|---|
| CLI one-liner | `coop "<goal>"` (one quoted argument with whitespace) or `coop -- <goal words>` | Creates the draft item and starts the run in the background; prints `created task #N (draft) · runner: starting · watch with: coop`. Unquoted or single-word input is an error (exit 2) with a quoting hint. Works on every supported OS. |
| Dashboard | Run `coop` (or `coop monitor`), type a goal in TASKS, press Enter. | Creates and starts the new item in the background. Use dashboard `/coop` only for an existing highlighted item. |
| Claude Code skill | `/coop <item-id>` or `/coop <goal text>`. | Resolves or creates the item, then calls `coop start --item …`. |
| Codex or Grok skill | Say "use Agent Co-op to <goal>" or "for item <id>". | Calls the same CLI runner. |
| CLI | `coop start --item <id>` | Starts that item from the nearest eligible board in the current Git worktree. Universal and scriptable. |

The one-liner is the most direct path and the only interactive launch on Linux. The
dashboard is the easiest path on Windows. Harness skills are convenience wrappers,
not independent runners; they launch only after the user names Co-op and supplies an
item, a new goal or `all`. A bare harness `/coop` lists items and asks for a target. It
never guesses and never substitutes `--all`. The CLI is explicit, universal and
scriptable.

The skill reaches a harness two ways. The first `coop` run installs it user-globally
(§5.10), so `/coop <goal>` works in any repo, board or not: the wrapper's first write
creates the board. `coop init --workspace` also installs the same adapters into the
target repository's harness discovery directories. The wheel carries both files.

The dashboard `/coop` command and the Claude Code `/coop` skill share a name but run in
different interfaces. TASKS Enter launches a new dashboard goal. Dashboard `/coop` launches
an existing highlighted item. A harness has no highlight, so its skill resolves the target
first.

From a workspace that contains `.coop/board.db`, start one item:

```bash
coop start --item 1
```

The CLI discovers the nearest eligible board from the current directory, within the
boundary described in §5.2. Use `--board <db>` only to target a different board. Pass
`--all` only after an explicit whole-board request. The runner also requires all three
core provider CLIs at recipe preflight.

A harness request for a new goal creates a draft, then starts that item:

```bash
coop item create --title "<goal>" --objective "<goal>"
coop item show <id> --packet
coop start --item <id>
```

The agents complete and peer-accept the draft contract before substantive work. This
matches the dashboard goal flow and avoids a hidden kickoff model call. For faster startup,
create an item with `--expand`, inspect its packet, then start it explicitly.

After launch, the user does not drive each provider. The runner wakes Claude, Codex and
Grok, and they follow the shared board until completion or a stop condition. Keep the
runner in the foreground, or watch it from another terminal with `coop`.

Resolve participants and the recipe without provider quota:

```bash
coop start --item 1 --dry-run
# participants: claude, codex, grok
# recipe: standard_three
# persistent providers: claude, codex, grok
# target: item 1
```

Every run writes a combined log and a status sidecar under `<board-directory>/.coop-runs/`
as `run-*.log` and `run-*.status.json`. The sidecar carries operational runner state only:
phase, current provider and action, elapsed time and terminal reason. It never repeats
board content.

### 5.5 Watch the run

In a second terminal, in the same repo:

```bash
coop            # same as: coop monitor
```

Bare `coop` in a terminal opens the dashboard; `coop --help` still prints help, and
`coop monitor` remains as the alias and keeps `--interval`. Opening the dashboard in a
folder with no board creates one (§5.2). The dashboard lists tasks, board messages and
live sessions, and redraws every two seconds. For the shortest interactive flow, focus the
TASKS pane, type a goal and press Enter. That single action creates, highlights, and
launches the draft item.

The interactive dashboard needs Windows console key input and a real TTY on both stdin
and stdout. Anywhere else — Linux, a pipe, a non-TTY — `coop` degrades to a plain
redraw loop that shows the same panes and takes no input. Launch there with
`coop "<goal>"` (§5.4) and use the loop to watch.

Press **Ctrl+O** to open the board switcher and move between the workspaces the dashboard
knows about, shown as home-relative folders (`~/projects/app`; a git worktree appears
under its repository). Type a folder path and press Enter to add one. Opening a folder
that has no board yet creates the board and the harness adapters there, exactly as
`coop init --workspace` does. Folders that no longer exist leave the list by themselves,
and boards under the operating system's temp directory are never listed. The dashboard
does not bind Ctrl+B, because tmux and Herdr use it as their prefix key. Ctrl+C quits.

Point the dashboard at a different board with the **global** `--db` flag, which must precede
the subcommand:

```bash
coop --db /path/to/.coop/board.db monitor
```

Inside the dashboard, `/coop` launches an existing highlighted task. New TASKS input does
not need that second command. Dashboard `/coop` is separate from the Claude Code `/coop`
skill. The dashboard also accepts `/coop all` to drain the board, `/coop stop` to stop after
the current owned turn, and `/coop join` to attach the current harness.

**Do not write to the board while the run is active.** A human board write between the
start and finish markers fails the product gate and is recorded. Audit the boundary
afterwards with `coop recover human-audit`.

### 5.6 Review and completion

Agents drive this sequence. It is written out here so you can read a run and know what
should have happened.

```bash
# The owner evidences the work.
coop receipt submit --claim 12 --path results/parser-fix.md \
  --summary "parser handles nested escapes; utf-16 path untested" \
  --proof "pytest: 495 passed" \
  --proof-ref file:results/parser-fix.md

# The owner requests an independent review.
coop review request --claim 12
coop review request --claim 12 --reviewer grok   # or name the reviewer

# A different provider claims the review lane and rules on it.
coop review claim 3 --intent "reviewing the parser evidence" --lease-seconds 7200
coop review submit --claim 18 --verdict approve
coop review submit --claim 18 --verdict changes --body "missing the utf-16 case"

# The owner passes the guarded completion gate.
coop item complete --claim 12
```

Completion re-validates everything at once: a complete contract, a current receipt at the
current contract version, every evidence file re-hashed, every board reference re-checked,
the qualifying approvals present, no open question and no pending handoff. If the evidence
changed on disk after approval, completion refuses, supersedes the receipt and records
every failure.

### 5.7 Recover a wedge

A crashed session stops renewing, and its claims go visibly stale. A peer waits for the
abandon horizon and then releases the stale claim:

```bash
coop recover wedge 12 --reason "peer abandoned after horizon"
```

This is the **autonomous** recovery path: it is a bound agent operation, so it preserves
the acceptance record. The peer then reclaims the item, files any existing receipt,
requests review if needed, and completes through the normal gates.

`coop admin release` and `coop admin answer` are offline end tools for a truly hung runner.
They are human-lane mutations. Do not use them mid-run.

### 5.8 Session binding rules

1. **If `COOP_SESSION_ID` is set, you are already bound.** Do not start a second session,
   and never pass `--as` or another identity flag.
2. Otherwise, attach the current process:

   ```bash
   eval "$(coop session start --as claude --export)"
   # … work …
   coop session end
   ```

3. Without a bound session, every write is attributed to the trusted-local `human`. You
   cannot claim, receipt or review.
4. An external session has no auto-renew. Extend its claims with `coop session renew`.
5. Interactive attach and `coop session run` are ordinary coordination and debug surfaces.
   They are **not** alternate product runners.

### 5.9 The agent loop

A bound agent repeats four steps:

1. Run `coop status --json`.
2. Read `next_action`. Its stable fields are `kind`, the target ids, `lease_seconds`,
   `command`, `required_inputs` and `choices`.
3. Execute `command` verbatim. Supply only the named `required_inputs`. If `command` is
   `null`, pick one legal `choice`.
4. Consume `coop inbox` when addressed entries are present, then repeat. Stop the turn when
   `next_action.kind` is `idle`.

A refused command is coordination feedback. Every rejection names a stable `reason_code`,
machine-checkable `evidence` and, where the board can derive one, the canonical repair in
`legal_next_actions`. Take the offered repair; never guess a sequence and never re-run the
refused command unchanged.

### 5.10 CLI reference

This reference is for human operators and interactive debugging. Autonomous bound turns
receive exact commands through `next_action.command` and do not need this reference.

Global options precede the subcommand: `coop [--db DB] [--as AS_AGENT] [--json] <command>`.

#### Setup and health

| Command | Purpose |
|---|---|
| `coop init --workspace <path> [--no-global-skills]` | Provision `<path>/.coop/board.db`, install the repo adapters, hide both via `.git/info/exclude`, and install the global `/coop` skill. |
| `coop migrate [--backup <path>] [--confirm-legacy-clients-stopped]` | Migrate an older board to schema v4. |
| `coop smoke [--offline]` | Preflight: temp board, claim cycle, process tree, provider CLIs. `--offline` skips the provider probes. |

The global `/coop` skill is a copy of the launch adapter, installed on dashboard open and
on `coop init` into `~/.claude/skills/coop/SKILL.md` (Claude adapter),
`~/.codex/skills/coop/SKILL.md` and `~/.grok/skills/coop/SKILL.md` (shared Agent Skills
adapter). Copies, not links. Hash-managed like the repo adapters: an unchanged managed file
is updated on upgrade, a local edit is preserved, and the state lives in
`~/.coop/global-skills.json`. A failed write (permissions, no home directory) is a notice,
never an error. Opt out with `--no-global-skills` on `coop`, `coop "<goal>"`,
`coop monitor` or `coop init`, or set `COOP_NO_GLOBAL_SKILLS=1`.

#### Running

| Command | Purpose |
|---|---|
| `coop start (--item <id> \| --all) [--board <db>]` | The single public runner. |
| `coop "<goal>"` / `coop -- <goal words>` `[--json] [--no-global-skills]` | Create a draft item from the goal and start it in the background (same as TASKS Enter). A goal is one quoted argument containing whitespace, or everything after `--`; unquoted words or a single word exit 2 with `to run a goal, quote it: coop "…"  (or: coop -- …)`. Prints `created task #N (draft) · runner: starting · watch with: coop`; `--json` prints `{"item_id": N, "run": "<run id>", "launched": true}`. Creates a missing board. |
| `coop [--interval <s>] [--no-global-skills]` | Live dashboard (bare `coop` in a terminal). Creates a missing board. `coop monitor` is the alias. |
| `coop status [--json] [--compact]` | The boot panel and `next_action`. |
| `coop queue [--json]` | Discoverable work. |
| `coop tasks [--status <s>] [--json]` | Task list with owners and claims. |
| `coop board [--limit <n>] [--json]` | Recent message board. |
| `coop agents` | Registered agents. |

`coop start` flags:

| Flag | Effect |
|---|---|
| `--item <id>` / `--all` | Required, mutually exclusive target. |
| `--board <db>` | Board path. |
| `--agents <list>` | Comma list; default `claude,codex,grok`. |
| `--dry-run` | Resolve participants and recipe, then exit. No quota spend. |
| `--interval`, `--timeout-seconds`, `--max-turns`, `--max-idle-rounds`, `--max-noop-cycles` | Runner bounds. |

#### Sessions

| Command | Purpose |
|---|---|
| `coop session start --as <provider> [--name <n>] [--export] [--json]` | Bind this process. `--export` prints shell export lines for `eval`. |
| `coop session end` | Finish the session named by `$COOP_SESSION_ID`. |
| `coop session renew` | Extend this session's active claims. |
| `coop session run --as <provider> [--env-allowlist NAME …] -- <cli> …` | Interactive and debug attach surface. The child inherits the full environment by default; `--env-allowlist` (repeatable) restricts it to the platform baseline, the named parent variables and `COOP_*`. |

#### Items and contracts

| Command | Purpose |
|---|---|
| `coop item create …` | Seed an item. Contract flags listed in §5.3. |
| `coop item show <id> [--packet] [--history] [--json]` | Read an item. `--packet` returns the complete contract. |
| `coop item revise <id> --reason <r> [contract flags]` | Human-lane contract revision. Kickoff or after the run stops. |
| `coop item define --claim <c> [contract flags]` | Agent lane: fill a goal-task's empty fields. |
| `coop item refine --claim <c> [contract flags]` | Agent lane: amend agent-authored fields after a `changes` huddle. |
| `coop item claim <id> --intent <i> [--reclaim --reason <r>] [--lease-seconds <s>]` | Take the implementation lane. |
| `coop item complete --claim <c>` | The guarded completion gate. |
| `coop claim release --claim <c> --reason <r>` | Release a claim you hold. |

#### Work, evidence and review

| Command | Purpose |
|---|---|
| `coop checkpoint {start,step,blocked,risky,predone} --claim <c> [--note <n>]` | Reasoning-boundary checkpoint. Returns your packet plus your unread inbox. `blocked` is the guarded stop. |
| `coop receipt submit --claim <c> --path <f> --summary <s> --proof <p> [--proof-ref <ref>]` | File hashed evidence. `--proof-ref` takes `file:<abs-path>`, `decision:<id>`, `event:<id>` or `debate:<id>`. |
| `coop review request --claim <c> [--reviewer <a>]` | Open a review. |
| `coop review claim <id> --intent <i> [--lease-seconds <s>]` | Take the review lane. Never the owner. |
| `coop review submit --claim <c> --verdict {approve,changes} [--body <b>]` | Rule on the evidence. |
| `coop decision record --claim <c> --text <t> [--rationale <r>]` | Record a binding, append-only decision. |

#### Coordination

| Command | Purpose |
|---|---|
| `coop needs-input --claim <c> --to <agent> --question <q>` | Route an exact question. Closes your execution claim. |
| `coop question claim <id> --intent <i>` | Only the addressed agent may claim. |
| `coop question answer --claim <c> --answer <a>` | Answer, opening the asker's resume window. |
| `coop handoff create --claim <c> --to <a> --reason --summary --completed --remaining --risks --next-action [--proof-ref <ref>]` | Structured transfer. All six fields required. |
| `coop handoff accept --id <n> --intent <i>` / `coop handoff decline --id <n> --reason <r>` | Ownership moves only on acceptance. |
| `coop huddle open --claim <c>` / `coop huddle open-plan …` | Contract-acceptance huddle (one-shot) / advisory plan huddle (opt-in). |
| `coop huddle post <id> --stance {proposal,concern,support,revision} --body <b>` | One post per participant per round; two rounds maximum. |
| `coop huddle close <id> --outcome {accepted,changes} --summary <s>` | Close the huddle. The author cannot accept their own contract. |
| `coop huddle show <id> [--json]` | Read the huddle. |
| `coop say <body> [--to <a>] [--item <i>] [--room <r>]` | Non-binding chatter. Never creates actionable work. |
| `coop inbox [--peek] [--json]` | Your delivery queue. `--peek` does not advance the cursor. |
| `coop envelope list [--item <i>] [--since <t>]` / `coop envelope show <event_id>` | Read-only envelope projection over board events. |

#### Recovery and human lane

| Command | Purpose |
|---|---|
| `coop recover wedge <claim_id> --reason <r> [--abandon-after-seconds <s>]` | **Agent** path. Release a stale claim after the abandon horizon. Default horizon 180 s. |
| `coop recover human-audit --since <iso> [--until <iso>] [--json]` | List human-lane mutations after a run-start timestamp. |
| `coop admin release <claim_id> --reason <r> --confirm-process-stopped` | **Offline end tool.** Not a mid-run path. |
| `coop admin answer <question_id> --answer <a> --reason <r>` | **Offline end tool.** Not a mid-run path. |

#### Prohibited

- Never write the SQLite file directly.
- Never edit an `inbox/*.md` projection to change state.
- Never run an agent mutation without a bound session.
- Never continue under a stale, released, completed or closed claim.
- Never carry operative content in a prompt file, stdin contract or external chat.
- Commands from pre-release builds (`item done`, `item update`, `assign`, `debate`,
  `watch`) are refused.

### 5.11 Opt-in flags

#### Promoted defaults

Worker reuse is **on** by default. Within a run, the Codex app-server, the Claude
bidirectional stream JSON transport and the Grok ACP stdio transport each reuse one owned
process. A transport failure gets one bounded restart, and the runner never replays a
failed turn automatically. Cold, per-turn spawns still serve native research, browser work
and configured external MCP.

Opt out for debugging:

| Flag | Effect |
|---|---|
| `--no-persistent-workers`, `--cold-workers` | Cold provider turns every time. |
| `--no-prompt-hydration` | Spawn turns with the bare content-free prefix, without the spawn-time board snapshot. |
| `--no-mechanical-precommit` | Stop the runner pre-executing judgment-free claim commands. |
| `--no-prompt-cache` | Disable the Claude prompt-cache reuse hint. |
| `--fresh-sessions` | Do not resume Claude's provider conversation from the previous run on this board. |
| `--inherit-env` | Give provider children the full shell environment instead of the per-provider allowlist (platform/runtime baseline, selected provider namespace, selected MCP values, `COOP_*`, `PYTHONPATH`). Shell secrets become readable by every model turn. See `SECURITY.md`. |

#### Unpromoted, opt-in

These are implemented, tested offline and **not promoted**. Do not read their presence as a
speed claim.

| Flag | Status |
|---|---|
| `--persistent-provider {claude,codex,grok}` | **Unpromoted.** Repeatable; opts named providers into worker reuse explicitly. |
| `--prompt-cache-provider claude` | **Unpromoted.** Enables Claude's prompt-cache reuse hint. A cache-hit count proves use, not speed. |
| `--token-efficient` | **Unpromoted.** Bounded action grammar plus a prompt-level post-write exit contract. |
| `--herdr` | **Unpromoted, default off.** See below. |

#### `--herdr`

`coop start --herdr` is an optional observability flag. It is **merged, opt-in and
unpromoted**; a machine without Herdr runs Co-op byte-identically to a machine with it.
It belongs only to the advanced explicit `coop start` path; the quick-goal and dashboard
launch paths do not expose a Herdr switch.

What it does: it opens one **read-only mirror pane** for each persistent provider. A mirror
pane renders that run's turn-trace events. It is never the worker's stdin or stdout —
workers keep their existing pipe transport unchanged. The dashboard `runner:` line
publishes the mirror pane ids as a jump target.

Binding boundaries:

1. Pane text is observability only. Co-op never parses it into board state.
2. Herdr waits never replace the Co-op watcher. The board decides.
3. Cold-path workers (research, browser, external MCP) are never pane-hosted.
4. **Run panes are read-only for the human between the start and finish markers.** A
   keystroke in a pane is human input inside an autonomous run, and the human-write gate
   records the violation.
5. Coupling is one-way: Co-op calls the Herdr CLI, always behind the flag, always inside
   one adapter module.

Co-op resolves one absolute Herdr launcher outside the selected workspace, keeps it for
the adapter, runs it from the launcher's directory and applies the Windows launcher
argument guard. The client receives only platform values and Herdr's current-session
selectors. The mirror bootstrap clears inherited provider and unrelated credential
values before it reads the trace.

The flag is explicit opt-in.

---

## 6. Tests

### 6.1 How to run

```bash
python -m pytest tests/ -q            # full suite
python -m pytest tests/test_adversarial_*.py -q   # adversarial trials only
coop smoke --offline                  # runtime preflight, no quota
```

**Unset `COOP_SESSION_ID` first.** A bound session makes the suite write as a live agent
instead of the trusted-local `human`. Clear the whole `COOP_*` surface before you run
pytest.

Every test runs offline. No test spends provider quota. Two skips are conditional by
design: `tests/test_herdr_live.py` is the opt-in live Herdr test and needs
`COOP_HERDR_LIVE=1` plus a reachable live Herdr session, and one `tests/test_process_tree.py`
case skips itself when the environment cannot deliver a console signal. Platform-guarded
classes also skip themselves on the other platform, so a Linux run reports a higher skip
count than a Windows run. That is expected.

Public CI runs the full suite on Windows and Ubuntu at Python 3.10 and 3.14.
Separate jobs build wheel and sdist artifacts on both operating systems,
install each artifact outside the checkout and run the console-script smoke.

### 6.2 What the suite covers

The suite covers the schema and state machine, claims and leases, contracts, checkpoints,
questions, handoffs, huddles, receipts, reviews, decisions, completion, the runner and
scheduler, provider workers and transports, the dashboard, the rejection taxonomy, the
envelope projection and the Herdr adapter.

Each protocol failure that Co-op is built to refuse has its own scripted trial in `tests/`.
None is live-only.

| Class | Trial |
|---|---|
| Mid-run human board write after `run_started` | `tests/test_adversarial_midrun_human_write.py` |
| Raw completion bypass | `tests/test_adversarial_completion_bypass.py` |
| Stale claim write after lease expiry | `tests/test_claims.py` and the receipt, completion and review suites |
| Forged or mismatched receipt hash | `tests/test_receipts.py`, `tests/test_completion.py` |
| Self-review counting toward quorum | `tests/test_reviews.py`, `tests/test_completion.py` |
| Provider crash mid-turn | `tests/test_adversarial_provider_crash.py` |
| Provider quota exhaustion | `tests/test_runner_status.py`, `tests/test_provider_failures.py` |
| Failed acceptance command | `tests/test_adversarial_failed_acceptance.py` |
| Scope drift without revise and re-review | `tests/test_adversarial_scope_drift.py` |
| Same-reviewer livelock and soft-order starvation | `tests/test_adversarial_review_livelock.py` |

---

## 7. Features

### 7.1 Coverage

Every feature below has offline test coverage. A live run still depends on the installed
provider versions, authentication and quota; prove that path with a small item in your own
environment.

### 7.2 Shipped surface

| Feature | State | Notes |
|---|---|---|
| Schema v4 board: sessions, claim lanes, supervisor, process tree, contracts, needs-input, inbox, projection | built / tested | |
| Supervised sessions, 5 s renewals, stale and reclaim, questions, grace, resume, takeover, auth matrix | built / tested | |
| Hashed receipts, reviews, handoffs, decisions, guarded completion, human revise | built / tested | |
| Goal-tasks + `item define` | built / tested | Empty fields only; peer huddle acceptance required before execution |
| Contract templates (`--template`) and kickoff expansion (`--expand`) | built / tested | Both skip the acceptance huddle; `--template` makes zero model calls |
| `coop start` + content-free watcher and runtime | built / tested | **Product mode.** The single public runner |
| Default `standard_three` workflow recipe | built / tested | Activates Claude, Codex and Grok |
| Sparse scheduling: up to three overlapping turns on distinct lanes | built / tested | Implementation, handoffs, completion and recovery serialize |
| Critique + bounded huddles (contract, plan) | built / tested | Two rounds maximum; author cannot self-accept |
| Explicit binding review policy and quorum | built / tested | One independent approval by default; quorum 2 is explicit |
| Review-lane `--lease-seconds` + timeout resilience | built / tested | |
| Run-scoped persistent provider workers | built / tested | Default on; cold path retained for research, browser and external MCP |
| Prompt hydration, mechanical precommit, cross-run warmth | built / tested | Opt-outs listed in §5.11 |
| Rejection and stall taxonomy + bounded recovery reflex | built / tested | |
| Communication envelope projection | built / tested | Read-only over events; zero schema change |
| Dashboard: Enter creates and launches a goal, `/coop` runs an existing item, plus board feed and controls | built / tested | Headless on Windows. Logs stay under `.coop-runs/`. |
| Runner status sidecars | built / tested | Operational state only; never board content |
| Foreign-harness session attach | built / tested | Ordinary join; not the autonomous acceptance path |
| Harness-native entrypoints (Claude Code, Codex, Grok) | built / tested | Workspace init installs wheel resources. Every launch routes to `coop start`. |
| Herdr mirror observability (`--herdr`) | built / tested | **Opt-in, unpromoted, default off** |
| Packaging: `agent-coop-cli` 0.1.0, console script `coop`, stdlib-only | built / tested | Wheel and sdist are built and installed in CI. |

---

## 8. Contribution to the space

Five things Co-op contributes that its reference peers do not combine.

1. **A neutral cross-provider protocol.** Heterogeneous CLIs are peers on one board with
   identical authority rules. Vendor-native teams coordinate one vendor's agents; Co-op
   coordinates whatever you already pay for.

2. **A refusal to claim what it cannot show.** Co-op quotes no reliability rate and no
   speed figure. Every protocol failure it is built to refuse has a scripted trial you can
   run yourself, and section 9 states the limitations plainly.

3. **Content-free wake.** The watcher wakes an agent with a category, never with content.
   No prompt side-channel exists, so the board really is the only channel — and any run
   can be reconstructed from the board alone.

4. **Hashed receipts plus independent review.** Done work is evidenced, never asserted.
   The board hashes evidence files and re-hashes them at the completion gate. Code enforces
   reviewer independence, so a provider cannot mark its own homework.

5. **The board as the only channel.** No terminal poke, no send-keys, no chat. A message
   that would change state must be a typed board object instead.

---

## 9. Limitations

State these before you adopt Co-op.

1. **Single machine.** The board is a local SQLite file. There is no cross-machine board.
2. **No web UI.** The surfaces are a CLI and a terminal dashboard. There is no hosted
   dashboard and no team layer.
3. **Provider CLIs and paid quotas are required.** `coop start` needs the Claude, Codex and
   Grok CLIs on PATH at preflight, and real runs spend real provider quota. Co-op supplies
   no models.
4. **No end-to-end speed guarantee.** Provider inference dominates run wall-clock.
   Persistent workers, prompt hydration, mechanical precommit and overlap remove avoidable
   protocol delay, but a coordinated run can still take longer than one agent.
5. **Windows and Linux only.** Both are release-gated in public CI. macOS is unsupported
   in v0.1.0 and has no compatibility claim.
6. **Reliability is in progress.** Co-op quotes no reliability rate. Run it yourself before
   you trust it with work you cannot review.
7. **Only Claude, Codex and Grok have provider adapters.**
8. **Crash durability is at protocol commit boundaries only.** A workspace file needs a
   committed checkpoint or a receipt to be board-known.
9. **Cross-machine boards and a web UI are out of scope for 0.1.0.**

---

## 10. Security

**Co-op spawns real agent processes that hold real tool authority.** Those processes read
and write files, run commands and reach the network exactly as they do when you launch them
by hand. Co-op adds coordination; it removes no authority.

**Posture: supervised use.**

1. **Run Co-op on repositories you trust.** Treat a Co-op run as you would treat running
   the agent CLI yourself on that repository.
2. **Keep the review gate on.** Review is the default for a reason. `--review-waiver`
   exists for mechanical work; it is not a speed setting.
3. **Write real `stop_conditions` and `allowed_actions`.** They are the contract's blast
   radius, and reviewers judge scope drift against them.
4. **The OS and process sandbox remains the security boundary.** Protocol purity does not
   make an unsafe task safe.
5. **Read the board before you accept the work.** The board is append-only and complete.
   `coop item show <id> --history` and `coop envelope list --item <id>` reconstruct the run.

**Containment rule for unattended runs.** An unattended agent run must hold at most **two**
of these three:

- **A.** untrusted input,
- **B.** secrets,
- **C.** consequential authority (broad shell access, wide write scope, network egress).

Name the dropped leg when you design the job. A job that needs all three is two jobs:
fetch → artifact → act. Never place untrusted content, credentials and broad authority in
one unattended Co-op run.

**Identity is cooperative.** All agents run as one OS user. `COOP_SESSION_ID` is
self-asserted: Co-op checks that the session exists and is running, not that the process
is the one it was minted for. The claim, review, completion and human-lane gates are
correctness rails against cooperating agents; a misbehaving process can bypass them. The
security boundary is the OS user and the repository you run on. Peer-readable views
(`item show --packet`, `item show --history`, the sessions strip) show session ids as an
eight-character prefix and omit the holder's command line and working directory.
[SECURITY.md](../SECURITY.md) states the full trust model, the workspace-trust rules
(`.coop/` as an instruction set, public-safe trace/status files under
`.coop/.coop-runs/`, sensitive runtime artifacts outside the workspace, tool shadowing,
the `.coop-auto-stop` file) and the reporting policy.

**Operational notes.**

- The board carries no secrets by design. Do not paste credentials into a contract, a
  question or a receipt — every one of them is append-only. Never pass a secret on a
  `coop session run … --` command line: the sessions table stores it.
- `coop init` hides `.coop/` through `.git/info/exclude`, so a board is not committed by
  accident. It rejects adapter paths whose symlinked parent escapes the workspace and uses
  exclusively created temporary files for atomic adapter updates. Verify repository state
  before you make it public.
- `coop init`, first-use auto-provision and dashboard open refuse the home directory, any
  directory above it and a filesystem root. Read commands never create a board (see 5.2).
  Implicit discovery stops at the nearest Git worktree boundary and checks only the
  working directory outside Git. Use an explicit board/database path for deliberate
  sharing across workspaces.
- Outside-repository writes include the three global `/coop` skill files plus
  `~/.coop/global-skills.json` (see 5.10), and sensitive run artifacts in the private
  per-user runtime root. The runtime root defaults under `%LOCALAPPDATA%` on Windows or
  `$XDG_STATE_HOME` on POSIX, with per-user local-state fallbacks. A relative platform
  base fails closed. A `COOP_RUNTIME_ROOT` override must be absolute and outside the
  workspace. The location
  keeps artifacts out of workspace sync by default; owner-only permissions reduce
  other-user access. Neither control isolates same-user processes or prevents a backup
  service from copying interrupted-run material. Normal owner cleanup removes each dedicated
  prompt/profile/worker directory. When a new private runtime artifact is created, the
  conservative lazy sweep touches only exact-marked directories older than 24 hours whose
  recorded process is definitely dead.
- New runs do not migrate or broadly delete sensitive residue left beneath
  `.coop/.coop-runs` by an older or interrupted version. Stop those old runs, inspect the
  specific profile/state/prompt directories and their markers, and handle them
  deliberately.
- Provider children of `coop start` receive a per-provider allowlist: platform/runtime
  values and Co-op bindings plus only Claude's, Codex's or Grok's own namespace and MCP
  values explicitly selected for that provider turn. Foreign provider namespaces and
  unrelated credential-shaped variables stay out of cold turns, persistent workers,
  structured one-shots and routed board commands. Structured one-shots also omit Co-op
  board/session state. Claude cloud credentials pass only when the matching backend flag
  selects that backend. `--inherit-env` deliberately restores full inheritance to provider
  children for one run; it does not widen the Herdr client environment.
  A `coop session run` child inherits the full environment by default; scope it with
  `--env-allowlist NAME` (repeatable). Keep tokens for other services out of the shell that
  starts any run.
- Prompts never travel in argv; launcher argv is checked before every spawn on Windows
  `.cmd` shims; provider CLIs and the optional Herdr client resolve to absolute paths
  outside the workspace and are kept for the run or adapter. Herdr runs from the launcher
  directory with its narrower environment. See `SECURITY.md`, "Provider launch".
- Human-lane admin tools (`coop admin release`, `coop admin answer`) are audited and are
  offline end tools. Mid-run use fails the product gate and is recorded.

---

## 11. FAQ

**Does the skill start Co-op automatically?**
No. Skill metadata only helps a harness discover launch instructions. A run starts only
after an explicit Co-op request supplies an item, a new goal or `all`. Every skill and
dashboard launch routes to `coop start`. The wheel includes the adapters, and workspace
init installs them into both harness discovery layers.

**Do I need all three providers?**
`coop start` requires the Claude, Codex and Grok CLIs at recipe preflight, because the
documented default recipe activates all three and the review gate needs an independent
provider. The promoted launch path does not offer arbitrary provider selection.

**Can I use it with one agent?**
You can drive the CLI with one bound session, and the board will still enforce claims,
receipts and the completion gate. You will not get the value, because independent review is
the point. Co-op is built for peers.

**Does Co-op make my agents faster?**
There is no end-to-end speed claim. Provider inference dominates run wall-clock. Co-op
reduces its own delay through reused provider workers, hydrated turns, mechanical
precommit, fewer turns and overlap across independent lanes. Coordination can still be
slower than one agent on a small task.

**Why SQLite instead of a queue service or a file per task?**
Transactions. Claims, leases, quorum counting and the completion gate all need atomic
check-and-write against current state. A file tree cannot fence a stale writer, and a
hosted queue would break the local-first, zero-dependency position. SQLite ships with
Python.

**Does it work on macOS?**
macOS is outside the v0.1.0 support contract. The release gates cover Windows and Linux
only. A macOS result is welcome, but it does not make the platform supported.

**What happens when an agent crashes mid-run?**
Its session stops renewing and its claims go visibly stale. After the abandon horizon, a
peer runs `coop recover wedge <claim_id> --reason "…"`, reclaims the item and continues
through the normal gates. That path is a bound agent operation, so recovery preserves the
autonomous acceptance record. The behaviour is covered by
`tests/test_adversarial_provider_crash.py`.

**Can an agent mark its own work as done?**
No. Self-review is refused at claim, at verdict and at completion. Approvals must come from
distinct providers, and the owner is never one of them. Completion re-hashes every evidence
file before it opens the gate.

**Why does Co-op quote no reliability rate?**
Because a rate without its denominator is marketing, not evidence, and this release ships
no numbers you can check. Run the suite, run `coop smoke --offline`, then run a real item
on a repo of your own. That is a denominator you own.
