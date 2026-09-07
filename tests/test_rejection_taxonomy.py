import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from agent_coop import cli as coop
from agent_coop import coop_errors
from agent_coop import coopdb


class ErrorEnvelope(unittest.TestCase):
    def test_default_reason_preserves_type_and_message(self):
        error = coop_errors.ReceiptMissing(
            "item has no receipt",
            evidence={
                "item_id": 25,
                "claim_id": 39,
                "constraint": "current_receipt_required",
            },
        )
        self.assertEqual(error.type, "receipt_missing")
        self.assertEqual(str(error), "item has no receipt")
        self.assertEqual(error.reason_code, "receipt_missing")
        self.assertEqual(
            error.as_dict(),
            {
                "type": "receipt_missing",
                "message": "item has no receipt",
                "reason_code": "receipt_missing",
                "evidence": {
                    "item_id": 25,
                    "claim_id": 39,
                    "constraint": "current_receipt_required",
                },
                "legal_next_actions": [],
            },
        )

    def test_broad_error_accepts_registered_override(self):
        error = coop_errors.InvalidTransition(
            "peer must answer first",
            reason_code="awaiting_peer",
            evidence={
                "item_id": 25,
                "target_agent_id": "codex",
                "current_state": "needs_input",
            },
        )
        self.assertEqual(error.type, "invalid_transition")
        self.assertEqual(error.reason_code, "awaiting_peer")

    def test_unknown_reason_is_refused(self):
        with self.assertRaisesRegex(ValueError, "unknown error reason code"):
            coop_errors.InvalidTransition(
                "bad code",
                reason_code="invented_recovery",
            )

    def test_explicit_falsy_reasons_are_refused(self):
        for reason_code in ("", []):
            with self.subTest(reason_code=reason_code):
                with self.assertRaisesRegex(
                    ValueError,
                    "unknown error reason code",
                ):
                    coop_errors.InvalidTransition(
                        "bad code",
                        reason_code=reason_code,
                    )

    def test_evidence_rejects_unknown_nested_and_long_values(self):
        invalid = (
            {"task_text": "secret"},
            {"item_id": {"nested": 25}},
            {"blocking_ids": list(range(33))},
            {"constraint": "x" * 257},
        )
        for evidence in invalid:
            with self.subTest(evidence=evidence):
                with self.assertRaises((TypeError, ValueError)):
                    coop_errors.InvalidTransition(
                        "unsafe evidence",
                        evidence=evidence,
                    )

    def test_evidence_rejects_long_string_list_entries(self):
        for values in (["x" * 257], ("x" * 257,)):
            with self.subTest(values=values):
                with self.assertRaisesRegex(
                    ValueError,
                    "error evidence value too long",
                ):
                    coop_errors.InvalidTransition(
                        "unsafe evidence",
                        evidence={"actionable_agents": values},
                    )

    def test_legal_actions_are_copied_into_projection(self):
        action = {
            "kind": "idle",
            "target_type": None,
            "target_id": None,
            "item_id": 25,
            "claim_id": None,
            "lease_seconds": None,
            "command": None,
            "required_inputs": [],
            "choices": [],
        }
        error = coop_errors.InvalidTransition(
            "wait",
            reason_code="awaiting_peer",
            evidence={"item_id": 25},
        ).attach_legal_next_actions([action])
        action["kind"] = "mutated"
        payload = error.as_dict()
        self.assertEqual(payload["legal_next_actions"][0]["kind"], "idle")
        json.dumps(payload)


