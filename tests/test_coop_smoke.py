"""coop smoke: the offline preflight composes real primitives end-to-end."""
import contextlib
import io
import json
import unittest
from unittest import mock

from agent_coop import cli as coop
from agent_coop import coop_smoke


def probe_all_ok(name):
    return (True, "ok")


def probe_missing(name):
    return (False, f"cli not found: {name}")


class RunSmoke(unittest.TestCase):
    def test_core_checks_pass_offline(self):
        result = coop_smoke.run_smoke(offline=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual([c["name"] for c in result["checks"]],
                         ["board", "tree"])
        self.assertTrue(all(c["ok"] for c in result["checks"]), result)
        self.assertIsNone(result["providers"])
        self.assertIsNone(result["start_ready"])

    def test_missing_provider_fails_readiness_not_core(self):
        result = coop_smoke.run_smoke(probe=probe_missing)
        self.assertTrue(all(c["ok"] for c in result["checks"]), result)
        self.assertFalse(result["start_ready"])
        self.assertFalse(result["ok"])
        self.assertEqual(sorted(result["providers"]),
                         sorted(coop_smoke.CORE_PROVIDERS))

    def test_all_providers_ready_passes(self):
        result = coop_smoke.run_smoke(probe=probe_all_ok)
        self.assertTrue(result["start_ready"])
        self.assertTrue(result["ok"])

    def test_render_text_names_every_surface(self):
        result = coop_smoke.run_smoke(probe=probe_missing)
        text = "\n".join(coop_smoke.render_text(result))
        self.assertIn("smoke: board ok", text)
        self.assertIn("smoke: tree ok", text)
        self.assertIn("smoke: provider claude MISSING", text)
        self.assertIn("smoke: start-ready no", text)
        self.assertIn("smoke: FAIL", text)


class SmokeCli(unittest.TestCase):
    def test_cli_offline_json_passes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            coop.main(["--json", "smoke", "--offline"])
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["providers"])

    def test_cli_failure_exits_one(self):
        failing = {"checks": [{"name": "board", "ok": False, "note": "x"}],
                   "providers": None, "start_ready": None, "ok": False}
        out = io.StringIO()
        with mock.patch("agent_coop.coop_smoke.run_smoke",
                        return_value=failing):
            with contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit) as ctx:
                    coop.main(["smoke", "--offline"])
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("smoke: FAIL", out.getvalue())


if __name__ == "__main__":
    unittest.main()
