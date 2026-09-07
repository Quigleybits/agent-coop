import pathlib
import unittest

from agent_coop import coop_recipes


SKILL = pathlib.Path(__file__).resolve().parents[1] / "SKILL.md"
QUICK_TAGS = tuple(sorted(coop_recipes.QUICK_TWO_TAGS))


def item(*, allowed_actions=(), review_quorum=1, contract_version=7):
    return {
        "id": 25,
        "title": "must not influence recipe selection",
        "objective": "research words must not trigger fuzzy selection",
        "allowed_actions": list(allowed_actions),
        "review_quorum": review_quorum,
        "contract_version": contract_version,
    }


class SkillRecipeContract(unittest.TestCase):
    def test_quick_two_tag_block_matches_registry(self):
        text = SKILL.read_text(encoding="utf-8")
        heading = "## Workflow recipes and sparse scheduling"
        self.assertIn(heading, text)
        section = text.split(heading, 1)[1].split("\n## ", 1)[0]
        fence = "```text\n"
        self.assertEqual(section.count(fence), 1)
        block = section.split(fence, 1)[1].split("\n```", 1)[0]
        documented = tuple(
            line.strip()
            for line in block.splitlines()
            if line.strip()
        )

        self.assertEqual(
            len(documented),
            len(coop_recipes.QUICK_TWO_TAGS),
        )
        self.assertEqual(
            frozenset(documented),
            coop_recipes.QUICK_TWO_TAGS,
        )


class WorkflowRecipeCompiler(unittest.TestCase):
    def test_standard_three_is_default_and_retains_soft_order(self):
        recipe = coop_recipes.compile_recipe(
            item(),
            ["grok", "claude", "codex"],
        )

        self.assertEqual(recipe.name, "standard_three")
        self.assertEqual(recipe.contract_version, 7)
        self.assertEqual(recipe.lead, "grok")
        self.assertEqual(
            recipe.active_participants,
            ("grok", "claude", "codex"),
        )
        self.assertEqual(recipe.reserve_participants, ())
        self.assertEqual(
            recipe.stages,
            (
                "implementation",
                "independent_contributions",
                "synthesis",
                "independent_review",
            ),
        )

    def test_standard_three_requires_all_three_core_providers(self):
        cases = (
            ["claude", "codex"],
            ["claude", "codex", "codex"],
            ["claude", "codex", "other"],
            ["claude", "codex", "grok", "other"],
        )

        for participants in cases:
            with self.subTest(participants=participants):
                with self.assertRaisesRegex(
                    coop_recipes.RecipeBlocked,
                    "standard_three_requires_claude_codex_grok",
                ):
                    coop_recipes.compile_recipe(item(), participants)

    def test_quick_two_selects_maker_verifier_and_dormant_reserve(self):
        recipe = coop_recipes.compile_recipe(
            item(allowed_actions=QUICK_TAGS),
            ["grok", "codex", "claude"],
        )

        self.assertEqual(recipe.name, "quick_two")
        self.assertEqual(recipe.lead, "grok")
        self.assertEqual(recipe.active_participants, ("grok", "codex"))
        self.assertEqual(recipe.reserve_participants, ("claude",))
        self.assertEqual(
            recipe.stages,
            ("implementation", "independent_review"),
        )

    def test_quick_two_requires_every_positive_tag(self):
        for missing in QUICK_TAGS:
            tags = tuple(tag for tag in QUICK_TAGS if tag != missing)
            with self.subTest(missing=missing):
                recipe = coop_recipes.compile_recipe(
                    item(allowed_actions=tags),
                    ["claude", "codex", "grok"],
                )
                self.assertEqual(recipe.name, "standard_three")

    def test_quick_two_requires_one_review_and_structured_tags(self):
        quorum_two = coop_recipes.compile_recipe(
            item(allowed_actions=QUICK_TAGS, review_quorum=2),
            ["claude", "codex", "grok"],
        )
        malformed = coop_recipes.compile_recipe(
            {
                **item(),
                "allowed_actions": ",".join(QUICK_TAGS),
            },
            ["claude", "codex", "grok"],
        )

        self.assertEqual(quorum_two.name, "standard_three")
        self.assertEqual(malformed.name, "standard_three")

    def test_task_prose_does_not_select_quick_two(self):
        recipe = coop_recipes.compile_recipe(
            {
                **item(),
                "title": "quick bounded reversible low-risk task",
                "objective": "no research and no high authority",
            },
            ["claude", "codex", "grok"],
        )
        self.assertEqual(recipe.name, "standard_three")

    def test_payload_is_json_safe_and_contains_no_task_prose(self):
        recipe = coop_recipes.compile_recipe(
            item(allowed_actions=QUICK_TAGS),
            ["claude", "codex", "grok"],
        )

        payload = recipe.as_payload()

        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["name"], "quick_two")
        self.assertEqual(payload["active_participants"], ["claude", "codex"])
        self.assertEqual(payload["reserve_participants"], ["grok"])
        self.assertNotIn("title", payload)
        self.assertNotIn("objective", payload)


