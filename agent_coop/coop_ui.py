"""Pure terminal presentation primitives for the Agent_coop transcript UI."""

from __future__ import annotations

from agent_coop import coopdb
import os
import re
import shutil
import sys
import textwrap
import time
from dataclasses import dataclass
from typing import Callable, Protocol, TextIO


ESC = "\033["
RESET = f"{ESC}0m"
BOLD = f"{ESC}1m"
CORAL = f"{ESC}38;2;215;119;87m"
BLUE = f"{ESC}38;2;136;192;208m"
GREY = f"{ESC}38;2;147;147;147m"
# Focus/selection accent — deliberately none of the three agent colours
# (coral/blue/grey); a soft violet reads as "this pane has the keys".
VIOLET_RGB = (180, 142, 173)  # #b48ead
VIOLET = f"{ESC}38;2;{VIOLET_RGB[0]};{VIOLET_RGB[1]};{VIOLET_RGB[2]}m"
# Folder identity (board label) — ochre from the CLI syntax palette
# (coop_cli_style.OCHRE / #D99518); unused elsewhere in the dashboard so
# the place name is not confused with agents or focus.
OCHRE = f"{ESC}38;2;217;149;24m"  # #D99518
# Legacy glyph only (not painted into the dashboard input). The input
# caret is the real terminal vertical bar parked between letters.
INPUT_CARET = "▏"

AGENT_COLORS = {"claude": CORAL, "codex": BLUE, "grok": GREY}
ACTIVITY_FRAMES = ("▪", " ")
ANSI_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CLEAR_LINE = f"{ESC}2K"
RESET_SCROLL_REGION = f"{ESC}r"
# DECSCUSR: 5 = blinking bar, 6 = steady bar.
BLINKING_BAR_CURSOR = f"{ESC}5 q"  # REPL / callers that want blink
STEADY_BAR_CURSOR = f"{ESC}6 q"
DEFAULT_CURSOR = f"{ESC}0 q"
# ATT610 cursor-blink mode — must be *off* for a solid bar. Many hosts
# ignore DECSCUSR steady (6) unless this private mode is cleared too.
CURSOR_BLINK_OFF = f"{ESC}?12l"
CURSOR_BLINK_ON = f"{ESC}?12h"
# OSC 12 / 112 — paint the hardware cursor the focus violet (#b48ead).
_CURSOR_COLOR_VIOLET = (
    f"\033]12;#{VIOLET_RGB[0]:02x}{VIOLET_RGB[1]:02x}{VIOLET_RGB[2]:02x}\007"
)
_CURSOR_COLOR_RESET = "\033]112\007"
SAVE_CURSOR = "\0337"
RESTORE_CURSOR = "\0338"


@dataclass(frozen=True)
class AgentActivity:
    name: str
    state: str
    item_id: int | None = None
    title: str | None = None


@dataclass(frozen=True)
class BoardSnapshot:
    agents: tuple[AgentActivity, ...]


_GLYPHS = {
    "C": (
        "11111",
        "11000",
        "11000",
        "11000",
        "11000",
        "11000",
        "11111",
    ),
    "O": (
        "01110",
        "11011",
        "11011",
        "11011",
        "11011",
        "11011",
        "01110",
    ),
    "P": (
        "11110",
        "11011",
        "11011",
        "11110",
        "11000",
        "11000",
        "11000",
    ),
}


def _paint(text: str, color_code: str, enabled: bool, *, bold: bool = False) -> str:
    if not enabled:
        return text
    weight = BOLD if bold else ""
    return f"{weight}{color_code}{text}{RESET}"


def _half_block_cell(top_on: bool, bottom_on: bool) -> str:
    """Pack two glyph rows into one terminal row (half vertical size)."""
    if top_on and bottom_on:
        return "█"
    if top_on:
        return "▀"
    if bottom_on:
        return "▄"
    return " "


def _wordmark_lines(color: bool, width: int) -> list[str]:
    """Half-size COOP mark (single-cell width + half-block height).

    Full-size was ██ over 7 glyph rows + 2 fall lines. Compact form packs
    bits to one cell and pairs glyph rows into ▀/▄/█ lines. COOP text
    colour only: coral · grey · blue · blue (fall dots are separate).
    """
    rows: list[str] = []
    word = "COOP"
    indent = "  " if width >= 2 else ""
    # COOP letter bands only (not the fall dots): less coral, more blue.
    pair_colors = (CORAL, GREY, BLUE, BLUE)
    pairs = ((0, 1), (2, 3), (4, 5), (6, None))
    for pair_index, (top_row, bottom_row) in enumerate(pairs):
        pieces = []
        for letter in word:
            glyph = _GLYPHS[letter]
            top = glyph[top_row]
            bottom = (glyph[bottom_row] if bottom_row is not None
                      else ("0" * len(top)))
            pieces.append("".join(
                _half_block_cell(top[i] == "1", bottom[i] == "1")
                for i in range(len(top))))
        # Letter gap was four spaces at full size; half is two.
        art = _paint("  ".join(pieces).rstrip(), pair_colors[pair_index], color,
                     bold=True)
        version = ""
        if pair_index == len(pairs) - 1:
            version = "  " + _paint("v0.1.0", GREY, color)
        rows.append(indent + art + version)

    # Dissolve texture under the mark — not part of the letter colour bands.
    fall = " ·   ·· ·    · ··   ·  ·  ·· "
    rows.append(indent + _paint(fall, BLUE, color))
    return rows


def prompt_text(color: bool = True) -> str:
    """Input caret — same violet accent as TASKS/LIVE CHAT focus highlight."""
    if not color:
        return "> "
    return f"{VIOLET}>{RESET} "


