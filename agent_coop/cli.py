#!/usr/bin/env python3
"""Agent_coop CLI.  Identity: --as/$COOP_AGENT.  DB: --db/$COOP_DB/.coop/board.db."""
import argparse, contextlib, json, os, pathlib, shutil, sqlite3, subprocess
import re, sys, time, uuid
from agent_coop import coop_adapters, coop_monitor, coop_schema, coop_start, coop_supervisor, coop_templates, coop_ui, coopdb
from agent_coop.coop_cli_style import CoopArgumentParser
from agent_coop.coop_errors import IncompleteContract, ReceiptHashMismatch

CONTRACT_FILE_KEYS = frozenset(coopdb.CONTRACT_FIELDS) | {
    "owner", "next_actor", "review_waiver", "review_quorum"}

# --- brand palette (24-bit truecolor ANSI): claude orange / codex blue / grok grey ---
_CLAUDE = "\033[38;2;215;119;87m"   # #d77757  Claude Code
_CODEX  = "\033[38;2;136;192;208m"  # #88c0d0  Codex
_GROK   = "\033[38;2;147;147;147m"  # #939393  Grok
_BOLD, _DIM, _RST = "\033[1m", "\033[2m", "\033[0m"
_AGENT_COLOR = {"claude": _CLAUDE, "codex": _CODEX, "grok": _GROK}

def _c(agent):
    return _AGENT_COLOR.get(agent, _GROK)

# Operator row reads live in coopdb (shared with coop_monitor); the old
# private names stay as aliases for existing callers and tests.
_task_rows = coopdb.task_rows
_message_rows = coopdb.message_rows
_session_rows = coopdb.session_rows

def _monitor_frame(conn, *, width, color, interval, include_logo):
    """One monitor frame from a live connection (the patchable render seam)."""
    return coop_ui.render_monitor_frame(
        tasks=_task_rows(conn),
        messages=_message_rows(conn, limit=12),
        sessions=_session_rows(conn),
        color=color,
        width=width,
        interval=interval,
        stamp=coopdb.now(),
        include_logo=include_logo,
    )

def render_status(conn, color=True):
    """Legacy composite board for tests and plain one-shot dumps."""
    width = shutil.get_terminal_size((100, 24)).columns
    return coop_ui.render_monitor_frame(
        tasks=_task_rows(conn),
        messages=_message_rows(conn, limit=20),
        sessions=_session_rows(conn),
        color=color,
        width=width,
        interval=0,
        stamp=coopdb.now(),
        include_logo=False,
    )

def monitor(db_path, interval=2, notice=""):
    """Live operator dashboard: redraw in place (alt-screen when VT works).

    ``notice`` is what the full dashboard would show on its notice line
    (board created, skill installed); this fallback has no notice line, so
    it prints the text once to stderr before the first frame."""
    if notice:
        print(notice, file=sys.stderr, flush=True)
    color = sys.stdout.isatty()
    used_alt = coop_ui.begin_live_display(sys.stdout) if color else False
    try:
        while True:
            width = shutil.get_terminal_size((100, 24)).columns
            conn = coopdb.connect(db_path, require_current=True)
            try:
                # Logo only on the live alt-screen; plain dumps stay grep-able.
                frame = _monitor_frame(
                    conn, width=width, color=color and used_alt,
                    interval=interval, include_logo=used_alt)
            finally:
                conn.close()
            if used_alt:
                coop_ui.paint_live_display(frame, sys.stdout)
            else:
                # Fallback for non-VT: still home+clear when possible.
                sys.stdout.write("\033[2J\033[H" + frame + "\n")
                sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        coop_ui.end_live_display(sys.stdout, used_alt=used_alt)

# Canonical env names with compatibility aliases; the supervisor writes both
# pairs with identical values, the CLI reads canonical first.
def _db(args):
    """--workspace → --db → COOP_DB → repo discovery from cwd →
    COOP_DEFAULT_DB → the cwd-relative default."""
    workspace = getattr(args, "workspace", None)
    if workspace:
        # B4: one board per working directory, inside it.
        return str(pathlib.Path(workspace).expanduser() / ".coop" / "board.db")
    explicit = (args.db or os.environ.get("COOP_DB")
                or os.environ.get("COOP_DB_PATH"))
    if explicit:
        return explicit
    discovered = coopdb.discover_board(os.getcwd())
    if discovered:
        return discovered
    return os.environ.get("COOP_DEFAULT_DB") or ".coop/board.db"

def _db_meta(args):
    """The resolved board path plus how it resolved.

    `source` is one of workspace | explicit | discovered | default_db |
    cwd_default. Only `cwd_default` (no --workspace, no --db/COOP_DB, no
    ancestor board, no COOP_DEFAULT_DB) is eligible for auto-provisioning:
    every other source is an intentional target whose absence is an error,
    not a reason to create a board."""
    if getattr(args, "workspace", None):
        return _db(args), "workspace"
    if (args.db or os.environ.get("COOP_DB")
            or os.environ.get("COOP_DB_PATH")):
        return _db(args), "explicit"
    if coopdb.discover_board(os.getcwd()):
        return _db(args), "discovered"
    if os.environ.get("COOP_DEFAULT_DB"):
        return _db(args), "default_db"
    return _db(args), "cwd_default"

def _print_adapter_results(workspace, *, stream):
    results = coop_adapters.install_workspace_adapters(workspace)
    for result in results:
        print(
            f"adapter {result['status']}: {result['path']}",
            file=stream,
        )


# Commands that only read the board. When no board resolves they report
# that and stop; only a write command may auto-provision one. Keys are
# "<cmd>" or "<cmd> <subcommand>" as `_command_key` renders them. `monitor`
# is not listed: opening the dashboard in a folder with no board creates
# one (`_dashboard_board`), the way the switcher's create-on-open does.
_READ_ONLY_COMMANDS = frozenset({
    "status", "inbox", "tasks", "task", "queue", "board", "agents",
    "item show", "envelope list", "envelope show", "huddle show",
})


def _command_key(args):
    sub = getattr(args, f"{args.cmd}_cmd", None)
    return f"{args.cmd} {sub}" if sub else args.cmd


def _reads_only(args):
    return _command_key(args) in _READ_ONLY_COMMANDS


def _auto_provision(db):
    """Create a board and harness adapters at the cwd-default workspace.

    Only a write command reaches here (`_reads_only` stops the read commands
    first). The workspace guard refuses the home directory, its parents, a
    filesystem root and the temp directory before any write. The file list
    goes to stderr before the first write, so a JSON stdout stays clean.
    """
    workspace = coopdb.board_workspace(pathlib.Path(db).expanduser())
    coop_adapters.refuse_unsafe_workspace(
        workspace, allow_temp_root=False,
        hint="cd into a project directory or run "
             "`coop init --workspace <dir>`")
    coop_adapters.validate_workspace_adapter_paths(workspace)
    print(coop_adapters.provision_notice(db), file=sys.stderr)
    for line in coop_adapters.provision_workspace_board(db):
        print(line, file=sys.stderr)

def _db_is_explicit(args):
    # `--workspace` names a board path as directly as `--db` does. Ordinary
    # commands with an explicit path must not pin the switcher registry
    # (scripts/tests/supervised sessions would pollute ~/.coop).
    # Creation is different: `cmd_init` always records.
    # Dashboard open also always records (`cmd_monitor`).
    return bool(getattr(args, "workspace", None) or args.db
                or os.environ.get("COOP_DB")
                or os.environ.get("COOP_DB_PATH"))

def _env_agent():
    return os.environ.get("COOP_AGENT") or os.environ.get("COOP_AGENT_ID")

def _env_item_id():
    raw = os.environ.get("COOP_ITEM_ID")
    if raw in (None, ""):
        return None
    try:
        item_id = int(raw)
    except ValueError as exc:
        raise coopdb.InvalidTransition(
            "COOP_ITEM_ID must be a positive integer",
            reason_code="input_invalid",
            evidence={"constraint": "valid_cli_arguments_required"},
        ) from exc
    if item_id <= 0:
        raise coopdb.InvalidTransition(
            "COOP_ITEM_ID must be a positive integer",
            reason_code="input_invalid",
            evidence={"constraint": "valid_cli_arguments_required"},
        )
    return item_id


def _positive_item_id(value):
    try:
        item_id = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be a positive integer") from exc
    if item_id <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return item_id

def _agent(args):
    a = getattr(args, "as_agent", None) or _env_agent()
    if not a: sys.exit("no identity: pass --as <name> or set COOP_AGENT")
    return a

def exclude_board_from_git(workspace):
    return coop_adapters.exclude_board_from_git(workspace)


def _install_global_skills(args) -> str:
    """Install the user-global `/coop` launch skill on a first run.

    Returns the one-line notice to show: the install announcement when a
    file was installed or updated, a `could not install …` line when the
    home is unwritable or unknown, and the empty string when nothing
    changed or the operator opted out (`--no-global-skills`,
    `COOP_NO_GLOBAL_SKILLS=1`). Never raises: the skill is a convenience,
    the board is the work.
    """
    if getattr(args, "no_global_skills", False) \
            or coop_adapters.global_skills_disabled():
        return ""
    try:
        results = coop_adapters.install_global_skills()
    except Exception as error:
        detail = str(error).strip() or type(error).__name__
        return f"could not install /coop skill: {detail}"
    return coop_adapters.global_skills_notice(results)


def cmd_init(conn, args):
    coopdb.init_db(conn)
    if args.judge: coopdb.register_agent(conn, args.judge)
    db = _db(args)
    # Board creation always pins the switcher so humans/agents see new boards
    # immediately. Tests redirect via COOP_BOARDS_REGISTRY — never skip this
    # call on the creation path, including init inside a foreign repo.
    coopdb.record_board(db)
    print("initialized " + db + (f" judge={args.judge}" if args.judge else ""))
    if getattr(args, "workspace", None):
        _print_adapter_results(args.workspace, stream=sys.stdout)
        print(exclude_board_from_git(args.workspace))
    skills_notice = _install_global_skills(args)
    if skills_notice:
        print(skills_notice)