class WorkflowRecipePromotion(unittest.TestCase):
    def setUp(self):
        self.recipe = coop_recipes.compile_recipe(
            item(allowed_actions=QUICK_TAGS),
            ["grok", "codex", "claude"],
        )

    def test_each_approved_reason_promotes_one_way(self):
        for reason in (
                "needs_input",
                "review_changes",
                "contract_expanded",
                "reserve_directed"):
            with self.subTest(reason=reason):
                promoted = coop_recipes.promote_to_standard_three(
                    self.recipe,
                    reason,
                )
                self.assertEqual(promoted.name, "standard_three")
                self.assertEqual(promoted.contract_version, 7)
                self.assertEqual(promoted.lead, "grok")
                self.assertEqual(
                    promoted.active_participants,
                    ("grok", "codex", "claude"),
                )
                self.assertEqual(promoted.reserve_participants, ())
                self.assertEqual(promoted.promoted_from, "quick_two")
                self.assertEqual(promoted.promotion_reason, reason)

    def test_unknown_reason_is_rejected(self):
        with self.assertRaisesRegex(
                coop_recipes.RecipeBlocked,
                "unsupported_promotion_reason",
        ):
            coop_recipes.promote_to_standard_three(
                self.recipe,
                "agent_felt_like_it",
            )

    def test_standard_recipe_cannot_be_promoted_again(self):
        promoted = coop_recipes.promote_to_standard_three(
            self.recipe,
            "needs_input",
        )
        with self.assertRaisesRegex(
                coop_recipes.RecipeBlocked,
                "recipe_already_standard_three",
        ):
            coop_recipes.promote_to_standard_three(
                promoted,
                "review_changes",
            )

    def test_board_evidence_maps_to_stable_promotion_reasons(self):
        cases = (
            (
                {
                    "current_contract_version": 8,
                    "events": (),
                    "reserve_action": {"kind": "idle"},
                },
                "contract_expanded",
            ),
            (
                {
                    "current_contract_version": 7,
                    "events": (
                        {"event_type": "needs_input", "payload": {}},
                    ),
                    "reserve_action": {"kind": "idle"},
                },
                "needs_input",
            ),
            (
                {
                    "current_contract_version": 7,
                    "events": (
                        {
                            "event_type": "review_resolved",
                            "payload": {"verdict": "changes"},
                        },
                    ),
                    "reserve_action": {"kind": "idle"},
                },
                "review_changes",
            ),
            (
                {
                    "current_contract_version": 7,
                    "events": (),
                    "reserve_action": {
                        "kind": "answer_question",
                        "item_id": 25,
                    },
                },
                "reserve_directed",
            ),
        )

        for inputs, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    coop_recipes.detect_promotion_reason(
                        self.recipe,
                        **inputs,
                    ),
                    expected,
                )

    def test_non_triggering_board_evidence_does_not_promote(self):
        cases = (
            {
                "events": (
                    {
                        "event_type": "review_resolved",
                        "payload": {"verdict": "approve"},
                    },
                ),
                "reserve_action": {"kind": "idle"},
            },
            {
                "events": (),
                "reserve_action": {
                    "kind": "claim_task",
                    "item_id": 25,
                },
            },
            {
                "events": (
                    {"event_type": "message_posted", "payload": {}},
                ),
                "reserve_action": {"kind": "idle"},
            },
        )

        for inputs in cases:
            with self.subTest(inputs=inputs):
                self.assertIsNone(
                    coop_recipes.detect_promotion_reason(
                        self.recipe,
                        current_contract_version=7,
                        **inputs,
                    )
                )

    def test_standard_three_never_reenters_promotion_detection(self):
        standard = coop_recipes.promote_to_standard_three(
            self.recipe,
            "needs_input",
        )
        self.assertIsNone(
            coop_recipes.detect_promotion_reason(
                standard,
                current_contract_version=8,
                events=(
                    {"event_type": "needs_input", "payload": {}},
                ),
                reserve_action={"kind": "review_task"},
            )
        )


if __name__ == "__main__":
    unittest.main()