def _compact_wordmark(color: bool) -> str:
    """One-line COOP mark when the full half-block art won't fit.

    Letter palette matches the large mark bands: C coral · O grey · O blue · P blue.
    """
    return (
        _paint("C", CORAL, color, bold=True)
        + _paint("O", GREY, color, bold=True)
        + _paint("O", BLUE, color, bold=True)
        + _paint("P", BLUE, color, bold=True)
        + "  "
        + _paint("v0.1.0", GREY, color)
    )


def splash_text(width: int = 100, color: bool = True) -> str:
    """Return the once-per-session transcript splash."""
    rule = _paint("─" * max(1, min(width, 100)), GREY, color)
    welcome = _paint("Welcome to the coop!", GREY, color)
    if width < 64:
        return "\n".join((_compact_wordmark(color), "", welcome, rule))

    return "\n".join((*_wordmark_lines(color, width), "", welcome, rule))


def _agent_name(name: str, color: bool) -> str:
    return _paint(name, AGENT_COLORS.get(name, GREY), color, bold=True)


def _visible_len(text: str) -> int:
    return len(ANSI_PATTERN.sub("", text))


def _truncate_ansi(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if _visible_len(text) <= width:
        return text

    limit = max(0, width - 1)
    visible = 0
    index = 0
    parts: list[str] = []
    while index < len(text) and visible < limit:
        match = ANSI_PATTERN.match(text, index)
        if match:
            parts.append(match.group(0))
            index = match.end()
            continue
        parts.append(text[index])
        visible += 1
        index += 1
    parts.append("…")
    if ESC in text:
        parts.append(RESET)
    return "".join(parts)


def render_footer(
    snapshot: BoardSnapshot,
    frame: int = 0,
    color: bool = True,
    width: int = 100,
) -> str:
    """Render one width-bounded provider status row for the fixed footer."""
    detailed = []
    basic = []
    compact = []
    for activity in snapshot.agents:
        if activity.state in {"working", "reviewing"}:
            indicator = ACTIVITY_FRAMES[frame % len(ACTIVITY_FRAMES)]
            state = f"{activity.state} {indicator}"
        else:
            state = activity.state
            indicator = ""
        item = ""
        if activity.item_id is not None:
            item = f" #{activity.item_id}"
            if activity.title:
                title = " ".join(activity.title.split())
                if len(title) > 18:
                    title = title[:17] + "…"
                item += f" {title}"
        name = _agent_name(activity.name, color)
        detailed.append(f"{name} {state}{item}")
        basic.append(f"{name} {state}")
        compact_suffix = f" {indicator}" if indicator else ""
        compact.append(f"{name}:{activity.state[0]}{compact_suffix}")

    candidates = (
        "   ".join(detailed),
        "   ".join(basic),
        "  ".join(compact),
    )
    for candidate in candidates:
        if _visible_len(candidate) <= width:
            return candidate
    return _truncate_ansi(candidates[-1], width)


def render_activity(
    snapshot: BoardSnapshot,
    frame: int = 0,
    color: bool = True,
    width: int = 100,
) -> str:
    """Compatibility alias for the original pre-footer renderer name."""
    return render_footer(snapshot, frame=frame, color=color, width=width)


class _WindowsKeys(Protocol):
    def kbhit(self) -> bool: ...

    def getwch(self) -> str: ...


def _terminal_size(
    size_provider: Callable[..., os.terminal_size] = shutil.get_terminal_size,
) -> os.terminal_size:
    size = size_provider((100, 24))
    return os.terminal_size((max(1, size.columns), max(1, size.lines)))


def _cursor_to(row: int, column: int = 1) -> str:
    return f"{ESC}{max(1, row)};{max(1, column)}H"


def _draw_footer(stdout: TextIO, size: os.terminal_size, text: str) -> None:
    if size.lines < 3:
        return
    bounded = _truncate_ansi(text, size.columns)
    stdout.write(
        SAVE_CURSOR
        + _cursor_to(size.lines)
        + CLEAR_LINE
        + bounded
        + RESTORE_CURSOR
    )


def _redraw_layout(
    stdout: TextIO,
    size: os.terminal_size,
    prompt: str,
    buffer: str,
    footer: str,
    *,
    previous_size: os.terminal_size | None = None,
) -> None:
    parts = [BLINKING_BAR_CURSOR, RESET_SCROLL_REGION]
    if previous_size is not None and previous_size.lines <= size.lines:
        parts.extend((_cursor_to(previous_size.lines), CLEAR_LINE))

    if size.lines >= 3:
        input_row = size.lines - 1
        parts.extend(
            (
                f"{ESC}1;{input_row}r",
                _cursor_to(input_row),
                CLEAR_LINE,
                _fit_input_line(prompt, buffer, size.columns),
            )
        )
    else:
        parts.extend(
            (
                _cursor_to(size.lines),
                CLEAR_LINE,
                _fit_input_line(prompt, buffer, size.columns),
            )
        )

    stdout.write("".join(parts))
    _draw_footer(stdout, size, footer)
    stdout.flush()


def restore_terminal(
    stdout: TextIO,
    size_provider: Callable[..., os.terminal_size] = shutil.get_terminal_size,
) -> None:
    """Restore cursor shape/colour and full-height scrolling for the shell."""
    size = _terminal_size(size_provider)
    stdout.write(
        _CURSOR_COLOR_RESET
        + DEFAULT_CURSOR
        + RESET
        + _cursor_to(size.lines)
        + CLEAR_LINE
        + RESET_SCROLL_REGION
        + _cursor_to(size.lines)
        + CLEAR_LINE
    )
    stdout.flush()


def _fit_input_line(prompt: str, buffer: str, width: int) -> str:
    """Window a long input buffer without changing its submitted value."""
    available = max(1, width - 1)
    prompt_width = len(ANSI_PATTERN.sub("", prompt))
    if prompt_width + len(buffer) <= available:
        return prompt + buffer
    if prompt_width + 1 >= available:
        return buffer[-available:]
    tail_width = available - prompt_width - 1
    return prompt + "…" + buffer[-tail_width:]


def _read_windows(
    prompt: str,
    status_provider: Callable[[int, int], str],
    stdout: TextIO,
    keys: _WindowsKeys,
    *,
    refresh: float = 0.35,
    size_provider: Callable[..., os.terminal_size] = shutil.get_terminal_size,
) -> str:
    """Read one Unicode line while refreshing a resize-safe fixed footer."""
    buffer: list[str] = []
    frame = 0
    size = _terminal_size(size_provider)

    def input_line() -> str:
        return _fit_input_line(prompt, "".join(buffer), size.columns)

    _redraw_layout(
        stdout,
        size,
        prompt,
        "".join(buffer),
        status_provider(frame, size.columns),
    )
    next_refresh = time.monotonic() + refresh

    while True:
        current_size = _terminal_size(size_provider)
        if current_size != size:
            previous_size = size
            size = current_size
            _redraw_layout(
                stdout,
                size,
                prompt,
                "".join(buffer),
                status_provider(frame, size.columns),
                previous_size=previous_size,
            )

        if keys.kbhit():
            char = keys.getwch()
            if char in {"\r", "\n"}:
                stdout.write("\r" + CLEAR_LINE + prompt + "".join(buffer) + "\n")
                stdout.flush()
                return "".join(buffer)
            if char == "\003":
                raise KeyboardInterrupt
            if char in {"\004", "\032"} and not buffer:
                raise EOFError
            if char == "\b":
                if buffer:
                    buffer.pop()
            elif char in {"\x00", "\xe0"}:
                # Consume the second half of Windows extended-key sequences.
                if keys.kbhit():
                    keys.getwch()
                continue
            elif char.isprintable():
                buffer.append(char)
            stdout.write("\r" + CLEAR_LINE + input_line())
            stdout.flush()

        now = time.monotonic()
        if now >= next_refresh:
            frame += 1
            _draw_footer(stdout, size, status_provider(frame, size.columns))
            stdout.flush()
            next_refresh = now + refresh
        time.sleep(0.02)


def read_command(
    prompt: str,
    status_provider: Callable[[int, int], str],
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    refresh: float = 0.35,
) -> str:
    """Read a command with animation on Windows and a pipe-safe fallback."""
    if live_footer_supported(stdin, stdout):
        import msvcrt

        return _read_windows(
            prompt, status_provider, stdout, msvcrt, refresh=refresh
        )

    stdout.write(prompt)
    stdout.flush()
    line = stdin.readline()
    if line == "":
        raise EOFError
    return line.rstrip("\r\n")


def live_footer_supported(stdin: TextIO, stdout: TextIO) -> bool:
    """Return whether this process can use Windows VT output plus msvcrt input."""
    return os.name == "nt" and stdin.isatty() and stdout.isatty()


# --- operator views (thin presentation; no protocol authority) ---------------

ENTER_ALT_SCREEN = f"{ESC}?1049h"
LEAVE_ALT_SCREEN = f"{ESC}?1049l"
HIDE_CURSOR = f"{ESC}?25l"
SHOW_CURSOR = f"{ESC}?25h"
CLEAR_HOME = f"{ESC}2J{ESC}H"
HOME = f"{ESC}H"
CLEAR_EOS = f"{ESC}0J"


def enable_vt(stdout: TextIO = sys.stdout) -> bool:
    """Enable Windows virtual-terminal processing so ANSI clears/redraws work.

    Returns True when the stream is a TTY that can use ANSI control sequences.
    On non-Windows platforms a TTY is enough. Failures degrade to plain output.
    """
    if not stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(wintypes.DWORD(-11))  # STD_OUTPUT_HANDLE
        if handle in (0, wintypes.HANDLE(-1).value):
            return False
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if not kernel32.SetConsoleMode(handle, new_mode):
            return False
        return True
    except Exception:
        return False


def _section(title: str, color: bool) -> str:
    return _paint(f"== {title} ==", GREY, color, bold=True)


def render_message_board(
    messages: list[dict],
    *,
    color: bool = True,
    limit: int = 30,
    width: int = 100,
) -> str:
    """Render recent board messages (newest last, chat-style)."""
    lines = [_section("board", color)]
    if not messages:
        lines.append(_paint("  (no messages)", GREY, color))
        return "\n".join(lines)

    shown = messages[-limit:]
    for msg in shown:
        mid = msg.get("id", "?")
        who = msg.get("from_agent") or "?"
        to = msg.get("to_agent")
        body = " ".join(str(msg.get("body") or "").split())
        when = str(msg.get("created_at") or "")
        if len(when) > 19:
            when = when[:19]
        dest = f" → {to}" if to else ""
        prefix = f"  #{mid} {_agent_name(who, color)}{dest}"
        meta = _paint(f"  {when}", GREY, color) if when else ""
        line = f"{prefix}{meta}: {body}"
        lines.append(_truncate_ansi(line, width))
    return "\n".join(lines)


def render_task_board(
    tasks: list[dict],
    *,
    color: bool = True,
    width: int = 100,
) -> str:
    """Render the task list with durable owner, next actor, and live claim."""
    lines = [_section("tasks", color)]
    if not tasks:
        lines.append(_paint("  (no tasks)", GREY, color))
        return "\n".join(lines)

    for task in tasks:
        tid = task.get("item_id") or task.get("id") or "?"
        status = task.get("status") or "?"
        title = " ".join(str(task.get("title") or "").split())
        owner = task.get("owner") or "-"
        next_actor = task.get("next_actor") or "-"
        claim = task.get("claim")
        claim_bit = ""
        if claim:
            claim_bit = (
                f"  claim#{claim.get('claim_id')} "
                f"{claim.get('agent') or '?'}"
            )
        labels = task.get("labels") or []
        label_bit = f" [{','.join(labels)}]" if labels else ""
        line = (
            f"  #{tid} [{status}] {title}{label_bit}"
            f"  owner={_agent_name(owner, color) if owner != '-' else owner}"
            f"  next={_agent_name(next_actor, color) if next_actor != '-' else next_actor}"
            f"{claim_bit}"
        )
        lines.append(_truncate_ansi(line, width))
    return "\n".join(lines)


def render_sessions_strip(
    sessions: list[dict],
    *,
    color: bool = True,
    width: int = 100,
) -> str:
    lines = [_section("sessions", color)]
    if not sessions:
        lines.append(_paint("  (no live sessions)", GREY, color))
        return "\n".join(lines)
    for sess in sessions:
        agent = sess.get("agent_id") or "?"
        provider = sess.get("provider") or "?"
        status = sess.get("status") or "?"
        sid = str(sess.get("session_id") or "")[:8]
        line = (
            f"  {_agent_name(agent, color)}  provider={provider}  "
            f"status={status}  session={sid}…"
        )
        lines.append(_truncate_ansi(line, width))
    return "\n".join(lines)


def render_monitor_frame(
    *,
    tasks: list[dict],
    messages: list[dict],
    sessions: list[dict] | None = None,
    color: bool = True,
    width: int = 100,
    interval: float = 2,
    stamp: str = "",
    include_logo: bool = True,
) -> str:
    """One full-screen monitor frame: logo + tasks + board + sessions."""
    chunks: list[str] = []
    if include_logo:
        chunks.append(splash_text(width=width, color=color))
        chunks.append("")
    chunks.append(render_task_board(tasks, color=color, width=width))
    chunks.append("")
    chunks.append(
        render_message_board(messages, color=color, width=width, limit=12)
    )
    if sessions is not None:
        chunks.append("")
        chunks.append(
            render_sessions_strip(sessions, color=color, width=width)
        )
    footer = _paint(
        f"refresh {interval:g}s · Ctrl-C to exit · {stamp}",
        GREY,
        color,
    )
    chunks.append("")
    chunks.append(footer)
    return "\n".join(chunks)


def begin_live_display(stdout: TextIO = sys.stdout) -> bool:
    """Enter alternate screen + hide cursor when VT is available."""
    if not enable_vt(stdout):
        return False
    stdout.write(ENTER_ALT_SCREEN + HIDE_CURSOR + CLEAR_HOME)
    stdout.flush()
    return True


def paint_live_display(
    frame: str,
    stdout: TextIO = sys.stdout,
    *,
    caret_cell: tuple[int, int] | None = None,
) -> None:
    """Overwrite the live surface in place (no scrolling transcript blocks).

    When ``caret_cell`` is ``(row, col)`` (1-based), park a *steady* thin
    violet bar cursor there — same between-letter line as a normal bar
    caret, with blink forced off (DECSCUSR 6 + CSI ?12l). The frame itself
    holds plain text only (no reverse highlight, no painted glyph).
    """
    parts = [HOME, CLEAR_EOS, frame]
    if caret_cell is not None:
        row, col = caret_cell
        parts.extend((
            HIDE_CURSOR,            # avoid a flash mid-repaint
            _CURSOR_COLOR_VIOLET,
            STEADY_BAR_CURSOR,      # vertical bar shape
            CURSOR_BLINK_OFF,       # solid — hosts that ignore 6 alone
            SHOW_CURSOR,
            _cursor_to(int(row), int(col)),
        ))
    else:
        parts.append(HIDE_CURSOR)
    stdout.write("".join(parts))
    stdout.flush()


def end_live_display(stdout: TextIO = sys.stdout, *, used_alt: bool) -> None:
    # Restore default cursor shape, blink, and colour for the parent shell.
    restore = (
        _CURSOR_COLOR_RESET
        + DEFAULT_CURSOR
        + CURSOR_BLINK_ON
        + SHOW_CURSOR
    )
    if used_alt:
        stdout.write(restore + LEAVE_ALT_SCREEN + RESET)
    else:
        stdout.write(restore + RESET + "\n")
    stdout.flush()


# --- dashboard v2 (pure renderers; string in → string out) -------------------

DASHBOARD_SPLIT_MIN_WIDTH = 110
FOOTER_HINTS = ("↑↓ select · ←→ pan title · esc clear/deselect · tab focus · "
                "pgup/pgdn scroll · ^o dir · ^c quit")
REVERSE_ON, REVERSE_OFF = f"{ESC}7m", f"{ESC}27m"


def _input_body(input_buffer: str) -> str:
    """Plain `> {buffer}` string used for wrap + caret-column math."""
    return f"> {input_buffer}"


def _render_input_lines(
    input_buffer: str, *, width: int, color: bool,
) -> list[str]:
    """Wrap the `> …` input field. Plain text only — the live display parks
    the real terminal bar cursor on the insert cell after each paint."""
    body = _input_body(input_buffer)
    step = max(1, width)
    chunks = [body[i:i + step]
              for i in range(0, len(body), step)] or [body]
    painted: list[str] = []
    for index, chunk in enumerate(chunks):
        if index == 0 and chunk.startswith(">"):
            chunk = _paint(">", VIOLET, color, bold=True) + chunk[1:]
        painted.append(chunk)
    return painted


def input_caret_offset(input_buffer: str, caret_at: int) -> int:
    """0-based insert index into `_input_body` (between-letter bar position)."""
    caret_at = max(0, min(int(caret_at), len(input_buffer)))
    return len("> ") + caret_at


def input_caret_line_col(
    input_buffer: str, caret_at: int, *, width: int,
) -> tuple[int, int]:
    """Line offset (0-based within the input field) and 1-based column."""
    width = max(1, width)
    abs0 = input_caret_offset(input_buffer, caret_at)
    return abs0 // width, (abs0 % width) + 1


def _pad_visible(line: str, width: int) -> str:
    line = _truncate_ansi(line, width)
    return line + " " * max(0, width - _visible_len(line))


def split_panes(
    left_lines: list[str],
    right_lines: list[str],
    *,
    width: int,
    height: int,
    gap: str = "│",
) -> list[str]:
    """Join two line lists into side-by-side panes of exact visible width."""
    left_width = max(1, (width - 3) // 2)
    right_width = max(1, width - 3 - left_width)
    rows: list[str] = []
    for index in range(height):
        left = left_lines[index] if index < len(left_lines) else ""
        right = right_lines[index] if index < len(right_lines) else ""
        rows.append(
            _pad_visible(left, left_width)
            + f" {gap} "
            + _pad_visible(right, right_width)
        )
    return rows


def render_feed(
    messages: list[dict],
    *,
    width: int,
    height: int,
    color: bool,
) -> list[str]:
    """Chat-style board feed: `name: body`, wrapped, newest at the bottom."""
    lines: list[str] = [_section("board", color)]
    if not messages:
        lines.append(_paint("  (no messages)", GREY, color))
    for msg in messages:
        who = str(msg.get("from_agent") or "?")
        body = " ".join(str(msg.get("body") or "").split())
        wrap_width = max(8, width - 2)
        wrapped = textwrap.wrap(
            f"{who}: {body}", width=wrap_width,
            subsequent_indent="  ") or [f"{who}: "]
        first = wrapped[0]
        if first.startswith(f"{who}:"):
            first = _agent_name(who, color) + first[len(who):]
        lines.append("  " + first)
        lines.extend("  " + cont for cont in wrapped[1:])
    if height is not None and len(lines) > height:
        lines = [lines[0]] + lines[-(height - 1):]
    return lines


def _hscroll_plain(line: str, width: int, offset: int) -> str:
    """One-line horizontal pan for a colourless task row (selected highlight).

    Keeps each task on a single line: long titles are not wrapped; `offset`
    slides the window right so ←/→ can reveal the rest.
    """
    if width <= 0:
        return ""
    # Selected rows are built without ANSI so char offsets match screen cells
    # for the Latin/BMP titles we show (whitespace already collapsed).
    max_off = max(0, len(line) - width)
    off = min(max(0, offset), max_off)
    return _pad_visible(line[off:], width)


def render_task_lines(
    tasks: list[dict],
    *,
    cursor: int | None,
    width: int,
    color: bool,
    hscroll: int = 0,
    blink_on: bool = True,
) -> list[str]:
    """One line per task; the selected row is a full-width colour highlight
    (reverse video) — plain renders mark it with a leading `>` instead.
    `hscroll` pans only the selected row so long titles stay one line."""
    lines: list[str] = []
    for index, task in enumerate(tasks):
        selected = cursor is not None and index == cursor
        # The highlighted row is built colourless so embedded RESETs can't
        # cancel the reverse-video attribute mid-row.
        row_color = color and not selected
        owner = task.get("owner") or "-"
        next_actor = task.get("next_actor") or "-"
        labels = task.get("labels") or []
        label_bit = f"  [{','.join(labels)}]" if labels else ""
        claim = task.get("claim")
        claim_bit = ""
        if claim:
            claim_bit = (
                f"  claim:{_agent_name(claim.get('agent') or '?', row_color)}")
        title = " ".join(str(task.get("title") or "").split())
        marker = "> " if selected and not color else "  "
        # a task worked by a LIVE session blinks its number ~1s; a claim held
        # by a dead/exited session is a zombie and must not read as active work
        num = f"#{task.get('item_id', '?')}"
        if claim and claim.get("session_live") and not blink_on:
            num = " " * len(num)
        line = (
            f"{marker}{num} [{task.get('status', '?')}] "
            f"{title}{label_bit}  "
            f"{_agent_name(owner, row_color) if owner != '-' else owner}→"
            f"{_agent_name(next_actor, row_color) if next_actor != '-' else next_actor}"
            f"{claim_bit}"
        )
        if selected:
            line = _hscroll_plain(line, width, hscroll)
            if color:
                line = REVERSE_ON + line + REVERSE_OFF
        else:
            line = _truncate_ansi(line, width)
        lines.append(line)
    if not tasks:
        lines.append(_paint("  (no tasks)", GREY, color))
    return lines


def conversation_max_scroll(
    entries: list[dict] | None,
    *,
    width: int,
    height: int,
) -> int:
    """Largest convo_scroll that still moves the window (top of history).

    Matches the wrap rules in `render_conversation` so key handling and
    paint share one ceiling — no phantom overscroll past the top.
    """
    wrap_width = max(8, width - 2)
    body_len = 0
    for entry in entries or []:
        if entry.get("kind") == "milestone":
            chunks = textwrap.wrap(
                "· " + str(entry.get("text") or ""), width=wrap_width,
                subsequent_indent="  ") or ["·"]
            body_len += len(chunks)
            continue
        who = str(entry.get("who") or "?")
        text = " ".join(str(entry.get("body") or "").split())
        wrapped = textwrap.wrap(
            f"{who}: {text}", width=wrap_width,
            subsequent_indent="  ") or [f"{who}: "]
        body_len += len(wrapped)
    if body_len == 0:
        body_len = 1  # empty-hint line
    avail = max(1, height - 1)
    return max(0, body_len - avail)


def render_conversation(
    entries: list[dict] | None,
    *,
    selected: dict | None,
    width: int,
    height: int,
    color: bool,
    scroll: int = 0,
    scroll_info: list | None = None,
) -> list[str]:
    """The selected task's record (messages + dim `·` protocol milestones),
    or — with no task selected — the board-wide feed, so an agent's
    board-wide notes are never invisible. Chronological, wrapped, tail-pinned.

    If `scroll_info` is provided, it is set to ``[clamped_scroll, max_scroll]``
    so the dashboard can write the real ceiling back into key state.
    """
    if selected is None:
        head = _pane_title("LIVE CHAT · board", color, False)
        empty_hint = "  (no board messages)"
    else:
        title = " ".join(str(selected.get("title") or "").split())
        head = _pane_title(
            f"LIVE CHAT · #{selected.get('item_id', '?')} {title}",
            color, False)
        empty_hint = "  (no conversation yet)"
    body: list[str] = []
    wrap_width = max(8, width - 2)
    for entry in entries or []:
        if entry.get("kind") == "milestone":
            for chunk in textwrap.wrap(
                    "· " + str(entry.get("text") or ""), width=wrap_width,
                    subsequent_indent="  ") or ["·"]:
                body.append(_paint("  " + chunk, GREY, color))
            continue
        who = str(entry.get("who") or "?")
        text = " ".join(str(entry.get("body") or "").split())
        wrapped = textwrap.wrap(
            f"{who}: {text}", width=wrap_width,
            subsequent_indent="  ") or [f"{who}: "]
        first = wrapped[0]
        if first.startswith(f"{who}:"):
            first = _agent_name(who, color) + first[len(who):]
        body.append("  " + first)
        body.extend("  " + cont for cont in wrapped[1:])
    if not body:
        body = [_paint(empty_hint, GREY, color)]
    avail = max(1, height - 1)
    max_scroll = max(0, len(body) - avail)
    offset = min(max(0, scroll), max_scroll)
    if scroll_info is not None:
        scroll_info.clear()
        scroll_info.extend([offset, max_scroll])
    start = max(0, len(body) - avail - offset)
    return [head] + body[start:start + avail]


def render_board_switcher(
    boards: list[str],
    *,
    cursor: int,
    width: int,
    height: int,
    color: bool,
) -> list[str]:
    """The ^o overlay: known folders/boards, freshest first, arrow-selectable.

    The list is a fixed-height viewport that **tracks the cursor** (same idea
    as `_windowed_tasks`). Older code sliced ``boards[:height-2]`` only, so
    moving the highlight past the first screenful walked it off-screen while
    the folder list stayed stuck at the top.
    """
    footer = _paint(
        "  ⏎ open selected · type a path + ⏎ add · esc cancel", GREY, color)
    # One reserved row for the footer; remaining rows are the scroll window.
    avail = max(1, height - 1)
    if not boards:
        return [_paint("  (no known boards yet)", GREY, color), footer][:height]

    n = len(boards)
    cur = max(0, min(int(cursor), n - 1))
    if n <= avail:
        start = 0
    else:
        # Keep the selection on-screen: prefer near the bottom of the window
        # when walking down (cursor - avail + 1), clamp to the last page.
        start = min(max(0, cur - avail + 1), n - avail)

    lines: list[str] = []
    for index in range(start, min(n, start + avail)):
        path = boards[index]
        line = _truncate_ansi(f"  {coopdb.display_board_path(path)}", width)
        if index == cur:
            if color:
                line = REVERSE_ON + _pad_visible(line, width) + REVERSE_OFF
            else:
                line = "> " + line[2:]
        lines.append(line)
    lines.append(footer)
    return lines


# Canonical seats on the dashboard agents line — always listed, offline
# as name- so the board never collapses to "no live sessions".
PREFERRED_DASHBOARD_AGENTS = ("claude", "codex", "grok")


def _sessions_inline(sessions: list[dict], color: bool, *,
                     active: frozenset[str] = frozenset(),
                     blink_on: bool = True) -> str:
    """Legacy: only running sessions (empty → 'no live sessions').

    Dashboard v3 uses `_agents_inline` instead so offline seats stay visible.
    """
    if not sessions:
        return _paint("no live sessions", GREY, color)
    dots = []
    for s in sessions:
        name = str(s.get("agent_id") or "?")
        # the active agent's dot blinks ~1s on / 1s off (blank on the off-beat)
        dot = " " if (name in active and not blink_on) else "●"
        dots.append(_agent_name(name, color) + dot)
    return " ".join(dots)


def _agents_inline(sessions: list[dict], color: bool, *,
                   active: frozenset[str] = frozenset(),
                   blink_on: bool = True) -> str:
    """Always-visible trio + any extra live seats.

    Markers:
      name●           online (running session), no active claim
      name● blinking  online and holds an unexpired active claim
      name-           offline (no running session) — still listed
    """
    online: list[str] = []
    seen: set[str] = set()
    for s in sessions:
        name = str(s.get("agent_id") or "?").strip() or "?"
        if name not in seen:
            seen.add(name)
            online.append(name)

    online_set = set(online)
    parts: list[str] = []

    def _seat(name: str, *, is_online: bool) -> str:
        label = _agent_name(name, color)
        if not is_online:
            return label + "-"
        # online: solid ●, or blank the glyph on the blink off-beat when active
        if name in active and not blink_on:
            return label + " "
        return label + "●"

    for name in PREFERRED_DASHBOARD_AGENTS:
        parts.append(_seat(name, is_online=name in online_set))
    for name in online:
        if name not in PREFERRED_DASHBOARD_AGENTS:
            parts.append(_seat(name, is_online=True))
    return " ".join(parts)


def dashboard_split_col(width: int) -> int | None:
    """1-based column of the pane gap in a wide frame; None when stacked."""
    if width < DASHBOARD_SPLIT_MIN_WIDTH:
        return None
    return max(1, (width - 3) // 2) + 2


def dashboard_body_height(
    *, height: int, width: int, input_buffer: str = "",
) -> int:
    """Rows available for TASKS/LIVE CHAT or the DIR switcher overlay.

    Mirrors the header/bottom accounting in `render_dashboard` so the
    switcher can window to the same height the frame will actually paint
    (instead of a rough ``lines - 8`` guess that could still clip the
    highlight after the dashboard truncates the overlay).
    """
    if height >= 28:
        header_n = len(_wordmark_lines(False, width)) + 2  # DIR strip + blank
    else:
        header_n = 3  # compact wordmark + DIR strip + blank
    input_n = len(_render_input_lines(
        input_buffer, width=max(1, width), color=False))
    # rule · input · rule · agents · hints
    bottom_n = 1 + input_n + 1 + 1 + 1
    return max(1, int(height) - header_n - bottom_n)


def _pane_title(title: str, color: bool, focused: bool) -> str:
    """v3 pane heading — clean caps; a violet ▌ bar marks keyboard focus
    (shown in both the split and stacked layouts, colour or plain)."""
    if focused:
        return _paint(f"▌ {title}", VIOLET, color, bold=True)
    return _paint(f"  {title}", GREY, color, bold=True)


def _convo_title(selected: dict | None, color: bool, focused: bool) -> str:
    """The LIVE CHAT pane heading; names the task once one is selected, else
    the board-wide feed."""
    if selected is not None:
        title = ("LIVE CHAT · #" + str(selected.get("item_id", "?")) + " "
                 + " ".join(str(selected.get("title") or "").split()))
    else:
        title = "LIVE CHAT · board"
    return _pane_title(title, color, focused)


def _windowed_tasks(
    tasks: list[dict], cursor: int | None, width: int, height: int,
    color: bool, hscroll: int = 0, blink_on: bool = True,
) -> list[str]:
    lines = render_task_lines(
        tasks, cursor=cursor, width=width, color=color, hscroll=hscroll,
        blink_on=blink_on)
    avail = max(1, height)
    if cursor is None or len(lines) <= avail:
        return lines[:avail]
    start = min(max(0, cursor - avail + 1), max(0, len(lines) - avail))
    return lines[start:start + avail]


def render_dashboard(
    *,
    tasks: list[dict],
    conversation: list[dict] | None,
    selected: dict | None,
    sessions: list[dict],
    cursor: int | None,
    width: int,
    height: int,
    color: bool,
    board_label: str,
    board_path: str,
    input_buffer: str = "",
    input_cursor: int | None = None,
    notice: str = "",
    focus: str = "tasks",
    convo_scroll: int = 0,
    task_hscroll: int = 0,
    stamp: str = "",
    overlay_lines: list[str] | None = None,
    active: frozenset[str] = frozenset(),
    blink_on: bool = True,
    convo_scroll_info: list | None = None,
    input_caret_cell: list | None = None,
) -> str:
    """One height-exact v3 frame: header · tasks|conversation · input +
    always-visible agents line (claude/codex/grok seats + extras)."""
    header: list[str] = []
    if height >= 28:
        header.extend(_wordmark_lines(color, width))
    else:
        # Short panes (height < 28) drop the multi-line mark; same palette as splash compact.
        header.append(_compact_wordmark(color))
    # Layer 1 (repo/dir) — labelled strip; place name is ochre from the
    # CLI five-colour palette (unused by agents/focus) so e.g.
    # _multi-provider-coordination does not collide with coral/blue/grey/violet.
    # The DIR label goes violet only while the ^O dir menu is open
    # (overlay shown) — the same "this has the keys" accent focused panes use;
    # grey otherwise.
    dir_color = VIOLET if overlay_lines is not None else GREY
    header.append(_truncate_ansi(
        _paint("DIR  ", dir_color, color, bold=True)
        + _paint(board_label, OCHRE, color, bold=True)
        + _paint("   ^O switch", GREY, color), width))
    header.append("")

    # Enter depends on focus: TASKS creates and runs a goal-task, LIVE CHAT
    # posts a message, and the DIR switcher adds a board path.
    # Violet accent matches the focused pane / open dir menu cue.
    if overlay_lines is not None:
        enter_hint = "⏎ new path"
    elif focus == "tasks":
        enter_hint = "⏎ create + run"
    else:
        enter_hint = "⏎ message"
    hint_tail = f" · {FOOTER_HINTS}"
    if stamp:
        hint_tail = f"{hint_tail} · {stamp}"
    hints = (
        (_paint(f"{notice} · ", GREY, color) if notice else "")
        + _paint(enter_hint, VIOLET, color, bold=True)
        + _paint(hint_tail, GREY, color)
    )
    # The input wraps across lines so long text never runs off the right
    # edge. Text is plain; the live display parks a steady thin bar cursor
    # on the insert cell (see input_caret_cell) — no reverse, no glyph.
    caret_at = (
        len(input_buffer)
        if input_cursor is None
        else max(0, min(int(input_cursor), len(input_buffer)))
    )
    input_lines = _render_input_lines(
        input_buffer, width=width, color=color)
    # Grey rules frame the input field above and below.
    rule = _paint("─" * max(1, width), GREY, color)
    # Bottom agents line: always the preferred trio (offline as name-),
    # plus any extra live seats; never "no live sessions".
    agents_line = _paint("agents: ", GREY, color) + _agents_inline(
        sessions, color, active=active, blink_on=blink_on)
    bottom = (
        [_truncate_ansi(rule, width)]
        + [_truncate_ansi(line, width) for line in input_lines]
        + [_truncate_ansi(rule, width)]
        + [
            _truncate_ansi(agents_line, width),
            _truncate_ansi(hints, width),
        ]
    )
    body_height = max(1, height - len(header) - len(bottom))

    if overlay_lines is not None:
        body = [_pad_visible(line, width) for line in overlay_lines]
        body = body[:body_height] + [""] * (body_height - len(body))
    elif width >= DASHBOARD_SPLIT_MIN_WIDTH:
        left_width = max(1, (width - 3) // 2)
        right_width = max(1, width - 3 - left_width)
        left = [_pane_title("TASKS", color,
                            focus == "tasks")] + _windowed_tasks(
            tasks, cursor, left_width, body_height - 1, color,
            hscroll=task_hscroll, blink_on=blink_on)
        right = render_conversation(
            conversation, selected=selected, width=right_width,
            height=body_height, color=color, scroll=convo_scroll,
            scroll_info=convo_scroll_info)
        # The LIVE CHAT heading always reflects keyboard focus.
        right[0] = _convo_title(selected, color, focus == "convo")
        body = split_panes(left, right, width=width, height=body_height)
    else:
        tasks_height = max(3, body_height // 2)
        convo_height = max(2, body_height - tasks_height)
        stacked = [_pane_title("TASKS", color,
                               focus == "tasks")] + _windowed_tasks(
            tasks, cursor, width, tasks_height - 1, color,
            hscroll=task_hscroll, blink_on=blink_on)
        stacked = stacked[:tasks_height]
        stacked += [""] * (tasks_height - len(stacked))
        convo_lines = render_conversation(
            conversation, selected=selected, width=width,
            height=convo_height, color=color, scroll=convo_scroll,
            scroll_info=convo_scroll_info)
        # Same focus rule as the split layout — the highlight must show here.
        convo_lines[0] = _convo_title(selected, color, focus == "convo")
        stacked += convo_lines
        body = [_pad_visible(line, width) for line in stacked]
        body = body[:body_height] + [""] * (body_height - len(body))

    lines = header + body[:body_height] + bottom
    lines += [""] * (height - len(lines))
    if input_caret_cell is not None:
        # Top grey rule is bottom[0]; input starts on the next frame row.
        input_start_row = len(header) + body_height + 2  # 1-based terminal row
        line_off, col = input_caret_line_col(
            input_buffer, caret_at, width=width)
        # Clamp to the painted input block if wrap math races height budget.
        max_off = max(0, len(input_lines) - 1)
        line_off = min(line_off, max_off)
        input_caret_cell.clear()
        input_caret_cell.extend((input_start_row + line_off, col))
    return "\n".join(lines[:height])


def _detail_row(label: str, value, color: bool, width: int) -> str:
    text = " ".join(str(value or "").split())
    return _truncate_ansi(
        "  " + _paint(f"{label}: ", GREY, color) + text, width)


def render_task_detail(
    *,
    item: dict,
    history: dict,
    item_messages: list[dict],
    width: int,
    height: int,
    color: bool,
) -> str:
    """One task's world: contract summary, protocol history, messages."""
    lines: list[str] = []
    title = " ".join(str(item.get("title") or "").split())
    lines.append(_truncate_ansi(
        _paint(f"#{item.get('item_id', '?')} {title} "
               f"[{item.get('status', '?')}]", CORAL, color, bold=True),
        width))
    for key in ("objective", "scope", "done_when", "output_contract"):
        if item.get(key):
            lines.append(_detail_row(key, item[key], color, width))

    def section(name: str, rows: list, render_row) -> None:
        if not rows:
            return
        lines.append("")
        lines.append(_section(name, color))
        for row in rows:
            lines.append(_truncate_ansi("  " + render_row(row), width))

    section("questions", history.get("questions") or [], lambda q: (
        f"q#{q.get('question_id')} {q.get('exact_question')} "
        f"→ {q.get('answer') if q.get('status') == 'answered' else q.get('status')}"))
    section("reviews", history.get("reviews") or [], lambda r: (
        f"review#{r.get('id')} [{r.get('status')}] "
        f"reviewer={r.get('reviewer') or r.get('reviewer_agent_id') or 'any'}"))
    section("handoffs", history.get("handoffs") or [], lambda h: (
        f"handoff#{h.get('handoff_id')} [{h.get('status')}] "
        f"{h.get('from_agent')}→{h.get('to_agent')}: {h.get('reason')}"))
    section("decisions", history.get("decisions") or [], lambda d: (
        f"decision#{d.get('id')} {d.get('text')}"))
    section("receipts", history.get("receipts") or [], lambda r: (
        f"receipt#{r.get('receipt_id')} {str(r.get('sha256') or '')[:10]} "
        f"{r.get('summary')}"
        + (f"  [{r.get('evidence_marker')}]" if r.get("evidence_marker") else "")
        + ("  (superseded)" if r.get("superseded_at") else "")))
    section("debates", history.get("debates") or [], lambda d: (
        f"debate#{d.get('id')} [{d.get('status')}] {d.get('topic')}"))
    section("messages", item_messages or [], lambda m: (
        f"{m.get('from_agent')}: "
        + " ".join(str(m.get('body') or '').split())))

    if len(lines) > height:
        kept = lines[:height - 1]
        kept.append(_paint(f"  … ({len(lines) - len(kept)} more)", GREY, color))
        lines = kept
    return "\n".join(lines)
