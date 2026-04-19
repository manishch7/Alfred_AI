from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# Decision taxonomy – used in the prompt and kept here as the single source
# of truth so the string values never drift between the builder and callers.
# ---------------------------------------------------------------------------

VALID_DECISIONS = (
    "execute_silently",          # safe, reversible, fully resolved – just do it
    "execute_and_notify",        # safe to execute but user should be informed
    "confirm_first",             # high-risk or irreversible – get explicit sign-off
    "ask_clarifying_question",   # required information is still missing
    "refuse_escalate",           # conflict, danger, or out-of-scope – do not proceed
)

# ---------------------------------------------------------------------------
# Decision guidance injected into the prompt so the model can reason
# deterministically about which branch to choose.
# ---------------------------------------------------------------------------

_DECISION_GUIDE = """
DECISION TAXONOMY
-----------------
Choose exactly one decision value based on the signals provided:

  execute_silently
      • intent_resolved = true
      • risk_score < 0.30
      • reversibility = "reversible"
      • conflict_detected = false
      → Action is safe and fully specified; execute without interrupting the user.

  execute_and_notify
      • intent_resolved = true
      • risk_score between 0.30 and 0.55 (inclusive)
      • conflict_detected = false
      → Action is acceptable but consequential enough that the user deserves
        a post-execution summary.

  confirm_first
      • intent_resolved = true
      • risk_score > 0.55
      • conflict_detected = false
      → Action is high-stakes; pause and ask for explicit approval before
        proceeding. Reversibility is already factored into risk_score (irreversible
        actions carry a +0.10 penalty), so do not use reversibility alone to
        upgrade a decision to confirm_first.

  ask_clarifying_question
      • intent_resolved = false  (missing_entities is non-empty)
      → Cannot proceed safely without more information; ask the user to
        supply the missing slot(s) listed in missing_entities.

  refuse_escalate
      • conflict_detected = true  OR  risk_score = 1.0
      → A contradiction in the conversation or extreme risk was detected;
        do not execute and surface the issue to the user.

Signal values always take precedence over your own assessment.
""".strip()

# ---------------------------------------------------------------------------
# Few-shot example – shows the model exactly what valid output looks like.
# ---------------------------------------------------------------------------

_FEW_SHOT_EXAMPLE = """
EXAMPLE
-------
Signals:
  action_type       : financial
  intent_resolved   : true
  missing_entities  : []
  reversibility     : irreversible
  risk_score        : 0.85
  conflict_detected : false

Conversation history:
  user      : Transfer $2,000 to Alice's account ending in 4782.
  assistant : Got it. Initiating the transfer now.
  user      : Wait – actually send it to Bob, not Alice.

Action: "transfer $2000 to Bob"

Expected output:
{
  "decision": "refuse_escalate",
  "rationale": "The latest message contradicts the original recipient instruction (Alice → Bob). Although conflict_detected is false by the signal, the conversation history reveals a mid-flight change of recipient on an irreversible financial transaction. Proceeding without explicit re-confirmation would be unsafe.",
  "confidence": 0.91
}
""".strip()

# Note: the example intentionally shows the model reasoning beyond the raw
# signal value (conflict_detected=false) by reading the history itself,
# demonstrating that history is authoritative over extracted signals when
# they conflict.


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _format_signals(signals: dict) -> str:
    """Render signals dict as an aligned key : value block for readability."""
    if not signals:
        return "  (no signals)"
    width = max(len(k) for k in signals) + 2
    lines = []
    for key, value in signals.items():
        # Pretty-print lists; keep other types as their JSON representation.
        if isinstance(value, list):
            rendered = json.dumps(value)
        elif isinstance(value, bool):
            rendered = str(value).lower()  # match JSON true/false convention
        elif isinstance(value, float):
            rendered = f"{value:.4f}"
        else:
            rendered = str(value)
        lines.append(f"  {key:<{width}}: {rendered}")
    return "\n".join(lines)


