# Security policy

## Supported versions

Security fixes apply to the latest `0.1.x` release. Pre-release commits on
`main` can change without a compatibility guarantee.

## Security boundary

Agent Co-op coordinates provider CLIs. It does not sandbox them. A provider
process keeps the file, command, network and credential authority that it would
have when launched directly.

The security boundary is the operating-system user and the repository you
choose to run on. Everything below that line is a correctness rail, not a
security control.

Use Co-op only in repositories that you trust. Prefer a disposable branch or
worktree. Do not combine all three of these in one unattended run:

1. untrusted input;
2. secrets;
3. broad write, shell or network authority.

The local SQLite board is not a secret store. Do not put credentials in board
contracts, questions, messages, receipts or reviews.

## Identity is cooperative

All agents in a run execute as one OS user, with the same file, command and
network authority. Co-op tells them apart by `COOP_SESSION_ID`, which each
process asserts for itself. Co-op checks that the asserted session exists and is
running; it cannot check that the process is the one the session was minted
for.

The claim, review, completion and human-lane gates are correctness rails. They
stop cooperating agents from stepping on each other's work, from reviewing
their own work, and from closing an item without evidence. A misbehaving
process can bypass them: it can unset `COOP_SESSION_ID` to act as the human
operator, or assert a peer's session id to act as that peer. Treat every
agent in a run as trusted to the same degree as the least trusted one.

What Co-op does about it:

- Every read that a peer can make shortens session references to an
  eight-character display prefix: the item packet (`coop item show --packet`),
  the history dump (`coop item show --history`) and the dashboard sessions
  strip. The packet no longer carries the claim holder's harness command line
  or working directory.
- The board rows keep the full id for the protocol's own joins. A process
  with read access to `.coop/board.db` can read them; the board is a local
  file owned by the same OS user as every agent.

## Run Co-op only on repositories you trust

A repository can carry instructions for fully privileged agents. Clone only
what you would run by hand.

- **`.coop/` is an instruction set.** A cloned repository can ship
  `.coop/board.db` and `.coop/coop-capabilities.local.json`. Inside Git, board
  discovery walks up only through the nearest worktree root (a `.git`
  directory or linked-worktree/submodule `.git` file). Outside Git it checks
  only the current directory. Use an explicit CLI board/database option or
  `COOP_DB` for deliberate cross-workspace coordination. A capability file
  can name an MCP `command` that the provider CLI starts
  before any model turn. `coop init`, a write command, and the first
  dashboard open (`coop` or `coop monitor`) in a folder with no board all
  create `.coop/board.db`, `.coop/harness-adapters.json` and the two
  workspace adapters (`.claude/skills/coop/SKILL.md`,
  `.agents/skills/coop/SKILL.md`), and hide them through
  `.git/info/exclude`, which is local to your clone; it cannot stop another
  clone from tracking the directory. Read commands (`status`, `inbox`,
  `tasks`, …) never create anything. Inspect `.coop/` before the first run in
  a repository you did not create.
- **Sensitive run artifacts use a private per-user runtime root.** Provider
  authentication copies, generated MCP profiles, resident-provider state and
  prompt files are outside the workspace. The default is
  `%LOCALAPPDATA%\agent-coop\runtime` on Windows and
  `$XDG_STATE_HOME/agent-coop/runtime` on POSIX, with the normal per-user local
  state fallback when either base variable is absent. A relative platform base
  is rejected rather than resolved against the workspace. Set `COOP_RUNTIME_ROOT`
  only to a nonempty absolute path outside the workspace (and not an ancestor
  of it). An invalid override, an existing symlink/reparse component, or a
  nonempty unmarked root fails closed instead of falling back.
  Co-op protects claimed directories and secret files for the current owner
  (protected owner-only DACL on Windows; `0700` directories and `0600` files
  on POSIX). Each prompt/profile/worker state has its own marker-owned random
  directory and normal owner cleanup. When a new private runtime artifact is
  created, the lazy stale sweep removes only exact-marked directories older
  than 24 hours whose recorded process is definitely dead;
  unknown, live, unmarked, malformed or symlink/reparse-containing paths stay
  untouched. The location keeps these files out of workspace sync by default,
  and the permissions block inherited other-user access where the platform
  control applies. They do not isolate processes running as the same OS user.
  A backup service that mirrors the runtime root can still copy interrupted-run
  material.
- **Older workspace residue is not migrated automatically.** A prior version
  or interrupted old run may have left `launch-profile-*`, `worker-state-*` or
  `coop-prompt-*` content under `.coop/.coop-runs`. Stop the old run, inspect
  those specific directories and their ownership/markers, then handle them
  deliberately. Do not use a broad cleanup command: the new runtime sweeper
  intentionally never guesses ownership of old workspace content.
- **External launchers are pinned outside the workspace.** Co-op resolves
  provider and Herdr launchers by walking `PATH` itself, excluding the selected
  workspace and links back into it, and invokes the resulting absolute path.
  For a command you invoke yourself, the Python import path is still your
  responsibility: a workspace-level `agent_coop/` directory can shadow the
  installed package under `python -m` semantics. The isolated Herdr mirror
  bootstrap pins the installed package root instead of relying on that lookup.
- **Any agent can stop a run.** Creating `<workspace>/.coop/.coop-auto-stop`
  halts the runner at the next boundary. This is by design and is a
  denial-of-service lever for any process that can write to the workspace.
