"""Backward-compatible launcher and symbol bridge for the FastAPI application."""

from __future__ import annotations

import os
import sys

import uvicorn

from mbd_api import core as _core

globals().update({name: getattr(_core, name) for name in dir(_core) if not name.startswith("__")})


def main() -> None:
    port = 8000
    if len(sys.argv) >= 2 and sys.argv[1] == "serve":
        if len(sys.argv) >= 3:
            os.environ["TOP_K"] = str(int(sys.argv[2]))
        if len(sys.argv) >= 4:
            port = int(sys.argv[3])
        uvicorn.run("mbd_api.app:app", host="0.0.0.0", port=port, workers=1)
        return
    raise SystemExit("Usage: python rag-chatbot.py serve [top_k] [port]")


if __name__ == "__main__":
    main()