def cmd_smoke(conn, args):
    # Preflight owns its own temp board; it never opens the caller's board.
    from agent_coop import coop_smoke
    result = coop_smoke.run_smoke(offline=args.offline)
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        for line in coop_smoke.render_text(result):
            print(line)
    if not result["ok"]:
        raise SystemExit(1)


def cmd_herdr(conn, args):
    """Read-only Herdr observability surfaces; never opens the board."""
    from agent_coop import coop_herdr

    outcome = coop_herdr.render_mirror(
        args.run_id,
        args.provider,
        max_idle_polls=args.max_idle_polls,
    )
    if outcome == "idle_timeout":
        raise coop_herdr.HerdrMirrorTimeout(
            "Herdr mirror stopped after the trace remained idle"
        )


def cmd_envelope_list(conn, args):
    from agent_coop import coop_envelope
    item_id = args.item if args.item is not None else _env_item_id()
    envelopes = coop_envelope.project(conn, item_id=item_id, since=args.since)
    if getattr(args, "json", False):
        print(json.dumps(envelopes, indent=2))
        return
    if not envelopes:
        print("(no envelopes)")
        return
    for line in coop_envelope.render_lines(envelopes):
        print(line)


def cmd_envelope_show(conn, args):
    from agent_coop import coop_envelope
    envelope = coop_envelope.show(conn, args.event_id)
    if envelope is None:
        raise coopdb.NotFound(
            f"event {args.event_id} does not project an envelope")
    print(json.dumps(envelope, indent=2))


def _peek_user_version(db_path):
    try:
        with contextlib.closing(
            sqlite3.connect(
                coop_schema._sqlite_uri(pathlib.Path(db_path), "ro"), uri=True
            )
        ) as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error:
        return "?"

def cmd_migrate(conn, args):
    db = _db(args)
    source = _peek_user_version(db)
    backup = coopdb.migrate_db(
        db,
        backup_path=args.backup,
        confirm_legacy_clients_stopped=args.confirm_legacy_clients_stopped,
    )
    print(f"migrated schema {source} -> {coop_schema.SCHEMA_VERSION} backup={backup}")

def cmd_say(conn, args):
    session_id = os.environ.get("COOP_SESSION_ID")
    item_id = args.item if args.item is not None else _env_item_id()
    mid = coopdb.say(conn, session_id=session_id, body=args.body,
                     to_agent=args.to, item_id=item_id, room=args.room)
    print(json.dumps({"message_id": mid}) if args.json
          else f"message {mid}")

def cmd_inbox(conn, args):
    session_id = os.environ.get("COOP_SESSION_ID")
    if session_id:
        agent, _ = coopdb.resolve_actor(conn, session_id, _env_agent())
    else:
        agent = _agent(args)
        if not args.peek:
            # A human read must never advance an agent's delivery cursor.
            raise coopdb.InvalidTransition(
                "outside a supervised session, inbox reads are peek-only "
                "(pass --peek); only the agent itself consumes its cursor")
    item_id = _env_item_id()
    entries = [dict(r) for r in coopdb.read_inbox(
        conn, agent, peek=args.peek, item_id=item_id)]
    if args.json:
        print(json.dumps(entries))
        return
    for e in entries:
        item = f" (item {e['item_id']})" if e["item_id"] else ""
        print(f"[{e['category']}]{item} {e['payload_json']}")

# Pre-release mutation subcommands (checkpoint, watch, assign, review, debate,
# decision) are retired: the version 4 protocol replaces them with
# claim-bound guarded transitions. The full-contract item family and the
# deterministic queue below are their replacements.

