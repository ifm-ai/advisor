"""Read a Harbor attempt directory: its public trajectory, the verifier's verdict and the
privileged layout the advisor used.

Nothing here writes to the attempt directory.
"""

from __future__ import annotations

import contextlib
import fnmatch
import json
import math
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..artifacts import TASK_MATERIAL_MANIFEST_NAME
from ..material import TaskLayout

# Harbor writes an ATIF trajectory and, for Mini-SWE-Agent, the raw conversation as well.
TRAJECTORY_CANDIDATES = (
    Path("agent") / "trajectory.json",
    Path("agent") / "mini-swe-agent.trajectory.json",
    Path("trajectory.json"),
)
MAX_TRAJECTORY_CHARS = 200_000
# How many layers of JSON-inside-a-string (e.g. tool-call arguments) are decoded for scanning.
MAX_DECODE_DEPTH = 3
MANIFEST_PATH = Path("advisor") / TASK_MATERIAL_MANIFEST_NAME
REWARD_KEY = "reward"
PASS_THRESHOLD = 1.0


def _regular_file(path: Path) -> bool:
    """True for a regular file that is not a symlink; symlinks are never followed.

    A missing path is not a file. Any other OSError (an unreadable parent directory) propagates,
    the same on every Python version.
    """

    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False


def locate_trajectory(attempt_dir: Path) -> Path:
    """The attempt's trajectory file. Raises FileNotFoundError when there is none."""

    for relative in TRAJECTORY_CANDIDATES:
        candidate = attempt_dir / relative
        if _regular_file(candidate):
            return candidate
    found = sorted(
        (
            Path(root) / name
            for root, _dirs, names in os.walk(attempt_dir, followlinks=False)
            for name in names
            if fnmatch.fnmatch(name, "*trajectory*.json")
        ),
        key=lambda found_path: found_path.relative_to(attempt_dir).as_posix(),
    )
    for candidate in found:
        if _regular_file(candidate):
            return candidate
    raise FileNotFoundError(f"no Harbor trajectory found beneath {attempt_dir}")


def _render(payload: Any) -> str:
    return json.dumps(payload, indent=1, ensure_ascii=False, default=str)


@dataclass(frozen=True, slots=True)
class Trajectory:
    """A trajectory file, read and parsed once.

    text: the file, decoded, without its byte order mark. rendered: a JSON document re-rendered,
    or None for JSON Lines and plain text. values: the JSON values whose strings the scan decodes,
    with objects as flat [key, value, ...] lists so that duplicate keys all survive.
    """

    text: str
    rendered: str | None
    values: tuple[Any, ...]

    def judge_text(self, max_chars: int | None = MAX_TRAJECTORY_CHARS) -> str:
        """The text the judges read, keeping both ends when it is long. None means no clipping."""

        if self.rendered is None:
            return self.text if max_chars is None else self.text[:max_chars]
        rendered = self.rendered
        if max_chars is None or len(rendered) <= max_chars:
            return rendered
        half = max_chars // 2
        return (
            f"{rendered[:half]}\n...[{len(rendered) - max_chars} characters "
            f"omitted from the middle]...\n{rendered[-half:]}"
        )

    def scan_text(self) -> str:
        """The text the mechanical leak checks scan: everything, escapes and encodings undone.

        The rendered document (or, for JSON Lines and plain text, the text itself), followed by
        every string key and value in it, decoded, one per line, so a copied line containing
        quotes, backslashes or tabs, or one inside a tool call's JSON-encoded arguments, reads as
        written. Duplicate keys are all decoded.
        """

        head = self.text if self.rendered is None else self.rendered
        return "\n".join(
            [head, *(text for value in self.values for text in _decoded_strings(value))]
        )


