from types import SimpleNamespace

import pytest
from helpers import bash_response, dict_response, finish_response, object_response

from ifm_advisor.responses import (
    prepend_reasoning_content,
    proposed_submission_summary,
    response_attempts_finish,
    response_reasoning_tokens,
    response_text,
    response_tool_call_names,
)


@pytest.mark.parametrize("dict_shape", [False, True])
def test_finish_tool_call_is_detected_in_both_response_shapes(dict_shape):
    response = finish_response(dict_shape=dict_shape)

    assert response_tool_call_names(response) == ("complete",)
    assert response_attempts_finish(response, "complete") is True


@pytest.mark.parametrize("dict_shape", [False, True])
def test_non_finish_tool_call_is_not_detected(dict_shape):
    response = bash_response(dict_shape=dict_shape)

    assert response_tool_call_names(response) == ("bash",)
    assert response_attempts_finish(response, "complete") is False


@pytest.mark.parametrize("builder", [object_response, dict_response])
def test_prose_mentioning_the_finish_tool_does_not_count(builder):
    """Detection is structural, so substrings can never trigger a review."""

    response = builder(content="I will now call complete to finish the task.")

    assert response_tool_call_names(response) == ()
    assert response_attempts_finish(response, "complete") is False


def test_finish_alongside_other_tool_calls_is_detected():
    response = object_response(tool_calls=(("bash", "{}"), ("complete", "{}")))

    assert response_attempts_finish(response, "complete") is True


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"tool_calls": None}}]},
        {"choices": [{"message": {"tool_calls": [{"function": {}}]}}]},
        object(),
        None,
    ],
)
def test_malformed_responses_are_treated_as_non_finish(response):
    assert response_attempts_finish(response, "complete") is False
    assert response_tool_call_names(response) == ()


def test_empty_finish_tool_name_never_matches():
    assert response_attempts_finish(finish_response(), "") is False


def test_response_text_is_read_from_both_shapes():
    assert response_text(object_response(content="hello")) == "hello"
    assert response_text(dict_response(content="hello")) == "hello"
    assert response_text({"choices": [{"message": {"content": None}}]}) == ""


def test_submission_summary_reports_structure():
    summary = proposed_submission_summary(finish_response(), "complete")

    assert "tool_calls: complete" in summary
    assert "attempts_finish: True" in summary
    assert "Done." in summary


def test_reasoning_tokens_are_read_when_reported():
    assert response_reasoning_tokens(dict_response(reasoning_tokens=412)) == 412
    assert response_reasoning_tokens(dict_response()) is None


def test_prefix_is_prepended_to_dict_reasoning_content():
    response = dict_response(reasoning_content=" and so on.")

    assert prepend_reasoning_content(response, "Hmm.") is True
    assert response["choices"][0]["message"]["reasoning_content"] == "Hmm. and so on."


def test_prefix_is_set_on_an_object_message_without_reasoning():
    response = object_response(content="x")

    assert prepend_reasoning_content(response, "Hmm.") is True
    assert response.choices[0].message.reasoning_content == "Hmm."


def test_a_frozen_message_is_left_alone():
    class Frozen(SimpleNamespace):
        def __setattr__(self, name, value):
            raise AttributeError(name)

    response = SimpleNamespace(choices=[SimpleNamespace(message=Frozen(content="x"))])

    assert prepend_reasoning_content(response, "Hmm.") is False


def test_empty_prefix_changes_nothing():
    response = dict_response(reasoning_content="kept")

    assert prepend_reasoning_content(response, "") is False
    assert response["choices"][0]["message"]["reasoning_content"] == "kept"
