"""Interactive operator dashboard v3 — sibling to coop_repl.

Three layers: repo (board discovery + ^o switcher) → tasks (left pane,
highlight selection) → conversation (right pane, auto-loaded). The REPL
bottom is merged in: a `>` input line (human-lane `say`, task-scoped when a
task is selected; command words dispatch through the REPL machinery) and the
per-agent status strip. Rendering is coop_ui's; every read is coopdb's; the
only write is the audited human `say`. coop.py imports this module — the
ladder lives in cmd_monitor; coop_repl is imported lazily to keep module
initialization one-way.
"""

from __future__ import annotations

import io
import datetime
import os
import pathlib
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import TextIO

from agent_coop import coop_ui
from agent_coop import coopdb
from agent_coop import coop_runner_status
from agent_coop import coop_turn_trace

_PREFIX_KEYS = {
    "H": "up", "P": "down", "K": "left", "M": "right",
    "I": "pgup", "Q": "pgdn", "G": "home", "O": "end",
}
_CSI_FINAL = {"A": "up", "B": "down", "C": "right", "D": "left"}
_CSI_TILDE = {"5": "pgup", "6": "pgdn", "1": "home", "7": "home",
              "4": "end", "8": "end"}
_PAGE = 10
_WHEEL_STEP = 3
_TASK_HSCROLL_STEP = 4


def dashboard_supported(stdin: TextIO, stdout: TextIO) -> bool:
    """Full dashboard needs Windows key input plus a real TTY both ways."""
    return os.name == "nt" and stdin.isatty() and stdout.isatty()


def _decode_csi(keys):
    """Decode one CSI sequence after ESC[ was consumed."""
    params = ""
    while keys.kbhit():
        char = keys.getwch()
        if char == "<":
            # SGR mouse: btn;col;row followed by M (press) or m (release).
            seq = ""
            while keys.kbhit():
                char = keys.getwch()
                if char in ("M", "m"):
                    parts = seq.split(";")
                    if char == "M" and len(parts) == 3:
                        try:
                            button, col = int(parts[0]), int(parts[1])
                        except ValueError:
                            return None
                        if button == 64:
                            return ("wheel_up", col)
                        if button == 65:
                            return ("wheel_down", col)
                    return None
                seq += char
            return None
        if char == "~":
            return _CSI_TILDE.get(params.split(";")[0])
        if char.isalpha():
            return _CSI_FINAL.get(char)
        params += char
    return None


def decode_key(keys):
    """Decode one keypress/mouse event from a _WindowsKeys-shaped object.

    Returns a token ("up", "enter", "tab", …), ("char", c) for printable
    input, ("wheel_up"|"wheel_down", column) for SGR mouse wheel, or None.
    A line break with more queued input is part of a multiline paste, not a
    submit key; it becomes ``paste_break`` and is folded into the buffer.
    """
    if not keys.kbhit():
        return None
    char = keys.getwch()
    if char in ("\x00", "\xe0"):
        # A special-key prefix is ALWAYS followed by its code byte in the same
        # key event — read it directly. The old kbhit() bail dropped the prefix
        # on a timing gap and leaked the code byte ('P'/'H'/…) into the input
        # buffer, which also broke switcher arrows and wheel-as-arrow scroll.
        return _PREFIX_KEYS.get(keys.getwch())
    if char == "\x1b":
        if keys.kbhit():
            follower = keys.getwch()
            if follower == "[":
                return _decode_csi(keys)
            return None
        return "esc"
    if char in ("\r", "\n") and keys.kbhit():
        return "paste_break"
    if char in ("\r", "\n"):
        return "enter"
    if char == "\t":
        return "tab"
    if char in ("\x08", "\x7f"):
        # VT input mode (enabled for mouse reporting) delivers Backspace as
        # DEL (0x7f), not BS (0x08) — accept both so editing always works.
        return "backspace"
    if char == "\x0f":
        # Ctrl+O opens the board switcher. Ctrl+B is the tmux and Herdr
        # prefix, so the dashboard never binds it.
        return "switch"
    if char == "\x03":
        return "quit"
    if char.isprintable():
        return ("char", char)
    return None


def decode_available_keys(keys) -> list:
    """Drain every key event already queued for this dashboard poll."""
    events = []
    while keys.kbhit():
        event = decode_key(keys)
        if event is not None:
            events.append(event)
    return events


@dataclass
class ViewState:
    """Everything the dashboard remembers between frames."""
    cursor: int | None = None
    focus: str = "tasks"
    buffer: str = ""
    # Caret index into `buffer` (0 = before first char, len = after last).
    # ←/→ move this when the input is non-empty; empty buffer keeps the
    # existing task-title hscroll on ←/→ under TASKS focus.
    buffer_pos: int = 0
    notice: str = ""
    convo_scroll: int = 0
    # Ceiling from last paint (top of history). ↑/PgUp/Home/wheel-up clamp
    # here so dead-key overscroll never accumulates past the top.
    convo_max_scroll: int = 0
    # Horizontal offset (chars) for the *selected* task title line — long
    # descriptions stay one line; ←/→ under TASKS focus pan the highlight
    # only while the input buffer is empty (see apply_key).
    task_hscroll: int = 0
    mode: str = "normal"
    board_cursor: int = 0
    split_col: int | None = None
    blinking: bool = False       # any agent currently holds a live claim
    runner_status_path: str | None = None
    runner_log_path: str | None = None


