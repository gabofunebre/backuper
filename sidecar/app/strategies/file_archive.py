"""Strategy that bundles filesystem paths into a tar or zip archive."""

from __future__ import annotations

import glob
import os
import tarfile
import zipfile
from pathlib import Path
from typing import Any

from ..exceptions import ConfigError, StrategyExecutionError
from .base import FileBasedStrategy


def _ensure_paths_list(value: Any, *, field: str) -> list[str]:
    if not value:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{field} must be a list of strings")
    return [item for item in value if item]


class FileArchiveStrategy(FileBasedStrategy):
    """Archive files/directories matched by glob patterns."""

    def __init__(self, *, artifact_config, paths, options: dict[str, Any]) -> None:
        super().__init__(artifact_config=artifact_config, paths=paths)
        pattern_list = _ensure_paths_list(options.get("paths"), field="strategy.config.paths")
        if not pattern_list:
            raise ConfigError("strategy.config.paths must contain at least one entry")
        self._patterns = pattern_list
        base_dir_value = options.get("base_dir")
        self._base_dir = Path(base_dir_value).expanduser() if base_dir_value else paths.workdir
        self._follow_symlinks = bool(options.get("follow_symlinks", False))
        format_value = (options.get("format") or "tar").lower()
        if format_value not in {"tar", "zip"}:
            raise ConfigError("strategy.config.format must be either 'tar' or 'zip'")
        self._format = format_value
        compression_value = options.get("compression")
        self._compression = str(compression_value).lower() if compression_value else None

    def _resolve_pattern(self, pattern: str) -> list[Path]:
        base = self._base_dir
        target_pattern = pattern
        if not Path(pattern).is_absolute():
            target_pattern = str((base / pattern).expanduser())
        matches = [Path(match) for match in sorted(glob.glob(target_pattern, recursive=True))]
        if not matches and Path(target_pattern).exists():
            matches = [Path(target_pattern)]
        return matches

    def _collect_paths(self) -> list[Path]:
        collected: list[Path] = []
        seen: set[Path] = set()
        for pattern in self._patterns:
            for match in self._resolve_pattern(pattern):
                candidate = Path(match)
                if candidate not in seen:
                    collected.append(candidate)
                    seen.add(candidate)
        return collected

    def _make_arcname(self, path: Path) -> str:
        try:
            relative = path.relative_to(self._base_dir)
            arcname = relative.as_posix()
            return arcname if arcname else path.name
        except ValueError:
            return path.name

    def _tar_mode(self) -> str:
        if self._compression is None:
            return "w"
        mapping = {"gz": "w:gz", "bz2": "w:bz2", "xz": "w:xz"}
        try:
            return mapping[self._compression]
        except KeyError as exc:
            raise ConfigError("Unsupported compression for tar archives. Use gz, bz2 or xz.") from exc

    def _add_directory_to_zip(self, zipf: zipfile.ZipFile, directory: Path, added: set[str]) -> None:
        arcname = self._make_arcname(directory).rstrip("/") + "/"
        if arcname in added:
            return
        info = zipfile.ZipInfo(arcname)
        info.external_attr = 0o40555 << 16
        zipf.writestr(info, "")
        added.add(arcname)

    def _create_tar_archive(self, paths: list[Path]) -> None:
        mode = self._tar_mode()
        with tarfile.open(self.paths.temp_dump, mode) as tar:
            added: set[str] = set()
            for path in paths:
                arcname = self._make_arcname(path)
                if arcname in added:
                    continue
                tar.add(
                    str(path),
                    arcname=arcname,
                    recursive=True,
                    dereference=self._follow_symlinks,
                )
                added.add(arcname)

    def _create_zip_archive(self, paths: list[Path]) -> None:
        compression = zipfile.ZIP_DEFLATED if self._compression not in {None, "store", "stored"} else zipfile.ZIP_STORED
        with zipfile.ZipFile(self.paths.temp_dump, mode="w", compression=compression, allowZip64=True) as zipf:
            added: set[str] = set()
            for path in paths:
                if path.is_dir():
                    if path.is_symlink() and not self._follow_symlinks:
                        continue
                    self._add_directory_to_zip(zipf, path, added)
                    for subpath in sorted(path.rglob("*")):
                        if subpath.is_dir():
                            if subpath.is_symlink() and not self._follow_symlinks:
                                continue
                            self._add_directory_to_zip(zipf, subpath, added)
                        else:
                            if subpath.is_symlink() and not self._follow_symlinks:
                                continue
                            arcname = self._make_arcname(subpath)
                            if arcname in added:
                                continue
                            zipf.write(subpath, arcname)
                            added.add(arcname)
                else:
                    if path.is_symlink() and not self._follow_symlinks:
                        continue
                    arcname = self._make_arcname(path)
                    if arcname in added:
                        continue
                    zipf.write(path, arcname)
                    added.add(arcname)

    def prepare(self, drive_folder_id=None):  # type: ignore[override]
        self._ensure_workspace()
        matched_paths = self._collect_paths()
        if not matched_paths:
            raise StrategyExecutionError("File archive strategy did not match any files")
        if self.paths.temp_dump.exists():
            self.paths.temp_dump.unlink()
        if self._format == "tar":
            self._create_tar_archive(matched_paths)
        else:
            self._create_zip_archive(matched_paths)
        os.chmod(self.paths.temp_dump, 0o444)
        artifact_path = self._move_to_artifact(self.paths.temp_dump)
        checksum, size = self._compute_checksum(artifact_path)
        return self._register_metadata(artifact_path, size=size, checksum=checksum)

