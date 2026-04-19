'use client';

import { useState, useEffect, type ReactNode } from 'react';

const API = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

// ── Types ────────────────────────────────────────────────────────────────────

type Role = 'user' | 'assistant';

interface Turn {
  role: Role;
  content: string;
}

interface CtxEntry {
  key: string;
  value: string;
}

interface Scenario {
  id: string;
  label: string;
  difficulty: 'easy' | 'medium' | 'hard';
  action: string;
  conversation_history: Turn[];
  context: Record<string, string>;
}

interface Signals {
  intent_resolved: boolean;
  missing_entities: string[];
  reversibility: string;
  risk_score: number;
  conflict_detected: boolean;
  action_type: string;
}

interface DecideResponse {
  inputs: Record<string, unknown>;
  computed_signals: Signals;
  prompt_sent: string;
  raw_model_output: string;
  parsed_decision: string;
  rationale: string;
  confidence: number;
  failure_mode: string | null;
}

// ── Static maps ──────────────────────────────────────────────────────────────

const DECISION_META: Record<string, { label: string; cls: string }> = {
  execute_silently:        { label: 'Execute Silently',        cls: 'bg-green-100 text-green-800 border-green-200' },
  execute_and_notify:      { label: 'Execute & Notify',        cls: 'bg-blue-100 text-blue-800 border-blue-200' },
  confirm_first:           { label: 'Confirm First',           cls: 'bg-amber-100 text-amber-800 border-amber-200' },
  ask_clarifying_question: { label: 'Ask Clarifying Question', cls: 'bg-purple-100 text-purple-800 border-purple-200' },
  refuse_escalate:         { label: 'Refuse & Escalate',       cls: 'bg-red-100 text-red-800 border-red-200' },
};

const DIFFICULTY_CLS: Record<string, string> = {
  easy:   'text-green-600',
  medium: 'text-amber-600',
  hard:   'text-red-500',
};

const FAILURE_LABELS: Record<string, string> = {
  llm_timeout:      'LLM Timeout',
  malformed_output: 'Malformed Model Output',
  missing_context:  'Missing Critical Context',
};

// ── Page ─────────────────────────────────────────────────────────────────────

