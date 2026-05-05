"""
WorkflowOrchestrator — the central pipeline.

Execution order
---------------
1. Deterministic tools   RiskCalculator + RuleChecker  (always run)
2. Severity scoring      SeverityScorer                (always run)
3. Document retrieval    DocumentLoader / FAISS         (always run)
4. LLM synthesis         Anthropic Claude / Ollama      (optional)
5. Score blending        weighted combination           (always run)
6. Result assembly       WorkflowResult with versions

Modes
-----
full               — all steps including LLM
deterministic_only — steps 1-3 + 5-6, no LLM; recommendation derived from
                     deterministic outputs alone.  This is NOT a mock — it is
                     an honest operational mode surfaced in every response.

No silent fallbacks.  If the LLM fails the orchestrator logs the reason,
sets llm_available=False, and runs in deterministic_only mode.
"""

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from src.audit.versions import VersionManifest, build_manifest
from src.ingestion.document_loader import DocumentLoader, RetrievedDoc
from src.privacy.pii_handler import mask_for_storage, strip_pii_from_request
from src.severity.severity_scorer import SeverityResult, SeverityScorer
from src.tools.risk_calculator import RiskCalcResult, RiskCalculator
from src.tools.rule_checker import RuleCheckResult, RuleChecker

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are an underwriting decision-support system.
Your role is to synthesise deterministic risk signals and retrieved policy evidence
into a structured recommendation.  You do NOT make final decisions — a licensed
human reviewer makes every final underwriting decision.

IMPORTANT CONSTRAINTS
- Only cite evidence that appears in the retrieved documents provided.
- If evidence is insufficient, say so explicitly.
- Never fabricate policy numbers, amounts, or rule citations.
- Always surface uncertainty rather than manufacturing false confidence.

