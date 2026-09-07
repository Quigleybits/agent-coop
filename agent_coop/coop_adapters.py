"""Install bundled Agent Co-op skills into workspace harness layers."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import tempfile
from importlib import resources
from typing import Mapping

from agent_coop.coop_errors import AdapterPathInvalid, CoopError


class WorkspaceRefused(CoopError):
    """A directory that must never receive a board (home, above home, a
    filesystem root, or — for silent auto-provision — the temp directory)."""

    type = "workspace_refused"
    default_reason_code = "input_invalid"


ADAPTER_DESTINATIONS = {
    "claude": pathlib.Path(".claude/skills/coop/SKILL.md"),
    "agents": pathlib.Path(".agents/skills/coop/SKILL.md"),
}
ADAPTER_STATE_PATH = pathlib.Path(".coop/harness-adapters.json")
_STATE_VERSION = 1
_RESOURCE_PATHS = {
    "claude": ("adapters", "claude", "SKILL.md"),
    "agents": ("adapters", "agents", "SKILL.md"),
}

# The user-global `/coop` launch skill: one copy per harness under the
# user's home, each a copy (never a link) of a bundled adapter. Claude Code
# reads `~/.claude/skills`; Codex reads `~/.codex/skills`; Grok reads
# `~/.grok/skills`. Values are (home-relative path, bundled template name).
GLOBAL_SKILL_DESTINATIONS = {
    "claude": (pathlib.Path(".claude/skills/coop/SKILL.md"), "claude"),
    "codex": (pathlib.Path(".codex/skills/coop/SKILL.md"), "agents"),
    "grok": (pathlib.Path(".grok/skills/coop/SKILL.md"), "agents"),
}
GLOBAL_SKILLS_STATE_PATH = pathlib.Path(".coop/global-skills.json")
# Test seam: the root that stands in for the home directory. Tests point it
# at a throwaway directory so no run touches the real ~/.claude, ~/.codex,
# ~/.grok or ~/.coop.
GLOBAL_SKILLS_HOME_ENV = "COOP_GLOBAL_SKILLS_HOME"
# Opt-out: any non-empty value skips the install (also `--no-global-skills`).
GLOBAL_SKILLS_OPT_OUT_ENV = "COOP_NO_GLOBAL_SKILLS"
GLOBAL_SKILLS_LABEL = ("/coop skill for Claude Code, Codex and Grok "
                       "(~/.claude/skills, ~/.codex/skills, ~/.grok/skills)")


def git_exclude_entries() -> tuple[str, ...]:
    """Return local paths that workspace bootstrap must hide from git."""
    return (
        ".coop/",
        ".claude/skills/coop/",
        ".agents/skills/coop/",
    )


def bundled_adapters() -> dict[str, str]:
    """Read the standalone adapters shipped inside the installed wheel."""
    package_root = resources.files("agent_coop")
    return {
        name: package_root.joinpath(*parts).read_text(encoding="utf-8")
        for name, parts in _RESOURCE_PATHS.items()
    }


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_state(path: pathlib.Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != _STATE_VERSION:
        return {}
    hashes = payload.get("managed_hashes")
    if not isinstance(hashes, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in hashes.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _workspace_path(
        root: pathlib.Path,
        relative: pathlib.Path) -> pathlib.Path:
    """Resolve one managed path without following it outside the workspace."""
    if relative.is_absolute() or ".." in relative.parts:
        raise AdapterPathInvalid(
            f"managed adapter path is not workspace-relative: {relative}"
        )
    path = root / relative
    if path.is_symlink():
        raise AdapterPathInvalid(
            f"managed adapter path must not be a symlink: {path}"
        )
    try:
        resolved_parent = path.parent.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise AdapterPathInvalid(
            f"managed adapter parent cannot be resolved: {path.parent}"
        ) from error
    if not resolved_parent.is_relative_to(root):
        raise AdapterPathInvalid(
            f"managed adapter path escapes workspace: {path}"
        )
    return path


def _same_path(left, right) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def workspace_refusal(workspace, *, allow_temp_root=True):
    """Why ``workspace`` must not receive a board; ``None`` when it may.

    Board discovery walks up from the working directory, so a board is
    adopted by every directory below it. A board in the home directory, in
    a parent of it, or at a filesystem root would attach to every repository
    on the machine. The OS temp directory itself is refused only when
    ``allow_temp_root`` is false (silent auto-provision); an explicit
    ``coop init --workspace`` may still target it. Directories *below* any of
    these are ordinary workspaces.
    """
    try:
        path = pathlib.Path(workspace).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return f"{workspace} cannot be resolved"
    try:
        home = pathlib.Path.home().resolve(strict=False)
    except (OSError, RuntimeError):
        home = None
    if home is not None:
        if _same_path(path, home):
            return f"{path} is your home directory"
        if any(_same_path(path, parent) for parent in home.parents):
            return f"{path} contains your home directory"
    if path.parent == path:
        return f"{path} is a filesystem root"
    if not allow_temp_root:
        temp_root = pathlib.Path(tempfile.gettempdir()).resolve(strict=False)
        if _same_path(path, temp_root):
            return f"{path} is the temp directory"
    return None


def refuse_unsafe_workspace(
        workspace, *, allow_temp_root=True, hint="") -> pathlib.Path:
    """Raise ``WorkspaceRefused`` before any write when ``workspace`` is a
    directory that must never hold a board; return the resolved path."""
    reason = workspace_refusal(workspace, allow_temp_root=allow_temp_root)
    if reason is not None:
        message = (f"refusing to create a board: {reason}; every repository "
                   f"below it would discover that board")
        if hint:
            message += f". {hint}"
        raise WorkspaceRefused(
            message, evidence={"constraint": "project_directory_required"})
    return pathlib.Path(workspace).expanduser().resolve(strict=False)


def provision_notice(db_path) -> str:
    """One line naming every file ``provision_workspace_board`` creates,
    for printing before the first write."""
    from agent_coop import coopdb
    board = pathlib.Path(db_path).expanduser().resolve(strict=False)
    workspace = coopdb.board_workspace(board)
    try:
        board_relative = board.relative_to(workspace).as_posix()
    except ValueError:
        board_relative = str(board)
    files = [board_relative, ADAPTER_STATE_PATH.as_posix()]
    files.extend(path.as_posix() for path in ADAPTER_DESTINATIONS.values())
    return (f"creating {', '.join(files)} in {workspace} "
            f"(plus a .git/info/exclude entry when it is a git repo)")


def validate_workspace_adapter_paths(workspace) -> pathlib.Path:
    """Validate every managed adapter path before any workspace write."""
    try:
        root = pathlib.Path(workspace).expanduser().resolve()
    except (OSError, RuntimeError) as error:
        raise AdapterPathInvalid(
            f"workspace path cannot be resolved: {workspace}"
        ) from error
    for relative in (*ADAPTER_DESTINATIONS.values(), ADAPTER_STATE_PATH):
        _workspace_path(root, relative)
    return root


def _atomic_write(path: pathlib.Path, text: str) -> None:
    """Atomically replace ``path`` through an exclusively created temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = pathlib.Path(temporary_name)
    descriptor_open = True
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor_open = False
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor_open:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _write_state(path: pathlib.Path, managed_hashes: Mapping[str, str]) -> None:
    payload = {
        "version": _STATE_VERSION,
        "managed_hashes": dict(sorted(managed_hashes.items())),
    }
    _atomic_write(path, json.dumps(payload, indent=2) + "\n")


