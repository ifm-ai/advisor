"""Plot pass rates from results/pass_rates.csv into assets/pass_rates.svg.

    pip install -e ".[results]"
    python results/plot.py [--csv results/pass_rates.csv] [--out assets/pass_rates.svg]

Pass rates and their 95% Wilson intervals are computed from the raw counts in the CSV; the CSV
never stores a rate. The output is deterministic, so regenerating it does not churn the diff.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from ifm_advisor.summarize import PASS_RATE_COLUMNS, wilson_interval

ROOT = Path(__file__).resolve().parents[1]
# Categorical hues in fixed order (blue, orange, aqua, yellow, magenta), assigned by arm order in
# the CSV. This set passes the dataviz validator on the light surface (adjacent CVD and
# normal-vision separation). Aqua, yellow and magenta sit below 3:1 contrast, so every bar carries
# a visible count label and the legend names each arm.
ARM_COLORS = ("#2A78D6", "#EB6834", "#1BAF7A", "#EDA100", "#E87BA4")
SURFACE = "#FCFCFB"
TEXT_PRIMARY = "#0B0B0B"
TEXT_SECONDARY = "#52514E"
GRID = "#E3E2DE"


def load_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != PASS_RATE_COLUMNS:
            raise ValueError(f"{path} must have exactly the columns {','.join(PASS_RATE_COLUMNS)}")
        rows = []
        for line, row in enumerate(reader, start=2):
            # DictReader fills missing fields with None and files extra ones under the None key.
            if None in row or any(row[column] is None for column in PASS_RATE_COLUMNS):
                raise ValueError(f"{path}:{line}: expected {len(PASS_RATE_COLUMNS)} fields")
            for column in ("tasks", "attempts", "correct"):
                try:
                    row[column] = int(row[column])
                except ValueError as error:
                    raise ValueError(f"{path}:{line}: {column} must be an integer") from error
            if not 0 <= row["correct"] <= row["attempts"]:
                raise ValueError(f"{path}:{line}: correct must be between 0 and attempts")
            rows.append(row)
    if not rows:
        raise ValueError(f"{path} has no rows")
    return rows


def plot(rows: list[dict], out: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch
    from matplotlib.ticker import PercentFormatter

    plt.rcParams["svg.hashsalt"] = "ifm-advisor"
    plt.rcParams["svg.fonttype"] = "none"
    benchmarks = list(dict.fromkeys(row["benchmark"] for row in rows))
    arms = list(dict.fromkeys(row["arm"] for row in rows))
    colors = {arm: ARM_COLORS[index % len(ARM_COLORS)] for index, arm in enumerate(arms)}
    figure, axes = plt.subplots(
        1,
        len(benchmarks),
        figsize=(1.6 + 1.3 * len(arms) * len(benchmarks), 3.6),
        sharey=True,
        squeeze=False,
        facecolor=SURFACE,
    )
    for axis, benchmark in zip(axes[0], benchmarks, strict=True):
        subset = [row for row in rows if row["benchmark"] == benchmark]
        axis.set_facecolor(SURFACE)
        axis.set_axisbelow(True)
        axis.grid(axis="y", color=GRID, linewidth=0.8)
        for position, row in enumerate(subset):
            rate = row["correct"] / row["attempts"] if row["attempts"] else 0.0
            low, high = wilson_interval(row["correct"], row["attempts"])
            axis.bar(position, rate, width=0.4, color=colors[row["arm"]])
            axis.errorbar(
                position,
                rate,
                yerr=[[rate - low], [high - rate]],
                fmt="none",
                ecolor=TEXT_SECONDARY,
                capsize=3,
                linewidth=1,
            )
            axis.text(
                position,
                high + 0.02,
                f"{row['correct']}/{row['attempts']}",
                ha="center",
                va="bottom",
                fontsize=8,
                color=TEXT_PRIMARY,
            )
        axis.set_xticks(
            range(len(subset)), [row["arm"] for row in subset], fontsize=9, color=TEXT_SECONDARY
        )
        axis.set_title(benchmark, fontsize=10, color=TEXT_PRIMARY)
        axis.set_ylim(0, 1.1)
        axis.set_xlim(-0.6, len(subset) - 0.4)
        axis.spines[["top", "right"]].set_visible(False)
        axis.spines[["left", "bottom"]].set_color(GRID)
        axis.tick_params(colors=TEXT_SECONDARY, length=0)
        axis.yaxis.set_major_formatter(PercentFormatter(1.0))
    axes[0][0].set_ylabel("pass rate (95% Wilson interval)", color=TEXT_SECONDARY)
    if len(arms) > 1:
        figure.legend(
            handles=[Patch(color=colors[arm], label=arm) for arm in arms],
            loc="lower center",
            ncol=len(arms),
            frameon=False,
            fontsize=8,
            labelcolor=TEXT_SECONDARY,
        )
        figure.tight_layout(rect=(0, 0.08, 1, 1))
    else:
        figure.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    is_svg = out.suffix.lower() == ".svg"
    figure.savefig(
        out,
        format=out.suffix.lstrip(".") or "svg",
        metadata={"Date": None} if is_svg else None,
        facecolor=SURFACE,
    )
    plt.close(figure)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--csv", type=Path, default=ROOT / "results" / "pass_rates.csv")
    parser.add_argument("--out", type=Path, default=ROOT / "assets" / "pass_rates.svg")
    args = parser.parse_args(argv)
    try:
        rows = load_rows(args.csv)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    plot(rows, args.out)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
