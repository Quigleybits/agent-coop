"""Fail-closed compiler tests for the opt-in version-2 ping mesh."""

import json
import hashlib
import os
import pathlib
import uuid
from dataclasses import replace

import pytest

from agent_coop import coop_action_scheduler
from agent_coop import coop_autonomous
from agent_coop import coop_mesh
from agent_coop import coop_templates
from agent_coop import coopdb


class MeshBoard:
    def __init__(self, tmp_path, *, template="mesh-v2"):
        self.conn = coopdb.connect(str(tmp_path / f"{uuid.uuid4().hex}.db"))
        self.root = tmp_path
        coopdb.init_db(self.conn)
        self.sessions = {}
        for agent in coop_mesh.MESH_PARTICIPANTS:
            sid = f"s-{agent}-{uuid.uuid4().hex}"
            coopdb.insert_session(
                self.conn,
                session_id=sid,
                agent_id=agent,
                provider=agent,
                command=["python", "-c", "pass"],
                cwd=".",
                max_runtime_s=28800,
                grace_s=10,
            )
            self.sessions[agent] = sid
        rendered = coop_templates.render_contract_template(
            template,
            goal="six directed pings",
            stamp="20260801-120000",
            nonce=uuid.uuid4().hex[:6],
        )
        provenance = coop_templates.contract_template_provenance(
            template, rendered)
        self.item_id = coopdb.create_item(
            self.conn,
            actor="human",
            session_id=None,
            owner="claude",
            next_actor="claude",
            template_provenance=provenance,
            **rendered,
        )
        self.claim_id = coopdb.claim_item(
            self.conn,
            item_id=self.item_id,
            actor="claude",
            session_id=self.sessions["claude"],
            intent="start compiled mesh",
        )["claim_id"]

    def close(self):
        self.conn.close()

    def action(self, agent):
        return coopdb.status(
            self.conn,
            agent,
            session_id=self.sessions[agent],
            item_id=self.item_id,
        )["next_action"]

    def compile(self, agent):
        return coop_mesh.compile_mesh_phase(
            self.conn,
            item=coopdb.item_show(self.conn, self.item_id),
            action=self.action(agent),
            agent=agent,
        )

    def post_and_answer(self, plan):
        assert isinstance(plan, coop_mesh.MeshOutboundPlan)
        questions = tuple(
            (recipient, f"Ping from {plan.sender} to {recipient}.")
            for recipient in plan.recipients
        )
        question_ids = coopdb.needs_input_batch(
            self.conn,
            claim_id=plan.claim_id,
            session_id=self.sessions[plan.sender],
            questions=questions,
        )
        for recipient, question_id in zip(plan.recipients, question_ids):
            response_claim = coopdb.claim_questions_batch(
                self.conn,
                question_ids=[question_id],
                session_id=self.sessions[recipient],
                intent=f"answer {question_id}",
            )[0]["claim_id"]
            coopdb.answer_questions_batch(
                self.conn,
                session_id=self.sessions[recipient],
                answers=[(
                    response_claim,
                    f"Acknowledged by {recipient} for {plan.sender}.",
                )],
            )
        self.claim_id = coopdb.claim_item(
            self.conn,
            item_id=self.item_id,
            actor=plan.sender,
            session_id=self.sessions[plan.sender],
            intent=f"resume {plan.sender}",
            reclaim_reason="all outbound answers arrived",
        )["claim_id"]
        return question_ids

    def transfer(self, plan):
        assert isinstance(plan, coop_mesh.MeshTransferPlan)
        created = coopdb.create_handoff(
            self.conn,
            claim_id=plan.claim_id,
            session_id=self.sessions[plan.sender],
            actor=plan.sender,
            to_agent=plan.to_agent,
            reason=plan.reason,
            summary=plan.summary,
            completed=plan.completed,
            remaining=plan.remaining,
            risks=plan.risks,
            next_action=plan.next_action,
            proof_refs=plan.proof_refs,
        )
        accepted = coopdb.accept_handoff(
            self.conn,
            handoff_id=created["handoff_id"],
            session_id=self.sessions[plan.to_agent],
            actor=plan.to_agent,
            intent=f"accept compiled mesh handoff {created['handoff_id']}",
        )
        self.claim_id = accepted["claim_id"]

    def reach_composition(self):
        for sender in coop_mesh.MESH_PARTICIPANTS:
            outbound = self.compile(sender)
            self.post_and_answer(outbound)
            phase = self.compile(sender)
            if isinstance(phase, coop_mesh.MeshTransferPlan):
                self.transfer(phase)
            else:
                return phase
        raise AssertionError("mesh did not reach composition")

    def reach_receipt(self):
        plan = self.reach_composition()
        (self.root / "docs" / "evidence").mkdir(parents=True, exist_ok=True)
        result = coop_autonomous.postcommit_mesh_report(
            self.conn,
            plan=plan,
            value=coop_mesh.default_mesh_report_value(),
            agent="grok",
            session_id=self.sessions["grok"],
            lease_seconds=3600,
            workspace=self.root,
        )
        assert result is not None
        return plan, result


