"""Nagisa command-line entry point."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


def _healthcheck() -> int:
    config_dir = Path(os.environ.get("CONFIG_DIR", "/config"))
    database = Path(os.environ.get("NAGISA_DB_PATH", str(config_dir / "nagisa.sqlite3")))
    if not database.is_file():
        return 1
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=2)
        try:
            result = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
        if result != ("ok",):
            return 1
        port = int(os.environ.get("PORT", "8787"))
        request = Request(f"http://127.0.0.1:{port}/healthz", method="GET")
        with urlopen(request, timeout=3) as response:
            return 0 if response.status == 200 and response.read(4096) else 1
    except (OSError, sqlite3.Error, URLError, ValueError):
        return 1


def _run() -> int:
    from nagisa.config import Config
    from nagisa.runtime import NagisaRuntime

    runtime = NagisaRuntime(Config.load(os.environ))
    runtime.run_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nagisa")
    parser.add_argument("command", nargs="?", choices=("run", "healthcheck"), default="run")
    arguments = parser.parse_args(argv)
    return _healthcheck() if arguments.command == "healthcheck" else _run()


if __name__ == "__main__":
    sys.exit(main())

