# API Contract

This document is the authoritative reference for all HTTP endpoints exposed by the Insurance AI Decision Workflow service. It specifies request schemas, response schemas, status codes, and behavioral guarantees.

**Base URL:** `http://<host>:8000`  
**Interactive docs:** `http://<host>:8000/docs`  
**Content type:** `application/json` for all request and response bodies.

---

## Authentication

All endpoints (except `/health` and `/versions`) require an API key.

```
X-API-Key: <your-api-key>
```

If `API_KEY` is not set in the environment, the service runs in open-access mode and logs a warning on every request. **This mode is for local development only.** Do not serve external traffic without `API_KEY` configured.

---

## Endpoints

### 1. `POST /assess`

Run the full underwriting workflow for a single policyholder. Creates a persistent case record and returns the complete assessment.

**Authentication:** Required

#### Request schema

```json
{
  "policyholder_id":  "string  — unique applicant identifier",
  "age":              "integer — 0–120",
  "annual_income":    "number  — USD, >= 0",
  "credit_score":     "integer — 300–850",
  "policy_type":      "string  — e.g. 'auto', 'home', 'renters'",
  "claims_history": [
    {
      "date":   "string — YYYY-MM-DD",
      "amount": "number — USD claim amount",
      "type":   "string — claim type"
    }
  ],
  "premium_amount":    "number  — USD, default 0.0",
  "policy_start_date": "string | null — ISO-8601 date; used for claims recency checks"
}
```

#### Response schema

```json
{
  "case_id":                 "string — UUID of the created case",
  "request_id":              "string — UUID of this assessment request",
  "recommendation":          "approve | decline | refer | insufficient_evidence",
  "rationale":               "string — plain-English explanation citing evidence",
  "confidence":              "number — 0.0–1.0",
  "risk_score":              "number — 0–100, blended deterministic + LLM",
  "risk_level":              "Low | Medium | High",
  "severity_tier":           "LOW | MEDIUM | HIGH | CRITICAL",
  "severity_estimated_cost": "number — estimated claim cost in USD",
  "severity_note":           "string — plain-English routing instruction for reviewer",
  "evidence_refs":           ["string — doc_id from FAISS index"],
  "rule_flags":              ["string — all flags raised by RuleChecker"],
  "rule_violations":         ["string — only hard violations, subset of flags"],
  "compliance_score":        "integer — 0–100 (100 - 10 × violation_count, floor 0)",
  "hard_decline":            "boolean — true if an eligibility rule mandates decline",
  "llm_available":           "boolean — true if LLM synthesis ran",
  "workflow_mode":           "full | deterministic_only | hard_decline",
  "latency_ms":              "integer — end-to-end response time",
  "versions": {
    "model_version":  "string — LLM model identifier",
    "prompt_version": "string — system prompt version",
    "rules_version":  "string — rule and scoring version",
    "kb_version":     "string — FAISS index build version"
  },
  "timestamp": "string — ISO-8601 UTC"
}
```

---

### 2. `GET /cases`

List cases with optional status filter and pagination.

**Authentication:** Required

#### Query parameters

| Parameter | Type    | Default | Description |
|-----------|---------|---------|-------------|
| `status`  | string  | (all)   | `pending_review`, `approved`, `rejected`, `escalated`, `info_requested` |
| `limit`   | integer | 50      | Maximum results per page |
| `offset`  | integer | 0       | Pagination offset |

#### Response schema

```json
[
  {
    "case_id":       "string",
    "status":        "string",
    "created_at":    "string — ISO-8601",
    "recommendation": "string",
    "severity_tier": "string",
    "workflow_mode": "string",
    "hard_decline":  "boolean",
    "reviewed_at":   "string | null"
  }
]
```

---

### 3. `GET /cases/{case_id}`

Retrieve the full persistent record for a single case, including all audit fields and reviewer data.

**Authentication:** Required

#### Response schema

```json
{
  "case_id":    "string",
  "request_id": "string",
  "created_at": "string — ISO-8601",
  "status":     "pending_review | approved | rejected | escalated | info_requested",
  "recommendation": {
    "recommendation": "string",
    "rationale":      "string",
    "confidence":     "number",
    "risk_score":     "number",
    "risk_level":     "string"
  },
  "evidence_refs": ["string"],
  "rule_findings": {
    "flags":            ["string"],
    "actions":          ["string"],
    "violations":       ["string"],
    "compliance_score": "integer"
  },
  "severity_tier":   "string",
  "model_version":   "string",
  "prompt_version":  "string",
  "rules_version":   "string",
  "kb_version":      "string",
  "llm_available":   "boolean",
  "workflow_mode":   "string",
  "hard_decline":    "boolean",
  "reviewer_id":     "string | null",
  "reviewer_action": "string | null",
  "reviewer_notes":  "string | null",
  "reviewed_at":     "string | null"
}
```