- **The board records each session's launch command.** Never pass a secret
  on a `coop session run … -- <cli> …` command line. The packet no longer
  exposes it to peers, but the sessions table stores it, and every process of
  the same OS user can read the board file.

## Global launch skill

The first dashboard open (`coop`, `coop monitor`) and every `coop init`
write the `/coop` launch skill into your home directory, outside the
workspace: a copy of the Claude adapter at `~/.claude/skills/coop/SKILL.md`
and copies of the shared Agent Skills adapter at
`~/.codex/skills/coop/SKILL.md` and `~/.grok/skills/coop/SKILL.md`. These are
plain files, not links. The hash record in `~/.coop/global-skills.json`
follows the workspace-adapter rules: a later version updates a file that
still holds the text Co-op last wrote, and preserves a file you edited. A
destination that is a symlink is skipped. Co-op reports the install once,
on the dashboard notice line or in the `coop init` output, and reports a
failed write as one line; it never fails the command for it. To opt out,
pass `--no-global-skills` or set `COOP_NO_GLOBAL_SKILLS=1`. The skill only
routes an explicit `/coop` request to the `coop` CLI; loading it starts no
process.

## Provider launch

- **Prompts never travel in argv.** Every prompt, including the isolated
  structured-answer and structured-decision prompts, reaches the provider on
  stdin or through a prompt file. Board text (item titles, question text)
  never becomes a command-line argument. Temporary delivery files are created
  in the protected per-user runtime root: Claude and Codex read them through
  stdin, while Grok receives the protected file path rather than the prompt
  bytes in argv.
- **Launcher argv is checked before the spawn.** On Windows the provider CLIs
  are npm `.cmd` shims, and cmd.exe does not honour Python's argument quoting.
  When the launcher is a `.cmd` or `.bat` file, Co-op refuses to spawn if any
  other argument contains a cmd.exe metacharacter (`& | < > ^ % !`, CR, LF)
  or a quote outside JSON syntax. A refused launch ends the turn as
  `worker_start_failed`; the run stops with a readable reason.
- **Launchers are resolved from `PATH`, never from the workspace.** Co-op
  walks `PATH` itself, skips empty and `.` entries, skips any directory that
  is the workspace or below it, and on Windows accepts only a `PATHEXT`
  launcher. It resolves each provider once per run and keeps that answer;
  a launcher dropped into the repository or onto `PATH` mid-run is not used.
- **The optional Herdr client follows the same boundary.** `coop start
  --herdr` resolves one absolute launcher outside the selected workspace,
  keeps it for that adapter, runs from the launcher's directory, applies the
  Windows launcher argument guard, and supplies only platform values plus
  Herdr's current-session selectors. Each mirror command starts the pinned
  Agent Co-op package and clears inherited provider and unrelated credential
  values before reading the trace. Herdr panes are display-only; providers do
  not run inside them and pane output never becomes board state.
- **Warm session ids are validated** (UUID shape) before they are reused.

## Child environment

Provider children of an autonomous run (`coop start`) receive an allowlisted
environment, not the full shell. Every child gets the platform/runtime
baseline and Co-op bindings. Claude receives `ANTHROPIC_*` and `CLAUDE_*`;
Codex receives `OPENAI_*` and `CODEX_*`; Grok receives `XAI_*` and `GROK_*`.
The launch profile adds only the environment values named by MCP capabilities
selected for that provider turn. A provider child and an MCP server it starts
therefore do not receive another provider's namespace by default.

Credential-shaped variables outside the selected provider or MCP capability
(`GITHUB_TOKEN`, `NPM_TOKEN`, unrelated `*_API_KEY` values, and similar) do
not reach cold turns, persistent workers, structured one-shots or routed board
commands. Claude cloud-backend credentials pass only when the matching
`CLAUDE_CODE_USE_*` flag selects that backend. Structured one-shots also omit
Co-op board/session state. Herdr CLI and mirror processes use the narrower
environment described above.

`coop start --inherit-env` deliberately restores the full parent environment
for provider children for one run; the run banner and board record that mode.
It does not widen the Herdr client environment.

`coop session run` is different: a human launches their own harness on
purpose, so that child inherits the full environment by default. To scope it,
pass `--env-allowlist NAME` (repeatable): the child then receives the platform
baseline, the named parent variables that exist, and the `COOP_*` set. Keep
tokens for other services out of the shell that starts any run.

## Report a vulnerability

Use GitHub private vulnerability reporting for this repository. Do not publish
an exploit, credential, private repository path or sensitive board content in a
public issue. If private reporting is unavailable, open a public issue that
asks the maintainer to enable a private channel, without vulnerability details.

Include:

- affected Agent Co-op version or commit;
- Windows or Linux version and Python version;
- minimal reproduction steps;
- expected and observed security boundary;
- impact;
- whether the report involves live credentials or provider quota.

Useful report classes include workspace path escape, command or argument
injection into a provider launch, process-tree escape, a write outside the
workspace or board that Co-op chose, a false successful completion recorded by
Co-op itself, and credentials included in a distribution.

Reports that a cooperating agent can bypass a claim, review or completion gate
by asserting another identity describe the documented trust model above, not a
vulnerability. Report a gate bypass when it works *without* asserting another
identity.

Provider-model behavior inside its authorized tool boundary is normally a
provider or task-design issue. Report it here when Co-op widens the granted
boundary, misroutes authority, or records a false successful completion.
