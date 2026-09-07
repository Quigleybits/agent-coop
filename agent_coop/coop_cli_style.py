"""TTY-only argparse syntax colours for the Agent_coop CLI."""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import TextIO


DEEP_BLUE = "\033[38;2;23;30;166m"
OCHRE = "\033[38;2;217;149;24m"
CORAL = "\033[38;2;217;114;91m"
OFF_WHITE = "\033[38;2;242;242;242m"
PALE_CYAN = "\033[38;2;204;239;240m"
BOLD = "\033[1m"
RESET = "\033[0m"

ANSI_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
TOKEN_PATTERN = re.compile(
    r"usage:|--?[A-Za-z0-9][A-Za-z0-9_-]*|"
    r"[A-Za-z_][A-Za-z0-9_-]*|\.\.\.|[][{}(),]"
)
SIGNATURE_ROW = re.compile(r"^(\s{2,4})(\S.*?)(\s{2,})(\S.*)$")


def strip_ansi(text: str) -> str:
    return ANSI_PATTERN.sub("", text)


def should_color(stream: TextIO) -> bool:
    return "NO_COLOR" not in os.environ and bool(
        getattr(stream, "isatty", lambda: False)()
    )


def _paint(text: str, color: str, *, bold: bool = False) -> str:
    weight = BOLD if bold else ""
    return f"{weight}{color}{text}{RESET}"


def _parser_roles(parser: argparse.ArgumentParser) -> dict[str, set[str]]:
    program = parser.prog.split()
    executable = {program[0]} if program else {"coop"}
    commands = set(program[1:])
    options: set[str] = set()
    values: set[str] = set()

    for action in parser._actions:
        options.update(action.option_strings)
        if isinstance(action, argparse._SubParsersAction):
            commands.update(action.choices)
            continue

        metavar = action.metavar
        if isinstance(metavar, tuple):
            values.update(str(value) for value in metavar)
        elif metavar is not None:
            values.add(str(metavar))
        elif action.option_strings and action.nargs != 0:
            values.add(action.dest.upper())
        elif not action.option_strings:
            values.add(action.dest)

    return {
        "executable": executable,
        "commands": commands,
        "options": options,
        "values": values,
    }


def _style_syntax(fragment: str, parser: argparse.ArgumentParser) -> str:
    roles = _parser_roles(parser)

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token == "usage:":
            return _paint(token, PALE_CYAN)
        if token in roles["executable"]:
            return _paint(token, OCHRE)
        if token in roles["commands"]:
            return _paint(token, CORAL)
        if token in roles["options"] or token.startswith("-"):
            return _paint(token, PALE_CYAN)
        if token in "[]{}()," or token == "...":
            return _paint(token, DEEP_BLUE, bold=True)
        if token in roles["values"] or token.isupper():
            return _paint(token, OFF_WHITE)
        return _paint(token, OFF_WHITE)

    return TOKEN_PATTERN.sub(replace, fragment)


def _split_ending(line: str) -> tuple[str, str]:
    content = line.rstrip("\r\n")
    return content, line[len(content) :]


def style_help(text: str, parser: argparse.ArgumentParser) -> str:
    """Colour syntax-bearing help spans after argparse has wrapped them."""
    styled: list[str] = []
    in_usage = False

    for raw_line in text.splitlines(keepends=True):
        line, ending = _split_ending(raw_line)
        stripped = line.lstrip()

        if line.startswith("usage:"):
            in_usage = True
            styled.append(_style_syntax(line, parser) + ending)
            continue
        if in_usage and line.startswith(" ") and stripped:
            styled.append(_style_syntax(line, parser) + ending)
            continue
        if not stripped:
            in_usage = False
            styled.append(raw_line)
            continue
        in_usage = False

        if stripped.startswith("$ coop") or stripped.startswith("coop "):
            indent = line[: len(line) - len(stripped)]
            styled.append(indent + _style_syntax(stripped, parser) + ending)
            continue

        signature = SIGNATURE_ROW.match(line)
        if signature:
            indent, syntax, gap, description = signature.groups()
            styled.append(
                indent
                + _style_syntax(syntax, parser)
                + gap
                + _paint(description, OFF_WHITE)
                + ending
            )
            continue

        if 2 <= len(line) - len(stripped) <= 4:
            styled.append(
                line[: len(line) - len(stripped)]
                + _style_syntax(stripped, parser)
                + ending
            )
            continue

        styled.append(raw_line)

    return "".join(styled)


class CoopArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that colours printed syntax without changing formats."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if hasattr(self, "color"):
            self.color = False

    def _styled(self, text: str, file: TextIO) -> str:
        if not should_color(file):
            return text
        try:
            return style_help(text, self)
        except Exception:
            return text

    def print_usage(self, file: TextIO | None = None) -> None:
        if file is None:
            file = sys.stdout
        self._print_message(self._styled(self.format_usage(), file), file)

    def print_help(self, file: TextIO | None = None) -> None:
        if file is None:
            file = sys.stdout
        self._print_message(self._styled(self.format_help(), file), file)

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        if should_color(sys.stderr):
            try:
                program = _style_syntax(self.prog, self)
                rendered = (
                    f"{program}: {_paint('error:', CORAL)} "
                    f"{_paint(message, OFF_WHITE)}\n"
                )
            except Exception:
                rendered = f"{self.prog}: error: {message}\n"
        else:
            rendered = f"{self.prog}: error: {message}\n"
        self.exit(2, rendered)