export default function Home() {
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [selectedId, setSelectedId] = useState('');

  const [action, setAction]         = useState('');
  const [history, setHistory]       = useState<Turn[]>([{ role: 'user', content: '' }]);
  const [ctxEntries, setCtxEntries] = useState<CtxEntry[]>([{ key: '', value: '' }]);

  const [loading, setLoading]   = useState(false);
  const [response, setResponse] = useState<DecideResponse | null>(null);
  const [apiError, setApiError] = useState<string | null>(null);
  const [hoodOpen, setHoodOpen] = useState(false);

  useEffect(() => {
    fetch(`${API}/scenarios`)
      .then(r => r.json())
      .then((d: Scenario[]) => setScenarios(d))
      .catch(() => {});
  }, []);

  function loadScenario(id: string) {
    setSelectedId(id);
    setResponse(null);
    setApiError(null);
    setHoodOpen(false);
    if (!id) return;
    const s = scenarios.find(s => s.id === id);
    if (!s) return;
    setAction(s.action);
    setHistory(s.conversation_history.length ? s.conversation_history : [{ role: 'user', content: '' }]);
    const entries = Object.entries(s.context).map(([key, value]) => ({ key, value: String(value) }));
    setCtxEntries(entries.length ? entries : [{ key: '', value: '' }]);
  }

  // History helpers
  function addTurn() {
    setHistory(p => [...p, { role: p.length % 2 === 0 ? 'user' : 'assistant', content: '' }]);
  }
  function removeTurn(i: number) { setHistory(p => p.filter((_, idx) => idx !== i)); }
  function setTurnField(i: number, field: 'role' | 'content', v: string) {
    setHistory(p => p.map((t, idx) => idx === i ? { ...t, [field]: v as Role } : t));
  }

  // Context helpers
  function addCtx() { setCtxEntries(p => [...p, { key: '', value: '' }]); }
  function removeCtx(i: number) { setCtxEntries(p => p.filter((_, idx) => idx !== i)); }
  function setCtxField(i: number, field: 'key' | 'value', v: string) {
    setCtxEntries(p => p.map((e, idx) => idx === i ? { ...e, [field]: v } : e));
  }

  async function submit() {
    setLoading(true);
    setApiError(null);
    setResponse(null);
    setHoodOpen(false);

    const context = Object.fromEntries(
      ctxEntries.filter(e => e.key.trim()).map(e => [e.key.trim(), e.value])
    );

    try {
      const res = await fetch(`${API}/decide`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action: action.trim(),
          conversation_history: history.filter(t => t.content.trim()),
          context,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(body.detail ?? `HTTP ${res.status}`);
      }
      setResponse(await res.json());
    } catch (e) {
      setApiError(e instanceof Error ? e.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  }

  const meta = response
    ? (DECISION_META[response.parsed_decision] ?? { label: response.parsed_decision, cls: 'bg-gray-100 text-gray-700 border-gray-200' })
    : null;

  return (
    <div className="min-h-screen bg-gray-50 flex flex-col">

      {/* Header */}
      <header className="sticky top-0 z-20 bg-white border-b border-gray-200 px-8 py-4 flex items-center gap-3">
        <span className="text-lg font-semibold tracking-tight text-gray-900">Alfred</span>
        <span className="text-gray-200 select-none">|</span>
        <span className="text-sm text-gray-400">Decision Pipeline</span>
      </header>

      {/* Two-panel body */}
      <div className="flex flex-1 overflow-hidden">

        {/* LEFT — input */}
        <div className="w-[46%] flex flex-col bg-white border-r border-gray-200 overflow-hidden">
          <div className="flex-1 overflow-y-auto px-7 py-6 space-y-7">

            {/* Scenario picker */}
            {scenarios.length > 0 && (
              <section>
                <FieldLabel>Load prebuilt scenario</FieldLabel>
                <select
                  value={selectedId}
                  onChange={e => loadScenario(e.target.value)}
                  className="mt-2 w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-gray-300"
                >
                  <option value="">— Manual input —</option>
                  {scenarios.map(s => (
                    <option key={s.id} value={s.id}>
                      {s.label}
                    </option>
                  ))}
                </select>
                {selectedId && (() => {
                  const s = scenarios.find(s => s.id === selectedId);
                  return s ? (
                    <p className={`mt-1.5 text-xs font-medium ${DIFFICULTY_CLS[s.difficulty]}`}>
                      {s.difficulty.charAt(0).toUpperCase() + s.difficulty.slice(1)} difficulty
                    </p>
                  ) : null;
                })()}
              </section>
            )}

            <Divider />

            {/* Action */}
            <section>
              <FieldLabel>Action</FieldLabel>
              <input
                type="text"
                value={action}
                onChange={e => setAction(e.target.value)}
                placeholder="e.g. send email to team about product update"
                className="mt-2 w-full rounded-lg border border-gray-200 px-3 py-2 text-sm text-gray-800 placeholder:text-gray-300 focus:outline-none focus:ring-2 focus:ring-gray-300"
              />
            </section>

            {/* Conversation history */}
            <section>
              <div className="flex items-center justify-between">
                <FieldLabel>Conversation history</FieldLabel>
                <button onClick={addTurn} className="text-xs text-gray-400 hover:text-gray-700 transition-colors">
                  + add turn
                </button>
              </div>
              <div className="mt-2 space-y-2">
                {history.map((turn, i) => (
                  <div key={i} className="flex gap-2 items-start">
                    <select
                      value={turn.role}
                      onChange={e => setTurnField(i, 'role', e.target.value)}
                      className="mt-0.5 shrink-0 rounded-md border border-gray-200 bg-white px-2 py-1.5 text-xs text-gray-500 focus:outline-none focus:ring-2 focus:ring-gray-300"
                    >
                      <option value="user">user</option>
                      <option value="assistant">assistant</option>
                    </select>
                    <textarea
                      value={turn.content}
                      onChange={e => setTurnField(i, 'content', e.target.value)}
                      rows={2}
                      placeholder={`${turn.role} message…`}
                      className="flex-1 rounded-lg border border-gray-200 px-3 py-2 text-sm text-gray-800 placeholder:text-gray-300 resize-none focus:outline-none focus:ring-2 focus:ring-gray-300"
                    />
                    {history.length > 1 && (
                      <button
                        onClick={() => removeTurn(i)}
                        aria-label="Remove turn"
                        className="mt-1.5 text-gray-200 hover:text-red-400 transition-colors text-xl leading-none"
                      >
                        ×
                      </button>
                    )}
                  </div>
                ))}
              </div>
            </section>

            {/* Context */}
            <section>
              <div className="flex items-center justify-between">
                <FieldLabel>Context</FieldLabel>
                <button onClick={addCtx} className="text-xs text-gray-400 hover:text-gray-700 transition-colors">
                  + add field
                </button>
              </div>
              <div className="mt-2 space-y-2">
                {ctxEntries.map((entry, i) => (
                  <div key={i} className="flex items-center gap-2">
                    <input
                      type="text"
                      value={entry.key}
                      onChange={e => setCtxField(i, 'key', e.target.value)}
                      placeholder="key"
                      className="w-28 rounded-md border border-gray-200 px-3 py-1.5 text-sm text-gray-700 placeholder:text-gray-300 focus:outline-none focus:ring-2 focus:ring-gray-300"
                    />
                    <span className="text-gray-200 text-sm select-none">→</span>
                    <input
                      type="text"
                      value={entry.value}
                      onChange={e => setCtxField(i, 'value', e.target.value)}
                      placeholder="value"
                      className="flex-1 rounded-md border border-gray-200 px-3 py-1.5 text-sm text-gray-700 placeholder:text-gray-300 focus:outline-none focus:ring-2 focus:ring-gray-300"
                    />
                    {ctxEntries.length > 1 && (
                      <button
                        onClick={() => removeCtx(i)}
                        aria-label="Remove field"
                        className="text-gray-200 hover:text-red-400 transition-colors text-xl leading-none"
                      >
                        ×
                      </button>
                    )}
                  </div>
                ))}
              </div>
            </section>

          </div>

          {/* Submit bar */}
          <div className="shrink-0 px-7 py-4 border-t border-gray-100 bg-white">
            <button
              onClick={submit}
              disabled={loading || !action.trim() || !history.some(t => t.content.trim())}
              className="w-full rounded-lg bg-gray-900 py-2.5 text-sm font-medium text-white transition-colors hover:bg-gray-700 disabled:opacity-35 disabled:cursor-not-allowed"
            >
              {loading ? 'Evaluating…' : 'Evaluate Action →'}
            </button>
          </div>
        </div>

        {/* RIGHT — output */}
        <div className="flex-1 overflow-y-auto px-7 py-6 space-y-4">

          {/* Network error */}
          {apiError && (
            <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
              <span className="font-semibold">Error: </span>{apiError}
            </div>
          )}

          {/* Empty state */}
          {!response && !apiError && !loading && (
            <div className="flex flex-col items-center justify-center h-full gap-3 text-gray-200 select-none">
              <span className="text-5xl">⚖</span>
              <p className="text-sm">Submit an action to see Alfred&apos;s decision</p>
            </div>
          )}

          {/* Loading skeleton */}
          {loading && (
            <div className="animate-pulse space-y-4 pt-2">
              <div className="h-7 w-44 rounded-full bg-gray-100" />
              <div className="h-4 w-full rounded bg-gray-100" />
              <div className="h-4 w-5/6 rounded bg-gray-100" />
              <div className="h-4 w-2/3 rounded bg-gray-100" />
            </div>
          )}

          {response && (
            <>
              {/* Failure banner */}
              {response.failure_mode && (
                <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3.5">
                  <div className="flex items-center gap-2">
                    <span className="text-red-500 text-base">⚠</span>
                    <span className="text-sm font-semibold text-red-800">
                      {FAILURE_LABELS[response.failure_mode] ?? response.failure_mode}
                    </span>
                  </div>
                  <p className="mt-0.5 ml-6 text-xs text-red-600">
                    Safe fallback applied — review before acting on this decision.
                  </p>
                </div>
              )}

              {/* Decision card */}
              <div className="rounded-xl border border-gray-200 bg-white p-5 space-y-4">

                {/* Decision + confidence row */}
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <MicroLabel>Decision</MicroLabel>
                    {meta && (
                      <span className={`mt-1.5 inline-block rounded-full border px-3.5 py-1 text-sm font-semibold ${meta.cls}`}>
                        {meta.label}
                      </span>
                    )}
                  </div>
                  <div className="text-right shrink-0">
                    <MicroLabel>Confidence</MicroLabel>
                    <p className="mt-1 text-3xl font-bold tabular-nums text-gray-900 leading-none">
                      {(response.confidence * 100).toFixed(0)}
                      <span className="text-base font-normal text-gray-400">%</span>
                    </p>
                  </div>
                </div>

                {/* Confidence bar */}
                <div className="h-1.5 w-full rounded-full bg-gray-100 overflow-hidden">
                  <div
                    className="h-full rounded-full bg-gray-800 transition-all duration-700"
                    style={{ width: `${response.confidence * 100}%` }}
                  />
                </div>

                {/* Rationale */}
                <div>
                  <MicroLabel>Rationale</MicroLabel>
                  <p className="mt-1.5 text-sm text-gray-700 leading-relaxed">{response.rationale}</p>
                </div>
              </div>

              {/* Under the Hood */}
              <div className="rounded-xl border border-gray-200 bg-white overflow-hidden">
                <button
                  onClick={() => setHoodOpen(o => !o)}
                  className="w-full flex items-center justify-between px-5 py-3.5 text-sm font-medium text-gray-600 hover:bg-gray-50 transition-colors"
                >
                  <span>Under the Hood</span>
                  <span className="text-xs text-gray-400">{hoodOpen ? '▲ hide' : '▼ show'}</span>
                </button>

                {hoodOpen && (
                  <div className="border-t border-gray-100 divide-y divide-gray-100">

                    {/* Signals */}
                    <div className="px-5 py-4 space-y-3">
                      <MicroLabel>Computed Signals</MicroLabel>
                      <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-2.5">
                        {(Object.entries(response.computed_signals) as [string, unknown][]).map(([k, v]) => (
                          <div key={k} className="flex items-center gap-2">
                            <span className="text-xs text-gray-400 w-36 shrink-0">{k}</span>
                            <SignalValue name={k} value={v} />
                          </div>
                        ))}
                      </div>
                    </div>

                    {/* Prompt */}
                    <div className="px-5 py-4 space-y-2">
                      <MicroLabel>Prompt Sent</MicroLabel>
                      <ScrollBox>{response.prompt_sent}</ScrollBox>
                    </div>

                    {/* Raw output */}
                    <div className="px-5 py-4 space-y-2">
                      <MicroLabel>Raw Model Output</MicroLabel>
                      <ScrollBox>{response.raw_model_output}</ScrollBox>
                    </div>

                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Shared micro-components ───────────────────────────────────────────────────

function FieldLabel({ children }: { children: ReactNode }) {
  return (
    <p className="text-[10px] font-semibold uppercase tracking-widest text-gray-400">{children}</p>
  );
}

function MicroLabel({ children }: { children: ReactNode }) {
  return (
    <p className="text-[10px] font-semibold uppercase tracking-widest text-gray-400">{children}</p>
  );
}

function Divider() {
  return (
    <div className="flex items-center gap-3 text-xs text-gray-300 select-none">
      <div className="flex-1 h-px bg-gray-100" />
      or fill in manually
      <div className="flex-1 h-px bg-gray-100" />
    </div>
  );
}

function ScrollBox({ children }: { children: ReactNode }) {
  return (
    <pre className="rounded-md border border-gray-100 bg-gray-50 px-3.5 py-3 text-xs font-mono text-gray-600 leading-relaxed whitespace-pre-wrap break-words max-h-60 overflow-y-auto">
      {children}
    </pre>
  );
}

// ── Signal value renderer ─────────────────────────────────────────────────────

function SignalValue({ name, value }: { name: string; value: unknown }) {
  if (typeof value === 'boolean') {
    return (
      <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${value ? 'bg-green-50 text-green-700' : 'bg-gray-100 text-gray-500'}`}>
        {value ? 'true' : 'false'}
      </span>
    );
  }

  if (name === 'risk_score' && typeof value === 'number') {
    const pct = Math.round(value * 100);
    const bar = value < 0.3 ? 'bg-green-500' : value < 0.6 ? 'bg-amber-400' : 'bg-red-500';
    return (
      <div className="flex items-center gap-2">
        <div className="w-20 h-1.5 rounded-full bg-gray-100 overflow-hidden">
          <div className={`h-full rounded-full ${bar}`} style={{ width: `${pct}%` }} />
        </div>
        <span className="text-xs text-gray-600 tabular-nums">{value.toFixed(2)}</span>
      </div>
    );
  }

  if (name === 'reversibility') {
    const cls =
      value === 'reversible'   ? 'bg-green-50 text-green-700'  :
      value === 'irreversible' ? 'bg-red-50 text-red-700'      :
                                 'bg-gray-100 text-gray-500';
    return (
      <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${cls}`}>
        {String(value)}
      </span>
    );
  }

  if (Array.isArray(value)) {
    return <span className="text-xs text-gray-600">{value.length ? value.join(', ') : '—'}</span>;
  }

  if (value !== null && typeof value === 'object') {
    return <span className="text-xs text-gray-600 font-mono">{JSON.stringify(value)}</span>;
  }

  return <span className="text-xs text-gray-600">{String(value)}</span>;
}
