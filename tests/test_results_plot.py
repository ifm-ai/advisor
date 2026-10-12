import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("matplotlib")

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("results_plot", ROOT / "results" / "plot.py")
plot_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plot_module)

HEADER = "benchmark,arm,solver,advisor,tasks,attempts,correct\n"


def test_plot_writes_a_deterministic_svg(tmp_path):
    data = tmp_path / "pass_rates.csv"
    data.write_text(
        HEADER
        + "toy-bench,baseline,K2-Horizon,,10,30,6\n"
        + "toy-bench,advised,K2-Horizon,K2-Horizon,10,30,12\n"
    )
    first, second = tmp_path / "a.svg", tmp_path / "b.svg"

    assert plot_module.main(["--csv", str(data), "--out", str(first)]) == 0
    assert plot_module.main(["--csv", str(data), "--out", str(second)]) == 0

    text = first.read_text()
    assert "<svg" in text and "12/30" in text
    assert text == second.read_text()


@pytest.mark.parametrize(
    "body",
    [
        "benchmark,arm,attempts,correct\ntoy,base,3,1\n",
        HEADER + "toy,base,K2-Horizon,,1,3,4\n",
        HEADER + "toy,base,K2-Horizon,,1,three,1\n",
        HEADER,
    ],
)
def test_bad_csvs_are_rejected(tmp_path, body, capsys):
    data = tmp_path / "pass_rates.csv"
    data.write_text(body)

    assert plot_module.main(["--csv", str(data), "--out", str(tmp_path / "x.svg")]) == 2
    assert "error:" in capsys.readouterr().err


def test_a_row_with_missing_fields_is_a_clean_error(tmp_path, capsys):
    data = tmp_path / "pass_rates.csv"
    data.write_text(HEADER + "toy-bench,baseline\n")

    assert plot_module.main(["--csv", str(data), "--out", str(tmp_path / "x.svg")]) == 2
    assert "pass_rates.csv:2: expected 7 fields" in capsys.readouterr().err
