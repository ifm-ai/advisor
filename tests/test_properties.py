"""Properties that hold for every input, checked with generated examples."""

from __future__ import annotations

import json

import pytest
from helpers import decision_payload
from hypothesis import given, settings
from hypothesis import strategies as st

from ifm_advisor.audit import checks
from ifm_advisor.audit.checks import (
    MIN_OVERLAP_LINE_CHARS,
    leak_term_hits,
    privileged_overlap_hits,
    screen_text,
)
from ifm_advisor.decision import AdvisorDecision, AdvisorDecisionError, parse_advisor_decision
from ifm_advisor.material import TaskMaterial

# A small alphabet makes windows collide often, which exercises the index; the fallback scan needs
# the dedicated repeated-fragment test below.
LINE_ALPHABET = "ab é\t"
privileged_lines = st.lists(
    st.text(alphabet=LINE_ALPHABET, min_size=MIN_OVERLAP_LINE_CHARS - 10, max_size=90),
    min_size=1,
    max_size=6,
)


def naive_overlap_hits(text: str, privileged_text: str) -> list[str]:
    """The reference: privileged_overlap_hits with a plain substring test and no index."""

    haystack = " ".join(text.split())
    hits: list[str] = []
    seen: set[str] = set()
    for line in privileged_text.splitlines():
        candidate = " ".join(line.split())
        if len(candidate) < MIN_OVERLAP_LINE_CHARS or candidate in seen:
            continue
        seen.add(candidate)
        if candidate in haystack:
            hits.append(f"{candidate[:60]}... ({len(candidate)} chars)")
            if len(hits) == 20:
                break
    return hits


@pytest.mark.parametrize("length", range(MIN_OVERLAP_LINE_CHARS, MIN_OVERLAP_LINE_CHARS + 6))
def test_a_copy_at_every_offset_is_found_like_the_naive_scan(length):
    line = "".join("abcdefghij"[(i * 7 + length) % 10] for i in range(length))
    for offset in range(61):
        text = "0123456789" * 8
        text = text[:offset].ljust(offset, "#") + line + "#" * 70
        assert naive_overlap_hits(text, line), (length, offset)
        assert privileged_overlap_hits(text, line) == naive_overlap_hits(text, line), (
            length,
            offset,
        )


def test_the_fallback_scan_agrees_with_the_naive_scan():
    fragment = "abcdefghijklmnop"
    run = fragment * 1000
    present = fragment * 2 + "XYZ123456789"
    absent = fragment * 2 + "987654321ZYX"
    text = run + "|" + present
    index = checks._window_index(" ".join(text.split()))
    for line in (present, absent):
        candidate = " ".join(line.split())
        found = [index.get(candidate[o : o + checks._GRAM]) or [] for o in range(checks._STRIDE)]
        # The index alone would need more verifications than allowed, so `_occurs` falls back.
        assert sum(len(positions) for positions in found) > checks._MAX_VERIFICATIONS
        assert privileged_overlap_hits(text, line) == naive_overlap_hits(text, line)
    assert privileged_overlap_hits(text, present) and not privileged_overlap_hits(text, absent)


@st.composite
def trajectories(draw: st.DrawFn) -> tuple[str, str]:
    """(trajectory text, privileged text): noise, exact copies and one-character near misses."""

    lines = draw(privileged_lines)
    pieces = []
    for _ in range(draw(st.integers(0, 6))):
        kind = draw(st.sampled_from(["noise", "copy", "near miss"]))
        if kind == "noise":
            pieces.append(draw(st.text(alphabet=LINE_ALPHABET + "\n", max_size=80)))
            continue
        line = draw(st.sampled_from(lines))
        if kind == "near miss" and line:
            position = draw(st.integers(0, len(line) - 1))
            line = line[:position] + "Z" + line[position + 1 :]
        pieces.append(line)
    return "".join(pieces), "\n".join(lines)


