"""Kickoff contract templates (mesh and compiled mesh v2)."""
import json

from agent_coop import coop_templates
from agent_coop import coopdb


def test_mesh_template_renders_complete_contract():
    fields = coop_templates.render_contract_template(
        "mesh", goal="ping test", stamp="20260727-120000", nonce="abc123")
    for key in coopdb.CONTRACT_FIELDS:
        assert fields.get(key), f"missing contract field: {key}"
    assert "ping test" in fields["objective"]
    report = "docs/evidence/three-agent-ping-mesh-20260727-120000-abc123.md"
    assert report in fields["done_when"]
    assert report in fields["output_contract"]
    assert any(report in action for action in fields["allowed_actions"])
    assert "{" not in fields["done_when"]  # every placeholder resolved


def test_mesh_template_routing_line_states_the_serial_truth():
    fields = coop_templates.render_contract_template("mesh", goal="g")
    line = coop_templates.ONE_TURN_ROUTING_LINE
    assert line in fields["context"]
    # needs-input closes the claim after ONE question (a back-to-back
    # instruction would be impossible to follow); the
    # line must never re-promise multi-question routing in one turn.
    assert "ONE question" in line
    assert "back to back" not in line.replace(
        "do not attempt several needs-input commands in one turn", "")
    assert "answers ALL open questions" in line


def test_mesh_template_carries_composition_skeletons():
    fields = coop_templates.render_contract_template("mesh", goal="g")
    assert coop_templates.MESH_REPORT_SKELETON in fields["context"]
    assert "| from | to |" in fields["context"]
    assert coop_templates.RECEIPT_SKELETON_LINE in fields["context"]
    assert "Stop boundaries:" in coop_templates.RECEIPT_SKELETON_LINE


def test_stamps_differ_when_supplied():
    a = coop_templates.render_contract_template(
        "mesh", goal="g", stamp="a", nonce="n1")
    b = coop_templates.render_contract_template(
        "mesh", goal="g", stamp="b", nonce="n1")
    assert a["output_contract"] != b["output_contract"]


def test_same_second_renders_never_collide():
    # Frozen clock: identical stamp, default nonce (template creation is
    # instantaneous, so two kickoffs in one second must not share an
    # output contract).
    a = coop_templates.render_contract_template(
        "mesh", goal="g", stamp="20260727-131737")
    b = coop_templates.render_contract_template(
        "mesh", goal="g", stamp="20260727-131737")
    assert a["output_contract"] != b["output_contract"]
    assert a["done_when"] != b["done_when"]


def test_mesh_v2_is_explicit_and_does_not_change_serial_mesh():
    serial = coop_templates.render_contract_template(
        "mesh", goal="g", stamp="20260801-120000", nonce="abc123")
    compiled = coop_templates.render_contract_template(
        "mesh-v2", goal="g", stamp="20260801-120000", nonce="abc123")

    assert coop_templates.TEMPLATE_VERSIONS == {"mesh": 1, "mesh-v2": 2}
    assert coop_templates.ONE_TURN_ROUTING_LINE in serial["context"]
    assert coop_templates.MESH_V2_ROUTING_LINE not in serial["context"]
    assert coop_templates.MESH_V2_ROUTING_LINE in compiled["context"]
    assert "needs-input batch" in compiled["context"]
    assert "mesh-v2" in compiled["context"]
    assert "three-agent-ping-mesh-v2-" in compiled["output_contract"]
    assert serial["output_contract"] != compiled["output_contract"]


def test_template_creation_event_pins_source_contract_fingerprint(tmp_path):
    conn = coopdb.connect(str(tmp_path / "board.db"))
    try:
        coopdb.init_db(conn)
        rendered = coop_templates.render_contract_template(
            "mesh-v2",
            goal="pin this contract",
            stamp="20260801-120000",
            nonce="abc123",
        )
        provenance = coop_templates.contract_template_provenance(
            "mesh-v2", rendered)
        item_id = coopdb.create_item(
            conn,
            actor="human",
            session_id=None,
            template_provenance=provenance,
            **rendered,
        )
        payload = json.loads(conn.execute(
            "SELECT payload_json FROM events WHERE item_id=? AND "
            "event_type='item_created'",
            (item_id,),
        ).fetchone()[0])
        item = coopdb.item_show(conn, item_id)

        assert payload["template"] == provenance
        assert provenance == {
            "name": "mesh-v2",
            "version": 2,
            "contract_fingerprint": coopdb.contract_fingerprint(rendered),
        }
        assert coopdb.contract_fingerprint(item) == provenance[
            "contract_fingerprint"
        ]
    finally:
        conn.close()


def test_template_provenance_stays_bound_to_pre_override_render():
    rendered = coop_templates.render_contract_template(
        "mesh-v2", goal="g", stamp="20260801-120000", nonce="abc123")
    provenance = coop_templates.contract_template_provenance(
        "mesh-v2", rendered)
    overridden = dict(rendered, scope="an explicit non-template override")

    assert coopdb.contract_fingerprint(overridden) != provenance[
        "contract_fingerprint"
    ]
