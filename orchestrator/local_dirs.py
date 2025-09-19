"""Helpers for working with configured local directories.

This module centralizes the parsing of the ``RCLONE_LOCAL_DIRECTORIES``
configuration value so it can be shared by the web application, scripts and
support tooling such as docker-compose helpers.
"""
from __future__ import annotations

import json
import os
import re
from typing import Iterator

__all__ = [
    "strip_enclosing_quotes",
    "parse_local_directory_config",
    "iter_directory_paths",
    "compute_bind_mounts",
    "render_compose_bind_mounts",
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


def compute_bind_mounts(value: str) -> list[tuple[str, str]]:
    """Return unique ``(source, target)`` tuples for docker bind mounts."""

    mounts: list[tuple[str, str]] = []
    seen: set[str] = set()
    for path in iter_directory_paths(value):
        expanded = os.path.expanduser(path)
        if not expanded:
            continue
        abs_path = os.path.abspath(expanded)
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
