"""
Insurance AI Decision Workflow — API

This is the public surface of the workflow system.  Every endpoint either
does real work or returns an honest error.  There are no mock fallbacks,
no in-memory state, and no placeholder endpoints.

Workflow summary
----------------
  POST /assess          Run the full workflow; creates a case; returns case_id
  GET  /cases           List cases (filterable by status)
  GET  /cases/{id}      Full case detail with version metadata
  POST /cases/{id}/review  Reviewer action (approve/reject/escalate/request_info)
  GET  /cases/{id}/history Audit event log for a case
  GET  /audit/export    Full audit log as JSONL
  GET  /health          Dependency status (retrieval, severity, LLM)
  GET  /versions        Current version manifest

Human role boundary
-------------------
  The model recommends.  The reviewer decides.
  No case is binding until a reviewer acts on it.
  This system is decision support, not autonomous underwriting.
"""

import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security, status
from fastapi.responses import PlainTextResponse, JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

import src.config as _cfg
from src.audit.versions import build_manifest
from src.privacy.pii_handler import mask_for_storage
from src.review.case_manager import CaseManager, ReviewRequest
from src.workflow.orchestrator import WorkflowOrchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Application lifespan — initialise singletons once                  #
# ------------------------------------------------------------------ #

_orchestrator: WorkflowOrchestrator | None = None
_case_manager: CaseManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _orchestrator, _case_manager
    logger.info("Starting Insurance AI Decision Workflow...")

    # Load config.yaml defaults before any component reads os.environ
    _cfg.load()

    if not os.getenv("API_KEY"):
        logger.warning(
            "API_KEY env var is not set — all endpoints are publicly accessible. "
            "Set API_KEY before deploying to production."
        )

    _case_manager = CaseManager()
    logger.info("CaseManager (SQLite) ready")

    _orchestrator = WorkflowOrchestrator()
    logger.info("WorkflowOrchestrator ready")

    yield

    logger.info("Shutting down")


# ------------------------------------------------------------------ #
#  API key authentication                                              #
# ------------------------------------------------------------------ #

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def _require_api_key(api_key: str | None = Security(_api_key_header)) -> None:
    """
    Require X-API-Key header when API_KEY env var is set.

    If API_KEY is not configured the endpoint is open (development mode).
    A startup warning is logged to remind operators to set it.
    """
    expected = os.getenv("API_KEY")
    if not expected:
        return  # open-access mode — operator was warned at startup
    if api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key. Include the X-API-Key header.",
        )


app = FastAPI(
    title="Insurance AI Decision Workflow",
    description=(
        "Workflow system for insurance underwriting decisions.\n\n"
        "**Documents in → information extracted → rules applied → "
        "recommendation produced → reviewer decides → everything auditable.**\n\n"
        "The model recommends. The reviewer decides. "
        "This is decision support, not autonomous underwriting."
    ),
    version="1.0.0",
    lifespan=lifespan,
    dependencies=[Depends(_require_api_key)],
)


# ------------------------------------------------------------------ #
#  Request / Response models                                          #
# ------------------------------------------------------------------ #

class PolicyholderInput(BaseModel):
    policyholder_id: str = Field(..., description="Unique applicant identifier")
    age: int = Field(..., ge=0, le=120)
    annual_income: float = Field(..., ge=0)
    credit_score: int = Field(..., ge=300, le=850)
    policy_type: str
    claims_history: list[dict[str, Any]] = Field(default_factory=list)
    premium_amount: float = Field(default=0.0)
    policy_start_date: str | None = None


class AssessmentResponse(BaseModel):
    case_id: str
    request_id: str
    recommendation: str
    rationale: str
    confidence: float
    risk_score: float
    risk_level: str
    severity_tier: str
    severity_estimated_cost: float
    severity_note: str
    evidence_refs: list[str]
    rule_flags: list[str]
    rule_violations: list[str]
    compliance_score: int
    hard_decline: bool
    llm_available: bool
    workflow_mode: str
    latency_ms: int
    versions: dict
    timestamp: str


class ReviewInput(BaseModel):
    reviewer_id: str = Field(..., min_length=1)
    action: str = Field(..., description="approve | reject | escalate | request_info")
    notes: str = Field(default="")


