# Alfred

An AI executive assistant that evaluates pending actions before executing them. The core thesis: an LLM should not be both the agent that decides to act *and* the sole judge of whether acting is safe. Alfred separates those concerns explicitly.

---

## Signals

Six signals are computed deterministically before the model sees anything:

| Signal | Type | Why deterministic |
|---|---|---|
| `action_type` | enum | Keyword taxonomy — same input always produces same output |
| `intent_resolved` | bool | A slot is either present in context or it isn't |
| `missing_entities` | list | Required schema per action type is defined in code |
| `reversibility` | enum | Determined by action category, not phrasing |
| `conflict_detected` | bool | Override vocabulary + shared topic tokens with a prior turn |
| `risk_score` | float | Additive formula: base rate + penalties for missing slots, conflict, irreversibility |

Signals give the model a structured briefing, not a verdict. They bound the decision space without eliminating judgment.

---

## LLM vs. code split

```
Request
  │
  ▼
compute_signals()          ← pure Python, no I/O, deterministic
  │
  ▼
build_prompt()             ← assembles signals + history + taxonomy
  │
  ▼
Groq (qwen/qwen3-32b)     ← reasons over briefing + history, decides
  │
  ▼
_validate_parsed()         ← enforces schema, rejects invalid values
  │
  ▼
Failure handlers           ← timeout → confirm_first, parse failure → confirm_first,
                              missing context → ask_clarifying_question
```

**Code computes** objective facts: action type, missing slots, risk score, reversibility, conflict heuristic.
**Model decides** what to do given those facts — and is explicitly told it can override signal-derived conclusions when conversation history warrants it.

---

## Prompt design

Five sections: computed signals, full conversation history (oldest → newest), the pending action, a decision taxonomy with explicit threshold rules per branch, and one adversarial few-shot example.

The few-shot example shows `conflict_detected = false` by signal, but the history reveals a mid-flight recipient change on an irreversible financial transaction — teaching the model that history is authoritative over signals when they disagree. The system message is static and cached, keeping per-request token cost low.

---

## Expected failure modes

- **Keyword misclassification** — shallow taxonomy misfires on natural language (e.g. "$500 budget line item" → `financial` not `calendar`). Model works against a bad briefing.
- **Conflict detection false negatives** — heuristic requires an override word. "Send it to Sarah" after "send it to John" (no override word) won't trigger the flag; model must catch it from history.
- **Adversarial phrasing** — "Yep send it" after a legal hold has no override signal; model is the last line of defense.
- **Timeout as a decision** — 10-second hard cap defaults to `confirm_first`, which is safe but looks like a hang in synchronous flows.

---

## Evolving as Alfred gains riskier tools

Two things break at scale: risk scoring and the taxonomy. A flat base rate per category doesn't distinguish a CFO wire from an intern wire. `confirm_first` does too much work across wildly different stakes.

What needs to change: tool-specific signal schemas, reversibility as actual infrastructure (undo queues, staged commits), tiered approval chains for high-stakes actions, and an audit log as a first-class output.

---

## What I'd build next with six months

1. **Dynamic risk scoring** — the flat base rates (email=0.45, financial=0.75) mean "send a reminder to one person" and "email the full investor list with Q3 financials" score identically. Risk needs to factor in recipient count, amount involved, content sensitivity, and time pressure in the message — not just action category.

2. **Smarter conflict detection** — the current heuristic requires an override keyword ("actually", "don't", "cancel"). It misses "Ok go ahead and send it" following an uncleared legal hold because the final turn has no override word. An NLP-based hold/gate detector that tracks pending approval states across the conversation would catch this class of adversarial inputs.

3. **Eval harness before anything else** — we saw during testing that the hardest scenarios (legal hold + implicit re-approval) produce borderline decisions. Without a harness that runs every scenario on every deploy and tracks decision distributions across model versions, there's no way to know if a prompt change made the system safer or less safe.

4. **Outcome feedback loop** — the model self-reports 80–95% confidence regardless of whether the decision was correct. Wiring in user corrections ("undo that", "that wasn't what I meant") and actual outcome data lets the risk scoring and confidence calibration improve over time rather than staying static.

5. **Policy engine separate from the model** — decision thresholds live inside the prompt today, so changing policy requires a prompt edit and a redeploy. Thresholds should be configuration that non-engineers can update. The model reasons about facts; humans declare policy. Those are different jobs and shouldn't be coupled.

6. **Model-agnostic inference layer** — during development we cycled through Anthropic, xAI, and Groq as the underlying model changed. Each swap required code changes. An abstraction layer with a consistent interface would let the best available model be swapped in without touching the decision logic.

---

## Running locally

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
GROQ_API_KEY=your_key uvicorn app.main:app --reload

# Frontend
cd frontend
npm install
npm run dev    # → http://localhost:3000
```

The frontend expects the backend at `http://localhost:8000`. Override with `NEXT_PUBLIC_API_URL` in `frontend/.env.local`.

---

## Deploying (both services on Railway)

Deploy backend first — you need its URL to configure the frontend.

**Backend service** (root: `backend/`)
- Add env var: `GROQ_API_KEY` (get free key at console.groq.com)
- Confirm `GET /health` returns `{"status":"ok"}`

**Frontend service** (root: `frontend/`)
- Add env var: `NEXT_PUBLIC_API_URL=<backend Railway URL>` — must be set before the build runs

**Wire CORS** — go back to the backend service and add `FRONTEND_URL=<frontend Railway URL>`. Without this, the browser blocks all API requests from the deployed frontend.

See `backend/.env.example` and `frontend/.env.example` for the full variable reference.
