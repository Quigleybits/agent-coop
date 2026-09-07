"""Security regressions for private runtime artifacts."""

from __future__ import annotations

import importlib
import ctypes
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

import pytest


def _runtime_module():
    try:
        return importlib.import_module("agent_coop.coop_runtime")
    except ImportError:
        pytest.fail("agent_coop.coop_runtime is missing")


def test_explicit_runtime_root_inside_workspace_fails_closed(tmp_path):
    """A runtime-root override must not put secrets back in the repository."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    coop_runtime = _runtime_module()

    with pytest.raises(coop_runtime.RuntimeSecurityError, match="workspace"):
        coop_runtime.resolve_runtime_root(
            workspace,
            env={"COOP_RUNTIME_ROOT": str(workspace / ".private")},
        )


def _runtime_env(tmp_path):
    return {"COOP_RUNTIME_ROOT": str(tmp_path / "private-runtime")}


def _windows_acl(path: Path) -> dict:
    from ctypes import wintypes

    owner_and_dacl = 0x00000001 | 0x00000004
    error_insufficient_buffer = 122
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetFileSecurityW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetFileSecurityW.restype = wintypes.BOOL
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    needed = wintypes.DWORD()
    advapi32.GetFileSecurityW(
        str(path), owner_and_dacl, None, 0, ctypes.byref(needed)
    )
    assert ctypes.get_last_error() == error_insufficient_buffer
    descriptor = ctypes.create_string_buffer(needed.value)
    assert advapi32.GetFileSecurityW(
        str(path),
        owner_and_dacl,
        descriptor,
        needed,
        ctypes.byref(needed),
    )
    rendered = wintypes.LPWSTR()
    assert advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
        descriptor, 1, owner_and_dacl, ctypes.byref(rendered), None
    )
    try:
        sddl = rendered.value
    finally:
        kernel32.LocalFree(rendered)
    owner = re.search(r"O:(.*?)(?:G:|D:)", sddl).group(1)
    aces = re.findall(r"\(([^)]*)\)", sddl.split("D:", 1)[1])
    return {
        "protected": "D:P" in sddl,
        "owner": owner,
        "aces": aces,
        "sddl": sddl,
    }


def _sddl_principal_to_sid(principal: str) -> str:
    """Resolve an SDDL principal to its canonical SID string.

    SDDL renders well-known accounts as two-letter aliases, and which alias
    appears depends on the account the tests run as, so the rendered text
    cannot be compared across machines.
    """
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    sid = ctypes.c_void_p()
    assert advapi32.ConvertStringSidToSidW(principal, ctypes.byref(sid))
    try:
        rendered = wintypes.LPWSTR()
        assert advapi32.ConvertSidToStringSidW(sid, ctypes.byref(rendered))
        try:
            return rendered.value
        finally:
            kernel32.LocalFree(rendered)
    finally:
        kernel32.LocalFree(sid)


def test_private_prompt_file_is_external_restricted_and_fully_cleaned(tmp_path):
    """Prompt bytes must never land in the workspace or leave an empty owner dir."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    coop_runtime = _runtime_module()
    runtime_root = Path(_runtime_env(tmp_path)["COOP_RUNTIME_ROOT"])

    owned = coop_runtime.create_private_file(
        workspace,
        prefix="coop-prompt-",
        suffix=".txt",
        content=b"synthetic secret",
        env=_runtime_env(tmp_path),
    )
    owner_dir = owned.path.parent
    assert owned.path.read_bytes() == b"synthetic secret"
    assert owner_dir.parent == runtime_root.resolve()
    assert workspace not in owned.path.parents
    if os.name == "nt":
        expected_sid = coop_runtime._windows_current_user_sid()
        for target in (runtime_root, owner_dir, owned.path):
            acl = _windows_acl(target)
            assert acl["protected"] is True
            assert len(acl["aces"]) == 1
            fields = acl["aces"][0].split(";")
            assert fields[0] == "A"
            assert "ID" not in fields[1]
            assert fields[2] == "FA"
            # The one grant must name this user. The file owner is not a
            # safe stand-in: a process running as an administrator
            # creates objects owned by the Administrators group, so the
            # owner and the granted principal legitimately differ.
            assert _sddl_principal_to_sid(fields[5]) == expected_sid
    else:
        assert stat.S_IMODE(runtime_root.stat().st_mode) == 0o700
        assert stat.S_IMODE(owner_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(owned.path.stat().st_mode) == 0o600

    owned.cleanup()
    owned.cleanup()
    assert not owner_dir.exists()
    assert not any(workspace.rglob("*"))


def test_stale_sweep_removes_only_dead_owned_directories(tmp_path):
    """A stale sweep must preserve live, unknown, and unmarked directories."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    coop_runtime = _runtime_module()
    env = _runtime_env(tmp_path)
    dead = coop_runtime.create_private_directory(
        workspace, "launch-profile", env=env
    )
    root = dead.path.parent
    arbitrary = root / f"launch-profile-{uuid.uuid4().hex}"
    arbitrary.mkdir()

    removed = coop_runtime.sweep_stale_runtime(
        workspace,
        env=env,
        stale_after_seconds=0,
        now=time.time() + 1,
        process_alive=lambda _pid: False,
    )
    assert removed == (dead.path,)
    assert not dead.path.exists()
    assert arbitrary.is_dir()

    live = coop_runtime.create_private_directory(
        workspace, "worker-state-grok", env=env
    )
    unknown = coop_runtime.create_private_directory(
        workspace, "launch-profile", env=env
    )
    assert coop_runtime.sweep_stale_runtime(
        workspace,
        env=env,
        stale_after_seconds=0,
        now=time.time() + 1,
        process_alive=lambda _pid: True,
    ) == ()
    assert live.path.is_dir()
    assert unknown.path.is_dir()
    assert coop_runtime.sweep_stale_runtime(
        workspace,
        env=env,
        stale_after_seconds=0,
        now=time.time() + 1,
        process_alive=lambda _pid: None,
    ) == ()
    assert live.path.is_dir()
    assert unknown.path.is_dir()
    live.cleanup()
    unknown.cleanup()


def test_interrupted_cleanup_keeps_the_owner_marker_for_recovery(
        tmp_path, monkeypatch):
    """A deletion that fails part-way must leave the directory recognisably owned."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    coop_runtime = _runtime_module()
    env = _runtime_env(tmp_path)
    owned = coop_runtime.create_private_directory(
        workspace, "launch-profile", env=env
    )
    nested = owned.directory("a-state")
    (nested / "state.json").write_text("{}", encoding="utf-8")
    owned.write_text("z-prompt.txt", "prompt bytes")
    marker = owned.path / ".agent-coop-owner.json"
    # Patch Path.unlink, not os.unlink: before 3.11 pathlib dispatches
    # through an accessor that bound os.unlink at import time, so
    # patching the os function never reaches the call and the fault
    # silently does not fire.
    real_unlink = Path.unlink
    fault = {"armed": True}

    def locked_unlink(self, *args, **kwargs):
        if fault["armed"] and self.name == "z-prompt.txt":
            raise PermissionError("simulated locked prompt file")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_unlink)
    with pytest.raises(PermissionError):
        owned.cleanup()

    assert marker.is_file(), "the owner marker must outlive a failed payload deletion"
    assert coop_runtime.is_owned_runtime_directory(
        owned.path, workspace, "launch-profile", env=env
    )
    fault["armed"] = False
    assert coop_runtime.sweep_stale_runtime(
        workspace,
        env=env,
        stale_after_seconds=0,
        now=time.time() + 1,
        process_alive=lambda _pid: False,
    ) == (owned.path,)
    assert not owned.path.exists()


