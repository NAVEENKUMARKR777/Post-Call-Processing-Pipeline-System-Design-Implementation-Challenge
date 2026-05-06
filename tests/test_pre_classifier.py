"""Pre-classifier tests, driven by the assignment's fixture file.

The fixture tags every transcript with `expected_lane` ∈ {hot, cold,
skip}. That tag is the business signal the README says the system
must respect, and this test file enforces it.

A failure here means we either:
  - mis-routed a hot call (latency penalty, recoverable)
  - mis-routed a cold call as hot (consumes reserved budget unfairly)
  - mis-routed a skip call (wastes LLM tokens — AC8 violation)

Each maps to a distinct severity in `SUBMISSION.md §6`.
"""

import json
from pathlib import Path

import pytest

from src.classifier import Lane, classify_transcript


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_transcripts.json"


@pytest.fixture(scope="module")
def all_transcripts():
    with open(FIXTURE_PATH) as f:
        return json.load(f)["transcripts"]


@pytest.mark.parametrize(
    "scenario",
    [
        "rebook_confirmed",
        "not_interested",
        "callback_requested",
        "demo_booked",
        "already_purchased",
        "short_call_hangup",
        "hinglish_ambiguous",
        "escalation_needed",
    ],
)
def test_classifier_matches_fixture_expected_lane(all_transcripts, scenario):
    """For every fixture scenario, the classifier's lane must match
    the fixture's `expected_lane` tag. This is the contract the
    fixture file is a specification of."""
    fixture = all_transcripts[scenario]
    expected = Lane(fixture["expected_lane"])
    conversation_data = {"transcript": fixture["transcript"]}

    result = classify_transcript(conversation_data)

    assert result.lane == expected, (
        f"{scenario}: got {result.lane.value}, expected {expected.value}; "
        f"reason={result.reason}, matched={result.matched_keywords}"
    )


def test_short_transcript_skips_without_keyword_lookup(all_transcripts):
    """AC8: <4-turn transcripts must skip even if they happen to
    contain hot keywords (which a `wrong number` reply could,
    indirectly). The skip decision is made *before* the keyword
    match for predictability."""
    short = all_transcripts["short_call_hangup"]
    result = classify_transcript({"transcript": short["transcript"]})
    assert result.lane == Lane.SKIP
    assert "short_transcript" in result.reason
    assert result.turn_count == len(short["transcript"])


def test_empty_or_missing_transcript_skips():
    assert classify_transcript(None).lane == Lane.SKIP
    assert classify_transcript({}).lane == Lane.SKIP
    assert classify_transcript({"transcript": []}).lane == Lane.SKIP
    assert classify_transcript({"transcript": [{"role": "agent", "content": "hi"}]}).lane == Lane.SKIP


def test_wrong_number_phrase_routes_to_skip_even_if_long():
    """A long call where the customer says 'wrong number' is still
    not useful for analysis. Hard-skip phrases override the turn-count
    threshold."""
    transcript = [
        {"role": "agent", "content": "Hello, am I speaking with Mr. Sharma?"},
        {"role": "customer", "content": "wrong number"},
        {"role": "agent", "content": "Sorry, sir, my mistake."},
        {"role": "customer", "content": "no problem"},
        {"role": "agent", "content": "Have a good day."},
    ]
    result = classify_transcript({"transcript": transcript})
    assert result.lane == Lane.SKIP
    assert "hard_skip_phrase" in result.reason


def test_hot_lane_requires_both_length_and_keyword():
    """A 4-turn call mentioning 'demo' is COLD: not enough engagement
    to justify reserved-budget consumption. The threshold is part
    of the design — one keyword in passing is not a hot lead."""
    transcript = [
        {"role": "agent", "content": "Quick question — interested in a demo?"},
        {"role": "customer", "content": "Maybe."},
        {"role": "agent", "content": "Okay, I'll send details."},
        {"role": "customer", "content": "Sure."},
    ]
    result = classify_transcript({"transcript": transcript})
    assert result.lane == Lane.COLD


def test_customer_keywords_extend_hot_signal():
    """Per-customer keyword lists (assumption #11) must be additive,
    not replacing. The default set still applies."""
    transcript = [
        {"role": "agent", "content": "Hello, am I speaking with—"},
        {"role": "customer", "content": "yes, hi"},
        {"role": "agent", "content": "Just confirming our quote."},
        {"role": "customer", "content": "fine, send it"},
        {"role": "agent", "content": "Done. Anything else?"},
        {"role": "customer", "content": "send the invoice please"},
    ]
    # Default classifier sees no hot keywords and lands cold.
    base = classify_transcript({"transcript": transcript})
    assert base.lane == Lane.COLD

    # Customer adds 'invoice' as a hot signal → upgraded to hot.
    tuned = classify_transcript(
        {"transcript": transcript},
        customer_keywords=("invoice",),
    )
    assert tuned.lane == Lane.HOT
    assert "invoice" in tuned.matched_keywords


def test_classification_is_deterministic():
    """Same input → same output. Required because we route off this
    in a hot path; non-determinism would make debugging impossible."""
    fixture_transcript = [
        {"role": "agent", "content": "Hello sir, am I speaking with Rajesh?"},
        {"role": "customer", "content": "yes"},
        {"role": "agent", "content": "Calling to schedule your demo."},
        {"role": "customer", "content": "yes thursday works"},
        {"role": "agent", "content": "Confirmed for thursday at 3pm."},
        {"role": "customer", "content": "thanks"},
    ]
    a = classify_transcript({"transcript": fixture_transcript})
    b = classify_transcript({"transcript": fixture_transcript})
    assert a == b
    assert a.lane == Lane.HOT
