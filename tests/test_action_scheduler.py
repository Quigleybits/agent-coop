import unittest

from agent_coop import coop_action_scheduler


def candidate(agent, kind, *, target_id=None, target_type=None, item_id=25):
    return coop_action_scheduler.ActionCandidate(
        agent=agent,
        hint=kind,
        action={
            "kind": kind,
            "target_id": target_id,
            "target_type": target_type,
            "item_id": item_id,
        },
    )


class ActionLaneIdentity(unittest.TestCase):
    def test_allowlisted_actions_have_narrow_lane_identity(self):
        cases = (
            (
                candidate("claude", "huddle_post", target_id=4),
                "huddle:4:claude",
            ),
            (
                candidate("codex", "answer_question", target_id=7),
                "question_response:7",
            ),
            (
                candidate("grok", "review_task", target_id=9),
                "review:9",
            ),
            (
                candidate(
                    "claude",
                    "continue_task",
                    target_id=11,
                    target_type="review",
                ),
                "review:11",
            ),
            (
                # Item-scoped: cross-item handoff responses may overlap.
                candidate("claude", "respond_handoff", target_id=3),
                "handoff:25",
            ),
        )

        for action, expected in cases:
            with self.subTest(action=action):
                self.assertEqual(
                    coop_action_scheduler.action_lane(action),
                    expected,
                )

    def test_serialized_or_incomplete_actions_have_no_parallel_lane(self):
        cases = (
            candidate("claude", "claim_task", target_id=25),
            candidate("claude", "continue_task", target_id=25),
            candidate("claude", "huddle_close", target_id=4),
            candidate("claude", "request_review", target_id=25),
            candidate("claude", "complete_task", target_id=25),
            candidate("claude", "unknown_action", target_id=1),
            candidate("claude", "answer_question", target_id=None),
            candidate("claude", "review_task", target_id=True),
        )

        for action in cases:
            with self.subTest(action=action):
                self.assertIsNone(
                    coop_action_scheduler.action_lane(action)
                )


class ActionFingerprint(unittest.TestCase):
    def setUp(self):
        self.base = {
            "kind": "answer_question",
            "target_type": "question",
            "target_id": 7,
            "item_id": 25,
            "claim_id": 41,
            "lease_seconds": 3600,
            "command": [
                "python",
                "-m",
                "agent_coop",
                "question",
                "answer",
                "--claim",
                "41",
                "--answer",
                "{answer}",
            ],
            "required_inputs": ["answer"],
            "choices": [{"kind": "answer", "metadata": {"b": 2, "a": 1}}],
        }

    def test_every_executable_field_changes_identity(self):
        mutations = {
            "kind": "review_task",
            "target_type": "review",
            "target_id": 8,
            "item_id": 26,
            "claim_id": 42,
            "lease_seconds": 7200,
            "command": ["different"],
            "required_inputs": ["verdict"],
            "choices": [{"kind": "decline"}],
        }

        baseline = coop_action_scheduler.action_fingerprint(self.base)
        for field, value in mutations.items():
            changed = {**self.base, field: value}
            with self.subTest(field=field):
                self.assertNotEqual(
                    baseline,
                    coop_action_scheduler.action_fingerprint(changed),
                )
                self.assertFalse(
                    coop_action_scheduler.actions_equivalent(
                        self.base,
                        changed,
                    )
                )

    def test_mapping_order_and_tuple_list_shape_are_equivalent(self):
        reordered = {
            "choices": [{"metadata": {"a": 1, "b": 2}, "kind": "answer"}],
            "required_inputs": ("answer",),
            "command": tuple(self.base["command"]),
            "lease_seconds": 3600,
            "claim_id": 41,
            "item_id": 25,
            "target_id": 7,
            "target_type": "question",
            "kind": "answer_question",
        }

        self.assertEqual(
            coop_action_scheduler.action_fingerprint(self.base),
            coop_action_scheduler.action_fingerprint(reordered),
        )
        self.assertTrue(
            coop_action_scheduler.actions_equivalent(self.base, reordered)
        )

    def test_non_mapping_actions_are_never_equivalent(self):
        self.assertFalse(
            coop_action_scheduler.actions_equivalent(None, None)
        )
        self.assertFalse(
            coop_action_scheduler.actions_equivalent(self.base, None)
        )


class ActionIndependence(unittest.TestCase):
    def test_distinct_question_review_and_huddle_slots_are_independent(self):
        pairs = (
            (
                candidate("claude", "answer_question", target_id=1),
                candidate("codex", "answer_question", target_id=2),
            ),
            (
                candidate("claude", "review_task", target_id=1),
                candidate("grok", "review_task", target_id=2),
            ),
            (
                candidate("codex", "huddle_post", target_id=5),
                candidate("grok", "huddle_post", target_id=5),
            ),
        )

        for left, right in pairs:
            with self.subTest(left=left, right=right):
                self.assertTrue(
                    coop_action_scheduler.actions_independent(left, right)
                )
                self.assertTrue(
                    coop_action_scheduler.actions_independent(right, left)
                )

    def test_same_lane_same_agent_or_serial_action_is_not_independent(self):
        pairs = (
            (
                candidate("claude", "answer_question", target_id=1),
                candidate("codex", "answer_question", target_id=1),
            ),
            (
                candidate("claude", "review_task", target_id=1),
                candidate("claude", "review_task", target_id=2),
            ),
            (
                candidate("claude", "claim_task", target_id=25),
                candidate("codex", "answer_question", target_id=2),
            ),
        )

        for left, right in pairs:
            with self.subTest(left=left, right=right):
                self.assertFalse(
                    coop_action_scheduler.actions_independent(left, right)
                )


class ActionBatchSelection(unittest.TestCase):
    def test_selects_at_most_three_in_stable_input_order(self):
        candidates = [
            candidate("claude", "answer_question", target_id=1),
            candidate("codex", "answer_question", target_id=2),
            candidate("grok", "answer_question", target_id=3),
            candidate("other", "answer_question", target_id=4),
        ]

        selected = coop_action_scheduler.select_action_batch(
            candidates,
            limit=10,
        )

        self.assertEqual(selected, tuple(candidates[:3]))

    def test_first_serial_candidate_runs_alone(self):
        candidates = [
            candidate("claude", "claim_task", target_id=25),
            candidate("codex", "answer_question", target_id=2),
        ]
        self.assertEqual(
            coop_action_scheduler.select_action_batch(candidates),
            (candidates[0],),
        )

    def test_serialization_barrier_is_not_reordered_around(self):
        candidates = [
            candidate("claude", "answer_question", target_id=1),
            candidate("codex", "claim_task", target_id=25),
            candidate("grok", "answer_question", target_id=3),
        ]
        self.assertEqual(
            coop_action_scheduler.select_action_batch(candidates),
            (candidates[0],),
        )

    def test_same_lane_barrier_stops_batch_growth(self):
        candidates = [
            candidate("claude", "review_task", target_id=1),
            candidate("codex", "review_task", target_id=1),
            candidate("grok", "review_task", target_id=2),
        ]
        self.assertEqual(
            coop_action_scheduler.select_action_batch(candidates),
            (candidates[0],),
        )

    def test_empty_input_returns_empty_batch(self):
        self.assertEqual(
            coop_action_scheduler.select_action_batch([]),
            (),
        )


if __name__ == "__main__":
    unittest.main()