def _load_contract_file(path):
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise IncompleteContract(f"could not read contract file: {exc}")
    except ValueError as exc:
        raise IncompleteContract(f"contract file is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise IncompleteContract("contract file must hold a JSON object")
    unknown = sorted(set(data) - CONTRACT_FILE_KEYS)
    if unknown:
        raise IncompleteContract(
            f"unknown contract field(s): {', '.join(unknown)}")
    return data

_EXPAND_FIELDS = ("objective", "scope", "done_when", "output_contract",
                  "context", "allowed_actions", "stop_conditions")


def _expand_goal_contract(fields, *, allow_bare_fallback=False):
    """Kickoff-time contract expansion: a fast model drafts the missing
    contract fields from the human's goal BEFORE the run starts, so the
    item enters the board as a complete human contract (no acceptance
    huddle). Explicitly provided fields are never overwritten.

    Expansion failure FAILS THE KICKOFF unless the caller opted into the
    bare-goal fallback: silently creating an incomplete item restores the
    contract-fill and acceptance-huddle turns the flag exists to remove."""
    import subprocess
    import sys as _sys
    goal = fields.get("objective") or fields.get("title")
    if not goal:
        raise IncompleteContract("--expand requires --title or --objective")
    missing = [f for f in _EXPAND_FIELDS if not fields.get(f)]
    if not missing:
        return fields
    from agent_coop import coop_start as _coop_start
    binary = _coop_start.resolve_cli("claude") or "claude"
    prompt = (
        "Draft an Agent Co-op task contract for the goal below. Reply with "
        "STRICT JSON only (no markdown fences) using exactly these keys: "
        + ", ".join(missing) + ". allowed_actions and stop_conditions are "
        "arrays of short strings; the rest are strings. Be concrete and "
        "bounded; name exact output file paths; include honest stop "
        "conditions (no fabricated peer responses, no extra files). "
        "Include this routing discipline verbatim in the context field: "
        + coop_templates.ONE_TURN_ROUTING_LINE + "\n\n"
        "Goal: " + str(goal)
    )
    try:
        proc = subprocess.run(
            [binary, "-p", prompt, "--model", "claude-sonnet-5"],
            capture_output=True, text=True, timeout=180,
        )
        raw = (proc.stdout or "").strip()
        draft = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    except Exception as exc:
        if allow_bare_fallback:
            print(f"warn: --expand failed ({type(exc).__name__}); "
                  "creating the bare goal item", file=_sys.stderr)
            return fields
        raise IncompleteContract(
            f"--expand failed ({type(exc).__name__}); no item was created. "
            "Re-run with --expand-fallback-bare to accept a bare goal item "
            "(it will need contract fill + an acceptance huddle)."
        ) from exc
    for key in missing:
        value = draft.get(key)
        if value:
            fields[key] = value
    if not fields.get("objective"):
        fields["objective"] = str(goal)
    return fields


def cmd_item(conn, args):
    if args.item_cmd == "create":
        fields = {}
        template_provenance = None
        if getattr(args, "template", None):
            # Base layer: a complete human contract with zero model calls;
            # contract file and explicit flags still override.
            rendered_template = coop_templates.render_contract_template(
                args.template, goal=args.title or args.objective)
            fields.update(rendered_template)
            template_provenance = (
                coop_templates.contract_template_provenance(
                    args.template, rendered_template
                )
            )
        if args.contract:
            fields.update(_load_contract_file(args.contract))
        for key in ("title", "objective", "scope", "done_when",
                    "output_contract", "context", "owner", "next_actor"):
            value = getattr(args, key)
            if value is not None:
                fields[key] = value
        if args.allowed_action is not None:
            fields["allowed_actions"] = args.allowed_action
        if args.stop_condition is not None:
            fields["stop_conditions"] = args.stop_condition
        if getattr(args, "expand", False):
            fields = _expand_goal_contract(
                fields,
                allow_bare_fallback=getattr(
                    args, "expand_fallback_bare", False),
            )
        if args.review_waiver is not None:
            fields["review_waiver"] = args.review_waiver
        if args.review_quorum is not None:
            fields["review_quorum"] = args.review_quorum
        if args.require_review:
            fields.pop("review_waiver", None)  # the default, reasserted
            fields["review_quorum"] = 1
        actor, session_id = coopdb.resolve_actor(
            conn, os.environ.get("COOP_SESSION_ID"), _env_agent())
        item_id = coopdb.create_item(
            conn, actor=actor, session_id=session_id,
            title=fields.get("title"), objective=fields.get("objective"),
            scope=fields.get("scope"), done_when=fields.get("done_when"),
            output_contract=fields.get("output_contract"),
            context=fields.get("context"),
            allowed_actions=fields.get("allowed_actions"),
            stop_conditions=fields.get("stop_conditions"),
            owner=fields.get("owner"), next_actor=fields.get("next_actor"),
            review_waiver_reason=fields.get("review_waiver"),
            review_quorum=fields.get("review_quorum"),
            template_provenance=template_provenance)
        print(json.dumps({"item_id": item_id}) if args.json else item_id)
    elif args.item_cmd == "claim":
        if args.reclaim and not args.reason:
            raise coopdb.InvalidTransition(
                "--reclaim requires --reason <text>",
                reason_code="input_invalid",
                evidence={"constraint": "valid_cli_arguments_required"},
            )
        if args.reason and not args.reclaim:
            raise coopdb.InvalidTransition(
                "--reason only applies with --reclaim",
                reason_code="input_invalid",
                evidence={"constraint": "valid_cli_arguments_required"},
            )
        actor, session_id = coopdb.resolve_actor(
            conn, os.environ.get("COOP_SESSION_ID"), _env_agent())
        result = coopdb.claim_item(
            conn, item_id=args.id, actor=actor, session_id=session_id,
            intent=args.intent,
            reclaim_reason=args.reason if args.reclaim else None,
            lease_seconds=args.lease_seconds)
        if args.json:
            print(json.dumps(result))
        else:
            print(f"claim {result['claim_id']} lane {result['lane']} "
                  f"lease-expires {result['lease_expires_at']}")
    elif args.item_cmd == "define":
        fields = _load_contract_file(args.contract) if args.contract else {}
        fields = {k: v for k, v in fields.items()
                  if k in coopdb.DEFINABLE_FIELDS}
        for key in ("scope", "done_when", "output_contract", "context"):
            value = getattr(args, key)
            if value is not None:
                fields[key] = value
        if args.allowed_action is not None:
            fields["allowed_actions"] = args.allowed_action
        if args.stop_condition is not None:
            fields["stop_conditions"] = args.stop_condition
        actor, session_id = coopdb.resolve_actor(
            conn, os.environ.get("COOP_SESSION_ID"), _env_agent())
        result = coopdb.define_item(
            conn, claim_id=args.claim, session_id=session_id, actor=actor,
            fields=fields)
        print(json.dumps(result) if args.json
              else f"item {result['item_id']} defined "
                   f"({', '.join(result['defined'])}); "
                   f"contract {'complete' if result['contract_complete'] else 'still a draft'}")
    elif args.item_cmd == "refine":
        fields = _load_contract_file(args.contract) if args.contract else {}
        for key in ("scope", "done_when", "output_contract", "context"):
            value = getattr(args, key)
            if value is not None:
                fields[key] = value
        if args.allowed_action is not None:
            fields["allowed_actions"] = args.allowed_action
        if args.stop_condition is not None:
            fields["stop_conditions"] = args.stop_condition
        actor, session_id = coopdb.resolve_actor(
            conn, os.environ.get("COOP_SESSION_ID"), _env_agent())
        result = coopdb.refine_item(
            conn, claim_id=args.claim, session_id=session_id, actor=actor,
            fields=fields)
        print(json.dumps(result) if args.json
              else f"item {result['item_id']} refined to contract "
                   f"version {result['contract_version']}; peer acceptance pending")
    elif args.item_cmd == "revise":
        fields = {}
        for key in ("title", "objective", "scope", "done_when",
                    "output_contract", "context"):
            value = getattr(args, key)
            if value is not None:
                fields[key] = value
        if args.allowed_action is not None:
            fields["allowed_actions"] = args.allowed_action
        if args.stop_condition is not None:
            fields["stop_conditions"] = args.stop_condition
        if args.review_waiver is not None:
            fields["review_waiver"] = args.review_waiver
        if args.review_quorum is not None:
            fields["review_quorum"] = args.review_quorum
        if args.require_review:
            fields["require_review"] = True
        result = coopdb.revise_item(
            conn, item_id=args.id, reason=args.reason, fields=fields,
            contract_path=args.contract)
        print(json.dumps(result) if args.json
              else f"item {result['item_id']} revised to contract "
                   f"version {result['contract_version']}")
    elif args.item_cmd == "complete":
        actor, session_id = coopdb.resolve_actor(
            conn, os.environ.get("COOP_SESSION_ID"), _env_agent())
        result = coopdb.complete_item(
            conn, claim_id=args.claim, session_id=session_id, actor=actor)
        if "failed" in result:
            # The recorded path: the supersede and its event are already
            # committed — the command layer converts the returned outcome
            # into the typed failure.
            details = "; ".join(
                f.get("detail", f.get("path", f.get("ref", "")))
                for f in result["failures"])
            raise ReceiptHashMismatch(
                f"completion evidence failed; receipt "
                f"{result['superseded_receipt_id']} superseded: {details}",
                reason_code="receipt_hash_mismatch",
                evidence={
                    "item_id": result["completed_item_id"],
                    "receipt_id": result["superseded_receipt_id"],
                    "constraint": "receipt_bytes_changed",
                },
            )
        print(json.dumps(result) if args.json
              else f"item {result['completed']} done "
                   f"(event {result['event_id']})")
    else:  # show
        data = coopdb.item_show(
            conn, args.id, packet=args.packet, history=args.history)
        print(json.dumps(data) if args.json else json.dumps(data, indent=2))

def cmd_claim(conn, args):
    actor, session_id = coopdb.resolve_actor(
        conn, os.environ.get("COOP_SESSION_ID"), _env_agent())
    coopdb.release_claim(
        conn, claim_id=args.claim, actor=actor, session_id=session_id,
        reason=args.reason)
    print(json.dumps({"released": args.claim}) if args.json
          else f"released claim {args.claim}")

def cmd_admin(conn, args):
    # Offline / end-of-run human tooling — not mid-run product happy path.
    if args.admin_cmd == "answer":
        coopdb.admin_answer(
            conn, question_id=args.question_id, answer=args.answer,
            reason=args.reason)
        print(json.dumps({"answered": args.question_id}) if args.json
              else f"answered question {args.question_id} (operator offline)")
        return
    coopdb.admin_release(
        conn, claim_id=args.claim_id, reason=args.reason,
        confirm_process_stopped=args.confirm_process_stopped)
    print(json.dumps({"released": args.claim_id}) if args.json
          else f"released claim {args.claim_id} (operator offline)")


def cmd_recover(conn, args):
    """Agent-side recovery (product path) — peers unwedge stale claims."""
    session_id = os.environ.get("COOP_SESSION_ID")
    if args.recover_cmd == "wedge":
        result = coopdb.agent_release_wedge(
            conn, claim_id=args.claim_id, session_id=session_id,
            reason=args.reason,
            abandon_after_s=args.abandon_after_seconds)
        print(json.dumps(result) if args.json
              else f"agent-released claim {result['released']}"
                   + (" (already)" if result.get("already") else ""))
        return
    if args.recover_cmd == "human-audit":
        report = coopdb.mid_run_human_mutations(
            conn, since_iso=args.since, until_iso=args.until)
        print(json.dumps(report) if args.json
              else f"mid-run human mutations: {report['count']}")
        return
    raise coopdb.InvalidTransition(
        f"unknown recover subcommand",
        reason_code="input_invalid",
        evidence={"constraint": "valid_cli_arguments_required"},
    )

_TIMING_LIMIT = 31_536_000  # one year of seconds
_TIMING_FLAGS = (("max_runtime_seconds", "--max-runtime-seconds"),
                 ("shutdown_grace_seconds", "--shutdown-grace-seconds"),
                 ("lease_seconds", "--lease-seconds"),
                 ("checkpoint_limit_seconds", "--checkpoint-limit-seconds"),
                 ("interval", "--interval"))

def _enforce_timing_bounds(args):
    """Every CLI timing surface satisfies 1 <= v <= 31_536_000 at the
    conversion chokepoint — a typed refusal before any database or process
    work, never an OverflowError traceback from timedelta arithmetic.
    (NaN fails the comparison and is refused with the rest.)"""
    for attr, flag in _TIMING_FLAGS:
        value = getattr(args, attr, None)
        if value is None:
            continue
        if not (1 <= value <= _TIMING_LIMIT):
            raise coopdb.InvalidTiming(
                f"{flag} must be between 1 and {_TIMING_LIMIT} seconds "
                f"(one year), got {value!r}")

def _session_id_or_env(args):
    sid = getattr(args, "session", None) or os.environ.get("COOP_SESSION_ID")
    if not sid:
        sys.exit("no session: pass --session <id> or set COOP_SESSION_ID")
    return sid


def cmd_session_start(args):
    """Mint a session for THIS (foreign-harness) process so its `coop`
    commands are attributed to the provider, not the trusted-local human.
    An external session has NO coop supervisor: no 5s auto-renew and no
    kill-on-death — claim with a long --lease-seconds and/or `session renew`,
    and `session end` when done (else it lingers until max_runtime + sweep)."""
    provider = args.provider
    agent = args.name or provider
    db = _db(args)
    max_runtime = int(args.max_runtime_seconds or 28800)
    session_id = "coopstart-" + provider + "-" + uuid.uuid4().hex[:12]
    conn = coopdb.connect(db, require_current=True)
    try:
        coopdb.register_or_bind_agent(conn, agent_id=agent, provider=provider)
        coopdb.insert_session(
            conn, session_id=session_id, agent_id=agent, provider=provider,
            command=["external", provider], cwd=os.getcwd(),
            max_runtime_s=max_runtime, grace_s=10,
            stdin_isatty=sys.stdin.isatty())
    finally:
        conn.close()
    env = {"COOP_SESSION_ID": session_id, "COOP_AGENT": agent,
           "COOP_PROVIDER": provider, "COOP_DB": db}
    exports = "\n".join(f'export {k}="{v}"' for k, v in env.items())
    if getattr(args, "json", False):
        print(json.dumps({"session_id": session_id, "agent": agent,
                          "provider": provider, "db": db, "env": env}))
        return
    if getattr(args, "export", False):
        print(exports)
        return
    print(f"session {session_id} started as {agent} (provider {provider})")
    print(exports)
    print("# source these before any other coop command, "
          "or your actions are attributed to 'human'")


def cmd_session_end(args):
    session_id = _session_id_or_env(args)
    conn = coopdb.connect(_db(args), require_current=True)
    try:
        coopdb.finish_session(conn, session_id, status="exited",
                              reason="child_exit", exit_code=0)
    finally:
        conn.close()
    print(f"session {session_id} ended")


def cmd_session_renew(args):
    session_id = _session_id_or_env(args)
    conn = coopdb.connect(_db(args), require_current=True)
    try:
        n = coopdb.renew_claims(conn, session_id=session_id)
    finally:
        conn.close()
    print(f"session {session_id}: renewed {n} claim(s)")


def cmd_session(conn, args):
    sub = getattr(args, "session_cmd", "run")
    if sub == "start":
        return cmd_session_start(args)
    if sub == "end":
        return cmd_session_end(args)
    if sub == "renew":
        return cmd_session_renew(args)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    timings = coop_supervisor.Timings()
    for flag, field in (("max_runtime_seconds", "max_runtime_s"),
                        ("shutdown_grace_seconds", "shutdown_grace_s"),
                        ("lease_seconds", "lease_s"),
                        ("checkpoint_limit_seconds", "checkpoint_limit_s")):
        value = getattr(args, flag)
        if value is not None:
            setattr(timings, field, value)
    supervisor = coop_supervisor.Supervisor(
        _db(args), provider=args.provider, agent_id=args.name,
        argv=command, timings=timings, env_allowlist=args.env_allowlist)
    raise SystemExit(supervisor.run())

def cmd_start(conn, args):
    """The sole public runner: delegate to wake-driven product mode."""
    board = args.board or _db(args)
    herdr_adapter = None
    if args.herdr:
        from agent_coop import coop_herdr

        herdr_adapter = coop_herdr.HerdrAdapter(
            workspace=coopdb.board_workspace(
                pathlib.Path(board).expanduser()
            ),
            environ=os.environ,
        )
        if not herdr_adapter.available():
            # The adapter intentionally exposes one silent boolean covering both
            # binary resolution and live-session reachability.  Keep the
            # public diagnostic equally bounded until that adapter offers a
            # more specific, non-output-bearing reason.
            raise coop_herdr.HerdrUnavailable(
                "--herdr requires the Herdr binary and a reachable live "
                "Herdr session",
                evidence={
                    "constraint": (
                        "herdr_binary_and_live_session_required"
                    ),
                },
            )

    from agent_coop import coop_autonomous

    timeout = float(args.timeout_seconds)
    runner_argv = ["--db", str(board)]
    if args.item is not None:
        runner_argv += ["--item", str(args.item)]
    else:
        runner_argv.append("--all")
    if args.agents:
        runner_argv += ["--agents", args.agents]
    runner_argv += [
        "--interval", str(args.interval),
        "--timeout", str(timeout),
        "--max-turns", str(args.max_turns),
        "--max-idle-rounds", str(args.max_idle_rounds),
        "--max-noop-cycles", str(args.max_noop_cycles),
    ]
    persistent_providers = args.persistent_provider or []
    if args.cold_workers:
        persistent_providers = []
        runner_argv.append("--no-persistent-workers")
    for provider in persistent_providers:
        runner_argv += ["--persistent-provider", provider]
    prompt_cache_providers = args.prompt_cache_provider or []
    for provider in prompt_cache_providers:
        runner_argv += ["--prompt-cache-provider", provider]
    if args.no_prompt_hydration:
        runner_argv.append("--no-prompt-hydration")
    if args.token_efficient:
        runner_argv.append("--token-efficient")
    if args.no_mechanical_precommit:
        runner_argv.append("--no-mechanical-precommit")
    if args.no_persistent_workers:
        runner_argv.append("--no-persistent-workers")
    if args.no_prompt_cache:
        runner_argv.append("--no-prompt-cache")
    if args.fresh_sessions:
        runner_argv.append("--fresh-sessions")
    if args.herdr:
        # This is the sole CLI-to-runner boolean handoff.  The runner consumes
        # this exact flag when it adds mirror panes; no pane work belongs here.
        runner_argv.append("--herdr")
    if args.inherit_env:
        runner_argv.append("--inherit-env")
    if args.dry_run:
        runner_argv.append("--dry-run")
    if herdr_adapter is None:
        code = coop_autonomous.main(runner_argv)
    else:
        code = coop_autonomous.main(
            runner_argv,
            herdr_adapter=herdr_adapter,
        )
    if code:
        raise SystemExit(code)

def cmd_receipt(conn, args):
    actor, session_id = coopdb.resolve_actor(
        conn, os.environ.get("COOP_SESSION_ID"), _env_agent())
    receipt_id = coopdb.submit_receipt(
        conn, claim_id=args.claim, session_id=session_id, actor=actor,
        path=args.path, summary=args.summary, proof=args.proof,
        proof_refs=args.proof_ref)
    print(json.dumps({"receipt_id": receipt_id}) if args.json
          else f"receipt {receipt_id}")

def cmd_review(conn, args):
    if args.review_cmd == "request":
        actor, session_id = coopdb.resolve_actor(
            conn, os.environ.get("COOP_SESSION_ID"), _env_agent())
        review_id = coopdb.request_review(
            conn, claim_id=args.claim, session_id=session_id, actor=actor,
            reviewer=args.reviewer)
        target = f" -> {args.reviewer}" if args.reviewer else " (unnamed)"
        print(json.dumps({"review_id": review_id}) if args.json
              else f"review {review_id}{target} (item enters review)")
    elif args.review_cmd == "submit":
        actor, session_id = coopdb.resolve_actor(
            conn, os.environ.get("COOP_SESSION_ID"), _env_agent())
        result = coopdb.submit_verdict(
            conn, claim_id=args.claim, session_id=session_id, actor=actor,
            verdict=args.verdict, body=args.body)
        print(json.dumps(result) if args.json
              else f"review {result['review_id']} {result['verdict']} "
                   f"(item {result['item_id']})")
    else:  # claim
        if args.reclaim and not args.reason:
            raise coopdb.InvalidTransition(
                "--reclaim requires --reason <text>",
                reason_code="input_invalid",
                evidence={"constraint": "valid_cli_arguments_required"},
            )
        if args.reason and not args.reclaim:
            raise coopdb.InvalidTransition(
                "--reason only applies with --reclaim",
                reason_code="input_invalid",
                evidence={"constraint": "valid_cli_arguments_required"},
            )
        session_id = os.environ.get("COOP_SESSION_ID")
        claim, packet = coopdb.claim_review(
            conn, review_id=args.id, session_id=session_id,
            intent=args.intent,
            reclaim_reason=args.reason if args.reclaim else None,
            lease_seconds=args.lease_seconds)
        if args.json:
            print(json.dumps({**claim, "packet": packet}))
        else:
            print(f"claim {claim['claim_id']} review {claim['review_id']} "
                  f"lane {claim['lane']} "
                  f"lease-expires {claim['lease_expires_at']} "
                  f"({len(packet['decisions'])} decision"
                  f"{'' if len(packet['decisions']) == 1 else 's'} "
                  "in packet)")

def cmd_decision(conn, args):
    actor, session_id = coopdb.resolve_actor(
        conn, os.environ.get("COOP_SESSION_ID"), _env_agent())
    decision_id = coopdb.record_decision(
        conn, claim_id=args.claim, session_id=session_id, actor=actor,
        text=args.text, rationale=args.rationale)
    print(json.dumps({"decision_id": decision_id}) if args.json
          else f"decision {decision_id} recorded (append-only)")

def cmd_handoff(conn, args):
    actor, session_id = coopdb.resolve_actor(
        conn, os.environ.get("COOP_SESSION_ID"), _env_agent())
    if args.handoff_cmd == "create":
        result = coopdb.create_handoff(
            conn, claim_id=args.claim, session_id=session_id, actor=actor,
            to_agent=args.to, reason=args.reason, summary=args.summary,
            completed=args.completed, remaining=args.remaining,
            risks=args.risks, next_action=args.next_action,
            proof_refs=args.proof_refs)
        print(json.dumps(result) if args.json
              else f"handoff {result['handoff_id']} -> {args.to} "
                   f"(item {result['item_id']} frozen pending response)")
        if not result.get("target_session_live", True):
            print(f"warning: {args.to} has no running session; the handoff "
                  "waits until one binds", file=sys.stderr)
    elif args.handoff_cmd == "accept":
        result = coopdb.accept_handoff(
            conn, handoff_id=args.id, session_id=session_id, actor=actor,
            intent=args.intent, lease_seconds=args.lease_seconds)
        print(json.dumps(result) if args.json
              else f"handoff {result['handoff_id']} accepted — claim "
                   f"{result['claim_id']} lane {result['lane']} "
                   f"lease-expires {result['lease_expires_at']}")
    else:  # decline
        result = coopdb.decline_handoff(
            conn, handoff_id=args.id, session_id=session_id, actor=actor,
            reason=args.reason)
        print(json.dumps(result) if args.json
              else f"handoff {result['handoff_id']} declined — item "
                   f"{result['item_id']} back to {result['resume_owner']} "
                   f"(grace until {result['grace_expires_at']})")


def cmd_huddle(conn, args):
    if args.huddle_cmd == "show":
        result = coopdb.huddle_show(conn, args.id)
    else:
        actor, session_id = coopdb.resolve_actor(
            conn, os.environ.get("COOP_SESSION_ID"), _env_agent())
        if args.huddle_cmd == "open":
            result = coopdb.open_contract_huddle(
                conn, claim_id=args.claim, session_id=session_id, actor=actor)
        elif args.huddle_cmd == "open-plan":
            result = coopdb.open_plan_huddle(
                conn, claim_id=args.claim, session_id=session_id, actor=actor,
                proposal_ref=getattr(args, "proposal_ref", None))
        elif args.huddle_cmd == "post":
            result = coopdb.post_huddle(
                conn, huddle_id=args.id, session_id=session_id, actor=actor,
                stance=args.stance, body=args.body)
        else:
            result = coopdb.close_huddle(
                conn, huddle_id=args.id, session_id=session_id, actor=actor,
                outcome=args.outcome, summary=args.summary)
    if args.json:
        print(json.dumps(result))
    elif args.huddle_cmd == "show":
        print(f"huddle {result['huddle_id']} [{result['status']}] "
              f"item {result['item_id']} · {len(result['posts'])} post(s)")
        for post in result["posts"]:
            print(f"  r{post['round']} {post['agent']} [{post['stance']}]: "
                  f"{post['body']}")
    else:
        print(f"huddle {result['huddle_id']} {args.huddle_cmd}")

def cmd_needs_input(conn, args):
    session_id = os.environ.get("COOP_SESSION_ID")
    groups = list(args.question or ())
    if getattr(args, "needs_input_cmd", None) == "batch":
        if any(len(group) != 2 for group in groups):
            raise coopdb.InvalidTransition(
                "batch --question requires <agent> <exact-text>",
                reason_code="input_invalid",
                evidence={"constraint": "question_pair_required"},
            )
        question_ids = coopdb.needs_input_batch(
            conn,
            claim_id=args.claim,
            session_id=session_id,
            questions=[tuple(group) for group in groups],
        )
        print(json.dumps({"question_ids": question_ids}))
        return
    if args.to is None or len(groups) != 1 or len(groups[0]) != 1:
        raise coopdb.InvalidTransition(
            "needs-input requires --to <agent> and one --question <text>",
            reason_code="input_invalid",
            evidence={"constraint": "valid_cli_arguments_required"},
        )
    question = groups[0][0]
    qid = coopdb.needs_input(
        conn, claim_id=args.claim, session_id=session_id,
        to_agent=args.to, question=question)
    print(json.dumps({"question_id": qid}) if args.json
          else f"question {qid} -> {args.to} (item enters needs_input)")

def cmd_question(conn, args):
    session_id = os.environ.get("COOP_SESSION_ID")
    if args.question_cmd == "claim":
        if args.reclaim and not args.reason:
            raise coopdb.InvalidTransition(
                "--reclaim requires --reason <text>",
                reason_code="input_invalid",
                evidence={"constraint": "valid_cli_arguments_required"},
            )
        if args.reason and not args.reclaim:
            raise coopdb.InvalidTransition(
                "--reason only applies with --reclaim",
                reason_code="input_invalid",
                evidence={"constraint": "valid_cli_arguments_required"},
            )
        result = coopdb.claim_question(
            conn, question_id=args.id, session_id=session_id,
            intent=args.intent,
            reclaim_reason=args.reason if args.reclaim else None,
            lease_seconds=args.lease_seconds)
        print(json.dumps(result) if args.json
              else f"claim {result['claim_id']} lane {result['lane']} "
                   f"lease-expires {result['lease_expires_at']}")
    else:  # answer
        coopdb.answer_question(
            conn, claim_id=args.claim, session_id=session_id,
            answer=args.answer)
        print(json.dumps({"answered": True}) if args.json else "answered")

def cmd_checkpoint(conn, args):
    actor, session_id = coopdb.resolve_actor(
        conn, os.environ.get("COOP_SESSION_ID"), _env_agent())
    result = coopdb.checkpoint(
        conn, ctype=args.type, claim_id=args.claim, actor=actor,
        session_id=session_id, note=args.note)
    if args.json:
        print(json.dumps(result))
    else:
        packet = result["packet"]
        print(f"checkpoint {args.type} recorded for claim {args.claim} "
              f"(item {packet['item_id']} [{packet['status']}]); "
              f"{len(result['inbox'])} inbox entr"
              f"{'y' if len(result['inbox']) == 1 else 'ies'}")

def cmd_queue(conn, args):
    rows = coopdb.queue(
        conn, for_agent=args.for_agent, item_id=_env_item_id())
    if args.json:
        print(json.dumps(rows))
        return
    for row in rows:
        if row["kind"] == "review":
            designated = row["designated_reviewer"] or "any"
            print(f"review #{row['review_id']}\titem {row['item_id']} "
                  f"{row['title']}\tdesignated={designated}")
        elif row["kind"] == "handoff":
            print(f"handoff #{row['handoff_id']}\titem {row['item_id']} "
                  f"{row['title']}\tfrom {row['from_agent']}")
        else:
            addressed = row["next_actor"] or row["owner"] or "-"
            print(f"#{row['item_id']}\t{row['title']}\t-> {addressed}")


def _error_item_id(error):
    raw = error.evidence.get("item_id")
    if raw is None:
        raw = os.environ.get("COOP_ITEM_ID")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _enrich_error(conn, error):
    if conn is None:
        return error
    session_id = os.environ.get("COOP_SESSION_ID") or None
    if session_id is None:
        return error
    try:
        agent, _provider = coopdb.resolve_actor(
            conn,
            session_id,
            _env_agent(),
        )
        raw_lease = os.environ.get("COOP_ACTION_LEASE_SECONDS")
        lease = float(raw_lease) if raw_lease else None
        actions = coopdb.rejection_actions(
            conn,
            agent,
            item_id=_error_item_id(error),
            lease_seconds=lease,
        )
    except Exception:
        actions = []
    return error.attach_legal_next_actions(actions)


def _render_error(error, *, as_json, stream):
    if as_json:
        print(json.dumps({"error": error.as_dict()}), file=stream)
        return
    print(f"error: {error.type}: {error}", file=stream)
    print(f"reason: {error.reason_code}", file=stream)
    print(
        "evidence: "
        + json.dumps(
            dict(error.as_dict()["evidence"]),
            sort_keys=True,
            separators=(",", ":"),
        ),
        file=stream,
    )
    print(
        "legal-next-actions: "
        + json.dumps(
            error.legal_next_actions,
            sort_keys=True,
            separators=(",", ":"),
        ),
        file=stream,
    )


def cmd_status(conn, args):
    session_id = os.environ.get("COOP_SESSION_ID") or None
    if session_id:
        agent, _ = coopdb.resolve_actor(conn, session_id, _env_agent())
    else:
        agent = getattr(args, "as_agent", None) or _env_agent() or "human"
    raw_lease = os.environ.get("COOP_ACTION_LEASE_SECONDS")
    try:
        action_lease = float(raw_lease) if raw_lease else None
    except ValueError:
        action_lease = None
    data = coopdb.status(
        conn, agent, session_id=session_id,
        action_lease_seconds=action_lease, item_id=_env_item_id())
    if args.json:
        print(json.dumps(data))
        return
    head = f"agent {agent}"
    if data["agent"]["provider"]:
        head += f" · provider {data['agent']['provider']}"
    if session_id:
        head += f" · session {session_id}"
    action = data["next_action"]
    lines = [head, f"next: {action['kind']}"]
    if action["command"]:
        lines.append("command: " + json.dumps(action["command"]))
    if action["required_inputs"]:
        lines.append("judgment: " + ", ".join(action["required_inputs"]))
    for warning in data["warnings"]:
        lines.append(f"! {warning}")
    if data["claims"]:
        lines.append("claims: " + ", ".join(
            f"#{c['claim_id']} item {c['item_id']} "
            f"(lease {c['lease_expires_at']})" for c in data["claims"]))
    if data["unread"]:
        lines.append("unread: " + ", ".join(
            f"{k}={v}" for k, v in sorted(data["unread"].items())))
    for grace in data["resume_grace"]:
        lines.append(f"resume item #{grace['item_id']} "
                     f"before {grace['expires_at']}")
    for entry in data["stale"]:
        flag = "reclaimable" if entry["reclaimable"] else "wedged"
        lines.append(f"stale: claim {entry['claim_id']} "
                     f"item {entry['item_id']} ({flag})")
    for item in data["owned_items"]:
        labels = f" [{','.join(item['labels'])}]" if item["labels"] else ""
        lines.append(f"item #{item['item_id']} ({item['status']}) "
                     f"{item['title']}{labels}")
    print("\n".join(lines))

def cmd_agents(conn, args):
    color = sys.stdout.isatty()
    for r in coopdb.list_agents(conn):
        name = f"{_c(r['name'])}{r['name']}{_RST}" if color else r["name"]
        print(f"  {name}  provider={r['provider'] or '-'}  "
              f"since {r['registered_at']}")

def cmd_board(conn, args):
    """Human message board — recent non-binding messages."""
    color = sys.stdout.isatty()
    width = shutil.get_terminal_size((100, 24)).columns
    limit = args.limit if args.limit is not None else 40
    messages = _message_rows(conn, limit=limit)
    if getattr(args, "json", False):
        print(json.dumps(messages))
        return
    print(coop_ui.render_message_board(
        messages, color=color, limit=limit, width=width))

def cmd_tasks(conn, args):
    """Human task list — status, owner, next actor, live claim."""
    color = sys.stdout.isatty()
    width = shutil.get_terminal_size((100, 24)).columns
    tasks = _task_rows(conn)
    if args.status:
        tasks = [t for t in tasks if t["status"] == args.status]
    if getattr(args, "json", False):
        print(json.dumps(tasks))
        return
    print(coop_ui.render_task_board(tasks, color=color, width=width))

def cmd_goal(conn, args):
    """`coop "<goal>"`: the dashboard's TASKS Enter from the shell.

    A write command, so main has already provisioned a missing board (with
    the file list on stderr) and applied the workspace guards. Creates the
    draft item as the human and launches `coop start --item N` detached.
    One line on stdout; `--json` prints ``{"item_id", "run", "launched"}``.
    Exit 1 when the item was created but the launch failed.
    """
    conn.close()
    db = _db(args)
    item_id, run, notice = coop_monitor.create_goal(db, args.goal)
    skills_notice = _install_global_skills(args)
    if skills_notice:
        print(skills_notice, file=sys.stderr)
    run_id = pathlib.Path(run.log_path).stem if run is not None else None
    if getattr(args, "json", False):
        print(json.dumps({"item_id": item_id, "run": run_id,
                          "launched": run is not None}))
    else:
        print(f"{notice} · watch with: coop")
    if run is None:
        raise SystemExit(1)


def _dashboard_board(args):
    """The board the dashboard opens, created when the folder has none.

    Returns ``(db_path, notice)``. Only the cwd-default resolution creates:
    an explicit `--db`/`COOP_DB`/`COOP_DEFAULT_DB` target that is absent is
    an error, never a creation. The workspace guards run before any write
    (home, above home, a filesystem root, the temp directory itself, an
    adapter path that escapes the workspace); creation then follows the
    switcher's create-on-open path and returns its notice.
    """
    db, source = _db_meta(args)
    if pathlib.Path(db).expanduser().is_file():
        return db, ""
    if source != "cwd_default":
        raise coopdb.BoardMissing(
            f"no board at {db} (run `coop init --workspace <dir>` to create "
            f"one)", evidence={"constraint": "board_required"})
    workspace = coopdb.board_workspace(pathlib.Path(db).expanduser())
    coop_adapters.refuse_unsafe_workspace(
        workspace, allow_temp_root=False,
        hint="cd into a project directory or run "
             "`coop init --workspace <dir>`")
    coop_adapters.validate_workspace_adapter_paths(workspace)
    conn, notice = coop_monitor.open_or_create_board(db)
    if conn is None:
        raise coopdb.BoardMissing(
            notice, evidence={"constraint": "board_required"})
    conn.close()
    return db, notice


def cmd_monitor(conn, args):
    # `monitor` owns its board lifecycle (main passes no connection): the
    # board may not exist yet, and a missing one is created here rather
    # than reported, because opening the dashboard is the first thing a new
    # user does. Bare `coop` routes here too (`_default_argv`).
    if conn is not None:
        conn.close()
    db, notice = _dashboard_board(args)
    # Always pin the board the operator opened into the switcher registry —
    # including explicit --db/COOP_DB. Scripts using --db for agents/status
    # still skip record_board in main(); creation (`init`) and dashboard
    # open are the two pin signals.
    coopdb.record_board(db)
    skills_notice = _install_global_skills(args)
    notice = " · ".join(text for text in (notice, skills_notice) if text)
    # Degradation ladder: interactive dashboard → plain in-place loop.
    # run_dashboard returns False when VT/alt-screen is unavailable.
    if coop_monitor.dashboard_supported(sys.stdin, sys.stdout) \
            and coop_monitor.run_dashboard(
                db, interval=args.interval, notice=notice):
        return
    monitor(db, interval=args.interval, notice=notice)

def _sub_json(parser):
    # --json lives on the read commands themselves; SUPPRESS
    # keeps a root-level `coop --json <cmd>` value from being clobbered by
    # the subparser default.
    parser.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS,
                        help="machine-readable output")


