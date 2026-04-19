# Alfred

An AI executive assistant that evaluates pending actions before executing them. The core thesis: an LLM should not be both the agent that decides to act *and* the sole judge of whether acting is safe. Alfred separates those concerns explicitly.

---

## Signals

Six signals are computed before the model sees anything. Each was chosen because it answers a question that has a correct, verifiable answer — making it wrong to leave that answer to the model's discretion.

| Signal | Type | Why deterministic |
|---|---|---|
| `action_type` | enum | Classification against a fixed keyword taxonomy. Letting the model classify would introduce variance on identical inputs. |
| `intent_resolved` | bool | A slot is either present in context or it isn't. This is a lookup, not a judgment. |
| `missing_entities` | list | Same. The required schema per action type is defined in code; completeness is binary. |
| `reversibility` | enum | Determined by action category, not phrasing. An email is irreversible regardless of how politely it's worded. |
| `conflict_detected` | bool | Heuristic: override vocabulary in the latest turn plus shared topic tokens with a prior turn. Deliberately conservative — false negatives are safer than false positives here (a missed conflict surfaces to the model via history anyway). |
| `risk_score` | float | Additive formula: base rate by category, plus penalties for missing slots, detected conflict, and irreversibility. Transparent, auditable, deterministic. A model-generated risk score is neither. |

The signals exist to give the model a structured briefing, not to replace its judgment. They bound the decision space without eliminating it.

---

## How responsibility is split

```
Request
  │
  ▼
compute_signals()          ← pure Python, no I/O, unit-testable
  │  action_type, missing_entities, reversibility,
  │  conflict_detected, risk_score, intent_resolved
  │
  ▼
build_prompt()             ← assembles signals + history + taxonomy into a prompt
  │
  ▼
Claude (claude-sonnet-4-6) ← reads the briefing, reasons over history, decides
  │  decision, rationale, confidence
  │
  ▼
_validate_parsed()         ← enforces schema; rejects out-of-range values
  │
  ▼
Failure handlers           ← timeout → confirm_first, parse failure → confirm_first,
                              missing context → ask_clarifying_question
```

The split is intentional and asymmetric. Code handles everything where correctness is verifiable. The model handles everything where the right answer requires reading between the lines.

---

## What the model decides vs. what code computes

**Code computes:** objective facts about the request — what kind of action it is, what information is missing, whether an override keyword appeared alongside shared topic vocabulary, what the aggregate risk score is.

**The model decides:** what to do given those facts. Crucially, the model is explicitly told it can override signal-derived conclusions when the conversation history warrants it. The few-shot example in the prompt demonstrates exactly this: `conflict_detected = false` by the signal, but the history shows a mid-flight recipient change on an irreversible financial transaction. The right answer is `refuse_escalate`. The model is expected to catch it.

This means the model functions as a *reasoner over a structured briefing*, not as a classifier. The taxonomy sets hard thresholds (execute_silently requires risk < 0.30 and reversibility = reversible), but the model's rationale is expected to engage with specific signal values and specific conversation turns — not produce a generic explanation.

One deliberate constraint: the model never sees raw tool outputs, authentication state, or system internals. It reasons from natural language descriptions of intent. This is a feature. It keeps the reasoning legible, and legible reasoning is auditable.

---

## Prompt design

The prompt is a structured briefing with five sections: computed signals as a formatted key-value block, full conversation history oldest-to-newest, the pending action, a decision taxonomy with explicit threshold rules for each branch, and a worked example.

The taxonomy section is the most important. Each decision maps to a named set of signal conditions. This gives the model a vocabulary and a decision procedure — it doesn't have to invent one. The model's job is to apply the taxonomy to the signals, then check whether history overrides the obvious reading.

The few-shot example is adversarial by design. It picks a case where `conflict_detected = false` but the history reveals a conflict the heuristic missed. The example teaches the model that history is authoritative over signals when they disagree — which is the most dangerous failure mode to get wrong.