#### Status codes

| Code | Condition |
|------|-----------|
| 200  | Case found |
| 404  | Case not found |

---

### 4. `POST /cases/{case_id}/review`

Submit a reviewer decision. Updates case status and appends an audit event.

**Authentication:** Required

#### Request schema

```json
{
  "reviewer_id": "string — reviewer identifier (stored in audit record)",
  "action":      "approve | reject | escalate | request_info",
  "notes":       "string — reviewer notes (stored verbatim in audit record)"
}
```

#### Action to status mapping

| Action        | Resulting status  | Terminal |
|---------------|-------------------|----------|
| `approve`     | `approved`        | Yes      |
| `reject`      | `rejected`        | Yes      |
| `escalate`    | `escalated`       | No       |
| `request_info`| `info_requested`  | No       |

#### Behavioral constraints

- Cases with `status` already `approved` or `rejected` cannot receive further review actions (`409`).
- Cases with `hard_decline: true` cannot be `approve`d (`400`). The reviewer may escalate or request information.

#### Status codes

| Code | Condition |
|------|-----------|
| 200  | Review accepted; returns updated case |
| 400  | `approve` action on a hard-declined case |
| 404  | Case not found |
| 409  | Case already in a terminal state (`approved` or `rejected`) |

---

### 5. `GET /cases/{case_id}/history`

Retrieve the ordered audit event log for a single case.

**Authentication:** Required

#### Response schema

```json
[
  {
    "id":         "integer — autoincrement event ID",
    "case_id":    "string",
    "event_at":   "string — ISO-8601 UTC",
    "event_type": "case_created | review_submitted | auto_escalated | status_changed",
    "actor":      "string — reviewer_id or 'system'",
    "detail":     {}
  }
]
```

Events are returned in ascending chronological order.

#### Status codes

| Code | Condition |
|------|-----------|
| 200  | History returned (empty array if no events beyond creation) |
| 404  | Case not found |

---

### 6. `GET /audit/export`

Export all case records as JSONL (newline-delimited JSON). PII fields are redacted before export.

**Authentication:** Required

**Response content-type:** `application/x-ndjson`  
Each line is one complete case record.

#### PII redaction in export

| Field              | Export value                  |
|--------------------|-------------------------------|
| `policyholder_id`  | `sha256:<16-char hex prefix>` |
| `annual_income`    | `sha256:<16-char hex prefix>` |
| `credit_score`     | `sha256:<16-char hex prefix>` |
| `name`, `address`, `ssn`, `date_of_birth`, `email`, `phone` | `[REDACTED]` |

---

### 7. `GET /health`

Return operational status of all subsystems. Suitable for load balancer health checks and monitoring dashboards.

**Authentication:** Not required

#### Response schema

```json
{
  "status": "ok | degraded",
  "subsystems": {
    "retrieval": {
      "available":  "boolean",
      "index_path": "string",
      "doc_count":  "integer",
      "kb_version": "string"
    },
    "severity_mode": "model | rule_based",
    "llm_provider":  "anthropic | ollama | none"
  },
  "case_counts": {
    "pending_review":  "integer",
    "approved":        "integer",
    "rejected":        "integer",
    "escalated":       "integer",
    "info_requested":  "integer"
  },
  "versions": {
    "model_version":  "string",
    "prompt_version": "string",
    "rules_version":  "string",
    "kb_version":     "string"
  }
}
```

`status` is `degraded` when `retrieval.available` is `false` (FAISS index missing or unloadable). All other endpoints remain functional in degraded mode; `evidence_refs` will be empty on new assessments.

---

### 8. `GET /versions`

Return the current version manifest without the full subsystem health check.

**Authentication:** Not required

#### Response schema

```json
{
  "model_version":  "string",
  "prompt_version": "string",
  "rules_version":  "string",
  "kb_version":     "string"
}
```

---

## Case Status Transitions

```
POST /assess
     │
     ├─ severity_tier == CRITICAL ──► escalated ─────────────┐
     │                                                        │
     └─ all other tiers ──────────► pending_review            │
                                         │                    │
                              POST /cases/{id}/review         │
                                    │                         │
                         ┌──────────┴──────────┐             │
                      approve               reject            │
                         │                    │               │
                      approved            rejected            │
                     (terminal)          (terminal)           │
                                                              │
                              escalate ──────────────────────►┘
                              request_info ──► info_requested
```

`approved` and `rejected` are terminal states. No further review actions are accepted once a case reaches either state.

---

## Workflow Modes

Every response from `POST /assess` and `GET /cases/{id}` includes `workflow_mode` and `llm_available`:

