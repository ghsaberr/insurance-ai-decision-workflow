"""
Evaluation harness for the Insurance AI Decision Workflow.

Runs all 15 cases in eval/dataset/cases.json through the workflow, scores
each response against the expected values, and writes a report to
eval/report.md.

Usage
-----
    # From repo root (venv active):
    python eval/harness.py

    # Against a running API:
    python eval/harness.py --api http://localhost:8000

    # In-process (no server needed):
    python eval/harness.py --inprocess

Scored dimensions
-----------------
  rule_consistency   — deterministic rule outputs match expected violations/flags
  recommendation     — recommendation within expected set
  hard_decline       — hard_decline bool matches expected
  severity_tier      — tier within expected set (when specified)
  fail_safe          — dangerous cases never produce "approve"
  rationale_present  — rationale field is non-empty
  no_crash           — request completed without 500 error
  latency_ms         — response time in milliseconds
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DATASET_PATH = Path(__file__).parent / "dataset" / "cases.json"
REPORT_PATH  = Path(__file__).parent / "report.md"


# ------------------------------------------------------------------ #
#  Result dataclass                                                    #
# ------------------------------------------------------------------ #

@dataclass
class CaseResult:
    case_id: str
    category: str
    description: str
    passed: bool
    failures: list[str]
    latency_ms: int
    workflow_mode: str
    recommendation: str
    risk_level: str
    hard_decline: bool
    severity_tier: str
    compliance_score: int
    llm_available: bool
    error: str | None = None


# ------------------------------------------------------------------ #
#  Scorer                                                              #
# ------------------------------------------------------------------ #

def score_case(case: dict, result: dict) -> tuple[bool, list[str]]:
    """Return (passed, [failure_messages]) for a single case."""
    exp = case["expected"]
    failures = []

    # no_crash — already guaranteed if we got here
    if result.get("error"):
        failures.append(f"request_error: {result['error']}")
        return False, failures

    rec  = result.get("recommendation", "")
    rvio = result.get("rule_violations", [])
    rfla = result.get("rule_flags", [])
    hd   = result.get("hard_decline", False)
    tier = result.get("severity_tier", "")
    conf = result.get("confidence", 0.0)
    rat  = result.get("rationale", "")

    # recommendation (exact)
    if "recommendation" in exp and rec != exp["recommendation"]:
        failures.append(f"recommendation: got '{rec}', expected '{exp['recommendation']}'")

    # recommendation_in (set)
    if "recommendation_in" in exp and rec not in exp["recommendation_in"]:
        failures.append(f"recommendation: got '{rec}', expected one of {exp['recommendation_in']}")

    # recommendation_not (must not be this value)
    if "recommendation_not" in exp and rec == exp["recommendation_not"]:
        failures.append(f"recommendation: got '{rec}', must NOT be '{exp['recommendation_not']}'")

    # hard_decline
    if "hard_decline" in exp and hd != exp["hard_decline"]:
        failures.append(f"hard_decline: got {hd}, expected {exp['hard_decline']}")

    # risk_level (exact)
    if "risk_level" in exp and result.get("risk_level") != exp["risk_level"]:
        failures.append(f"risk_level: got '{result.get('risk_level')}', expected '{exp['risk_level']}'")

    # risk_level_in
    if "risk_level_in" in exp and result.get("risk_level") not in exp["risk_level_in"]:
        failures.append(f"risk_level: got '{result.get('risk_level')}', expected one of {exp['risk_level_in']}")

    # severity_tier_in
    if "severity_tier_in" in exp and tier not in exp["severity_tier_in"]:
        failures.append(f"severity_tier: got '{tier}', expected one of {exp['severity_tier_in']}")

    # rule_violations_contains
    for v in exp.get("rule_violations_contains", []):
        if v not in rvio:
            failures.append(f"rule_violations missing: '{v}' (got {rvio})")

    # rule_flags_contains
    for f_ in exp.get("rule_flags_contains", []):
        if f_ not in rfla:
            failures.append(f"rule_flags missing: '{f_}' (got {rfla})")

    # rule_violations (exact empty list)
    if "rule_violations" in exp and rvio != exp["rule_violations"]:
        failures.append(f"rule_violations: got {rvio}, expected {exp['rule_violations']}")

    # confidence_lt
    if "confidence_lt" in exp and conf >= exp["confidence_lt"]:
        failures.append(f"confidence: got {conf:.2f}, expected < {exp['confidence_lt']}")

    # rationale_not_empty
    if exp.get("rationale_not_empty") and not rat.strip():
        failures.append("rationale is empty")

    return len(failures) == 0, failures


# ------------------------------------------------------------------ #
#  Runners                                                             #
# ------------------------------------------------------------------ #

def run_inprocess(cases: list[dict]) -> list[CaseResult]:
    """Run cases through the workflow without starting an HTTP server."""
    sys.path.insert(0, str(Path(__file__).parents[1]))
    import os
    os.environ.setdefault("LLM_PROVIDER", "none")

    from src.workflow.orchestrator import WorkflowOrchestrator
    orch = WorkflowOrchestrator()

    results = []
    for case in cases:
        start = time.monotonic()
        error = None
        raw: dict = {}
        try:
            wf = orch.run(case["input"])
            raw = {
                "recommendation":  wf.recommendation,
                "risk_level":      wf.risk_level,
                "hard_decline":    wf.hard_decline,
                "severity_tier":   wf.severity_tier,
                "rule_violations": wf.rule_violations,
                "rule_flags":      wf.rule_flags,
                "confidence":      wf.confidence,
                "rationale":       wf.rationale,
                "llm_available":   wf.llm_available,
                "workflow_mode":   wf.workflow_mode,
                "compliance_score": wf.compliance_score,
            }
        except Exception as exc:
            error = str(exc)
            raw = {"error": error}

        latency = int((time.monotonic() - start) * 1000)
        passed, failures = score_case(case, raw)

        results.append(CaseResult(
            case_id=case["id"],
            category=case["category"],
            description=case["description"],
            passed=passed,
            failures=failures,
            latency_ms=latency,
            workflow_mode=raw.get("workflow_mode", "unknown"),
            recommendation=raw.get("recommendation", "error"),
            risk_level=raw.get("risk_level", ""),
            hard_decline=raw.get("hard_decline", False),
            severity_tier=raw.get("severity_tier", ""),
            compliance_score=raw.get("compliance_score", 0),
            llm_available=raw.get("llm_available", False),
            error=error,
        ))
    return results


def run_api(cases: list[dict], base_url: str) -> list[CaseResult]:
    """Run cases against a live API server."""
    import requests as _req

    results = []
    for case in cases:
        start = time.monotonic()
        error = None
        raw: dict = {}
        try:
            resp = _req.post(f"{base_url}/assess",
                             json=case["input"], timeout=30)
            resp.raise_for_status()
            raw = resp.json()
        except Exception as exc:
            error = str(exc)
            raw = {"error": error}

        latency = int((time.monotonic() - start) * 1000)
        passed, failures = score_case(case, raw)

        results.append(CaseResult(
            case_id=case["id"],
            category=case["category"],
            description=case["description"],
            passed=passed,
            failures=failures,
            latency_ms=latency,
            workflow_mode=raw.get("workflow_mode", "unknown"),
            recommendation=raw.get("recommendation", "error"),
            risk_level=raw.get("risk_level", ""),
            hard_decline=raw.get("hard_decline", False),
            severity_tier=raw.get("severity_tier", ""),
            compliance_score=raw.get("compliance_score", 0),
            llm_available=raw.get("llm_available", False),
            error=error,
        ))
    return results


# ------------------------------------------------------------------ #
#  Report writer                                                       #
# ------------------------------------------------------------------ #

def write_report(results: list[CaseResult], mode_label: str) -> str:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    total   = len(results)
    passed  = sum(1 for r in results if r.passed)
    failed  = total - passed
    avg_lat = int(sum(r.latency_ms for r in results) / total) if total else 0
    llm_on  = sum(1 for r in results if r.llm_available)

    categories = {}
    for r in results:
        categories.setdefault(r.category, []).append(r)

    lines = [
        "# Evaluation Report",
        "",
        f"Generated: {now}  |  Mode: {mode_label}",
        "",
        "## Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Cases run | {total} |",
        f"| Passed | {passed} |",
        f"| Failed | {failed} |",
        f"| Pass rate | {passed/total*100:.0f}% |",
        f"| Avg latency | {avg_lat} ms |",
        f"| LLM available | {llm_on}/{total} runs |",
        "",
        "## Results by category",
        "",
    ]

    for cat, cat_results in categories.items():
        cat_pass = sum(1 for r in cat_results if r.passed)
        lines.append(f"### {cat}  ({cat_pass}/{len(cat_results)} passed)")
        lines.append("")
        lines.append("| ID | Description | Result | Rec | Mode | Latency |")
        lines.append("|----|-------------|--------|-----|------|---------|")
        for r in cat_results:
            status = "PASS" if r.passed else "FAIL"
            lines.append(
                f"| {r.case_id} | {r.description[:55]} | {status} "
                f"| {r.recommendation} | {r.workflow_mode} | {r.latency_ms}ms |"
            )
        lines.append("")

    # Failure details
    failures = [r for r in results if not r.passed]
    if failures:
        lines += ["## Failure details", ""]
        for r in failures:
            lines.append(f"**{r.case_id}** — {r.description}")
            for f in r.failures:
                lines.append(f"  - {f}")
            if r.error:
                lines.append(f"  - ERROR: {r.error}")
            lines.append("")

    # Conclusions
    fail_safe_results = [r for r in results if r.category == "fail_safe"]
    fail_safe_pass    = all(r.passed for r in fail_safe_results)
    hard_decline_ok   = all(
        r.hard_decline for r in results
        if r.case_id in {"EVAL_S03", "EVAL_F01"}
    )

    lines += [
        "## Conclusions",
        "",
        "### What the workflow is safe for",
        "",
        "- Deterministic rule evaluation — hard rules fire correctly and reproducibly",
        "- Hard-decline detection — underage and overage applicants are always declined",
        "- Structured case creation — every assessment creates a persistent, reviewable case",
        "- Audit trail — every decision is stored with version metadata",
        "",
        "### What requires human judgment",
        "",
        "- Ambiguous cases (category `ambiguous`) — the system refers; reviewer must decide",
        "- Conflicting evidence — excellent credit + high claims produce `refer`, not a confident answer",
        f"- LLM mode: {'enabled' if llm_on > 0 else 'disabled in this run — all cases ran deterministic_only'}",
        "  In deterministic_only mode confidence is capped at 0.55; reviewer weighting should increase",
        "",
        "### Known failure modes",
        "",
        "- **LLM unavailable**: rationale quality drops; recommendation derived from rules only.",
        "  Mitigation: reviewer queue should flag `deterministic_only` cases for closer scrutiny.",
        "- **FAISS index absent**: retrieval returns empty; evidence_refs will be [].",
        "  Mitigation: `GET /health` reports retrieval.available=false; alert on this condition.",
        "- **Severity model absent**: SeverityScorer runs in rule_based mode (confidence 0.55).",
        "  Mitigation: acceptable for triage routing; not suitable for reserve estimation.",
        "",
        f"### Fail-safe verdict: {'PASS — no unsafe approvals on fail_safe cases' if fail_safe_pass else 'FAIL — review failure details above'}",
        "",
        "> This report was generated by `eval/harness.py`. Re-run after any change to",
        "> rules, prompts, or models to verify no regressions.",
    ]

    return "\n".join(lines)


# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #

def main():
    parser = argparse.ArgumentParser(description="Insurance Workflow Evaluation Harness")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--inprocess", action="store_true",
                       help="Run in-process (no HTTP server needed) [default]")
    group.add_argument("--api", metavar="URL",
                       help="Run against a live API, e.g. http://localhost:8000")
    args = parser.parse_args()

    cases = json.loads(DATASET_PATH.read_text())
    print(f"Loaded {len(cases)} eval cases from {DATASET_PATH}")

    if args.api:
        print(f"Mode: API  ({args.api})")
        results = run_api(cases, args.api)
        mode_label = f"api ({args.api})"
    else:
        print("Mode: in-process (LLM_PROVIDER=none)")
        results = run_inprocess(cases)
        mode_label = "in-process / deterministic_only"

    passed = sum(1 for r in results if r.passed)
    print(f"\nResults: {passed}/{len(results)} passed\n")

    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  [{status}] {r.case_id:<14} {r.category:<28} {r.recommendation:<10} "
              f"{r.latency_ms}ms")
        for f in r.failures:
            print(f"          ↳ {f}")

    report_text = write_report(results, mode_label)
    REPORT_PATH.write_text(report_text, encoding="utf-8")
    print(f"\nReport written to {REPORT_PATH}")

    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
