"""Kickoff contract templates: complete human contracts with zero model
calls (faster, cheaper, and deterministic vs `item create --expand`).

A template renders to a full contract-field dict, so the item enters the
board as a complete human contract and skips the acceptance huddle. The
routing guidance states the SERIAL protocol truth: needs-input closes the
implementation claim after one question (a "route everything back to back"
instruction is impossible under that rule and costs every owner a
proof-and-recover cycle; batch routing needs a real `needs-input batch`
primitive first)."""

from __future__ import annotations

import secrets
import time


ONE_TURN_ROUTING_LINE = (
    "Routing discipline: a needs-input closes your implementation claim "
    "after ONE question, so route your single most-blocking outbound "
    "question, end the turn, and route the next only after resuming - do "
    "not attempt several needs-input commands in one turn (the second is "
    "always refused). Every peer answers ALL open questions addressed to "
    "it in one turn."
)

# Composition is the largest per-turn cost; skeletons cut drafting to
# fill-in-the-cells.
MESH_REPORT_SKELETON = (
    "Report skeleton - copy verbatim into the report file and fill it in; "
    "do not redesign the structure:\n"
    "# Three-agent ping mesh - results\n"
    "## 1. What was asked\n(one sentence)\n"
    "## 2. Method\n(2-3 sentences: board-only needs-input routing)\n"
    "## 3. Results\n"
    "| from | to | ping text | response text | outcome |\n"
    "|---|---|---|---|---|\n"
    "(6 rows, outcome = answered | not-completed)\n"
    "## 4. Not completed / stop boundaries\n(list or 'none')\n"
    "## 5. What this does and does not prove\n(2-3 sentences)"
)

RECEIPT_SKELETON_LINE = (
    "Receipt summary skeleton - use exactly this shape: "
    "'Done: <what>. Not done: <what, or none>. Stop boundaries: <where "
    "work stopped and why>.' Keep it under five sentences."
)

MESH_V2_ROUTING_LINE = (
    "Compiled mesh-v2 routing discipline: when the runner recognizes this "
    "exact unchanged contract under --token-efficient, each owner supplies "
    "text for both outbound peers in one bounded decision and the runner "
    "commits both ordinary questions with `needs-input batch`. Each addressed "
    "peer still authors its own answer. Unknown or changed state falls back "
    "to the ordinary tool-enabled protocol."
)

CONTRACT_TEMPLATES = {
    "mesh": {
        "objective": (
            "All three registered providers ping each other, answer every "
            "ping, and record the results in one markdown report. "
            "Goal as given: {goal}"
        ),
        "scope": (
            "All three registered providers (claude, codex, grok) exchange "
            "directed pings over the board only. Full mesh = 6 ordered "
            "pairs: claude->codex, claude->grok, codex->claude, "
            "codex->grok, grok->claude, grok->codex. Each ping is a board "
            "'needs-input' question routed to the named peer; each response "
            "is that peer's own answer, authored under its own identity. "
            "The owner then writes ONE markdown report. No source code, "
            "tests, or protocol behaviour change in this item."
        ),
        "done_when": (
            "Every ordered pair above is represented on the board by a "
            "question authored by the sender and an answer authored by the "
            "addressed peer (or, where a pair could not be completed, an "
            "honest recorded shortfall naming the pair and the blocking "
            "reason); AND {report_path} exists containing the per-pair "
            "table and the shortfall list; AND a receipt is filed whose "
            "summary names both what was done and what was not; AND one "
            "independent provider approves the receipt."
        ),
        "output_contract": (
            "Exactly one new file: {report_path}. Required sections: "
            "(1) What was asked; (2) Method - how pings were routed on the "
            "board; (3) Results table with columns: from, to, ping text, "
            "response text, outcome (answered | not-completed); (4) Not "
            "completed / stop boundaries; (5) What this does and does not "
            "prove about Co-op routing. No other file is created or "
            "modified."
        ),
        "context": (
            "Complete human contract from the 'mesh' kickoff template - "
            "no acceptance huddle. This is a liveness/routing demonstration "
            "of the Co-op board, not a feature. Author identity matters: "
            "the owner must NOT ping or answer on another provider's "
            "behalf and must NOT use 'coop say' as a substitute (say is "
            "passive and creates no actionable work). Lanes are transferred "
            "with structured handoffs so each provider authors its own "
            "board writes. Providers may be idle-but-live; old last_seen_at "
            "is not unavailability. " + ONE_TURN_ROUTING_LINE + " "
            + MESH_REPORT_SKELETON + " " + RECEIPT_SKELETON_LINE
        ),
        "allowed_actions": [
            "item claim / handoff / accept-handoff",
            "needs-input question routed to a named peer",
            "answer a routed question",
            "coop say for non-binding narration only",
            "create {report_path}",
            "receipt file and review request/record",
        ],
        "stop_conditions": [
            "Do not create or modify any file other than the single report file",
            "Do not author a ping or a response on another provider's behalf; a proxied exchange is a failed pair, not a completed one",
            "Do not ask 'human' or use admin commands; route every question to a peer",
            "Do not fabricate or simulate a peer response; an unanswered pair is recorded as not-completed with its reason",
            "Do not run git commit or push",
        ],
    },
}

