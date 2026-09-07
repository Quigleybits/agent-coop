"""Read-only inbox projection: a disposable Markdown view over SQLite.

Rendered exclusively from peek reads and direct queries — the projection
never consumes the delivery cursor, never mutates state, and is never
parsed for recovery. The supervisor calls
refresh_inbox() each maintenance tick; failure is a returned warning
string for its log, never an exception into the maintenance loop.
"""
import json
import os
import pathlib
import tempfile

from agent_coop import coopdb


def render_inbox(conn, agent):
    """Pure render of one agent's compact view: unread deliveries (peek),
    owned/addressed work, open questions, migrated review requests (only
    when rows exist — never inventing review state), currently-stale
    claims, and the packet for each piece of actively claimed work."""
    lines = [f"# Inbox — {agent}", ""]
    unread = coopdb.read_inbox(conn, agent, peek=True)
    lines.append(f"## Unread ({len(unread)})")
    if not unread:
        lines.append("_nothing waiting_")
    for e in unread:
        item = f" (item {e['item_id']})" if e["item_id"] else ""
        lines.append(f"- [{e['category']}]{item} {e['payload_json']}")

    work = conn.execute(
        "SELECT * FROM items WHERE (owner_agent_id=? OR "
        "next_actor_agent_id=?) AND status!='done' ORDER BY id",
        (agent, agent)).fetchall()
    lines += ["", f"## Work ({len(work)})"]
    if not work:
        lines.append("_none_")
    for r in work:
        role = "owner" if r["owner_agent_id"] == agent else "next actor"
        lines.append(f"- #{r['id']} [{r['status']}] {r['title']} ({role})")
        receipt = conn.execute(
            "SELECT * FROM receipts WHERE item_id=? AND superseded_at IS "
            "NULL ORDER BY receipt_id DESC LIMIT 1", (r["id"],)).fetchone()
        if receipt is not None:
            marker = coopdb.receipt_marker(receipt)
            flag = f" [{marker}]" if marker else ""
            refs = len(json.loads(receipt["proof_references_json"]))
            lines.append(
                f"  receipt {receipt['receipt_id']} · "
                f"{receipt['sha256'][:12]} · refs {refs}{flag}")
        decision = conn.execute(
            "SELECT * FROM decisions WHERE item_id=? ORDER BY id DESC "
            "LIMIT 1", (r["id"],)).fetchone()
        if decision is not None:
            by = decision["decided_by_agent"] or decision["decided_by"]
            lines.append(
                f"  decision {decision['id']} by {by}: {decision['text']}")

    questions = conn.execute(
        "SELECT * FROM questions WHERE assigned_to_agent=? AND "
        "status='open' ORDER BY question_id", (agent,)).fetchall()
    if questions:
        lines += ["", f"## Questions for you ({len(questions)})"]
        for q in questions:
            lines.append(
                f"- q{q['question_id']} (item {q['item_id']}): "
                f"{q['exact_question']}")

    reviews = conn.execute(
        "SELECT * FROM reviews WHERE status IN ('requested','changes') "
        "AND (reviewer_agent_id=? OR reviewer=?) ORDER BY id",
        (agent, agent)).fetchall()
    if reviews:
        lines += ["", f"## Review requests ({len(reviews)})"]
        for r in reviews:
            designated = r["reviewer"] or "any"
            claimant = r["reviewer_agent_id"] or "-"
            # The claims lane is the authority for the current holder;
            # `last-claimant` stays informational.
            active = conn.execute(
                "SELECT claimed_by_agent FROM claims WHERE lane_key=? AND "
                "status='active'", (f"review:{r['id']}",)).fetchone()
            holder = active["claimed_by_agent"] if active else "-"
            lines.append(
                f"- review {r['id']} (item {r['item_id']}) [{r['status']}] "
                f"designated={designated} holder={holder} "
                f"last-claimant={claimant}")

    # Pending handoffs addressed to this agent — the
    # transfer waits on an explicit accept/decline, never a timeout.
    handoffs = conn.execute(
        "SELECT handoff_id, item_id, from_agent, summary FROM handoffs "
        "WHERE status='pending' AND to_agent=? ORDER BY handoff_id",
        (agent,)).fetchall()
    if handoffs:
        lines += ["", f"## Pending handoffs ({len(handoffs)})"]
        for h in handoffs:
            lines.append(
                f"- handoff {h['handoff_id']} (item {h['item_id']}) "
                f"from {h['from_agent']}: {h['summary']}")

    stale = conn.execute(
        "SELECT * FROM claims c WHERE claimed_by_agent=? AND "
        "status='stale' AND claim_id=(SELECT MAX(claim_id) FROM claims "
        "WHERE lane_key=c.lane_key) ORDER BY claim_id",
        (agent,)).fetchall()
    if stale:
        lines += ["", f"## Stale claims ({len(stale)})"]
        for c in stale:
            lines.append(
                f"- claim {c['claim_id']} lane {c['lane_key']} "
                f"(lease lapsed {c['lease_expires_at']})")

    active = conn.execute(
        "SELECT DISTINCT item_id FROM claims WHERE claimed_by_agent=? AND "
        "status='active' ORDER BY item_id", (agent,)).fetchall()
    for row in active:
        packet = coopdb.item_show(conn, row["item_id"], packet=True)
        lines += ["", f"## Packet — item {row['item_id']}", "```json",
                  json.dumps(packet, indent=2, sort_keys=True), "```"]
    return "\n".join(lines) + "\n"


def contained(root, target):
    """True when the resolved target sits at or under the resolved root.
    Symlink-resistant (realpath both sides) and prefix-trap-safe: 'workx'
    is not under 'work'. The single containment definition — the
    launch preflight and the runtime recheck both call this."""
    root = os.path.normcase(os.path.realpath(root))
    target = os.path.normcase(os.path.realpath(target))
    return target == root or target.startswith(root + os.sep)


def write_inbox(path, content):
    """Atomic publication: sibling temporary file, flush + fsync,
    os.replace. No reader ever observes a partial file; a failed write
    leaves the previous file untouched and the temp cleaned up."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def refresh_inbox(conn, agent, out_dir="inbox", contain_within=None):
    """Render and publish inbox/<agent-id>.md. Returns None on success or
    a warning string for the supervisor to log — never raises into the
    maintenance loop. The agent name is validated at this sink because it
    becomes a filename component (an unvalidated name is an arbitrary
    file write). contain_within re-asserts containment on every refresh: a violation
    (a directory swapped or re-symlinked mid-run) is reported as the
    standard warning and the refresh skipped — the never-raises contract
    holds."""
    if not coopdb.valid_agent_name(agent):
        return (f"projection refused: invalid agent name {agent!r} "
                "(letters/digits/._- only, no path separators)")
    if contain_within is not None and not contained(contain_within, out_dir):
        return (f"projection refused: out-dir {out_dir!r} escapes the "
                f"launch working directory {contain_within!r}; "
                "refresh skipped")
    try:
        content = render_inbox(conn, agent)
        write_inbox(pathlib.Path(out_dir) / f"{agent}.md", content)
        return None
    except Exception as exc:
        return f"projection failed for {agent!r}: {exc}"