def _clamp_buffer_pos(state: ViewState) -> None:
    state.buffer_pos = max(0, min(state.buffer_pos, len(state.buffer)))


def _clear_buffer(state: ViewState) -> None:
    state.buffer = ""
    state.buffer_pos = 0


def _insert_into_buffer(state: ViewState, text: str) -> None:
    """Insert `text` at the caret and advance the caret past it."""
    _clamp_buffer_pos(state)
    pos = state.buffer_pos
    state.buffer = state.buffer[:pos] + text + state.buffer[pos:]
    state.buffer_pos = pos + len(text)


def _backspace_buffer(state: ViewState) -> None:
    """Delete the character immediately before the caret, if any."""
    _clamp_buffer_pos(state)
    pos = state.buffer_pos
    if pos <= 0:
        return
    state.buffer = state.buffer[: pos - 1] + state.buffer[pos:]
    state.buffer_pos = pos - 1


@dataclass(frozen=True)
class DetachedRun:
    log_path: str
    status_path: str
    trace_path: str


def apply_key(state: ViewState, key, *, task_count: int, board_count: int = 0):
    """Pure-ish state step: mutate state, return an action or None.

    Actions: "quit" · ("submit", text) · ("open_board", index)
    · ("add_folder", path_text) · None.
    """
    if key is None:
        return None
    if key == "quit":
        return "quit"

    if state.mode == "switcher":
        # Typing is live in the folder menu: Enter with text adds a path;
        # empty Enter opens the highlighted folder.
        if isinstance(key, tuple) and key[0] == "char":
            _insert_into_buffer(state, key[1])
            return None
        if key == "backspace":
            _backspace_buffer(state)
            return None
        if key == "paste_break":
            _clamp_buffer_pos(state)
            if state.buffer and (
                    state.buffer_pos == 0
                    or state.buffer[state.buffer_pos - 1] != " "):
                _insert_into_buffer(state, " ")
            return None
        if key == "left":
            _clamp_buffer_pos(state)
            state.buffer_pos = max(0, state.buffer_pos - 1)
            return None
        if key == "right":
            _clamp_buffer_pos(state)
            state.buffer_pos = min(len(state.buffer), state.buffer_pos + 1)
            return None
        if key == "up":
            state.board_cursor = max(0, state.board_cursor - 1)
        elif key == "down":
            state.board_cursor = min(
                max(0, board_count - 1), state.board_cursor + 1)
        elif key == "enter":
            text = state.buffer.strip()
            if text:
                _clear_buffer(state)
                return ("add_folder", text)
            state.mode = "normal"
            if board_count:
                return ("open_board", state.board_cursor)
        elif key in ("esc", "switch"):
            state.mode = "normal"
            _clear_buffer(state)
        return None

    if isinstance(key, tuple) and key[0] == "char":
        _insert_into_buffer(state, key[1])
        return None
    if key == "backspace":
        _backspace_buffer(state)
        return None
    if key == "paste_break":
        _clamp_buffer_pos(state)
        if state.buffer and (
                state.buffer_pos == 0
                or state.buffer[state.buffer_pos - 1] != " "):
            _insert_into_buffer(state, " ")
        return None
    if key == "enter":
        text = state.buffer.strip()
        _clear_buffer(state)
        return ("submit", text) if text else None
    if key == "esc":
        if state.buffer:
            _clear_buffer(state)
        elif state.cursor is not None:
            state.cursor = None
            state.convo_scroll = 0
            state.task_hscroll = 0
        return None
    if key == "tab":
        state.focus = "convo" if state.focus == "tasks" else "tasks"
        return None
    if key == "switch":
        state.mode = "switcher"
        state.board_cursor = 0
        # Fresh path entry; don't carry leftover task/chat draft into the menu.
        _clear_buffer(state)
        return None

    if key in ("left", "right"):
        # Non-empty input: ←/→ move the text caret (the typing-edit case).
        # Empty input + TASKS focus + a selected row: pan the one-line title
        # so long descriptions stay readable without wrapping.
        if state.buffer:
            _clamp_buffer_pos(state)
            if key == "left":
                state.buffer_pos = max(0, state.buffer_pos - 1)
            else:
                state.buffer_pos = min(len(state.buffer), state.buffer_pos + 1)
            return None
        if state.focus == "tasks" and state.cursor is not None:
            if key == "right":
                state.task_hscroll += _TASK_HSCROLL_STEP
            else:
                state.task_hscroll = max(
                    0, state.task_hscroll - _TASK_HSCROLL_STEP)
        return None

    if key in ("up", "down"):
        # Arrows are scoped to the focused pane: they scroll LIVE CHAT when
        # it holds focus, and move the task cursor when TASKS does.
        if state.focus == "convo":
            if key == "up":
                state.convo_scroll = min(
                    state.convo_max_scroll, state.convo_scroll + 1)
            else:
                state.convo_scroll = max(0, state.convo_scroll - 1)
        elif task_count:
            if state.cursor is None:
                state.cursor = 0
            elif key == "up":
                state.cursor = max(0, state.cursor - 1)
            else:
                state.cursor = min(task_count - 1, state.cursor + 1)
            state.convo_scroll = 0
            state.task_hscroll = 0
        return None

    if key in ("pgup", "pgdn", "home", "end"):
        if state.focus == "convo":
            if key == "pgup":
                state.convo_scroll = min(
                    state.convo_max_scroll, state.convo_scroll + _PAGE)
            elif key == "pgdn":
                state.convo_scroll = max(0, state.convo_scroll - _PAGE)
            elif key == "home":
                state.convo_scroll = state.convo_max_scroll
            else:
                state.convo_scroll = 0
        elif task_count:
            if state.cursor is None:
                state.cursor = 0
            elif key == "pgup":
                state.cursor = max(0, state.cursor - _PAGE)
            elif key == "pgdn":
                state.cursor = min(task_count - 1, state.cursor + _PAGE)
            elif key == "home":
                state.cursor = 0
            else:
                state.cursor = task_count - 1
            state.task_hscroll = 0
        return None

    if isinstance(key, tuple) and key[0] in ("wheel_up", "wheel_down"):
        direction, column = key
        if state.split_col is not None and column < state.split_col:
            pane = "tasks"
        elif state.split_col is not None:
            pane = "convo"
        else:
            pane = state.focus
        if pane == "convo":
            if direction == "wheel_up":
                state.convo_scroll = min(
                    state.convo_max_scroll, state.convo_scroll + _WHEEL_STEP)
            else:
                state.convo_scroll = max(0, state.convo_scroll - _WHEEL_STEP)
        elif task_count:
            if state.cursor is None:
                state.cursor = 0
            elif direction == "wheel_up":
                state.cursor = max(0, state.cursor - 1)
            else:
                state.cursor = min(task_count - 1, state.cursor + 1)
            state.task_hscroll = 0
        return None
    return None