@pytest.fixture
def mesh(tmp_path):
    board = MeshBoard(tmp_path)
    try:
        yield board
    finally:
        board.close()


def test_compiler_maps_all_three_legal_owner_phases(mesh):
    claude_out = mesh.compile("claude")
    assert isinstance(claude_out, coop_mesh.MeshOutboundPlan)
    assert claude_out.recipients == ("codex", "grok")
    assert claude_out.action_fingerprint == (
        coop_action_scheduler.action_fingerprint(mesh.action("claude"))
    )

    mesh.post_and_answer(claude_out)
    claude_transfer = mesh.compile("claude")
    assert isinstance(claude_transfer, coop_mesh.MeshTransferPlan)
    assert claude_transfer.to_agent == "codex"
    assert len(claude_transfer.proof_refs) == 2
    assert all(ref.startswith("event:") for ref in claude_transfer.proof_refs)
    mesh.transfer(claude_transfer)

    codex_out = mesh.compile("codex")
    assert isinstance(codex_out, coop_mesh.MeshOutboundPlan)
    assert codex_out.recipients == ("claude", "grok")
    mesh.post_and_answer(codex_out)
    codex_transfer = mesh.compile("codex")
    assert isinstance(codex_transfer, coop_mesh.MeshTransferPlan)
    assert codex_transfer.to_agent == "grok"
    mesh.transfer(codex_transfer)

    grok_out = mesh.compile("grok")
    assert isinstance(grok_out, coop_mesh.MeshOutboundPlan)
    assert grok_out.recipients == ("claude", "codex")
    mesh.post_and_answer(grok_out)
    composition = mesh.compile("grok")
    assert isinstance(composition, coop_mesh.MeshComposePlan)
    assert [(entry.sender, entry.recipient) for entry in composition.exchanges] == list(
        coop_mesh.MESH_PAIRS
    )
    assert all(entry.answer_event_id > 0 for entry in composition.exchanges)
    assert "three-agent-ping-mesh-v2-" in composition.report_path


def test_old_serial_mesh_never_compiles(tmp_path):
    board = MeshBoard(tmp_path, template="mesh")
    try:
        assert board.compile("claude") is None
    finally:
        board.close()


def test_revised_or_fingerprint_mismatched_contract_never_compiles(mesh):
    action = mesh.action("claude")
    item = coopdb.item_show(mesh.conn, mesh.item_id)
    mesh.conn.execute(
        "UPDATE items SET contract_version=2 WHERE id=?", (mesh.item_id,)
    )
    mesh.conn.commit()
    assert coop_mesh.compile_mesh_phase(
        mesh.conn, item=coopdb.item_show(mesh.conn, mesh.item_id),
        action=action, agent="claude") is None

    mesh.conn.execute(
        "UPDATE items SET contract_version=1, scope='changed' WHERE id=?",
        (mesh.item_id,),
    )
    mesh.conn.commit()
    assert coop_mesh.compile_mesh_phase(
        mesh.conn, item=coopdb.item_show(mesh.conn, mesh.item_id),
        action=action, agent="claude") is None


def test_missing_expected_live_participant_never_compiles(mesh):
    mesh.conn.execute(
        "UPDATE sessions SET status='exited' WHERE session_id=?",
        (mesh.sessions["grok"],),
    )
    mesh.conn.commit()
    assert mesh.compile("claude") is None


