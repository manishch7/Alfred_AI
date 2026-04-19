import asyncio
import json
import os
import re
from typing import Any

import openai
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.prompt_builder import VALID_DECISIONS, build_prompt
from app.scenarios import SCENARIOS
from app.signals import compute_signals

# ---------------------------------------------------------------------------
# Groq client – lazy so a missing key fails at request time, not startup.
# ---------------------------------------------------------------------------

_client: openai.OpenAI | None = None
_MODEL = "qwen/qwen3-32b"


def _get_client() -> openai.OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise HTTPException(status_code=500, detail="GROQ_API_KEY is not set")
        _client = openai.OpenAI(
            api_key=api_key,
            base_url="https://api.groq.com/openai/v1",
        )
    return _client

# ---------------------------------------------------------------------------
# Static system message.
# ---------------------------------------------------------------------------

_SYSTEM_MESSAGE = (
    "You are Alfred, an AI executive assistant that evaluates pending actions "
    "for safety and appropriateness. "
    "Always respond with a single valid JSON object and nothing else — "
    "no markdown fences, no prose, no extra keys."
)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:3001",
]
if _frontend_url := os.getenv("FRONTEND_URL", "").strip():
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
    inputs: dict[str, Any]
    computed_signals: dict[str, Any]
    prompt_sent: str
    raw_model_output: str
    parsed_decision: str
    rationale: str
    confidence: float
    failure_mode: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict:
    """Parse the model response as JSON, stripping DeepSeek <think> blocks first."""
    # DeepSeek R1 emits <think>...</think> before the actual answer — strip it.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
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
    """Full decision pipeline: signals → prompt → Grok → parsed JSON."""

    # Failure mode 3 – missing critical context.
    if not request.action.strip() or not request.conversation_history:
        return _failure_response(
            request,
            decision="ask_clarifying_question",
            rationale="Insufficient context provided",
            failure_mode="missing_context",
        )

    # Step 1 – deterministic signal computation (no LLM).
    # Step 2 – build the structured prompt string.
    try:
        signals: dict[str, Any] = compute_signals(
            request.action, request.conversation_history, request.context
        )
        prompt: str = build_prompt(request.action, request.conversation_history, signals)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Signal computation error: {exc}")

    # Step 3 – call the xAI / Grok API (OpenAI-compatible).
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                _get_client().chat.completions.create,
                model=_MODEL,
                max_tokens=512,
                messages=[
                    {"role": "system", "content": _SYSTEM_MESSAGE},
                    {"role": "user", "content": prompt},
                ],
            ),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        # Failure mode 1 – LLM did not respond within 30 seconds.
        return _failure_response(
            request,
            decision="confirm_first",
            rationale="LLM timeout, defaulting to safe behavior",
            failure_mode="llm_timeout",
            signals=signals,
            prompt=prompt,
        )
    except openai.AuthenticationError as exc:
        raise HTTPException(status_code=500, detail=f"xAI auth error: {exc.message}")
    except openai.BadRequestError as exc:
        raise HTTPException(status_code=400, detail=f"Bad request to xAI: {exc.message}")
    except openai.RateLimitError as exc:
        raise HTTPException(status_code=429, detail=f"xAI rate limit: {exc.message}")
    except openai.APIError as exc:
        raise HTTPException(status_code=502, detail=f"xAI API error: {exc.message}")

    # Step 4 – extract raw text.
    raw_output: str = response.choices[0].message.content or ""

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
