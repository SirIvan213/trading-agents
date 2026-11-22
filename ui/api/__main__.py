"""python -m ui.api [--port N]: serve the read-only UI on 127.0.0.1 only.

There is deliberately no --host option: the UI shows the trading journal, so it is never
reachable from another machine.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _port(text: str) -> int:
    port = int(text)
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be 1024-65535")
    return port


def main(argv: list[str] | None = None, run: Callable[..., Any] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m ui.api",
                                description="Serve the read-only UI on 127.0.0.1.")
    p.add_argument("--port", type=_port, default=DEFAULT_PORT)
    args = p.parse_args(argv)
    from ui.api.app import create_app

    if run is None:
        import uvicorn

        run = uvicorn.run
    print(f"trading-agents UI (read-only): http://{HOST}:{args.port}/api/health")
    run(create_app(), host=HOST, port=args.port, server_header=False, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
