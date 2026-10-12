"""Advisor artifacts for one attempt.

`private_trace.jsonl` holds the advisor's full decisions, including `plan`, `hint` and
`private_diagnosis`, which are derived from privileged material: never publish it.
`summary.json` and `task_material_manifest.json` hold counters, paths, sizes and hashes only.
Every file is readable and writable by its owner only (mode 0600), and a directory created here
is the owner's only (0700). The trace is never written through a symlink or into a FIFO.

Writing artifacts is observability, not rollout work: every failure here is logged and swallowed,
so a full disk or a read-only mount can never fail an attempt.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PRIVATE_TRACE_NAME = "private_trace.jsonl"
SUMMARY_NAME = "summary.json"
TASK_MATERIAL_MANIFEST_NAME = "task_material_manifest.json"
_PRIVATE_MODE = 0o600
_PRIVATE_DIRECTORY_MODE = 0o700
# The trace is appended to, never replaced, so it is opened without following a symlink and without
# waiting on a FIFO; anything but a regular file is refused.
_APPEND_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_APPEND
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


def _dumps(record: Any) -> str:
    return json.dumps(
        record,
        allow_nan=False,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
        sort_keys=True,
    )


class ArtifactWriter:
    """Append-safe JSONL and atomic JSON writes beneath one advisor directory."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    @property
    def private_trace_path(self) -> Path:
        return self.directory / PRIVATE_TRACE_NAME

    @property
    def summary_path(self) -> Path:
        return self.directory / SUMMARY_NAME

    @property
    def task_material_manifest_path(self) -> Path:
        return self.directory / TASK_MATERIAL_MANIFEST_NAME

    def append_trace(self, record: dict[str, Any]) -> bool:
        """Append one private trace record. Returns whether it was written."""

        try:
            line = _dumps(record) + "\n"
            self.directory.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
            descriptor = os.open(self.private_trace_path, _APPEND_FLAGS, _PRIVATE_MODE)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise OSError(f"{self.private_trace_path} is not a regular file")
                # A trace left by an earlier run may have a looser mode; the mode argument above
                # applies only when the file is created.
                os.fchmod(descriptor, _PRIVATE_MODE)
                os.set_blocking(descriptor, True)
            except BaseException:
                os.close(descriptor)
                raise
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                stream.write(line)
        except (OSError, TypeError, ValueError):
            logger.warning("could not append advisor trace record", exc_info=True)
            return False
        return True

    def write_summary(self, summary: dict[str, Any]) -> bool:
        return self._write_json(self.summary_path, summary)

    def write_task_material_manifest(self, manifest: dict[str, Any]) -> bool:
        return self._write_json(self.task_material_manifest_path, manifest)

    def _write_json(self, path: Path, payload: dict[str, Any]) -> bool:
        try:
            self.directory.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
            write_private(path, _dumps(payload).encode("utf-8"))
        except (OSError, TypeError, ValueError):
            logger.warning("could not write advisor artifact %s", path.name, exc_info=True)
            return False
        return True


def write_private(path: Path, content: bytes) -> None:
    """Replace *path* atomically with *content*, readable and writable by its owner only.

    The content goes to a temporary file in the same directory (created 0600 by mkstemp), which
    then replaces *path*, so a reader never sees a partial file and an existing file's looser mode
    is not kept. Raises OSError; the temporary file is removed on failure.
    """

    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
