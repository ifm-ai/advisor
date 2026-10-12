"""Command-line interface: `ifm-advisor audit` and `ifm-advisor summarize`."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from . import __version__
from .artifacts import write_private
from .audit.report import PASSING_VERDICTS, run_audit
from .config import EndpointConfig
from .summarize import (
    INTERVENTION_COLUMNS,
    PASS_RATE_COLUMNS,
    expand,
    intervention_rows,
    pass_rate_rows,
    render_table,
    summarize_arm,
    write_csv,
)


def _add_audit(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "audit",
        help="audit one advised attempt's public trajectory",
        description=(
            "Audit one advised attempt: offline leak checks, a public-coherence judge, a "
            "privileged-leakage judge, and the verifier's verdict. Exit 0 on a pass, 1 on a fail "
            "or an inconclusive audit, 2 on unusable input."
        ),
    )
    parser.add_argument("--attempt-dir", required=True, type=Path, help="Harbor attempt directory")
    parser.add_argument(
        "--task-dir",
        required=True,
        type=Path,
        help="task directory holding the privileged material",
    )
    parser.add_argument("--output", required=True, type=Path, help="where to write the audit JSON")
    parser.add_argument(
        "--trajectory",
        type=Path,
        help="audit this file (any JSON, e.g. a list of chat messages) instead of the attempt's",
    )
    parser.add_argument(
        "--privileged-root",
        action="append",
        metavar="PATH",
        help=(
            "a privileged root relative to the task directory; repeat for each root. Overrides "
            "the layout in the attempt's advisor/task_material_manifest.json (default: that "
            "layout, else README.md, solution and tests)"
        ),
    )
    parser.add_argument(
        "--judge-model",
        default=os.environ.get("JUDGE_MODEL", os.environ.get("ADVISOR_MODEL", "")),
        help="judge model (default: $JUDGE_MODEL, then $ADVISOR_MODEL)",
    )
    parser.add_argument(
        "--judge-base-url",
        default=os.environ.get("JUDGE_BASE_URL", os.environ.get("ADVISOR_BASE_URL", "")),
        help="judge endpoint (default: $JUDGE_BASE_URL, then $ADVISOR_BASE_URL)",
    )
    parser.add_argument(
        "--judge-provider", default="openai", help="LiteLLM provider prefix (default: %(default)s)"
    )
    parser.add_argument(
        "--judge-api-key-env",
        default="JUDGE_API_KEY",
        help="environment variable holding the judge key (default: %(default)s)",
    )
    parser.add_argument("--judge-timeout-sec", type=float, default=300.0)
    parser.add_argument("--no-judge", action="store_true", help="run only the offline checks")


def _run_audit(args: argparse.Namespace) -> int:
    judge_endpoint = None
    if not args.no_judge and args.judge_model and args.judge_base_url:
        try:
            judge_endpoint = EndpointConfig(
                model=args.judge_model,
                provider=args.judge_provider,
                api_base=args.judge_base_url,
                api_key_env=args.judge_api_key_env,
                timeout_sec=args.judge_timeout_sec,
            )
        except ValidationError as error:
            print(f"error: invalid judge endpoint: {error}", file=sys.stderr)
            return 2
    try:
        audit = asyncio.run(
            run_audit(
                args.attempt_dir,
                args.task_dir,
                trajectory=args.trajectory,
                privileged_roots=args.privileged_root,
                judge_endpoint=judge_endpoint,
                judge_api_key=os.environ.get(args.judge_api_key_env, ""),
                no_judge=args.no_judge,
            )
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    output = args.output.expanduser().resolve()
    if output == Path(audit["trajectory_path"]).resolve():
        print("error: --output would overwrite the audited trajectory", file=sys.stderr)
        return 2
    rendered = json.dumps(audit, indent=2, sort_keys=True)
    try:
        # Owner-only: the audit may quote the start of privileged lines.
        output.parent.mkdir(parents=True, exist_ok=True)
        write_private(output, (rendered + "\n").encode("utf-8"))
    except OSError as error:
        print(f"error: cannot write {output}: {error}", file=sys.stderr)
        return 2
    print(rendered)
    return 0 if audit["verdict"] in PASSING_VERDICTS else 1


def _add_summarize(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "summarize",
        help="aggregate attempt directories into per-arm results",
        description=(
            "Aggregate attempt directories into per-arm pass rates and advisor statistics. Reads "
            "verifier output, advisor/summary.json, advisor/audit.json and the trigger and action "
            "fields of the private trace; never hints, plans or diagnoses."
        ),
    )
    parser.add_argument(
        "--arm",
        action="append",
        required=True,
        metavar="NAME=GLOB",
        help="an arm and a glob of its attempt directories; repeat for each arm or glob",
    )
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    parser.add_argument(
        "--csv", type=Path, help="write pass-rate rows (results/pass_rates.csv format)"
    )
    parser.add_argument(
        "--interventions-csv",
        type=Path,
        help="write intervention rows (results/interventions.csv format)",
    )
    parser.add_argument("--benchmark", default="", help="benchmark name for the CSV rows")
    parser.add_argument("--solver", default="", help="solver model name for the CSV rows")
    parser.add_argument("--advisor", default="", help="advisor model name for the CSV rows")


def _parse_arms(values: list[str]) -> dict[str, list[str]]:
    arms: dict[str, list[str]] = {}
    for value in values:
        name, separator, pattern = value.partition("=")
        if not separator or not name or not pattern:
            raise ValueError(f"--arm must look like NAME=GLOB, got {value!r}")
        arms.setdefault(name, []).append(pattern)
    return arms


def _run_summarize(args: argparse.Namespace) -> int:
    try:
        arms = _parse_arms(args.arm)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    summaries = []
    try:
        for name, patterns in arms.items():
            directories = expand(patterns)
            if not directories:
                print(f"warning: arm {name!r} matched no directories", file=sys.stderr)
            summaries.append(summarize_arm(name, directories))
        if args.csv:
            rows = pass_rate_rows(
                summaries, benchmark=args.benchmark, solver=args.solver, advisor=args.advisor
            )
            write_csv(args.csv, PASS_RATE_COLUMNS, rows)
        if args.interventions_csv:
            write_csv(
                args.interventions_csv,
                INTERVENTION_COLUMNS,
                intervention_rows(summaries, benchmark=args.benchmark),
            )
    except OSError as error:
        # An attempt file that exists but cannot be read would otherwise miscount the arm.
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summaries, indent=2) if args.json else render_table(summaries))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ifm-advisor", description="Audit and summarize advised rollouts."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_audit(subparsers)
    _add_summarize(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "audit":
        return _run_audit(args)
    if args.command == "summarize":
        return _run_summarize(args)
    raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
