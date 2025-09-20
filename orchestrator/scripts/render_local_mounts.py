"""Render docker-compose bind mounts for local rclone directories."""
from __future__ import annotations

import argparse
import os
import sys
from typing import Iterable

from orchestrator.local_dirs import compute_bind_mounts, render_compose_bind_mounts


def _ensure_directories(paths: Iterable[str]) -> None:
    for path in paths:
        os.makedirs(path, exist_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate docker-compose volume entries for configured local "
            "directories. Falls back to the default /backupsLocales mount when "
            "the legacy RCLONE_LOCAL_DIRECTORIES variable is not provided."
        )
    )
    parser.add_argument(
        "--directories",
        "-d",
        default=None,
        help=(
            "Directories string to parse. Defaults to the value from the "
            "RCLONE_LOCAL_DIRECTORIES environment variable, or the default "
            "local backups directory if it is unset."
        ),
    )
    parser.add_argument(
        "--ensure",
        action="store_true",
        help="Create the directories on the host if they do not exist.",
    )
    args = parser.parse_args(argv)

    raw_value = args.directories if args.directories is not None else os.getenv(
        "RCLONE_LOCAL_DIRECTORIES", ""
    )
    mounts = compute_bind_mounts(raw_value)
    if args.ensure and mounts:
        _ensure_directories(source for source, _ in mounts)
    snippet = render_compose_bind_mounts(raw_value)
    if snippet:
        sys.stdout.write(snippet)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