Output valid JSON matching this schema exactly:
{
  "recommendation": "approve" | "decline" | "refer" | "insufficient_evidence",
  "rationale": "<concise explanation citing specific evidence>",
  "confidence": <float 0.0–1.0>,
  "evidence_citations": ["<doc_id>", ...],
  "rule_conflicts": ["<description of any conflicting signals>"],
  "reviewer_guidance": "<what the reviewer should focus on>"
}"""


@dataclass
class WorkflowResult:
    # Core recommendation
    recommendation: str           # approve | decline | refer | insufficient_evidence
    rationale: str
    confidence: float

    # Scores
    risk_score: float
    risk_level: str               # Low | Medium | High
    severity_tier: str            # LOW | MEDIUM | HIGH | CRITICAL
    severity_estimated_cost: float
    severity_note: str

    # Evidence
    evidence_refs: list[str]
    evidence_count: int

    # Rule findings
    rule_flags: list[str]
    rule_actions: list[str]
    rule_violations: list[str]
    compliance_score: int
    hard_decline: bool

    # System metadata
    llm_available: bool
    workflow_mode: str            # full | deterministic_only
    latency_ms: int
    versions: dict

    # Internals for case creation
    _rule_findings_raw: dict = field(default_factory=dict, repr=False)
    _risk_calc_raw: dict = field(default_factory=dict, repr=False)


class WorkflowOrchestrator:
    """
    Singleton-safe orchestrator.  Initialise once at application startup
    and reuse across requests.
    """

    def __init__(self):
        self._risk_calc = RiskCalculator()
        self._rule_checker = RuleChecker()
        self._severity = SeverityScorer()
        self._retriever = DocumentLoader()
        self._llm_client = _build_llm_client()

        logger.info(
            "WorkflowOrchestrator ready: retrieval=%s severity=%s llm=%s",
            "available" if self._retriever.is_available else "unavailable",
            self._severity.mode,
            type(self._llm_client).__name__,
        )

    # ------------------------------------------------------------------ #
    #  Public                                                              #
    # ------------------------------------------------------------------ #

    def run(self, request_data: dict[str, Any]) -> WorkflowResult:
        start = time.monotonic()

        # 1 — Deterministic tools
        risk_result: RiskCalcResult = self._risk_calc.calculate(request_data)
        rule_result: RuleCheckResult = self._rule_checker.check_all(request_data)

        # Hard decline short-circuit — no LLM needed, result is certain
        if rule_result.hard_decline:
            return self._hard_decline_result(
                risk_result, rule_result, start, request_data
            )

        # 2 — Severity scoring
        severity: SeverityResult = self._severity.score(request_data)

        # 3 — Document retrieval
        query = _build_retrieval_query(request_data)
        docs: list[RetrievedDoc] = self._retriever.retrieve(query, top_k=4)

        # 4 — LLM synthesis (optional)
        llm_output, llm_available = self._call_llm(request_data, risk_result,
                                                    rule_result, severity, docs)

        # 5 — Blend scores
        final_score, final_level = _blend_scores(
            risk_result.score,
            llm_output.get("llm_risk_score"),
            rule_result.violations,
            request_data,
        )

        # 6 — Assemble result
        mode = "full" if llm_available else "deterministic_only"
        versions = build_manifest(self._retriever.kb_version).to_dict()

        result = WorkflowResult(
            recommendation=llm_output.get("recommendation", _rule_recommendation(rule_result, final_score)),
            rationale=llm_output.get("rationale", _rule_rationale(risk_result, rule_result, final_score)),
            confidence=llm_output.get("confidence", 0.55 if not llm_available else 0.75),
            risk_score=round(final_score, 1),
            risk_level=final_level,
            severity_tier=severity.tier,
            severity_estimated_cost=severity.estimated_cost_usd,
            severity_note=severity.business_note,
            evidence_refs=[d.doc_id for d in docs],
            evidence_count=len(docs),
            rule_flags=rule_result.flags,
            rule_actions=rule_result.actions,
            rule_violations=rule_result.violations,
            compliance_score=rule_result.compliance_score,
            hard_decline=False,
            llm_available=llm_available,
            workflow_mode=mode,
            latency_ms=int((time.monotonic() - start) * 1000),
            versions=versions,
            _rule_findings_raw={
                "flags": rule_result.flags,
                "actions": rule_result.actions,
                "violations": rule_result.violations,
                "compliance_score": rule_result.compliance_score,
            },
            _risk_calc_raw={"score": risk_result.score, "breakdown": risk_result.breakdown},
        )

        logger.info(
            "Workflow complete: mode=%s score=%.1f level=%s severity=%s "
            "recommendation=%s llm=%s latency=%dms",
            mode, final_score, final_level, severity.tier,
            result.recommendation, llm_available, result.latency_ms,
        )
        return result

    def health(self) -> dict[str, Any]:
        return {
            "retrieval": self._retriever.health(),
            "severity_mode": self._severity.mode,
            "llm_provider": type(self._llm_client).__name__,
            "llm_available": self._llm_client.is_available(),
        }

    # ------------------------------------------------------------------ #
    #  LLM call (isolated so failures cannot corrupt deterministic path)  #
    # ------------------------------------------------------------------ #

    def _call_llm(
        self,
        data: dict,
        risk: RiskCalcResult,
        rules: RuleCheckResult,
        severity: SeverityResult,
        docs: list[RetrievedDoc],
    ) -> tuple[dict, bool]:
        if not self._llm_client.is_available():
            return {}, False
        try:
            pii_safe = strip_pii_from_request(data)
            user_message = _build_user_message(pii_safe, risk, rules, severity, docs)
            raw = self._llm_client.complete(_SYSTEM_PROMPT, user_message)
            parsed = _parse_llm_json(raw)
            return parsed, True
        except Exception:
            logger.exception("LLM call failed — continuing in deterministic_only mode")
            return {}, False

    def _hard_decline_result(
        self,
        risk: RiskCalcResult,
        rules: RuleCheckResult,
        start: float,
        data: dict,
    ) -> WorkflowResult:
        versions = build_manifest(self._retriever.kb_version).to_dict()
        severity = self._severity.score(data)
        rationale = (
            f"Hard decline triggered by rule violations: "
            f"{', '.join(rules.violations)}. "
            "This application does not meet minimum eligibility requirements."
        )
        return WorkflowResult(
            recommendation="decline",
            rationale=rationale,
            confidence=1.0,
            risk_score=round(risk.score, 1),
            risk_level=risk.level,
            severity_tier=severity.tier,
            severity_estimated_cost=severity.estimated_cost_usd,
            severity_note=severity.business_note,
            evidence_refs=[],
            evidence_count=0,
            rule_flags=rules.flags,
            rule_actions=rules.actions,
            rule_violations=rules.violations,
            compliance_score=rules.compliance_score,
            hard_decline=True,
            llm_available=False,
            workflow_mode="hard_decline",
            latency_ms=int((time.monotonic() - start) * 1000),
            versions=versions,
            _rule_findings_raw={"flags": rules.flags, "violations": rules.violations,
                                 "compliance_score": rules.compliance_score},
            _risk_calc_raw={"score": risk.score},
        )


# ------------------------------------------------------------------ #
#  LLM client abstraction                                             #
# ------------------------------------------------------------------ #

class _AnthropicClient:
    """Anthropic Claude via official SDK with prompt caching."""

    def __init__(self, model: str):
        self.model = model
        try:
            import anthropic
            self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            self._ok = True
            logger.info("LLM: Anthropic %s", model)
        except (ImportError, KeyError):
            self._client = None
            self._ok = False
            logger.info("LLM: Anthropic unavailable (no SDK or missing API key)")

    def is_available(self) -> bool:
        return self._ok

    def complete(self, system_prompt: str, user_message: str) -> str:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},   # cache stable system prompt
                }
            ],
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text


class _OllamaClient:
    """Ollama local LLM."""

    def __init__(self, model: str, base_url: str):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._ok = self._ping()

    def _ping(self) -> bool:
        try:
            import requests
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            available = r.status_code == 200
            logger.info("LLM: Ollama %s — %s", self.model,
                        "available" if available else "not responding")
            return available
        except Exception:
            logger.info("LLM: Ollama not reachable at %s", self.base_url)
            return False

    def is_available(self) -> bool:
        return self._ok

    def complete(self, system_prompt: str, user_message: str) -> str:
        import requests
        full_prompt = f"SYSTEM:\n{system_prompt}\n\nUSER:\n{user_message}"
        resp = requests.post(
            f"{self.base_url}/api/generate",
            json={"model": self.model, "prompt": full_prompt,
                  "stream": False, "options": {"temperature": 0.1}},
            timeout=300,
        )
        resp.raise_for_status()
        return resp.json()["response"]


class _NoLLM:
    """Sentinel: no LLM configured."""
    def is_available(self) -> bool:
        return False
    def complete(self, *_) -> str:
        return ""


def _build_llm_client():
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    if provider == "anthropic":
        model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
        return _AnthropicClient(model)
    if provider == "ollama":
        model = os.getenv("OLLAMA_MODEL", "llama2:7b")
        url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        return _OllamaClient(model, url)
    logger.info("LLM_PROVIDER=%s — no LLM; running deterministic_only", provider)
    return _NoLLM()


# ------------------------------------------------------------------ #
#  Prompt builders                                                    #
# ------------------------------------------------------------------ #

def _build_retrieval_query(data: dict) -> str:
    return (
        f"underwriting guidelines {data.get('policy_type', 'insurance')} "
        f"age {data.get('age', '')} claims {len(data.get('claims_history', []))}"
    )


def _build_user_message(
    pii_safe: dict,
    risk: RiskCalcResult,
    rules: RuleCheckResult,
    severity: SeverityResult,
    docs: list[RetrievedDoc],
) -> str:
    doc_block = "\n".join(
        f"[{d.doc_id}] {d.content_snippet[:300]}" for d in docs
    ) or "No documents retrieved."

    return f"""APPLICANT PROFILE (PII-safe)