def conversation_entries(history: dict) -> list[dict]:
    """Chronological merge of a task's messages and protocol milestones.

    `message_posted` events are skipped — their message rows carry the
    content; every other event becomes a dim milestone line. Claim-gated
    actions append wall-clock claim hold time as ``[34s]`` when known."""
    claims_by_id: dict = {}
    for claim in history.get("claims") or []:
        cid = claim.get("claim_id")
        if cid is not None:
            claims_by_id[cid] = claim
    entries: list[dict] = []
    for message in history.get("messages") or []:
        entries.append({
            "kind": "message",
            "who": message.get("from_agent"),
            "body": message.get("body"),
            "ts": str(message.get("created_at") or ""),
            "_seq": (1, message.get("id") or 0),
        })
    for event in history.get("events") or []:
        if event.get("event_type") == "message_posted":
            continue
        entries.append({
            "kind": "milestone",
            "text": _milestone_text(event, claims_by_id),
            "ts": str(event.get("created_at") or ""),
            "_seq": (0, event.get("event_id") or 0),
        })
    entries.sort(key=lambda entry: (entry["ts"], entry["_seq"]))
    for entry in entries:
        entry.pop("_seq", None)
    return entries


# Claim-open events are instantaneous bookkeeping; duration would always be ~0.
_NO_DURATION_EVENT_TYPES = frozenset({
    "claim_acquired",
    "question_claimed",
    "review_claimed",
})


def _parse_iso_ts(value) -> datetime.datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def format_action_duration(seconds: int) -> str:
    """Compact wall-clock duration for live-chat milestones, e.g. ``[34s]``."""
    if seconds < 0:
        return ""
    if seconds < 60:
        return f"[{seconds}s]"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"[{minutes}m{sec:02d}s]" if sec else f"[{minutes}m]"
    hours, minutes = divmod(minutes, 60)
    return f"[{hours}h{minutes}m]" if minutes else f"[{hours}h]"


def _claim_hold_seconds(event: dict, claims_by_id: dict) -> int | None:
    """Seconds the claim was held up to this event, if both timestamps exist."""
    claim_id = event.get("claim_id")
    if claim_id is None:
        return None
    claim = claims_by_id.get(claim_id)
    if not claim:
        return None
    start = _parse_iso_ts(claim.get("claimed_at"))
    end = _parse_iso_ts(event.get("created_at"))
    if end is None:
        end = _parse_iso_ts(claim.get("closed_at"))
    if start is None or end is None:
        return None
    return max(0, int(round((end - start).total_seconds())))


def _milestone_text(event: dict, claims_by_id: dict | None = None) -> str:
    kind = str(event.get("event_type") or "event")
    text = kind.replace("_", " ")
    claim_id = event.get("claim_id")
    if kind == "claim_acquired" and claim_id:
        text = f"claim #{claim_id} acquired"
    actor = event.get("actor_agent_id")
    label = f"{text} ({actor})" if actor else text
    if kind in _NO_DURATION_EVENT_TYPES or kind.endswith("_claimed"):
        return label
    seconds = _claim_hold_seconds(event, claims_by_id or {})
    if seconds is None:
        return label
    duration = format_action_duration(seconds)
    return f"{label} {duration}" if duration else label


