import contextlib
import json
import os
import stat
import subprocess
import sys

import pytest
from helpers import FINISH_TOOL, FakeLiteLLM, ScriptedAdvisor, dict_response, finishes

from ifm_advisor.advisor import Advisor
from ifm_advisor.audit import attempt as attempt_module
from ifm_advisor.audit.attempt import (
    locate_trajectory,
    read_privileged_layout,
    read_task_name,
    read_trajectory,
    read_verification,
)
from ifm_advisor.audit.judges import COHERENCE_SCHEMA, LEAKAGE_SCHEMA, validate_answer
from ifm_advisor.audit.report import MAX_PRIVILEGED_CHARS, run_audit
from ifm_advisor.cli import main
from ifm_advisor.config import EndpointConfig
from ifm_advisor.material import PrivilegedFile, TaskLayout, TaskMaterial

JUDGE = EndpointConfig(
    model="IFM/K2-Horizon-375B-A23B",
    api_base="https://judge.example/v1",
    api_key_env="JUDGE_API_KEY",
)


VALID_COHERENCE = {"coherent": True, "unsupported_direction_changes": [], "notes": ""}
VALID_LEAKAGE = {
    "mentions_advisor": False,
    "suspicious_privileged_overlap": False,
    "leaked_items": [],
    "notes": "",
}


def _trajectory(attempt, content: str):
    (attempt / "agent").mkdir(parents=True, exist_ok=True)
    path = attempt / "agent" / "trajectory.json"
    path.write_text(json.dumps({"steps": [{"source": "agent", "message": content}]}))
    return path


def _result(attempt, payload_text: str):
    attempt.mkdir(parents=True, exist_ok=True)
    (attempt / "result.json").write_text(payload_text)


# --- readers ----------------------------------------------------------------------------------


def test_the_atif_trajectory_is_preferred(tmp_path):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")
    (attempt / "agent" / "mini-swe-agent.trajectory.json").write_text("{}")

    assert locate_trajectory(attempt).name == "trajectory.json"


def test_a_missing_trajectory_raises(tmp_path):
    attempt = tmp_path / "attempt"
    attempt.mkdir()

    with pytest.raises(FileNotFoundError, match="no Harbor trajectory"):
        locate_trajectory(attempt)


def test_long_trajectories_keep_both_ends(tmp_path):
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"text": "A" * 300 + "B" * 300}))

    text = read_trajectory(path).judge_text(200)

    assert "characters omitted from the middle" in text
    assert text.lstrip().startswith("{") and text.rstrip().endswith("}")


def test_reward_is_read_from_harbor_verifier_result(tmp_path):
    attempt = tmp_path / "attempt"
    _result(
        attempt, json.dumps({"task_name": "toy", "verifier_result": {"rewards": {"reward": 1.0}}})
    )

    assert read_verification(attempt) == {
        "result_json_present": True,
        "reward": 1.0,
        "passed": True,
    }
    assert read_task_name(attempt) == "toy"


def test_the_first_numeric_reward_is_used_when_the_default_key_is_missing(tmp_path):
    attempt = tmp_path / "attempt"
    _result(attempt, json.dumps({"verifier_result": {"rewards": {"score": 0.5}}}))

    assert read_verification(attempt)["reward"] == 0.5
    assert read_verification(attempt)["passed"] is False


def test_empty_rewards_count_as_zero(tmp_path):
    attempt = tmp_path / "attempt"
    _result(attempt, json.dumps({"verifier_result": {"rewards": None}}))

    assert read_verification(attempt)["reward"] == 0.0


def test_non_finite_reward_is_unverified(tmp_path):
    attempt = tmp_path / "attempt"
    _result(attempt, '{"verifier_result": {"rewards": {"reward": NaN}}}')

    verification = read_verification(attempt)
    assert verification["reward"] is None
    assert verification["passed"] is None


def test_reward_txt_is_the_fallback(tmp_path):
    attempt = tmp_path / "attempt"
    (attempt / "verifier").mkdir(parents=True)
    (attempt / "verifier" / "reward.txt").write_text("0\n")

    assert read_verification(attempt) == {
        "result_json_present": False,
        "reward": 0.0,
        "passed": False,
    }


def test_reward_txt_is_used_when_result_json_has_no_verifier_result(tmp_path):
    attempt = tmp_path / "attempt"
    _result(attempt, json.dumps({"task_name": "toy", "verifier_result": None}))
    (attempt / "verifier").mkdir()
    (attempt / "verifier" / "reward.txt").write_text("1\n")

    assert read_verification(attempt) == {
        "result_json_present": True,
        "reward": 1.0,
        "passed": True,
    }


def test_no_verifier_output_is_unverified(tmp_path):
    attempt = tmp_path / "attempt"
    attempt.mkdir()

    assert read_verification(attempt) == {
        "result_json_present": False,
        "reward": None,
        "passed": None,
    }
    assert read_task_name(attempt) == ""


# --- run_audit with fake judges ---------------------------------------------------------------


def _judges(coherence: dict, leakage: dict) -> FakeLiteLLM:
    async def route(*_args, **kwargs):
        system = kwargs["messages"][0]["content"]
        payload = coherence if "internal coherence" in system else leakage
        return dict_response(content=json.dumps(payload))

    return FakeLiteLLM([route, route])


async def test_both_judges_pass_a_clean_trajectory(tmp_path, task_dir):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "I read DESIGN.md, traced the merge path, and ran the suite.")
    fake = _judges(
        {"coherent": True, "unsupported_direction_changes": [], "notes": ""},
        {
            "mentions_advisor": False,
            "suspicious_privileged_overlap": False,
            "leaked_items": [],
            "notes": "",
        },
    )

    audit = await run_audit(
        attempt, task_dir, judge_endpoint=JUDGE, judge_api_key="k", litellm_module=fake
    )

    assert audit["verdict"] == "pass"
    coherence_input, leakage_input = sorted(
        (kwargs["messages"][1]["content"] for _, kwargs in fake.calls), key=len
    )
    # The coherence judge never sees privileged material; the leakage judge does.
    assert "REFERENCE_GC_IMPLEMENTATION" not in coherence_input
    assert "REFERENCE_GC_IMPLEMENTATION" in leakage_input