The system message is kept minimal and constant across requests so prompt caching is effective. Everything dynamic goes in the user turn. The result is that the static system message is served from cache after the first request at roughly 10% of standard input cost.

One deliberate omission: chain-of-thought. The model is asked for `decision`, `rationale`, and `confidence` — not a scratchpad. Extended reasoning inside the response would bloat the JSON contract and make validation harder. If extended thinking were enabled at the SDK level, that would be the right lever, not prompt-level CoT.

---

## Expected failure modes

**Classification errors.** The keyword taxonomy is shallow. "Please move the $500 budget line item to next quarter" will be classified as `financial` (contains "500"), not `calendar`. Misclassification propagates to the wrong required-entity schema, wrong reversibility, and a skewed risk score — all before the model sees anything. The model can partially recover via history reasoning, but it's working against a bad briefing.

**Conflict detection false negatives.** The heuristic requires an override word *and* shared topic vocabulary. A user who writes "send it to Sarah" after "send it to John" (no override word, just a new name) won't trigger `conflict_detected`. The model should catch it from history; whether it does depends on how obviously the name change reads in context.

**Confidence miscalibration.** The model's `confidence` field reflects self-reported certainty, not a calibrated probability. High confidence on a wrong decision is worse than low confidence on a right one. There's no feedback loop today that would let the model learn from outcomes.

**Adversarial phrasing.** "Yep send it" after a legal hold is the obvious case — no override signal, all slots filled, signals point toward execution. The model is the last line of defense. Whether it reads the hold correctly depends on whether the hold instruction is still visible in the context window and whether the few-shot example has trained the relevant pattern.

**Timeout as a decision.** Defaulting to `confirm_first` on a 10-second timeout is safe but not free. If Alfred is embedded in a synchronous user flow, a timeout looks like a hang. The 10-second budget also leaves no room for the model to do extended reasoning on hard cases — which are exactly the ones where you'd want more thinking time.

---

## How the system evolves as Alfred gains riskier tools

The current architecture breaks at two points as the tool surface grows.

**Risk scoring becomes inadequate.** A flat base rate per action category doesn't compose. "Schedule a meeting with the CFO and the board" and "schedule a standup with your team" are both `calendar` actions with the same base risk. The relevant difference — who's involved, what's being discussed — isn't captured. Risk needs to be a function of action parameters, not just action type.

**The five-way decision taxonomy doesn't scale.** `confirm_first` is doing too much work. For a $50 wire transfer and a $500,000 wire transfer, the right confirmation experience is completely different. As action stakes increase, you need tiered approval chains: lightweight in-app confirmation for medium-risk actions, out-of-band confirmation (email, push, phone) for high-stakes irreversible ones, and multi-party authorization for anything that crosses a financial threshold or affects a third party without their consent.

What needs to change structurally:

- **Tool-specific signal schemas.** Each tool integration defines its own required entities, reversibility model, and risk function. The current flat keyword taxonomy is a prototype, not a production classification system.
- **Reversibility as infrastructure.** Today `reversibility` is a label. A production system needs actual undo queues: emails held in a sending queue for 30 seconds, calendar events soft-deleted before propagation, financial transactions staged with an explicit commit step.
- **Audit log as a first-class output.** Every decision — including the signals, the prompt, the raw model output, and the final action taken — needs to be append-only and tamper-evident. This is the prerequisite for everything else: debugging, compliance, user trust, and model fine-tuning.

---

## What I would build next with six months

**An evaluation harness before anything else.** The six preloaded scenarios are a start, not an eval suite. A real harness runs every scenario against the model on every deploy, tracks decision distributions across model versions, and flags regressions. Right now there's no way to know if a prompt change made the system safer or less safe. That's the most important gap.