def _spawn_detached(db_path: str, args: list) -> DetachedRun:
    """Launch `coop start` as a detached background process so the dashboard
    keeps rendering. Return its durable log and status sidecar paths."""
    argv = [sys.executable, "-u", "-m", "agent_coop", "start",
            "--board", str(db_path), *args]
    log_dir = pathlib.Path(db_path).resolve().parent / ".coop-runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"run-{stamp}-{uuid.uuid4().hex[:8]}.log"
    status_path = pathlib.Path(
        coop_runner_status.status_path_for_log(log_path))
    trace_path = pathlib.Path(coop_turn_trace.trace_path_for_log(log_path))
    started_at = coop_runner_status.now()
    env = dict(os.environ)
    caller_pane = coop_runner_status.caller_pane_from_env(env)
    herdr = coop_runner_status.canonical_herdr_metadata(
        caller_pane=caller_pane
    )
    herdr_fields = {"herdr": herdr} if herdr is not None else {}
    coop_runner_status.write_status(
        status_path,
        phase="starting",
        started_at=started_at,
        **herdr_fields,
    )
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    env["COOP_RUN_STATUS_PATH"] = str(status_path)
    env["COOP_RUN_TRACE_PATH"] = str(trace_path)
    try:
        with log_path.open("ab", buffering=0) as run_log:
            subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=run_log,
                stderr=subprocess.STDOUT, env=env, **kwargs)
    except Exception:
        coop_runner_status.write_status(
            status_path, phase="failed", started_at=started_at,
            reason="launch_failed", turns=0, **herdr_fields)
        raise
    return DetachedRun(str(log_path), str(status_path), str(trace_path))


def _slash_command(db_path: str, line: str, item_id=None,
                   launch_sink=None) -> str:
    """Dashboard slash-commands. `/coop [args]` convenes the agents (spawns
    `coop start` detached); `/coop stop` signals it to halt at a boundary."""
    parts = line[1:].split()
    cmd = parts[0].lower() if parts else ""
    rest = parts[1:]
    if cmd == "coop":
        if rest[:1] == ["stop"]:
            from agent_coop import coop_autonomous
            pathlib.Path(coop_autonomous.stop_flag_path(db_path)).write_text(
                "stop", encoding="utf-8")
            return "coop: stop requested"
        if rest == ["all"]:
            launch_args = ["--all"]
        elif rest:
            return "coop: use /coop for the highlighted task or /coop all"
        elif item_id is None:
            return "coop: select a task before /coop"
        else:
            launch_args = ["--item", str(item_id)]
        run = _spawn_detached(db_path, launch_args)
        if launch_sink is not None:
            launch_sink(run)
        return "runner: starting"
    return f"unknown command: /{cmd}"


def create_goal(db_path: str, text: str, launch_sink=None):
    """Create and launch one goal-task; the shared core of TASKS Enter and
    the shell one-liner `coop "<goal>"`.

    The human-lane write creates a draft before the run marker. The autonomous
    agents define and peer-accept its contract before substantive work.
    Returns ``(item_id, run, notice)``: ``run`` is the ``DetachedRun`` or
    ``None`` when the launch failed, and ``notice`` is the one-line text
    the dashboard shows for either outcome.
    """
    conn = coopdb.connect(db_path, require_current=True)
    try:
        item_id = coopdb.create_item(
            conn, actor="human", session_id=None, title=text, objective=text)
    finally:
        conn.close()
    try:
        run = _spawn_detached(db_path, ["--item", str(item_id)])
    except Exception as exc:
        return item_id, None, (
            f"created task #{item_id} (draft) · launch failed: "
            f"{type(exc).__name__}: {exc}"
        )
    if launch_sink is not None:
        launch_sink(run)
    return item_id, run, f"created task #{item_id} (draft) · runner: starting"


def create_task(db_path: str, text: str, launch_sink=None) -> str:
    """Create and launch one goal-task from TASKS input; returns the notice."""
    return create_goal(db_path, text, launch_sink)[2]


def resolve_folder_path(text: str) -> str:
    """Turn a typed folder/board path into a registry board path.

    Accepts a ``*.db`` file, a directory, relative paths (resolved against
    cwd), and ``~`` expansion. For a directory the existing board wins, in
    discovery order (``.coop/board.db``, ``board.db``, ``coop/board.db``);
    a directory with no board maps to ``.coop/board.db``, the layout
    ``coop init --workspace`` creates, so opening it from the switcher
    creates the same thing ``init`` would.
    """
    raw = (text or "").strip().strip('"').strip("'")
    if not raw:
        raise ValueError("empty path")
    path = pathlib.Path(raw).expanduser()
    if not path.is_absolute():
        path = pathlib.Path.cwd() / path
    path = path.resolve(strict=False)
    if path.suffix.lower() == ".db":
        return str(path)
    for board in (path / ".coop" / "board.db", path / "board.db",
                  path / "coop" / "board.db"):
        if board.is_file():
            return str(board)
    return str(path / ".coop" / "board.db")

