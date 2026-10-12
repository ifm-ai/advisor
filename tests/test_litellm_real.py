"""The hook composed onto the real litellm module, with a stubbed transport and no network."""

import json

import pytest

litellm = pytest.importorskip("litellm")

from ifm_advisor import (  # noqa: E402
    Advisor,
    AdvisorConfig,
    EndpointConfig,
    TaskMaterial,
    litellm_hook,
)
from ifm_advisor.cues import (  # noqa: E402
    SELF_CHECK_PREAMBLE,
    compile_self_check_instruction,
)

FINISH_TOOL = "complete"
ADVISOR_BASE = "https://advisor.invalid/v1"


@pytest.fixture
def real_litellm(monkeypatch):
    """Undo the hook on the real module after each test (monkeypatch restores acompletion)."""

    try:
        yield litellm
    finally:
        if hasattr(litellm, litellm_hook.MARKER):
            delattr(litellm, litellm_hook.MARKER)


def _tool_call_response(name: str, content: str = "") -> dict:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": "call_0",
                            "type": "function",
                            "function": {"name": name, "arguments": "{}"},
                        }
                    ],
                },
            }
        ]
    }


def _decision_response() -> dict:
    payload = {
        "intervene": True,
        "confidence": 0.9,
        "reason_code": "INSUFFICIENT_END_TO_END_VALIDATION",
        "action": "RUN_BROADER_VALIDATION",
        "plan": "",
        "hint": "",
        "private_diagnosis": "PRIVATE-MARKER",
    }
    return {"choices": [{"message": {"role": "assistant", "content": json.dumps(payload)}}]}


def _task_dir(tmp_path):
    root = tmp_path / "session-window-debug"
    (root / "tests").mkdir(parents=True)
    (root / "solution").mkdir()
    (root / "cheat").mkdir()
    (root / "instruction.md").write_text("Fix the session window processor.\n")
    (root / "README.md").write_text("## Solution explanation\nFive fixes.\n")
    (root / "tests" / "test_outputs.py").write_text("HIDDEN_ASSERTION_MARKER = 1\n")
    (root / "solution" / "solve.sh").write_text("REFERENCE_MARKER\n")
    (root / "cheat" / "solve.sh").write_text("CHEAT_MARKER\n")
    return root


def test_install_and_uninstall_on_the_real_module(real_litellm, monkeypatch):
    async def transport(*args, **kwargs):
        return _tool_call_response(FINISH_TOOL)

    monkeypatch.setattr(real_litellm, "acompletion", transport)

    assert litellm_hook.install() is True
    advised = real_litellm.acompletion
    assert litellm_hook.install() is False
    assert real_litellm.acompletion is advised
    assert litellm_hook.uninstall() is True
    assert real_litellm.acompletion is transport


async def test_real_module_is_inert_without_an_advised_block(real_litellm, monkeypatch):
    calls = []

    async def transport(*args, **kwargs):
        calls.append(kwargs)
        return _tool_call_response(FINISH_TOOL)

    monkeypatch.setattr(real_litellm, "acompletion", transport)
    litellm_hook.install()

    response = await real_litellm.acompletion(
        model="openai/solver-model", messages=[{"role": "user", "content": "task"}]
    )

    assert response["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == FINISH_TOOL
    assert len(calls) == 1


async def test_real_module_reviews_and_revises_a_finish_call(real_litellm, monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_REAL_ADVISOR_KEY", "advisor-key")
    seen = []

    async def transport(*args, **kwargs):
        seen.append(kwargs)
        if kwargs.get("api_base") == ADVISOR_BASE:
            return _decision_response()
        if SELF_CHECK_PREAMBLE in kwargs["messages"][-1]["content"]:
            return _tool_call_response("bash", "Running the full suite.")
        return _tool_call_response(FINISH_TOOL, "Done.")

    monkeypatch.setattr(real_litellm, "acompletion", transport)
    litellm_hook.install()
    config = AdvisorConfig(
        endpoint=EndpointConfig(
            model="advisor-model", api_base=ADVISOR_BASE, api_key_env="TEST_REAL_ADVISOR_KEY"
        )
    )
    # The default backend: the advisor's own call goes through litellm and is bypassed.
    advisor = Advisor(
        config,
        TaskMaterial.from_task_dir(_task_dir(tmp_path)),
        finish_tool=FINISH_TOOL,
        trace_dir=tmp_path / "attempt-real" / "advisor",
    )
    messages = [{"role": "user", "content": "Fix the session window processor."}]

    with litellm_hook.advised(advisor):
        response = await real_litellm.acompletion(
            model="openai/solver-model",
            messages=messages,
            tools=[{"type": "function", "function": {"name": "bash"}}],
        )

    assert response["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "bash"
    assert len(seen) == 3
    assert (advisor.finish_reviews, advisor.interventions) == (1, 1)
    assert messages == [{"role": "user", "content": "Fix the session window processor."}]

    advisor_request = json.dumps(seen[1]["messages"])
    assert "HIDDEN_ASSERTION_MARKER" in advisor_request
    assert "REFERENCE_MARKER" in advisor_request
    assert "CHEAT_MARKER" not in advisor_request

    # The revision is the public conversation plus exactly the compiled sentence, byte for byte:
    # a stronger claim than the absence of markers, because no other text can be present.
    assert seen[2]["messages"] == [
        {"role": "user", "content": "Fix the session window processor."},
        {"role": "user", "content": compile_self_check_instruction("RUN_BROADER_VALIDATION")},
    ]
    revision_request = json.dumps(seen[2]["messages"]).lower()
    for word in (
        "hidden_assertion_marker",
        "reference_marker",
        "private-marker",
        "advisor",
        "review",
    ):
        assert word not in revision_request

    advisor_dir = tmp_path / "attempt-real" / "advisor"
    trace = [
        json.loads(line) for line in (advisor_dir / "private_trace.jsonl").read_text().splitlines()
    ]
    assert len(trace) == 1 and trace[0]["intervened"] is True
    assert trace[0]["decision"]["private_diagnosis"] == "PRIVATE-MARKER"
    summary = json.loads((advisor_dir / "summary.json").read_text())
    assert summary["interventions"] == 1
    assert "PRIVATE-MARKER" not in json.dumps(summary)
    manifest = json.loads((advisor_dir / "task_material_manifest.json").read_text())
    assert {entry["relative_path"] for entry in manifest["files"]} == {
        "README.md",
        "solution/solve.sh",
        "tests/test_outputs.py",
    }
    assert "REFERENCE_MARKER" not in json.dumps(manifest)


async def test_solver_errors_pass_through_the_hook_unchanged(real_litellm, monkeypatch, tmp_path):
    async def bad_request(*args, **kwargs):
        raise litellm.BadRequestError(message="bad", model="solver-model", llm_provider="openai")

    monkeypatch.setattr(real_litellm, "acompletion", bad_request)
    litellm_hook.install()
    config = AdvisorConfig(endpoint=EndpointConfig(model="m", api_base=ADVISOR_BASE))
    monkeypatch.setenv("ADVISOR_API_KEY", "k")
    advisor = Advisor(
        config, TaskMaterial.from_task_dir(_task_dir(tmp_path)), finish_tool=FINISH_TOOL
    )

    with pytest.raises(litellm.BadRequestError), litellm_hook.advised(advisor):
        await real_litellm.acompletion(
            model="openai/solver-model", messages=[{"role": "user", "content": "x"}]
        )
