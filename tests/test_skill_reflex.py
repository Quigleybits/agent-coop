"""Recovery reflex: SKILL.md prose stays bound to the live rejection taxonomy.

The reflex is agent guidance, not a second state machine — these tests pin
its section to the code-owned vocabulary (coop_errors registries and the
next_action grammar) so prose cannot drift from what the CLI actually emits.
"""
import pathlib
import re
import unittest

from agent_coop import coop_errors

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
GUIDE = ROOT / "COOP_GUIDE.md"
HEADING = "## Rejection recovery reflex"


def _section():
    text = SKILL.read_text(encoding="utf-8")
    start = text.index(HEADING)
    rest = text[start + len(HEADING):]
    end = rest.index("\n## ")
    return rest[:end]


class LaunchGuidance(unittest.TestCase):
    def test_launch_requires_explicit_target_and_uses_public_runner(self):
        text = SKILL.read_text(encoding="utf-8")
        for phrase in (
            "Loading a skill does not start a run",
            "Never launch because an ordinary task appears",
            "`coop start --item <id>`",
            "`coop start --all`",
            '`coop item create --title "<goal>" --objective "<goal>"`',
            "Dashboard TASKS Enter creates and launches a new goal",
            "Dashboard `/coop` launches an existing highlighted item",
        ):
            self.assertIn(phrase, text)

    def test_guide_is_not_an_autonomous_runtime_dependency(self):
        skill = SKILL.read_text(encoding="utf-8")
        guide = GUIDE.read_text(encoding="utf-8").replace("> ", "")

        self.assertIn("does not need `COOP_GUIDE.md`", skill)
        self.assertIn(
            "Human operators and interactive debug sessions use this "
            "appendix",
            guide,
        )
        self.assertIn("next_action.command", guide)


class ReceiptGuidance(unittest.TestCase):
    """Receipts must name what was NOT done; reviewers enforce it."""

    def test_stop_boundary_guidance_present_on_both_sides(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("stop boundaries", text)
        self.assertIn("names what was **not** done", text)
        self.assertIn("grounds for `changes`", text)


class ReflexSection(unittest.TestCase):
    def test_section_exists_between_loop_and_judgment(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn(HEADING, text)
        self.assertLess(text.index("## A bound agent's loop"),
                        text.index(HEADING))
        self.assertLess(text.index(HEADING),
                        text.index("## Where agent judgment is expected"))

    def test_reflex_names_the_envelope_fields(self):
        body = _section()
        for field in ("reason_code", "evidence", "legal_next_actions",
                      "required_inputs", "choice"):
            self.assertIn(field, body)

    def test_idle_and_escalation_use_real_vocabulary(self):
        body = _section()
        self.assertIn("`awaiting_peer`", body)
        self.assertIn("awaiting_peer", coop_errors.ERROR_REASON_CODES)
        self.assertIn("`needs-input`", body)
        self.assertIn("status --json", body)
        # escalation stays peer-only mid-run
        self.assertIn("Never ask `human`", body)

    def test_runner_codes_are_labelled_operator_only_and_registered(self):
        body = _section()
        for code in sorted(coop_errors.RUNNER_DETAIL_CODES):
            self.assertIn(f"`{code}`", body)
        self.assertIn("not agent actions", body)

    def test_every_backticked_code_like_token_is_registered(self):
        # drift guard: a snake_case backticked token in the reflex that looks
        # like a taxonomy code must exist in a registry (or the known
        # envelope/action vocabulary) — prose cannot invent codes.
        body = _section()
        known = (coop_errors.ERROR_REASON_CODES
                 | coop_errors.RUNNER_DETAIL_CODES
                 | {"reason_code", "evidence", "legal_next_actions",
                    "next_action", "required_inputs", "command", "choice",
                    "idle", "needs-input", "human", "admin"})
        for token in re.findall(r"`([a-z][a-z_-]+)`", body):
            self.assertIn(token, known,
                          f"reflex names unregistered token: {token}")

    def test_reflex_does_not_restate_code_owned_gates(self):
        # the audit rule: correctness mechanics live in code, not prose.
        body = _section()
        for forbidden in ("SHA-256", "sha256", "quorum", "fencing token",
                          "lease_seconds=", "--lease-seconds"):
            self.assertNotIn(forbidden, body)


if __name__ == "__main__":
    unittest.main()