@settings(max_examples=400, deadline=None)
@given(trajectories())
def test_indexed_overlap_equals_the_naive_scan(case):
    text, privileged_text = case

    assert privileged_overlap_hits(text, privileged_text) == naive_overlap_hits(
        text, privileged_text
    )


@settings(max_examples=200, deadline=None)
@given(trajectories())
def test_screen_text_reports_terms_then_overlaps(case):
    text, privileged_text = case

    assert screen_text(text, privileged_text) == [
        f"leak term: {hit}" for hit in leak_term_hits(text)
    ] + [f"verbatim privileged line: {hit}" for hit in naive_overlap_hits(text, privileged_text)]


# No field name, enum value or pydantic message contains a run of 8 of these letters.
markers = st.text(alphabet="QXZ", min_size=8, max_size=24)
PLACEMENTS = (
    "bool field",
    "enum field",
    "reason_code",
    "confidence",
    "unknown key",
    "nested value",
    "not json",
    "diagnosis",
    "hint",
)


def _invalid_response(marker: str, placement: str) -> str:
    payload = decision_payload()
    if placement == "bool field":
        payload["intervene"] = marker
    elif placement == "enum field":
        payload["action"] = marker
    elif placement == "reason_code":
        payload["reason_code"] = marker
    elif placement == "confidence":
        payload["confidence"] = marker
    elif placement == "hint":
        payload["hint"] = marker
        payload["confidence"] = 2.0  # invalid, so the hint is in a rejected response
    elif placement == "unknown key":
        payload[marker] = 1
    elif placement == "nested value":
        payload["plan"] = {"step": marker}
    elif placement == "diagnosis":
        payload["private_diagnosis"] = marker
        payload["confidence"] = 2.0  # invalid, so the diagnosis is in a rejected response
    else:
        return f"not json {marker}"
    return json.dumps(payload)


@settings(max_examples=300, deadline=None)
@given(markers, st.sampled_from(PLACEMENTS))
def test_a_parse_error_never_repeats_the_advisor_text(marker, placement):
    try:
        parse_advisor_decision(_invalid_response(marker, placement))
    except AdvisorDecisionError as error:
        assert marker not in str(error) and marker not in repr(error)
        assert error.__cause__ is None and error.__context__ is None
    else:
        raise AssertionError("an invalid response was accepted")


json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=12), children, max_size=4)
    ),
    max_leaves=12,
)
arbitrary_responses = st.one_of(
    st.text(),
    json_values.map(json.dumps),
    json_values.map(lambda value: f"```json\n{json.dumps(value)}\n```"),
)


@settings(max_examples=500, deadline=None)
@given(arbitrary_responses)
def test_parsing_arbitrary_text_returns_a_decision_or_raises_the_decision_error(response):
    try:
        decision = parse_advisor_decision(response)
    except AdvisorDecisionError:
        return
    assert isinstance(decision, AdvisorDecision)


@settings(max_examples=300, deadline=None)
@given(
    st.dictionaries(st.text(min_size=1, max_size=8), st.text(max_size=300), max_size=8),
    st.integers(1, 200),
    st.integers(0, 400),
)
def test_material_budgets_hold(privileged, max_file_bytes, extra):
    max_total_bytes = max_file_bytes + extra

    material = TaskMaterial.from_texts(
        "Fix it.", privileged, max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes
    )

    admitted = {entry.relative_path: entry for entry in material.files}
    assert material.total_content_bytes <= max_total_bytes
    assert set(admitted) | set(material.omitted_paths) == set(privileged)
    assert not set(admitted) & set(material.omitted_paths)
    assert set(material.truncated_paths) <= set(admitted)
    for path, entry in admitted.items():
        assert entry.text is not None
        assert len(entry.text.encode("utf-8")) <= max_file_bytes
        assert entry.truncated == (path in material.truncated_paths)
        if not entry.truncated:
            assert entry.text == privileged[path]