class CaseResponse(BaseModel):
    case_id: str
    request_id: str
    created_at: str
    status: str
    recommendation: dict
    evidence_refs: list[str]
    rule_findings: dict
    severity_tier: str
    llm_available: bool
    workflow_mode: str
    hard_decline: bool
    versions: dict
    reviewer_action: str | None
    reviewer_id: str | None
    reviewer_notes: str | None
    reviewed_at: str | None


# ------------------------------------------------------------------ #
#  Dependency helpers                                                  #
# ------------------------------------------------------------------ #

def _get_orchestrator() -> WorkflowOrchestrator:
    if _orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WorkflowOrchestrator not initialised — service is starting up.",
        )
    return _orchestrator


def _get_case_manager() -> CaseManager:
    if _case_manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CaseManager not initialised — service is starting up.",
        )
    return _case_manager


# ------------------------------------------------------------------ #
#  Endpoints                                                           #
# ------------------------------------------------------------------ #

@app.get("/health", tags=["Operations"])
async def health():
    """
    Dependency health check.

    Returns the status of each subsystem.  A 200 response does not mean all
    subsystems are available — check individual fields.  The workflow can
    operate in deterministic_only mode if the LLM is unavailable.
    """
    orch = _get_orchestrator()
    cm = _get_case_manager()

    health_data = orch.health()
    case_counts = cm.count_by_status()

    return {
        "status": "operational",
        "timestamp": _now(),
        "subsystems": health_data,
        "case_counts": case_counts,
        "versions": build_manifest().to_dict(),
        "human_role_note": (
            "The model recommends. The reviewer decides. "
            "No case is binding until a reviewer acts on it."
        ),
    }


@app.get("/versions", tags=["Operations"])
async def get_versions():
    """Current version manifest for all workflow components."""
    orch = _get_orchestrator()
    health_data = orch.health()
    return build_manifest(
        kb_version=health_data["retrieval"]["kb_version"],
        model_version=health_data["llm_provider"],
    ).to_dict()


@app.post("/assess", response_model=AssessmentResponse, tags=["Workflow"])
async def assess(request: Request, payload: PolicyholderInput):
    """
    Run the full underwriting workflow for a policyholder.

    1. Deterministic tools (RiskCalculator + RuleChecker) — always executed
    2. Severity scoring                                   — always executed
    3. Document retrieval (FAISS)                         — always executed
    4. LLM synthesis (Claude / Ollama)                    — if available
    5. Score blending                                     — always executed

    Returns a recommendation and creates a persistent Case record.
    The recommendation is advisory — a reviewer must act on the case.
    """
    orch = _get_orchestrator()
    cm = _get_case_manager()

    request_id = str(uuid.uuid4())
    logger.info("Assessment request: request_id=%s policyholder=%s",
                request_id, payload.policyholder_id)

    # Run workflow
    input_dict = payload.model_dump()
    try:
        result = orch.run(input_dict)
    except Exception as exc:
        logger.exception("Workflow execution error for request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Workflow execution failed: {exc}",
        ) from exc

    # Store case — PII masked before persistence
    masked_id = mask_for_storage({"policyholder_id": payload.policyholder_id})[
        "policyholder_id"
    ]

    case = cm.create_case(
        request_id=request_id,
        policyholder_id_hashed=masked_id,
        recommendation={
            "recommendation": result.recommendation,
            "rationale": result.rationale,
            "confidence": result.confidence,
            "risk_score": result.risk_score,
            "risk_level": result.risk_level,
        },
        evidence_refs=result.evidence_refs,
        rule_findings=result._rule_findings_raw,
        severity_tier=result.severity_tier,
        versions=result.versions,
        llm_available=result.llm_available,
        workflow_mode=result.workflow_mode,
        hard_decline=result.hard_decline,
    )

    logger.info(
        "Case created: case_id=%s recommendation=%s mode=%s severity=%s",
        case.case_id, result.recommendation, result.workflow_mode, result.severity_tier,
    )

    return AssessmentResponse(
        case_id=case.case_id,
        request_id=request_id,
        recommendation=result.recommendation,
        rationale=result.rationale,
        confidence=result.confidence,
        risk_score=result.risk_score,
        risk_level=result.risk_level,
        severity_tier=result.severity_tier,
        severity_estimated_cost=result.severity_estimated_cost,
        severity_note=result.severity_note,
        evidence_refs=result.evidence_refs,
        rule_flags=result.rule_flags,
        rule_violations=result.rule_violations,
        compliance_score=result.compliance_score,
        hard_decline=result.hard_decline,
        llm_available=result.llm_available,
        workflow_mode=result.workflow_mode,
        latency_ms=result.latency_ms,
        versions=result.versions,
        timestamp=_now(),
    )


