"""DIR header label is violet only while the ^B dir menu is open.

Pure-render check: the menu-open signal is `overlay_lines is not None`
(the switcher is the only overlay), so the label paints violet then and
grey otherwise.
"""
import pathlib
import unittest

from agent_coop import coop_autonomous
from agent_coop import coop_monitor
from agent_coop import coop_ui

VIOLET_DIR = r"180;142;173mDIR"  # #b48ead focus accent
GREY_DIR = r"147;147;147mDIR"


def _frame(overlay):
    return coop_ui.render_dashboard(
        tasks=[], conversation=[], selected=None, sessions=[], cursor=None,
        width=130, height=30, color=True, board_label="demo_repo",
        board_path="x.db", input_buffer="", notice="", focus="tasks",
        convo_scroll=0, stamp="T", overlay_lines=overlay)


class BoardLabel(unittest.TestCase):
    """The switcher label names the workspace, not the state directory."""

    def test_dot_coop_board_labels_its_workspace(self):
        self.assertEqual(
            coop_monitor._board_label("/repos/feature-a/.coop/board.db"),
            "feature-a")

    def test_legacy_coop_board_still_labels_its_repo(self):
        self.assertEqual(
            coop_monitor._board_label("/repos/demo_repo/coop/board.db"),
            "demo_repo")

    def test_plain_board_labels_its_own_directory(self):
        self.assertEqual(
            coop_monitor._board_label("/repos/agent_coop/board.db"),
            "agent_coop")


class WorkspaceDir(unittest.TestCase):
    """The turn cwd is the workspace; board-adjacent state stays board-side."""

    def test_dot_coop_board_names_its_parent(self):
        self.assertEqual(
            coop_autonomous.workspace_dir("/repos/feature-a/.coop/board.db"),
            pathlib.Path("/repos/feature-a").resolve())

    def test_older_layouts_are_their_own_workspace(self):
        for board in ("/repos/agent_coop/board.db",
                      "/repos/demo_repo/coop/board.db"):
            with self.subTest(board=board):
                self.assertEqual(
                    coop_autonomous.workspace_dir(board),
                    pathlib.Path(board).resolve().parent)


class DirLabelColor(unittest.TestCase):
    def test_grey_when_menu_closed(self):
        frame = _frame(None)
        self.assertRegex(frame, GREY_DIR)
        self.assertNotRegex(frame, VIOLET_DIR)

    def test_violet_when_menu_open(self):
        frame = _frame(["  /some/board.db", "  ⏎ open · esc cancel"])
        self.assertRegex(frame, VIOLET_DIR)
        self.assertNotRegex(frame, GREY_DIR)


if __name__ == "__main__":
    unittest.main()
