"""Public and privileged task material: x and z.

`TaskMaterial` holds the instruction the solver sees and the privileged files only the advisor may
read, typically the reference solution, the hidden verifier tests and private task notes. It is
loaded in the advisor's own process; nothing here writes to a task directory or reaches the
solver's sandbox. Symlinks are never followed, including a symlinked directory partway along a
nested layout path such as "hidden/tests", and only regular files are read: a FIFO or device is
never read. A path is checked with lstat before it is opened, and one swapped in after that check
is rejected on its open descriptor without blocking. A privileged file or directory that cannot be
read is recorded as skipped, never dropped.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

DEFAULT_MAX_FILE_BYTES = 1_000_000
DEFAULT_MAX_TOTAL_BYTES = 4_000_000
INSTRUCTION_PLACEHOLDER = "(public task instruction unavailable)"
_MAX_INSTRUCTION_BYTES = 100_000
_HASH_CHUNK_BYTES = 65_536


def has_symlink_component(root: Path, relative: str | PurePosixPath) -> bool:
    """Whether any component of root/relative is a symlink.

    Each prefix is checked, from the first part to the full path, so "hidden/sub" counts as a
    symlink when "hidden" is one. *root* itself is not checked. A missing component is not a
    symlink; any other OSError (a directory that cannot be searched) propagates, the same on every
    Python version.
    """

    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except (FileNotFoundError, NotADirectoryError):
            return False
    return False


# O_NOFOLLOW refuses a symlink in the final component; O_NONBLOCK keeps opening a FIFO from waiting
# for a writer. Both are 0 where the platform lacks them.
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _open_regular(path: Path) -> BinaryIO | None:
    """*path* opened for binary reading when it is a regular file; None for anything else.

    The type is checked on the open descriptor, so a FIFO or device swapped in after an earlier
    lstat check is never read and opening it does not block. O_NOFOLLOW covers only the final
    component; callers check the directories above it with `has_symlink_component`. Other errors
    (a missing or unreadable file) propagate.
    """

    try:
        descriptor = os.open(path, _OPEN_FLAGS)
    except OSError as error:
        if error.errno == errno.ELOOP:  # the final component is a symlink
            return None
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            return None
        os.set_blocking(descriptor, True)  # a regular file read must never return None on EAGAIN
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


@dataclass(frozen=True, slots=True)
class TaskLayout:
    """Where a task directory keeps its public instruction and its privileged material.

    The defaults match Harbor and Terminal-Bench tasks. Privileged roots are read in order; the
    task README comes first because it explains the intended solution and how it is verified.
    """

    instruction_file: str = "instruction.md"
    privileged_roots: tuple[str, ...] = ("README.md", "solution", "tests")
    # `cheat` is an anti-cheat tripwire in some benchmarks, not review material.
    excluded_dirs: frozenset[str] = frozenset({"cheat", "__pycache__", ".git", ".pytest_cache"})
    excluded_suffixes: frozenset[str] = frozenset({".pyc", ".pyo", ".so", ".o", ".a"})

    def __post_init__(self) -> None:
        """Validate that layout paths are relative and stay inside the task directory."""
        for entry in [self.instruction_file, *self.privileged_roots]:
            path = PurePosixPath(entry)
            if not entry or not path.parts or path.is_absolute() or ".." in path.parts:
                raise ValueError(
                    f"layout paths must be relative and stay inside the task directory: {entry!r}"
                )

    def excludes(self, relative: Path) -> bool:
        if any(part in self.excluded_dirs for part in relative.parts):
            return True
        return relative.suffix.lower() in self.excluded_suffixes

    def _entries(self, root: Path) -> Iterator[tuple[Path, OSError | None]]:
        """Privileged paths beneath *root* in deterministic order, each with the OSError that
        kept it from being examined, if any.

        Symlinks are yielded, never followed. A root with a symlink anywhere along its path (the
        root itself or an intermediate directory of a nested root) is yielded as itself and never
        descended. A directory that cannot be listed, a root included, is yielded as itself.
        """

        for root_name in self.privileged_roots:
            base = root / root_name
            found: dict[Path, OSError | None] = {}
            try:
                if has_symlink_component(root, root_name) or not stat.S_ISDIR(base.lstat().st_mode):
                    found[base] = None
                else:
                    _walk(base, found)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as error:  # the root itself cannot be examined
                found = {base: error}
            for path in sorted(found, key=lambda path: path.relative_to(root).as_posix()):
                if not self.excludes(path.relative_to(root)):
                    yield path, found[path]


def _walk(base: Path, found: dict[Path, OSError | None]) -> None:
    """Record every path beneath the directory *base* in *found*, never following a symlink.

    Files, symlinks and special files map to None. A directory that cannot be listed, or a name
    whose type cannot be checked, maps to the OSError that stopped it, so it is recorded, not
    dropped.
    """

    def unlistable(error: OSError) -> None:
        found.setdefault(Path(error.filename) if error.filename else base, error)

    for directory, dirnames, filenames in os.walk(base, onerror=unlistable, followlinks=False):
        found.update(dict.fromkeys((Path(directory) / name for name in filenames), None))
        for name in dirnames:
            path = Path(directory) / name
            try:
                if stat.S_ISLNK(path.lstat().st_mode):  # recorded and skipped, never descended
                    found[path] = None
            except OSError as error:
                found[path] = error


@dataclass(frozen=True, slots=True)
class PrivilegedFile:
    """One privileged file, with bounded text content when it is decodable."""

    relative_path: str
    size_bytes: int
    sha256: str
    text: str | None
    truncated: bool
    skipped_reason: str | None = None

    def manifest_entry(self) -> dict[str, Any]:
        """Metadata only. File contents never enter the manifest."""

        return {
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "text": self.text is not None,
            "truncated": self.truncated,
            "skipped_reason": self.skipped_reason,
        }


def _decode(raw: bytes, *, truncated: bool) -> str | None:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        # A cut inside a multi-byte character is an artifact of truncation, not a binary file.
        if truncated and error.reason == "unexpected end of data" and error.start >= len(raw) - 3:
            try:
                return raw[: error.start].decode("utf-8")
            except UnicodeDecodeError:
                return None
        return None


class _Budget:
    """Applies the per-file and total content limits in load order."""

    def __init__(self, max_file_bytes: int, max_total_bytes: int) -> None:
        if max_file_bytes < 1 or max_total_bytes < 1:
            raise ValueError("privileged size limits must be positive")
        if max_total_bytes < max_file_bytes:
            raise ValueError("max_total_bytes must not be below max_file_bytes")
        self.max_file_bytes = max_file_bytes
        self.remaining = max_total_bytes
        self.used = 0
        self.files: list[PrivilegedFile] = []
        self.truncated: list[str] = []
        self.omitted: list[str] = []

    def allowance(self) -> int:
        """Bytes the next file may contribute; 0 means the total budget is spent."""

        return min(self.max_file_bytes, self.remaining)

    def skip(self, relative: str, reason: str, *, size: int = 0, sha256: str = "") -> None:
        self.files.append(PrivilegedFile(relative, size, sha256, None, False, reason))

    def admit(self, relative: str, raw: bytes, *, size: int, sha256: str) -> None:
        truncated = size > len(raw)
        text = _decode(raw, truncated=truncated)
        if text is None:
            # Not text: record identity only; nothing is charged against the content budget.
            self.skip(relative, "not valid utf-8", size=size, sha256=sha256)
            return
        self.remaining -= len(raw)
        self.used += len(raw)
        if truncated:
            self.truncated.append(relative)
        self.files.append(PrivilegedFile(relative, size, sha256, text, truncated))


def _read_prefix_and_hash(stream: BinaryIO, allowance: int) -> tuple[bytes, int, str]:
    """(the first *allowance* bytes, the size, the SHA-256 of the whole stream)."""

    digest = hashlib.sha256()
    prefix = bytearray()
    size = 0
    while chunk := stream.read(_HASH_CHUNK_BYTES):
        digest.update(chunk)
        size += len(chunk)
        if len(prefix) < allowance:
            prefix += chunk[: allowance - len(prefix)]
    return bytes(prefix), size, digest.hexdigest()


def _read_instruction(root: Path, relative: str) -> str:
    """The instruction, or the placeholder when it is missing, unreadable, not a regular file or
    behind a symlink."""

    try:
        if has_symlink_component(root, relative):
            return INSTRUCTION_PLACEHOLDER
        # Checked before opening: opening a FIFO or device would still release a waiting writer or
        # run a driver's open routine.
        if not stat.S_ISREG(os.lstat(root / relative).st_mode):
            return INSTRUCTION_PLACEHOLDER
        stream = _open_regular(root / relative)
        if stream is None:
            return INSTRUCTION_PLACEHOLDER
        with stream:
            raw = stream.read(_MAX_INSTRUCTION_BYTES)
    except OSError:
        return INSTRUCTION_PLACEHOLDER
    return raw.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class TaskMaterial:
    """The public instruction x and the privileged material z for one task."""

    instruction: str
    files: tuple[PrivilegedFile, ...] = ()
    name: str = ""
    source: str = ""
    truncated_paths: tuple[str, ...] = ()
    omitted_paths: tuple[str, ...] = ()
    total_content_bytes: int = 0
    layout: TaskLayout | None = None

    @classmethod
    def from_texts(
        cls,
        instruction: str,
        privileged: Mapping[str, str],
        *,
        name: str = "",
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    ) -> TaskMaterial:
        """Material for tasks that are not laid out as a directory; keys are display paths."""

        budget = _Budget(max_file_bytes, max_total_bytes)
        for relative, text in privileged.items():
            raw = text.encode("utf-8")
            allowance = budget.allowance()
            if allowance <= 0:
                budget.omitted.append(relative)
                continue
            budget.admit(
                relative,
                raw[:allowance],
                size=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        return cls._from_budget(instruction, budget, name=name)

    @classmethod
    def from_task_dir(
        cls,
        path: str | Path,
        layout: TaskLayout | None = None,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    ) -> TaskMaterial:
        """Load a task directory once. Raises FileNotFoundError when it does not exist.

        Nothing is read through a symlink: a privileged path with a symlink anywhere along it is
        recorded as skipped "symlink", and an instruction file behind one gives the placeholder.
        A privileged file or directory that cannot be read is recorded as skipped
        "unreadable: <ExceptionType>"; loading continues without it.
        """

        root = Path(path)
        if not root.is_dir():
            raise FileNotFoundError(f"task directory not found: {root}")
        layout = layout or TaskLayout()
        budget = _Budget(max_file_bytes, max_total_bytes)
        for file_path, walk_error in layout._entries(root):
            relative = file_path.relative_to(root).as_posix()
            if walk_error is not None:
                budget.skip(relative, f"unreadable: {type(walk_error).__name__}")
                continue
            try:
                if has_symlink_component(root, relative):
                    budget.skip(relative, "symlink")
                    continue
                info = file_path.lstat()
            except OSError as error:
                budget.skip(relative, f"unreadable: {type(error).__name__}")
                continue
            if not stat.S_ISREG(info.st_mode):
                budget.skip(relative, "not a regular file")
                continue
            allowance = budget.allowance()
            if allowance <= 0:
                budget.omitted.append(relative)
                continue
            try:
                stream = _open_regular(file_path)
                if stream is None:  # replaced by a FIFO, device or symlink since the check
                    budget.skip(relative, "not a regular file")
                    continue
                with stream:
                    raw, size, checksum = _read_prefix_and_hash(stream, allowance)
            except OSError as error:
                budget.skip(relative, f"unreadable: {type(error).__name__}")
                continue
            budget.admit(relative, raw, size=size, sha256=checksum)
        return cls._from_budget(
            _read_instruction(root, layout.instruction_file),
            budget,
            name=Path(os.path.abspath(root)).name,
            source=str(root),
            layout=layout,
        )

    @classmethod
    def _from_budget(
        cls,
        instruction: str,
        budget: _Budget,
        *,
        name: str,
        source: str = "",
        layout: TaskLayout | None = None,
    ) -> TaskMaterial:
        return cls(
            instruction=instruction,
            files=tuple(budget.files),
            name=name,
            source=source,
            truncated_paths=tuple(budget.truncated),
            omitted_paths=tuple(budget.omitted),
            total_content_bytes=budget.used,
            layout=layout,
        )

    def privileged_text(self) -> str:
        """All decodable privileged text, for leak screening."""

        return "\n".join(entry.text for entry in self.files if entry.text)

    def has_privileged_text(self) -> bool:
        """Whether any privileged text was loaded; whitespace alone does not count."""

        return bool(self.privileged_text().strip())

    def manifest(self) -> dict[str, Any]:
        """Paths, sizes and hashes only: never contents, never the task's absolute path."""

        layout = self.layout
        return {
            "task": self.name,
            "privileged_roots": list(layout.privileged_roots) if layout else None,
            "excluded_directory_names": sorted(layout.excluded_dirs) if layout else None,
            "file_count": len(self.files),
            "total_content_bytes": self.total_content_bytes,
            "truncated_paths": list(self.truncated_paths),
            "omitted_paths": list(self.omitted_paths),
            "files": [entry.manifest_entry() for entry in self.files],
        }

    def render(self) -> str:
        """Clearly delimited privileged files for the advisor prompt."""

        blocks: list[str] = []
        for entry in self.files:
            header = (
                f"----- BEGIN PRIVILEGED FILE {entry.relative_path} "
                f"(sha256={entry.sha256[:16]} bytes={entry.size_bytes}"
                f"{' TRUNCATED' if entry.truncated else ''}) -----"
            )
            body = entry.text
            if body is None:
                body = f"(binary or undecodable content omitted: {entry.skipped_reason})"
            blocks.append(
                f"{header}\n{body}\n----- END PRIVILEGED FILE {entry.relative_path} -----"
            )
        if self.omitted_paths:
            blocks.append(
                "NOTE: the following privileged files were omitted because the total "
                "size budget was exhausted: " + ", ".join(self.omitted_paths)
            )
        return "\n\n".join(blocks) if blocks else "(no privileged material available)"