# The serial template above is retained unchanged as `mesh`; `mesh-v2` is a
# separately selected contract version, so boards using the existing default
# cannot drift.
CONTRACT_TEMPLATES["mesh-v2"] = {
    **CONTRACT_TEMPLATES["mesh"],
    "context": CONTRACT_TEMPLATES["mesh"]["context"].replace(
        "Complete human contract from the 'mesh' kickoff template",
        "Complete human contract from the 'mesh-v2' kickoff template",
    ).replace(ONE_TURN_ROUTING_LINE, MESH_V2_ROUTING_LINE),
    "allowed_actions": [
        (
            "needs-input batch with one ordinary question routed to each "
            "named peer"
            if action == "needs-input question routed to a named peer"
            else action
        )
        for action in CONTRACT_TEMPLATES["mesh"]["allowed_actions"]
    ],
}

TEMPLATE_VERSIONS = {"mesh": 1, "mesh-v2": 2}


def render_contract_template(name, *, goal=None, stamp=None, nonce=None):
    """Render one template to contract fields, substituting {goal} and a
    collision-free {report_path}.

    The path carries a second-resolution stamp PLUS a random nonce - two
    kickoffs in the same second must not share an output contract
    (template creation is intentionally instantaneous).
    """
    template = CONTRACT_TEMPLATES[name]
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    stamp = f"{stamp}-{nonce or secrets.token_hex(3)}"
    values = {
        "goal": str(goal or "(none given)"),
        "report_path": (
            f"docs/evidence/three-agent-ping-mesh{('-v2' if name == 'mesh-v2' else '')}-{stamp}.md"
            if name in {"mesh", "mesh-v2"} else ""
        ),
    }
    rendered = {}
    for key, value in template.items():
        if isinstance(value, list):
            rendered[key] = [str(v).format_map(values) for v in value]
        else:
            rendered[key] = str(value).format_map(values)
    rendered.setdefault(
        "title", str(goal) if goal else f"{name} template task {stamp}")
    return rendered


def contract_template_provenance(name, rendered):
    """Return the bounded creation marker for one already-rendered template.

    The caller captures this before applying contract-file or explicit CLI
    overrides. Consequently an override remains legal but is intentionally
    ineligible for compiled execution because its current fingerprint differs.
    """
    from agent_coop import coopdb

    if name not in TEMPLATE_VERSIONS:
        raise KeyError(name)
    return {
        "name": name,
        "version": TEMPLATE_VERSIONS[name],
        "contract_fingerprint": coopdb.contract_fingerprint(rendered),
    }


__all__ = [
    "CONTRACT_TEMPLATES",
    "MESH_REPORT_SKELETON",
    "MESH_V2_ROUTING_LINE",
    "ONE_TURN_ROUTING_LINE",
    "RECEIPT_SKELETON_LINE",
    "TEMPLATE_VERSIONS",
    "contract_template_provenance",
    "render_contract_template",
]
