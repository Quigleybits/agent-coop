#!/usr/bin/env python3
"""Transcript REPL helpers for Agent Co-op.

The dashboard (`coop_monitor`) imports `classify_line` and `dispatch_line`.
`run_session` is the plain-terminal transcript loop. The module has no entry
point: the caller supplies the board path, so nothing here derives a board
location from the package's own install directory."""

from __future__ import annotations

import argparse
import contextlib
import os
import shlex
import shutil
import sys
from typing import TextIO

from agent_coop import cli as coopcli
from agent_coop import coop_ui
from agent_coop import coopdb


BUILTINS = frozenset({"help", "clear", "quit", "exit"})
PREFERRED_AGENTS = {"claude": 0, "codex": 1, "grok": 2}


def known_commands() -> frozenset[str]:
    """Read root command names from the existing argparse parser."""
    parser = coopcli.build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return frozenset(action.choices)
    return frozenset()


def classify_line(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return "empty"
    first = stripped.split(maxsplit=1)[0].lower()
    if first in BUILTINS:
        return "builtin"
    if first in known_commands():
        return "command"
    return "prompt"


def _split_command(line: str) -> list[str]:
    tokens = shlex.split(line, posix=os.name != "nt")
    if os.name == "nt":
        tokens = [
            token[1:-1]
            if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}
            else token
            for token in tokens
        ]
    if tokens:
        tokens[0] = tokens[0].lower()
    return tokens


def _root_help() -> str:
    commands = "  ".join(sorted(known_commands()))
    return "\n".join(
        (
            "Commands",
            f"  {commands}",
            "  help  clear  quit  exit",
            "",
            "Anything else posts the line to the board as a message from the human operator.",
        )
    )


def dispatch_line(
    line: str,
    db_path: str,
    agent: str,
    out: TextIO,
    err: TextIO,
) -> bool:
    """Execute one REPL line; return False only when the session should end."""
    kind = classify_line(line)
    stripped = line.strip()
    if kind == "empty":
        return True

    if kind == "builtin":
        command = stripped.lower()
        if command in {"quit", "exit"}:
            return False
        if command == "help":
            print(_root_help(), file=out)
            return True
        if command == "clear":
            width = shutil.get_terminal_size((100, 24)).columns
            print("\033[2J\033[H" + coop_ui.splash_text(width, out.isatty()), file=out)
            return True

    if kind == "prompt":
        # Version 4: bare free text is a non-binding broadcast from the
        # reserved human actor (null session, no recipient entries).
        # Creating a task requires the explicit complete-contract command.
        conn = coopdb.connect(db_path, require_current=True)
        try:
            message_id = coopdb.say(conn, session_id=None, body=stripped)
        finally:
            conn.close()
        print(f"posted message #{message_id}: {stripped}", file=out)
        return True

    try:
        tokens = _split_command(stripped)
    except ValueError as exc:
        print(f"error: {exc}", file=err)
        return True

    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            coopcli.main(["--db", db_path, "--as", agent, *tokens])
        except SystemExit as exc:
            if isinstance(exc.code, str):
                print(exc.code, file=err)
        except Exception as exc:
            print(f"error: {exc}", file=err)
    return True


def _activity_rows(conn) -> dict[str, coop_ui.AgentActivity]:
    activities: dict[str, coop_ui.AgentActivity] = {}

    review_rows = conn.execute(
        """
        SELECT r.reviewer AS agent, i.id AS item_id, i.title
        FROM reviews r
        JOIN items i ON i.id = r.item_id
        WHERE r.status='requested' AND r.reviewer IS NOT NULL
        ORDER BY r.created_at DESC, r.id DESC
        """
    ).fetchall()
    for row in review_rows:
        activities.setdefault(
            row["agent"],
            coop_ui.AgentActivity(row["agent"], "reviewing", row["item_id"], row["title"]),
        )

    owner_rows = conn.execute(
        """
        SELECT a.agent, i.id AS item_id, i.title
        FROM assignments a
        JOIN items i ON i.id = a.item_id
        WHERE a.role='owner'
          AND a.state='active'
          AND i.status IN ('working','review')
        ORDER BY i.updated_at DESC, i.id DESC
        """
    ).fetchall()
    for row in owner_rows:
        activities.setdefault(
            row["agent"],
            coop_ui.AgentActivity(row["agent"], "working", row["item_id"], row["title"]),
        )
    return activities


def _agent_sort_key(activity: coop_ui.AgentActivity) -> tuple[int, str]:
    return (PREFERRED_AGENTS.get(activity.name, 10), activity.name)


def load_snapshot(db_path: str, human: str = "human") -> coop_ui.BoardSnapshot:
    conn = coopdb.connect(db_path, require_current=True)
    try:
        activities = _activity_rows(conn)
        rendered = []
        for name in PREFERRED_AGENTS:
            activity = activities.get(name)
            if activity is None:
                activity = coop_ui.AgentActivity(name, "idle")
            rendered.append(activity)
        rendered.sort(key=_agent_sort_key)
        return coop_ui.BoardSnapshot(tuple(rendered))
    finally:
        conn.close()


def run_session(
    db_path: str,
    agent: str = "human",
    *,
    reader=None,
    stdin: TextIO = sys.stdin,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    color: bool | None = None,
) -> int:
    """Run a persistent scrolling transcript until the operator exits."""
    if color is None:
        color = out.isatty()
    width = shutil.get_terminal_size((100, 24)).columns

    try:
        load_snapshot(db_path, human=agent)
    except Exception as exc:
        print(f"error: unable to open coop board {db_path}: {exc}", file=err)
        return 1

    print(coop_ui.splash_text(width=width, color=color), file=out)

    def footer_provider(frame: int, current_width: int) -> str:
        try:
            snapshot = load_snapshot(db_path, human=agent)
            return coop_ui.render_footer(
                snapshot, frame=frame, color=color, width=current_width
            )
        except Exception as exc:
            return f"activity unavailable: {exc}"[:current_width]

    prompt = coop_ui.prompt_text(color=color)
    interactive = reader is None and coop_ui.live_footer_supported(stdin, out)
    try:
        while True:
            try:
                if reader is not None:
                    line = reader(prompt, footer_provider)
                    print(prompt + line, file=out)
                else:
                    line = coop_ui.read_command(
                        prompt,
                        footer_provider,
                        stdin=stdin,
                        stdout=out,
                    )
                    if not stdin.isatty():
                        print(line, file=out)
            except (EOFError, KeyboardInterrupt):
                print("", file=out)
                break

            if not dispatch_line(line, db_path, agent, out, err):
                break
    finally:
        if interactive:
            coop_ui.restore_terminal(out)
        elif color:
            out.write(coop_ui.RESET)
            out.flush()
    return 0
