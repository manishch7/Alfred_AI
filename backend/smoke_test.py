#!/usr/bin/env python3
"""
smoke_test.py – Runs all 6 preloaded scenarios through compute_signals and
build_prompt without making any LLM call, prints the full pipeline output for
each, then runs 3 signal-level assertions.

Run from /backend:
    python smoke_test.py
"""

import sys

from app.prompt_builder import build_prompt
from app.scenarios import SCENARIOS
from app.signals import compute_signals

# ── Terminal formatting ───────────────────────────────────────────────────────

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

DIFF_COLOR = {"easy": GREEN, "medium": YELLOW, "hard": RED}

W = 74  # separator width


def sep(char: str = "─") -> None:
    print(char * W)


def header(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}")


def _assert(label: str, condition: bool, note: str = "") -> bool:
    tag    = f"{GREEN}PASS{RESET}" if condition else f"{RED}FAIL{RESET}"
    suffix = f"  {DIM}# {note}{RESET}" if note else ""
    print(f"  {tag}  {label}{suffix}")
    return condition


# ── Pipeline run ──────────────────────────────────────────────────────────────

header(f"Alfred — Smoke Test  ({len(SCENARIOS)} scenarios, no LLM call)")
sep("═")

pipeline: dict[str, dict] = {}

for s in SCENARIOS:
    signals: dict = compute_signals(s["action"], s["conversation_history"], s["context"])
    prompt:  str  = build_prompt(s["action"], s["conversation_history"], signals)

    pipeline[s["id"]] = {"scenario": s, "signals": signals, "prompt": prompt}

    dc = DIFF_COLOR.get(s["difficulty"], "")
    print(f"\n{BOLD}{s['label']}{RESET}  {DIM}[{s['id']}]{RESET}")
    print(f"  difficulty    : {dc}{s['difficulty']}{RESET}")
    print(f"  action        : {s['action']}")

    # ── Signals ──────────────────────────────────────────────────────────────
    print(f"\n  computed_signals")
    for k, v in signals.items():
        # Colour the two boolean signals for quick scanning.
        if isinstance(v, bool):
            vc = (GREEN if v else DIM) + str(v).lower() + RESET
        elif k == "risk_score":
            color = GREEN if v < 0.3 else (YELLOW if v < 0.6 else RED)
            vc = f"{color}{v:.4f}{RESET}"
        else:
            vc = str(v)
        print(f"    {k:<22}: {vc}")

    # ── Prompt preview ────────────────────────────────────────────────────────
    lines   = prompt.splitlines()
    preview = 10
    print(f"\n  prompt  ({len(lines)} lines total — first {preview} shown)")
    for line in lines[:preview]:
        print(f"    {line}")
    if len(lines) > preview:
        print(f"    {DIM}… {len(lines) - preview} more lines{RESET}")

    # ── Conversation history summary ──────────────────────────────────────────
    hist = s["conversation_history"]
    print(f"\n  history ({len(hist)} turn{'s' if len(hist) != 1 else ''})")
    for msg in hist:
        role    = msg.get("role", "?") if isinstance(msg, dict) else "?"
        content = msg.get("content", str(msg)) if isinstance(msg, dict) else str(msg)
        snippet = content[:80] + ("…" if len(content) > 80 else "")
        print(f"    {role:<10}: {snippet}")

    print()
    sep()

# ── Assertions ────────────────────────────────────────────────────────────────

header("Assertions")
print()

unexpected: list[str] = []   # failures that indicate a real regression
expected:   list[str] = []   # failures that document a known limitation


# ── Assertion 1 ──────────────────────────────────────────────────────────────
# The "Yep send it" adversarial scenario (discount_email_legal_hold).
#
# The last user turn is "Yep send it." — tokenises to {"yep", "send", "it"}.
# None of those tokens appear in _OVERRIDE_SIGNALS, and no multi-word override
# phrase ("do not", "never mind", "scratch that") is present either.
# _detect_conflict therefore returns False.
#
# This assertion is EXPECTED TO FAIL.  It documents the exact boundary where
# deterministic signal computation stops and LLM reasoning must take over:
# the heuristic cannot catch an adversarial approval that follows an uncleared
# legal hold because the final turn contains no linguistic override marker.
sig_legal = pipeline["discount_email_legal_hold"]["signals"]
ok = _assert(
    "discount_email_legal_hold  →  conflict_detected is True",
    sig_legal["conflict_detected"] is True,
    note="EXPECTED FAIL — 'Yep send it' has no override keyword; LLM must catch this",
)
if not ok:
    expected.append("discount_email_legal_hold: conflict_detected")


# ── Assertion 2 ──────────────────────────────────────────────────────────────
# Ambiguous email scenario with a missing subject slot (email_vague_subject).
#
# The user answered "just something professional — you decide" when asked for a
# subject line.  Because 'subject' is absent from context, missing_entities
# must contain it and intent_resolved must be False.
sig_vague = pipeline["email_vague_subject"]["signals"]
ok = _assert(
    "email_vague_subject        →  missing_entities is non-empty",
    len(sig_vague["missing_entities"]) > 0,
)
if not ok:
    unexpected.append("email_vague_subject: missing_entities")


# ── Assertion 3 ──────────────────────────────────────────────────────────────
# High-risk financial scenario (ceo_wire_bypass_approval).
#
# Score breakdown: financial base (0.75) + irreversibility penalty (0.10) = 0.85.
# No missing entities (recipient + amount both present), no detected conflict.
# 0.85 must exceed the 0.7 threshold with clear margin.
sig_ceo = pipeline["ceo_wire_bypass_approval"]["signals"]
ok = _assert(
    "ceo_wire_bypass_approval   →  risk_score > 0.70",
    sig_ceo["risk_score"] > 0.70,
    note=f"actual: {sig_ceo['risk_score']:.4f}",
)
if not ok:
    unexpected.append("ceo_wire_bypass_approval: risk_score")


# ── Summary ───────────────────────────────────────────────────────────────────

total      = 3
n_expected = len(expected)
n_surprise = len(unexpected)
n_pass     = total - n_expected - n_surprise

print()
sep("═")

if n_surprise == 0 and n_expected == 1:
    print(
        f"\n  {GREEN}{BOLD}{n_pass}/{total} passed{RESET}"
        f"  ·  1 expected failure  ·  0 regressions\n"
    )
    print(
        f"  {DIM}The expected failure (conflict_detected on 'Yep send it') is a "
        f"documented\n"
        f"  heuristic limitation, not a bug.  The LLM reasoning layer handles "
        f"it.\n{RESET}"
    )
elif n_surprise == 0 and n_expected == 0:
    print(f"\n  {GREEN}{BOLD}{total}/{total} passed{RESET}  ·  0 regressions\n")
else:
    print(f"\n  {RED}{BOLD}{n_surprise} unexpected failure(s){RESET}  — see details above\n")
    for label in unexpected:
        print(f"    {RED}✗{RESET}  {label}")
    print()

sys.exit(1 if unexpected else 0)