def _select_created_task(state: ViewState, notice: str,
                         prior_task_count: int) -> None:
    """Keep the dashboard selection on a task just created in TASKS focus."""
    del prior_task_count  # newest-first rows always place the new task at 0
    if state.focus == "tasks" and notice.startswith("created task #"):
        state.cursor = 0
        state.task_hscroll = 0


def submit_line(db_path: str, line: str, item_id, focus="convo",
                launch_sink=None) -> str:
    """The input seam. `/…` is always a dashboard command.

    TASKS input creates and launches a goal-task. LIVE CHAT input posts a
    message, scoped to the selected task when one exists. Command words such as
    ``agents`` still dispatch through the REPL.
    """
    stripped = line.strip()
    if not stripped:
        return ""
    if stripped.startswith("/"):
        return _slash_command(
            db_path, stripped, item_id, launch_sink=launch_sink)
    from agent_coop import coop_repl
    kind = coop_repl.classify_line(stripped)
    if kind == "prompt":
        if focus == "tasks":
            return create_task(
                db_path,
                stripped,
                launch_sink=launch_sink,
            )
        conn = coopdb.connect(db_path, require_current=True)
        try:
            message_id = coopdb.say(
                conn, session_id=None, body=stripped, item_id=item_id)
        finally:
            conn.close()
        scope = f" → #{item_id}" if item_id else ""
        return f"posted message #{message_id}{scope}"
    if kind == "builtin":
        return "builtins stay in the REPL — ^c quits the dashboard"
    out, err = io.StringIO(), io.StringIO()
    coop_repl.dispatch_line(stripped, db_path, "human", out, err)
    combined = " ".join((out.getvalue() + " " + err.getvalue()).split())
    return combined[:200] if combined else "ok"


def _board_feed(conn) -> list[dict]:
    """Board-wide messages (item_id IS NULL) as conversation entries, so an
    agent's board-wide notes are visible when no task is selected."""
    return [
        {"kind": "message", "who": m.get("from_agent"),
         "body": m.get("body"), "ts": str(m.get("created_at") or "")}
        for m in coopdb.message_rows(conn, limit=200)
        if m.get("item_id") is None
    ]


def load_snapshot(conn) -> dict:
    return {
        "tasks": coopdb.task_rows(conn),
        "sessions": coopdb.session_rows(conn),
    }


def _tasks_newest_first(tasks: list[dict]) -> list[dict]:
    """Canonical dashboard row order used by both rendering and actions."""
    return sorted(
        tasks, key=lambda task: task.get("item_id") or 0, reverse=True)


def _task_id_at_cursor(conn, cursor: int | None) -> int | None:
    """Resolve a rendered task cursor through the canonical row order."""
    if cursor is None:
        return None
    tasks = _tasks_newest_first(coopdb.task_rows(conn))
    if 0 <= cursor < len(tasks):
        return tasks[cursor]["item_id"]
    return None


def open_board_conn(db_path: str):
    """Open a current-schema board for the dashboard.

    Returns ``(conn, None)`` on success, or ``(None, notice)`` on any open
    failure. Never raises for missing files, empty DBs, or schema mismatch —
    ^o must be able to land on any registry path without exiting the app.
    """
    path = pathlib.Path(db_path)
    if not path.is_file():
        return None, f"empty board (file missing): {db_path}"
    try:
        return coopdb.connect(str(path), require_current=True), None
    except Exception as exc:
        # Empty sqlite files, pre-release boards, locked paths, etc.
        msg = str(exc).strip() or type(exc).__name__
        return None, f"empty board ({msg}): {db_path}"



def open_or_create_board(db_path: str):
    """Switcher open: return ``(conn, notice)``.

    A listed folder that has no board yet gets one created on open — board
    plus harness adapters, exactly as ``coop init --workspace`` — because the
    folder only reached the list through an explicit user action. A board
    that exists but cannot be read still returns a notice, never raises.
    """
    path = pathlib.Path(db_path)
    if not path.is_file():
        workspace = coopdb.board_workspace(path)
        if not workspace.is_dir():
            return None, f"folder no longer exists: {workspace}"
        try:
            from agent_coop import coop_adapters
            coop_adapters.provision_workspace_board(str(path))
        except Exception as exc:
            msg = str(exc).strip() or type(exc).__name__
            return None, f"could not create board ({msg}): {db_path}"
        conn, err = open_board_conn(str(path))
        if conn is None:
            return None, err
        return conn, f"created board in {coopdb.display_board_path(path)}"
    conn, err = open_board_conn(str(path))
    if conn is None:
        return None, err
    return conn, f"board: {coopdb.display_board_path(path)}"


def _board_label(db_path: str) -> str:
    try:
        parent = pathlib.Path(db_path).resolve().parent
        # `.coop` is the current layout, `coop` an older one — in both the
        # useful label is the workspace (or worktree) the board belongs to.
        if parent.name in (".coop", "coop") and parent.parent.name:
            return parent.parent.name
        return parent.name or db_path
    except Exception:
        return db_path


