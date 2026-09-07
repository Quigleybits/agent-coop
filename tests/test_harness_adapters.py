"""Packaged harness adapters install safely into foreign workspaces."""

import json
import pathlib
import tempfile
import unittest

from agent_coop import coop_adapters


ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKOUT_ADAPTERS = {
    "claude": ROOT / ".claude" / "skills" / "coop" / "SKILL.md",
    "agents": ROOT / ".agents" / "skills" / "coop" / "SKILL.md",
}


class BundledAdapters(unittest.TestCase):
    def test_package_resources_match_checkout_adapters(self):
        bundled = coop_adapters.bundled_adapters()

        self.assertEqual(set(bundled), set(CHECKOUT_ADAPTERS))
        for name, path in CHECKOUT_ADAPTERS.items():
            self.assertEqual(bundled[name], path.read_text(encoding="utf-8"))

    def test_pyproject_includes_adapter_resources(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"adapters/*/SKILL.md"', text)


class WorkspaceAdapterInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = pathlib.Path(self.tmp.name) / "repo"
        self.workspace.mkdir()

    def result_map(self, results):
        return {result["name"]: result for result in results}

    def test_fresh_install_writes_both_harness_layers_and_state(self):
        results = self.result_map(
            coop_adapters.install_workspace_adapters(self.workspace)
        )
        bundled = coop_adapters.bundled_adapters()

        self.assertEqual(
            {name: result["status"] for name, result in results.items()},
            {"claude": "installed", "agents": "installed"},
        )
        for name, relative in coop_adapters.ADAPTER_DESTINATIONS.items():
            installed = self.workspace / relative
            self.assertEqual(
                installed.read_text(encoding="utf-8"),
                bundled[name],
            )
        state = json.loads(
            (self.workspace / coop_adapters.ADAPTER_STATE_PATH).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(set(state["managed_hashes"]), {
            str(path).replace("\\", "/")
            for path in coop_adapters.ADAPTER_DESTINATIONS.values()
        })

    def test_second_install_is_idempotent(self):
        coop_adapters.install_workspace_adapters(self.workspace)
        results = self.result_map(
            coop_adapters.install_workspace_adapters(self.workspace)
        )

        self.assertEqual(
            {name: result["status"] for name, result in results.items()},
            {"claude": "current", "agents": "current"},
        )

    def test_modified_adapter_is_preserved(self):
        coop_adapters.install_workspace_adapters(self.workspace)
        target = self.workspace / coop_adapters.ADAPTER_DESTINATIONS["claude"]
        target.write_text("user-owned instructions\n", encoding="utf-8")

        results = self.result_map(
            coop_adapters.install_workspace_adapters(self.workspace)
        )

        self.assertEqual(results["claude"]["status"], "preserved")
        self.assertEqual(target.read_text(encoding="utf-8"),
                         "user-owned instructions\n")
        self.assertEqual(results["agents"]["status"], "current")

    def test_unmodified_managed_adapter_updates(self):
        first = {"claude": "claude v1\n", "agents": "agents v1\n"}
        second = {"claude": "claude v2\n", "agents": "agents v2\n"}
        coop_adapters.install_workspace_adapters(
            self.workspace,
            templates=first,
        )

        results = self.result_map(
            coop_adapters.install_workspace_adapters(
                self.workspace,
                templates=second,
            )
        )

        self.assertEqual(
            {name: result["status"] for name, result in results.items()},
            {"claude": "updated", "agents": "updated"},
        )
        for name, relative in coop_adapters.ADAPTER_DESTINATIONS.items():
            self.assertEqual(
                (self.workspace / relative).read_text(encoding="utf-8"),
                second[name],
            )

    def test_parent_symlink_escape_is_rejected_before_any_write(self):
        outside = pathlib.Path(self.tmp.name) / "outside"
        outside.mkdir()
        try:
            (self.workspace / ".claude").symlink_to(
                outside,
                target_is_directory=True,
            )
        except OSError as error:
            self.skipTest(f"directory symlinks unavailable: {error}")

        with self.assertRaises(coop_adapters.AdapterPathInvalid):
            coop_adapters.install_workspace_adapters(self.workspace)

        self.assertFalse((outside / "skills" / "coop" / "SKILL.md").exists())
        self.assertFalse((self.workspace / ".agents").exists())
        self.assertFalse(
            (self.workspace / coop_adapters.ADAPTER_STATE_PATH).exists()
        )

    def test_planted_atomic_temp_symlink_is_never_followed(self):
        destination = (
            self.workspace
            / coop_adapters.ADAPTER_DESTINATIONS["claude"]
        )
        destination.parent.mkdir(parents=True)
        outside = pathlib.Path(self.tmp.name) / "outside.txt"
        outside.write_text("keep me\n", encoding="utf-8")
        planted = destination.with_name(
            destination.name + ".agent-coop.tmp"
        )
        try:
            planted.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"file symlinks unavailable: {error}")

        coop_adapters.install_workspace_adapters(self.workspace)

        self.assertEqual(outside.read_text(encoding="utf-8"), "keep me\n")
        self.assertEqual(
            destination.read_text(encoding="utf-8"),
            coop_adapters.bundled_adapters()["claude"],
        )


if __name__ == "__main__":
    unittest.main()