def package_version() -> str:
    """Installed `agent-coop` version; pyproject fallback for a checkout."""
    try:
        from importlib import metadata
        return metadata.version("agent-coop")
    except Exception:
        pass
    # Checkout fallback (no tomllib on 3.10): read the version line only.
    try:
        pyproject = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
        match = re.search(
            r'^version\s*=\s*"([^"]+)"',
            pyproject.read_text(encoding="utf-8"),
            re.M,
        )
        if match:
            return match.group(1)
    except Exception:
        pass
    return "0+unknown"

# Root options that take a value; `_split_goal` must step over the value.
_ROOT_VALUE_OPTIONS = ("--db", "--as")


GOAL_HINT = 'to run a goal, quote it: coop "…"  (or: coop -- …)'


def _subcommand_names(parser):
    names = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            names.update(action.choices)
    return names


def _root_split(argv):
    """``(root_options, rest)``: ``rest`` starts at the first positional or
    at `--`. Root options that take a value keep their value; an unknown
    option stays in ``root_options`` for argparse to reject."""
    prefix = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--" or not token.startswith("-"):
            return prefix, list(argv[index:])
        if token in _ROOT_VALUE_OPTIONS:
            prefix.extend(argv[index:index + 2])
            index += 2
        else:
            prefix.append(token)
            index += 1
    return prefix, []