def _follow_latest_runner_status(state: ViewState, db_path: str) -> bool:
    """Point the notice at the newest board-local status sidecar.

    Returns True when the tracked path changed (caller should repaint). A
    finished earlier run must not pin the line once a later task launches —
    TASKS Enter, dashboard /coop, and bare ``coop start`` all write under
    ``.coop-runs/``.
    """
    discovered = coop_runner_status.latest_status_path(
        coop_runner_status.runs_dir_for_board(db_path))
    if not discovered or discovered == state.runner_status_path:
        return False
    state.runner_status_path = discovered
    return True


def _runner_notice(state: ViewState, *, now=None) -> str:
    """Prefer live operational runner telemetry over the launch notice.

    When the newest sidecar carries Herdr mirror metadata, its opaque pane IDs
    are appended as a jump target. They are addresses for `herdr pane
    …`, never a second rendering of board content.
    """
    if state.runner_status_path:
        status = coop_runner_status.read_status(state.runner_status_path)
        if status is not None:
            line = coop_runner_status.format_status(status, now=now)
            if line:
                jump = coop_runner_status.format_herdr_jump(status)
                return f"{line} · {jump}" if jump else line
    return state.notice


def _caret_tuple(cell: list) -> tuple[int, int] | None:
    if len(cell) >= 2:
        return (int(cell[0]), int(cell[1]))
    return None


def _frame_empty(
    db_path: str, state: ViewState, size, boards, *, notice: str | None = None,
) -> tuple[str, int, tuple[int, int] | None]:
    """Render a valid dashboard for an unreadable/missing board (0 tasks)."""
    if notice:
        state.notice = notice
    state.cursor = None
    state.convo_scroll = 0
    state.convo_max_scroll = 0
    state.task_hscroll = 0
    overlay = None
    if state.mode == "switcher":
        overlay = coop_ui.render_board_switcher(
            boards, cursor=state.board_cursor, width=size.columns,
            height=coop_ui.dashboard_body_height(
                height=size.lines, width=size.columns,
                input_buffer=state.buffer),
            color=True)
    state.split_col = coop_ui.dashboard_split_col(size.columns)
    scroll_info: list[int] = []
    caret_cell: list[int] = []
    frame = coop_ui.render_dashboard(
        tasks=[], conversation=[], selected=None, sessions=[],
        cursor=None, width=size.columns, height=size.lines, color=True,
        board_label=_board_label(db_path), board_path=db_path,
        input_buffer=state.buffer, input_cursor=state.buffer_pos,
        notice=_runner_notice(state),
        focus=state.focus, convo_scroll=0, task_hscroll=0,
        stamp=coopdb.now(), overlay_lines=overlay,
        convo_scroll_info=scroll_info, input_caret_cell=caret_cell)
    _apply_convo_scroll_info(state, scroll_info)
    return frame, 0, _caret_tuple(caret_cell)


def _apply_convo_scroll_info(state: ViewState, scroll_info: list[int]) -> None:
    """Write paint-clamped scroll + ceiling back so keys never overshoot."""
    if len(scroll_info) >= 2:
        state.convo_scroll = int(scroll_info[0])
        state.convo_max_scroll = int(scroll_info[1])
    elif state.mode == "switcher":
        # Overlay hides the convo pane; keep prior ceiling, no debt.
        state.convo_scroll = min(state.convo_scroll, state.convo_max_scroll)


def _render(
    db_path: str, state: ViewState, size, boards,
) -> tuple[str, int, tuple[int, int] | None]:
    """One frame from a fresh connection; returns (frame, task_count, caret).

    Unreadable boards render an empty task list and a notice — never raise.
    ``caret`` is a 1-based ``(row, col)`` for the terminal bar cursor, or None.
    """
    conn, err = open_board_conn(db_path)
    if conn is None:
        return _frame_empty(db_path, state, size, boards, notice=err)
    try:
        snapshot = load_snapshot(conn)
        tasks = _tasks_newest_first(snapshot["tasks"])
        count = len(tasks)
        if state.cursor is not None and count:
            state.cursor = min(state.cursor, count - 1)
        elif not count:
            state.cursor = None
        selected = tasks[state.cursor] if state.cursor is not None else None
        if selected is not None:
            history = coopdb.item_show(
                conn, selected["item_id"], history=True)
            conversation = conversation_entries(history)
        else:
            # No task selected → show the board-wide feed, not a blank pane.
            conversation = _board_feed(conn)
        overlay = None
        if state.mode == "switcher":
            overlay = coop_ui.render_board_switcher(
                boards, cursor=state.board_cursor, width=size.columns,
                height=coop_ui.dashboard_body_height(
                    height=size.lines, width=size.columns,
                    input_buffer=state.buffer),
                color=True)
        state.split_col = coop_ui.dashboard_split_col(size.columns)
        now_ts = coopdb.now()
        active = frozenset(
            r["claimed_by_agent"] for r in conn.execute(
                "SELECT DISTINCT claimed_by_agent FROM claims "
                "WHERE status='active' AND lease_expires_at > ?", (now_ts,)))
        # blink only for claims whose owning session is live — a zombie claim
        # on an exited session must not animate the repaint beat as active work
        state.blinking = any(
            t.get("claim") and t["claim"].get("session_live") for t in tasks)
        scroll_info: list[int] = []
        caret_cell: list[int] = []
        frame = coop_ui.render_dashboard(
            tasks=tasks, conversation=conversation, selected=selected,
            sessions=snapshot["sessions"], cursor=state.cursor,
            width=size.columns, height=size.lines, color=True,
            board_label=_board_label(db_path), board_path=db_path,
            input_buffer=state.buffer, input_cursor=state.buffer_pos,
            notice=_runner_notice(state),
            focus=state.focus, convo_scroll=state.convo_scroll,
            task_hscroll=state.task_hscroll,
            stamp=now_ts, overlay_lines=overlay,
            active=active, blink_on=int(time.monotonic()) % 2 == 0,
            convo_scroll_info=scroll_info, input_caret_cell=caret_cell)
        _apply_convo_scroll_info(state, scroll_info)
        return frame, count, _caret_tuple(caret_cell)
    except Exception as exc:
        # Mid-read failure (corrupt tables mid-session): stay on this path.
        return _frame_empty(
            db_path, state, size, boards,
            notice=f"empty board (read failed: {exc}): {db_path}")
    finally:
        conn.close()