def test_stale_or_noncanonical_continue_action_never_compiles(mesh):
    action = dict(mesh.action("claude"))
    action["claim_id"] += 1
    assert coop_mesh.compile_mesh_phase(
        mesh.conn,
        item=coopdb.item_show(mesh.conn, mesh.item_id),
        action=action,
        agent="claude",
    ) is None


@pytest.mark.parametrize("mutation", ["duplicate", "foreign", "wrong_answerer"])
def test_malformed_exchange_rows_fail_closed(mesh, mutation):
    outbound = mesh.compile("claude")
    mesh.post_and_answer(outbound)
    if mutation == "duplicate":
        source = mesh.conn.execute(
            "SELECT * FROM questions WHERE item_id=? ORDER BY question_id LIMIT 1",
            (mesh.item_id,),
        ).fetchone()
        mesh.conn.execute(
            "INSERT INTO questions(item_id,exact_question,asked_by_agent,"
            "asked_by_session,assigned_to_agent,status,answer,"
            "answered_by_agent,answered_by_session,asked_at,answered_at) "
            "VALUES (?,?,?,?,?,'answered',?,?,?,?,?)",
            (
                mesh.item_id, source["exact_question"], "claude",
                mesh.sessions["claude"], source["assigned_to_agent"],
                "duplicate", source["assigned_to_agent"],
                mesh.sessions[source["assigned_to_agent"]],
                coopdb.now(), coopdb.now(),
            ),
        )
    elif mutation == "foreign":
        mesh.conn.execute(
            "INSERT INTO questions(item_id,exact_question,asked_by_agent,"
            "asked_by_session,assigned_to_agent,status,asked_at) "
            "VALUES (?,?,?,?,?,'open',?)",
            (
                mesh.item_id, "foreign", "codex", mesh.sessions["codex"],
                "grok", coopdb.now(),
            ),
        )
    else:
        mesh.conn.execute(
            "UPDATE questions SET answered_by_agent='claude' "
            "WHERE question_id=(SELECT MIN(question_id) FROM questions "
            "WHERE item_id=?)",
            (mesh.item_id,),
        )
    mesh.conn.commit()

    assert mesh.compile("claude") is None


def test_existing_handoff_must_match_code_rendered_fields(mesh):
    outbound = mesh.compile("claude")
    mesh.post_and_answer(outbound)
    transfer = mesh.compile("claude")
    mesh.transfer(transfer)
    mesh.conn.execute(
        "UPDATE handoffs SET summary='tampered' WHERE item_id=?",
        (mesh.item_id,),
    )
    mesh.conn.commit()

    assert mesh.compile("codex") is None


def test_creation_provenance_is_exact_and_bounded(mesh):
    payload = json.loads(mesh.conn.execute(
        "SELECT payload_json FROM events WHERE item_id=? AND "
        "event_type='item_created'",
        (mesh.item_id,),
    ).fetchone()[0])
    assert set(payload["template"]) == {
        "name", "version", "contract_fingerprint"
    }
    assert len(payload["template"]["contract_fingerprint"]) == 64


def test_compiled_profiles_promote_only_safe_arms(mesh):
    outbound = mesh.compile("claude")
    admitted = coop_action_scheduler.DispatchProfile(
        lane=None,
        workspace_surface="write",
        execution_mode="tool_turn",
        action_fingerprint=outbound.action_fingerprint,
    )
    promoted = coop_autonomous.compiled_mesh_profile(
        admitted, outbound, agent="claude")
    assert (promoted.workspace_surface, promoted.execution_mode) == (
        "none", "isolated_structured_mesh_questions")
    failed = coop_autonomous.compiled_mesh_profile(
        admitted,
        outbound,
        agent="claude",
        failed_isolated={("claude", outbound.action_fingerprint)},
    )
    assert failed == admitted

    # Provider-specific A/B guard: Codex/Grok outbound text stays on the
    # ordinary tool path until each adapter has its own one-token preflight.
    codex_outbound = replace(outbound, sender="codex")
    assert coop_autonomous.compiled_mesh_profile(
        admitted, codex_outbound, agent="codex") == admitted

    mesh.post_and_answer(outbound)
    transfer = mesh.compile("claude")
    admitted = replace(
        admitted, action_fingerprint=transfer.action_fingerprint)
    mechanical = coop_autonomous.compiled_mesh_profile(
        admitted, transfer, agent="claude")
    assert (mechanical.workspace_surface, mechanical.execution_mode) == (
        "none", "compiled_mesh_transfer")


