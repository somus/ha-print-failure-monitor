"""Question merging and the per-frame verdict."""

import pytest

from custom_components.print_failure_monitor.questions import BUILTIN, OPTIONAL, VISIBLE, build_questions, verdict


def test_defaults():
    questions, failures = build_questions({})
    assert questions == BUILTIN
    assert failures == ("spaghetti", "detached", "nozzle_blob")


def test_override_disable_and_add():
    questions, failures = build_questions(
        {
            "nozzle_blob": False,
            "spaghetti": {"no_means": "Purge waste near the chute."},
            "layer_shift": {"instructions": "Shifted?", "yes_means": "Offset.", "no_means": "Straight."},
        }
    )
    assert failures == ("spaghetti", "detached", "layer_shift")
    assert questions["spaghetti"]["no_means"] == "Purge waste near the chute."
    assert questions["spaghetti"]["instructions"] == BUILTIN["spaghetti"]["instructions"]
    assert "nozzle_blob" not in questions


def test_optional_questions():
    questions, failures = build_questions({"warping": True})
    assert failures == ("spaghetti", "detached", "nozzle_blob", "warping")
    assert questions["warping"] == OPTIONAL["warping"]
    questions, _ = build_questions({"warping": {"no_means": "Flat edges."}})
    assert questions["warping"]["instructions"] == OPTIONAL["warping"]["instructions"]
    assert questions["warping"]["no_means"] == "Flat edges."
    assert build_questions({"warping": False})[1] == ("spaghetti", "detached", "nozzle_blob")  # off already


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({VISIBLE: False}, "can't be disabled"),
        ({"stringing": {"instructions": "Strings?"}}, "needs instructions"),
        ({"stringing": True}, "isn't an optional question"),
        ({"spaghetti": False, "detached": False, "nozzle_blob": False}, "at least one"),
        ({"unknown": False}, "unknown question"),
    ],
)
def test_invalid(overrides, message):
    with pytest.raises(ValueError, match=message):
        build_questions(overrides)


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ({VISIBLE: 0.9, "spaghetti": 0.8, "detached": 0.1}, True),
        ({VISIBLE: 0.9, "spaghetti": 0.1, "detached": 0.2}, False),
        ({VISIBLE: 0.9, "spaghetti": 0.5, "detached": 0.1}, None),  # between thresholds
        ({VISIBLE: 0.5, "spaghetti": 0.99, "detached": 0.99}, None),  # plate not visible
    ],
)
def test_verdict(answers, expected):
    assert verdict(answers, 0.7, 0.3) is expected
