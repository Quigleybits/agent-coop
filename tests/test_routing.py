"""Soft provider profiles: preferences without eligibility enforcement."""

import contextlib
import io
import pathlib
import tempfile
import unittest
from unittest import mock

from agent_coop import coop_autonomous
from agent_coop import coop_routing
from agent_coop import coop_start
from agent_coop import coopdb


class ProviderProfiles(unittest.TestCase):
    def test_base_roles_match_the_product_profiles(self):
        self.assertEqual(
            coop_routing.PROVIDER_PROFILES["claude"]["role"],
            "orchestration")
        self.assertEqual(
            coop_routing.PROVIDER_PROFILES["codex"]["role"],
            "code review and deep dives")
        self.assertEqual(
            coop_routing.PROVIDER_PROFILES["grok"]["role"],
            "fast tasks and research")


class SoftParticipantOrdering(unittest.TestCase):
    participants = ["claude", "codex", "grok"]

    def order(self, title, **fields):
        return coop_routing.soft_order_participants(
            self.participants, {"title": title, **fields})

    def test_orchestration_prefers_claude(self):
        self.assertEqual(
            self.order("Coordinate a multi-agent contract huddle"),
            ["claude", "codex", "grok"])

    def test_code_review_and_deep_dive_prefer_codex(self):
        self.assertEqual(
            self.order("Audit the scheduler", objective="Deep dive review"),
            ["codex", "claude", "grok"])

    def test_fast_research_prefers_grok(self):
        self.assertEqual(
            self.order("Quick research task", scope="Investigate options"),
            ["grok", "claude", "codex"])

    def test_unmatched_task_preserves_incoming_order(self):
        original = ["grok", "codex", "claude"]
        task = {"title": "Write the requested output"}
        self.assertEqual(
            coop_routing.soft_order_participants(original, task),
            original)
        self.assertEqual(original, ["grok", "codex", "claude"])

    def test_protocol_review_boilerplate_does_not_select_the_owner(self):
        task = {
            "title": "Implement the parser",
            "objective": "Implement the parser",
            "done_when": "A distinct-provider review approves the receipt",
            "allowed_actions": ["request review"],
        }
        self.assertEqual(
            coop_routing.soft_order_participants(self.participants, task),
            self.participants)

    def test_preference_never_adds_or_removes_agents(self):
        self.assertEqual(
            coop_routing.soft_order_participants(
                ["claude", "grok"], {"title": "Code review"}),
            ["claude", "grok"])
        self.assertEqual(
            coop_routing.soft_order_participants(
                ["gemini", "grok"], {"title": "Fast research"}),
            ["grok", "gemini"])

    def test_empty_or_non_mapping_task_is_a_noop(self):
        for task in (None, {}, "research"):
            with self.subTest(task=task):
                self.assertEqual(
                    coop_routing.soft_order_participants(
                        self.participants, task),
                    self.participants)


class SelectedRunRouting(unittest.TestCase):
    def _participants_for(self, title, *, all_items=False):
        with tempfile.TemporaryDirectory() as tmp:
            board = str(pathlib.Path(tmp) / "board.db")
            conn = coopdb.connect(board)
            coopdb.init_db(conn)
            item = coopdb.create_item(
                conn, actor="human", session_id=None,
                title=title, objective=title)
            conn.close()
            captured = []

            def run(participants, **_kwargs):
                captured.append(list(participants))
                return "stopped", 0, []

            args = ["--db", board]
            args.extend(["--all"] if all_items else ["--item", str(item)])
            with mock.patch.object(
                    coop_start, "resolve_participants",
                    return_value={
                        "available": ["claude", "codex", "grok"],
                        "skipped": [],
                    }), \
                 mock.patch.object(
                     coop_start, "resolved_provider_argv",
                     side_effect=lambda name: [name]), \
                 mock.patch.object(
                     coop_autonomous, "run_autonomous", side_effect=run), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(coop_autonomous.main(args), 0)
            return captured[0]

    def test_selected_task_softly_routes_the_initial_owner(self):
        cases = [
            ("Coordinate the multi-agent huddle",
             ["claude", "codex", "grok"]),
            ("Deep dive code review",
             ["codex", "claude", "grok"]),
            ("Quick research comparison",
             ["grok", "claude", "codex"]),
        ]
        for title, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(self._participants_for(title), expected)

    def test_all_items_keeps_the_fixed_fallback_order(self):
        self.assertEqual(
            self._participants_for("Quick research", all_items=True),
            ["claude", "codex", "grok"])


if __name__ == "__main__":
    unittest.main()