def test_outbound_request_exposes_text_choice_but_no_board_authority(mesh):
    plan = mesh.compile("claude")
    request = coop_autonomous.mesh_questions_decision_request(
        plan,
        item=coopdb.item_show(mesh.conn, mesh.item_id),
        provider="claude",
    )
    assert request.decision_kind == "compose_mesh_questions"
    assert request.action_fingerprint == plan.action_fingerprint
    assert "--claim" not in request.prompt
    assert mesh.sessions["claude"] not in request.prompt
    assert request.json_schema["properties"]["questions"]["items"][
        "properties"
    ]["recipient"]["enum"] == ["codex", "grok"]


def test_outbound_postcommit_rechecks_full_plan_and_recipient_order(mesh):
    plan = mesh.compile("claude")
    value = {
        "questions": [
            {"recipient": "codex", "question": "Ping Codex."},
            {"recipient": "grok", "question": "Ping Grok."},
        ]
    }
    result = coop_autonomous.postcommit_mesh_questions(
        mesh.conn,
        plan=plan,
        value=value,
        agent="claude",
        session_id=mesh.sessions["claude"],
        lease_seconds=3600,
    )
    assert len(result) == 2
    rows = mesh.conn.execute(
        "SELECT assigned_to_agent, exact_question FROM questions "
        "WHERE item_id=? ORDER BY question_id",
        (mesh.item_id,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("codex", "Ping Codex."), ("grok", "Ping Grok.")
    ]


def test_outbound_postcommit_stale_or_recipient_tampering_writes_nothing(mesh):
    for value in (
        {
            "questions": [
                {"recipient": "grok", "question": "wrong order"},
                {"recipient": "codex", "question": "wrong order"},
            ]
        },
        {
            "questions": [
                {"recipient": "codex", "question": "one only"},
            ]
        },
        {
            "questions": [
                {"recipient": "codex", "question": "\ud800"},
                {"recipient": "grok", "question": "valid"},
            ]
        },
    ):
        plan = mesh.compile("claude")
        assert coop_autonomous.postcommit_mesh_questions(
            mesh.conn,
            plan=plan,
            value=value,
            agent="claude",
            session_id=mesh.sessions["claude"],
            lease_seconds=3600,
        ) is None
        assert mesh.conn.execute(
            "SELECT COUNT(*) FROM questions WHERE item_id=?",
            (mesh.item_id,),
        ).fetchone()[0] == 0

    plan = mesh.compile("claude")
    mesh.conn.execute(
        "UPDATE items SET scope='changed after inference' WHERE id=?",
        (mesh.item_id,),
    )
    mesh.conn.commit()
    valid = {
        "questions": [
            {"recipient": "codex", "question": "Ping Codex."},
            {"recipient": "grok", "question": "Ping Grok."},
        ]
    }
    assert coop_autonomous.postcommit_mesh_questions(
        mesh.conn,
        plan=plan,
        value=valid,
        agent="claude",
        session_id=mesh.sessions["claude"],
        lease_seconds=3600,
    ) is None
    assert mesh.conn.execute(
        "SELECT COUNT(*) FROM questions WHERE item_id=?", (mesh.item_id,)
    ).fetchone()[0] == 0


def test_mechanical_transfer_rechecks_plan_before_canonical_write(mesh):
    outbound = mesh.compile("claude")
    mesh.post_and_answer(outbound)
    plan = mesh.compile("claude")
    result = coop_autonomous.postcommit_mesh_transfer(
        mesh.conn,
        plan=plan,
        agent="claude",
        session_id=mesh.sessions["claude"],
        lease_seconds=3600,
    )
    assert result["handoff_id"] > 0
    row = mesh.conn.execute(
        "SELECT * FROM handoffs WHERE handoff_id=?", (result["handoff_id"],)
    ).fetchone()
    assert row["summary"] == plan.summary
    assert json.loads(row["proof_references"]) == [
        {"id": int(ref.split(":", 1)[1]), "type": "event"}
        for ref in plan.proof_refs
    ]


def test_mechanical_transfer_stale_evidence_writes_nothing(mesh):
    outbound = mesh.compile("claude")
    mesh.post_and_answer(outbound)
    plan = mesh.compile("claude")
    mesh.conn.execute(
        "UPDATE questions SET answer='tampered after inference' "
        "WHERE question_id=(SELECT MIN(question_id) FROM questions "
        "WHERE item_id=?)",
        (mesh.item_id,),
    )
    mesh.conn.commit()
    assert coop_autonomous.postcommit_mesh_transfer(
        mesh.conn,
        plan=plan,
        agent="claude",
        session_id=mesh.sessions["claude"],
        lease_seconds=3600,
    ) is None
    assert mesh.conn.execute(
        "SELECT COUNT(*) FROM handoffs WHERE item_id=?", (mesh.item_id,)
    ).fetchone()[0] == 0


def report_value(**overrides):
    value = {
        "what_was_asked": "Exchange one directed ping on every provider pair.",
        "method": "Each sender posted board questions; each peer answered itself.",
        "stop_boundaries": ["none"],
        "what_this_proves": "The six directed board routes completed.",
        "what_this_does_not_prove": "It does not benchmark substantive work.",
        "receipt_summary": (
            "Done: six directed pings and one evidence report. "
            "Not done: none. Stop boundaries: none."
        ),
    }
    value.update(overrides)
    return value


def test_report_renderer_owns_structure_order_and_evidence(mesh):
    plan = mesh.reach_composition()
    rendered = coop_mesh.render_mesh_report(plan, report_value())

    assert rendered.startswith("# Three-agent ping mesh - results\n")
    assert rendered.count("\n## ") == 5
    assert "| from | to | ping text | response text | outcome |" in rendered
    lines = [line for line in rendered.splitlines() if line.startswith("| ")]
    data = lines[1:]
    assert [
        tuple(cell.strip() for cell in line.strip("|").split(" | ")[:2])
        for line in data
    ] == list(
        coop_mesh.MESH_PAIRS
    )
    assert len(data) == 6
    assert all(line.endswith("| answered |") for line in data)
    for exchange in plan.exchanges:
        assert exchange.question in rendered
        assert exchange.answer in rendered
    assert "## 4. Not completed / stop boundaries\n- none" in rendered


def test_report_renderer_neutralizes_markdown_and_html_in_model_text(mesh):
    plan = mesh.reach_composition()
    exchanges = list(plan.exchanges)
    exchanges[0] = replace(
        exchanges[0],
        question="Ping | split\n## forged <script>alert(1)</script>",
        answer="Ack | yes\n# forged",
    )
    plan = replace(plan, exchanges=tuple(exchanges))
    value = report_value(
        what_was_asked="# forged heading\n<script>bad</script>",
        method="normal\n## forged method",
    )
    rendered = coop_mesh.render_mesh_report(plan, value)

    assert rendered.count("\n## ") == 5
    assert "<script>" not in rendered
    assert "\\|" in rendered
    assert "<br>" in rendered


def test_report_postcommit_exclusively_creates_file_and_hashed_receipt(mesh):
    plan = mesh.reach_composition()
    (mesh.root / "docs" / "evidence").mkdir(parents=True)
    result = coop_autonomous.postcommit_mesh_report(
        mesh.conn,
        plan=plan,
        value=report_value(),
        agent="grok",
        session_id=mesh.sessions["grok"],
        lease_seconds=3600,
        workspace=mesh.root,
    )
    assert result["receipt_id"] > 0
    target = mesh.root / pathlib.PurePosixPath(plan.report_path)
    data = target.read_bytes()
    receipt = mesh.conn.execute(
        "SELECT * FROM receipts WHERE receipt_id=?", (result["receipt_id"],)
    ).fetchone()
    assert receipt["source_path"] == str(target.resolve())
    assert receipt["sha256"] == hashlib.sha256(data).hexdigest()
    assert receipt["summary"] == report_value()["receipt_summary"]
    assert json.loads(receipt["proof_references_json"]) == [
        {"id": exchange.answer_event_id, "type": "event"}
        for exchange in plan.exchanges
    ]
    assert mesh.conn.execute(
        "SELECT COUNT(*) FROM events WHERE item_id=? AND "
        "event_type='receipt_submitted'",
        (mesh.item_id,),
    ).fetchone()[0] == 1


@pytest.mark.parametrize("case", ["malformed", "stale", "collision"])
def test_report_postcommit_failures_leave_no_new_artifact_or_receipt(mesh, case):
    plan = mesh.reach_composition()
    evidence_dir = mesh.root / "docs" / "evidence"
    evidence_dir.mkdir(parents=True)
    target = mesh.root / pathlib.PurePosixPath(plan.report_path)
    value = report_value()
    original = None
    if case == "malformed":
        value = report_value(stop_boundaries=["invented shortfall"])
    elif case == "stale":
        mesh.conn.execute(
            "UPDATE questions SET answer='changed after composition' "
            "WHERE question_id=(SELECT MIN(question_id) FROM questions "
            "WHERE item_id=?)",
            (mesh.item_id,),
        )
        mesh.conn.commit()
    else:
        original = b"existing user evidence\n"
        target.write_bytes(original)

    assert coop_autonomous.postcommit_mesh_report(
        mesh.conn,
        plan=plan,
        value=value,
        agent="grok",
        session_id=mesh.sessions["grok"],
        lease_seconds=3600,
        workspace=mesh.root,
    ) is None
    assert mesh.conn.execute(
        "SELECT COUNT(*) FROM receipts WHERE item_id=?", (mesh.item_id,)
    ).fetchone()[0] == 0
    if original is None:
        assert not target.exists()
    else:
        assert target.read_bytes() == original


def test_report_receipt_failure_removes_only_the_new_file(mesh, monkeypatch):
    plan = mesh.reach_composition()
    (mesh.root / "docs" / "evidence").mkdir(parents=True)
    target = mesh.root / pathlib.PurePosixPath(plan.report_path)

    def fail_receipt(*_args, **_kwargs):
        raise RuntimeError("injected receipt failure")

    monkeypatch.setattr(coopdb, "submit_receipt", fail_receipt)
    assert coop_autonomous.postcommit_mesh_report(
        mesh.conn,
        plan=plan,
        value=report_value(),
        agent="grok",
        session_id=mesh.sessions["grok"],
        lease_seconds=3600,
        workspace=mesh.root,
    ) is None
    assert not target.exists()


def test_report_target_rejects_escape_and_symlink_parent(mesh, tmp_path):
    assert coop_mesh.resolve_mesh_report_target(
        mesh.root, "../outside.md") is None
    evidence = mesh.root / "docs" / "evidence"
    evidence.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, evidence, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this Windows host")
    assert coop_mesh.resolve_mesh_report_target(
        mesh.root, "docs/evidence/report.md") is None


def test_report_profile_defaults_to_zero_model_and_keeps_ab_arm(mesh):
    plan = mesh.reach_composition()
    admitted = coop_action_scheduler.DispatchProfile(
        lane=None,
        workspace_surface="write",
        execution_mode="tool_turn",
        action_fingerprint=plan.action_fingerprint,
    )
    mechanical = coop_autonomous.compiled_mesh_profile(
        admitted, plan, agent="grok")
    assert (mechanical.workspace_surface, mechanical.execution_mode) == (
        "write", "compiled_mesh_report")
    experimental = coop_autonomous.compiled_mesh_profile(
        admitted,
        plan,
        agent="grok",
        structured_report_providers={"grok"},
    )
    assert (experimental.workspace_surface, experimental.execution_mode) == (
        "write", "isolated_structured_mesh_report")


def test_report_request_contains_narrative_facts_not_authority(mesh):
    plan = mesh.reach_composition()
    request = coop_autonomous.mesh_report_decision_request(
        plan,
        item=coopdb.item_show(mesh.conn, mesh.item_id),
        provider="grok",
    )
    assert request.decision_kind == "compose_mesh_report_sections"
    assert request.action_fingerprint == plan.action_fingerprint
    assert plan.report_path not in request.prompt
    assert "event:" not in request.prompt
    assert mesh.sessions["grok"] not in request.prompt


def test_evidence_validator_binds_contract_rows_report_receipt_and_hash(mesh):
    compose, result = mesh.reach_receipt()
    packet = coop_mesh.validate_mesh_evidence(
        mesh.conn, item_id=mesh.item_id, workspace=mesh.root)
    assert packet.item_id == mesh.item_id
    assert packet.receipt_id == result["receipt_id"]
    assert packet.report_path == compose.report_path
    assert packet.report_sha256 == hashlib.sha256(
        pathlib.Path(result["path"]).read_bytes()
    ).hexdigest()
    assert packet.exchanges == compose.exchanges
    assert packet.owner == "grok"


@pytest.mark.parametrize("mutation", ["file", "receipt_ref", "receipt_event"])
def test_evidence_validator_rejects_any_packet_drift(mesh, mutation):
    _compose, result = mesh.reach_receipt()
    if mutation == "file":
        pathlib.Path(result["path"]).write_text(
            "# forged evidence\n", encoding="utf-8")
    elif mutation == "receipt_ref":
        mesh.conn.execute(
            "UPDATE receipts SET proof_references_json='[]' "
            "WHERE receipt_id=?",
            (result["receipt_id"],),
        )
        mesh.conn.commit()
    else:
        mesh.conn.execute(
            "UPDATE events SET payload_json='{}' WHERE item_id=? AND "
            "event_type='receipt_submitted'",
            (mesh.item_id,),
        )
        mesh.conn.commit()
    assert coop_mesh.validate_mesh_evidence(
        mesh.conn, item_id=mesh.item_id, workspace=mesh.root) is None


def test_review_request_is_compiled_to_named_independent_provider(mesh):
    _compose, _result = mesh.reach_receipt()
    action = mesh.action("grok")
    plan = coop_mesh.compile_mesh_review_request(
        mesh.conn,
        item=coopdb.item_show(mesh.conn, mesh.item_id),
        action=action,
        agent="grok",
        workspace=mesh.root,
    )
    assert isinstance(plan, coop_mesh.MeshReviewRequestPlan)
    assert plan.reviewer == "claude"
    assert plan.owner == "grok"
    admitted = coop_action_scheduler.DispatchProfile(
        lane=None,
        workspace_surface="write",
        execution_mode="tool_turn",
        action_fingerprint=plan.action_fingerprint,
    )
    profile = coop_autonomous.compiled_mesh_review_profile(
        admitted, plan, agent="grok")
    assert (profile.workspace_surface, profile.execution_mode) == (
        "read", "compiled_mesh_review_request")

    created = coop_autonomous.postcommit_mesh_review_request(
        mesh.conn,
        plan=plan,
        agent="grok",
        session_id=mesh.sessions["grok"],
        lease_seconds=3600,
        workspace=mesh.root,
    )
    row = mesh.conn.execute(
        "SELECT reviewer, requested_by_agent FROM reviews WHERE id=?",
        (created["review_id"],),
    ).fetchone()
    assert tuple(row) == ("claude", "grok")


def test_claimed_mesh_review_compiles_to_bounded_independent_decision(mesh):
    _compose, _result = mesh.reach_receipt()
    request_plan = coop_mesh.compile_mesh_review_request(
        mesh.conn,
        item=coopdb.item_show(mesh.conn, mesh.item_id),
        action=mesh.action("grok"),
        agent="grok",
        workspace=mesh.root,
    )
    created = coop_autonomous.postcommit_mesh_review_request(
        mesh.conn,
        plan=request_plan,
        agent="grok",
        session_id=mesh.sessions["grok"],
        lease_seconds=3600,
        workspace=mesh.root,
    )
    claim, _packet = coopdb.claim_review(
        mesh.conn,
        review_id=created["review_id"],
        session_id=mesh.sessions["claude"],
        intent="review compiled mesh evidence",
        lease_seconds=3600,
    )
    action = mesh.action("claude")
    plan = coop_mesh.compile_mesh_review(
        mesh.conn,
        item=coopdb.item_show(mesh.conn, mesh.item_id),
        action=action,
        agent="claude",
        workspace=mesh.root,
    )
    assert isinstance(plan, coop_mesh.MeshReviewPlan)
    assert plan.claim_id == claim["claim_id"]
    assert plan.review_id == created["review_id"]
    assert plan.reviewer == "claude"
    assert plan.owner == "grok"

    admitted = coop_action_scheduler.DispatchProfile(
        lane=f"review:{plan.review_id}",
        workspace_surface="read",
        execution_mode="tool_turn",
        action_fingerprint=plan.action_fingerprint,
    )
    profile = coop_autonomous.compiled_mesh_review_profile(
        admitted, plan, agent="claude")
    assert (profile.workspace_surface, profile.execution_mode) == (
        "read", "isolated_structured_mesh_review")
    request = coop_autonomous.mesh_review_decision_request(
        plan,
        item=coopdb.item_show(mesh.conn, mesh.item_id),
        provider="claude",
    )
    assert request.decision_kind == "review_mesh_report"
    assert request.json_schema["properties"]["review_id"]["const"] == (
        plan.review_id)
    assert plan.report_path not in request.prompt
    assert mesh.sessions["claude"] not in request.prompt


def test_review_postcommit_rechecks_packet_and_uses_canonical_verdict(mesh):
    _compose, _result = mesh.reach_receipt()
    request_plan = coop_mesh.compile_mesh_review_request(
        mesh.conn,
        item=coopdb.item_show(mesh.conn, mesh.item_id),
        action=mesh.action("grok"),
        agent="grok",
        workspace=mesh.root,
    )
    created = coop_autonomous.postcommit_mesh_review_request(
        mesh.conn,
        plan=request_plan,
        agent="grok",
        session_id=mesh.sessions["grok"],
        lease_seconds=3600,
        workspace=mesh.root,
    )
    coopdb.claim_review(
        mesh.conn,
        review_id=created["review_id"],
        session_id=mesh.sessions["claude"],
        intent="review compiled mesh evidence",
        lease_seconds=3600,
    )
    plan = coop_mesh.compile_mesh_review(
        mesh.conn,
        item=coopdb.item_show(mesh.conn, mesh.item_id),
        action=mesh.action("claude"),
        agent="claude",
        workspace=mesh.root,
    )
    verdict = {
        "review_id": plan.review_id,
        "verdict": "approve",
        "body": "",
    }
    result = coop_autonomous.postcommit_mesh_review(
        mesh.conn,
        plan=plan,
        value=verdict,
        agent="claude",
        session_id=mesh.sessions["claude"],
        lease_seconds=3600,
        workspace=mesh.root,
    )
    assert result["verdict"] == "approve"
    assert mesh.conn.execute(
        "SELECT status FROM reviews WHERE id=?", (plan.review_id,)
    ).fetchone()[0] == "approved"


def test_review_postcommit_stale_file_makes_zero_board_writes(mesh):
    _compose, receipt = mesh.reach_receipt()
    request_plan = coop_mesh.compile_mesh_review_request(
        mesh.conn,
        item=coopdb.item_show(mesh.conn, mesh.item_id),
        action=mesh.action("grok"),
        agent="grok",
        workspace=mesh.root,
    )
    created = coop_autonomous.postcommit_mesh_review_request(
        mesh.conn,
        plan=request_plan,
        agent="grok",
        session_id=mesh.sessions["grok"],
        lease_seconds=3600,
        workspace=mesh.root,
    )
    coopdb.claim_review(
        mesh.conn,
        review_id=created["review_id"],
        session_id=mesh.sessions["claude"],
        intent="review compiled mesh evidence",
        lease_seconds=3600,
    )
    plan = coop_mesh.compile_mesh_review(
        mesh.conn,
        item=coopdb.item_show(mesh.conn, mesh.item_id),
        action=mesh.action("claude"),
        agent="claude",
        workspace=mesh.root,
    )
    before = mesh.conn.execute(
        "SELECT COUNT(*) FROM events WHERE item_id=?", (mesh.item_id,)
    ).fetchone()[0]
    pathlib.Path(receipt["path"]).write_text("changed", encoding="utf-8")
    assert coop_autonomous.postcommit_mesh_review(
        mesh.conn,
        plan=plan,
        value={
            "review_id": plan.review_id,
            "verdict": "approve",
            "body": "",
        },
        agent="claude",
        session_id=mesh.sessions["claude"],
        lease_seconds=3600,
        workspace=mesh.root,
    ) is None
    assert mesh.conn.execute(
        "SELECT COUNT(*) FROM events WHERE item_id=?", (mesh.item_id,)
    ).fetchone()[0] == before
    assert mesh.conn.execute(
        "SELECT status FROM reviews WHERE id=?", (plan.review_id,)
    ).fetchone()[0] == "requested"