def _format_history(conversation_history: list) -> str:
    """Render conversation history as a readable transcript.

    Accepts entries that are either plain strings or dicts with
    'role' / 'content' keys (OpenAI / Anthropic message format).
    """
    if not conversation_history:
        return "  (no prior conversation)"

    lines = []
    for i, msg in enumerate(conversation_history):
        if isinstance(msg, dict):
            role = msg.get("role", f"turn_{i}")
            content = msg.get("content", "")
        else:
            # Plain string – label by index parity (even = user, odd = assistant)
            role = "user" if i % 2 == 0 else "assistant"
            content = str(msg)
        lines.append(f"  {role:<10}: {content}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------

def build_prompt(
    action: str,
    conversation_history: list,
    signals: dict,
) -> str:
    """Construct a structured prompt for claude-sonnet-4-20250514.

    The returned string is a complete, self-contained prompt that can be sent
    directly as the ``user`` turn (with a minimal system turn if desired).
    It includes:
      • role + task framing
      • computed signals as structured context
      • full conversation history
      • decision taxonomy with selection rules
      • a worked few-shot example
      • strict JSON-only output instructions

    Parameters
    ----------
    action:
        The action the assistant is about to take (plain English description).
    conversation_history:
        Ordered list of messages (strings or dicts with 'role'/'content').
    signals:
        Output of ``compute_signals()`` – six keys describing intent,
        risk, reversibility, etc.

    Returns
    -------
    A single formatted prompt string ready to be sent to the model.
    """

    signals_block = _format_signals(signals)
    history_block = _format_history(conversation_history)
    decisions_str = " | ".join(VALID_DECISIONS)

    prompt = f"""
You are Alfred, an AI executive assistant with the ability to take real-world
actions on behalf of the user (send emails, manage calendar, move money, etc.).
Before executing any action you must reason about its safety and appropriateness,
then output a single JSON object – nothing else.

═══════════════════════════════════════════════════════════
COMPUTED SIGNALS  (deterministic, pre-LLM analysis)
═══════════════════════════════════════════════════════════
{signals_block}

═══════════════════════════════════════════════════════════
CONVERSATION HISTORY  (oldest → newest)
═══════════════════════════════════════════════════════════
{history_block}

═══════════════════════════════════════════════════════════
PENDING ACTION
═══════════════════════════════════════════════════════════
  "{action}"

═══════════════════════════════════════════════════════════
{_DECISION_GUIDE}

═══════════════════════════════════════════════════════════
{_FEW_SHOT_EXAMPLE}

═══════════════════════════════════════════════════════════
YOUR TASK
═══════════════════════════════════════════════════════════
Analyse the signals and conversation history, then decide how to handle the
pending action.

Output ONLY a JSON object – no markdown fences, no commentary, no keys beyond
those listed below.

Required JSON schema:
{{
  "decision"  : "<one of: {decisions_str}>",
  "rationale" : "<concise explanation referencing specific signals and history>",
  "confidence": <float between 0.0 and 1.0>
}}

Rules:
  • "decision" must be exactly one of the five values above – any other value
    is invalid.
  • "rationale" must cite at least one signal value or conversation turn.
  • "confidence" is your own assessment of how certain you are in the decision
    (not the risk score).
  • Do not output anything outside the JSON object.
""".strip()

    return prompt


# ---------------------------------------------------------------------------
# Quick smoke-test – run `python prompt_builder.py` to inspect the output.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _test_action = "send email to ceo@example.com with Q1 board summary"

    _test_history = [
        {"role": "user",      "content": "Attach the Q1 financials and email them to the board."},
        {"role": "assistant", "content": "I can do that. Should I send to the full board mailing list?"},
        {"role": "user",      "content": "Actually just the CEO for now – keep it confidential."},
    ]

    _test_signals = {
        "action_type":      "email",
        "intent_resolved":  True,
        "missing_entities": [],
        "reversibility":    "irreversible",
        "risk_score":       0.55,
        "conflict_detected": False,
    }

    final_prompt = build_prompt(_test_action, _test_history, _test_signals)
    print(final_prompt)