def test_stale_sweep_does_not_follow_symlink_or_reparse_point(tmp_path):
    """A marker-looking link must never make cleanup touch its target."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    coop_runtime = _runtime_module()
    env = _runtime_env(tmp_path)
    initializer = coop_runtime.create_private_directory(
        workspace, "launch-profile", env=env
    )
    root = initializer.path.parent
    initializer.cleanup()
    target = tmp_path / "must-survive"
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    link = root / f"launch-profile-{uuid.uuid4().hex}"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory links unavailable: {exc}")

    assert coop_runtime.sweep_stale_runtime(
        workspace,
        env=env,
        stale_after_seconds=0,
        now=time.time() + 1,
        process_alive=lambda _pid: False,
    ) == ()
    assert link.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_preexisting_nonempty_runtime_root_is_not_claimed_or_repermissioned(
    tmp_path,
):
    """An unrelated nonempty override must remain byte- and permission-intact."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "unrelated"
    root.mkdir()
    sentinel = root / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    if os.name == "nt":
        before = _windows_acl(root)["sddl"]
    else:
        root.chmod(0o755)
        before = stat.S_IMODE(root.stat().st_mode)
    coop_runtime = _runtime_module()

    with pytest.raises(coop_runtime.RuntimeSecurityError, match="owned"):
        coop_runtime.create_private_directory(
            workspace,
            "launch-profile",
            env={"COOP_RUNTIME_ROOT": str(root)},
        )

    after = (
        _windows_acl(root)["sddl"]
        if os.name == "nt"
        else stat.S_IMODE(root.stat().st_mode)
    )
    assert after == before
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_runtime_root_override_rejects_link_before_resolution(tmp_path):
    """An override link must not hide its link/reparse identity via resolve()."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "runtime-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory links unavailable: {exc}")
    coop_runtime = _runtime_module()

    with pytest.raises(coop_runtime.RuntimeSecurityError, match="link|reparse"):
        coop_runtime.resolve_runtime_root(
            workspace,
            env={"COOP_RUNTIME_ROOT": str(link)},
        )


def test_default_runtime_root_rejects_link_before_resolution(tmp_path):
    """A default root link must not redirect private data to its target."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    base = tmp_path / "user-state"
    parent = base / "agent-coop"
    parent.mkdir(parents=True)
    target = tmp_path / "unexpected-target"
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    link = parent / "runtime"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory links unavailable: {exc}")
    environment = (
        {"LOCALAPPDATA": str(base)}
        if os.name == "nt"
        else {"XDG_STATE_HOME": str(base)}
    )
    coop_runtime = _runtime_module()

    with pytest.raises(coop_runtime.RuntimeSecurityError, match="link|reparse"):
        coop_runtime.resolve_runtime_root(workspace, env=environment)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_expanded_xdg_runtime_root_rejects_link_before_resolution(
        tmp_path, monkeypatch):
    """Tilde expansion must happen before checking the default root tree."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    parent = home / "state" / "agent-coop"
    parent.mkdir(parents=True)
    target = tmp_path / "unexpected-target"
    target.mkdir()
    link = parent / "runtime"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory links unavailable: {exc}")
    coop_runtime = _runtime_module()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        coop_runtime,
        "os",
        type("Platform", (), {"name": "posix"})(),
    )

    with pytest.raises(coop_runtime.RuntimeSecurityError, match="link|reparse"):
        coop_runtime.resolve_runtime_root(
            workspace,
            env={"XDG_STATE_HOME": "~/state"},
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows profile defaults")
@pytest.mark.parametrize(
    ("environment", "setting"),
    [
        ({"LOCALAPPDATA": "relative-local-data"}, "LOCALAPPDATA"),
        ({"USERPROFILE": "relative-profile"}, "USERPROFILE"),
    ],
)
def test_windows_default_runtime_base_must_be_absolute(
        tmp_path, environment, setting):
    """A malformed profile variable must not create state under the cwd."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    coop_runtime = _runtime_module()

    with pytest.raises(
        coop_runtime.RuntimeSecurityError,
        match=setting,
    ):
        coop_runtime.resolve_runtime_root(workspace, env=environment)


@pytest.mark.skipif(os.name == "nt", reason="POSIX home default")
def test_posix_home_default_must_be_absolute(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    coop_runtime = _runtime_module()

    with pytest.raises(
        coop_runtime.RuntimeSecurityError,
        match="HOME",
    ):
        coop_runtime.resolve_runtime_root(
            workspace,
            env={"HOME": "relative-home"},
        )


def test_owned_directory_check_requires_an_owned_runtime_root(tmp_path):
    """A child marker cannot make an unrelated root trusted."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "unrelated-root"
    root.mkdir()
    owner_id = uuid.uuid4().hex
    candidate = root / f"worker-state-grok-{owner_id}"
    candidate.mkdir()
    (candidate / ".agent-coop-owner.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "product": "agent-coop",
                "kind": "worker-state-grok",
                "owner_id": owner_id,
                "pid": os.getpid(),
                "created": time.time(),
            }
        ),
        encoding="utf-8",
    )
    coop_runtime = _runtime_module()

    assert not coop_runtime.is_owned_runtime_directory(
        candidate,
        workspace,
        "worker-state-grok",
        env={"COOP_RUNTIME_ROOT": str(root)},
    )