class RejectionActions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = pathlib.Path(self.tmp.name) / "board.db"
        self.conn = coopdb.connect(self.db)
        self.addCleanup(self.conn.close)
        coopdb.init_db(self.conn)
        coopdb.register_agent(self.conn, "claude", "claude")
        coopdb.register_agent(self.conn, "codex", "codex")

    def test_adapter_returns_the_exact_blocking_first_action(self):
        with mock.patch.object(
            coopdb,
            "_derive_next_action",
            return_value=(
                {
                    "kind": "idle",
                    "target_type": None,
                    "target_id": None,
                    "item_id": 25,
                    "claim_id": None,
                    "lease_seconds": None,
                    "command": None,
                    "required_inputs": [],
                    "choices": [],
                },
                [],
            ),
        ) as derive:
            actions = coopdb.rejection_actions(
                self.conn,
                "claude",
                item_id=25,
                lease_seconds=90,
            )
        self.assertEqual(actions[0]["kind"], "idle")
        derive.assert_called_once_with(
            self.conn,
            "claude",
            mock.ANY,
            lease_seconds=90,
            item_id=25,
        )

    def test_enrichment_failure_does_not_mask_original_error(self):
        error = coop_errors.InvalidTransition(
            "original",
            evidence={"item_id": 25},
        )
        with mock.patch.dict(
            "os.environ",
            {"COOP_SESSION_ID": "missing", "COOP_AGENT": "claude"},
            clear=False,
        ):
            enriched = coop._enrich_error(self.conn, error)
        self.assertIs(enriched, error)
        self.assertEqual(enriched.legal_next_actions, [])

    def test_text_renderer_preserves_first_line(self):
        error = coop_errors.InvalidTransition(
            "wait for codex",
            reason_code="awaiting_peer",
            evidence={"item_id": 25, "target_agent_id": "codex"},
        ).attach_legal_next_actions([{
            "kind": "idle",
            "target_type": None,
            "target_id": None,
            "item_id": 25,
            "claim_id": None,
            "lease_seconds": None,
            "command": None,
            "required_inputs": [],
            "choices": [],
        }])
        stream = io.StringIO()
        coop._render_error(error, as_json=False, stream=stream)
        lines = stream.getvalue().splitlines()
        self.assertEqual(
            lines[0],
            "error: invalid_transition: wait for codex",
        )
        self.assertEqual(lines[1], "reason: awaiting_peer")
        self.assertTrue(lines[2].startswith("evidence: {"))
        self.assertTrue(lines[3].startswith("legal-next-actions: ["))

    def test_json_renderer_keeps_old_and_adds_new_fields(self):
        error = coop_errors.SchemaMismatch("run coop init")
        stream = io.StringIO()
        coop._render_error(error, as_json=True, stream=stream)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["error"]["type"], "schema_mismatch")
        self.assertEqual(payload["error"]["message"], "run coop init")
        self.assertEqual(payload["error"]["reason_code"], "schema_mismatch")
        self.assertEqual(payload["error"]["evidence"], {})
        self.assertEqual(payload["error"]["legal_next_actions"], [])


class CliValidationRejections(unittest.TestCase):
    def assert_input_invalid(self, callback):
        with self.assertRaises(coop_errors.InvalidTransition) as caught:
            callback()
        error = caught.exception
        self.assertEqual(error.reason_code, "input_invalid")
        self.assertEqual(
            dict(error.evidence),
            {"constraint": "valid_cli_arguments_required"},
        )

    def test_env_item_id_rejections_are_classified(self):
        for raw in ("not-an-integer", "0"):
            with self.subTest(raw=raw):
                with mock.patch.dict(
                    "os.environ",
                    {"COOP_ITEM_ID": raw},
                    clear=False,
                ):
                    self.assert_input_invalid(coop._env_item_id)

    def test_reclaim_flag_rejections_are_classified(self):
        parser = coop.build_parser()
        cases = (
            (coop.cmd_item, [
                "item", "claim", "1", "--intent", "work", "--reclaim",
            ]),
            (coop.cmd_item, [
                "item", "claim", "1", "--intent", "work",
                "--reason", "resume",
            ]),
            (coop.cmd_review, [
                "review", "claim", "1", "--intent", "review", "--reclaim",
            ]),
            (coop.cmd_review, [
                "review", "claim", "1", "--intent", "review",
                "--reason", "resume",
            ]),
            (coop.cmd_question, [
                "question", "claim", "1", "--intent", "answer", "--reclaim",
            ]),
            (coop.cmd_question, [
                "question", "claim", "1", "--intent", "answer",
                "--reason", "resume",
            ]),
        )
        for command, argv in cases:
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assert_input_invalid(lambda: command(None, args))

    def test_unknown_recover_subcommand_is_classified(self):
        args = mock.Mock(recover_cmd="unknown")
        self.assert_input_invalid(lambda: coop.cmd_recover(None, args))
