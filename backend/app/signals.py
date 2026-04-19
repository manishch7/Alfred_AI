from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Keyword banks – used for action classification and conflict detection.
# ---------------------------------------------------------------------------

_EMAIL_KEYWORDS: set[str] = {
    "send", "email", "mail", "message", "reply", "forward", "compose", "draft",
}
_CALENDAR_KEYWORDS: set[str] = {
    "schedule", "meeting", "calendar", "appointment", "event",
    "book", "reserve", "invite", "reschedule",
}
_REMINDER_KEYWORDS: set[str] = {
    "remind", "reminder", "alert", "notify", "notification", "alarm", "remember",
}
_FINANCIAL_KEYWORDS: set[str] = {
    "pay", "payment", "transfer", "purchase", "buy", "charge",
    "invoice", "refund", "transaction", "wire", "deposit", "withdraw",
    "venmo", "paypal", "zelle",
}
# Multi-word financial phrase checked as a substring before token matching.
_FINANCIAL_PHRASES: list[str] = ["send money", "bank transfer"]

# ---------------------------------------------------------------------------
# Per-action-type lookup tables.
# ---------------------------------------------------------------------------

# Whether completing this action can be undone after the fact.
_REVERSIBILITY: dict[str, str] = {
    "email": "irreversible",    # a sent email cannot be recalled in general
    "calendar": "reversible",   # events can be cancelled or rescheduled
    "reminder": "reversible",   # reminders can be deleted or modified at any time
    "financial": "irreversible", # monetary transactions cannot be rolled back
    "unknown": "unknown",
}

# Entity slots that must be present in `context` for intent to be considered resolved.
_REQUIRED_ENTITIES: dict[str, list[str]] = {
    "email": ["recipient", "subject"],
    "calendar": ["date", "time"],
    "reminder": ["time"],
    "financial": ["recipient", "amount"],
    "unknown": [],  # no known schema to validate against
}

# Baseline risk score (0–1) reflecting inherent danger of each action category.
_BASE_RISK: dict[str, float] = {
    "financial": 0.75,  # high – money movement is irreversible and high-stakes
    "email": 0.45,      # medium – external communication, hard to retract
    "calendar": 0.15,   # low – scheduling is easily adjusted
    "reminder": 0.10,   # lowest – entirely local, trivially reversible
    "unknown": 0.40,    # uncertain category gets a cautious default
}

# Words/phrases in the latest message that suggest the user is overriding a prior instruction.
_OVERRIDE_SIGNALS: set[str] = {
    "don't", "dont", "do not", "cancel", "stop", "actually",
    "instead", "forget", "never mind", "nevermind", "disregard",
    "ignore", "change", "update", "modify", "scratch that",
}

# Common words excluded when comparing token overlap between messages.
_STOP_WORDS: set[str] = {
    "i", "you", "the", "a", "an", "to", "for", "of", "and", "or",
    "is", "it", "that", "this", "me", "my", "please", "can", "could",
    "will", "would", "should", "be", "have", "has", "had", "do", "did",
}


# ---------------------------------------------------------------------------
# Private helpers.
# ---------------------------------------------------------------------------

def _classify_action(action: str) -> str:
    """Map the action string to a known category via keyword matching.

    Financial phrases are checked first as a substring (before tokenising)
    to catch multi-word phrases like "send money". Then single-token sets
    are checked in priority order (financial > email > calendar > reminder).
    """
    action_lower = action.lower()

    # Substring check for multi-word financial phrases.
    if any(phrase in action_lower for phrase in _FINANCIAL_PHRASES):
        return "financial"

    tokens = set(re.findall(r"\w+", action_lower))

    if tokens & _FINANCIAL_KEYWORDS:
        return "financial"
    if tokens & _EMAIL_KEYWORDS:
        return "email"
    if tokens & _CALENDAR_KEYWORDS:
        return "calendar"
    if tokens & _REMINDER_KEYWORDS:
        return "reminder"
    return "unknown"


def _extract_missing_entities(action_type: str, context: dict) -> list[str]:
    """Return the required slot names that are absent (or falsy) in context.

    An empty return value means the assistant has collected everything it
    needs to execute this action type confidently.
    """
    required = _REQUIRED_ENTITIES.get(action_type, [])
    # A slot is considered missing if the key is absent or its value is falsy.
    return [slot for slot in required if not context.get(slot)]


def _message_text(msg: object) -> str:
    """Normalise a message to plain lowercase text.

    Accepts either a raw string or a dict with a 'content' key
    (OpenAI / Anthropic-style chat history entries).
    """
    if isinstance(msg, dict):
        return str(msg.get("content", "")).lower()
    return str(msg).lower()