| `workflow_mode`      | Condition                         | Confidence ceiling | Score blending |
|----------------------|-----------------------------------|--------------------|----------------|
| `full`               | LLM ran successfully              | Up to 1.0          | `0.6 × calc + 0.4 × llm` |
| `deterministic_only` | LLM unavailable or failed         | Capped at 0.55     | `1.0 × calc` only |
| `hard_decline`       | Eligibility rule fired; LLM skipped | Fixed at 1.0     | Not applicable |

No response claims `full` when `deterministic_only` ran. The field is always present and always accurate.

---

## Deterministic Rule Thresholds

These values are fixed at `rules_version: v1.0`. They are reproduced here so reviewers and integrators can interpret rule flags without reading source code.

### Risk score weights

| Factor        | Weight |
|---------------|--------|
| Age risk      | 20%    |
| Credit risk   | 30%    |
| Income risk   | 20%    |
| Claims risk   | 30%    |

### Risk levels

| Level  | Score range |
|--------|-------------|
| Low    | 0 – 39      |
| Medium | 40 – 69     |
| High   | 70 – 100    |

### Hard-decline conditions

| Condition                | Flag                   |
|--------------------------|------------------------|
| `age < 18`               | `age_below_minimum`    |
| `age > 80`               | `age_above_maximum`    |

Hard-declined cases receive `recommendation: decline` and `confidence: 1.0` regardless of LLM availability.

### Severity tiers

| Tier       | Estimated cost | Routing |
|------------|----------------|---------|
| `LOW`      | < $5,000       | Standard review queue |
| `MEDIUM`   | $5k – $25k     | Priority review; verify coverage limits |
| `HIGH`     | $25k – $100k   | Specialist reviewer required |
| `CRITICAL` | > $100k        | Auto-escalated to senior underwriter on case creation |

---

## End-to-End Example

### Step 1 — Submit assessment

**Request**

```bash
curl -X POST http://localhost:8000/assess \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "policyholder_id": "PH-20240112-0041",
    "age": 34,
    "annual_income": 72000,
    "credit_score": 715,
    "policy_type": "auto",
    "premium_amount": 1800,
    "claims_history": [
      { "date": "2024-08-15", "amount": 3200.00, "type": "collision" }
    ],
    "policy_start_date": "2026-05-01"
  }'
```

**Response**

```json
{
  "case_id": "b3a2f891-4c10-4e9a-a847-16f2c3d85e01",
  "request_id": "f09c1aa3-9b2e-4d11-8cfe-5e7a2bc14d22",
  "recommendation": "refer",
  "rationale": "Risk score is moderate. One recent collision claim under the high-severity threshold. Credit is preferred tier. Refer for standard underwriting review.",
  "confidence": 0.71,
  "risk_score": 38.5,
  "risk_level": "Low",
  "severity_tier": "MEDIUM",
  "severity_estimated_cost": 12400.0,
  "severity_note": "Priority review. Estimated cost $5k–$25k; may require additional documentation.",
  "evidence_refs": ["DOC_014", "DOC_022"],
  "rule_flags": ["recent_claims_present", "good_credit", "medium_income"],
  "rule_violations": [],
  "compliance_score": 100,
  "hard_decline": false,
  "llm_available": true,
  "workflow_mode": "full",
  "latency_ms": 1840,
  "versions": {
    "model_version": "claude-haiku-4-5-20251001",
    "prompt_version": "v1.1",
    "rules_version": "v1.0",
    "kb_version": "faiss-2025-05-05"
  },
  "timestamp": "2026-05-10T14:23:07Z"
}
```

### Step 2 — Submit reviewer decision

**Request**

```bash
curl -X POST http://localhost:8000/cases/b3a2f891-4c10-4e9a-a847-16f2c3d85e01/review \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "reviewer_id": "underwriter_jane",
    "action": "approve",
    "notes": "Single prior collision, well within exposure limit. Income and credit verified. Approved at standard rate."
  }'
```

**Response** — updated case with `status: approved`.

### Step 3 — Export audit record

```bash
curl -H "X-API-Key: your-api-key" http://localhost:8000/audit/export
# Returns JSONL; PII fields appear as sha256:... or [REDACTED]
```

---

## Hard-Decline Example

A policyholder aged 17 (below the minimum eligible age of 18):

```json
{
  "recommendation": "decline",
  "rationale":      "Hard rule: applicant age is below the minimum eligible age of 18.",
  "confidence":     1.0,
  "hard_decline":   true,
  "workflow_mode":  "hard_decline",
  "llm_available":  false,
  "rule_violations": ["age_below_minimum"]
}
```

A reviewer cannot `approve` this case. Only `escalate` or `request_info` are accepted at `POST /cases/{id}/review`.