def install_workspace_adapters(
        workspace,
        *,
        templates: Mapping[str, str] | None = None) -> list[dict[str, str]]:
    """Install or safely update both project-local harness adapters.

    The state file records the hash that Co-op last installed. A later package
    can update an unchanged managed file. Co-op preserves a file whose current
    hash differs from that record because the user owns that edit.
    """
    root = validate_workspace_adapter_paths(workspace)
    supplied = dict(templates) if templates is not None else bundled_adapters()
    if set(supplied) != set(ADAPTER_DESTINATIONS):
        raise ValueError("adapter templates must contain claude and agents")

    state_path = _workspace_path(root, ADAPTER_STATE_PATH)
    managed_hashes = _load_state(state_path)
    results = []
    for name, relative in ADAPTER_DESTINATIONS.items():
        destination = _workspace_path(root, relative)
        status = _install_managed_file(
            destination, supplied[name], relative.as_posix(), managed_hashes)
        results.append({
            "name": name,
            "path": str(destination),
            "status": status,
        })

    _write_state(state_path, managed_hashes)
    return results


def _install_managed_file(
        destination: pathlib.Path,
        desired: str,
        key: str,
        managed_hashes: dict[str, str]) -> str:
    """Install or safely update one hash-managed file; return its status.

    ``installed``: the file was absent. ``current``: it already holds the
    desired text. ``updated``: it held the text Co-op last installed (the
    hash under ``key``), so the new text replaces it. ``preserved``: it holds
    something else, which the user owns. A successful install or update
    records the desired hash under ``key``.
    """
    desired_hash = _digest(desired)
    current = (
        destination.read_text(encoding="utf-8")
        if destination.is_file()
        else None
    )
    current_hash = _digest(current) if current is not None else None
    previous_hash = managed_hashes.get(key)

    if current is None:
        _atomic_write(destination, desired)
        status = "installed"
    elif current_hash == desired_hash:
        status = "current"
    elif previous_hash is not None and current_hash == previous_hash:
        _atomic_write(destination, desired)
        status = "updated"
    else:
        status = "preserved"

    if status != "preserved":
        managed_hashes[key] = desired_hash
    return status


def global_skills_home() -> pathlib.Path:
    """The directory that holds the user-global skill trees: the test seam
    ``COOP_GLOBAL_SKILLS_HOME`` when set, else the home directory."""
    override = os.environ.get(GLOBAL_SKILLS_HOME_ENV)
    if override:
        return pathlib.Path(override).expanduser()
    return pathlib.Path.home()


def global_skills_disabled() -> bool:
    """True when ``COOP_NO_GLOBAL_SKILLS`` carries any non-empty value."""
    return bool(os.environ.get(GLOBAL_SKILLS_OPT_OUT_ENV))


