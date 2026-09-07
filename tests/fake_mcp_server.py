"""Minimal stdio MCP server used only by the opt-in provider smoke."""

from __future__ import annotations

import json
import os
import pathlib
import sys


def main() -> int:
    marker = pathlib.Path(os.environ["COOP_FAKE_MCP_MARKER"])
    with marker.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("started\n")

    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        request_id = request.get("id")
        if request_id is None:
            continue
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "fake_probe",
                    "version": "1",
                },
            }
        elif method == "tools/list":
            result = {"tools": []}
        else:
            result = {}
        print(
            json.dumps({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            }),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
