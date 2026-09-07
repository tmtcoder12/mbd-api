"""Local development entrypoint."""

from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the MBD Restaurant RAG API")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve"])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run(
        "mbd_api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=1,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