def install_global_skills(
        *,
        home=None,
        templates: Mapping[str, str] | None = None) -> list[dict[str, str]]:
    """Install or safely update the `/coop` launch skill under ``home``.

    Writes a copy of the Claude adapter to ``.claude/skills/coop/SKILL.md``
    and a copy of the shared Agent Skills adapter to
    ``.codex/skills/coop/SKILL.md`` and ``.grok/skills/coop/SKILL.md``, with
    the hash record in ``.coop/global-skills.json``. Same rules as the
    workspace adapters: an unchanged managed file is updated, a local edit
    is preserved. A destination that is a symlink is skipped, never written
    through. Raises ``OSError`` when a write fails; the caller turns that
    into a notice.
    """
    root = pathlib.Path(home).expanduser() if home is not None \
        else global_skills_home()
    supplied = dict(templates) if templates is not None else bundled_adapters()
    if set(supplied) != set(ADAPTER_DESTINATIONS):
        raise ValueError("adapter templates must contain claude and agents")

    state_path = root / GLOBAL_SKILLS_STATE_PATH
    managed_hashes = _load_state(state_path)
    results = []
    for name, (relative, template) in GLOBAL_SKILL_DESTINATIONS.items():
        destination = root / relative
        if destination.is_symlink():
            status = "skipped"
        else:
            status = _install_managed_file(
                destination, supplied[template], relative.as_posix(),
                managed_hashes)
        results.append({
            "name": name,
            "path": str(destination),
            "status": status,
        })

    _write_state(state_path, managed_hashes)
    return results


def global_skills_notice(results) -> str:
    """One line when the install changed something, else the empty string:
    ``installed …`` when any file was new, ``updated …`` when the only
    changes were updates of unchanged managed files."""
    statuses = {result["status"] for result in results}
    if "installed" in statuses:
        return f"installed {GLOBAL_SKILLS_LABEL}"
    if "updated" in statuses:
        return f"updated {GLOBAL_SKILLS_LABEL}"
    return ""


__all__ = [
    "ADAPTER_DESTINATIONS",
    "ADAPTER_STATE_PATH",
    "AdapterPathInvalid",
    "GLOBAL_SKILL_DESTINATIONS",
    "GLOBAL_SKILLS_HOME_ENV",
    "GLOBAL_SKILLS_OPT_OUT_ENV",
    "GLOBAL_SKILLS_STATE_PATH",
    "WorkspaceRefused",
    "bundled_adapters",
    "git_exclude_entries",
    "global_skills_disabled",
    "global_skills_home",
    "global_skills_notice",
    "install_global_skills",
    "install_workspace_adapters",
    "provision_notice",
    "provision_workspace_board",
    "refuse_unsafe_workspace",
    "validate_workspace_adapter_paths",
    "workspace_refusal",
]

def exclude_board_from_git(workspace):
    """Hide Co-op state and local harness adapters from git.

    The function writes to the common git exclude file and never changes the
    tracked ``.gitignore``. A non-git workspace remains valid.
    """
    workspace = pathlib.Path(workspace).expanduser().resolve()
    entries = git_exclude_entries()
    try:
        found = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return "git unavailable — add local Co-op paths to your ignore rules"
    if found.returncode != 0:
        return "not a git repo — nothing to exclude"
    common = pathlib.Path(found.stdout.strip())
    if not common.is_absolute():
        common = workspace / common
    exclude = common / "info" / "exclude"
    try:
        existing = (exclude.read_text(encoding="utf-8")
                    if exclude.is_file() else "")
        present = {line.strip() for line in existing.splitlines()}
        missing = [entry for entry in entries if entry not in present]
        if not missing:
            return f"already excluded local Co-op paths in {exclude}"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        addition = "".join(f"{entry}\n" for entry in missing)
        exclude.write_text(
            f"{existing}{prefix}{addition}", encoding="utf-8")
    except OSError as error:
        return f"could not write {exclude}: {error}"
    return f"excluded local Co-op paths in {exclude}"


def provision_workspace_board(db_path) -> list[str]:
    """Create a board and both harness adapters for the workspace that owns
    ``db_path``; hide the local files from git. Returns the notice lines,
    one per provisioned item, in the order they happened. Used by `coop init`,
    first-use auto-provision, and the dashboard switcher when a listed
    folder has no board yet."""
    from agent_coop import coopdb
    board = pathlib.Path(db_path).expanduser().resolve()
    workspace = coopdb.board_workspace(board)
    # Both checks run before the first write: a refused workspace (home,
    # above home, a root) and an adapter path that escapes it.
    refuse_unsafe_workspace(workspace)
    validate_workspace_adapter_paths(workspace)
    board.parent.mkdir(parents=True, exist_ok=True)
    conn = coopdb.connect(str(board))
    try:
        coopdb.init_db(conn)
    finally:
        conn.close()
    coopdb.record_board(str(board))
    lines = [f"auto-created board {board}"]
    for result in install_workspace_adapters(workspace):
        lines.append(f"adapter {result['status']}: {result['path']}")
    lines.append(exclude_board_from_git(workspace))
    return lines

