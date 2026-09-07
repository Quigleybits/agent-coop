"""Suite-wide sandbox for the user-global `/coop` launch skill.

Dashboard open and `coop init` install the launch skill under the user's
home (`~/.claude/skills`, `~/.codex/skills`, `~/.grok/skills`, state in
`~/.coop/global-skills.json`). Every test that opens a dashboard or runs
`init` would otherwise write into the developer's real home, so the seam
`COOP_GLOBAL_SKILLS_HOME` points at a throwaway directory for the whole
session. In-process tests read it from `os.environ`; subprocess tests that
copy `os.environ` inherit it; tests that build a cleaned environment set it
themselves (see `test_auto_provision.py`, `test_workspace_init.py`).
"""
import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _global_skills_sandbox(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("global-skills-home")
    previous = os.environ.get("COOP_GLOBAL_SKILLS_HOME")
    os.environ["COOP_GLOBAL_SKILLS_HOME"] = str(sandbox)
    try:
        yield sandbox
    finally:
        if previous is None:
            os.environ.pop("COOP_GLOBAL_SKILLS_HOME", None)
        else:
            os.environ["COOP_GLOBAL_SKILLS_HOME"] = previous


@pytest.fixture(scope="session", autouse=True)
def _private_runtime_sandbox(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("private-runtime-home")
    values = {
        "COOP_RUNTIME_ROOT": str(sandbox / "runtime"),
        "LOCALAPPDATA": str(sandbox / "local-app-data"),
        "APPDATA": str(sandbox / "app-data"),
        "XDG_STATE_HOME": str(sandbox / "xdg-state"),
        "XDG_CONFIG_HOME": str(sandbox / "xdg-config"),
        "XDG_DATA_HOME": str(sandbox / "xdg-data"),
        "XDG_CACHE_HOME": str(sandbox / "xdg-cache"),
        "XDG_RUNTIME_DIR": str(sandbox / "xdg-runtime"),
        "HOME": str(sandbox / "home"),
        "USERPROFILE": str(sandbox / "profile"),
        "CODEX_HOME": str(sandbox / "codex-home"),
        "GROK_HOME": str(sandbox / "grok-home"),
        "CLAUDE_CONFIG_DIR": str(sandbox / "claude-config"),
        "TEMP": str(sandbox / "temp"),
        "TMP": str(sandbox / "temp"),
    }
    for path in values.values():
        if os.path.isabs(path):
            os.makedirs(path, exist_ok=True)
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield sandbox
    finally:
        for name, prior in previous.items():
            if prior is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = prior
