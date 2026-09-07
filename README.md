# Agent Co-op

**Co-op is the shared memory + rules of engagement between coding agents.**

*A local-first coordination protocol for heterogeneous terminal coding agents.*

[![CI](https://github.com/Quigleybits/agent-coop/actions/workflows/ci.yml/badge.svg)](https://github.com/Quigleybits/agent-coop/actions/workflows/ci.yml)

Release support: Windows and Linux · CPython 3.10–3.14 · beta

---

## Install

Co-op's default recipe launches three publicly available provider CLIs. Install and sign
in to each one before a real run:

| Provider | Official setup | Local check |
|---|---|---|
| Claude Code | [Setup](https://docs.anthropic.com/en/docs/claude-code/setup) · [quickstart and sign-in](https://docs.anthropic.com/en/docs/claude-code/quickstart) | `claude --version` |
| OpenAI Codex | [CLI install](https://developers.openai.com/codex/cli) · [authentication](https://developers.openai.com/codex/auth) | `codex --version` |
| xAI Grok | [Official Grok CLI and installers](https://github.com/xai-org/grok-build) · [authentication](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/02-authentication.md) | `grok --version` |

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then install Co-op:

```bash
uv tool install agent-coop
coop smoke --offline
```

Already use [pipx](https://pipx.pypa.io/stable/installation/)?
`pipx install agent-coop` is an equally supported alternative.

`smoke --offline` checks Co-op in a private temporary directory. It makes no provider call
and spends no provider quota. Run `coop smoke` to add executable preflight for the three
CLIs; a real goal is still the end-to-end check.

Provider CLIs change independently. Confirm that each CLI works and is signed in
before starting a real goal.

## Run

In the repo you want worked on, one line, any OS:

```bash
cd your-repo
coop "add a unit test for greet()"
# created task #1 (draft) · runner: starting · watch with: coop
```

Quote the goal. `coop -- add a unit test` also works.

Or open the dashboard with bare `coop`, type what you want done and press Enter. The
dashboard is interactive on Windows; on Linux it is a read-only redraw loop.

Or from inside your agent:

```text
/coop add a unit test for greet()                  # Claude Code
use Agent Co-op to add a unit test for greet()     # Codex or Grok
```

All three forms give the same result. The first run creates `.coop/board.db` in the repo,
hidden through `.git/info/exclude`, and installs the `/coop` skill for the three harnesses
under your home directory. Opt out of the skill install with `--no-global-skills` or
`COOP_NO_GLOBAL_SKILLS=1`.

A run starts only on an explicit user action: a goal given to `coop`, a goal typed in the
dashboard, or a `/coop` request that names a goal or an item. Loading a skill never starts
a run. Every run starts provider processes and can spend provider quota.

### Five-minute local setup, then one real run

The setup below is deliberately small. Creating the files, running `unittest`, and running
`coop smoke --offline` make no provider calls. The final `coop "…"` command is different:
it launches the three providers and can spend their quota. The agents' run time is not part
of the five-minute setup estimate.

Create a disposable Git repository containing `greeting.py`:

```python
def greet(name):
    return f"Hello, {name}!"
```

and `test_greeting.py`:

```python
import unittest

from greeting import greet


class GreetingTest(unittest.TestCase):
    def test_named_greeting(self):
        self.assertEqual(greet("Ada"), "Hello, Ada!")
```

Prepare and check it locally:

```bash
git init
python -m unittest -q
coop smoke --offline
```

When you intend to start a real provider run:

```bash
coop "make greet() reject blank names with ValueError; add a regression test; acceptance: python -m unittest -q"
```

Use the dashboard to watch. When the run reports `all_done`, run
`python -m unittest -q` yourself and inspect the diff before keeping it.

## What happens next

The three agents share one board. One agent defines the contract; the others accept it.
One agent implements; a different provider reviews. Done work is evidenced by hashed
receipts, not asserted. The item closes only after independent review and the completion
gate. You come back for the result.

Watch it: the dashboard shows tasks, messages and live sessions. Do not write to the
board while a run is active.

---

## What it is, and what it is not

Co-op is a **local multi-agent coordination protocol for terminal coding agents**. Claude
Code, Codex and Grok share one SQLite **board**. The board holds the items, the goal
contracts, the exclusive claims, the peer questions, the hashed receipts, the independent
reviews and the completion gate. Code decides who owns what, what counts as done, and how
proof travels.

Co-op is **not** a harness: it does not own tools, context windows or the coding loop —
your existing agent CLIs do that. It is **not** a model router, a SaaS or a workflow suite.
It is one layer *across* the harnesses you already run.

---

## What Co-op claims, and what it does not

**Co-op makes no speed claim.** Provider inference dominates run wall-clock, so end-to-end
latency cannot measure the protocol. The only levers that move wall-clock are
protocol-level: fewer turns, and overlapping independent lanes.

**Co-op makes no reliability-rate claim.** Run `coop smoke --offline`, run the suite, then
run a real item on a repo of your own. That is the evidence this release offers.

What Co-op does state, and what the code enforces: the board is the only channel, done work
is evidenced instead of asserted, and a provider never marks its own homework.

---

## Platform status

| Platform | Status |
|---|---|
| Windows | **Supported.** Full suite, process-tree smoke, wheel and sdist install gates run on every public change. One limit: a workspace path containing a cmd.exe metacharacter (such as `&`, `%` or `!`) is refused at provider launch — [SECURITY.md](https://github.com/Quigleybits/agent-coop/blob/main/SECURITY.md#provider-launch) has the full list. |
| Linux | **Supported.** The same release gates run on Ubuntu. |
| macOS | **Unsupported in v0.1.0.** No compatibility claim or release gate. |

The public CI badge is the release signal. Check your own environment too — both
commands are offline and use no provider quota:

```bash
python -m pytest -q          # the full suite
coop smoke --offline         # runtime preflight
```

## More

| Doc | What it covers |
|---|---|
| [`docs/manual.md`](https://github.com/Quigleybits/agent-coop/blob/main/docs/manual.md) | Full manual: the contract-first `coop item create` path, `coop start --item` / `--all`, dashboard keys, `--herdr`, security posture. |
| [`SKILL.md`](https://github.com/Quigleybits/agent-coop/blob/main/SKILL.md) | Agent behavioral policy for launch, attach and judgment. |
| [`COOP_GUIDE.md`](https://github.com/Quigleybits/agent-coop/blob/main/COOP_GUIDE.md) | Operator and debug CLI appendix. |
| [`SECURITY.md`](https://github.com/Quigleybits/agent-coop/blob/main/SECURITY.md) | Security policy and vulnerability reporting. |
| [`CHANGELOG.md`](https://github.com/Quigleybits/agent-coop/blob/main/CHANGELOG.md) | Release history. |
| [`CONTRIBUTING.md`](https://github.com/Quigleybits/agent-coop/blob/main/CONTRIBUTING.md) | Reproduce the pinned test/build toolchain and run release checks without provider calls. |

## Security

Co-op spawns real agent processes that hold real tool authority. Run it on repos you
trust. Keep the review gate on. Never combine untrusted input, secrets and broad authority
in one unattended run. The manual's [security
section](https://github.com/Quigleybits/agent-coop/blob/main/docs/manual.md#10-security) states the full posture. Report a
vulnerability through the repository's [security policy](https://github.com/Quigleybits/agent-coop/blob/main/SECURITY.md).

## License

MIT. Copyright (c) 2026 Quigleybits. See [`LICENSE`](https://github.com/Quigleybits/agent-coop/blob/main/LICENSE).
