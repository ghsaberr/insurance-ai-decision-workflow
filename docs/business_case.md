# Business Case

## Problem Statement

Manual underwriting combines three compounding failure modes that this system addresses directly.

**Inconsistency.** Two underwriters applying identical rules to identical applicants may reach different decisions. The discrepancy is difficult to detect, impossible to audit retrospectively, and creates regulatory exposure when challenged. Rules exist in policy documents; their application exists only in the memory of individual reviewers.

**Latency.** Complex cases require assembling prior claims records, credit summaries, and policy precedents from separate systems before a decision can begin. This assembly work is unbounded and queues accumulate during high-volume periods.

**Auditability gaps.** When a decision is questioned by a regulator, an internal audit team, or the policyholder months later, the basis for that decision may no longer be reconstructable. Rationale exists in email threads and freeform notes — not structured, queryable records tied to specific rule versions and evidence.

---

## What This System Does

The workflow provides **structured, evidence-grounded decision support** for underwriting teams. It does not replace reviewers. It eliminates the assembly and consistency work that precedes every review.

For every submission, the system:

1. Applies a deterministic four-factor risk score and a binary rule check in under two seconds.
2. Retrieves semantically similar policy precedents and claims guidelines from an indexed knowledge base.
3. Optionally synthesises a recommendation using a language model operating under strict PII and citation constraints.
4. Persists every case with all rule findings, all evidence references, LLM rationale, and four audit version fields.
5. Surfaces the complete record to the reviewer — recommendation, evidence, violations, severity tier — at decision time.
6. Records the reviewer's action as the binding decision of record.

**What this system does not do:** It does not make underwriting decisions. No recommendation has any operational effect until a licensed reviewer submits an action.

---

## Target Users

| Role | How they interact | Value delivered |
|------|-------------------|----------------|
| Underwriter / reviewer | Reviews the assessment output; submits approve / reject / escalate / request_info via `POST /cases/{id}/review` | Structured evidence and rule findings at decision time; no manual assembly |
| Team lead | Monitors case queue by status and severity tier via `GET /cases` | Priority routing for `CRITICAL` cases; consistent rule application across the team |
| Compliance officer | Exports audit log via `GET /audit/export`; queries individual case history | Reproducible, version-stamped decision trail for regulatory review |
| Platform engineer | Deploys and operates the API | Observable failure modes; no silent degradation; clear health endpoint |

---

## Key Performance Indicators

These KPIs reflect what the system is designed to move. Baselines should be measured before deployment and tracked per quarter.

| KPI | Description | Target (indicative) |
|-----|-------------|---------------------|
| Mean case preparation time | Time from submission to reviewer action | Reduce by ≥ 40% vs. manual baseline |
| Rule consistency rate | Proportion of cases where deterministic flags match expected rule outputs | 100% (system-guaranteed) |
| Evidence citation rate | Proportion of `full`-mode cases where `evidence_refs` is non-empty | ≥ 80% |
| `full` mode coverage | Proportion of assessments that ran LLM synthesis | ≥ 90% under normal operating conditions |
| CRITICAL escalation accuracy | Proportion of `CRITICAL` severity cases automatically routed to senior underwriter | 100% (system-guaranteed) |
| Hard decline accuracy | Proportion of hard-declined cases that match a mandatory ineligibility rule | 100% (system-guaranteed; confidence: 1.0) |
| Audit completeness | Proportion of cases with all four version fields populated | 100% (system-guaranteed) |

---

## Compliance and Regulatory Alignment

This system was designed with insurance-industry regulatory constraints as primary requirements, not implementation afterthoughts.

### Human-in-the-loop

No case is binding until a licensed reviewer acts. The API enforces this structurally: there is no code path that produces a binding outcome without a `reviewer_id` and a deliberate `action`. Model recommendations do not create obligations; reviewer actions do.

### Auditability

Every case record contains:

- All input signals used in the assessment (with sensitive fields hashed per the PII policy)
- The complete rule output — every flag raised, every violation, every recommended action
- All evidence references (document IDs returned by the knowledge base)
- The LLM recommendation and rationale, when applicable
- The four version fields that identify the exact rule set, prompt, model, and knowledge base that produced the recommendation
- The reviewer's action, notes, and timestamp

A compliance officer reviewing a case six months after the fact can reconstruct the exact system state that produced the recommendation the reviewer acted on. This is the minimum necessary for regulated decision traceability.

### PII minimisation

Sensitive fields (`policyholder_id`, `annual_income`, `credit_score`) are SHA-256 hashed before any data reaches the database. Fields that carry identity risk (`name`, `address`, `ssn`, `date_of_birth`, `email`, `phone`) are dropped by the privacy layer before storage and never appear in the audit log.

The language model prompt never receives raw numeric values. It receives tier labels only (`income_tier: high/medium/low`, `credit_tier: excellent/good/fair/poor`). The raw `policyholder_id` is never sent to the LLM.

### Hard eligibility rules

Age-based ineligibility (applicants under 18 or over 80) is deterministic and non-overridable. The `hard_decline` flag cannot be set to `true` by any LLM output, and the review endpoint will not accept an `approve` action on a hard-declined case. These constraints are enforced in code, not documentation.

### Transparent degradation

The `workflow_mode` field (`full`, `deterministic_only`, `hard_decline`) is present in every assessment response. Downstream teams can distinguish cases processed with LLM synthesis from those processed deterministically, and route accordingly. There is no silent fallback. The `llm_available` boolean makes this machine-readable for automated routing.

### Version traceability

Every case stores four version strings: `model_version`, `prompt_version`, `rules_version`, `kb_version`. When rule thresholds or prompt templates change, the corresponding version constant is incremented and the change is recorded in `CHANGELOG.md`. Without this, a rule change silently alters all future recommendations while old cases appear to have been scored under the same conditions — a compliance risk in any regulated context.

---

## Scope Boundaries

This system is one component in a three-repository portfolio. Each repository has a defined boundary and is independently deployable.

| Repository | Role |
|------------|------|
| [`insurance-nlp-aws`](../Insurance-NLP-AWS) | Document ingestion pipeline — PDF extraction, NER, FAISS index construction, AWS deployment |
| `Insurance-AI-Decision-Workflow` | **This repo** — Underwriting decision workflow, risk scoring, rule checking, severity routing, human review, audit |
| [`claims-severity-prediction`](../Claims-Severity-Prediction) | Severity model — fine-tuned LoRA/QLoRA adapter that powers `SeverityScorer` when `SEVERITY_MODEL_PATH` is set |

The interface from `insurance-nlp-aws` to this repo is two files: `insurance_faiss.index` and `insurance_metadata.json`. The interface from `claims-severity-prediction` is one directory: the LoRA adapter at `SEVERITY_MODEL_PATH`. Neither companion repository contains underwriting logic; this repo contains no ingestion or model-training logic.

---

## What This System Is Not

Precisely scoping what the system does not do prevents misapplication.

- **Not an autonomous underwriting engine.** Every case requires a human action. The system produces inputs to a decision, not the decision itself.
- **Not a real-time fraud detection system.** It applies static business rules to structured applicant data. Anomaly detection and fraud signals are outside scope.
- **Not a regulatory compliance system.** It is designed to support auditability, not to certify compliance with any specific jurisdiction's insurance regulations. Legal and compliance review is required before production deployment in any regulated market.
- **Not a general-purpose RAG assistant.** The language model operates under strict constraints: it may only cite retrieved documents, must declare uncertainty, and must produce a structured JSON response. It does not answer free-form questions.
