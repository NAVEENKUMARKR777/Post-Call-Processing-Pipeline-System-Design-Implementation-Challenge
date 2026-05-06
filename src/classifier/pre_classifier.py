"""Cheap heuristic classifier that routes interactions into one of
three lanes — hot, cold, or skip — at endpoint-receive time.

Why a heuristic instead of an LLM?

Routing is the prerequisite to rate limiting: if every transcript
needs an LLM call to decide which lane it goes in, the rate-limit
problem is unsolvable. The classifier has to be cheap (string
match), deterministic, and run in the request path. Mistakes are
recoverable — the LLM still produces the final disposition for
hot/cold lanes, so a mis-routed call only experiences extra
latency, never a wrong business outcome.

Rules — the deliberate trade-offs:

  skip   < 4 turns OR a hard-skip phrase like "wrong number".

  hot    A *customer turn* contains a commitment keyword (a forward-
         looking confirmation like "thursday works", "confirmed",
         "yes please") or an escalation keyword ("manager",
         "complaint", "supervisor"), AND the call is long enough
         (>= 6 turns) to suggest real engagement, AND the customer
         did not negate the keyword ("already booked", "not
         interested").

  cold   Default. Defers when global utilization > 70%.

Why customer-turn-weighted? The agent always says "I'll schedule
your demo" regardless of whether the customer agrees. The signal
that distinguishes a hot call from a cold one is the *customer's*
response, not the agent's pitch. The fixture file confirms this:
  - rebook_confirmed (HOT): customer says "Haan theek hai, confirmed"
  - already_purchased (COLD): customer says "I already booked it"
  - hinglish_ambiguous (COLD): customer says "main soch raha hoon"
A keyword-match-anywhere classifier hits all three as HOT, which is
why this version weights customer turns and negation modifiers.

Customers can extend the hot keyword set per-customer through
customer_llm_budgets.config — see assumption #11.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional


class Lane(str, enum.Enum):
    HOT = "hot"
    COLD = "cold"
    SKIP = "skip"


@dataclass(frozen=True)
class Classification:
    lane: Lane
    reason: str
    matched_keywords: tuple[str, ...]
    turn_count: int


# Customer-side commitment signals: forward-looking confirmations of
# something the agent has just proposed. These are deliberately tied
# to action verbs and time references; vague affirmations like "okay"
# or "haan" alone are too noisy to classify on.
COMMITMENT_KEYWORDS: tuple[str, ...] = (
    "confirmed",
    "confirm",
    "book the slot",
    "rebook",
    "tomorrow works",
    "thursday works",
    "friday works",
    "monday works",
    "tuesday works",
    "wednesday works",
    "saturday works",
    "sunday works",
    "works for me",
    "let me block my calendar",
    "block my calendar",
    "send me the",
    "send the invite",
    "send a calendar invite",
    "calendar invite",
    "schedule the demo",
    "yes, i want",
    "yes i want",
    "go ahead",
    "let's do",
    "lets do",
    "sounds good",
    "looking forward",
)

# Escalation signals: customer is angry, demands escalation, or
# threatens action. These are HOT for risk-mitigation reasons even
# without a commitment keyword.
ESCALATION_KEYWORDS: tuple[str, ...] = (
    "manager",
    "supervisor",
    "senior",
    "escalate",
    "escalation",
    "file a complaint",
    "complaint",
    "unacceptable",
    "lawyer",
    "legal action",
)

# Phrases that pull the classification toward COLD even if a
# commitment keyword appeared. "Already booked" is the canonical
# example: customer is not committing, they're declining further
# engagement.
NEGATION_PREFIXES: tuple[str, ...] = (
    "already",
    "not interested",
    "don't call",
    "do not call",
    "won't",
    "cannot",
    "can't",
    "stop calling",
)

# Phrases that mean "abandon analysis entirely" — the call has no
# useful content for the LLM to extract.
HARD_SKIP_PHRASES: tuple[str, ...] = (
    "wrong number",
)

MIN_HOT_TURNS = 6
MIN_USEFUL_TURNS = 4


def _extract_transcript(conversation_data: Optional[Mapping[str, Any]]) -> list[dict]:
    if not conversation_data:
        return []
    transcript = conversation_data.get("transcript")
    if not isinstance(transcript, list):
        return []
    return transcript


def _customer_lines(transcript: Iterable[Mapping[str, Any]]) -> list[str]:
    """Lowercased contents of every customer turn. Agents pitch the
    same script to everyone; the customer's reply is the signal."""
    lines: list[str] = []
    for turn in transcript:
        if not isinstance(turn, Mapping):
            continue
        if turn.get("role") != "customer":
            continue
        content = turn.get("content")
        if isinstance(content, str):
            lines.append(content.lower())
    return lines


def _all_lines(transcript: Iterable[Mapping[str, Any]]) -> str:
    parts = []
    for turn in transcript:
        if not isinstance(turn, Mapping):
            continue
        content = turn.get("content")
        if isinstance(content, str):
            parts.append(content.lower())
    return " \n".join(parts)


def _matches_with_negation_check(line: str, keywords: Iterable[str]) -> list[str]:
    """Return keywords that appear in the line *not* prefixed by any
    NEGATION_PREFIXES. The negation prefix has to appear within the
    same line (window of ~30 characters before the keyword) — distant
    negations don't count."""
    found = []
    for kw in keywords:
        idx = line.find(kw)
        if idx == -1:
            continue
        window = line[max(0, idx - 30) : idx]
        if any(neg in window for neg in NEGATION_PREFIXES):
            continue
        found.append(kw)
    return found


def classify_transcript(
    conversation_data: Optional[Mapping[str, Any]],
    *,
    customer_keywords: Optional[Iterable[str]] = None,
) -> Classification:
    """Decide which lane an interaction goes in based on its transcript.

    `customer_keywords` lets a customer's row in customer_llm_budgets
    extend the commitment keyword set without a deploy.
    """
    transcript = _extract_transcript(conversation_data)
    turn_count = len(transcript)

    if turn_count < MIN_USEFUL_TURNS:
        return Classification(
            lane=Lane.SKIP,
            reason=f"short_transcript({turn_count}<{MIN_USEFUL_TURNS})",
            matched_keywords=(),
            turn_count=turn_count,
        )

    flat = _all_lines(transcript)
    for phrase in HARD_SKIP_PHRASES:
        if phrase in flat:
            return Classification(
                lane=Lane.SKIP,
                reason=f"hard_skip_phrase:{phrase}",
                matched_keywords=(phrase,),
                turn_count=turn_count,
            )

    customer_lines = _customer_lines(transcript)
    commitment_kws: tuple[str, ...] = COMMITMENT_KEYWORDS + tuple(customer_keywords or ())

    matched_commitments: list[str] = []
    matched_escalations: list[str] = []
    for line in customer_lines:
        matched_commitments.extend(_matches_with_negation_check(line, commitment_kws))
        matched_escalations.extend(_matches_with_negation_check(line, ESCALATION_KEYWORDS))

    if matched_escalations and turn_count >= MIN_USEFUL_TURNS:
        return Classification(
            lane=Lane.HOT,
            reason="customer_escalation",
            matched_keywords=tuple(matched_escalations),
            turn_count=turn_count,
        )

    if matched_commitments and turn_count >= MIN_HOT_TURNS:
        return Classification(
            lane=Lane.HOT,
            reason="customer_commitment",
            matched_keywords=tuple(matched_commitments),
            turn_count=turn_count,
        )

    return Classification(
        lane=Lane.COLD,
        reason="default_cold",
        matched_keywords=tuple(matched_commitments + matched_escalations),
        turn_count=turn_count,
    )