def _enable_mouse(stdout: TextIO):
    """Best-effort SGR mouse reporting; returns an opaque restore token."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(wintypes.DWORD(-10))
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return None
        ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
        if not kernel32.SetConsoleMode(
                handle, mode.value | ENABLE_VIRTUAL_TERMINAL_INPUT):
            return None
        stdout.write("\033[?1000;1006h")
        stdout.flush()
        return (kernel32, handle, mode.value)
    except Exception:
        return None


def _disable_mouse(stdout: TextIO, token) -> None:
    if token is None:
        return
    try:
        stdout.write("\033[?1000;1006l")
        stdout.flush()
        kernel32, handle, old_mode = token
        kernel32.SetConsoleMode(handle, old_mode)
    except Exception:
        pass


def _quiet_ctrl_c():
    """Read Ctrl+C as a keystroke (\\x03 → 'quit') instead of a console signal,
    so quitting the dashboard never leaves cmd.exe's 'Terminate batch job
    (Y/N)?' prompt. Returns a restore token, or None if unavailable."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(wintypes.DWORD(-10))  # STD_INPUT_HANDLE
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return None
        ENABLE_PROCESSED_INPUT = 0x0001
        if not kernel32.SetConsoleMode(handle, mode.value & ~ENABLE_PROCESSED_INPUT):
            return None
        return (kernel32, handle, mode.value)
    except Exception:
        return None


def _restore_console_input(token) -> None:
    if token is None:
        return
    try:
        kernel32, handle, old_mode = token
        kernel32.SetConsoleMode(handle, old_mode)
    except Exception:
        pass


