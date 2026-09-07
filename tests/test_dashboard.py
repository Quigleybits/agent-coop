"""Operator dashboard v2: pure renderers, change probe, key decode, ladder.

Presentation only — no protocol authority. The dashboard is a read-only
viewer; every renderer is a pure function and every test runs on a
temporary board (ClaimBoard fixture) or plain data.
"""
import contextlib
import io
import pathlib
import unittest
from unittest import mock

from agent_coop import cli as coopcli
from agent_coop import coop_monitor
from agent_coop import coop_ui
from agent_coop import coopdb
from tests.test_claims import ClaimBoard


class FakeKeys:
    """Minimal _WindowsKeys stand-in: a scripted character stream."""

    def __init__(self, chars):
        self.chars = list(chars)

    def kbhit(self):
        return bool(self.chars)

    def getwch(self):
        # Real msvcrt.getwch blocks for the next byte; a special-key prefix is
        # always followed by its code byte. An empty stream returns "" here so
        # a (never-real) lone prefix decodes to None instead of erroring.
        return self.chars.pop(0) if self.chars else ""


class DecodeKey(unittest.TestCase):
    def test_arrows_both_prefixes(self):
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\xe0", "H"])), "up")
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\x00", "P"])), "down")
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\xe0", "P"])), "down")
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\xe0", "K"])), "left")
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\xe0", "M"])), "right")

    def test_paging_prefix_codes(self):
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\xe0", "I"])), "pgup")
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\xe0", "Q"])), "pgdn")
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\xe0", "G"])), "home")
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\xe0", "O"])), "end")

    def test_control_keys(self):
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\r"])), "enter")
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\n"])), "enter")
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\x1b"])), "esc")
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\x03"])), "quit")
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\t"])), "tab")
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\x08"])), "backspace")
        # VT input mode delivers Backspace as DEL (0x7f) — must also edit.
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\x7f"])), "backspace")
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\x0f"])), "switch")
        # Ctrl+B is the tmux/Herdr prefix: it must never reach the switcher.
        self.assertIsNone(coop_monitor.decode_key(FakeKeys(["\x02"])))

    def test_printable_chars_feed_the_input_buffer(self):
        # v3: j/k/q are typable text, not bindings.
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["j"])), ("char", "j"))
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["k"])), ("char", "k"))
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["q"])), ("char", "q"))
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["A"])), ("char", "A"))
        self.assertEqual(coop_monitor.decode_key(FakeKeys([" "])), ("char", " "))

    def test_multiline_paste_stays_one_submission(self):
        keys = FakeKeys(list(
            "first wrapped line\r\n"
            "second wrapped line\r\n"
            "third wrapped line"
        ))
        state = coop_monitor.ViewState()
        actions = []

        while keys.kbhit():
            key = coop_monitor.decode_key(keys)
            action = coop_monitor.apply_key(state, key, task_count=0)
            if action is not None:
                actions.append(action)

        self.assertEqual(actions, [])
        self.assertEqual(
            state.buffer,
            "first wrapped line second wrapped line third wrapped line",
        )
        self.assertEqual(
            coop_monitor.apply_key(state, "enter", task_count=0),
            ("submit",
             "first wrapped line second wrapped line third wrapped line"),
        )

    def test_queued_paste_is_drained_in_one_poll(self):
        keys = FakeKeys(list(
            "first wrapped line\r\n"
            "second wrapped line\r\n"
            "third wrapped line"
        ))

        events = coop_monitor.decode_available_keys(keys)

        self.assertFalse(keys.kbhit())
        state = coop_monitor.ViewState()
        actions = [
            action
            for event in events
            if (action := coop_monitor.apply_key(
                state, event, task_count=0)) is not None
        ]
        self.assertEqual(actions, [])
        self.assertEqual(
            state.buffer,
            "first wrapped line second wrapped line third wrapped line",
        )

    def test_vt_csi_arrows_paging_and_wheel(self):
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\x1b", "[", "A"])), "up")
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\x1b", "[", "B"])), "down")
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\x1b", "[", "C"])), "right")
        self.assertEqual(coop_monitor.decode_key(FakeKeys(["\x1b", "[", "D"])), "left")
        self.assertEqual(
            coop_monitor.decode_key(FakeKeys(["\x1b", "[", "5", "~"])), "pgup")
        self.assertEqual(
            coop_monitor.decode_key(FakeKeys(["\x1b", "[", "6", "~"])), "pgdn")
        self.assertEqual(
            coop_monitor.decode_key(FakeKeys(list("\x1b[<64;30;5M"))),
            ("wheel_up", 30))
        self.assertEqual(
            coop_monitor.decode_key(FakeKeys(list("\x1b[<65;99;5M"))),
            ("wheel_down", 99))

    def test_empty_lone_prefix_and_noise(self):
        self.assertIsNone(coop_monitor.decode_key(FakeKeys([])))
        self.assertIsNone(coop_monitor.decode_key(FakeKeys(["\xe0"])))
        self.assertIsNone(coop_monitor.decode_key(FakeKeys(["\x00"])))

    def test_prefix_code_survives_a_kbhit_gap(self):
        # Wheel-as-arrow sequences deliver the code byte a beat after the
        # prefix; decode must read it, not bail and leak 'P'/'H' as text.
        class GapKeys:
            def __init__(self):
                self.q, self.i = ["\xe0", "P"], 0

            def kbhit(self):
                return self.i == 0  # False right after the prefix is read

            def getwch(self):
                c = self.q[self.i]
                self.i += 1
                return c

        self.assertEqual(coop_monitor.decode_key(GapKeys()), "down")


class BoardProbe(ClaimBoard):
    def test_probe_advances_on_message_then_event_and_is_stable(self):
        p0 = coopdb.board_probe(self.conn)
        coopdb.post_message(self.conn, "codex", "hi")
        p1 = coopdb.board_probe(self.conn)
        self.assertGreater(p1[1], p0[1])  # broadcast say writes no event
        self.assertEqual(p1[0], p0[0])
        self.make_item(title="probe")
        p2 = coopdb.board_probe(self.conn)
        self.assertGreater(p2[0], p1[0])
        self.assertEqual(coopdb.board_probe(self.conn), p2)

    def test_probe_counts_running_sessions(self):
        p0 = coopdb.board_probe(self.conn)
        self.make_session("claude", sid="s-live")
        p1 = coopdb.board_probe(self.conn)
        self.assertEqual(p1[2], p0[2] + 1)


class SplitPanes(unittest.TestCase):
    def test_exact_width_gap_and_height(self):
        lines = coop_ui.split_panes(["left"], ["r1", "r2"], width=41, height=3)
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertEqual(coop_ui._visible_len(line), 41)
            self.assertIn("│", line)

    def test_pads_by_visible_length_with_ansi(self):
        colored = coop_ui._agent_name("codex", True)
        lines = coop_ui.split_panes([colored], [""], width=31, height=1)
        self.assertEqual(coop_ui._visible_len(lines[0]), 31)
        self.assertIn("\033[", lines[0])


class FeedRender(unittest.TestCase):
    def test_chat_shape_and_wrap(self):
        messages = [
            {"from_agent": "codex", "body": "hi"},
            {"from_agent": "claude", "body": "hello codex " * 6},
        ]
        lines = coop_ui.render_feed(
            messages, width=30, height=20, color=False)
        joined = "\n".join(lines)
        self.assertIn("codex: hi", joined)
        self.assertIn("claude:", joined)
        self.assertGreater(len(lines), 3)  # the long body wrapped
        for line in lines:
            self.assertLessEqual(coop_ui._visible_len(line), 30)

    def test_tail_scroll_keeps_newest(self):
        messages = [
            {"from_agent": "codex", "body": f"m{n}"} for n in range(30)]
        lines = coop_ui.render_feed(messages, width=40, height=6, color=False)
        self.assertLessEqual(len(lines), 6)
        self.assertIn("m29", "\n".join(lines))
        self.assertNotIn("m0 ", "\n".join(lines))


class TaskLines(unittest.TestCase):
    TASKS = [
        {"item_id": 1, "status": "todo", "title": "one", "owner": None,
         "next_actor": None, "labels": [], "claim": None},
        {"item_id": 2, "status": "working", "title": "two", "owner": "codex",
         "next_actor": "claude", "labels": ["contract_incomplete"],
         "claim": {"claim_id": 9, "agent": "codex", "status": "active",
                   "intent": "work"}},
    ]

    def test_plain_selection_marker_and_fields(self):
        lines = coop_ui.render_task_lines(
            self.TASKS, cursor=1, width=100, color=False)
        self.assertEqual(len(lines), 2)
        self.assertNotIn("▸", "".join(lines))  # v3: no triangle anywhere
        self.assertFalse(lines[0].startswith("> "))
        self.assertTrue(lines[1].startswith("> "))
        self.assertIn("#2 [working] two", lines[1])
        self.assertIn("codex→claude", lines[1].replace(" ", ""))
        self.assertIn("contract_incomplete", lines[1])
        self.assertNotIn("\033[", "".join(lines))

    def test_colour_selection_is_row_highlight(self):
        lines = coop_ui.render_task_lines(
            self.TASKS, cursor=0, width=100, color=True)
        self.assertIn("\033[7m", lines[0])   # reverse-video highlight
        self.assertNotIn("\033[7m", lines[1])
        self.assertNotIn("▸", "".join(lines))

    def test_focus_highlight_is_violet_not_an_agent_colour(self):
        focused = coop_ui._pane_title("LIVE CHAT", color=True, focused=True)
        unfocused = coop_ui._pane_title("TASKS", color=True, focused=False)
        self.assertIn("180;142;173", focused)      # violet accent
        self.assertIn("▌", focused)                # focus bar
        self.assertNotIn("215;119;87", focused)    # not claude coral
        self.assertNotIn("136;192;208", focused)   # not codex blue
        self.assertNotIn("▌", unfocused)
        # Plain mode still distinguishes focus by the bar, no ANSI.
        plain = coop_ui._pane_title("LIVE CHAT", color=False, focused=True)
        self.assertTrue(plain.startswith("▌"))
        self.assertNotIn("\033[", plain)

    def test_no_selection_renders_no_marker(self):
        lines = coop_ui.render_task_lines(
            self.TASKS, cursor=None, width=100, color=True)
        self.assertNotIn("\033[7m", "".join(lines))


class DashboardRender(ClaimBoard):
    def _seed(self):
        item = self.make_item(title="dash item")
        sid = self.make_session("codex", provider="codex")
        self.claim(item, "codex", sid, intent="work")
        coopdb.post_message(self.conn, "codex", "hi", item_id=item)
        return {
            "item": item,
            "tasks": coopdb.task_rows(self.conn),
            "sessions": coopdb.session_rows(self.conn),
        }

    def _frame(self, snap, *, width, height=30, color=False, cursor=None,
               selected=None, conversation=None, input_buffer="", notice=""):
        return coop_ui.render_dashboard(
            tasks=snap["tasks"], conversation=conversation, selected=selected,
            sessions=snap["sessions"], cursor=cursor, width=width,
            height=height, color=color, board_label="demo_repo",
            board_path=self.db,
            input_buffer=input_buffer, notice=notice, focus="tasks",
            convo_scroll=0, stamp="T")

    def test_tasks_left_conversation_right(self):
        snap = self._seed()
        wide = self._frame(snap, width=130)
        lines = wide.splitlines()
        task_cols = [ln.index("TASKS") for ln in lines if "TASKS" in ln]
        hint_cols = [ln.index("(no board messages)") for ln in lines
                     if "(no board messages)" in ln]
        self.assertTrue(task_cols and hint_cols)
        self.assertLess(task_cols[0], 65, "tasks pane must sit on the left")
        self.assertGreater(hint_cols[0], 65,
                           "LIVE CHAT pane must sit on the right")
        self.assertIn("LIVE CHAT", wide)
        self.assertIn("DIR  demo_repo", wide)
        # Folder name uses CLI-palette ochre (#D99518), not agent coral.
        colored = self._frame(snap, width=130, color=True)
        self.assertRegex(colored, r"217;149;24mdemo_repo")
        self.assertNotRegex(colored, r"215;119;87mdemo_repo")

    def test_conversation_blank_until_selected_then_titled(self):
        snap = self._seed()
        blank = self._frame(snap, width=130)
        self.assertIn("(no board messages)", blank)
        entries = [
            {"kind": "milestone", "text": "claim #1 acquired (codex)",
             "ts": "t1"},
            {"kind": "message", "who": "codex", "body": "hi", "ts": "t2"},
        ]
        selected = snap["tasks"][0]
        frame = self._frame(snap, width=130, cursor=0, selected=selected,
                            conversation=entries)
        self.assertNotIn("(no board messages)", frame)
        self.assertIn(f"#{selected['item_id']}", frame)
        self.assertIn("codex: hi", frame)
        self.assertIn("· claim #1 acquired", frame)

    def test_stacked_narrow_keeps_bottom_section(self):
        snap = self._seed()
        narrow = self._frame(snap, width=90, input_buffer="typing")
        joint = [ln for ln in narrow.splitlines()
                 if "TASKS" in ln and "(no board messages)" in ln]
        self.assertFalse(joint, "narrow frame should stack panes")
        self.assertIn("TASKS", narrow)
        self.assertIn("> typing", narrow)
        self.assertIn("agents:", narrow)   # always-visible agents line

    def test_live_chat_focus_highlight_shows_when_stacked(self):
        snap = self._seed()
        selected = snap["tasks"][0]
        convo = [{"kind": "message", "who": "codex", "body": "hi", "ts": "t"}]
        frame = coop_ui.render_dashboard(
            tasks=snap["tasks"], conversation=convo, selected=selected,
            sessions=snap["sessions"], cursor=0, width=90, height=30,
            color=False, board_label="demo_repo", board_path=self.db,
            input_buffer="", notice="",
            focus="convo", convo_scroll=0, stamp="T")
        live = [ln for ln in frame.splitlines() if "LIVE CHAT" in ln]
        tasks = [ln for ln in frame.splitlines()
                 if "TASKS" in ln and "LIVE CHAT" not in ln]
        self.assertTrue(live and tasks)
        self.assertTrue(live[0].lstrip().startswith("▌"),
                        "LIVE CHAT must show the focus bar when stacked")
        self.assertNotIn("▌", tasks[0])   # unfocused pane has no bar

    def test_bottom_section_input_then_agents_line(self):
        snap = self._seed()
        frame = self._frame(snap, width=130, input_buffer="hello")
        lines = frame.splitlines()
        input_lines = [i for i, ln in enumerate(lines) if "> hello" in ln]
        self.assertTrue(input_lines)
        idx = input_lines[0]
        # Grey rules frame the input; agents sit under the lower rule.
        self.assertTrue(set(lines[idx - 1].strip()) <= {"─"},
                        "grey rule above input")
        self.assertTrue(set(lines[idx + 1].strip()) <= {"─"},
                        "grey rule below input")
        self.assertIn(">", lines[idx])
        # Hardware bar caret — nothing painted into the input text.
        self.assertNotIn(coop_ui.INPUT_CARET, lines[idx])
        self.assertNotIn("|", lines[idx], "ASCII | caret retired")
        self.assertNotIn("_", lines[idx].replace("demo_repo", ""))
        self.assertIn("agents:", lines[idx + 2])
        self.assertIn("codex", lines[idx + 2])
        self.assertIn("claude", lines[idx + 2])
        self.assertIn("grok", lines[idx + 2])

    def test_paint_live_display_steady_violet_bar_cursor(self):
        buf = io.StringIO()
        coop_ui.paint_live_display("frame", buf, caret_cell=(10, 4))
        out = buf.getvalue()
        # Thin bar, blink forced off, violet, parked on the insert cell.
        self.assertIn("\033[6 q", out)
        self.assertIn("\033[?12l", out)
        self.assertNotIn("\033[5 q", out)
        self.assertIn("\033]12;#b48ead\007", out)
        self.assertIn("\033[?25h", out)
        self.assertIn("\033[10;4H", out)

    def test_input_text_plain_caret_cell_between_letters(self):
        snap = self._seed()
        caret: list[int] = []
        frame = coop_ui.render_dashboard(
            tasks=snap["tasks"], conversation=None, selected=None,
            sessions=snap["sessions"], cursor=None, width=130, height=30,
            color=False, board_label="demo_repo", board_path=self.db,
            input_buffer="this", input_cursor=2, notice="",
            focus="tasks", convo_scroll=0, stamp="T",
            input_caret_cell=caret)
        raw_line = next(
            ln for ln in frame.splitlines()
            if "this" in coop_ui.ANSI_PATTERN.sub("", ln)
            and "agents:" not in ln
        )
        plain = coop_ui.ANSI_PATTERN.sub("", raw_line)
        # All letters visible, no reverse, no painted bar in the string.
        self.assertIn("> this", plain)
        self.assertNotIn(coop_ui.INPUT_CARET, plain)
        self.assertNotIn(coop_ui.REVERSE_ON, raw_line)
        # Caret cell = insert point between 'h' and 'i' → col 5 (" > th|is").
        self.assertEqual(len(caret), 2)
        line_off, col = coop_ui.input_caret_line_col("this", 2, width=130)
        self.assertEqual(line_off, 0)
        self.assertEqual(col, 5)
        self.assertEqual(caret[1], col)

    def test_long_input_wraps_instead_of_running_off_screen(self):
        snap = self._seed()
        frame = self._frame(snap, width=80, input_buffer="Z" * 200)
        lines = frame.splitlines()
        zlines = [ln for ln in lines if "Z" in ln]
        self.assertGreater(len(zlines), 1, "long input must wrap, not truncate")
        self.assertEqual("".join(zlines).count("Z"), 200,
                         "no typed characters lost off the right edge")
        for ln in zlines:
            self.assertLessEqual(coop_ui._visible_len(ln), 80)

    def test_footer_hint_reflects_the_focused_mode(self):
        snap = self._seed()

        def frame(focus):
            return coop_ui.render_dashboard(
                tasks=snap["tasks"], conversation=None, selected=None,
                sessions=snap["sessions"], cursor=None, width=130, height=30,
                color=False, board_label="demo_repo", board_path=self.db,
                input_buffer="", notice="", focus=focus, convo_scroll=0,
                stamp="T")
        self.assertIn("⏎ create + run", frame("tasks"))
        self.assertNotIn("⏎ message", frame("tasks"))
        self.assertIn("⏎ message", frame("convo"))
        self.assertNotIn("⏎ create + run", frame("convo"))

    def test_prompt_marker_matches_the_focus_accent(self):
        snap = self._seed()
        frame = coop_ui.render_dashboard(
            tasks=snap["tasks"], conversation=None, selected=None,
            sessions=snap["sessions"], cursor=None, width=130, height=30,
            color=True, board_label="demo_repo", board_path=self.db,
            input_buffer="hi", notice="",
            focus="tasks", convo_scroll=0, stamp="T")
        prompt_line = next(
            ln for ln in frame.splitlines()
            if "hi" in coop_ui.ANSI_PATTERN.sub("", ln)
            and "agents:" not in ln
        )
        # Prompt `>` keeps the violet focus accent; caret is the hardware bar.
        self.assertIn("180;142;173m>", prompt_line)
        self.assertNotIn(coop_ui.INPUT_CARET, prompt_line)

    def test_enter_action_hint_matches_the_focus_accent(self):
        snap = self._seed()

        def frame(focus):
            return coop_ui.render_dashboard(
                tasks=snap["tasks"], conversation=None, selected=None,
                sessions=snap["sessions"], cursor=None, width=130, height=30,
                color=True, board_label="demo_repo", board_path=self.db,
                input_buffer="", notice="", focus=focus, convo_scroll=0,
                stamp="T")

        # Violet (not grey) wraps only the enter action words.
        tasks_frame = frame("tasks")
        self.assertIn("180;142;173m", tasks_frame)
        self.assertRegex(tasks_frame, r"180;142;173m.*⏎ create \+ run")
        convo_frame = frame("convo")
        self.assertRegex(convo_frame, r"180;142;173m.*⏎ message")

    def test_height_bounded_exactly(self):
        snap = self._seed()
        frame = self._frame(snap, width=130, height=24)
        self.assertEqual(len(frame.splitlines()), 24)

    def test_footer_hints_and_no_ansi_when_plain(self):
        snap = self._seed()
        frame = self._frame(snap, width=130, color=False)
        self.assertIn("↑↓ select", frame)
        self.assertIn("^o dir", frame)
        self.assertIn("^c quit", frame)
        self.assertNotIn("⏎ open task", frame)   # v3: Enter submits input
        self.assertNotIn("\033[", frame)

    def test_token_absent_with_live_claim(self):
        snap = self._seed()
        selected = snap["tasks"][0]
        conn_history = coopdb.item_show(self.conn, snap["item"], history=True)
        entries = coop_monitor.conversation_entries(conn_history)
        frame = self._frame(snap, width=130, cursor=0, selected=selected,
                            conversation=entries)
        self.assertNotIn("fencing", frame)

    def test_agents_line_always_lists_preferred_trio(self):
        # Empty live sessions → offline markers, never "no live sessions".
        frame = coop_ui.render_dashboard(
            tasks=[], conversation=None, selected=None, sessions=[],
            cursor=None, width=100, height=24, color=False,
            board_label="demo_repo", board_path=self.db)
        self.assertIn("agents:", frame)
        self.assertNotIn("no live sessions", frame)
        self.assertIn("claude-", frame)
        self.assertIn("codex-", frame)
        self.assertIn("grok-", frame)

    def test_agents_inline_online_idle_solid_dot(self):
        line = coop_ui._agents_inline(
            [{"agent_id": "codex"}], color=False)
        self.assertIn("claude-", line)
        self.assertIn("codex●", line)
        self.assertIn("grok-", line)

    def test_agents_inline_active_claim_blinks(self):
        on = coop_ui._agents_inline(
            [{"agent_id": "claude"}], color=False,
            active=frozenset({"claude"}), blink_on=True)
        off = coop_ui._agents_inline(
            [{"agent_id": "claude"}], color=False,
            active=frozenset({"claude"}), blink_on=False)
        self.assertIn("claude●", on)
        self.assertIn("claude ", off)   # glyph blank on the off-beat
        self.assertNotIn("claude●", off)

    def test_agents_inline_extra_live_seat_appends(self):
        line = coop_ui._agents_inline(
            [{"agent_id": "claude"}, {"agent_id": "claude-502a066a"}],
            color=False)
        # Preferred order first, then the collision seat.
        self.assertRegex(
            line, r"claude●\s+codex-\s+grok-\s+claude-502a066a●")


class DetailRender(ClaimBoard):
    def _walk(self):
        """Item walked through claim → question → answer → receipt → review."""
        import pathlib
        item = self.make_item(title="detail item")
        sid_codex = self.make_session("codex", provider="codex")
        sid_claude = self.make_session("claude", provider="claude")
        claim = self.claim(item, "codex", sid_codex, intent="build")
        qid = coopdb.needs_input(
            self.conn, claim_id=claim["claim_id"], session_id=sid_codex,
            to_agent="claude", question="which port?")
        response = coopdb.claim_question(
            self.conn, question_id=qid, session_id=sid_claude,
            intent="answering")
        coopdb.answer_question(
            self.conn, claim_id=response["claim_id"], session_id=sid_claude,
            answer="port 8080")
        resumed = self.claim(item, "codex", sid_codex, intent="resume",
                             reclaim_reason="resuming after the answer")
        evidence = pathlib.Path(self.tmp.name) / "result.md"
        evidence.write_text("done", encoding="utf-8")
        coopdb.submit_receipt(
            self.conn, claim_id=resumed["claim_id"], session_id=sid_codex,
            actor="codex", path=str(evidence), summary="built",
            proof="see file", proof_refs=[f"file:{evidence}"])
        coopdb.request_review(
            self.conn, claim_id=resumed["claim_id"], session_id=sid_codex,
            actor="codex")
        return item

    def _render(self, item_id, *, height=60):
        packet = coopdb.item_show(self.conn, item_id, packet=True)
        history = coopdb.item_show(self.conn, item_id, history=True)
        return coop_ui.render_task_detail(
            item=packet, history=history,
            item_messages=history["messages"], width=100, height=height,
            color=False)

    def test_sections_present_from_walked_board(self):
        item = self._walk()
        detail = self._render(item)
        self.assertIn("detail item", detail)
        self.assertIn("which port?", detail)
        self.assertIn("port 8080", detail)
        self.assertIn("receipt", detail.lower())
        self.assertIn("review", detail.lower())
        self.assertNotIn("fencing", detail)
        self.assertNotIn("\033[", detail)

    def test_height_bound_with_more_marker(self):
        item = self._walk()
        detail = self._render(item, height=8)
        lines = detail.splitlines()
        self.assertLessEqual(len(lines), 8)
        self.assertIn("more", lines[-1])


class AgentsProvider(ClaimBoard):
    def test_agents_shows_provider_not_kind(self):
        self.make_session("codex", provider="codex")
        out = io.StringIO()
        args = mock.Mock()
        with contextlib.redirect_stdout(out):
            with mock.patch.object(
                    coopcli.sys, "stdout", out):
                coopcli.cmd_agents(self.conn, args)
        text = out.getvalue()
        self.assertIn("provider=codex", text)
        self.assertNotIn("kind=", text)


class LadderSelection(unittest.TestCase):
    def setUp(self):
        # cmd_monitor always pins the opened board, so
        # without this these tests write x.db into the operator's real
        # ~/.coop/boards.json every run.
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patched = mock.patch.dict(
            coopdb.os.environ,
            {"COOP_BOARDS_REGISTRY": str(
                pathlib.Path(tmp.name) / "boards.json")})
        patched.start()
        self.addCleanup(patched.stop)

    def _args(self):
        # A real board: `monitor` now owns its board lifecycle and treats an
        # explicit --db that is missing as an error, never a creation.
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(pathlib.Path(tmp.name) / "x.db")
        conn = coopdb.connect(db)
        coopdb.init_db(conn)
        conn.close()
        args = mock.Mock()
        args.db = db
        args.interval = 2
        args.workspace = None   # `monitor` has no --workspace; Mock() invents one
        args.no_global_skills = True   # never touch a home from this test
        return args

    def test_dashboard_preferred_when_supported(self):
        conn = mock.Mock()
        with (
            mock.patch.object(coopcli.coop_monitor, "dashboard_supported",
                              return_value=True),
            mock.patch.object(coopcli.coop_monitor, "run_dashboard",
                              return_value=True) as run,
            mock.patch.object(coopcli, "monitor") as plain,
        ):
            coopcli.cmd_monitor(conn, self._args())
        run.assert_called_once()
        plain.assert_not_called()
        conn.close.assert_called_once_with()

    def test_falls_back_when_unsupported(self):
        conn = mock.Mock()
        with (
            mock.patch.object(coopcli.coop_monitor, "dashboard_supported",
                              return_value=False),
            mock.patch.object(coopcli.coop_monitor, "run_dashboard") as run,
            mock.patch.object(coopcli, "monitor") as plain,
        ):
            coopcli.cmd_monitor(conn, self._args())
        run.assert_not_called()
        plain.assert_called_once()

    def test_falls_back_when_vt_refused(self):
        conn = mock.Mock()
        with (
            mock.patch.object(coopcli.coop_monitor, "dashboard_supported",
                              return_value=True),
            mock.patch.object(coopcli.coop_monitor, "run_dashboard",
                              return_value=False),
            mock.patch.object(coopcli, "monitor") as plain,
        ):
            coopcli.cmd_monitor(conn, self._args())
        plain.assert_called_once()


class DefaultArgv(unittest.TestCase):
    """Bare `coop` is the dashboard, terminal or not (golden path A); the
    plain redraw loop is the non-terminal fallback."""

    def test_bare_opens_the_dashboard(self):
        self.assertEqual(coopcli._default_argv([]), ["monitor"])

    def test_root_options_alone_still_open_the_dashboard(self):
        self.assertEqual(
            coopcli._default_argv(["--db", "x"]), ["--db", "x", "monitor"])

    def test_a_subcommand_passes_through(self):
        self.assertEqual(coopcli._default_argv(["agents"]), ["agents"])
        self.assertEqual(
            coopcli._default_argv(["--db", "x", "agents"]),
            ["--db", "x", "agents"])


class BoardDiscovery(unittest.TestCase):
    def setUp(self):
        import tempfile, pathlib
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.repo = self.root / "repoA"
        self.sub = self.repo / "sub" / "dir"
        self.sub.mkdir(parents=True)
        # Discovery stops at the nearest Git boundary; mark the repo root.
        (self.repo / ".git").mkdir()
        (self.repo / "coop").mkdir()
        self.board = self.repo / "coop" / "board.db"
        self.board.write_bytes(b"")
        self.plain = self.root / "plain"
        self.plain.mkdir()

    def test_walk_up_finds_nearest_board(self):
        self.assertEqual(
            coopdb.discover_board(str(self.sub)), str(self.board.resolve()))
        self.assertEqual(
            coopdb.discover_board(str(self.repo)), str(self.board.resolve()))

    def test_no_board_anywhere_returns_none(self):
        self.assertIsNone(coopdb.discover_board(str(self.plain)))

    def test_walk_up_finds_dot_coop_board(self):
        # B4: the layout `coop init --workspace` creates, and the only one
        # that works on a repo that is not Co-op.
        repo = self.root / "repoC"
        sub = repo / "sub" / "dir"
        sub.mkdir(parents=True)
        (repo / ".git").mkdir()
        (repo / ".coop").mkdir()
        board = repo / ".coop" / "board.db"
        board.write_bytes(b"")
        self.assertEqual(
            coopdb.discover_board(str(sub)), str(board.resolve()))

    def test_dot_coop_wins_over_a_sibling_legacy_board(self):
        repo = self.root / "repoD"
        repo.mkdir()
        (repo / ".coop").mkdir()
        (repo / ".coop" / "board.db").write_bytes(b"")
        (repo / "board.db").write_bytes(b"")
        self.assertEqual(
            coopdb.discover_board(str(repo)),
            str((repo / ".coop" / "board.db").resolve()))

    def test_walk_up_finds_repo_root_board(self):
        # An older board layout that stays discoverable: the board sits
        # at the repo root, not under a coop/ subdir.
        repo = self.root / "repoB"
        sub = repo / "sub" / "dir"
        sub.mkdir(parents=True)
        (repo / ".git").mkdir()
        board = repo / "board.db"
        board.write_bytes(b"")
        self.assertEqual(
            coopdb.discover_board(str(sub)), str(board.resolve()))

    def _resolve(self, db=None, env=None, cwd=None):
        import argparse
        args = argparse.Namespace(db=db)
        env = {"COOP_DB": "", "COOP_DB_PATH": "", "COOP_DEFAULT_DB": "",
               **(env or {})}
        with mock.patch.dict(coopcli.os.environ, env, clear=False), \
                mock.patch.object(coopcli.os, "getcwd",
                                  return_value=str(cwd or self.plain)):
            return coopcli._db(args)

    def test_resolution_order(self):
        self.assertEqual(self._resolve(db="explicit.db", cwd=self.sub),
                         "explicit.db")
        self.assertEqual(
            self._resolve(env={"COOP_DB": "env.db"}, cwd=self.sub), "env.db")
        self.assertEqual(self._resolve(cwd=self.sub),
                         str(self.board.resolve()))
        self.assertEqual(
            self._resolve(env={"COOP_DEFAULT_DB": "fallback.db"}),
            "fallback.db")
        self.assertEqual(self._resolve(), ".coop/board.db")


class BoardsRegistry(unittest.TestCase):
    def setUp(self):
        import tempfile, pathlib
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = pathlib.Path(self.tmp.name) / "sub" / "boards.json"
        patcher = mock.patch.dict(
            coopdb.os.environ, {"COOP_BOARDS_REGISTRY": str(self.registry)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_record_creates_updates_and_orders_newest_first(self):
        with mock.patch.object(coopdb, "now",
                               side_effect=["t1", "t2", "t3"]):
            coopdb.record_board("a.db")
            coopdb.record_board("b.db")
            coopdb.record_board("a.db")
        boards = coopdb.known_boards()
        self.assertEqual(len(boards), 2)
        self.assertTrue(boards[0].endswith("a.db"))  # freshest first
        self.assertTrue(boards[1].endswith("b.db"))

    def test_corrupt_registry_tolerated(self):
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        self.registry.write_text("not json", encoding="utf-8")
        self.assertEqual(coopdb.known_boards(), [])
        coopdb.record_board("a.db")   # rewrites over the corruption
        self.assertEqual(len(coopdb.known_boards()), 1)

    def test_missing_registry_reads_empty(self):
        self.assertEqual(coopdb.known_boards(), [])

    def test_temp_dir_boards_never_reach_the_home_registry(self):
        import pathlib, tempfile
        scratch = pathlib.Path(tempfile.gettempdir()) / "coop-scratch-x"
        scratch.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: scratch.rmdir())
        board = scratch / ".coop" / "board.db"
        self.assertTrue(coopdb.is_temp_path(board))
        # The suite sandboxes HOME/USERPROFILE under the temp tree, so a
        # real-looking home must be synthesised outside it (need not exist).
        real_home = pathlib.Path(scratch.anchor, "users", "someone")
        self.assertFalse(coopdb.is_temp_path(real_home / "repo"))
        # Home registry (no redirect): the temp board is refused.
        env = dict(coopdb.os.environ); env.pop("COOP_BOARDS_REGISTRY", None)
        with mock.patch.dict(coopdb.os.environ, env, clear=True),                 mock.patch.object(coopdb, "_boards_registry_path",
                                  return_value=self.registry):
            coopdb.record_board(str(board))
            self.assertFalse(self.registry.exists())
            self.assertEqual(coopdb.known_boards(), [])
        # Redirected registry (tests, throwaway harnesses): recorded.
        coopdb.record_board(str(board))
        self.assertEqual(len(coopdb.known_boards()), 1)

    def test_open_or_create_board_creates_on_open_and_reports_gone_folders(self):
        import pathlib
        from agent_coop import coop_monitor
        workspace = pathlib.Path(self.tmp.name) / "fresh-repo"
        workspace.mkdir()
        board = workspace / ".coop" / "board.db"
        self.assertFalse(board.exists())
        conn, notice = coop_monitor.open_or_create_board(str(board))
        self.assertIsNotNone(conn); conn.close()
        self.assertTrue(board.is_file())
        self.assertTrue((workspace / ".claude" / "skills" / "coop" / "SKILL.md").is_file())
        self.assertTrue((workspace / ".agents" / "skills" / "coop" / "SKILL.md").is_file())
        self.assertTrue(notice.startswith("created board in "))
        conn, notice = coop_monitor.open_or_create_board(str(board))
        self.assertIsNotNone(conn); conn.close()
        self.assertTrue(notice.startswith("board: "))
        gone = pathlib.Path(self.tmp.name) / "vanished" / ".coop" / "board.db"
        conn, notice = coop_monitor.open_or_create_board(str(gone))
        self.assertIsNone(conn)
        self.assertIn("folder no longer exists", notice)

    def test_vanished_workspace_is_hidden_and_dropped_on_next_write(self):
        import json, pathlib
        keep = pathlib.Path(self.tmp.name) / "keep"
        keep.mkdir()
        gone = pathlib.Path(self.tmp.name) / "gone"
        gone.mkdir()
        with mock.patch.object(coopdb, "is_temp_path", return_value=False):
            coopdb.record_board(str(keep / ".coop" / "board.db"))
            coopdb.record_board(str(gone / ".coop" / "board.db"))
            self.assertEqual(len(coopdb.known_boards()), 2)
            gone.rmdir()
            boards = coopdb.known_boards()
            self.assertEqual(len(boards), 1)
            self.assertTrue(boards[0].startswith(str(keep.resolve())), boards)
            # a folder with no board yet stays listed: opening it creates one
            self.assertFalse((keep / ".coop" / "board.db").exists())
            coopdb.record_board(str(keep / ".coop" / "board.db"))
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 1)
        self.assertNotIn(str(gone), "".join(data))

    def test_display_board_path_is_home_relative_and_names_worktrees(self):
        import os, pathlib
        home = pathlib.Path.home().resolve()
        repo = home / "projects" / "app"
        self.assertEqual(
            coopdb.display_board_path(repo / ".coop" / "board.db"),
            "~" + os.sep + os.sep.join(["projects", "app"]))
        self.assertEqual(coopdb.board_workspace(repo / "board.db"), repo)
        # A linked git worktree: `.git` is a file pointing into the main
        # repository's .git/worktrees/<name>. Detected from that, never from
        # a folder-name convention.
        base = pathlib.Path(self.tmp.name)
        main_repo = base / "app"
        (main_repo / ".git" / "worktrees" / "feat").mkdir(parents=True)
        linked = base / "elsewhere" / "feat-checkout"
        linked.mkdir(parents=True)
        (linked / ".git").write_text(
            "gitdir: " + str(main_repo / ".git" / "worktrees" / "feat") + os.linesep,
            encoding="utf-8")
        label = coopdb.display_board_path(linked / ".coop" / "board.db")
        self.assertTrue(label.endswith(" · worktree:feat"), label)
        expected_prefix = coopdb._home_relative(
            main_repo.resolve(strict=False))
        self.assertTrue(label.startswith(expected_prefix), (label, expected_prefix))
        self.assertIsNone(coopdb.git_worktree_main(main_repo))

class RegistryHookInMain(ClaimBoard):
    def _run_main(self, argv, cwd, registry):
        env = {"COOP_BOARDS_REGISTRY": str(registry), "COOP_DB": "",
               "COOP_DB_PATH": "", "COOP_DEFAULT_DB": self.db}
        out = io.StringIO()
        with mock.patch.dict(coopcli.os.environ, env, clear=False), \
                mock.patch.object(coopcli.os, "getcwd",
                                  return_value=str(cwd)), \
                mock.patch.object(coopdb, "is_temp_path",
                                  return_value=False), \
                contextlib.redirect_stdout(out):
            try:
                coopcli.main(argv)
            except SystemExit:
                pass

    def test_discovered_default_recorded_explicit_not(self):
        import pathlib, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            registry = pathlib.Path(tmp) / "boards.json"
            plain = pathlib.Path(tmp) / "nowhere"
            plain.mkdir()
            self._run_main(["agents"], plain, registry)      # via DEFAULT_DB
            self.assertIn("board.db", registry.read_text(encoding="utf-8"))
            registry.unlink()
            self._run_main(["--db", self.db, "agents"], plain, registry)
            self.assertFalse(registry.exists())               # explicit: no record

    def test_explicit_monitor_records_board_for_switcher(self):
        """Opening the dashboard pins even an
        explicit --db path so scratch boards appear in the ^o switcher.
        Non-monitor explicit CLI still does not pollute the registry."""
        import json
        import pathlib
        import tempfile
        from agent_coop import coop_monitor
        with tempfile.TemporaryDirectory() as tmp:
            registry = pathlib.Path(tmp) / "boards.json"
            plain = pathlib.Path(tmp) / "nowhere"
            plain.mkdir()
            with mock.patch.object(
                    coop_monitor, "dashboard_supported", return_value=True), \
                    mock.patch.object(
                        coop_monitor, "run_dashboard", return_value=True):
                self._run_main(["--db", self.db, "monitor"], plain, registry)
            data = json.loads(registry.read_text(encoding="utf-8"))
            keys = {str(pathlib.Path(k).resolve()) for k in data}
            self.assertIn(str(pathlib.Path(self.db).resolve()), keys)


class ActionDurationFormat(unittest.TestCase):
    def test_compact_brackets(self):
        self.assertEqual(coop_monitor.format_action_duration(0), "[0s]")
        self.assertEqual(coop_monitor.format_action_duration(34), "[34s]")
        self.assertEqual(coop_monitor.format_action_duration(60), "[1m]")
        self.assertEqual(coop_monitor.format_action_duration(72), "[1m12s]")
        self.assertEqual(coop_monitor.format_action_duration(3600), "[1h]")
        self.assertEqual(coop_monitor.format_action_duration(3723), "[1h2m]")
        self.assertEqual(coop_monitor.format_action_duration(-1), "")


class ConversationEntries(ClaimBoard):
    def test_interleaves_messages_and_milestones_chronologically(self):
        item = self.make_item(title="talkative")
        sid = self.make_session("codex", provider="codex")
        self.clock.advance(1)
        self.claim(item, "codex", sid, intent="work")
        self.clock.advance(1)
        coopdb.say(self.conn, session_id=sid, body="starting now",
                   item_id=item)
        self.clock.advance(1)
        coopdb.say(self.conn, session_id=None, body="ack from human",
                   item_id=item)
        history = coopdb.item_show(self.conn, item, history=True)
        entries = coop_monitor.conversation_entries(history)
        kinds = [e["kind"] for e in entries]
        self.assertIn("milestone", kinds)
        self.assertIn("message", kinds)
        stamps = [e["ts"] for e in entries]
        self.assertEqual(stamps, sorted(stamps))
        first_claim = next(
            e for e in entries if e["kind"] == "milestone"
            and "claim" in e["text"])
        first_msg = next(e for e in entries if e["kind"] == "message")
        self.assertLess(entries.index(first_claim), entries.index(first_msg))
        self.assertEqual(first_msg["who"], "codex")
        self.assertEqual(first_msg["body"], "starting now")
        self.assertNotIn("fencing", str(entries))
        # Acquire is bookkeeping — no duration suffix.
        self.assertNotRegex(first_claim["text"], r"\[\d")

    def test_claim_gated_milestones_show_hold_duration(self):
        item = self.make_item(title="timed")
        alice = self.make_session("alice", sid="s-alice")
        bob = self.make_session("bob", sid="s-bob")
        claim = coopdb.claim_item(
            self.conn, item_id=item, actor="alice", session_id="s-alice",
            intent="work", lease_seconds=300)
        self.clock.advance(34)
        qid = coopdb.needs_input(
            self.conn, claim_id=claim["claim_id"], session_id="s-alice",
            to_agent="bob", question="which path?")
        self.clock.advance(2)
        qclaim = coopdb.claim_question(
            self.conn, question_id=qid, session_id="s-bob",
            intent="answer", lease_seconds=300)
        self.clock.advance(12)
        coopdb.answer_question(
            self.conn, claim_id=qclaim["claim_id"], session_id="s-bob",
            answer="path B")
        history = coopdb.item_show(self.conn, item, history=True)
        entries = coop_monitor.conversation_entries(history)
        texts = [e["text"] for e in entries if e["kind"] == "milestone"]
        needs = next(t for t in texts if t.startswith("needs input"))
        answered = next(t for t in texts if t.startswith("question answered"))
        claimed_q = next(t for t in texts if t.startswith("question claimed"))
        self.assertIn("needs input (alice) [34s]", needs)
        self.assertIn("question answered (bob) [12s]", answered)
        # Claim-open events stay undurationed.
        self.assertEqual(claimed_q, "question claimed (bob)")
        self.assertNotRegex(claimed_q, r"\[")


class ConversationRender(unittest.TestCase):
    ENTRIES = [
        {"kind": "milestone", "text": "claim #9 acquired (codex)", "ts": "1"},
        {"kind": "message", "who": "codex", "body": "on it", "ts": "2"},
        {"kind": "message", "who": "claude", "body": "b" * 80, "ts": "3"},
    ]

    def test_blank_hint_when_no_selection(self):
        lines = coop_ui.render_conversation(
            None, selected=None, width=50, height=6, color=False, scroll=0)
        self.assertTrue(any("(no board messages)" in ln for ln in lines))
        self.assertIn("LIVE CHAT · board", lines[0])

    def test_board_feed_shows_board_wide_messages_when_unselected(self):
        feed = [
            {"kind": "message", "who": "grok", "body": "grok online", "ts": "1"},
            {"kind": "message", "who": "codex", "body": "boot idle", "ts": "2"},
        ]
        lines = coop_ui.render_conversation(
            feed, selected=None, width=60, height=10, color=False, scroll=0)
        text = "\n".join(lines)
        self.assertIn("LIVE CHAT · board", lines[0])
        self.assertIn("grok: grok online", text)
        self.assertIn("codex: boot idle", text)

    def test_milestones_dim_dot_messages_named(self):
        lines = coop_ui.render_conversation(
            self.ENTRIES, selected={"item_id": 5, "title": "T"},
            width=60, height=12, color=False, scroll=0)
        text = "\n".join(lines)
        self.assertIn("· claim #9 acquired", text)
        self.assertIn("codex: on it", text)
        self.assertIn("#5", lines[0])

    def test_scroll_windows_history(self):
        tail = coop_ui.render_conversation(
            self.ENTRIES, selected={"item_id": 5, "title": "T"},
            width=30, height=4, color=False, scroll=0)
        self.assertIn("b" * 10, "\n".join(tail))
        back = coop_ui.render_conversation(
            self.ENTRIES, selected={"item_id": 5, "title": "T"},
            width=30, height=4, color=False, scroll=50)
        self.assertIn("claim #9", "\n".join(back))

    def test_scroll_info_reports_clamped_ceiling(self):
        info: list[int] = []
        coop_ui.render_conversation(
            self.ENTRIES, selected={"item_id": 5, "title": "T"},
            width=30, height=4, color=False, scroll=50, scroll_info=info)
        self.assertEqual(len(info), 2)
        clamped, max_scroll = info
        self.assertEqual(clamped, max_scroll)
        self.assertEqual(
            max_scroll,
            coop_ui.conversation_max_scroll(self.ENTRIES, width=30, height=4))
        self.assertGreater(max_scroll, 0)

    def test_plain_has_no_ansi(self):
        lines = coop_ui.render_conversation(
            self.ENTRIES, selected={"item_id": 5, "title": "T"},
            width=60, height=12, color=False, scroll=0)
        self.assertNotIn("\033[", "\n".join(lines))


class TaskHscrollRender(unittest.TestCase):
    def test_selected_row_pans_without_wrapping(self):
        long_title = "word-" * 40  # well past a narrow pane
        tasks = [
            {"item_id": 1, "status": "todo", "title": long_title,
             "owner": None, "next_actor": None, "labels": [], "claim": None},
            {"item_id": 2, "status": "todo", "title": "short",
             "owner": None, "next_actor": None, "labels": [], "claim": None},
        ]
        width = 40
        head = coop_ui.render_task_lines(
            tasks, cursor=0, width=width, color=False, hscroll=0)
        mid = coop_ui.render_task_lines(
            tasks, cursor=0, width=width, color=False, hscroll=20)
        # One physical line each; no wrap blanks.
        self.assertEqual(len(head), 2)
        self.assertEqual(len(mid), 2)
        self.assertLessEqual(coop_ui._visible_len(head[0]), width)
        self.assertLessEqual(coop_ui._visible_len(mid[0]), width)
        # Pan reveals later title content that the left-aligned window hid.
        self.assertNotEqual(head[0], mid[0])
        self.assertIn("word-", mid[0])
        # Non-selected row is unaffected by hscroll.
        self.assertEqual(head[1], mid[1])


class ApplyKey(unittest.TestCase):
    def _state(self, **over):
        state = coop_monitor.ViewState()
        for key, value in over.items():
            setattr(state, key, value)
        return state

    def test_chars_and_backspace_edit_the_buffer(self):
        state = self._state()
        for ch in "hey":
            self.assertIsNone(
                coop_monitor.apply_key(state, ("char", ch), task_count=0))
        self.assertEqual(state.buffer, "hey")
        self.assertEqual(state.buffer_pos, 3)
        coop_monitor.apply_key(state, "backspace", task_count=0)
        self.assertEqual(state.buffer, "he")
        self.assertEqual(state.buffer_pos, 2)

    def test_left_right_move_caret_and_edit_mid_buffer(self):
        state = self._state()
        for ch in "hey":
            coop_monitor.apply_key(state, ("char", ch), task_count=0)
        # caret at end: ← ← lands between 'h' and 'e'
        coop_monitor.apply_key(state, "left", task_count=0)
        coop_monitor.apply_key(state, "left", task_count=0)
        self.assertEqual(state.buffer_pos, 1)
        # insert mid-string
        coop_monitor.apply_key(state, ("char", "X"), task_count=0)
        self.assertEqual(state.buffer, "hXey")
        self.assertEqual(state.buffer_pos, 2)
        # backspace mid-string removes the char before the caret
        coop_monitor.apply_key(state, "backspace", task_count=0)
        self.assertEqual(state.buffer, "hey")
        self.assertEqual(state.buffer_pos, 1)
        # bounds: left at 0 / right at len are no-ops for the caret
        coop_monitor.apply_key(state, "left", task_count=0)
        coop_monitor.apply_key(state, "left", task_count=0)
        self.assertEqual(state.buffer_pos, 0)
        coop_monitor.apply_key(state, "right", task_count=0)
        coop_monitor.apply_key(state, "right", task_count=0)
        coop_monitor.apply_key(state, "right", task_count=0)
        coop_monitor.apply_key(state, "right", task_count=0)
        self.assertEqual(state.buffer_pos, 3)
        # empty-buffer ←/→ still pan a selected title (caret is a no-op)
        state.buffer = ""
        state.buffer_pos = 0
        state.cursor = 0
        state.focus = "tasks"
        coop_monitor.apply_key(state, "right", task_count=2)
        self.assertEqual(state.task_hscroll, coop_monitor._TASK_HSCROLL_STEP)

    def test_arrows_select_and_bound(self):
        state = self._state()
        self.assertIsNone(state.cursor)
        coop_monitor.apply_key(state, "down", task_count=2)
        self.assertEqual(state.cursor, 0)
        coop_monitor.apply_key(state, "down", task_count=2)
        self.assertEqual(state.cursor, 1)
        coop_monitor.apply_key(state, "down", task_count=2)
        self.assertEqual(state.cursor, 1)
        coop_monitor.apply_key(state, "up", task_count=2)
        self.assertEqual(state.cursor, 0)

    def test_arrows_scoped_to_focused_pane(self):
        state = self._state(cursor=0, convo_max_scroll=20)
        # TASKS focus (default): arrows move the cursor, pin convo to tail.
        coop_monitor.apply_key(state, "down", task_count=3)
        self.assertEqual(state.cursor, 1)
        self.assertEqual(state.convo_scroll, 0)
        # Focus LIVE CHAT: arrows now scroll it; the cursor is frozen.
        coop_monitor.apply_key(state, "tab", task_count=3)
        self.assertEqual(state.focus, "convo")
        coop_monitor.apply_key(state, "up", task_count=3)
        self.assertEqual(state.convo_scroll, 1)
        self.assertEqual(state.cursor, 1)
        coop_monitor.apply_key(state, "up", task_count=3)
        self.assertEqual(state.convo_scroll, 2)
        coop_monitor.apply_key(state, "down", task_count=3)
        self.assertEqual(state.convo_scroll, 1)
        self.assertEqual(state.cursor, 1)

    def test_convo_scroll_up_clamps_at_top_no_overscroll(self):
        """Extra ↑ past the top must not create dead ↓ keys."""
        state = self._state(focus="convo", convo_max_scroll=5)
        for _ in range(20):
            coop_monitor.apply_key(state, "up", task_count=1)
        self.assertEqual(state.convo_scroll, 5)
        # One down must leave the top immediately — no phantom debt.
        coop_monitor.apply_key(state, "down", task_count=1)
        self.assertEqual(state.convo_scroll, 4)
        # Home lands on the real top, not a huge sentinel.
        coop_monitor.apply_key(state, "home", task_count=1)
        self.assertEqual(state.convo_scroll, 5)
        coop_monitor.apply_key(state, "pgup", task_count=1)
        self.assertEqual(state.convo_scroll, 5)
        coop_monitor.apply_key(state, ("wheel_up", 80), task_count=1)
        self.assertEqual(state.convo_scroll, 5)

    def test_left_right_pan_selected_task_title_under_tasks_focus(self):
        # Title pan only while the input buffer is empty — non-empty text
        # owns ←/→ for caret movement (see test_left_right_move_caret…).
        state = self._state(cursor=0, focus="tasks")
        coop_monitor.apply_key(state, "right", task_count=2)
        self.assertEqual(state.task_hscroll, coop_monitor._TASK_HSCROLL_STEP)
        coop_monitor.apply_key(state, "right", task_count=2)
        self.assertEqual(state.task_hscroll, 2 * coop_monitor._TASK_HSCROLL_STEP)
        coop_monitor.apply_key(state, "left", task_count=2)
        self.assertEqual(state.task_hscroll, coop_monitor._TASK_HSCROLL_STEP)
        # Moving the selection resets the pan so the next title starts left.
        coop_monitor.apply_key(state, "down", task_count=2)
        self.assertEqual(state.cursor, 1)
        self.assertEqual(state.task_hscroll, 0)
        # LIVE CHAT focus + empty buffer ignores ←→ (no accidental pan).
        state.task_hscroll = 8
        state.focus = "convo"
        coop_monitor.apply_key(state, "right", task_count=2)
        self.assertEqual(state.task_hscroll, 8)
        # Non-empty buffer steals ←/→ for the caret even under TASKS focus.
        state.focus = "tasks"
        state.cursor = 0
        state.buffer = "draft"
        state.buffer_pos = 5
        state.task_hscroll = 4
        coop_monitor.apply_key(state, "left", task_count=2)
        self.assertEqual(state.buffer_pos, 4)
        self.assertEqual(state.task_hscroll, 4)

    def test_esc_clears_buffer_then_deselects(self):
        state = self._state(cursor=1, buffer="typed", buffer_pos=5)
        coop_monitor.apply_key(state, "esc", task_count=3)
        self.assertEqual(state.buffer, "")
        self.assertEqual(state.buffer_pos, 0)
        self.assertEqual(state.cursor, 1)
        coop_monitor.apply_key(state, "esc", task_count=3)
        self.assertIsNone(state.cursor)

    def test_enter_submits_and_clears(self):
        state = self._state(buffer="hello there", buffer_pos=11)
        action = coop_monitor.apply_key(state, "enter", task_count=0)
        self.assertEqual(action, ("submit", "hello there"))
        self.assertEqual(state.buffer, "")
        self.assertEqual(state.buffer_pos, 0)

    def test_tab_toggles_focus_and_paging_scrolls_convo(self):
        state = self._state(cursor=0, convo_max_scroll=50)
        self.assertEqual(state.focus, "tasks")
        coop_monitor.apply_key(state, "tab", task_count=1)
        self.assertEqual(state.focus, "convo")
        coop_monitor.apply_key(state, "pgup", task_count=1)
        self.assertGreater(state.convo_scroll, 0)
        coop_monitor.apply_key(state, "end", task_count=1)
        self.assertEqual(state.convo_scroll, 0)

    def test_wheel_targets_pane_under_cursor(self):
        state = self._state(cursor=0, split_col=60, convo_max_scroll=50)
        coop_monitor.apply_key(state, ("wheel_up", 80), task_count=1)
        self.assertGreater(state.convo_scroll, 0)
        before = state.cursor
        coop_monitor.apply_key(state, ("wheel_down", 80), task_count=1)
        coop_monitor.apply_key(state, ("wheel_up", 10), task_count=5)
        self.assertEqual(state.cursor, max(0, before - 1))

    def test_switcher_flow(self):
        state = self._state()
        coop_monitor.apply_key(state, "switch", task_count=0, board_count=3)
        self.assertEqual(state.mode, "switcher")
        coop_monitor.apply_key(state, "down", task_count=0, board_count=3)
        action = coop_monitor.apply_key(
            state, "enter", task_count=0, board_count=3)
        self.assertEqual(action, ("open_board", 1))
        self.assertEqual(state.mode, "normal")
        coop_monitor.apply_key(state, "switch", task_count=0, board_count=3)
        coop_monitor.apply_key(state, "esc", task_count=0, board_count=3)
        self.assertEqual(state.mode, "normal")

    def test_switcher_typed_path_adds_folder(self):
        state = self._state()
        state.buffer = "leftover-task-draft"
        coop_monitor.apply_key(state, "switch", task_count=0, board_count=0)
        self.assertEqual(state.mode, "switcher")
        self.assertEqual(state.buffer, "")  # draft cleared on open
        for ch in r"C:\work\myproject":
            coop_monitor.apply_key(
                state, ("char", ch), task_count=0, board_count=0)
        self.assertEqual(state.buffer, r"C:\work\myproject")
        action = coop_monitor.apply_key(
            state, "enter", task_count=0, board_count=0)
        self.assertEqual(action, ("add_folder", r"C:\work\myproject"))
        self.assertEqual(state.buffer, "")
        self.assertEqual(state.mode, "switcher")  # stay to see the list

    def test_switcher_empty_enter_opens_highlighted(self):
        state = self._state()
        coop_monitor.apply_key(state, "switch", task_count=0, board_count=2)
        action = coop_monitor.apply_key(
            state, "enter", task_count=0, board_count=2)
        self.assertEqual(action, ("open_board", 0))
        self.assertEqual(state.mode, "normal")

    def test_quit_action(self):
        state = self._state()
        self.assertEqual(
            coop_monitor.apply_key(state, "quit", task_count=0), "quit")


class BoardSwitchResilience(unittest.TestCase):
    """^o must land on any registry path without raising (empty task list)."""

    def _size(self):
        return type("Size", (), {"columns": 100, "lines": 30})()

    def test_open_board_conn_missing_file(self):
        conn, err = coop_monitor.open_board_conn(
            r"C:\does\not\exist\coop\board.db")
        self.assertIsNone(conn)
        self.assertIn("missing", err.lower())

    def test_open_board_conn_empty_sqlite(self):
        import pathlib
        import sqlite3
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "empty.db"
            sqlite3.connect(path).close()  # zero-table sqlite file
            conn, err = coop_monitor.open_board_conn(str(path))
            self.assertIsNone(conn)
            self.assertIn("empty board", err.lower())

    def test_render_missing_path_returns_empty_frame(self):
        state = coop_monitor.ViewState()
        frame, count, caret = coop_monitor._render(
            r"C:\does\not\exist\coop\board.db",
            state, self._size(), boards=[r"C:\does\not\exist\coop\board.db"])
        self.assertEqual(count, 0)
        self.assertIsInstance(frame, str)
        self.assertTrue(len(frame) > 0)
        self.assertIn("empty board", state.notice.lower())
        self.assertIsNotNone(caret)

    def test_render_empty_sqlite_returns_empty_frame(self):
        import pathlib
        import sqlite3
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "x.db"
            sqlite3.connect(path).close()
            state = coop_monitor.ViewState()
            frame, count, caret = coop_monitor._render(
                str(path), state, self._size(), boards=[str(path)])
            self.assertEqual(count, 0)
            self.assertIsInstance(frame, str)
            self.assertIn("empty board", state.notice.lower())
            self.assertIsNotNone(caret)

    def test_render_valid_empty_board_is_zero_tasks(self):
        """A current-schema board with no items is a normal empty task list."""
        import pathlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "board.db"
            conn = coopdb.connect(str(path))
            try:
                coopdb.init_db(conn)
            finally:
                conn.close()
            state = coop_monitor.ViewState(notice="board: ok")
            frame, count, caret = coop_monitor._render(
                str(path), state, self._size(), boards=[str(path)])
            self.assertEqual(count, 0)
            self.assertIsInstance(frame, str)
            # Successful open must not replace a fine notice with an error.
            self.assertEqual(state.notice, "board: ok")
            self.assertIsNotNone(caret)


class SubmitSeam(ClaimBoard):
    def test_cursor_resolves_against_newest_first_render_order(self):
        older = self.make_item(title="older")
        newer = self.make_item(title="newer")

        self.assertEqual(
            coop_monitor._task_id_at_cursor(self.conn, 0), newer)
        self.assertEqual(
            coop_monitor._task_id_at_cursor(self.conn, 1), older)

    def test_created_task_becomes_the_highlighted_task(self):
        state = coop_monitor.ViewState(focus="tasks", cursor=0)
        coop_monitor._select_created_task(
            state, "created task #24 (draft)", prior_task_count=3)
        self.assertEqual(state.cursor, 0)

    def test_bare_text_posts_scoped_to_selection(self):
        item = self.make_item(title="scoped")
        notice = coop_monitor.submit_line(self.db, "hello team", item)
        row = self.conn.execute(
            "SELECT from_agent, body, item_id FROM messages "
            "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["from_agent"], "human")
        self.assertEqual(row["body"], "hello team")
        self.assertEqual(row["item_id"], item)
        self.assertIn("posted", notice)

    def test_bare_text_board_wide_without_selection(self):
        coop_monitor.submit_line(self.db, "to everyone", None)
        row = self.conn.execute(
            "SELECT item_id FROM messages ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNone(row["item_id"])

    def test_command_word_dispatches_not_posts(self):
        coopdb.register_or_bind_agent(
            self.conn, agent_id="codex", provider="codex")
        notice = coop_monitor.submit_line(self.db, "agents", None)
        self.assertIn("codex", notice)
        count = self.conn.execute(
            "SELECT count(*) FROM messages").fetchone()[0]
        self.assertEqual(count, 0)

    def test_tasks_focus_creates_and_launches_a_goal_task_draft(self):
        run = coop_monitor.DetachedRun(
            log_path="run.log",
            status_path="run.status.json",
            trace_path="run.trace.jsonl",
        )
        launches = []
        with mock.patch.object(
                coop_monitor, "_spawn_detached", return_value=run) as spawn:
            notice = coop_monitor.submit_line(
                self.db,
                "explain the review gate",
                None,
                focus="tasks",
                launch_sink=launches.append,
            )

        row = self.conn.execute(
            "SELECT id, title, objective FROM items ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["title"], "explain the review gate")
        self.assertEqual(row["objective"], "explain the review gate")
        spawn.assert_called_once_with(self.db, ["--item", str(row["id"])])
        self.assertEqual(launches, [run])
        self.assertIn(f"created task #{row['id']}", notice)
        self.assertIn("runner: starting", notice)
        # No message was posted — the task is the artifact.
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM messages").fetchone()[0], 0)

    def test_tasks_focus_keeps_the_draft_when_launch_fails(self):
        with mock.patch.object(
                coop_monitor,
                "_spawn_detached",
                side_effect=OSError("runner unavailable"),
        ):
            notice = coop_monitor.submit_line(
                self.db,
                "keep this draft",
                None,
                focus="tasks",
            )

        row = self.conn.execute(
            "SELECT id, title FROM items ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["title"], "keep this draft")
        self.assertIn(f"created task #{row['id']}", notice)
        self.assertIn("launch failed", notice)
        self.assertIn("runner unavailable", notice)

    def test_convo_focus_posts_a_message_not_a_task(self):
        item = self.make_item(title="scoped")
        before = self.conn.execute("SELECT count(*) FROM items").fetchone()[0]
        coop_monitor.submit_line(self.db, "looks good", item, focus="convo")
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM items").fetchone()[0],
            before)
        row = self.conn.execute(
            "SELECT body, item_id FROM messages ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["body"], "looks good")
        self.assertEqual(row["item_id"], item)

    def test_slash_never_creates_or_posts(self):
        before_i = self.conn.execute("SELECT count(*) FROM items").fetchone()[0]
        notice = coop_monitor.submit_line(self.db, "/nope", None, focus="tasks")
        self.assertIn("unknown command", notice)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM items").fetchone()[0],
            before_i)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM messages").fetchone()[0], 0)


class SwitcherRender(unittest.TestCase):
    def test_lists_boards_with_highlight(self):
        lines = coop_ui.render_board_switcher(
            ["C:/a/coop/board.db", "C:/b/coop/board.db"],
            cursor=1, width=60, height=10, color=True)
        text = "\n".join(lines)
        label_a = coopdb.display_board_path("C:/a/coop/board.db")
        label_b = coopdb.display_board_path("C:/b/coop/board.db")
        self.assertIn(label_a, text)
        self.assertIn(label_b, text)
        self.assertNotIn("board.db", text)   # workspace labels, not files
        selected = [ln for ln in lines if label_b in ln]
        self.assertIn("\033[7m", selected[0])
        self.assertIn("type a path", text)

    def test_viewport_tracks_cursor_past_first_page(self):
        # Old bug: only boards[:height-2] rendered, so cursor past that page
        # put the reverse-video highlight off-screen while the list stayed put.
        boards = [f"C:/repo{i:02d}/board.db" for i in range(20)]
        height = 6  # 5 board rows + footer
        lines = coop_ui.render_board_switcher(
            boards, cursor=15, width=60, height=height, color=True)
        text = "\n".join(lines)
        self.assertIn("repo15", text)
        self.assertNotIn("repo00", text)  # scrolled past the top
        selected = [ln for ln in lines if "repo15" in ln]
        self.assertEqual(len(selected), 1)
        self.assertIn("\033[7m", selected[0])
        self.assertIn("type a path", text)
        # Footer stays; total lines fit the viewport.
        self.assertLessEqual(len(lines), height)

    def test_viewport_top_still_shows_first_boards(self):
        boards = [f"C:/repo{i:02d}/board.db" for i in range(12)]
        lines = coop_ui.render_board_switcher(
            boards, cursor=0, width=60, height=6, color=False)
        text = "\n".join(lines)
        self.assertIn("repo00", text)
        self.assertIn("> ", lines[0])
        self.assertNotIn("repo11", text)

    def test_enter_hint_is_new_path_when_folder_menu_open(self):
        closed = coop_ui.render_dashboard(
            tasks=[], conversation=None, selected=None, sessions=[],
            cursor=None, width=100, height=24, color=False,
            board_label="demo_repo", board_path="x.db",
            focus="tasks", overlay_lines=None)
        self.assertIn("⏎ create + run", closed)
        self.assertNotIn("⏎ new path", closed)
        open_menu = coop_ui.render_dashboard(
            tasks=[], conversation=None, selected=None, sessions=[],
            cursor=None, width=100, height=24, color=False,
            board_label="demo_repo", board_path="x.db",
            focus="tasks", overlay_lines=["  C:/a/board.db"])
        self.assertIn("⏎ new path", open_menu)
        self.assertNotIn("⏎ create + run", open_menu)


class ResolveFolderPath(unittest.TestCase):
    def test_directory_without_board_maps_to_the_init_layout(self):
        import os, pathlib
        given = os.path.join(os.path.abspath(os.sep), "work", "myproject")
        out = pathlib.Path(coop_monitor.resolve_folder_path(given))
        self.assertEqual(out.name, "board.db")
        self.assertEqual(out.parent.name, ".coop")
        self.assertEqual(out.parent.parent.name, "myproject")

    def test_directory_with_an_existing_board_keeps_that_board(self):
        import pathlib, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "legacy"
            root.mkdir()
            (root / "board.db").write_bytes(b"")
            out = pathlib.Path(coop_monitor.resolve_folder_path(str(root)))
            self.assertEqual(out, (root / "board.db").resolve())
            (root / ".coop").mkdir()
            (root / ".coop" / "board.db").write_bytes(b"")
            out = pathlib.Path(coop_monitor.resolve_folder_path(str(root)))
            self.assertEqual(out, (root / ".coop" / "board.db").resolve())

    def test_db_file_kept(self):
        import os, pathlib
        given = os.path.join("C:" + os.sep, "work", "myproject", "board.db")
        out = pathlib.Path(coop_monitor.resolve_folder_path(given))
        self.assertEqual(out.name, "board.db")
        self.assertEqual(out.parent.name, "myproject")

    def test_relative_resolved_against_cwd(self):
        import pathlib
        out = coop_monitor.resolve_folder_path("scratch_coop")
        p = pathlib.Path(out)
        self.assertTrue(p.is_absolute())
        self.assertEqual(p.name, "board.db")
        self.assertEqual(p.parent.name, ".coop")
        self.assertEqual(p.parent.parent.name, "scratch_coop")

if __name__ == "__main__":
    unittest.main()