def _split_goal(argv, parser):
    """``(root_options, words)`` when argv carries a goal; ``None`` otherwise.

    A goal is recognised in exactly two shapes, so a typo or a retired
    command can never launch a run:

    - one positional element that contains whitespace — the user quoted a
      sentence: `coop "add a unit test for greet()"`. A subcommand name is
      never a goal (`coop status` stays the read command), but the quoted
      form may begin with one (`coop "status the build"`);
    - the `--` form: everything after `--` is the goal, quoted or not
      (`coop -- add a unit test`).

    A single word (`coop add`) or several unquoted words (`coop assign 1
    claude`) are left to argparse, which rejects them (exit 2)."""
    prefix, rest = _root_split(argv)
    if not rest:
        return None
    if rest[0] == "--":
        words = rest[1:]
        return (prefix, words) if words else None
    if rest[0] in _subcommand_names(parser):
        return None
    if len(rest) == 1 and any(char.isspace() for char in rest[0]):
        return prefix, rest
    return None


def _unquoted_goal(argv, parser) -> bool:
    """True when argv reads like an unquoted goal: the first positional is
    neither a subcommand nor `--`. argparse will reject it; main adds the
    quoting hint after argparse's own message."""
    _, rest = _root_split(argv)
    return bool(rest) and rest[0] != "--" \
        and rest[0] not in _subcommand_names(parser)


