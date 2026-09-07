"""Agent Co-op runtime package.

Board-routed commands pin the interpreter that loaded ``agent_coop``. The
installed ``coop`` script is the human-facing entrypoint, and the root
``coop.py`` file is a checkout alias for source-tree operators. Autonomous
provider prompts are self-contained, so repository policy files are not runtime
dependencies. Everything the entrypoints import lives here.
"""