async def test_judge_request_prefixes_a_slashed_model_id(tmp_path, task_dir):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")
    fake = _judges(VALID_COHERENCE, VALID_LEAKAGE)

    await run_audit(attempt, task_dir, judge_endpoint=JUDGE, judge_api_key="k", litellm_module=fake)

    assert {kwargs["model"] for _, kwargs in fake.calls} == {"openai/IFM/K2-Horizon-375B-A23B"}


async def test_a_judge_failure_is_inconclusive(tmp_path, task_dir):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")
    fake = FakeLiteLLM([RuntimeError("judge down"), RuntimeError("judge down")])

    audit = await run_audit(
        attempt, task_dir, judge_endpoint=JUDGE, judge_api_key="k", litellm_module=fake
    )

    assert audit["verdict"] == "inconclusive"
    assert any("judge failed" in issue for issue in audit["issues"])


# --- the command ------------------------------------------------------------------------------


def _audit_args(attempt, task_dir, output, *extra):
    return [
        "audit",
        "--attempt-dir",
        str(attempt),
        "--task-dir",
        str(task_dir),
        "--output",
        str(output),
        *extra,
    ]


def test_cli_passes_a_clean_trajectory_without_a_judge(tmp_path, task_dir):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "I read DESIGN.md, traced the merge path, and ran the suite.")
    _result(attempt, json.dumps({"verifier_result": {"rewards": {"reward": 1.0}}}))
    output = tmp_path / "audit.json"

    assert main(_audit_args(attempt, task_dir, output, "--no-judge")) == 0

    payload = json.loads(output.read_text())
    assert payload["verdict"] == "pass_mechanical_only"
    assert payload["mentions_advisor"] is False
    assert payload["suspicious_privileged_overlap"] is False
    assert payload["verification"] == {"result_json_present": True, "reward": 1.0, "passed": True}


def test_cli_fails_a_leaky_trajectory_and_preserves_the_original(tmp_path, task_dir):
    attempt = tmp_path / "attempt"
    stolen = "def test_unfired_survives_gc_and_emits_before_reclaim(processor, clock):"
    (task_dir / "tests" / "test_outputs.py").write_text(f"{stolen}\n    assert True\n")
    path = _trajectory(attempt, f"The advisor pointed at the hidden tests:\n{stolen}")
    before = path.read_bytes()
    output = tmp_path / "audit.json"

    assert main(_audit_args(attempt, task_dir, output, "--no-judge")) == 1

    payload = json.loads(output.read_text())
    assert payload["verdict"] == "fail"
    assert payload["mechanical"]["leak_pattern_hits"] == ["advisor", "hidden test"]
    assert len(payload["mechanical"]["verbatim_privileged_lines"]) == 1
    assert path.read_bytes() == before  # the audit never rewrites the trajectory


def test_cli_is_inconclusive_without_a_judge_endpoint(tmp_path, task_dir):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean transcript")
    output = tmp_path / "audit.json"

    code = main(_audit_args(attempt, task_dir, output, "--judge-model", "", "--judge-base-url", ""))

    payload = json.loads(output.read_text())
    assert code == 1
    assert payload["verdict"] == "inconclusive"
    assert any("judge endpoint not configured" in issue for issue in payload["issues"])


def test_cli_audits_an_explicit_message_list(tmp_path, task_dir):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    messages = tmp_path / "messages.json"
    messages.write_text(json.dumps([{"role": "assistant", "content": "the reviewer said so"}]))
    output = tmp_path / "audit.json"

    assert (
        main(_audit_args(attempt, task_dir, output, "--trajectory", str(messages), "--no-judge"))
        == 1
    )
    assert json.loads(output.read_text())["mechanical"]["leak_pattern_hits"] == ["reviewer"]


def test_cli_rejects_a_missing_attempt_directory(tmp_path, task_dir, capsys):
    code = main(_audit_args(tmp_path / "missing", task_dir, tmp_path / "audit.json", "--no-judge"))

    assert code == 2
    assert "not a directory" in capsys.readouterr().err
    assert not (tmp_path / "audit.json").exists()


def test_cli_reports_its_version(capsys):
    with pytest.raises(SystemExit) as raised:
        main(["--version"])

    assert raised.value.code == 0
    assert "ifm-advisor 0.1.0" in capsys.readouterr().out


# --- fail-closed behavior ---------------------------------------------------------------------


def test_mechanical_checks_scan_the_whole_trajectory(tmp_path, task_dir):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "x" * 150_000 + " the advisor said so " + "x" * 150_000)
    output = tmp_path / "audit.json"

    assert main(_audit_args(attempt, task_dir, output, "--no-judge")) == 1

    payload = json.loads(output.read_text())
    assert payload["verdict"] == "fail"
    assert "advisor" in payload["mechanical"]["leak_pattern_hits"]


async def test_judge_input_truncation_is_recorded(tmp_path, task_dir):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "x" * 150_000 + "y" * 150_000)
    fake = _judges(VALID_COHERENCE, VALID_LEAKAGE)

    audit = await run_audit(
        attempt, task_dir, judge_endpoint=JUDGE, judge_api_key="k", litellm_module=fake
    )

    assert audit["judge_input_truncated"] is True
    assert audit["verdict"] == "pass"


async def test_a_malformed_judge_answer_is_a_judge_failure(tmp_path, task_dir):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")
    fake = _judges(VALID_COHERENCE, {})

    audit = await run_audit(
        attempt, task_dir, judge_endpoint=JUDGE, judge_api_key="k", litellm_module=fake
    )

    assert audit["verdict"] == "inconclusive"
    assert any("leakage judge failed" in issue for issue in audit["issues"])


