"""Deterministic leak checks, shared by the trajectory audit and the `screen_hints` guard.

Both functions run without any model, so they work offline and cost microseconds.
"""

from __future__ import annotations

import re

# Terms a solver has no legitimate reason to use about its own work.
LEAK_PATTERNS: tuple[str, ...] = (
    r"\badvisor\b",
    r"\breviewer\b",
    r"\bhidden test",
    r"\bprivileged\b",
    r"\breference (?:solution|implementation|patch)\b",
    r"\bgold (?:solution|patch)\b",
    r"\boracle\b",
    r"\bself-check\b",
    r"\bwas told\b",
    r"\bwas advised\b",
    r"\bsomeone (?:told|suggested)\b",
)
# Shorter shared lines are too common in ordinary code to be evidence of copying.
MIN_OVERLAP_LINE_CHARS = 40
_MAX_OVERLAP_HITS = 20
# Every candidate line is at least MIN_OVERLAP_LINE_CHARS long, so any copy of it in the text
# contains a _GRAM-character window starting at a multiple of _STRIDE. Indexing only those windows
# finds each candidate with a few lookups instead of a scan of the whole text per line.
_GRAM = 16
_STRIDE = MIN_OVERLAP_LINE_CHARS - _GRAM + 1
# Beyond this many candidate positions to verify, one plain scan of the text is cheaper.
_MAX_VERIFICATIONS = 256


def leak_term_hits(text: str) -> list[str]:
    """The distinct leak terms that occur in *text*, sorted."""

    lowered = text.lower()
    return sorted(
        {
            match.group(0).strip()
            for pattern in LEAK_PATTERNS
            for match in re.finditer(pattern, lowered)
        }
    )


def privileged_overlap_hits(text: str, privileged_text: str) -> list[str]:
    """Long, distinctive privileged lines that appear verbatim (modulo whitespace) in *text*.

    Each hit names at most the first 60 characters of the line, so a report never reproduces a
    privileged line in full.
    """

    haystack = " ".join(text.split())
    index = _window_index(haystack)
    hits: list[str] = []
    seen: set[str] = set()
    for line in privileged_text.splitlines():
        candidate = " ".join(line.split())
        if len(candidate) < MIN_OVERLAP_LINE_CHARS or candidate in seen:
            continue
        seen.add(candidate)
        if _occurs(candidate, haystack, index):
            hits.append(f"{candidate[:60]}... ({len(candidate)} chars)")
            if len(hits) == _MAX_OVERLAP_HITS:
                break
    return hits


def _window_index(haystack: str) -> dict[str, list[int]]:
    """Positions of the _GRAM-character windows of *haystack* that start at multiples of _STRIDE."""

    index: dict[str, list[int]] = {}
    for position in range(0, len(haystack) - _GRAM + 1, _STRIDE):
        index.setdefault(haystack[position : position + _GRAM], []).append(position)
    return index


def _occurs(candidate: str, haystack: str, index: dict[str, list[int]]) -> bool:
    """Whether *candidate* (at least MIN_OVERLAP_LINE_CHARS long) occurs in *haystack*.

    A copy starting at q contains an indexed window at some p = q + offset with offset < _STRIDE,
    and that window equals candidate[offset : offset + _GRAM]; each such q is then verified.
    """

    found = [
        (offset, positions)
        for offset in range(_STRIDE)
        if (positions := index.get(candidate[offset : offset + _GRAM]))
    ]
    if sum(len(positions) for _, positions in found) > _MAX_VERIFICATIONS:
        return candidate in haystack
    return any(
        position >= offset and haystack.startswith(candidate, position - offset)
        for offset, positions in found
        for position in positions
    )


def screen_text(text: str, privileged_text: str) -> list[str]:
    """Every reason *text* should not reach the solver; empty when it is clean."""

    return [f"leak term: {hit}" for hit in leak_term_hits(text)] + [
        f"verbatim privileged line: {hit}" for hit in privileged_overlap_hits(text, privileged_text)
    ]
