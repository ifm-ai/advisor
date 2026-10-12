"""Aggregate attempt directories into per-arm results.

An arm is a set of attempt directories, for example every baseline attempt or every advised one.
For each attempt this reads only the verifier's verdict (Harbor's result.json or
verifier/reward.txt), advisor/summary.json, advisor/audit.json, and the trigger, intervened and
action fields of advisor/private_trace.jsonl. It never reads hints, plans or diagnoses.
"""

from __future__ import annotations

import csv
import glob
import json
import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audit.attempt import read_task_name, read_verification

Z_95 = 1.959963984540054
PASS_RATE_COLUMNS = ("benchmark", "arm", "solver", "advisor", "tasks", "attempts", "correct")
INTERVENTION_COLUMNS = ("benchmark", "arm", "trigger", "action", "count")


def wilson_interval(successes: int, trials: int, z: float = Z_95) -> tuple[float, float]:
    """The Wilson score interval for a binomial proportion; (0, 0) for zero trials."""

    if trials <= 0:
        return (0.0, 0.0)
    if not 0 <= successes <= trials:
        raise ValueError("successes must be between 0 and trials")
    p = successes / trials
    z2 = z * z
    denominator = 1 + z2 / trials
    centre = (p + z2 / (2 * trials)) / denominator
    margin = z * math.sqrt(p * (1 - p) / trials + z2 / (4 * trials * trials)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    directory: Path
    task: str
    reward: float | None
    passed: bool | None
    advised: bool
    reviews: int
    interventions: int
    advisor_failures: int
    hint_screen_fallbacks: int
    reviews_by_trigger: Counter[str]
    interventions_by_trigger_action: Counter[tuple[str, str]]
    audit_verdict: str | None

    @property
    def effectively_unadvised(self) -> bool:
        """An advised attempt whose advisor failed and never intervened ran as a baseline."""

        return self.advised and self.advisor_failures > 0 and self.interventions == 0

    @property
    def advised_without_reviews(self) -> bool:
        """An advised attempt that was never reviewed ran as a baseline.

        Usually the agent's calls never reached the advisor, or its finish tool was not the one
        the advisor was given.
        """

        return self.advised and self.reviews == 0


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_trace(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue  # a torn final line from an interrupted attempt
        if isinstance(record, dict):
            records.append(record)
    return records


def read_attempt(directory: Path) -> AttemptOutcome:
    directory = Path(directory)
    verification = read_verification(directory)
    summary = _read_json(directory / "advisor" / "summary.json")
    audit = _read_json(directory / "advisor" / "audit.json")
    reviews: Counter[str] = Counter()
    interventions: Counter[tuple[str, str]] = Counter()
    for record in _read_trace(directory / "advisor" / "private_trace.jsonl"):
        trigger = str(record.get("trigger", "unknown"))
        reviews[trigger] += 1
        if record.get("intervened"):
            decision = record.get("decision")
            action = decision.get("action", "unknown") if isinstance(decision, dict) else "unknown"
            interventions[(trigger, str(action))] += 1
    counters = summary or {}
    if "finish_reviews" in counters or "periodic_reviews" in counters:
        review_count = int(counters.get("finish_reviews", 0)) + int(
            counters.get("periodic_reviews", 0)
        )
    else:
        review_count = sum(reviews.values())
    return AttemptOutcome(
        directory=directory,
        task=read_task_name(directory) or str(counters.get("task") or ""),
        reward=verification["reward"],
        passed=verification["passed"],
        advised=summary is not None,
        reviews=review_count,
        interventions=int(counters.get("interventions", sum(interventions.values()))),
        advisor_failures=int(counters.get("advisor_failures", 0)),
        hint_screen_fallbacks=int(counters.get("hint_screen_fallbacks", 0)),
        reviews_by_trigger=reviews,
        interventions_by_trigger_action=interventions,
        audit_verdict=str(audit["verdict"]) if audit and "verdict" in audit else None,
    )


def expand(patterns: Sequence[str]) -> list[Path]:
    """Directories matched by glob patterns, sorted and de-duplicated."""

    found = {
        Path(match) for pattern in patterns for match in glob.glob(str(Path(pattern).expanduser()))
    }
    return sorted(path for path in found if path.is_dir())


def summarize_arm(name: str, directories: Iterable[Path]) -> dict[str, Any]:
    outcomes = [read_attempt(Path(directory)) for directory in sorted(directories)]
    attempts = len(outcomes)
    correct = sum(1 for outcome in outcomes if outcome.passed)
    reviews: Counter[str] = Counter()
    by_trigger_action: Counter[tuple[str, str]] = Counter()
    audits: Counter[str] = Counter()
    for outcome in outcomes:
        reviews.update(outcome.reviews_by_trigger)
        by_trigger_action.update(outcome.interventions_by_trigger_action)
        if outcome.audit_verdict is not None:
            audits[outcome.audit_verdict] += 1
    by_action: Counter[str] = Counter()
    for (_, action), count in by_trigger_action.items():
        by_action[action] += count
    return {
        "arm": name,
        "attempts": attempts,
        "tasks": len({outcome.task for outcome in outcomes if outcome.task}),
        "correct": correct,
        "unverified": sum(1 for outcome in outcomes if outcome.passed is None),
        "pass_rate": correct / attempts if attempts else 0.0,
        "pass_rate_ci95": list(wilson_interval(correct, attempts)),
        "advised_attempts": sum(1 for outcome in outcomes if outcome.advised),
        "attempts_with_intervention": sum(1 for outcome in outcomes if outcome.interventions > 0),
        "interventions": sum(outcome.interventions for outcome in outcomes),
        "reviews_by_trigger": dict(sorted(reviews.items())),
        "interventions_by_action": dict(sorted(by_action.items())),
        "interventions_by_trigger_action": {
            f"{trigger}:{action}": count
            for (trigger, action), count in sorted(by_trigger_action.items())
        },
        "advisor_failures": sum(outcome.advisor_failures for outcome in outcomes),
        "hint_screen_fallbacks": sum(outcome.hint_screen_fallbacks for outcome in outcomes),
        "effectively_unadvised": sum(1 for outcome in outcomes if outcome.effectively_unadvised),
        "advised_without_reviews": sum(
            1 for outcome in outcomes if outcome.advised_without_reviews
        ),
        "audit": dict(sorted(audits.items())),
    }


def pass_rate_rows(
    arms: Sequence[dict[str, Any]], *, benchmark: str, solver: str, advisor: str
) -> list[dict[str, Any]]:
    """Rows for results/pass_rates.csv. The advisor column is empty for an unadvised arm."""

    return [
        {
            "benchmark": benchmark,
            "arm": arm["arm"],
            "solver": solver,
            "advisor": advisor if arm["advised_attempts"] else "",
            "tasks": arm["tasks"],
            "attempts": arm["attempts"],
            "correct": arm["correct"],
        }
        for arm in arms
    ]


def intervention_rows(arms: Sequence[dict[str, Any]], *, benchmark: str) -> list[dict[str, Any]]:
    """Rows for results/interventions.csv."""

    rows = []
    for arm in arms:
        for key, count in arm["interventions_by_trigger_action"].items():
            trigger, action = key.split(":", 1)
            rows.append(
                {
                    "benchmark": benchmark,
                    "arm": arm["arm"],
                    "trigger": trigger,
                    "action": action,
                    "count": count,
                }
            )
    return rows


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def render_table(arms: Sequence[dict[str, Any]]) -> str:
    header = (
        f"{'arm':<24} {'attempts':>8} {'correct':>7} {'pass rate':>9} {'95% CI':>13} "
        f"{'intervened':>10} {'unadvised':>9} {'unreviewed':>10}"
    )
    lines = [header, "-" * len(header)]
    for arm in arms:
        low, high = arm["pass_rate_ci95"]
        interval = f"{low:.0%}-{high:.0%}"
        lines.append(
            f"{arm['arm']:<24} {arm['attempts']:>8} {arm['correct']:>7} {arm['pass_rate']:>9.1%} "
            f"{interval:>13} {arm['attempts_with_intervention']:>10} "
            f"{arm['effectively_unadvised']:>9} {arm['advised_without_reviews']:>10}"
        )
    return "\n".join(lines)