def _detect_conflict(conversation_history: list) -> bool:
    """Heuristically decide whether the latest message contradicts a prior instruction.

    Strategy (no LLM):
      1. The latest message must contain at least one override/negation signal
         (e.g. "actually", "cancel", "don't", "instead").
      2. The latest message must share meaningful non-stop-word tokens with at
         least one earlier message, indicating it references the same topic.

    Both conditions must hold to avoid false positives on unrelated negations
    like "don't forget to add a subject" when there's nothing prior to conflict.
    """
    if len(conversation_history) < 2:
        # Need at least two turns for a contradiction to exist.
        return False

    latest_text = _message_text(conversation_history[-1])
    latest_tokens = set(re.findall(r"\w+", latest_text))

    # Condition 1: override language must be present in the latest turn.
    has_override = bool(latest_tokens & _OVERRIDE_SIGNALS) or any(
        phrase in latest_text for phrase in _OVERRIDE_SIGNALS if " " in phrase
    )
    if not has_override:
        return False

    # Meaningful tokens in latest message (strip stop-words and override words).
    latest_meaningful = latest_tokens - _STOP_WORDS - _OVERRIDE_SIGNALS

    # Condition 2: shared meaningful vocabulary with any prior message.
    for prior in conversation_history[:-1]:
        prior_tokens = set(re.findall(r"\w+", _message_text(prior))) - _STOP_WORDS
        if prior_tokens & latest_meaningful:
            # The latest turn overrides something that shares topic with a prior turn.
            return True

    return False


def _compute_risk_score(
    action_type: str,
    missing_entities: list[str],
    conflict_detected: bool,
    reversibility: str,
) -> float:
    """Aggregate a 0–1 risk score from multiple additive factors.

    Base score comes from the action category. Penalties are added for:
      - Each missing required entity (incomplete information raises uncertainty).
      - A detected conflict (acting on contradictory instructions is dangerous).
      - An irreversible action (mistakes cannot be corrected after execution).

    Result is clamped to [0.0, 1.0] and rounded to four decimal places.
    """
    score = _BASE_RISK[action_type]

    # Each unfilled slot increases uncertainty about what the user actually wants.
    score += len(missing_entities) * 0.05

    # A conflict between turns means the assistant may be acting on stale intent.
    if conflict_detected:
        score += 0.15

    # Irreversible actions carry extra inherent risk regardless of other factors.
    if reversibility == "irreversible":
        score += 0.10

    return min(round(score, 4), 1.0)


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------

def compute_signals(
    action: str,
    conversation_history: list,
    context: dict,
) -> dict:
    """Deterministically compute decision signals for a pending assistant action.

    All logic is pure Python – no LLM calls, no I/O, no randomness.

    Parameters
    ----------
    action:
        Short human-readable description of what the assistant intends to do
        (e.g. "send email to John", "schedule meeting at 3pm", "transfer $500").
    conversation_history:
        Ordered list of messages, oldest first. Each entry is either a plain
        string or a dict with at least a ``"content"`` key.
    context:
        Slot/value pairs extracted from the conversation so far.
        Expected keys vary by action type – see ``_REQUIRED_ENTITIES``.

    Returns
    -------
    dict with keys:
        intent_resolved  – bool   – True when all required slots are filled.
        missing_entities – list   – Slot names still needed to execute safely.
        reversibility    – str    – "reversible" | "irreversible" | "unknown".
        risk_score       – float  – 0.0 (safe) → 1.0 (dangerous).
        conflict_detected– bool   – True when latest message contradicts a prior one.
        action_type      – str    – Classified domain category of the action.
    """

    # Classify the action domain so we know which schema and rules to apply.
    action_type: str = _classify_action(action)

    # Determine which required information is still missing from context.
    missing_entities: list[str] = _extract_missing_entities(action_type, context)

    # Intent is resolved only when every required slot has been supplied.
    intent_resolved: bool = len(missing_entities) == 0

    # Look up whether this action category is undoable after execution.
    reversibility: str = _REVERSIBILITY[action_type]

    # Check conversation history for signs the user is overriding a prior instruction.
    conflict_detected: bool = _detect_conflict(conversation_history)

    # Combine all factors into a single 0–1 risk score.
    risk_score: float = _compute_risk_score(
        action_type, missing_entities, conflict_detected, reversibility
    )

    return {
        "intent_resolved": intent_resolved,
        "missing_entities": missing_entities,
        "reversibility": reversibility,
        "risk_score": risk_score,
        "conflict_detected": conflict_detected,
        "action_type": action_type,
    }
