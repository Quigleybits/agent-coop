# Contributing to Agent Co-op

Issues and patches are welcome. Never attach credentials, board databases, `.coop-runs`
content, machine paths, or private-repository material to an issue or patch.

## Reproduce the release toolchain

The runtime supports CPython 3.10–3.14 and has no third-party runtime dependency. Public CI
runs the complete suite on Windows and Ubuntu at Python 3.10 and 3.14.

The following versions match `.github/workflows/ci.yml` and the build-system pin in
`pyproject.toml`. They are contributor pins for repeatable verification, not new runtime
dependency floors:

| Tool | Version |
|---|---:|
| setuptools | 82.0.1 |
| pytest | 9.0.2 |
| build | 1.3.0 |
| twine | 7.0.0 |

Create an isolated environment. On Windows:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install "setuptools==82.0.1" "pytest==9.0.2" "build==1.3.0" "twine==7.0.0"
.\.venv\Scripts\python -m pip install --no-build-isolation -e .
```

On Linux:

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install 'setuptools==82.0.1' 'pytest==9.0.2' 'build==1.3.0' 'twine==7.0.0'
.venv/bin/python -m pip install --no-build-isolation -e .
```

Use any supported Python for ordinary development. Run the minimum and maximum versions
before changing a compatibility claim.

## Run the offline checks

Use a fresh terminal that is not attached to a Co-op session. Clear any `COOP_*` bindings,
then use the same virtual-environment interpreter for the provenance check, suite and smoke.

On Windows:

```powershell
.\.venv\Scripts\python -c "import os; assert not [k for k in os.environ if k.startswith('COOP_')]"
.\.venv\Scripts\python -c "import agent_coop, pathlib; p=pathlib.Path(agent_coop.__file__).resolve(); root=pathlib.Path.cwd().resolve(); assert root in p.parents, p; print(p)"
.\.venv\Scripts\python -m pytest tests/ -q
.\.venv\Scripts\coop.exe smoke --offline
```

On Linux:

```bash
.venv/bin/python -c "import os; assert not [k for k in os.environ if k.startswith('COOP_')]"
.venv/bin/python -c "import agent_coop, pathlib; p=pathlib.Path(agent_coop.__file__).resolve(); root=pathlib.Path.cwd().resolve(); assert root in p.parents, p; print(p)"
.venv/bin/python -m pytest tests/ -q
.venv/bin/coop smoke --offline
```

The suite and `smoke --offline` make no provider calls and spend no provider quota. The
live Herdr test is separately gated and requires an explicit live-session environment; do
not enable it for an ordinary patch.

For a distribution change, continue with the same environment. On Windows:

```powershell
.\.venv\Scripts\python -m build --no-isolation
.\.venv\Scripts\python -m twine check --strict dist\*
```

On Linux:

```bash
.venv/bin/python -m build --no-isolation
.venv/bin/python -m twine check --strict dist/*
```

Install both wheel and sdist into clean environments outside the checkout before calling a
release artifact proven. Exercise `coop`, `agent-coop`, and `python -m agent_coop` through
their version, help, and `smoke --offline` routes.

## Patch discipline

- Add a regression that fails before a behavior fix and passes afterward.
- Keep Windows and Linux path, process, and environment behavior explicit.
- Preserve the board as the sole coordination authority and a different provider as the
  default reviewer.
- Keep tests offline. A real `coop "goal"` starts provider processes and can spend quota;
  it is never an automatic contribution check.
- Update README, manual, security policy, and changelog claims when user-visible behavior
  changes.
- Do not add reliability percentages, speed claims, or private evidence references to the
  public product.

Report vulnerabilities through [`SECURITY.md`](SECURITY.md), without publishing exploit
details or sensitive evidence first.