def build_parser():
    p = CoopArgumentParser(
        prog="coop",
        description="Agent Co-op: a local board that Claude, Codex and "
                    "Grok work from.",
        epilog='bare `coop` opens the dashboard; `coop "<goal>"` (quoted, '
               'or `coop -- <goal words>`) creates a task and launches the '
               'agents on it; an unquoted goal is a usage error.')
    p.add_argument("--version", action="version",
                   version=f"coop {package_version()}")
    p.add_argument("--db"); p.add_argument("--as", dest="as_agent")
    p.add_argument("--json", action="store_true", help="machine-readable errors")
    p.add_argument("--no-global-skills", action="store_true",
                   help="do not install the /coop launch skill into "
                        "~/.claude, ~/.codex and ~/.grok "
                        "(also COOP_NO_GLOBAL_SKILLS=1)")
    # Not required: bare `coop` (root options allowed, no subcommand) is
    # the dashboard — `_default_argv` appends `monitor` when the parse
    # yields no subcommand.
    sub = p.add_subparsers(dest="cmd", required=False)
    i = sub.add_parser("init"); i.add_argument("--judge")
    i.add_argument("--workspace",
                   help="provision a board for this working directory at "
                        "<workspace>/.coop/board.db and hide it from git")
    i.set_defaults(fn=cmd_init)
    m = sub.add_parser("migrate"); m.add_argument("--backup")
    m.add_argument("--confirm-legacy-clients-stopped", action="store_true")
    m.set_defaults(fn=cmd_migrate)
    sm = sub.add_parser(
        "smoke",
        help="offline preflight: temp board, claim cycle, process tree, "
             "provider CLIs (no quota spend)")
    sm.add_argument("--offline", action="store_true",
                    help="skip provider CLI probes")
    sm.set_defaults(fn=cmd_smoke)
    hd = sub.add_parser(
        "herdr", help="optional Herdr observability adapter"
    )
    hds = hd.add_subparsers(dest="herdr_cmd", required=True)
    hdm = hds.add_parser(
        "mirror", help="render one provider's read-only run trace"
    )
    hdm.add_argument("run_id", metavar="run")
    hdm.add_argument("provider", choices=("claude", "codex", "grok"))
    hdm.add_argument(
        "--max-idle-polls",
        type=int,
        default=None,
        help="optional idle poll bound (default: wait for run completion)",
    )
    hd.set_defaults(fn=cmd_herdr)
    env = sub.add_parser(
        "envelope",
        help="read-only communication-envelope projection over board events")
    envs = env.add_subparsers(dest="envelope_cmd", required=True)
    envl = envs.add_parser("list")
    envl.add_argument("--item", type=int)
    envl.add_argument("--since", type=int)
    envl.set_defaults(fn=cmd_envelope_list)
    envsh = envs.add_parser("show")
    envsh.add_argument("event_id", type=int)
    envsh.set_defaults(fn=cmd_envelope_show)
    s = sub.add_parser("say"); s.add_argument("body"); s.add_argument("--room", default="#general")
    s.add_argument("--to"); s.add_argument("--item", type=int); s.set_defaults(fn=cmd_say)
    ib = sub.add_parser("inbox")
    ib.add_argument("--peek", action="store_true",
                    help="read without consuming the cursor")
    _sub_json(ib)
    ib.set_defaults(fn=cmd_inbox)
    it = sub.add_parser("item"); its = it.add_subparsers(dest="item_cmd", required=True)
    itc = its.add_parser("create")
    for flag in ("--title", "--objective", "--scope", "--done-when",
                 "--output-contract", "--context", "--owner", "--next-actor"):
        itc.add_argument(flag)
    itc.add_argument("--allowed-action", action="append", dest="allowed_action")
    itc.add_argument("--stop-condition", action="append", dest="stop_condition")
    itc.add_argument("--contract", help="JSON file carrying contract fields")
    itc.add_argument(
        "--expand", action="store_true",
        help="draft the missing contract fields from the goal with a fast "
             "model at kickoff, so the item enters as a complete human "
             "contract (skips the acceptance huddle); fails the kickoff "
             "closed if drafting fails")
    itc.add_argument(
        "--expand-fallback-bare", action="store_true",
        help="on --expand failure, create the bare goal item anyway "
             "(restores the contract-fill + acceptance-huddle turns)")
    itc.add_argument(
        "--template",
        choices=tuple(sorted(coop_templates.CONTRACT_TEMPLATES)),
        help="start from a built-in complete contract template (zero model "
             "calls; skips the acceptance huddle); explicit flags and "
             "--contract still override")
    waiver = itc.add_mutually_exclusive_group()
    waiver.add_argument("--review-waiver", dest="review_waiver",
                        help="waive the default review requirement, "
                             "recording this reason")
    waiver.add_argument("--require-review", action="store_true",
                         help="require review before completion (the default)")
    waiver.add_argument("--review-quorum", type=int, choices=(1, 2),
                        help="explicit binding approvals required (1 or 2)")
    itd = its.add_parser("define",
                         help="agent-lane: fill a goal-task's empty contract "
                              "fields (title/objective stay the human's goal)")
    itd.add_argument("--claim", type=int, required=True)
    for flag in ("--scope", "--done-when", "--output-contract", "--context"):
        itd.add_argument(flag)
    itd.add_argument("--allowed-action", action="append", dest="allowed_action")
    itd.add_argument("--stop-condition", action="append", dest="stop_condition")
    itd.add_argument("--contract", help="JSON file carrying contract fields")
    _sub_json(itd)
    itf = its.add_parser(
        "refine", help="agent-lane: amend agent-authored fields after peer changes")
    itf.add_argument("--claim", type=int, required=True)
    for flag in ("--scope", "--done-when", "--output-contract", "--context"):
        itf.add_argument(flag)
    itf.add_argument("--allowed-action", action="append", dest="allowed_action")
    itf.add_argument("--stop-condition", action="append", dest="stop_condition")
    itf.add_argument("--contract", help="JSON file carrying fields to refine")
    _sub_json(itf)
    itr = its.add_parser("revise")
    itr.add_argument("id", type=int)
    itr.add_argument("--reason", required=True)
    for flag in ("--title", "--objective", "--scope", "--done-when",
                 "--output-contract", "--context"):
        itr.add_argument(flag)
    itr.add_argument("--allowed-action", action="append",
                     dest="allowed_action")
    itr.add_argument("--stop-condition", action="append",
                     dest="stop_condition")
    itr.add_argument("--contract", help="JSON file carrying contract fields")
    rwaiver = itr.add_mutually_exclusive_group()
    rwaiver.add_argument("--review-waiver", dest="review_waiver",
                         help="waive the review requirement, recording "
                              "this reason")
    rwaiver.add_argument("--require-review", action="store_true",
                         help="restore the default review requirement")
    rwaiver.add_argument("--review-quorum", type=int, choices=(1, 2),
                         help="set the explicit binding approvals required")
    itsh = its.add_parser("show"); itsh.add_argument("id", type=int)
    itsh.add_argument("--packet", action="store_true")
    itsh.add_argument("--history", action="store_true")
    _sub_json(itsh)
    itcl = its.add_parser("claim"); itcl.add_argument("id", type=int)
    itcl.add_argument("--intent", required=True)
    itcl.add_argument("--reclaim", action="store_true")
    itcl.add_argument("--reason")
    # A long lease keeps the claim alive across a long working turn; renewal
    # cannot revive an already-expired claim, so the lease must be set here.
    itcl.add_argument("--lease-seconds", type=float)
    itco = its.add_parser("complete")
    itco.add_argument("--claim", type=int, required=True)
    it.set_defaults(fn=cmd_item)
    cl = sub.add_parser("claim")
    cls_ = cl.add_subparsers(dest="claim_cmd", required=True)
    clr = cls_.add_parser("release")
    clr.add_argument("--claim", type=int, required=True)
    clr.add_argument("--reason", required=True)
    cl.set_defaults(fn=cmd_claim)
    ad = sub.add_parser("admin")
    ads = ad.add_subparsers(dest="admin_cmd", required=True)
    adr = ads.add_parser("release")
    adr.add_argument("claim_id", type=int)
    adr.add_argument("--reason", required=True)
    adr.add_argument("--confirm-process-stopped", action="store_true")
    ada = ads.add_parser("answer")
    ada.add_argument("question_id", type=int)
    ada.add_argument("--answer", required=True)
    ada.add_argument("--reason", required=True)
    ad.set_defaults(fn=cmd_admin)
    rcvr = sub.add_parser(
        "recover",
        help="agent recovery (peer wedge release; mid-run human audit)")
    rcvrs = rcvr.add_subparsers(dest="recover_cmd", required=True)
    rw = rcvrs.add_parser(
        "wedge",
        help="release a stale claim after abandon horizon (agent, not human)")
    rw.add_argument("claim_id", type=int)
    rw.add_argument("--reason", required=True)
    rw.add_argument("--abandon-after-seconds", type=float, default=None,
                    help="seconds without session last_seen before abandon "
                         f"(default {coopdb.DEFAULT_ABANDON_SECONDS})")
    _sub_json(rw)
    rha = rcvrs.add_parser(
        "human-audit",
        help="list human-lane mutations after a run-start timestamp")
    rha.add_argument("--since", required=True,
                     help="ISO timestamp (autonomous run start)")
    rha.add_argument("--until", default=None)
    _sub_json(rha)
    rcvr.set_defaults(fn=cmd_recover)
    se = sub.add_parser("session")
    ses = se.add_subparsers(dest="session_cmd", required=True)
    ser = ses.add_parser("run")
    ser.add_argument("--as", dest="provider", required=True)
    ser.add_argument("--name")
    ser.add_argument("--max-runtime-seconds", type=float)
    ser.add_argument("--shutdown-grace-seconds", type=float)
    ser.add_argument("--lease-seconds", type=float)
    ser.add_argument("--checkpoint-limit-seconds", type=float)
    ser.add_argument("--env-allowlist", action="append", dest="env_allowlist",
                     metavar="NAME",
                     help="opt-in: restrict the child environment to the "
                          "platform baseline plus these named parent "
                          "variables plus COOP_* (repeatable)")
    import argparse as _argparse
    ser.add_argument("command", nargs=_argparse.REMAINDER,
                     help="opaque harness command after --")
    sst = ses.add_parser("start", help="mint a session for THIS process "
                                       "(foreign-harness attach; bind before "
                                       "any other coop command)")
    sst.add_argument("--as", dest="provider", required=True)
    sst.add_argument("--name")
    sst.add_argument("--max-runtime-seconds", type=float)
    sst.add_argument("--export", action="store_true",
                     help="print only the shell export lines (for eval)")
    _sub_json(sst)
    sen = ses.add_parser("end", help="finish a session started for this "
                                     "process (reads $COOP_SESSION_ID)")
    sen.add_argument("--session")
    sre = ses.add_parser("renew", help="extend this session's active claims "
                                       "(external sessions have no auto-renew)")
    sre.add_argument("--session")
    se.set_defaults(fn=cmd_session)
    st = sub.add_parser("start", help="start the wake-driven autonomous Co-op")
    target = st.add_mutually_exclusive_group(required=True)
    target.add_argument("--item", type=_positive_item_id,
                        help="run exactly this item id")
    target.add_argument("--all", action="store_true", dest="all_items",
                        help="explicitly drain all active board items")
    st.add_argument("--agents", help="comma list, default claude,codex,grok")
    st.add_argument("--board")
    st.add_argument("--interval", type=float, default=3.0,
                    help="idle board poll seconds")
    st.add_argument("--timeout-seconds", type=float, default=600.0,
                    dest="timeout_seconds", help="per-provider turn seconds")
    st.add_argument("--max-turns", type=int, default=200, dest="max_turns")
    st.add_argument("--max-idle-rounds", type=int, default=40,
                    dest="max_idle_rounds")
    st.add_argument("--max-noop-cycles", type=int, default=3,
                    dest="max_noop_cycles")
    st.add_argument(
        "--persistent-provider",
        action="append",
        choices=("claude", "codex", "grok"),
        default=[],
        help="opt in providers to persistent worker reuse (repeatable) [default: all active]",
    )
    st.add_argument(
        "--cold-workers", action="store_true",
        help="disable persistent workers",
    )
    st.add_argument(
        "--prompt-cache-provider",
        action="append",
        choices=("claude",),
        default=[],
        help="enable Claude's prompt-cache reuse hint [default: enabled]",
    )
    st.add_argument(
        "--no-prompt-hydration", action="store_true",
        help="spawn turns with the bare content-free prefix instead of "
             "appending the spawn-time board snapshot",
    )
    st.add_argument(
        "--token-efficient", action="store_true",
        help="opt in to the bounded action grammar and prompt-level "
             "post-write exit contract",
    )
    st.add_argument(
        "--no-mechanical-precommit", action="store_true",
        help="do not let the runner pre-execute judgment-free claim "
             "commands before spawning the turn",
    )
    st.add_argument(
        "--no-persistent-workers", action="store_true",
        help="cold provider turns every time (debug mode; persistent "
             "workers are the default)",
    )
    st.add_argument(
        "--no-prompt-cache", action="store_true",
        help="disable the default Claude prompt-cache reuse hint",
    )
    st.add_argument(
        "--fresh-sessions", action="store_true",
        help="do not resume claude's provider conversation from the "
             "previous run on this board",
    )
    st.add_argument(
        "--herdr", action="store_true",
        help="opt in to Herdr mirror observability (requires a live session)",
    )
    st.add_argument(
        "--inherit-env", action="store_true",
        help="give provider children the full shell environment instead of "
             "the platform + provider + COOP_* allowlist (shell secrets "
             "become readable by every model turn)",
    )
    st.add_argument("--dry-run", action="store_true")
    st.set_defaults(fn=cmd_start)
    rc = sub.add_parser("receipt")
    rcs = rc.add_subparsers(dest="receipt_cmd", required=True)
    rcsu = rcs.add_parser("submit")
    rcsu.add_argument("--claim", type=int, required=True)
    rcsu.add_argument("--path", required=True)
    rcsu.add_argument("--summary", required=True)
    rcsu.add_argument("--proof", required=True)
    rcsu.add_argument("--proof-ref", action="append", dest="proof_ref",
                      help="typed reference: debate:<id> decision:<id> "
                           "event:<id> file:<absolute-path>")
    rc.set_defaults(fn=cmd_receipt)
    rv = sub.add_parser("review")
    rvs = rv.add_subparsers(dest="review_cmd", required=True)
    rvr = rvs.add_parser("request")
    rvr.add_argument("--claim", type=int, required=True)
    rvr.add_argument("--reviewer")
    rvc = rvs.add_parser("claim"); rvc.add_argument("id", type=int)
    rvc.add_argument("--intent", required=True)
    rvc.add_argument("--reclaim", action="store_true")
    rvc.add_argument("--reason")
    rvc.add_argument("--lease-seconds", type=float)
    rvsu = rvs.add_parser("submit")
    rvsu.add_argument("--claim", type=int, required=True)
    rvsu.add_argument("--verdict", choices=("approve", "changes"),
                      required=True)
    rvsu.add_argument("--body")
    rv.set_defaults(fn=cmd_review)
    dc = sub.add_parser("decision")
    dcs = dc.add_subparsers(dest="decision_cmd", required=True)
    dcr = dcs.add_parser("record")
    dcr.add_argument("--claim", type=int, required=True)
    dcr.add_argument("--text", required=True)
    dcr.add_argument("--rationale")
    dc.set_defaults(fn=cmd_decision)
    hf = sub.add_parser("handoff")
    hfs = hf.add_subparsers(dest="handoff_cmd", required=True)
    hfc = hfs.add_parser("create")
    hfc.add_argument("--claim", type=int, required=True)
    hfc.add_argument("--to", required=True)
    hfc.add_argument("--reason", required=True)
    hfc.add_argument("--summary", required=True)
    hfc.add_argument("--completed", required=True)
    hfc.add_argument("--remaining", required=True)
    hfc.add_argument("--risks", required=True)
    hfc.add_argument("--next-action", dest="next_action", required=True)
    hfc.add_argument("--proof-ref", dest="proof_refs", action="append")
    hfa = hfs.add_parser("accept")
    hfa.add_argument("--id", type=int, required=True)
    hfa.add_argument("--intent", required=True)
    hfa.add_argument("--lease-seconds", type=float)
    hfd = hfs.add_parser("decline")
    hfd.add_argument("--id", type=int, required=True)
    hfd.add_argument("--reason", required=True)
    hf.set_defaults(fn=cmd_handoff)
    hd = sub.add_parser("huddle", help="bounded peer deliberation")
    hds = hd.add_subparsers(dest="huddle_cmd", required=True)
    hdo = hds.add_parser("open", help="open contract-acceptance huddle")
    hdo.add_argument("--claim", type=int, required=True)
    hdop = hds.add_parser(
        "open-plan",
        help="open post-acceptance plan critique huddle (advisory; "
             "does not reopen contract finality)")
    hdop.add_argument("--claim", type=int, required=True)
    hdop.add_argument("--proposal-ref", dest="proposal_ref",
                      help="short ref to the plan under critique")
    hdp = hds.add_parser("post")
    hdp.add_argument("id", type=int)
    hdp.add_argument("--stance", choices=coopdb.HUDDLE_STANCES, required=True)
    hdp.add_argument("--body", required=True)
    hdc = hds.add_parser("close")
    hdc.add_argument("id", type=int)
    hdc.add_argument("--outcome", choices=coopdb.HUDDLE_OUTCOMES, required=True)
    hdc.add_argument("--summary", required=True)
    hdshow = hds.add_parser("show")
    hdshow.add_argument("id", type=int)
    for parser in (hdo, hdop, hdp, hdc, hdshow):
        _sub_json(parser)
    hd.set_defaults(fn=cmd_huddle)
    ni = sub.add_parser("needs-input")
    ni.add_argument("needs_input_cmd", nargs="?", choices=("batch",))
    ni.add_argument("--claim", type=int, required=True)
    ni.add_argument("--to")
    ni.add_argument("--question", action="append", nargs="+", required=True)
    ni.set_defaults(fn=cmd_needs_input)
    qn = sub.add_parser("question")
    qns = qn.add_subparsers(dest="question_cmd", required=True)
    qnc = qns.add_parser("claim"); qnc.add_argument("id", type=int)
    qnc.add_argument("--intent", required=True)
    qnc.add_argument("--reclaim", action="store_true")
    qnc.add_argument("--reason")
    qnc.add_argument("--lease-seconds", type=float)
    qna = qns.add_parser("answer")
    qna.add_argument("--claim", type=int, required=True)
    qna.add_argument("--answer", required=True)
    qn.set_defaults(fn=cmd_question)
    cp = sub.add_parser("checkpoint")
    cp.add_argument("type", choices=coopdb.CHECKPOINT_TYPES)
    cp.add_argument("--claim", type=int, required=True)
    cp.add_argument("--note")
    cp.set_defaults(fn=cmd_checkpoint)
    q = sub.add_parser("queue"); q.add_argument("--for", dest="for_agent")
    _sub_json(q)
    q.set_defaults(fn=cmd_queue)
    st = sub.add_parser("status")
    st.add_argument("--compact", action="store_true",
                    help="the human panel (default form)")
    _sub_json(st)
    st.set_defaults(fn=cmd_status)
    sub.add_parser("agents").set_defaults(fn=cmd_agents)
    bd = sub.add_parser("board", help="recent message board")
    bd.add_argument("--limit", type=int, default=40)
    _sub_json(bd)
    bd.set_defaults(fn=cmd_board)
    # `tasks` is the human task list; `task` is a friendly alias.
    for name in ("tasks", "task"):
        tk = sub.add_parser(name, help="task list with owners and claims")
        tk.add_argument("--status",
                        help="filter by item status (todo/working/…)")
        _sub_json(tk)
        tk.set_defaults(fn=cmd_tasks)
    mo = sub.add_parser("monitor", help="live dashboard (redraws in place)")
    mo.add_argument("--interval", type=float, default=2)
    mo.set_defaults(fn=cmd_monitor)
    return p