async def test_a_leak_fails_even_when_a_judge_is_down(tmp_path, task_dir):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "the advisor said so")

    async def coherence_down(*_args, **kwargs):
        if "internal coherence" in kwargs["messages"][0]["content"]:
            raise RuntimeError("judge down")
        return dict_response(content=json.dumps(VALID_LEAKAGE))

    fake = FakeLiteLLM([coherence_down, coherence_down])

    audit = await run_audit(
        attempt, task_dir, judge_endpoint=JUDGE, judge_api_key="k", litellm_module=fake
    )

    assert audit["verdict"] == "fail"


async def test_a_leak_fails_without_a_judge_endpoint(tmp_path, task_dir):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "the advisor said so")

    audit = await run_audit(attempt, task_dir)

    assert audit["verdict"] == "fail"
    assert any("judge endpoint not configured" in issue for issue in audit["issues"])


def test_a_symlinked_trajectory_is_not_followed(tmp_path, task_dir):
    attempt = tmp_path / "attempt"
    (attempt / "agent").mkdir(parents=True)
    secret = tmp_path / "secret.json"
    secret.write_text('{"x": "HOST-SECRET"}')
    (attempt / "agent" / "trajectory.json").symlink_to(secret)

    with pytest.raises(FileNotFoundError):
        locate_trajectory(attempt)
    assert main(_audit_args(attempt, task_dir, tmp_path / "audit.json", "--no-judge")) == 2


def test_a_symlinked_result_json_is_ignored(tmp_path):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"verifier_result": {"rewards": {"reward": 1.0}}}))
    (attempt / "result.json").symlink_to(outside)

    assert read_verification(attempt) == {
        "result_json_present": False,
        "reward": None,
        "passed": None,
    }
    assert read_task_name(attempt) == ""


def test_output_may_not_overwrite_the_trajectory(tmp_path, task_dir, capsys):
    attempt = tmp_path / "attempt"
    path = _trajectory(attempt, "clean")
    before = path.read_bytes()

    assert main(_audit_args(attempt, task_dir, path, "--no-judge")) == 2

    assert path.read_bytes() == before
    assert "would overwrite the audited trajectory" in capsys.readouterr().err


def test_a_non_bool_flag_fails_validation():
    with pytest.raises(ValueError, match="mentions_advisor"):
        validate_answer({**VALID_LEAKAGE, "mentions_advisor": "maybe"}, LEAKAGE_SCHEMA)


def test_a_missing_key_fails_validation():
    with pytest.raises(ValueError, match="coherent"):
        validate_answer({"unsupported_direction_changes": []}, COHERENCE_SCHEMA)


# --- escaped and encoded copies ---------------------------------------------------------------

QUOTED_LINE = 'assert summarize_sessions(events) == "3 sessions, longest 42 minutes"'


def _quoted_privileged_test(task_dir):
    (task_dir / "tests" / "test_outputs.py").write_text(
        f"def test_summary(events):\n    {QUOTED_LINE}\n"
    )


async def test_a_copied_line_with_quotes_is_a_verbatim_hit(tmp_path, task_dir):
    _quoted_privileged_test(task_dir)
    attempt = tmp_path / "attempt"
    _trajectory(attempt, f"I will add this check:\n{QUOTED_LINE}\nDone.")

    audit = await run_audit(attempt, task_dir, no_judge=True)

    assert len(audit["mechanical"]["verbatim_privileged_lines"]) == 1
    assert audit["mechanical"]["leak_pattern_hits"] == []


async def test_a_line_copied_inside_tool_call_arguments_is_a_verbatim_hit(tmp_path, task_dir):
    _quoted_privileged_test(task_dir)
    attempt = tmp_path / "attempt"
    (attempt / "agent").mkdir(parents=True)
    command = f"cat >> test_summary.py <<'EOF'\n{QUOTED_LINE}\nEOF"
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "bash", "arguments": json.dumps({"command": command})},
                }
            ],
        }
    ]
    (attempt / "agent" / "trajectory.json").write_text(json.dumps(messages))

    audit = await run_audit(attempt, task_dir, no_judge=True)

    assert len(audit["mechanical"]["verbatim_privileged_lines"]) == 1


def test_cli_fails_a_copied_line_with_quotes_without_a_judge(tmp_path, task_dir):
    _quoted_privileged_test(task_dir)
    attempt = tmp_path / "attempt"
    _trajectory(attempt, f"I will add this check:\n{QUOTED_LINE}\nDone.")
    output = tmp_path / "audit.json"

    assert main(_audit_args(attempt, task_dir, output, "--no-judge")) == 1

    payload = json.loads(output.read_text())
    assert payload["verdict"] == "fail"
    assert payload["suspicious_privileged_overlap"] is True


def test_scan_text_appends_decoded_string_values(tmp_path):
    path = tmp_path / "trajectory.json"
    code = 'pattern = re.compile(r"^\\d{4}$")\tdone'
    inner = json.dumps({"code": code, "deeper": json.dumps(['nested "value"'])})
    path.write_text(json.dumps({"steps": [{"arguments": inner, "count": 3}]}))

    text = read_trajectory(path).scan_text()

    assert text.startswith(read_trajectory(path).judge_text(None))
    lines = text.splitlines()
    assert inner in lines
    assert code in text
    assert 'nested "value"' in lines


def test_scan_text_of_a_non_json_file_is_the_raw_text(tmp_path):
    path = tmp_path / "trajectory.json"
    path.write_text('not json: "quoted" \\ text\n')

    assert read_trajectory(path).scan_text() == 'not json: "quoted" \\ text\n'


# --- the privileged set the audit checks ------------------------------------------------------

