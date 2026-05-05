"""
Day 3 smoke test: CRITICAL auto-escalation + FAISS round-trip.

Run from repo root with venv active:
    python tests/smoke_day3.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("LLM_PROVIDER", "none")

from src.review.case_manager import CaseManager, ReviewRequest
from src.workflow.orchestrator import WorkflowOrchestrator

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
failures = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [{PASS}] {label}")
    else:
        msg = f"{label}" + (f": {detail}" if detail else "")
        print(f"  [{FAIL}] {msg}")
        failures.append(msg)


# ------------------------------------------------------------------ #
#  Test 1 — CRITICAL auto-escalation                                   #
# ------------------------------------------------------------------ #
print("\n--- Test 1: CRITICAL auto-escalation ---")

with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
    db_path = f.name

cm = CaseManager(db_path=db_path)

case = cm.create_case(
    request_id="smoke-req-001",
    policyholder_id_hashed="hashed-abc",
    recommendation={"recommendation": "refer"},
    evidence_refs=[],
    rule_findings={},
    severity_tier="CRITICAL",
    versions={"model_version": "test", "prompt_version": "v1",
               "kb_version": "test", "rules_version": "v1"},
    llm_available=False,
    workflow_mode="deterministic_only",
)

check("status == escalated", case.status == "escalated",
      f"got '{case.status}'")

events = cm.get_case_events(case.case_id)
event_types = [e["event_type"] for e in events]
check("auto_escalated event logged", "auto_escalated" in event_types,
      f"events: {event_types}")

# ------------------------------------------------------------------ #
#  Test 2 — NON-CRITICAL stays pending_review                          #
# ------------------------------------------------------------------ #
print("\n--- Test 2: Non-CRITICAL stays pending_review ---")

case2 = cm.create_case(
    request_id="smoke-req-002",
    policyholder_id_hashed="hashed-def",
    recommendation={"recommendation": "approve"},
    evidence_refs=[],
    rule_findings={},
    severity_tier="LOW",
    versions={"model_version": "test", "prompt_version": "v1",
               "kb_version": "test", "rules_version": "v1"},
    llm_available=False,
    workflow_mode="deterministic_only",
)

check("status == pending_review", case2.status == "pending_review",
      f"got '{case2.status}'")

events2 = cm.get_case_events(case2.case_id)
event_types2 = [e["event_type"] for e in events2]
check("no auto_escalated event", "auto_escalated" not in event_types2,
      f"events: {event_types2}")

# ------------------------------------------------------------------ #
#  Test 3 — Review round-trip (approve)                               #
# ------------------------------------------------------------------ #
print("\n--- Test 3: Review round-trip (approve) ---")

updated = cm.submit_review(
    case2.case_id,
    ReviewRequest(reviewer_id="reviewer-1", action="approve", notes="Looks good"),
)

check("status == approved after approve", updated.status == "approved",
      f"got '{updated.status}'")
check("reviewer_id stored", updated.reviewer_id == "reviewer-1",
      f"got '{updated.reviewer_id}'")

# terminal status guard
blocked = False
try:
    cm.submit_review(case2.case_id,
                     ReviewRequest(reviewer_id="r2", action="reject", notes=""))
except ValueError:
    blocked = True
check("terminal status blocks re-review", blocked)

# ------------------------------------------------------------------ #
#  Test 4 — Full orchestrator round-trip (FAISS live if available)    #
# ------------------------------------------------------------------ #
print("\n--- Test 4: Orchestrator round-trip ---")

orch = WorkflowOrchestrator()

sample_input = {
    "policyholder_id": "PH-SMOKE-001",
    "age": 35,
    "annual_income": 75000,
    "credit_score": 720,
    "claims_history": [{"date": "2023-06-01", "amount": 1500, "type": "minor"}],
    "policy_type": "auto",
    "premium_amount": 1200,
    "query": "comprehensive auto insurance claim history",
}

result = orch.run(sample_input)

check("recommendation non-empty", bool(result.recommendation),
      f"got '{result.recommendation}'")
check("risk_score in 0-100", 0 <= result.risk_score <= 100,
      f"got {result.risk_score}")
check("severity_tier set", result.severity_tier in {"LOW", "MEDIUM", "HIGH", "CRITICAL"},
      f"got '{result.severity_tier}'")
check("workflow_mode declared", result.workflow_mode in {"full", "deterministic_only", "hard_decline"},
      f"got '{result.workflow_mode}'")
check("llm_available is bool", isinstance(result.llm_available, bool))

if result.evidence_refs:
    print(f"  [info] FAISS returned {len(result.evidence_refs)} evidence refs (index live)")
else:
    print(f"  [info] evidence_refs empty (FAISS index not found — degraded mode)")

# ------------------------------------------------------------------ #
#  Summary                                                             #
# ------------------------------------------------------------------ #
print()
if failures:
    print(f"FAILED — {len(failures)} check(s) failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All checks passed.")
    sys.exit(0)
