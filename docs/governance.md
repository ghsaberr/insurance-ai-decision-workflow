# Governance, Privacy, and Human Role Boundary

## 1. Human Role Boundary

**The model recommends. The reviewer decides.**

This system is decision support, not autonomous underwriting. No case has
any binding effect until a licensed human reviewer acts on it.

| Actor | Responsibility |
|-------|---------------|
| Workflow system | Produces a recommendation, evidence references, rule findings, and severity tier |
| Human reviewer | Approves, rejects, escalates, or requests more information |
| Audit log | Records both the system output and the reviewer decision permanently |

A recommendation of `approve` from the model does not approve the policy.
A recommendation of `decline` does not decline it.
These labels describe the model's assessment — the reviewer's action is the
decision of record.

### Hard Declines

The only exception to reviewer discretion is a `hard_decline`. When the
`RuleChecker` fires a hard rule (e.g. `age_below_minimum`, `age_above_maximum`),
the workflow sets `hard_decline: true` and `recommendation: decline`
deterministically. Reviewers may still view the case but the system will not
allow an `approve` action on a hard-declined case.

---

## 2. Data Retention and PII Policy

### What is stored

| Field | Treatment |
|-------|-----------|
| `age` | Stored in plain — risk signal, not PII by itself |
| `policy_type` | Stored in plain |
| `claims_count` | Stored as count only, never as raw claims records |
| `policyholder_id` | **SHA-256 hashed** before storage |
| `annual_income` | **SHA-256 hashed** before storage |
| `credit_score` | **SHA-256 hashed** before storage |

The hash uses a fixed application salt (`insurance-workflow-v1`) so records
from the same policyholder remain linkable within the system without
exposing the raw value.

### What is never stored

The following fields are **dropped** by `pii_handler.mask_for_storage()`
before any data reaches the database:

- `name`
- `address`
- `ssn` / `date_of_birth`
- `email` / `phone`

If these fields are sent in a request payload they are used within the
current request scope only and never written to the audit DB, JSONL export,
or logs.

### What the LLM sees

The LLM prompt is constructed by `pii_handler.strip_pii_from_request()`.
The model receives:

- `age` (kept — necessary for risk assessment)
- `policy_type`
- `income_tier` (high / medium / low — not the raw figure)
- `credit_tier` (excellent / good / fair / poor — not the raw score)
- `claims_count` (integer count)

The raw `policyholder_id`, `annual_income`, and `credit_score` values are
**never sent to the LLM**.

### Audit export

`GET /audit/export` returns JSONL. All hashed fields appear as their
`sha256:` prefix. Raw PII fields are `[REDACTED]` in the export.

---

## 3. Versioning Policy

Every audit record stores four version strings. Increment the relevant
constant whenever the corresponding component changes.

| Field | Location | When to increment |
|-------|----------|-------------------|
| `model_version` | `src/audit/versions.py → MODEL_VERSION` | When the LLM model ID or system-prompt parameters change |
| `prompt_version` | `src/audit/versions.py → PROMPT_VERSION` | When the system prompt or user message template in `orchestrator.py` changes |
| `rules_version` | `src/audit/versions.py → RULES_VERSION` (also in `risk_calculator.py` and `rule_checker.py`) | When any RuleChecker threshold or RiskCalculator weight changes |
| `kb_version` | Written by the ingestion pipeline to `data/kb_version.txt`; read at startup | When the FAISS index is rebuilt from new source documents |

### Rationale

A reviewer acting on a case six months from now must be able to reproduce
the exact system state that produced the recommendation. The four version
fields, combined with the `case_id` and `created_at` timestamp, are the
minimum necessary to do that.

Without version tracking, a rule change or prompt change silently alters
all future recommendations while old cases look identical — a compliance
risk in any regulated context.

---

## 4. Failure Modes and Transparency

The system operates in one of three modes, always declared in the response:

| `workflow_mode` | Meaning |
|-----------------|---------|
| `full` | All layers ran, including LLM synthesis |
| `deterministic_only` | Deterministic tools + retrieval ran; LLM unavailable |
| `hard_decline` | Hard rule fired; LLM not consulted |

The `llm_available` boolean in every response makes the operating mode
machine-readable. Downstream systems can route `deterministic_only` cases
to a different reviewer queue if desired.

**No response claims full LLM synthesis when only deterministic tools ran.**
