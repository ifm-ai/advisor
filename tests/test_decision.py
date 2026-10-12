import json

import pytest
from helpers import decision_payload

from ifm_advisor.decision import (
    RESPONSE_SCHEMA_TEXT,
    AdvisorDecision,
    AdvisorDecisionError,
    parse_advisor_decision,
    should_intervene,
)

VALID = {
    "intervene": True,
    "confidence": 0.8,
    "reason_code": "PREMATURE_SUBMISSION",
    "action": "RECONSIDER_SUBMISSION",
    "plan": "1. fix the gc path. 2. add regression coverage.",
    "hint": "Wait, I should reassess whether this is really complete.",
    "private_diagnosis": "the gc path was never exercised",
}


def test_valid_decision_parses():
    decision = parse_advisor_decision(json.dumps(VALID))

    assert decision.intervene is True
    assert decision.action == "RECONSIDER_SUBMISSION"
    assert decision.plan == VALID["plan"]
    assert decision.hint == VALID["hint"]
    assert decision.private_diagnosis == "the gc path was never exercised"


def test_fenced_json_is_accepted():
    decision = parse_advisor_decision(f"```json\n{json.dumps(VALID)}\n```")

    assert decision.action == "RECONSIDER_SUBMISSION"


@pytest.mark.parametrize(
    "content",
    [
        "",
        "   ",
        "not json",
        json.dumps({**VALID, "extra_field": 1}),
        json.dumps({key: value for key, value in VALID.items() if key != "action"}),
        json.dumps({key: value for key, value in VALID.items() if key != "plan"}),
        json.dumps({key: value for key, value in VALID.items() if key != "hint"}),
        json.dumps({**VALID, "action": "DO_SOMETHING_ELSE"}),
        json.dumps({**VALID, "reason_code": "MADE_UP"}),
        json.dumps({**VALID, "confidence": 1.5}),
        json.dumps({**VALID, "confidence": "high"}),
        json.dumps({**VALID, "intervene": "yes please"}),
        json.dumps({**VALID, "action": ["CONTINUE"]}),
        json.dumps([VALID]),
    ],
)
def test_invalid_decisions_are_rejected(content):
    with pytest.raises(AdvisorDecisionError):
        parse_advisor_decision(content)


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({**VALID, "private_diagnosis": "SECRET-DIAGNOSIS", "action": "NOT_AN_ACTION"}),
        json.dumps({**VALID, "confidence": "SECRET-DIAGNOSIS"}),
        json.dumps({**VALID, "private_diagnosis": ["SECRET-DIAGNOSIS"]}),
        json.dumps({**VALID, "private_diagnosis": "SECRET-DIAGNOSIS"})[:-1],
        "SECRET-DIAGNOSIS is not json",
    ],
)
def test_a_rejected_decision_never_quotes_the_advisor_output(content):
    """With fail_open=False this error reaches the agent, which may write it into the trajectory."""

    with pytest.raises(AdvisorDecisionError) as raised:
        parse_advisor_decision(content)

    assert "SECRET-DIAGNOSIS" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert str(raised.value).startswith("advisor response was not a valid decision: ")


def test_a_rejection_names_the_failing_field():
    with pytest.raises(AdvisorDecisionError, match="action"):
        parse_advisor_decision(json.dumps({**VALID, "action": "NOT_AN_ACTION"}))


@pytest.mark.parametrize(
    ("intervene", "action", "expected"),
    [
        (True, "RECONSIDER_SUBMISSION", True),
        (True, "CONFIRM_AND_SUBMIT", True),
        (True, "CONTINUE", False),
        (False, "RECONSIDER_SUBMISSION", False),
    ],
)
def test_intervention_gate(intervene, action, expected):
    decision = AdvisorDecision(
        intervene=intervene,
        confidence=0.9,
        reason_code="OTHER",
        action=action,
        plan="",
        hint="",
        private_diagnosis="",
    )

    assert should_intervene(decision) is expected


def test_schema_text_describes_enums_as_prose_not_lists():
    """A JSON array in the schema invites the model to answer with a list."""

    schema = json.loads(RESPONSE_SCHEMA_TEXT)

    assert schema["action"].startswith("string - exactly one of: CONTINUE | ")
    assert "[" not in schema["reason_code"]


def test_an_unknown_field_is_not_named_in_the_error():
    payload = json.dumps({**decision_payload(), "SECRET-FIELD-NAME": 1})

    with pytest.raises(AdvisorDecisionError) as caught:
        parse_advisor_decision(payload)

    assert "SECRET-FIELD-NAME" not in str(caught.value)
    assert "<unknown field>: Extra inputs are not permitted" in str(caught.value)


def test_a_parse_error_carries_no_chained_exception():
    with pytest.raises(AdvisorDecisionError) as caught:
        parse_advisor_decision(json.dumps({**decision_payload(), "intervene": "SECRET-VALUE"}))

    assert "SECRET-VALUE" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
