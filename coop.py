#!/usr/bin/env python3
"""Root alias for the Co-op CLI.

Canonical module: ``agent_coop/cli.py``. This file exists so ``python coop.py``
keeps working from the Co-op checkout — it is in every doc and every harness
skill. The board routes ``python -m agent_coop`` instead, because that resolves
from a workspace that holds no Co-op files.
"""
from agent_coop.cli import main

if __name__ == "__main__":
    main()
