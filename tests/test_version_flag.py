"""`coop --version` reports the installed package version and exits 0."""
import io
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stdout

from agent_coop import cli


class TestVersionFlag(unittest.TestCase):
    def test_parser_version_action_prints_and_exits_zero(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            cli.build_parser().parse_args(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertRegex(out.getvalue().strip(), r"^coop \d+\.\d+\.\d+")

    def test_module_entrypoint_reports_the_same_version(self):
        proc = subprocess.run(
            [sys.executable, "-m", "agent_coop", "--version"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), f"coop {cli.package_version()}")
        self.assertNotEqual(cli.package_version(), "0+unknown")
