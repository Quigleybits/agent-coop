import argparse
import contextlib
import importlib
import importlib.util
import io
import os
import unittest
from unittest import mock


class TTY(io.StringIO):
    def isatty(self):
        return True


def load_style(testcase):
    spec = importlib.util.find_spec("agent_coop.coop_cli_style")
    testcase.assertIsNotNone(spec, "coop_cli_style must be implemented")
    return importlib.import_module("agent_coop.coop_cli_style")


def build_assign_parser(style):
    parser = style.CoopArgumentParser(prog="coop")
    sub = parser.add_subparsers(dest="cmd", required=True)
    assign = sub.add_parser("assign")
    assign.add_argument("id", type=int)
    assign.add_argument("--to", required=True, help="send work to provider")
    assign.add_argument("--role", default="owner", help="assignment role")
    return parser, assign


def get_subparser(parser, *path):
    current = parser
    for command in path:
        action = next(
            item
            for item in current._actions
            if isinstance(item, argparse._SubParsersAction)
        )
        current = action.choices[command]
    return current


class TestCliStyle(unittest.TestCase):

    def setUp(self):
        # The host shell may export NO_COLOR (it disables colour in
        # coop_cli_style regardless of the TTY). These tests decide colour
        # through explicit TTY fakes and explicit NO_COLOR patches, so start
        # every test from an environment without it.
        patcher = mock.patch.dict(os.environ, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("NO_COLOR", None)
    @unittest.skipUnless(
        importlib.util.find_spec("_colorize"),
        "Python argparse color support is unavailable",
    )
    def test_python_argparse_palette_cannot_override_coop_palette(self):
        style = load_style(self)
        _parser, assign = build_assign_parser(style)
        out = TTY()

        with mock.patch("_colorize.can_colorize", return_value=True):
            assign.print_usage(file=out)

        rendered = out.getvalue()
        self.assertIn(
            f"{style.OCHRE}coop{style.RESET} "
            f"{style.CORAL}assign{style.RESET}",
            rendered,
        )
        self.assertIn(f"{style.OFF_WHITE}id{style.RESET}", rendered)
        self.assertNotIn("\033[1;35mcoop assign", rendered)
        self.assertNotIn("\033[32mid", rendered)

    def test_tty_assign_usage_uses_approved_roles_and_preserves_plain_text(self):
        style = load_style(self)
        _parser, assign = build_assign_parser(style)
        out = TTY()
        assign.print_usage(file=out)
        rendered = out.getvalue()

        self.assertIn(f"{style.PALE_CYAN}usage:{style.RESET}", rendered)
        self.assertIn(f"{style.OCHRE}coop{style.RESET}", rendered)
        self.assertIn(f"{style.CORAL}assign{style.RESET}", rendered)
        self.assertIn(f"{style.PALE_CYAN}--to{style.RESET}", rendered)
        self.assertIn(f"{style.OFF_WHITE}TO{style.RESET}", rendered)
        self.assertIn(f"{style.OFF_WHITE}id{style.RESET}", rendered)
        self.assertIn(
            f"{style.BOLD}{style.DEEP_BLUE}[{style.RESET}", rendered
        )
        self.assertEqual(assign.format_usage(), style.strip_ansi(rendered))

    def test_help_styles_signatures_without_tokenizing_description_prose(self):
        style = load_style(self)
        _parser, assign = build_assign_parser(style)
        out = TTY()
        assign.print_help(file=out)
        rendered = out.getvalue()

        self.assertIn(f"{style.PALE_CYAN}--to{style.RESET}", rendered)
        self.assertIn(
            f"{style.OFF_WHITE}send work to provider{style.RESET}", rendered
        )
        self.assertNotIn(f"{style.CORAL}provider{style.RESET}", rendered)
        self.assertEqual(assign.format_help(), style.strip_ansi(rendered))

    def test_redirected_and_programmatic_help_stay_plain(self):
        style = load_style(self)
        _parser, assign = build_assign_parser(style)
        out = io.StringIO()
        assign.print_help(file=out)
        self.assertNotIn("\033[", out.getvalue())
        self.assertNotIn("\033[", assign.format_help())
        self.assertEqual(assign.format_help(), out.getvalue())

    def test_no_color_disables_tty_ansi(self):
        style = load_style(self)
        _parser, assign = build_assign_parser(style)
        out = TTY()
        with mock.patch.dict(os.environ, {"NO_COLOR": ""}, clear=False):
            assign.print_usage(file=out)
        self.assertNotIn("\033[", out.getvalue())
        self.assertEqual(assign.format_usage(), out.getvalue())


class TestCoopParserIntegration(unittest.TestCase):

    def setUp(self):
        # The host shell may export NO_COLOR (it disables colour in
        # coop_cli_style regardless of the TTY). These tests decide colour
        # through explicit TTY fakes and explicit NO_COLOR patches, so start
        # every test from an environment without it.
        patcher = mock.patch.dict(os.environ, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("NO_COLOR", None)
    def test_root_usage_does_not_list_or_accept_splash(self):
        style = load_style(self)
        from agent_coop import cli as coop

        parser = coop.build_parser()
        self.assertNotIn("splash", parser.format_usage())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                parser.parse_args(["splash"])
        self.assertEqual(2, raised.exception.code)
        self.assertNotIn("splash", parser.format_usage())
        self.assertNotIn("\033[", err.getvalue())

    def test_real_root_and_subcommand_parsers_use_the_style(self):
        # The pre-release `assign` / `item done` parsers are retired, so the
        # style proof targets surviving commands; nested subcommand groups
        # are covered by the `item` family.
        style = load_style(self)
        from agent_coop import cli as coop

        parser = coop.build_parser()
        self.assertIsInstance(parser, style.CoopArgumentParser)
        say = get_subparser(parser, "say")
        migrate = get_subparser(parser, "migrate")
        self.assertIsInstance(say, style.CoopArgumentParser)
        self.assertIsInstance(migrate, style.CoopArgumentParser)

        say_out = TTY()
        say.print_usage(file=say_out)
        self.assertEqual(
            say.format_usage(), style.strip_ansi(say_out.getvalue())
        )
        self.assertIn(f"{style.CORAL}say{style.RESET}", say_out.getvalue())

        migrate_out = TTY()
        migrate.print_help(file=migrate_out)
        self.assertEqual(
            migrate.format_help(), style.strip_ansi(migrate_out.getvalue())
        )
        self.assertIn(
            f"{style.CORAL}migrate{style.RESET}", migrate_out.getvalue()
        )

    def test_real_say_error_is_colored_and_still_exits_two(self):
        style = load_style(self)
        from agent_coop import cli as coop

        parser = coop.build_parser()
        err = TTY()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                parser.parse_args(["say"])

        rendered = err.getvalue()
        self.assertEqual(2, raised.exception.code)
        self.assertIn(f"{style.PALE_CYAN}usage:{style.RESET}", rendered)
        self.assertIn(f"{style.OCHRE}coop{style.RESET}", rendered)
        self.assertIn(f"{style.CORAL}say{style.RESET}", rendered)
        self.assertIn(f"{style.CORAL}error:{style.RESET}", rendered)
        self.assertIn(
            f"{style.OFF_WHITE}the following arguments are required: "
            f"body{style.RESET}",
            rendered,
        )
        plain = style.strip_ansi(rendered)
        self.assertIn(
            "usage: coop say [-h] [--room ROOM] [--to TO] [--item ITEM] body",
            plain,
        )
        self.assertIn(
            "coop say: error: the following arguments are required: body",
            plain,
        )


if __name__ == "__main__":
    unittest.main()