REFERENCE_LINE = "def compute_session_windows_with_gap_boundary(events, gap_seconds):"


def _custom_task(tmp_path):
    task = tmp_path / "custom-task"
    (task / "reference").mkdir(parents=True)
    (task / "instruction.md").write_text("Compute session windows.\n")
    (task / "reference" / "impl.py").write_text(f"{REFERENCE_LINE}\n    return []\n")
    return task


async def test_the_audit_uses_the_layout_in_the_advisors_manifest(tmp_path, advisor_config):
    task = _custom_task(tmp_path)
    attempt = tmp_path / "attempt"
    layout = TaskLayout(privileged_roots=("reference",))
    Advisor(
        advisor_config,
        TaskMaterial.from_task_dir(task, layout),
        finish_tool=FINISH_TOOL,
        trace_dir=attempt / "advisor",
        complete=ScriptedAdvisor(),
    )
    _trajectory(attempt, f"Writing the function:\n{REFERENCE_LINE}")

    audit = await run_audit(attempt, task, no_judge=True)

    assert audit["privileged_roots"] == ["reference"]
    assert len(audit["mechanical"]["verbatim_privileged_lines"]) == 1
    assert audit["verdict"] == "fail"


def test_cli_privileged_root_selects_the_layout(tmp_path):
    task = _custom_task(tmp_path)
    attempt = tmp_path / "attempt"
    _trajectory(attempt, f"Writing the function:\n{REFERENCE_LINE}")
    output = tmp_path / "audit.json"

    code = main(_audit_args(attempt, task, output, "--no-judge", "--privileged-root", "reference"))

    payload = json.loads(output.read_text())
    assert code == 1
    assert payload["verdict"] == "fail"
    assert payload["privileged_roots"] == ["reference"]


async def test_privileged_roots_override_the_manifest(tmp_path, task_dir):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")
    (attempt / "advisor").mkdir()
    manifest = TaskMaterial.from_task_dir(task_dir, TaskLayout(privileged_roots=("solution",)))
    (attempt / "advisor" / "task_material_manifest.json").write_text(
        json.dumps(manifest.manifest())
    )

    from_manifest = await run_audit(attempt, task_dir, no_judge=True)
    overridden = await run_audit(attempt, task_dir, no_judge=True, privileged_roots=["tests"])

    assert from_manifest["privileged_roots"] == ["solution"]
    assert overridden["privileged_roots"] == ["tests"]


async def test_without_a_manifest_the_default_layout_is_used(tmp_path, task_dir):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")

    audit = await run_audit(attempt, task_dir, no_judge=True)

    assert audit["privileged_roots"] == ["README.md", "solution", "tests"]
    assert audit["verdict"] == "pass_mechanical_only"


def test_a_symlinked_manifest_is_unusable_input(tmp_path, capsys):
    task = _custom_task(tmp_path)
    attempt = tmp_path / "attempt"
    _trajectory(attempt, f"Writing the function:\n{REFERENCE_LINE}")
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"privileged_roots": ["reference"]}))
    (attempt / "advisor").mkdir()
    (attempt / "advisor" / "task_material_manifest.json").symlink_to(elsewhere)
    output = tmp_path / "audit.json"

    with pytest.raises(ValueError, match="task_material_manifest.json is a symlink"):
        read_privileged_layout(attempt)
    assert main(_audit_args(attempt, task, output, "--no-judge")) == 2
    assert "error:" in capsys.readouterr().err
    assert not output.exists()


def test_cli_rejects_a_malformed_manifest(tmp_path, task_dir, capsys):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")
    (attempt / "advisor").mkdir()
    (attempt / "advisor" / "task_material_manifest.json").write_text(
        json.dumps({"privileged_roots": ["../outside"]})
    )
    output = tmp_path / "audit.json"

    assert main(_audit_args(attempt, task_dir, output, "--no-judge")) == 2
    assert "task_material_manifest.json" in capsys.readouterr().err
    assert not output.exists()


def test_cli_without_privileged_material_is_inconclusive(tmp_path):
    task = tmp_path / "bare-task"
    task.mkdir()
    (task / "instruction.md").write_text("Do it.\n")
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "I read the code and ran the suite.")
    output = tmp_path / "audit.json"

    assert main(_audit_args(attempt, task, output, "--no-judge")) == 1

    payload = json.loads(output.read_text())
    assert payload["verdict"] == "inconclusive"
    assert "no privileged material was loaded from the task directory" in payload["issues"]
    assert payload["suspicious_privileged_overlap"] is None


def test_cli_with_whitespace_only_material_is_inconclusive(tmp_path):
    task = tmp_path / "blank-task"
    (task / "tests").mkdir(parents=True)
    (task / "instruction.md").write_text("Do it.\n")
    (task / "tests" / "test_blank.py").write_text("   \n\n")
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "I read the code and ran the suite.")
    output = tmp_path / "audit.json"

    assert main(_audit_args(attempt, task, output, "--no-judge")) == 1

    payload = json.loads(output.read_text())
    assert payload["verdict"] == "inconclusive"
    assert "no privileged material was loaded from the task directory" in payload["issues"]


async def test_a_finding_without_privileged_material_still_fails(tmp_path):
    task = tmp_path / "bare-task"
    task.mkdir()
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "the advisor said so")

    audit = await run_audit(attempt, task, no_judge=True)

    assert audit["verdict"] == "fail"
    assert "no privileged material was loaded from the task directory" in audit["issues"]


LATE_LINE = "def test_late_boundary_case_is_detected_after_the_cap(processor):"


def _large_privileged_test(task_dir):
    filler = "".join(f"# filler line {index:06d}\n" for index in range(8_000))
    assert len(filler) > MAX_PRIVILEGED_CHARS
    (task_dir / "tests" / "test_outputs.py").write_text(f"{filler}{LATE_LINE}\n    ...\n")


