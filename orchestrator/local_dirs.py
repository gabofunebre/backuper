"""Helpers for working with configured local directories.

Historically the orchestrator relied on the ``RCLONE_LOCAL_DIRECTORIES``
environment variable to list every bind mounted folder that could be exposed in
the UI. The stack now ships with a fixed mount point (``/backupsLocales``)
backed by the ``./datosPersistentes/backups`` directory on the host, so most
deployments no longer need to provide that environment variable. This module
keeps the parsing helpers for backwards compatibility while also exposing the
new default directory.
"""
from __future__ import annotations

import json
import os
import re
from typing import Iterator

DEFAULT_LOCAL_BACKUPS_ROOT = "/backupsLocales"
"""Default directory inside the container to store local remotes."""

BACKUPS_ROOT_ENV = "BACKUPER_LOCAL_BACKUPS_DIR"
"""Environment variable that overrides the default local backups root."""

LEGACY_DIRECTORIES_ENV = "RCLONE_LOCAL_DIRECTORIES"
"""Legacy environment variable that accepted multiple bind mounts."""

__all__ = [
    "strip_enclosing_quotes",
    "parse_local_directory_config",
    "iter_directory_paths",
    "compute_bind_mounts",
    "render_compose_bind_mounts",
    "load_local_directory_entries",
    "get_local_backups_root",
]


def strip_enclosing_quotes(value: str | None) -> str:
    """Return *value* without matching surrounding quotes."""

    if value is None:
        return ""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"\"", "'"}:
        text = text[1:-1].strip()
    return text


def parse_local_directory_config(value: str) -> list[dict[str, str]]:
    """Parse a delimited list of labelled directories.

    The configuration accepts entries separated by ``;``, ``,`` or newlines.
    Each entry may optionally include a label prefix (``Label|/path``). The
    function returns dictionaries with ``label`` and ``path`` keys.
    """

    entries: list[dict[str, str]] = []
    if not value:
        return entries
    for raw in re.split(r"[;,\n]+", value):
        item = raw.strip()
        if not item:
            continue
        label_part, sep, path_part = item.partition("|")
        if sep:
            cleaned_path = strip_enclosing_quotes(path_part)
            cleaned_label = strip_enclosing_quotes(label_part) or cleaned_path
        else:
            cleaned_path = strip_enclosing_quotes(label_part)
            cleaned_label = cleaned_path
        if not cleaned_path:
            continue
        entries.append({"label": cleaned_label or cleaned_path, "path": cleaned_path})
    return entries


def iter_directory_paths(value: str) -> Iterator[str]:
    """Yield cleaned path strings from a configuration value."""

    for entry in parse_local_directory_config(value):
        raw_path = entry.get("path") if isinstance(entry, dict) else None
        path = strip_enclosing_quotes(str(raw_path or ""))
        if not path:
            continue
        yield path


def get_local_backups_root() -> str:
    """Return the absolute path to the default local backups directory."""

    raw_value = strip_enclosing_quotes(os.getenv(BACKUPS_ROOT_ENV, ""))
    candidate = raw_value or DEFAULT_LOCAL_BACKUPS_ROOT
    expanded = os.path.expanduser(candidate)
    if not expanded:
        expanded = candidate
    return os.path.abspath(expanded)


def _default_local_directory_entries() -> list[dict[str, str]]:
    """Return entries for the default local backups directory."""

    root_path = get_local_backups_root()
    try:
        os.makedirs(root_path, exist_ok=True)
    except OSError:
        return []
    return [{"label": root_path, "path": root_path}]


def load_local_directory_entries(value: str | None = None) -> list[dict[str, str]]:
    """Return configured directories or fall back to the default root."""

    raw_value = value if value is not None else os.getenv(LEGACY_DIRECTORIES_ENV, "")
    entries = parse_local_directory_config(raw_value)
    if not entries:
        entries = _default_local_directory_entries()
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in entries:
        raw_path = entry.get("path") if isinstance(entry, dict) else None
        path = strip_enclosing_quotes(str(raw_path or ""))
        if not path:
            continue
        expanded = os.path.abspath(os.path.expanduser(path))
        if not expanded:
            continue
        normalized_key = os.path.normcase(os.path.normpath(expanded))
        if normalized_key in seen:
            continue
        seen.add(normalized_key)
        raw_label = entry.get("label") if isinstance(entry, dict) else None
        label = strip_enclosing_quotes(str(raw_label or "")) or expanded
        normalized.append({"label": label, "path": expanded})
    return normalized


def compute_bind_mounts(value: str) -> list[tuple[str, str]]:
    """Return unique ``(source, target)`` tuples for docker bind mounts."""

    mounts: list[tuple[str, str]] = []
    seen: set[str] = set()
    directories = load_local_directory_entries(value)
    for entry in directories:
        path = strip_enclosing_quotes(entry.get("path") if isinstance(entry, dict) else "")
        if not path:
            continue
        abs_path = os.path.abspath(os.path.expanduser(path))
        if not abs_path:
            continue
        normalized = os.path.normcase(os.path.normpath(abs_path))
        if normalized in seen:
            continue
        seen.add(normalized)
        mounts.append((abs_path, abs_path))
    return mounts


def render_compose_bind_mounts(value: str) -> str:
    """Render docker-compose volume entries for the configured directories."""

    mounts = compute_bind_mounts(value)
    if not mounts:
        return ""
    rendered_entries: list[str] = []
    for source, target in mounts:
        quoted_source = json.dumps(source)
        quoted_target = json.dumps(target)
        rendered_entries.append(
            "- type: bind\n"
            f"        source: {quoted_source}\n"
            f"        target: {quoted_target}"
        )
    return "\n      ".join(rendered_entries)
