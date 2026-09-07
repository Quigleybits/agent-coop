"""Owner-private runtime storage outside an Agent Co-op workspace."""

from __future__ import annotations

import os
import ctypes
import dataclasses
import json
import re
import shutil
import stat
import time
import uuid
from collections.abc import Mapping
from pathlib import Path


class RuntimeSecurityError(RuntimeError):
    """Runtime storage cannot satisfy the local ownership boundary."""


def _resolved(path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _overlaps_workspace(path: Path, workspace: Path) -> bool:
    return (
        path == workspace
        or workspace in path.parents
        or path in workspace.parents
    )


def resolve_runtime_root(
    workspace,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Return the per-user runtime root, rejecting workspace overlap."""
    environment = os.environ if env is None else env
    workspace_path = _resolved(workspace)
    override = environment.get("COOP_RUNTIME_ROOT")
    if override is not None:
        if not str(override).strip():
            raise RuntimeSecurityError("COOP_RUNTIME_ROOT is empty")
        override_path = Path(override).expanduser()
        if not override_path.is_absolute():
            raise RuntimeSecurityError("COOP_RUNTIME_ROOT must be absolute")
        candidate_path = override_path
    elif os.name == "nt":
        base = environment.get("LOCALAPPDATA")
        if base:
            base_path = Path(base).expanduser()
            if not base_path.is_absolute():
                raise RuntimeSecurityError("LOCALAPPDATA must be absolute")
        else:
            profile = environment.get("USERPROFILE")
            if profile:
                profile_path = Path(profile).expanduser()
                if not profile_path.is_absolute():
                    raise RuntimeSecurityError("USERPROFILE must be absolute")
            else:
                profile_path = Path.home()
            base_path = profile_path / "AppData" / "Local"
        candidate_path = base_path / "agent-coop" / "runtime"
    else:
        state_home = environment.get("XDG_STATE_HOME")
        if state_home:
            if not Path(state_home).expanduser().is_absolute():
                raise RuntimeSecurityError("XDG_STATE_HOME must be absolute")
            base = Path(state_home).expanduser()
        else:
            home = environment.get("HOME")
            home_path = Path(home).expanduser() if home else Path.home()
            if not home_path.is_absolute():
                raise RuntimeSecurityError("HOME must be absolute")
            base = home_path / ".local" / "state"
        candidate_path = base / "agent-coop" / "runtime"
    if _path_has_link_or_reparse_component(candidate_path):
        raise RuntimeSecurityError(
            "private runtime root contains a link or reparse point"
        )
    candidate = _resolved(candidate_path)
    if _overlaps_workspace(candidate, workspace_path):
        raise RuntimeSecurityError(
            "private runtime root must be outside the workspace"
        )
    return candidate


_OWNER_FILE = ".agent-coop-owner.json"
_ROOT_OWNER_FILE = ".agent-coop-runtime-root.json"
_OWNER_SCHEMA = 1
_PRODUCT = "agent-coop"
_KIND = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_OWNED_NAME = re.compile(
    r"^(?P<kind>[a-z][a-z0-9-]{0,63})-(?P<owner>[0-9a-f]{32})$"
)
_FILE_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}-$")
_FILE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,16}$")
DEFAULT_STALE_AFTER_SECONDS = 24 * 60 * 60


def _is_reparse_or_link(path: Path) -> bool:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(details.st_mode):
        return True
    attributes = getattr(details, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)


def _path_has_link_or_reparse_component(path: Path) -> bool:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if _is_reparse_or_link(current):
            return True
        if not current.exists():
            break
    return False


def _windows_current_user_sid() -> str:
    from ctypes import wintypes

    token_query = 0x0008
    token_user_class = 1
    error_insufficient_buffer = 122
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token, token_user_class, None, 0, ctypes.byref(size)
        )
        if ctypes.get_last_error() != error_insufficient_buffer:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token,
            token_user_class,
            buffer,
            size,
            ctypes.byref(size),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        class _SidAndAttributes(ctypes.Structure):
            _fields_ = [
                ("sid", ctypes.c_void_p),
                ("attributes", wintypes.DWORD),
            ]

        class _TokenUser(ctypes.Structure):
            _fields_ = [("user", _SidAndAttributes)]

        sid = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents.user.sid
        rendered = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(rendered)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return rendered.value
        finally:
            kernel32.LocalFree(rendered)
    finally:
        kernel32.CloseHandle(token)


def _set_windows_private_acl(path: Path, *, directory: bool) -> None:
    from ctypes import wintypes

    sid = _windows_current_user_sid()
    inheritance = "OICI" if directory else ""
    sddl = f"D:P(A;{inheritance};FA;;;{sid})"
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    advapi32.SetFileSecurityW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    advapi32.SetFileSecurityW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        dacl_security_information = 0x00000004
        if not advapi32.SetFileSecurityW(
            str(path), dacl_security_information, descriptor
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.LocalFree(descriptor)


def _secure_permissions(path: Path, *, directory: bool) -> None:
    if os.name == "nt":
        _set_windows_private_acl(path, directory=directory)
    else:
        path.chmod(0o700 if directory else 0o600)


def _root_marker_payload() -> bytes:
    return json.dumps(
        {
            "schema": _OWNER_SCHEMA,
            "product": _PRODUCT,
            "purpose": "private-runtime-root",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _valid_runtime_root(root: Path) -> bool:
    if _is_reparse_or_link(root) or not root.is_dir():
        return False
    marker = root / _ROOT_OWNER_FILE
    if _is_reparse_or_link(marker) or not marker.is_file():
        return False
    try:
        return marker.read_bytes() == _root_marker_payload()
    except OSError:
        return False


def _ensure_runtime_root(workspace, *, env=None) -> Path:
    root = resolve_runtime_root(workspace, env=env)
    if _is_reparse_or_link(root):
        raise RuntimeSecurityError("private runtime root is a link or reparse point")
    if root.exists() and not root.is_dir():
        raise RuntimeSecurityError("private runtime root is not a directory")
    existed = root.exists()
    if existed and not _valid_runtime_root(root):
        try:
            nonempty = next(root.iterdir(), None) is not None
        except OSError as exc:
            raise RuntimeSecurityError(
                "private runtime root ownership is unreadable"
            ) from exc
        if nonempty:
            raise RuntimeSecurityError(
                "private runtime root is not owned by Agent Co-op"
            )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if _is_reparse_or_link(root):
        raise RuntimeSecurityError("private runtime root is a link or reparse point")
    try:
        _secure_permissions(root, directory=True)
        marker = root / _ROOT_OWNER_FILE
        if not marker.exists():
            _write_new_file(marker, _root_marker_payload())
        elif not _valid_runtime_root(root):
            raise RuntimeSecurityError(
                "private runtime root ownership marker is invalid"
            )
        else:
            _secure_permissions(marker, directory=False)
    except BaseException:
        if not existed:
            try:
                root.rmdir()
            except OSError:
                pass
        raise
    return root


def _write_new_file(path: Path, content: bytes) -> Path:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            path.unlink()
        except OSError:
            pass
        raise
    try:
        _secure_permissions(path, directory=False)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return path


def _validate_leaf(name: str) -> str:
    value = str(name)
    if (
        not value
        or value in {".", ".."}
        or Path(value).name != value
        or "/" in value
        or "\\" in value
    ):
        raise RuntimeSecurityError("runtime artifact name must be one path segment")
    return value


def _owner_payload(kind: str, owner_id: str, *, created=None) -> dict:
    return {
        "schema": _OWNER_SCHEMA,
        "product": _PRODUCT,
        "kind": kind,
        "owner_id": owner_id,
        "pid": os.getpid(),
        "created": time.time() if created is None else float(created),
    }


def _read_owner(path: Path) -> dict | None:
    marker = path / _OWNER_FILE
    if _is_reparse_or_link(marker) or not marker.is_file():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    match = _OWNED_NAME.fullmatch(path.name)
    if match is None:
        return None
    if (
        payload.get("schema") != _OWNER_SCHEMA
        or payload.get("product") != _PRODUCT
        or payload.get("kind") != match.group("kind")
        or payload.get("owner_id") != match.group("owner")
        or not isinstance(payload.get("pid"), int)
        or payload["pid"] <= 0
        or not isinstance(payload.get("created"), (int, float))
    ):
        return None
    return payload


def _tree_contains_link_or_reparse(root: Path) -> bool:
    if _is_reparse_or_link(root):
        return True
    try:
        for current, directories, files in os.walk(root, topdown=True):
            base = Path(current)
            for name in (*directories, *files):
                if _is_reparse_or_link(base / name):
                    return True
    except OSError:
        return True
    return False


def _remove_owned_payload(root: Path) -> None:
    """Delete the payload first and the ownership marker last.

    An interrupted deletion (a locked prompt file, a transient OSError) then
    leaves a directory that is still recognisably owned, so a later cleanup
    or the stale sweep can finish it instead of refusing it as unowned.
    """
    for child in sorted(root.iterdir()):
        if child.name == _OWNER_FILE:
            continue
        if _is_reparse_or_link(child):
            raise RuntimeSecurityError("refused linked runtime cleanup content")
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    marker = root / _OWNER_FILE
    if marker.is_file():
        marker.unlink()
    root.rmdir()


def _remove_owned_tree(root: Path, *, runtime_root: Path, owner_id: str) -> None:
    try:
        target = root.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise RuntimeSecurityError("could not resolve runtime cleanup target") from exc
    if not root.exists():
        return
    if not _valid_runtime_root(runtime_root):
        raise RuntimeSecurityError("refused cleanup under an unowned runtime root")
    if target.parent != runtime_root or _is_reparse_or_link(root):
        raise RuntimeSecurityError("refused unsafe runtime cleanup target")
    payload = _read_owner(root)
    if payload is None or payload["owner_id"] != owner_id:
        raise RuntimeSecurityError("refused unowned runtime cleanup target")
    if _tree_contains_link_or_reparse(root):
        raise RuntimeSecurityError("refused linked runtime cleanup content")
    _remove_owned_payload(root)


class OwnedRuntimeDirectory:
    """One marker-owned directory with fail-closed cleanup."""

    def __init__(self, path: Path, runtime_root: Path, owner_id: str):
        self.path = path
        self._runtime_root = runtime_root
        self._owner_id = owner_id
        self._cleaned = False

    def directory(self, name: str) -> Path:
        leaf = _validate_leaf(name)
        path = self.path / leaf
        path.mkdir(mode=0o700)
        _secure_permissions(path, directory=True)
        return path

    def write_bytes(self, name: str, content: bytes) -> Path:
        if not isinstance(content, bytes):
            raise TypeError("runtime file content must be bytes")
        return _write_new_file(self.path / _validate_leaf(name), content)

    def write_text(self, name: str, content: str) -> Path:
        return self.write_bytes(name, str(content).encode("utf-8"))

    def cleanup(self) -> None:
        if self._cleaned:
            return
        _remove_owned_tree(
            self.path,
            runtime_root=self._runtime_root,
            owner_id=self._owner_id,
        )
        self._cleaned = True


@dataclasses.dataclass(frozen=True)
class OwnedRuntimeFile:
    """A private file whose handle owns its containing runtime directory."""

    path: Path
    _directory: OwnedRuntimeDirectory = dataclasses.field(repr=False)

    def cleanup(self) -> None:
        self._directory.cleanup()


def create_private_directory(
    workspace,
    kind,
    *,
    env: Mapping[str, str] | None = None,
) -> OwnedRuntimeDirectory:
    """Create one protected, ownership-marked runtime directory."""
    kind = str(kind)
    if _KIND.fullmatch(kind) is None:
        raise RuntimeSecurityError("invalid runtime artifact kind")
    root = _ensure_runtime_root(workspace, env=env)
    sweep_stale_runtime(workspace, env=env)
    owner_id = uuid.uuid4().hex
    path = root / f"{kind}-{owner_id}"
    path.mkdir(mode=0o700)
    try:
        _secure_permissions(path, directory=True)
        marker = json.dumps(
            _owner_payload(kind, owner_id),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        _write_new_file(path / _OWNER_FILE, marker)
    except BaseException:
        if path.exists() and not _is_reparse_or_link(path):
            try:
                path.rmdir()
            except OSError:
                pass
        raise
    return OwnedRuntimeDirectory(path, root, owner_id)


def create_private_file(
    workspace,
    *,
    prefix,
    suffix,
    content: bytes,
    env: Mapping[str, str] | None = None,
) -> OwnedRuntimeFile:
    """Create a private file in its own independently cleaned directory."""
    prefix = str(prefix)
    suffix = str(suffix)
    if _FILE_PREFIX.fullmatch(prefix) is None:
        raise RuntimeSecurityError("invalid runtime file prefix")
    if _FILE_SUFFIX.fullmatch(suffix) is None:
        raise RuntimeSecurityError("invalid runtime file suffix")
    directory = create_private_directory(workspace, "prompt", env=env)
    try:
        path = directory.write_bytes(f"{prefix}{uuid.uuid4().hex}{suffix}", content)
    except BaseException:
        directory.cleanup()
        raise
    return OwnedRuntimeFile(path=path, _directory=directory)


def is_owned_runtime_directory(
    path,
    workspace,
    kind,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return whether `path` is a marked direct child of the runtime root."""
    kind = str(kind)
    if _KIND.fullmatch(kind) is None:
        return False
    root = resolve_runtime_root(workspace, env=env)
    if not _valid_runtime_root(root):
        return False
    candidate = Path(path)
    if _is_reparse_or_link(candidate):
        return False
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    if resolved.parent != root or not resolved.is_dir():
        return False
    payload = _read_owner(resolved)
    return bool(payload is not None and payload["kind"] == kind)


def _process_alive(pid: int) -> bool | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        from ctypes import wintypes

        query_limited_information = 0x1000
        still_active = 259
        error_invalid_parameter = 87
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(query_limited_information, False, pid)
        if not handle:
            return False if ctypes.get_last_error() == error_invalid_parameter else None
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return None
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError:
        return None
    return True


def sweep_stale_runtime(
    workspace,
    *,
    env: Mapping[str, str] | None = None,
    stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS,
    now=None,
    process_alive=None,
) -> tuple[Path, ...]:
    """Remove only old marked directories whose owner is definitely dead."""
    if stale_after_seconds < 0:
        raise ValueError("stale_after_seconds must be non-negative")
    root = resolve_runtime_root(workspace, env=env)
    if not root.exists():
        return ()
    if not _valid_runtime_root(root):
        raise RuntimeSecurityError("private runtime root is not owned")
    check_process = _process_alive if process_alive is None else process_alive
    current_time = time.time() if now is None else float(now)
    removed = []
    for candidate in tuple(root.iterdir()):
        if _is_reparse_or_link(candidate) or not candidate.is_dir():
            continue
        payload = _read_owner(candidate)
        if payload is None:
            continue
        age = current_time - float(payload["created"])
        if age < stale_after_seconds:
            continue
        try:
            alive = check_process(payload["pid"])
        except Exception:
            alive = None
        if alive is not False:
            continue
        try:
            _remove_owned_tree(
                candidate,
                runtime_root=root,
                owner_id=payload["owner_id"],
            )
        except (OSError, RuntimeSecurityError):
            continue
        removed.append(candidate)
    return tuple(removed)