async def test_a_late_line_of_a_large_privileged_file_is_checked(tmp_path, task_dir):
    _large_privileged_test(task_dir)
    attempt = tmp_path / "attempt"
    _trajectory(attempt, f"Adding a test:\n{LATE_LINE}")

    audit = await run_audit(attempt, task_dir, no_judge=True)

    assert len(audit["mechanical"]["verbatim_privileged_lines"]) == 1
    assert audit["verdict"] == "fail"


async def test_only_the_leakage_judge_input_is_capped(tmp_path, task_dir):
    _large_privileged_test(task_dir)
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")
    fake = _judges(VALID_COHERENCE, VALID_LEAKAGE)

    audit = await run_audit(
        attempt, task_dir, judge_endpoint=JUDGE, judge_api_key="k", litellm_module=fake
    )

    leakage_input = next(
        kwargs["messages"][1]["content"]
        for _, kwargs in fake.calls
        if "PRIVILEGED MATERIAL" in kwargs["messages"][1]["content"]
    )
    assert audit["privileged_input_truncated"] is True
    assert "characters of privileged material omitted" in leakage_input
    assert LATE_LINE not in leakage_input
    assert len(leakage_input) < MAX_PRIVILEGED_CHARS + 1_000
    assert audit["verdict"] == "pass"


async def test_small_privileged_material_is_not_truncated(tmp_path, task_dir):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")
    fake = _judges(VALID_COHERENCE, VALID_LEAKAGE)

    audit = await run_audit(
        attempt, task_dir, judge_endpoint=JUDGE, judge_api_key="k", litellm_module=fake
    )

    assert audit["privileged_input_truncated"] is False


def test_scan_text_includes_decoded_dict_keys(tmp_path):
    key = 'x == "3 sessions, longest 42 minutes"'
    path = tmp_path / "trajectory.json"
    path.write_text(json.dumps({"notes": {key: 1}}))

    assert key in read_trajectory(path).scan_text()


def test_scan_text_decodes_json_lines(tmp_path):
    line = 'assert summarize_sessions(events) == "3 sessions, longest 42 minutes"'
    path = tmp_path / "trajectory.jsonl"
    path.write_text(
        json.dumps({"role": "assistant", "content": line})
        + "\n"
        + json.dumps({"role": "user", "content": "ok"})
        + "\n"
    )

    text = read_trajectory(path).scan_text()

    assert line in text and text.startswith(path.read_text())


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any directory")
def test_cli_rejects_an_unreadable_advisor_directory(tmp_path, task_dir, capsys):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")
    (attempt / "advisor").mkdir()
    (attempt / "advisor" / "task_material_manifest.json").write_text("{}")
    output = tmp_path / "audit.json"
    os.chmod(attempt / "advisor", 0)
    try:
        code = main(_audit_args(attempt, task_dir, output, "--no-judge"))
    finally:
        os.chmod(attempt / "advisor", 0o755)

    assert code == 2
    assert "error:" in capsys.readouterr().err
    assert not output.exists()


LEAK = 'assert summarize_sessions(events) == "3 sessions, longest 42 minutes"'


def test_json_lines_records_are_split_on_newlines_only(tmp_path):
    path = tmp_path / "trajectory.jsonl"
    record = {"role": "assistant", "content": f"{LEAK} and more\u0085"}
    other = {"role": "user", "content": "ok"}
    lines = [json.dumps(item, ensure_ascii=False) for item in (record, other)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert f"{LEAK} and more\u0085" in read_trajectory(path).scan_text().split("\n", 1)[1]


def test_scan_text_keeps_the_first_of_duplicate_keys(tmp_path):
    path = tmp_path / "trajectory.json"
    path.write_text('{"content": ' + json.dumps(LEAK) + ', "content": "ok"}')

    assert LEAK in read_trajectory(path).scan_text().split("\n", 1)[1]


def test_scan_text_decodes_a_json_file_with_a_byte_order_mark(tmp_path):
    path = tmp_path / "trajectory.json"
    path.write_text("﻿" + json.dumps({"content": LEAK}), encoding="utf-8")

    assert LEAK in read_trajectory(path).scan_text().split("\n", 1)[1]


def test_scan_text_decodes_json_lines_with_a_byte_order_mark(tmp_path):
    path = tmp_path / "trajectory.jsonl"
    path.write_text("﻿" + json.dumps({"content": LEAK}) + "\n", encoding="utf-8")

    assert LEAK in read_trajectory(path).scan_text().split("\n", 1)[1]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any directory")
def test_cli_rejects_an_unreadable_verifier_directory(tmp_path, task_dir, capsys):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")
    (attempt / "verifier").mkdir()
    (attempt / "verifier" / "reward.txt").write_text("1")
    output = tmp_path / "audit.json"
    os.chmod(attempt / "verifier", 0)
    try:
        code = main(_audit_args(attempt, task_dir, output, "--no-judge"))
    finally:
        os.chmod(attempt / "verifier", 0o755)

    assert code == 2
    assert "error:" in capsys.readouterr().err


def test_a_symlinked_advisor_directory_is_unusable_input(tmp_path):
    attempt = tmp_path / "attempt"
    real = tmp_path / "real"
    real.mkdir()
    (real / "task_material_manifest.json").write_text('{"privileged_roots": ["x"]}')
    attempt.mkdir()
    (attempt / "advisor").symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="advisor is a symlink"):
        read_privileged_layout(attempt)


def test_an_advisor_path_that_is_a_file_is_unusable_input(tmp_path):
    (tmp_path / "advisor").write_text("not a directory")

    with pytest.raises(ValueError, match="advisor is not a directory"):
        read_privileged_layout(tmp_path)


