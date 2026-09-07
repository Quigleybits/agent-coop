"""``python -m agent_coop`` — the cwd-independent CLI entrypoint.

This is what ``next_action.command`` routes. It resolves wherever ``agent_coop``
is importable, which the runner guarantees via PYTHONPATH (coop_start).
"""
from agent_coop.cli import main

if __name__ == "__main__":
    main()