@app.get("/cases", tags=["Review"])
async def list_cases(
    status_filter: str | None = Query(None, alias="status",
                                      description="pending_review | approved | rejected | escalated | info_requested"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List cases, optionally filtered by status."""
    cm = _get_case_manager()
    cases = cm.list_cases(status=status_filter, limit=limit, offset=offset)
    return {
        "cases": [_case_to_dict(c) for c in cases],
        "count": len(cases),
        "filters": {"status": status_filter},
    }


@app.get("/cases/{case_id}", tags=["Review"])
async def get_case(case_id: str):
    """Full case detail including recommendation, evidence, rule findings, and versions."""
    cm = _get_case_manager()
    try:
        case = cm.get_case(case_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Case not found: {case_id}")
    return _case_to_dict(case)


@app.post("/cases/{case_id}/review", tags=["Review"])
async def submit_review(case_id: str, review: ReviewInput):
    """
    Submit a reviewer decision on a case.

    Actions:
      approve      — accept the recommendation; case moves to approved
      reject       — override the recommendation; case moves to rejected
      escalate     — send to senior reviewer; case moves to escalated
      request_info — hold pending additional documentation; case moves to info_requested

    The reviewer_id and action are persisted in the audit log.
    Approved and rejected cases cannot be updated (terminal statuses).
    """
    cm = _get_case_manager()
    try:
        updated = cm.submit_review(
            case_id,
            ReviewRequest(
                reviewer_id=review.reviewer_id,
                action=review.action,
                notes=review.notes,
            ),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Case not found: {case_id}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    logger.info(
        "Review recorded: case_id=%s action=%s reviewer=%s",
        case_id, review.action, review.reviewer_id,
    )
    return _case_to_dict(updated)


@app.get("/cases/{case_id}/history", tags=["Review"])
async def get_case_history(case_id: str):
    """Full audit event log for a case (creation + all reviewer actions)."""
    cm = _get_case_manager()
    try:
        cm.get_case(case_id)   # existence check
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Case not found: {case_id}")
    events = cm.get_case_events(case_id)
    return {"case_id": case_id, "events": events}


@app.get("/audit/export", response_class=PlainTextResponse, tags=["Audit"])
async def export_audit():
    """
    Export the full audit log as JSONL (one case per line).

    Each line contains: case_id, request_id, recommendation, reviewer_action,
    all four version fields, timestamps, and masked policyholder_id.
    Raw PII is never stored and will not appear in this export.
    """
    cm = _get_case_manager()
    return cm.export_audit_jsonl()


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _case_to_dict(case) -> dict:
    return {
        "case_id": case.case_id,
        "request_id": case.request_id,
        "created_at": case.created_at,
        "status": case.status,
        "recommendation": case.recommendation,
        "evidence_refs": case.evidence_refs,
        "rule_findings": case.rule_findings,
        "severity_tier": case.severity_tier,
        "llm_available": case.llm_available,
        "workflow_mode": case.workflow_mode,
        "hard_decline": case.hard_decline,
        "versions": {
            "model_version": case.model_version,
            "prompt_version": case.prompt_version,
            "kb_version": case.kb_version,
            "rules_version": case.rules_version,
        },
        "reviewer_id": case.reviewer_id,
        "reviewer_action": case.reviewer_action,
        "reviewer_notes": case.reviewer_notes,
        "reviewed_at": case.reviewed_at,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api.main:app", host="0.0.0.0", port=8000, reload=True)
