"""The published results: the raw counts add up, and every page that shows them agrees."""

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COUNTS = ROOT / "results" / "advisor_rescue.csv"
COLUMNS = ("category", "tasks", "solved_without_advisor", "solved_by_advisor")
PAGES = (ROOT / "README.md", ROOT / "docs" / "index.md")


def _rows() -> list[dict[str, int | str]]:
    with COUNTS.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        assert tuple(reader.fieldnames or ()) == COLUMNS
        return [
            {name: value if name == "category" else int(value) for name, value in row.items()}
            for row in reader
        ]


def _table_row(row: dict[str, int | str]) -> str:
    """The Markdown table row the pages must show for *row*, derived only from the counts."""

    tasks, without, by = (int(row[name]) for name in COLUMNS[1:])
    solved = without + by
    before, after = 100 * without / tasks, 100 * solved / tasks
    cells = [
        str(row["category"]),
        f"{tasks:,}",
        f"{without:,} ({before:.1f}%)",
        f"{by:,}",
        f"{solved:,} ({after:.1f}%)",
        f"+{after - before:.1f} pp",
    ]
    if row["category"] == "Overall":
        cells = [f"**{cell}**" for cell in cells]
    return "| " + " | ".join(cells) + " |"


def test_the_counts_add_up():
    rows = _rows()
    categories, overall = rows[:-1], rows[-1]

    assert overall["category"] == "Overall"
    for column in COLUMNS[1:]:
        assert overall[column] == sum(int(row[column]) for row in categories), column
    for row in rows:
        unsolved = int(row["tasks"]) - int(row["solved_without_advisor"])
        assert 0 <= int(row["solved_by_advisor"]) <= unsolved, row["category"]


def test_the_pages_show_exactly_the_counts():
    for page in PAGES:
        text = page.read_text(encoding="utf-8")
        for row in _rows():
            assert _table_row(row) in text, f"{page.name}: {row['category']}"
        assert "advisor_comparison.svg" in text, page.name