def run_dashboard(db_path: str, *, interval: float = 2.0,
                  notice: str = "") -> bool:
    """The live app. Returns False when the surface is unavailable (no VT),
    True after a normal quit — the caller falls back on False. ``notice``
    is the first text on the notice line (board created, skill
    installed); the next action replaces it."""
    try:
        import msvcrt as keys
    except ImportError:
        return False
    stdout = sys.stdout
    if not coop_ui.begin_live_display(stdout):
        return False
    # Mouse reporting is intentionally NOT enabled: capturing the mouse would
    # disable the terminal's native click-drag text selection/copy in the
    # panes. Scrolling stays on the keyboard (pgup/pgdn/home/end); the mouse
    # belongs to the terminal so you can select and copy freely.
    mouse_token = None
    # Take Ctrl+C as a keystroke so quitting shows our own farewell, not
    # cmd.exe's "Terminate batch job (Y/N)?".
    input_token = _quiet_ctrl_c()

    state = ViewState()
    state.notice = notice
    boards: list[str] = []
    task_count = 0
    last_probe = None
    last_size = None
    next_probe = 0.0
    next_blink = 0.0
    next_runner_tick = 0.0
    last_runner_token = None
    dirty = True
    current_db = db_path
    # Pin whatever path the dashboard opened (explicit --db included) so the
    # ^o switcher can find scratch boards. cmd_monitor also records;
    # double record is fine (updates last_opened). Switcher open_board below
    # records on each switch.
    coopdb.record_board(db_path)
    try:
        while True:
            try:
                for key in decode_available_keys(keys):
                    if key == "switch" and state.mode == "normal":
                        # Registry entries are folders that still exist;
                        # a folder without a board is created on open, and
                        # an unreadable board lands on an empty task list
                        # (open_or_create_board), never exits the dashboard.
                        boards = coopdb.known_boards()
                    selected_id = None
                    if state.cursor is not None and task_count:
                        conn, _err = open_board_conn(current_db)
                        if conn is not None:
                            try:
                                selected_id = _task_id_at_cursor(
                                    conn, state.cursor)
                            finally:
                                conn.close()
                    action = apply_key(
                        state, key, task_count=task_count,
                        board_count=len(boards))
                    if action == "quit":
                        return True
                    if isinstance(action, tuple) and action[0] == "submit":
                        try:
                            launches = []
                            state.notice = submit_line(
                                current_db, action[1], selected_id,
                                state.focus, launch_sink=launches.append)
                            if launches:
                                state.runner_log_path = launches[-1].log_path
                                state.runner_status_path = (
                                    launches[-1].status_path)
                            _select_created_task(
                                state, state.notice, task_count)
                        except Exception as exc:
                            state.notice = (
                                f"submit failed (is this board current?): "
                                f"{exc}")
                    elif isinstance(action, tuple) and action[0] == "add_folder":
                        try:
                            candidate = resolve_folder_path(action[1])
                            workspace = coopdb.board_workspace(candidate)
                            if not workspace.is_dir():
                                raise ValueError(
                                    f"folder does not exist: {workspace}")
                            if (coopdb.is_temp_path(candidate)
                                    and not os.environ.get(
                                        "COOP_BOARDS_REGISTRY")):
                                raise ValueError(
                                    "folders under the temp directory are "
                                    "not listed")
                            coopdb.record_board(candidate)
                            boards = coopdb.known_boards()
                            if candidate in boards:
                                state.board_cursor = boards.index(candidate)
                            state.notice = (
                                "dir added: "
                                f"{coopdb.display_board_path(candidate)}")
                        except Exception as exc:
                            state.notice = f"dir add failed: {exc}"
                    elif isinstance(action, tuple) and action[0] == "open_board":
                        # Always switch. A folder without a board gets one
                        # created; an unreadable board still becomes the
                        # current path with an empty task list + notice.
                        if 0 <= action[1] < len(boards):
                            candidate = boards[action[1]]
                            current_db = candidate
                            coopdb.record_board(candidate)
                            state.cursor = None
                            state.convo_scroll = 0
                            state.convo_max_scroll = 0
                            state.task_hscroll = 0
                            state.runner_log_path = None
                            state.runner_status_path = None
                            _conn, open_notice = open_or_create_board(
                                candidate)
                            if _conn is not None:
                                _conn.close()
                            state.notice = open_notice
                    dirty = True

                size = shutil.get_terminal_size((100, 24))
                if size != last_size:
                    last_size, dirty = size, True

                now = time.monotonic()
                if now >= next_runner_tick:
                    # Always re-resolve the newest sidecar so a later task's
                    # run replaces a stuck "finished · all_done" from the
                    # previous one (CLI start, TASKS Enter, or /coop).
                    if _follow_latest_runner_status(state, current_db):
                        dirty = True
                        last_runner_token = None
                    if state.runner_status_path:
                        token = coop_runner_status.status_token(
                            state.runner_status_path)
                        status = coop_runner_status.read_status(
                            state.runner_status_path)
                        if token != last_runner_token:
                            dirty = True   # phase / turns / terminal flip
                            last_runner_token = token
                        elif (
                            status is not None
                            and not coop_runner_status.is_terminal(status)
                        ):
                            dirty = True   # keep elapsed runner time moving
                    next_runner_tick = now + 1.0
                if state.blinking and now >= next_blink:
                    dirty = True          # animate the active-agent dot pulse
                    next_blink = now + 0.5
                if now >= next_probe:
                    if _follow_latest_runner_status(state, current_db):
                        dirty = True
                        last_runner_token = None
                    conn, _err = open_board_conn(current_db)
                    if conn is None:
                        # Treat unreadable board as a stable empty probe so
                        # we repaint once into the empty view, then idle.
                        probe = (
                            "unreadable", current_db,
                            coop_runner_status.status_token(
                                state.runner_status_path),
                        )
                        if probe != last_probe:
                            last_probe, dirty = probe, True
                    else:
                        try:
                            probe = (
                                coopdb.board_probe(conn),
                                coop_runner_status.status_token(
                                    state.runner_status_path),
                            )
                        finally:
                            conn.close()
                        if probe != last_probe:
                            last_probe, dirty = probe, True
                    next_probe = now + max(0.25, float(interval))

                if dirty:
                    frame, task_count, caret = _render(
                        current_db, state, size, boards)
                    coop_ui.paint_live_display(
                        frame, stdout, caret_cell=caret)
                    dirty = False
            except Exception as exc:
                # Never let a per-frame glitch dump the operator out of coop.
                state.notice = f"dashboard error (staying open): {exc}"
                dirty = True
                try:
                    size = shutil.get_terminal_size((100, 24))
                    frame, task_count, caret = _frame_empty(
                        current_db, state, size, boards, notice=state.notice)
                    coop_ui.paint_live_display(
                        frame, stdout, caret_cell=caret)
                    dirty = False
                except Exception:
                    pass
            time.sleep(0.03)
    except KeyboardInterrupt:
        return True
    finally:
        _disable_mouse(stdout, mouse_token)
        _restore_console_input(input_token)
        coop_ui.end_live_display(stdout, used_alt=True)
        stdout.write(coop_ui._paint("Leaving the coop?", coop_ui.GREY, True)
                     + "\n")
        stdout.flush()
