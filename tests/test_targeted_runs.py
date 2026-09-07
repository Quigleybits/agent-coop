"""Selected-task product runs: dashboard target -> scoped autonomous turn."""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from agent_coop import cli as coopcli
from agent_coop import coop_autonomous
from agent_coop import coop_monitor
from agent_coop import coop_start
from agent_coop import coopdb
from tests.test_claims import ClaimBoard


class SelectedDashboardTask(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.board = str(pathlib.Path(self.tmp.name) / "coop" / "board.db")
        pathlib.Path(self.board).parent.mkdir(parents=True)
        conn = coopdb.connect(self.board)
        coopdb.init_db(conn)
        conn.close()

    def _launch(self, line, item_id):
        with mock.patch.object(
                coop_start, "resolve_participants",
                return_value={"available": ["claude", "codex"],
                              "skipped": []}), \
             mock.patch.object(
                 coop_monitor, "_spawn_detached",
                 return_value=coop_monitor.DetachedRun(
                     log_path=str(pathlib.Path(self.tmp.name) / "run.log"),
                     status_path=str(
                         pathlib.Path(self.tmp.name) / "run.status.json"),
                     trace_path=str(
                         pathlib.Path(self.tmp.name) / "run.trace.jsonl"),
                 )) as spawn:
            notice = coop_monitor.submit_line(self.board, line, item_id)
        return notice, spawn

    def test_slash_coop_refuses_without_highlight(self):
        notice, spawn = self._launch("/coop", None)
        self.assertIn("select a task", notice.lower())
        spawn.assert_not_called()

    def test_slash_coop_targets_highlighted_item(self):
        notice, spawn = self._launch("/coop", 24)
        spawn.assert_called_once_with(self.board, ["--item", "24"])
        self.assertEqual(notice, "runner: starting")

    def test_slash_coop_all_is_explicit(self):
        _notice, spawn = self._launch("/coop all", None)
        spawn.assert_called_once_with(self.board, ["--all"])


class ExplicitStartMode(unittest.TestCase):
    def test_public_start_refuses_ambiguous_global_default(self):
        with contextlib.redirect_stderr(io.StringIO()), \
             self.assertRaises(SystemExit):
            coopcli.build_parser().parse_args([
                "start", "--board", "C:/work/board.db"])

    def test_cmd_start_forwards_item(self):
        args = coopcli.build_parser().parse_args([
            "start", "--board", "C:/work/board.db", "--item", "24",
            "--dry-run"])
        with mock.patch.object(coop_autonomous, "main", return_value=0) as run:
            coopcli.cmd_start(None, args)
        forwarded = run.call_args.args[0]
        self.assertIn("--item", forwarded)
        self.assertEqual(forwarded[forwarded.index("--item") + 1], "24")
        self.assertNotIn("--all", forwarded)

    def test_cmd_start_forwards_explicit_all(self):
        args = coopcli.build_parser().parse_args([
            "start", "--board", "C:/work/board.db", "--all", "--dry-run"])
        with mock.patch.object(coop_autonomous, "main", return_value=0) as run:
            coopcli.cmd_start(None, args)
        forwarded = run.call_args.args[0]
        self.assertIn("--all", forwarded)
        self.assertNotIn("--item", forwarded)

    def test_autonomous_parser_refuses_ambiguous_global_default(self):
        with contextlib.redirect_stderr(io.StringIO()), \
             self.assertRaises(SystemExit):
            coop_autonomous.main(["--db", "unused.db", "--dry-run"])

    def test_autonomous_parser_accepts_token_efficient_opt_in(self):
        with contextlib.redirect_stdout(io.StringIO()):
            code = coop_autonomous.main([
                "--selftest",
                "--token-efficient",
            ])
        self.assertEqual(code, 0)


class _ImmediateTree:
    def poll_root(self):
        return 0

    def close(self):
        pass

    def is_empty(self):
        return True


class _Prepared:
    def release(self):
        return _ImmediateTree()


class BoundTurnScope(unittest.TestCase):
    def test_invoke_turn_exports_target_item(self):
        captured = {}

        def tree_factory(_argv, **kwargs):
            captured.update(kwargs["env"])
            return _Prepared()

        result = coop_start.invoke_turn(
            provider="claude", prompt="work", session_id="s-target",
            agent_id="claude", board_path="board.db", cwd=".", timeout_s=5,
            item_id=24, tree_factory=tree_factory,
            resolve=lambda _name: "claude.exe")
        self.assertTrue(result["ok"])
        self.assertEqual(captured["COOP_ITEM_ID"], "24")


class TargetCompletion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.board = str(pathlib.Path(self.tmp.name) / "board.db")
        self.conn = coopdb.connect(self.board)
        coopdb.init_db(self.conn)
        self.item23 = coopdb.create_item(
            self.conn, actor="human", session_id=None,
            title="old active", objective="old active")
        self.item24 = coopdb.create_item(
            self.conn, actor="human", session_id=None,
            title="target", objective="target")
        self.conn.execute(
            "UPDATE items SET status='working' WHERE id=?", (self.item23,))
        self.conn.execute(
            "UPDATE items SET status='done' WHERE id=?", (self.item24,))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_target_done_ignores_other_active_items(self):
        self.assertFalse(coop_autonomous.board_all_done(self.conn))
        self.assertTrue(coop_autonomous.board_all_done(
            self.conn, item_id=self.item24))
        self.assertFalse(coop_autonomous.board_all_done(
            self.conn, item_id=self.item23))


class ScopedBoardRouting(ClaimBoard):
    def _draft(self, title):
        return coopdb.create_item(
            self.conn, actor="human", session_id=None,
            title=title, objective=title)

    def _next(self, agent, item_id):
        return coopdb._derive_next_action(
            self.conn, agent, coopdb.now(), item_id=item_id)[0]

    def test_old_open_huddle_cannot_preempt_selected_draft(self):
        claude_sid = self.make_session(
            "claude", sid="s-claude", provider="claude")
        self.make_session("codex", sid="s-codex", provider="codex")
        self.make_session("grok", sid="s-grok", provider="grok")
        old_item = self._draft("old stalled item")
        claim = coopdb.claim_item(
            self.conn, item_id=old_item, actor="claude",
            session_id=claude_sid, intent="define old item",
            lease_seconds=3600)
        coopdb.define_item(
            self.conn, claim_id=claim["claim_id"],
            session_id=claude_sid, actor="claude", fields={
                "scope": "one file",
                "done_when": "the file exists",
                "output_contract": "OLD.md",
                "context": "old run",
                "allowed_actions": ["write the file"],
                "stop_conditions": ["stop on ambiguity"],
            })
        huddle = coopdb.open_contract_huddle(
            self.conn, claim_id=claim["claim_id"],
            session_id=claude_sid, actor="claude")
        target = self._draft("new selected item")

        self.assertEqual(self._next("codex", None)["target_id"],
                         huddle["huddle_id"])
        scoped = self._next("codex", target)
        self.assertEqual(scoped["kind"], "claim_task")
        self.assertEqual(scoped["item_id"], target)

    def test_unrelated_question_and_active_claim_are_ignored(self):
        asker_sid = self.make_session("asker", sid="s-asker")
        codex_sid = self.make_session("codex", sid="s-codex")
        question_item = self.make_item(title="unrelated question")
        asker_claim = self.claim(question_item, "asker", asker_sid)
        question_id = coopdb.needs_input(
            self.conn, claim_id=asker_claim["claim_id"],
            session_id=asker_sid, to_agent="codex", question="old work?")
        active_item = self.make_item(title="unrelated active")
        self.claim(active_item, "codex", codex_sid)
        target = self._draft("selected draft")

        self.assertEqual(self._next("codex", None)["target_id"], question_id)
        scoped = self._next("codex", target)
        self.assertEqual(scoped["kind"], "claim_task")
        self.assertEqual(scoped["item_id"], target)

    def test_unrelated_pending_handoff_is_ignored(self):
        owner_sid = self.make_session("owner", sid="s-owner")
        self.make_session("codex", sid="s-codex")
        old_item = self.make_item(title="old handoff")
        claim = self.claim(old_item, "owner", owner_sid)
        event_id = self.conn.execute(
            "SELECT event_id FROM events WHERE item_id=? "
            "ORDER BY event_id LIMIT 1", (old_item,)).fetchone()[0]
        handoff = coopdb.create_handoff(
            self.conn, claim_id=claim["claim_id"],
            session_id=owner_sid, actor="owner", to_agent="codex",
            reason="rotate", summary="started", completed="setup",
            remaining="finish", risks="none", next_action="continue",
            proof_refs=[f"event:{event_id}"])
        target = self._draft("selected draft")

        self.assertEqual(
            self._next("codex", None)["target_id"],
            handoff["handoff_id"])
        scoped = self._next("codex", target)
        self.assertEqual((scoped["kind"], scoped["item_id"]),
                         ("claim_task", target))

    def test_unrelated_review_is_ignored(self):
        owner_sid = self.make_session("owner", sid="s-owner")
        self.make_session("codex", sid="s-codex", provider="codex")
        old_item = self.make_item(title="old review")
        claim = self.claim(old_item, "owner", owner_sid)
        evidence = pathlib.Path(self.tmp.name) / "old-review.txt"
        evidence.write_text("evidence", encoding="utf-8")
        coopdb.submit_receipt(
            self.conn, claim_id=claim["claim_id"],
            session_id=owner_sid, actor="owner", path=str(evidence),
            summary="done", proof="file", proof_refs=[])
        review_id = coopdb.request_review(
            self.conn, claim_id=claim["claim_id"],
            session_id=owner_sid, actor="owner", reviewer="codex")
        target = self._draft("selected draft")

        self.assertEqual(self._next("codex", None)["target_id"], review_id)
        scoped = self._next("codex", target)
        self.assertEqual((scoped["kind"], scoped["item_id"]),
                         ("claim_task", target))

    def test_unrelated_resume_and_stale_recovery_are_ignored(self):
        codex_sid = self.make_session("codex", sid="s-codex")
        helper_sid = self.make_session("helper", sid="s-helper")
        resume_item = self.make_item(title="old resume")
        claim = self.claim(resume_item, "codex", codex_sid)
        question_id = coopdb.needs_input(
            self.conn, claim_id=claim["claim_id"],
            session_id=codex_sid, to_agent="helper", question="answer?")
        question_claim = coopdb.claim_question(
            self.conn, question_id=question_id,
            session_id=helper_sid, intent="answer")
        coopdb.answer_question(
            self.conn, claim_id=question_claim["claim_id"],
            session_id=helper_sid, answer="yes")
        target = self._draft("selected draft")

        self.assertEqual(self._next("codex", None)["kind"], "resume_task")
        scoped = self._next("codex", target)
        self.assertEqual((scoped["kind"], scoped["item_id"]),
                         ("claim_task", target))

        # Once the old grace item is removed, an unrelated reclaimable lane
        # still cannot pre-empt the selected item.
        self.conn.execute(
            "UPDATE items SET status='done' WHERE id=?", (resume_item,))
        self.conn.commit()
        stale_item = self.make_item(title="old stale")
        stale_claim = self.claim(stale_item, "codex", codex_sid)
        self.clock.advance(31)
        coopdb.sweep_expired(self.conn)
        coopdb.finish_session(
            self.conn, codex_sid, status="exited", reason="child_exit",
            exit_code=0)
        self.assertEqual(self._next("codex", None)["kind"], "recover_claim")
        scoped = self._next("codex", target)
        self.assertEqual((scoped["kind"], scoped["item_id"]),
                         ("claim_task", target))

    def test_scoped_queue_and_status_show_only_selected_item(self):
        codex_sid = self.make_session("codex", sid="s-codex")
        old_item = self.make_item(title="old active")
        self.claim(old_item, "codex", codex_sid)
        target = self._draft("selected draft")

        rows = coopdb.queue(
            self.conn, for_agent="codex", item_id=target)
        self.assertEqual([row["item_id"] for row in rows], [target])
        status = coopdb.status(
            self.conn, "codex", session_id=codex_sid, item_id=target)
        self.assertEqual(status["claims"], [])
        self.assertEqual(status["owned_items"], [])
        self.assertEqual(status["next_action"]["item_id"], target)

    def test_cli_status_uses_exported_target_item(self):
        self.make_session("codex", sid="s-codex")
        self._draft("old draft")
        target = self._draft("selected draft")
        stdout = io.StringIO()
        args = mock.Mock(as_agent="codex", json=True)
        with mock.patch.dict(
                "os.environ", {"COOP_ITEM_ID": str(target)}, clear=True), \
             contextlib.redirect_stdout(stdout):
            coopcli.cmd_status(self.conn, args)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["next_action"]["item_id"], target)

    def test_scoped_inbox_is_filtered_and_never_advances_global_cursor(self):
        sender_sid = self.make_session("sender", sid="s-sender")
        self.make_session("codex", sid="s-codex")
        old_item = self._draft("old")
        target = self._draft("target")
        coopdb.say(
            self.conn, session_id=sender_sid, body="old message",
            to_agent="codex", item_id=old_item)
        coopdb.say(
            self.conn, session_id=sender_sid, body="target message",
            to_agent="codex", item_id=target)

        rows = coopdb.read_inbox(
            self.conn, "codex", item_id=target, peek=False)
        self.assertEqual([row["item_id"] for row in rows], [target])
        offset = self.conn.execute(
            "SELECT last_consumed_entry_id FROM inbox_offsets "
            "WHERE agent_id='codex'").fetchone()
        self.assertIsNone(offset)
        self.assertEqual(len(coopdb.read_inbox(
            self.conn, "codex", peek=True)), 2)


if __name__ == "__main__":
    unittest.main()
