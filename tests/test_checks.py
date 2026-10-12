import random

import pytest

from ifm_advisor.audit.checks import leak_term_hits, privileged_overlap_hits, screen_text
from ifm_advisor.cues import SAFE_INSTRUCTIONS, compile_self_check_instruction

PRIVILEGED = "\n".join(
    [
        "x = 1",  # too short to be distinctive
        "def test_unfired_sessions_survive_garbage_collection(processor):",
    ]
)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("I inspected DESIGN.md and ran the tests.", []),
        ("The advisor told me to look again.", ["advisor"]),
        ("Per the reference solution I will patch gc.py.", ["reference solution"]),
        ("The hidden tests check retraction behavior.", ["hidden test"]),
    ],
)
def test_leak_terms(content, expected):
    assert leak_term_hits(content) == expected


def test_overlap_requires_a_long_distinctive_line():
    assert privileged_overlap_hits("x = 1", PRIVILEGED) == []
    hits = privileged_overlap_hits(
        "I wrote def test_unfired_sessions_survive_garbage_collection(processor): myself",
        PRIVILEGED,
    )
    assert len(hits) == 1
    # The report describes the overlap; it does not echo the whole line.
    assert hits[0].endswith("chars)")


def test_overlap_ignores_whitespace_differences():
    text = "def  test_unfired_sessions_survive_garbage_collection(processor):"

    assert len(privileged_overlap_hits(text, PRIVILEGED)) == 1


def test_screen_combines_both_checks():
    hits = screen_text(
        "The hidden tests want def test_unfired_sessions_survive_garbage_collection(processor):",
        PRIVILEGED,
    )

    assert hits[0] == "leak term: hidden test"
    assert hits[1].startswith("verbatim privileged line: ")
    assert screen_text("Hmm. Only odd-length lists have run so far.", PRIVILEGED) == []


@pytest.mark.parametrize("action", sorted(SAFE_INSTRUCTIONS))
def test_compiled_instruction_cannot_trip_the_audit(action):
    """The self-check sentence is text this package injects; a solver echoing it is not a leak."""

    assert leak_term_hits(compile_self_check_instruction(action)) == []


def test_audit_still_detects_the_terms_the_instruction_avoids():
    """Guards the test above against being satisfied by a broken check."""

    assert leak_term_hits("the advisor and the reviewer said") == ["advisor", "reviewer"]


def _naive_overlap_hits(text, privileged_text):
    """The definition: each long, distinct privileged line found anywhere in the text."""

    haystack = " ".join(text.split())
    hits, seen = [], set()
    for line in privileged_text.splitlines():
        candidate = " ".join(line.split())
        if len(candidate) < 40 or candidate in seen:
            continue
        seen.add(candidate)
        if candidate in haystack:
            hits.append(f"{candidate[:60]}... ({len(candidate)} chars)")
    return hits[:20]


@pytest.mark.parametrize("seed", range(20))
def test_overlap_finds_a_copy_at_every_offset(seed):
    """A copy is found wherever it starts or ends: mid-token, at either end, or across lines."""

    rng = random.Random(seed)
    alphabet = "abcdefghij (),=_"
    lines = ["".join(rng.choices(alphabet, k=rng.randint(40, 90))) for _ in range(30)]
    privileged = "\n".join(lines)
    copied = rng.sample(lines, 10)
    pieces = []
    for line in copied:
        pieces.append("".join(rng.choices(alphabet, k=rng.randint(0, 60))))
        pieces.append(line)
    pieces.append("".join(rng.choices(alphabet, k=rng.randint(0, 3))))
    text = "".join(pieces)

    hits = privileged_overlap_hits(text, privileged)

    assert hits == _naive_overlap_hits(text, privileged)
    long_enough = {" ".join(line.split()) for line in copied} - {""}
    assert len(hits) >= sum(len(line) >= 40 for line in long_enough) > 0


def test_overlap_with_frequent_fragments_matches_the_definition():
    """A fragment repeated thousands of times in the text does not change the answer."""

    line = "self.assertEqual(result, expected)  # " + "x" * 30
    near_miss = "self.assertEqual(result, expected)  # " + "x" * 29 + "y"
    text = (near_miss + "\n") * 3_000 + line
    privileged = f"{line}\n{near_miss}y\n"

    assert privileged_overlap_hits(text, privileged) == _naive_overlap_hits(text, privileged)
    assert len(privileged_overlap_hits(text, privileged)) == 1
    assert privileged_overlap_hits(text[:-1], privileged) == []
