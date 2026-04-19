import asyncio
import json
import os
import re
from typing import Any

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.prompt_builder import VALID_DECISIONS, build_prompt
from app.scenarios import SCENARIOS
from app.signals import compute_signals

# ---------------------------------------------------------------------------
# Anthropic client – instantiated once at module load; reads ANTHROPIC_API_KEY.
# ---------------------------------------------------------------------------

_client = anthropic.Anthropic()

# ---------------------------------------------------------------------------
# Static system message (always identical → gets cached by the API).
# ---------------------------------------------------------------------------

_SYSTEM_MESSAGE = (
    "You are Alfred, an AI executive assistant that evaluates pending actions "
    "for safety and appropriateness. "
    "Always respond with a single valid JSON object and nothing else — "
    "no markdown fences, no prose, no extra keys."
)

# ---------------------------------------------------------------------------
# CORS – always allow local dev origins; add FRONTEND_URL in production.
# Not setting FRONTEND_URL means the deployed frontend won't be able to reach
# the API — fail loudly in prod rather than silently allow everything.
# ---------------------------------------------------------------------------

_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:3001",
]
if _frontend_url := os.getenv("FRONTEND_URL"):
    _ALLOWED_ORIGINS.append(_frontend_url)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Alfred API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class DecideRequest(BaseModel):
    action: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Plain-English description of the pending action.",
    )
    conversation_history: list[str | dict[str, Any]] = Field(
        default_factory=list,
        max_length=50,
        description="Ordered message list (strings or {role, content} dicts), oldest first.",
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Extracted slot/value pairs (recipient, time, amount, …).",
    )


class DecideResponse(BaseModel):
    # Mirror of the request inputs for full traceability.
    inputs: dict[str, Any]
    # Output of compute_signals – deterministic, no LLM involved.
    computed_signals: dict[str, Any]
    # The exact prompt string sent to the model.
    prompt_sent: str
    # Raw text returned by the model before parsing.
    raw_model_output: str
    # Fields parsed from the model's JSON response.
    parsed_decision: str
    rationale: str
    confidence: float
    # Set on degraded responses so the frontend can display failure state.
    # None on happy-path responses.
    failure_mode: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict:
    """Parse the model response as JSON, with a fallback regex extraction."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Cannot parse model output as JSON: {text[:300]!r}")


def _validate_parsed(parsed: dict) -> tuple[str, str, float]:
    """Extract and validate the three required fields from the parsed JSON."""
    decision = parsed.get("decision", "")
    if decision not in VALID_DECISIONS:
        raise ValueError(
            f"Invalid decision {decision!r}. Must be one of: {', '.join(VALID_DECISIONS)}"
        )

    rationale = str(parsed.get("rationale", ""))

    raw_confidence = parsed.get("confidence", 0.0)
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        raise ValueError(f"confidence must be a float, got {raw_confidence!r}")

    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence {confidence} is out of [0, 1] range")

    return decision, rationale, confidence


def _failure_response(
    request: DecideRequest,
    *,
    decision: str,
    rationale: str,
    failure_mode: str,
    signals: dict | None = None,
    prompt: str = "",
    raw_output: str = "",
) -> DecideResponse:
    """Build a safe-default DecideResponse for any of the three failure paths."""
    return DecideResponse(
        inputs={
            "action": request.action,
            "conversation_history": request.conversation_history,
            "context": request.context,
        },
        computed_signals=signals or {},
        prompt_sent=prompt,
        raw_model_output=raw_output,
        parsed_decision=decision,
        rationale=rationale,
        confidence=0.0,
        failure_mode=failure_mode,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/scenarios")
def scenarios() -> list[dict]:
    return SCENARIOS


@app.post("/decide", response_model=DecideResponse)
async def decide(request: DecideRequest) -> DecideResponse:
    """Full decision pipeline: signals → prompt → Claude → parsed JSON."""

    # Failure mode 3 – missing critical context.
    # action must be non-empty; conversation_history must have at least one turn.
    if not request.action.strip() or not request.conversation_history:
        return _failure_response(
            request,
            decision="ask_clarifying_question",
            rationale="Insufficient context provided",
            failure_mode="missing_context",
        )

    # Step 1 – deterministic signal computation (no LLM).
    # Step 2 – build the structured prompt string.
    # Both are pure Python and should never throw on valid inputs, but an
    # unexpected error here must still produce a structured response rather
    # than a raw FastAPI 500 with no detail.
    try:
        signals: dict[str, Any] = compute_signals(
            request.action, request.conversation_history, request.context
        )
        prompt: str = build_prompt(request.action, request.conversation_history, signals)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Signal computation error: {exc}")

    # Step 3 – call the Anthropic API.
    # The sync client is run in a thread so it doesn't block the event loop,
    # wrapped in wait_for to enforce a 10-second hard deadline.
    try:
        message = await asyncio.wait_for(
            asyncio.to_thread(
                _client.messages.create,
                model="claude-sonnet-4-6",
                max_tokens=512,
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_MESSAGE,
                        # Cache this static block: after the first request the system
                        # message is served from cache at ~10 % of standard input cost.
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=10.0,
        )
    except asyncio.TimeoutError:
        # Failure mode 1 – LLM did not respond within 10 seconds.
        return _failure_response(
            request,
            decision="confirm_first",
            rationale="LLM timeout, defaulting to safe behavior",
            failure_mode="llm_timeout",
            signals=signals,
            prompt=prompt,
        )
    except anthropic.AuthenticationError as exc:
        raise HTTPException(status_code=500, detail=f"Anthropic auth error: {exc.message}")
    except anthropic.BadRequestError as exc:
        raise HTTPException(status_code=400, detail=f"Bad request to Anthropic: {exc.message}")
    except anthropic.RateLimitError as exc:
        raise HTTPException(status_code=429, detail=f"Anthropic rate limit: {exc.message}")
    except anthropic.APIError as exc:
        raise HTTPException(status_code=502, detail=f"Anthropic API error: {exc.message}")

    # Step 4 – extract the raw text from the response content blocks.
    raw_output: str = next(
        (block.text for block in message.content if block.type == "text"),
        "",
    )

    # Step 5 – parse and validate the JSON response.
    try:
        parsed = _extract_json(raw_output)
        decision, rationale, confidence = _validate_parsed(parsed)
    except ValueError:
        # Failure mode 2 – model returned something we cannot parse or validate.
        return _failure_response(
            request,
            decision="confirm_first",
            rationale="Could not parse model output",
            failure_mode="malformed_output",
            signals=signals,
            prompt=prompt,
            raw_output=raw_output,
        )

    return DecideResponse(
        inputs={
            "action": request.action,
            "conversation_history": request.conversation_history,
            "context": request.context,
        },
        computed_signals=signals,
        prompt_sent=prompt,
        raw_model_output=raw_output,
        parsed_decision=decision,
        rationale=rationale,
        confidence=confidence,
    )