**Outcome feedback loop.** When Alfred executes an action, did it turn out to be the right call? User corrections ("undo that", "that wasn't what I meant"), error rates, and explicit ratings are signals that should feed back into both the prompt and the risk scoring function. A system that can't learn from its mistakes will accumulate them.

**Per-user risk profiles.** Risk is not uniform across users. A CFO authorizing a wire transfer is different from an intern doing the same thing. The context dictionary is currently a flat bag of slots; it should include role, historical approval patterns, and declared authorization levels. This changes what `confirm_first` means for different actors.

**Separate the policy engine from the model.** Today the decision taxonomy lives inside the prompt, which means changing policy requires a prompt change, a deploy, and a manual test pass. Policy rules (what requires confirmation, what thresholds trigger escalation) should be configuration that can be updated independently of the model. The model reasons about facts; humans declare policy. Those are different jobs and shouldn't be coupled.

**Replace keyword classification with a lightweight embeddings-based classifier.** A few hundred labeled examples, a small embedding model, and a classifier on top would eliminate the most common failure mode (misclassification on natural-language action descriptions) at minimal inference cost. The keyword taxonomy was the right thing to build first — it's transparent and requires no training data — but it has a hard ceiling on recall.

**Tool-native reversibility.** For every tool Alfred can use, define a compensation action and hold it until a configurable window passes. This changes the risk calculus for an entire class of medium-risk actions: if you can undo it reliably, `execute_and_notify` becomes the right answer for things that currently need `confirm_first`. Reducing friction on safe actions is as important as blocking unsafe ones.

---

## Running locally

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ANTHROPIC_API_KEY=your_key uvicorn app.main:app --reload

# Frontend
cd frontend
npm install
npm run dev          # → http://localhost:3000
```

The frontend expects the backend at `http://localhost:8000`. Override with `NEXT_PUBLIC_API_URL` in `frontend/.env.local`.

---

## Deploying

Deploy the backend first — you need its URL before you can configure the frontend, and you need the frontend URL before CORS is fully locked down. The sequence matters.

### Backend → Railway

1. Push the repo to GitHub.
2. In Railway: **New Project → Deploy from GitHub repo**. When prompted for the root directory, set it to `backend/`.
3. Railway detects `runtime.txt` and `requirements.txt` via Nixpacks. The start command in `railway.toml` takes over from there.
4. Add environment variables under **Variables**:

   | Variable | Value |
   |---|---|
   | `ANTHROPIC_API_KEY` | Your key from [console.anthropic.com](https://console.anthropic.com/) |
   | `FRONTEND_URL` | Leave blank for now — fill in after Vercel deploy |

5. Deploy. Confirm `GET /health` returns `{"status": "ok"}` on the Railway-assigned URL (e.g. `https://alfred-backend-production.up.railway.app`).

### Frontend → Vercel

1. In Vercel: **Add New Project → Import Git Repository**.
2. Set **Root Directory** to `frontend/`. Vercel picks up `vercel.json` and detects Next.js automatically.
3. Add environment variable:

   | Variable | Value |
   |---|---|
   | `NEXT_PUBLIC_API_URL` | Your Railway backend URL, no trailing slash |

4. Deploy. Note the assigned Vercel URL (e.g. `https://alfred-your-name.vercel.app`).

### Wire CORS

Go back to Railway → **Variables** and set `FRONTEND_URL` to the Vercel URL from step 4 above. Railway redeploys automatically. Without this, the browser will block all API requests from the deployed frontend.

> **Preview deployments:** Vercel generates unique URLs for every branch deploy (`alfred-git-branch-name-user.vercel.app`). These are not in the CORS allowlist. If you need preview deploys to hit production backend, add the preview URL as a second `FRONTEND_URL` value — or run a dedicated staging backend with its own Railway service and env vars.

### Environment variable reference

See `backend/.env.example` and `frontend/.env.example` for annotated copies of every variable. Neither file is committed with real values — copy to `.env` / `.env.local` locally and set the production values directly in Railway and Vercel's dashboards.
