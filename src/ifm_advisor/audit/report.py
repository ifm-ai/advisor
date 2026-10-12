"""Audit one advised attempt's public trajectory.

Answers five questions: does the trajectory read as one coherent solver; does each change of
direction have public support; does the solver mention an advisor, hidden tests or a reference
solution; does it reproduce privileged text; and did the final implementation pass verification
(read from the verifier, never judged). The trajectory is never modified.

The privileged material is the set the advisor saw: loaded with the advisor's own layout (from its
task-material manifest, or `privileged_roots`) and the loader's default size limits. Only the
leakage judge's copy of it is capped.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..config import EndpointConfig
from ..material import TaskLayout, TaskMaterial
from .attempt import (
    locate_trajectory,
    read_privileged_layout,
    read_trajectory,
    read_verification,
)
from .checks import leak_term_hits, privileged_overlap_hits
from .judges import COHERENCE_PROMPT, COHERENCE_SCHEMA, LEAKAGE_PROMPT, LEAKAGE_SCHEMA, judge

PASSING_VERDICTS = frozenset({"pass", "pass_mechanical_only"})
# Caps only the privileged material shown to the leakage judge; the mechanical checks use it all.
MAX_PRIVILEGED_CHARS = 120_000
NO_PRIVILEGED_MATERIAL = "no privileged material was loaded from the task directory"


async def run_audit(
    attempt_dir: Path,
    task_dir: Path,
    *,
    trajectory: Path | None = None,
    privileged_roots: Sequence[str] | None = None,
    judge_endpoint: EndpointConfig | None = None,
    judge_api_key: str = "",
    no_judge: bool = False,
    litellm_module: Any | None = None,
) -> dict[str, Any]:
    """Audit one attempt.

    privileged_roots: the task's privileged roots, overriding the layout recorded in
        `<attempt_dir>/advisor/task_material_manifest.json`; without either, the default layout.
        `layout_source` in the result says which was used: "flag", "manifest" or "default".

    Raises FileNotFoundError for a missing directory or trajectory, and ValueError for an unusable
    layout (a bad privileged root, a symlinked or special `advisor/` or manifest, or a manifest
    that cannot be read as a layout), privileged
    material that cannot be read, or a trajectory that cannot be decoded or parsed (see
    `read_trajectory`). Other OSError (an unreadable `advisor/` directory or attempt file)
    propagates; the CLI reports it with exit 2.
    """

    attempt_dir = Path(attempt_dir).expanduser().resolve()
    task_dir = Path(task_dir).expanduser().resolve()
    for directory in (attempt_dir, task_dir):
        if not directory.is_dir():
            raise FileNotFoundError(f"not a directory: {directory}")
    path = Path(trajectory).expanduser().resolve() if trajectory else locate_trajectory(attempt_dir)
    if not path.is_file():
        raise FileNotFoundError(f"trajectory not found: {path}")

    layout_source = "flag"
    if privileged_roots:
        layout = TaskLayout(privileged_roots=tuple(privileged_roots))
    elif recorded := read_privileged_layout(attempt_dir):
        layout, layout_source = recorded, "manifest"
    else:
        layout, layout_source = TaskLayout(), "default"

    transcript = read_trajectory(path)
    text = transcript.judge_text()
    material = TaskMaterial.from_task_dir(task_dir, layout)
    unreadable = [
        entry.relative_path
        for entry in material.files
        if (entry.skipped_reason or "").startswith("unreadable")
    ]
    if unreadable:
        # The audit cannot vouch for material it never saw: no verdict at all.
        raise ValueError(f"cannot read privileged material in {task_dir}: {', '.join(unreadable)}")
    privileged_text = material.privileged_text()
    has_material = material.has_privileged_text()
    issues: list[str] = []
    audit: dict[str, Any] = {
        "attempt_dir": str(attempt_dir),
        "task_dir": str(task_dir),
        "trajectory_path": str(path),
        "privileged_roots": list(layout.privileged_roots),
        "layout_source": layout_source,
        "judge_model": None,
        "coherent": None,
        "direct_leakage": None,
        "suspicious_privileged_overlap": None,
        "mentions_advisor": None,
        "verification": read_verification(attempt_dir),
        "mechanical": {},
        "issues": issues,
        "verdict": "inconclusive",
    }

    if not has_material:
        # Nothing to compare against: the overlap check and the leakage judge cannot pass it.
        issues.append(NO_PRIVILEGED_MATERIAL)

    # The mechanical checks scan everything, decoded; only the judges get the clipped text.
    audit["judge_input_truncated"] = text != transcript.judge_text(max_chars=None)
    privileged_input, audit["privileged_input_truncated"] = _leakage_judge_material(material)
    scan_text = transcript.scan_text()
    term_hits = leak_term_hits(scan_text)
    overlap_hits = privileged_overlap_hits(scan_text, privileged_text)
    audit["mechanical"] = {
        "leak_pattern_hits": term_hits,
        "verbatim_privileged_lines": overlap_hits,
    }
    found = bool(term_hits or overlap_hits)  # a finding of leakage or incoherence
    if term_hits:
        issues.append(f"transcript matched leakage patterns: {term_hits}")
        audit["mentions_advisor"] = audit["direct_leakage"] = True
    if overlap_hits:
        issues.append(f"transcript reproduces {len(overlap_hits)} privileged line(s) verbatim")
        audit["suspicious_privileged_overlap"] = True

    judged_both = False
    if no_judge:
        audit["direct_leakage"] = audit["mentions_advisor"] = bool(term_hits)
        # Undetermined (null) when there was no privileged material to compare against.
        audit["suspicious_privileged_overlap"] = bool(overlap_hits) if has_material else None
    elif judge_endpoint is None:
        issues.append(
            "judge endpoint not configured; "
            "pass --judge-model and --judge-base-url or use --no-judge"
        )
    elif not judge_api_key:
        issues.append(f"judge API key variable {judge_endpoint.api_key_env!r} is not exported")
    else:
        found_by_judges, judged_both = await _run_judges(
            audit,
            text,
            material.instruction,
            privileged_input,
            judge_endpoint,
            judge_api_key,
            litellm_module,
        )
        found = found or found_by_judges

    # Fail closed: any finding fails, even when a judge was missing or down, and nothing passes
    # without privileged material to check against.
    if found:
        audit["verdict"] = "fail"
    elif not has_material:
        audit["verdict"] = "inconclusive"
    elif no_judge:
        audit["verdict"] = "pass_mechanical_only"
    elif judged_both:
        audit["verdict"] = "pass"
    else:
        audit["verdict"] = "inconclusive"
    return audit


def _leakage_judge_material(material: TaskMaterial) -> tuple[str, bool]:
    """The rendered privileged material for the leakage judge, capped. Returns (text, truncated)."""

    rendered = material.render()
    if len(rendered) <= MAX_PRIVILEGED_CHARS:
        return rendered, False
    omitted = len(rendered) - MAX_PRIVILEGED_CHARS
    return (
        f"{rendered[:MAX_PRIVILEGED_CHARS]}\n...[{omitted} characters of privileged material "
        "omitted]...",
        True,
    )


async def _run_judges(
    audit: dict[str, Any],
    text: str,
    instruction: str,
    privileged: str,
    judge_endpoint: EndpointConfig,
    judge_api_key: str,
    litellm_module: Any | None,
) -> tuple[bool, bool]:
    """Run both judges, recording their answers. Returns (found something, both completed)."""

    issues: list[str] = audit["issues"]
    found = False
    audit["judge_model"] = judge_endpoint.model
    # Two separate prompts: the coherence judge never sees privileged material.
    coherence_input = f"=== PUBLIC TASK ===\n{instruction}\n\n=== AGENT TRANSCRIPT ===\n{text}\n"
    leakage_input = (
        f"=== AGENT TRANSCRIPT ===\n{text}\n\n"
        "=== PRIVILEGED MATERIAL (the agent must not have had this) ===\n"
        f"{privileged}\n"
    )
    answers: tuple[
        dict[str, Any] | BaseException, dict[str, Any] | BaseException
    ] = await asyncio.gather(
        judge(
            COHERENCE_PROMPT,
            coherence_input,
            judge_endpoint,
            api_key=judge_api_key,
            schema=COHERENCE_SCHEMA,
            litellm_module=litellm_module,
        ),
        judge(
            LEAKAGE_PROMPT,
            leakage_input,
            judge_endpoint,
            api_key=judge_api_key,
            schema=LEAKAGE_SCHEMA,
            litellm_module=litellm_module,
        ),
        return_exceptions=True,
    )
    coherence, leakage = answers

    if isinstance(coherence, BaseException):
        issues.append(f"coherence judge failed: {type(coherence).__name__}: {coherence}")
    else:
        audit["coherent"] = coherence["coherent"]
        unsupported = coherence["unsupported_direction_changes"]
        audit["unsupported_direction_changes"] = unsupported
        audit["coherence_notes"] = coherence.get("notes", "")
        if not audit["coherent"]:
            issues.append("coherence judge found the trajectory incoherent")
            found = True
        if unsupported:
            issues.append(f"{len(unsupported)} unsupported direction change(s) reported")
            found = True

    if isinstance(leakage, BaseException):
        issues.append(f"leakage judge failed: {type(leakage).__name__}: {leakage}")
    else:
        audit["mentions_advisor"] = leakage["mentions_advisor"] or bool(audit["mentions_advisor"])
        audit["suspicious_privileged_overlap"] = leakage["suspicious_privileged_overlap"] or bool(
            audit["suspicious_privileged_overlap"]
        )
        audit["direct_leakage"] = audit["mentions_advisor"]
        audit["leaked_items"] = leakage["leaked_items"]
        audit["leakage_notes"] = leakage.get("notes", "")
        if leakage["mentions_advisor"]:
            issues.append("leakage judge found a reference to privileged information")
            found = True
        if leakage["suspicious_privileged_overlap"]:
            issues.append("leakage judge found suspicious overlap with privileged material")
            found = True
        if leakage["leaked_items"] and not (
            leakage["mentions_advisor"] or leakage["suspicious_privileged_overlap"]
        ):
            # Fail closed: a judge that lists leaked items has found something, flags or not.
            issues.append(
                f"leakage judge listed {len(leakage['leaked_items'])} leaked item(s) but set "
                "neither flag"
            )
            found = True

    judged_both = not isinstance(coherence, BaseException) and not isinstance(
        leakage, BaseException
    )
    return found, judged_both