def test_a_manifest_that_is_a_directory_is_unusable_input(tmp_path):
    (tmp_path / "advisor" / "task_material_manifest.json").mkdir(parents=True)

    with pytest.raises(ValueError, match="is not a regular file"):
        read_privileged_layout(tmp_path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
def test_a_manifest_that_is_a_fifo_is_unusable_input_without_blocking(tmp_path):
    (tmp_path / "advisor").mkdir()
    os.mkfifo(tmp_path / "advisor" / "task_material_manifest.json")

    with pytest.raises(ValueError, match="is not a regular file"):
        finishes(lambda: read_privileged_layout(tmp_path))


def test_an_empty_advisor_directory_gives_no_layout(tmp_path):
    (tmp_path / "advisor").mkdir()

    assert read_privileged_layout(tmp_path) is None


HIDDEN_CHECK = "def check_window_boundaries_are_inclusive_on_both_ends(windows):"


def _hidden_layout_attempt(tmp_path):
    """A task whose privileged root is hidden/, an attempt whose real advisor/ records it, and a
    trajectory that copies a line from hidden/check.py."""

    task = tmp_path / "task"
    (task / "hidden").mkdir(parents=True)
    (task / "instruction.md").write_text("Fix the windows.\n")
    (task / "hidden" / "check.py").write_text(f"{HIDDEN_CHECK}\n    ...\n")
    # Material in the default layout too, so that falling back to it would pass the copy.
    (task / "tests").mkdir()
    (task / "tests" / "test_outputs.py").write_text("def test_windows(): ...\n")
    attempt = tmp_path / "attempt"
    _trajectory(attempt, f"Adding a check:\n{HIDDEN_CHECK}")
    (attempt / "advisor").mkdir()
    (attempt / "advisor" / "task_material_manifest.json").write_text(
        json.dumps(
            TaskMaterial.from_task_dir(task, TaskLayout(privileged_roots=("hidden",))).manifest()
        )
    )
    return task, attempt


def test_cli_fails_a_copy_from_the_layout_in_a_real_advisor_directory(tmp_path):
    task, attempt = _hidden_layout_attempt(tmp_path)
    output = tmp_path / "audit.json"

    assert main(_audit_args(attempt, task, output, "--no-judge")) == 1
    payload = json.loads(output.read_text())
    assert payload["privileged_roots"] == ["hidden"]
    assert payload["layout_source"] == "manifest"


def test_cli_refuses_a_symlinked_advisor_directory(tmp_path, capsys):
    task, attempt = _hidden_layout_attempt(tmp_path)
    (attempt / "advisor").rename(tmp_path / "advisor-elsewhere")
    (attempt / "advisor").symlink_to(tmp_path / "advisor-elsewhere", target_is_directory=True)
    output = tmp_path / "audit.json"

    assert main(_audit_args(attempt, task, output, "--no-judge")) == 2
    assert "error:" in capsys.readouterr().err
    assert not output.exists()


def test_privileged_roots_bypass_a_symlinked_advisor_directory(tmp_path):
    task, attempt = _hidden_layout_attempt(tmp_path)
    (attempt / "advisor").rename(tmp_path / "advisor-elsewhere")
    (attempt / "advisor").symlink_to(tmp_path / "advisor-elsewhere", target_is_directory=True)
    output = tmp_path / "audit.json"

    code = main(_audit_args(attempt, task, output, "--no-judge", "--privileged-root", "hidden"))

    assert code == 1
    assert json.loads(output.read_text())["layout_source"] == "flag"


@pytest.mark.parametrize(
    ("manifest_roots", "flag_roots", "source", "roots"),
    [
        (None, None, "default", ["README.md", "solution", "tests"]),
        (["solution"], None, "manifest", ["solution"]),
        (["solution"], ["tests"], "flag", ["tests"]),
    ],
)
async def test_the_layout_source_is_recorded(
    tmp_path, task_dir, manifest_roots, flag_roots, source, roots
):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")
    if manifest_roots:
        layout = TaskLayout(privileged_roots=tuple(manifest_roots))
        (attempt / "advisor").mkdir()
        (attempt / "advisor" / "task_material_manifest.json").write_text(
            json.dumps(TaskMaterial.from_task_dir(task_dir, layout).manifest())
        )

    audit = await run_audit(attempt, task_dir, no_judge=True, privileged_roots=flag_roots)

    assert audit["layout_source"] == source
    assert audit["privileged_roots"] == roots


async def test_a_manifest_without_a_layout_gives_the_default_layout(tmp_path, task_dir):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")
    (attempt / "advisor").mkdir()
    (attempt / "advisor" / "task_material_manifest.json").write_text(
        json.dumps(TaskMaterial.from_texts("Fix it.", {"notes.md": "notes"}).manifest())
    )

    audit = await run_audit(attempt, task_dir, no_judge=True)

    assert audit["layout_source"] == "default"
    assert audit["privileged_roots"] == ["README.md", "solution", "tests"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_cli_rejects_an_unreadable_manifest_file(tmp_path, task_dir, capsys):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")
    (attempt / "advisor").mkdir()
    manifest = attempt / "advisor" / "task_material_manifest.json"
    manifest.write_text("{}")
    os.chmod(manifest, 0)
    try:
        code = main(_audit_args(attempt, task_dir, tmp_path / "audit.json", "--no-judge"))
    finally:
        os.chmod(manifest, 0o644)

    assert code == 2
    assert "error:" in capsys.readouterr().err


# --- privileged material the audit cannot read ------------------------------------------------

needs_non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
HIDDEN_LINE = "def test_unfired_survives_gc_and_emits_before_reclaim(processor, clock):"
SIBLING_LINE = "def test_sibling_window_merges_overlapping_sessions(processor):"


@contextlib.contextmanager
def _unreadable(path):
    """*path* with every permission removed for the duration; its mode is restored after."""

    mode = stat.S_IMODE(path.stat().st_mode)
    path.chmod(0)
    try:
        yield
    finally:
        path.chmod(mode)


@needs_non_root
@pytest.mark.parametrize("copied", [HIDDEN_LINE, SIBLING_LINE], ids=["its-line", "sibling-line"])
def test_cli_refuses_an_unreadable_privileged_file(tmp_path, task_dir, capsys, copied):
    hidden = task_dir / "tests" / "test_hidden.py"
    hidden.write_text(f"{HIDDEN_LINE}\n    ...\n")
    (task_dir / "tests" / "test_sibling.py").write_text(f"{SIBLING_LINE}\n    ...\n")
    attempt = tmp_path / "attempt"
    _trajectory(attempt, f"Adding a test:\n{copied}")
    output = tmp_path / "audit.json"

    with _unreadable(hidden):
        code = main(_audit_args(attempt, task_dir, output, "--no-judge"))

    assert code == 2
    error = capsys.readouterr().err
    assert "error: cannot read privileged material" in error and "tests/test_hidden.py" in error
    assert not output.exists()


@needs_non_root
@pytest.mark.parametrize("directory", ["tests", "tests/hidden"])
def test_cli_refuses_an_unreadable_privileged_directory(tmp_path, task_dir, capsys, directory):
    (task_dir / "tests" / "hidden").mkdir()
    (task_dir / "tests" / "hidden" / "test_hidden.py").write_text(f"{HIDDEN_LINE}\n    ...\n")
    attempt = tmp_path / "attempt"
    _trajectory(attempt, f"Adding a test:\n{HIDDEN_LINE}")
    output = tmp_path / "audit.json"

    with _unreadable(task_dir / directory):
        code = main(_audit_args(attempt, task_dir, output, "--no-judge"))

    assert code == 2
    error = capsys.readouterr().err
    assert "error: cannot read privileged material" in error and directory in error
    assert not output.exists()


async def test_run_audit_refuses_any_unreadable_entry(tmp_path, task_dir, monkeypatch):
    """Runs as root too: the refusal follows the recorded reason, not the file modes."""

    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")
    material = TaskMaterial(
        instruction="Fix it.",
        files=(
            PrivilegedFile("tests/test_outputs.py", 9, "", "assert x\n", False),
            PrivilegedFile("tests/hidden", 0, "", None, False, "unreadable: OSError"),
        ),
    )
    monkeypatch.setattr(TaskMaterial, "from_task_dir", lambda *args, **kwargs: material)

    with pytest.raises(ValueError, match=r"cannot read privileged material in .+: tests/hidden$"):
        await run_audit(attempt, task_dir, no_judge=True)


# --- trajectories the audit cannot parse ------------------------------------------------------

# Quotes, a backslash and the word "tab": JSON escapes all but the last, so only a decoded copy
# reads as written.
PARSER_LINE = 'assert parse_line("a\\tb") == ["a", "b"], "tab split must keep both fields"'
_STEPS = {"steps": [{"source": "agent", "message": f"Adding a check:\n{PARSER_LINE}"}]}
_DOCUMENT = json.dumps(_STEPS, indent=2)
UNPARSABLE_TRAJECTORIES = {
    "truncated": _DOCUMENT[:-6].encode(),
    "concatenated": (_DOCUMENT + _DOCUMENT).encode(),
    "trailing-comma": _DOCUMENT.replace('"\n    }', '",\n    }').encode(),
    "empty": b"",
    "blank": b" \n\t\n",
    "deeply-nested": b"[" * 100_000,
    "bad-json-lines": (json.dumps(_STEPS) + "\n" + json.dumps(_STEPS)[:-2] + "\n").encode(),
    "invalid-utf-8": _DOCUMENT.encode().replace(b"Adding", b"Add\xffing"),
}


def _parser_task(task_dir):
    (task_dir / "tests" / "test_outputs.py").write_text(f"def test_split():\n    {PARSER_LINE}\n")


def test_the_complete_trajectory_fails(tmp_path, task_dir):
    """The control for the unparsable cases below: the whole file is caught."""

    _parser_task(task_dir)
    attempt = tmp_path / "attempt"
    (attempt / "agent").mkdir(parents=True)
    (attempt / "agent" / "trajectory.json").write_text(_DOCUMENT)

    assert main(_audit_args(attempt, task_dir, tmp_path / "audit.json", "--no-judge")) == 1


@pytest.mark.parametrize("name", UNPARSABLE_TRAJECTORIES)
def test_cli_refuses_a_trajectory_it_cannot_parse(tmp_path, task_dir, capsys, name):
    _parser_task(task_dir)
    attempt = tmp_path / "attempt"
    (attempt / "agent").mkdir(parents=True)
    (attempt / "agent" / "trajectory.json").write_bytes(UNPARSABLE_TRAJECTORIES[name])
    output = tmp_path / "audit.json"

    assert main(_audit_args(attempt, task_dir, output, "--no-judge")) == 2

    assert "error:" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-be", "utf-32"])
def test_cli_decodes_a_utf16_or_utf32_trajectory(tmp_path, task_dir, encoding):
    attempt = tmp_path / "attempt"
    (attempt / "agent").mkdir(parents=True)
    steps = {"steps": [{"source": "agent", "message": "The advisor said to check the tab split."}]}
    (attempt / "agent" / "trajectory.json").write_text(json.dumps(steps), encoding=encoding)
    output = tmp_path / "audit.json"

    assert main(_audit_args(attempt, task_dir, output, "--no-judge")) == 1

    payload = json.loads(output.read_text())
    assert payload["verdict"] == "fail"
    assert payload["mechanical"]["leak_pattern_hits"] == ["advisor"]


def test_cli_refuses_text_whose_encoding_is_only_guessed(tmp_path, task_dir, capsys):
    # NUL bytes near the start make json.detect_encoding guess UTF-16 for what is UTF-8 text; read
    # that way it is garbage, so the leak below would be missed.
    attempt = tmp_path / "attempt"
    (attempt / "agent").mkdir(parents=True)
    raw = b"A\x00B\x00 The advisor said to check the tab split.\n"
    (attempt / "agent" / "trajectory.json").write_bytes(raw)
    output = tmp_path / "audit.json"

    assert main(_audit_args(attempt, task_dir, output, "--no-judge")) == 2

    assert "no byte order mark" in capsys.readouterr().err
    assert not output.exists()


def test_cli_still_fails_a_plain_text_transcript_with_a_copied_line(tmp_path, task_dir):
    _parser_task(task_dir)
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    transcript = tmp_path / "transcript.txt"
    transcript.write_text(f"$ cat test_split.py\n    {PARSER_LINE}\n$ pytest\n1 passed\n")
    output = tmp_path / "audit.json"

    code = main(
        _audit_args(attempt, task_dir, output, "--trajectory", str(transcript), "--no-judge")
    )

    assert code == 1
    assert len(json.loads(output.read_text())["mechanical"]["verbatim_privileged_lines"]) == 1


def test_valid_json_lines_are_still_decoded(tmp_path, task_dir):
    _parser_task(task_dir)
    attempt = tmp_path / "attempt"
    (attempt / "agent").mkdir(parents=True)
    records = [
        {"role": "user", "content": "Fix it."},
        {"role": "assistant", "content": PARSER_LINE},
    ]
    (attempt / "agent" / "trajectory.json").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n\n"
    )
    output = tmp_path / "audit.json"

    assert main(_audit_args(attempt, task_dir, output, "--no-judge")) == 1
    assert json.loads(output.read_text())["suspicious_privileged_overlap"] is True


def test_a_document_too_deep_to_render_is_unusable(tmp_path, monkeypatch):
    # Python 3.12 parses some documents its indented encoder cannot render without recursing.
    path = tmp_path / "trajectory.json"
    path.write_text(json.dumps({"steps": []}))

    def too_deep(payload):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(attempt_module, "_render", too_deep)

    with pytest.raises(ValueError, match="nested too deeply"):
        read_trajectory(path)


def test_a_utf8_byte_order_mark_is_removed_once(tmp_path):
    path = tmp_path / "trajectory.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"content": LEAK}).encode())

    trajectory = read_trajectory(path)

    assert not trajectory.text.startswith("﻿")
    assert trajectory.rendered is not None and LEAK in trajectory.scan_text()


# --- the command's output, and running it as a module ------------------------------------------


def test_cli_reports_an_output_that_is_a_directory(tmp_path, task_dir, capsys):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")
    output = tmp_path / "audit.json"
    output.mkdir()

    assert main(_audit_args(attempt, task_dir, output, "--no-judge")) == 2
    assert "error:" in capsys.readouterr().err


@needs_non_root
@pytest.mark.parametrize("relative", ["audit.json", "nested/audit.json"])
def test_cli_reports_an_unwritable_output(tmp_path, task_dir, capsys, relative):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")
    locked = tmp_path / "locked"
    locked.mkdir()

    with _unreadable(locked):
        code = main(_audit_args(attempt, task_dir, locked / relative, "--no-judge"))

    assert code == 2
    assert "error:" in capsys.readouterr().err


def test_cli_writes_the_audit_readable_by_its_owner_only(tmp_path, task_dir):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")
    output = tmp_path / "audit.json"
    output.write_text("{}")
    output.chmod(0o644)  # an earlier audit, written before this was fixed

    previous = os.umask(0o022)
    try:
        assert main(_audit_args(attempt, task_dir, output, "--no-judge")) == 0
    finally:
        os.umask(previous)

    assert stat.S_IMODE(output.stat().st_mode) & 0o077 == 0
    assert json.loads(output.read_text())["verdict"] == "pass_mechanical_only"
    assert [path.name for path in tmp_path.iterdir() if path.name.endswith(".tmp")] == []


@pytest.mark.parametrize("module", ["ifm_advisor", "ifm_advisor.cli"])
def test_the_command_runs_as_a_module(tmp_path, module):
    completed = subprocess.run(
        [sys.executable, "-m", module, "--version"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ifm-advisor 0.1.0"


# --- the verifier's files ---------------------------------------------------------------------


@needs_non_root
@pytest.mark.parametrize("relative", ["result.json", "verifier/reward.txt"])
def test_cli_refuses_an_unreadable_verifier_file(tmp_path, task_dir, capsys, relative):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")
    (attempt / "verifier").mkdir()
    (attempt / relative).write_text("1")
    output = tmp_path / "audit.json"

    with _unreadable(attempt / relative):
        code = main(_audit_args(attempt, task_dir, output, "--no-judge"))

    assert code == 2
    assert "error:" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", "\xff"])
def test_an_invalid_result_json_is_unverified(tmp_path, content):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    (attempt / "result.json").write_bytes(content.encode("latin-1"))

    assert read_verification(attempt)["reward"] is None
    assert read_task_name(attempt) == ""


def test_an_invalid_reward_txt_is_unverified(tmp_path):
    attempt = tmp_path / "attempt"
    (attempt / "verifier").mkdir(parents=True)
    (attempt / "verifier" / "reward.txt").write_text("passed\n")

    assert read_verification(attempt)["reward"] is None


# --- leaked items without a flag --------------------------------------------------------------


async def test_leaked_items_without_a_flag_are_a_finding(tmp_path, task_dir):
    attempt = tmp_path / "attempt"
    _trajectory(attempt, "clean")
    leakage = {**VALID_LEAKAGE, "leaked_items": ["the expected error message of the hidden test"]}
    fake = _judges(VALID_COHERENCE, leakage)

    audit = await run_audit(
        attempt, task_dir, judge_endpoint=JUDGE, judge_api_key="k", litellm_module=fake
    )

    assert audit["verdict"] == "fail"
    assert audit["leaked_items"] == leakage["leaked_items"]
    assert any("1 leaked item" in issue for issue in audit["issues"])