def read_trajectory(path: Path) -> Trajectory:
    """Read and parse a trajectory once. Raises ValueError when it is unusable.

    The encoding is detected the way `json.loads` detects it for bytes (UTF-8 with or without a
    byte order mark, UTF-16 or UTF-32) and decoding is strict. Text that starts with `{` or `[`
    must be one JSON document, or JSON Lines in which every non-blank line parses; anything else
    that starts that way (truncated, concatenated documents, a trailing comma, nesting too deep to
    parse) is unusable, as are an empty file and one that does not decode. Any other text is a
    plain transcript: it is scanned as it is, followed by the decoded strings of each of its lines
    that is a JSON value.
    """

    raw = path.read_bytes()
    encoding = json.detect_encoding(raw)
    try:
        # The utf-8-sig, utf-16 and utf-32 codecs consume one leading byte order mark.
        text = raw.decode(encoding, errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError(f"cannot decode the trajectory {path} as {encoding}: {error}") from None
    if not text.strip():
        raise ValueError(f"the trajectory {path} is empty or blank")
    if encoding.endswith(("-be", "-le")) and text.lstrip()[:1] not in ("{", "["):
        # Without a byte order mark, UTF-16 and UTF-32 are guessed from NUL bytes, which is only
        # reliable for JSON: UTF-8 text with a NUL near its start would be read as garbage.
        raise ValueError(f"the trajectory {path} has no byte order mark and is not JSON")
    if text.lstrip()[:1] not in ("{", "["):
        return Trajectory(text, None, tuple(_json_lines(text, strict=False)))
    try:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as document_error:
            try:
                values = _json_lines(text, strict=True)
            except ValueError as lines_error:
                raise ValueError(
                    f"the trajectory {path} is neither one JSON document ({document_error}) nor "
                    f"JSON Lines ({lines_error})"
                ) from None
            return Trajectory(text, None, tuple(values))
        return Trajectory(text, _render(payload), (_parse_every_pair(text),))
    except RecursionError:
        raise ValueError(f"the trajectory {path} is nested too deeply to parse") from None


def _json_lines(text: str, *, strict: bool) -> list[Any]:
    """The JSON value of each non-blank line of *text*.

    Only "\n" separates records: splitlines() would also cut at U+2028, U+2029 and U+0085, which
    JSON allows unescaped inside a string. When *strict*, a line that does not parse raises
    ValueError (and nesting too deep, RecursionError); otherwise it is skipped.
    """

    values = []
    for number, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            values.append(_parse_every_pair(line))
        except RecursionError:
            if strict:
                raise
        except ValueError as error:
            if strict:
                raise ValueError(f"line {number}: {error}") from None
    return values


def _decoded_strings(payload: Any) -> Iterator[str]:
    """Every string key and value in *payload*, decoded, depth first.

    A string that itself holds a JSON object or array, such as a tool call's `arguments`, is
    followed by the string keys and values inside it, up to MAX_DECODE_DEPTH layers of encoding.
    """

    stack: list[tuple[Any, int]] = [(payload, 0)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, dict):
            parts = [part for key, item in value.items() for part in (key, item)]
            stack.extend((part, depth) for part in reversed(parts))
        elif isinstance(value, list):
            stack.extend((item, depth) for item in reversed(value))
        elif isinstance(value, str):
            yield value
            if depth >= MAX_DECODE_DEPTH or value.lstrip()[:1] not in ("{", "["):
                continue
            try:
                nested = json.loads(value, object_pairs_hook=_pairs)
            except (ValueError, RecursionError):
                continue
            if isinstance(nested, list):
                stack.append((nested, depth + 1))


def _pairs(pairs: list[tuple[str, Any]]) -> list[Any]:
    """An object as the flat list [key, value, key, value, ...], so duplicate keys all survive."""

    return [part for pair in pairs for part in pair]


def _parse_every_pair(text: str) -> Any:
    return json.loads(text.removeprefix("\ufeff"), object_pairs_hook=_pairs)


def read_privileged_layout(attempt_dir: Path) -> TaskLayout | None:
    """The privileged layout recorded in the advisor's task-material manifest, if any.

    None when `advisor/` or the manifest is absent, or when the manifest records no layout
    (material built with `from_texts`). Raises ValueError when `advisor/` is a symlink or not a
    directory, when the manifest is a symlink or not a regular file, when either cannot be read
    (including when `advisor/` is unreadable), or when the manifest cannot be read as a layout:
    the audit cannot tell which layout the advisor used, and guessing the default could pass a
    copy of material the advisor saw. Nothing is followed through a symlink, and a special file is
    never opened.
    """

    directory = attempt_dir / MANIFEST_PATH.parent
    path = attempt_dir / MANIFEST_PATH
    for candidate, is_expected_type, expected in (
        (directory, stat.S_ISDIR, "a directory"),
        (path, stat.S_ISREG, "a regular file"),
    ):
        try:
            mode = candidate.lstat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as error:
            raise ValueError(f"cannot read the task-material manifest {path}: {error}") from None
        if not is_expected_type(mode):
            kind = "a symlink" if stat.S_ISLNK(mode) else f"not {expected}"
            raise ValueError(
                f"{candidate} is {kind}, so the privileged layout the advisor used is unknown; "
                "pass the privileged roots explicitly"
            )
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read the task-material manifest {path}: {error}") from None
    if not isinstance(manifest, dict):
        raise ValueError(f"the task-material manifest {path} is not a JSON object")
    roots = manifest.get("privileged_roots")
    if roots is None:
        return None
    excluded = manifest.get("excluded_directory_names")
    if not _strings(roots) or (excluded is not None and not _strings(excluded)):
        raise ValueError(
            f"the task-material manifest {path} does not record a layout: privileged_roots and "
            "excluded_directory_names must be lists of strings"
        )
    try:
        if excluded is None:
            return TaskLayout(privileged_roots=tuple(roots))
        return TaskLayout(privileged_roots=tuple(roots), excluded_dirs=frozenset(excluded))
    except ValueError as error:
        raise ValueError(f"the task-material manifest {path} is not usable: {error}") from None


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _reward_from_result(payload: dict[str, Any]) -> tuple[float | None, bool]:
    """(reward, reported). `reported` is False when result.json carries no verifier outcome."""

    verifier_result = payload.get("verifier_result")
    if isinstance(verifier_result, dict):
        # Harbor's TrialResult: verifier_result.rewards. A missing "reward" key falls back to the
        # first numeric reward; a verifier that reported no rewards scores 0.0; a non-finite
        # reward is a verifier failure, not a score.
        rewards = verifier_result.get("rewards")
        if not isinstance(rewards, dict) or not rewards:
            return 0.0, True
        if REWARD_KEY in rewards:
            return _finite(rewards[REWARD_KEY]), True
        return next((r for r in map(_finite, rewards.values()) if r is not None), None), True
    verifier = payload.get("verifier")
    legacy = (
        payload.get(REWARD_KEY),
        verifier.get(REWARD_KEY) if isinstance(verifier, dict) else None,
    )
    for candidate in legacy:
        if candidate is not None:
            return _finite(candidate), True
    return None, False


def _read_result(attempt_dir: Path) -> dict[str, Any] | None:
    """result.json as an object, or None when it is missing, not JSON or not an object.

    An OSError reading a result.json that exists (no permission) propagates.
    """

    result_path = attempt_dir / "result.json"
    if not _regular_file(result_path):
        return None
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except ValueError:  # invalid JSON or UTF-8
        return None
    return payload if isinstance(payload, dict) else None


def read_verification(attempt_dir: Path) -> dict[str, Any]:
    """The verifier's verdict, read from Harbor's own files and never judged by a model.

    A missing file, or one whose content is not valid JSON or a number, gives no reward. An
    OSError reading a file that exists (no permission) propagates: the verdict is unknown, not
    absent.
    """

    result = _read_result(attempt_dir)
    reward, reported = _reward_from_result(result) if result is not None else (None, False)
    reward_path = attempt_dir / "verifier" / "reward.txt"
    if not reported and _regular_file(reward_path):
        with contextlib.suppress(ValueError):  # not UTF-8, or not a number
            reward = _finite(float(reward_path.read_text(encoding="utf-8").strip()))
    return {
        "result_json_present": _regular_file(attempt_dir / "result.json"),
        "reward": reward,
        "passed": None if reward is None else reward >= PASS_THRESHOLD,
    }


def read_task_name(attempt_dir: Path) -> str:
    """Harbor's task_name for the attempt, or "" when it is unknown."""

    result = _read_result(attempt_dir) or {}
    name = result.get("task_name")
    return name if isinstance(name, str) else ""
