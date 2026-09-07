"""coop smoke — cheap offline preflight.

Composes existing primitives and invents no protocol state:

  board      fresh schema + human-seeded item + bind -> claim -> renew ->
             release, all inside a private temp dir (coopdb)
  tree       coop_process.prepare_tree launch + owned close, root exit and
             captured output confirmed
  providers  coop_start.probe_agent CLI resolution only — no invocation,
             no quota spend

Core checks gate PASS; provider readiness also gates PASS unless --offline.
Smoke never touches a real board.
"""
import sys
import tempfile
import time
import uuid
from pathlib import Path

from agent_coop import coop_process
from agent_coop import coop_start
from agent_coop import coopdb

CORE_PROVIDERS = ("claude", "codex", "grok")
_TREE_DEADLINE_S = 20.0

_CONTRACT = dict(
    title="smoke preflight", objective="verify the claim cycle",
    scope="temp board only", done_when="claim released",
    output_contract="none", context="coop smoke",
    allowed_actions=["read"], stop_conditions=["end of smoke"],
)


def _board_cycle(tmp):
    db = str(Path(tmp) / "smoke-board.db")
    conn = coopdb.connect(db)
    try:
        coopdb.init_db(conn)
        item_id = coopdb.create_item(
            conn, actor="human", session_id=None, **_CONTRACT)
        coopdb.register_agent(conn, "smoke")
        sid = uuid.uuid4().hex
        coopdb.insert_session(
            conn, session_id=sid, agent_id="smoke", provider="claude",
            command=["coop", "smoke"], cwd=tmp, max_runtime_s=600, grace_s=60)
        claim = coopdb.claim_item(
            conn, item_id=item_id, actor="smoke", session_id=sid,
            intent="smoke claim cycle")
        coopdb.renew_claims(conn, session_id=sid)
        coopdb.release_claim(
            conn, claim_id=claim["claim_id"], actor="smoke",
            session_id=sid, reason="smoke complete")
        return "schema + claim/renew/release ok"
    finally:
        conn.close()


def _tree_cycle():
    with tempfile.TemporaryFile() as out:
        prepared = coop_process.prepare_tree(
            [sys.executable, "-c", "print('smoke-ok')"],
            session_id=uuid.uuid4().hex, stdout=out)
        tree = prepared.release()
        try:
            deadline = time.monotonic() + _TREE_DEADLINE_S
            while tree.poll_root() is None:
                if time.monotonic() > deadline:
                    raise RuntimeError("smoke child did not exit in time")
                time.sleep(0.05)
        finally:
            tree.close()
        out.seek(0)
        if b"smoke-ok" not in out.read():
            raise RuntimeError("child output not captured through owned tree")
    return "process-tree launch + owned close ok"


def run_smoke(*, offline=False, probe=coop_start.probe_agent):
    """Run every preflight check; report, never raise."""
    checks = []

    def run(name, fn):
        try:
            checks.append({"name": name, "ok": True, "note": fn()})
        except Exception as exc:
            checks.append({"name": name, "ok": False,
                           "note": f"{type(exc).__name__}: {exc}"})

    with tempfile.TemporaryDirectory() as tmp:
        run("board", lambda: _board_cycle(tmp))
    run("tree", _tree_cycle)
    core_ok = all(check["ok"] for check in checks)

    providers = None
    start_ready = None
    if not offline:
        providers = {}
        for name in CORE_PROVIDERS:
            ok, note = probe(name)
            providers[name] = {"ok": bool(ok), "note": note}
        start_ready = all(entry["ok"] for entry in providers.values())

    return {
        "checks": checks,
        "providers": providers,
        "start_ready": start_ready,
        "ok": core_ok and start_ready is not False,
    }


def render_text(result):
    lines = []
    for check in result["checks"]:
        mark = "ok" if check["ok"] else "FAIL"
        lines.append(f"smoke: {check['name']} {mark} ({check['note']})")
    if result["providers"] is not None:
        for name, entry in result["providers"].items():
            mark = "ok" if entry["ok"] else "MISSING"
            lines.append(f"smoke: provider {name} {mark} ({entry['note']})")
        lines.append(
            "smoke: start-ready "
            + ("yes" if result["start_ready"] else "no"))
    lines.append("smoke: PASS" if result["ok"] else "smoke: FAIL")
    return lines