def _default_argv(argv, parser=None):
    """Bare `coop` opens the dashboard: `coop` is `coop monitor`, and
    `coop --db x.db` is `coop --db x.db monitor`. The decision comes from
    a parse, not from inspecting argv, so root options that take a value
    (`--db`, `--as`) cannot be mistaken for a subcommand. `--help` and
    `--version` exit inside that parse, unchanged. Terminal or not makes
    no difference: without a terminal the dashboard falls back to the
    plain redraw loop."""
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = parser or build_parser()
    if parser.parse_args(argv).cmd is None:
        return argv + ["monitor"]
    return argv

def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        # ENC: Windows consoles default to cp1252; block/dot glyphs need utf-8.
        # SHELL/CR: force LF so `for id in $(coop …)` in Git Bash does not
        # glue a trailing \r onto printed session/item IDs.
        try:
            stream.reconfigure(encoding="utf-8", newline="\n")
        except Exception:
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    coop_ui.enable_vt(sys.stdout)
    parser = build_parser()
    argv = sys.argv[1:] if argv is None else list(argv)
    goal = _split_goal(argv, parser)
    if goal is None:
        try:
            args = parser.parse_args(_default_argv(argv, parser))
        except SystemExit as exit_:
            # An unquoted goal (`coop add a test`, `coop add`) is a usage
            # error, never a launch; say how to write it.
            if exit_.code == 2 and _unquoted_goal(argv, parser):
                print(GOAL_HINT, file=sys.stderr)
            raise
    else:
        # `coop "<goal>"`: argparse sees only the root options; the words
        # never pass through it, so nothing in a goal reads as an option.
        root_options, words = goal
        args = parser.parse_args(root_options)
        args.cmd, args.fn, args.goal = "goal", cmd_goal, " ".join(words)
    conn = None
    try:
        _enforce_timing_bounds(args)  # before any database/process work
        if args.cmd in ("migrate", "session", "start", "smoke", "herdr",
                        "monitor"):
            # These own their database lifecycle (migrate is offline; the
            # supervisor and the start orchestrator each hold their own
            # connection across the whole run; the dashboard creates a
            # missing board itself, then opens it).
            args.fn(None, args)
            return
        db = _db(args)
        if args.cmd == "init":
            # A workspace-shaped target (explicit --workspace or the cwd
            # default) must not be home, above home, or a filesystem root:
            # every repository below it would discover that board. An
            # explicit --db names one file and is the caller's decision.
            _, db_source = _db_meta(args)
            if db_source in ("workspace", "cwd_default"):
                coop_adapters.refuse_unsafe_workspace(
                    coopdb.board_workspace(pathlib.Path(db).expanduser()),
                    hint="choose a project directory")
            # Refuse an adapter escape before sqlite creates any workspace
            # state. An explicit --db without --workspace installs no
            # adapters and therefore needs no adapter-path validation.
            if getattr(args, "workspace", None):
                coop_adapters.validate_workspace_adapter_paths(
                    args.workspace
                )
            # sqlite cannot create a board inside a directory that is not
            # there yet — this is the `.coop/` that `--workspace` names.
            pathlib.Path(db).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True)
        else:
            # Auto-provision: a write command run in a directory with no
            # resolvable board (cwd-default source, file absent) creates one
            # in place. A read command never creates anything: it reports
            # the missing board and stops.
            _, db_source = _db_meta(args)
            if db_source == "cwd_default" and not pathlib.Path(
                    db).expanduser().is_file():
                if _reads_only(args):
                    raise coopdb.BoardMissing(
                        "no board here (run `coop init --workspace .` "
                        "to create one)",
                        evidence={"constraint": "board_required"})
                _auto_provision(db)
        conn = coopdb.connect(db, require_current=args.cmd != "init")
        if not _db_is_explicit(args):
            # Human-flow boards (discovered or defaulted) feed the switcher
            # registry; explicit --db/COOP_DB (scripts, tests, supervised
            # sessions) never touch it.
            coopdb.record_board(db)
        args.fn(conn, args)
    except (coop_adapters.WorkspaceRefused, coopdb.BoardMissing) as error:
        # Bootstrap refusals happen before any board exists: one plain line
        # for a human; JSON callers keep the structured error shape.
        if getattr(args, "json", False):
            _render_error(error, as_json=True, stream=sys.stderr)
        else:
            print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
    except coopdb.CoopError as error:
        _render_error(
            _enrich_error(conn, error),
            as_json=getattr(args, "json", False),
            stream=sys.stderr,
        )
        raise SystemExit(1)
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass

if __name__ == "__main__":
    main()
