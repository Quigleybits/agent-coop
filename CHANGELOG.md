# Changelog

## 0.1.0 — Initial public beta

Agent Co-op coordinates Claude, Codex and Grok on a local SQLite board.

### Features

- Start a goal with `coop "<goal>"`, or launch an existing item with
  `coop start --item <id>`.
- Track contracts, task ownership, questions, handoffs and decisions.
- Require hashed receipts, independent review and acceptance before completion.
- Open the terminal dashboard with `coop`; switch workspaces with Ctrl+O.
- Install workspace adapters and the `/coop` launch skill for supported agents.
- Observe provider traces through optional Herdr integration.
- Verify the installation with `coop smoke --offline`, without provider calls.
- Install from PyPI using uv or pipx. The runtime has no third-party dependencies.

### Security and support

- Provider launchers are pinned outside the workspace, with guarded arguments
  and provider-specific environment forwarding.
- Sensitive run files use a protected per-user directory outside the workspace.
- Board discovery respects the nearest Git worktree boundary.
- Windows and Linux are supported; macOS is not yet supported.
- Co-op is a local coordination tool, not a sandbox. Use trusted repositories
  and supervised agents. See [SECURITY.md](SECURITY.md) for its trust boundary.