age: {pii_safe.get('age')}
policy_type: {pii_safe.get('policy_type')}
income_tier: {pii_safe.get('income_tier')}
credit_tier: {pii_safe.get('credit_tier')}
claims_count: {pii_safe.get('claims_count')}

DETERMINISTIC RISK SCORE
score: {risk.score:.1f}/100  level: {risk.level}
breakdown: {risk.breakdown}

RULE CHECK
flags: {rules.flags}
violations: {rules.violations}
compliance_score: {rules.compliance_score}

SEVERITY ESTIMATE
tier: {severity.tier}  estimated_cost: ${severity.estimated_cost_usd:,.0f}
mode: {severity.mode}

RETRIEVED POLICY EVIDENCE
{doc_block}

Based on the above, provide your structured recommendation as JSON."""


# ------------------------------------------------------------------ #
#  Score blending (deterministic, auditable)                          #
# ------------------------------------------------------------------ #

def _blend_scores(
    calc_score: float,
    llm_score: float | None,
    violations: list[str],
    data: dict,
) -> tuple[float, str]:
    if llm_score is not None:
        final = 0.6 * calc_score + 0.4 * llm_score
    else:
        final = calc_score

    # Hard floors for serious violations
    age = int(data.get("age", 0))
    credit = int(data.get("credit_score", 0))
    income = float(data.get("annual_income", 0))
    claims = len(data.get("claims_history", []))

    triggers = sum([
        age < 21, credit < 500, income < 10_000, claims >= 3
    ])
    if triggers >= 2 and final < 85:
        final = 85.0
    elif triggers == 1 and final < 70:
        final = 70.0

    # Hard cap for low-risk profiles
    if credit >= 800 and 25 <= age <= 60 and claims == 0 and income >= 80_000:
        if final > 30:
            final = 30.0

    final = round(max(0.0, min(100.0, final)), 1)
    level = "High" if final >= 70 else "Medium" if final >= 40 else "Low"
    return final, level


# ------------------------------------------------------------------ #
#  Deterministic recommendation (when LLM unavailable)               #
# ------------------------------------------------------------------ #

def _rule_recommendation(rules: RuleCheckResult, score: float) -> str:
    if rules.hard_decline:
        return "decline"
    if score >= 70 or rules.violations:
        return "refer"
    if score >= 40:
        return "refer"
    return "approve"


def _rule_rationale(risk: RiskCalcResult, rules: RuleCheckResult, score: float) -> str:
    parts = [f"Risk score: {score:.1f}/100 ({risk.level})."]
    if rules.violations:
        parts.append(f"Rule violations: {', '.join(rules.violations)}.")
    if rules.flags:
        parts.append(f"Flags: {', '.join(rules.flags[:4])}.")
    parts.append(
        "LLM synthesis unavailable — recommendation derived from deterministic tools only. "
        "Human reviewer should apply additional judgment."
    )
    return " ".join(parts)


# ------------------------------------------------------------------ #
#  LLM response parser                                                #
# ------------------------------------------------------------------ #

def _parse_llm_json(text: str) -> dict:
    import json, re

    # Extract first JSON object from response
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        logger.warning("LLM returned no JSON block — falling back to deterministic")
        return {}
    try:
        parsed = json.loads(match.group())
        # Validate expected keys
        rec = parsed.get("recommendation", "")
        if rec not in {"approve", "decline", "refer", "insufficient_evidence"}:
            parsed["recommendation"] = "refer"
        llm_confidence = float(parsed.get("confidence", 0.0))
        parsed["confidence"] = max(0.0, min(1.0, llm_confidence))
        parsed["llm_risk_score"] = None   # LLM doesn't produce a numeric score; kept for blending
        return parsed
    except (json.JSONDecodeError, ValueError):
        logger.warning("LLM JSON parse failed")
        return {}
