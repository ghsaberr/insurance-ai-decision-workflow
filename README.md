# Insurance AI Decision Workflow

An insurer-facing workflow system that takes insurance documents from intake
to a human-reviewed, fully auditable underwriting decision.

```
documents in
  → information extracted
  → rules applied and evidence retrieved
  → recommendation produced
  → reviewer decides
  → everything is auditable and versioned
```

This is not a RAG assistant. This is not an agentic app.
This is a **workflow**.

---

## What the system proves

| Question | Where answered |
|----------|---------------|
| Where does information come from? | `insurance-nlp-aws` pipeline → FAISS index → `src/ingestion/document_loader.py` |
| What is extracted? | PDF text → NER entities → structured JSON by the ingestion pipeline |
| What is retrieved? | Top-k policy/guideline documents by semantic similarity — `GET /cases/{id}` → `evidence_refs` |
| What deterministic checks run? | `RiskCalculator` + `RuleChecker` — fully reproducible, versioned at `rules_version` |
| How is the recommendation created? | Weighted blend of deterministic score + LLM synthesis — `src/workflow/orchestrator.py` |
| How does a human review it? | `POST /cases/{id}/review` — approve / reject / escalate / request_info |
| What is stored? | Every case in SQLite with masked PII — `data/workflow.db` |
| What is versioned? | model, prompt, rules, and knowledge-base — four fields on every case |
| How are failures handled? | `workflow_mode` is always declared: `full`, `deterministic_only`, or `hard_decline` |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                  Insurance AI Decision Workflow                  │
│                                                                 │
│  ① Ingestion          insurance-nlp-aws pipeline               │
│     PDF → ETL → FAISS index  (companion repo)                  │
│     ↓ insurance_faiss.index + insurance_metadata.json           │
│                                                                 │
│  ② Retrieval          src/ingestion/document_loader.py         │
│     FAISS semantic search → top-k RetrievedDoc                  │
│                                                                 │
│  ③ Deterministic      src/tools/                               │
│     RiskCalculator    weighted 0-100 score                      │
│     RuleChecker       binary flags + hard-decline logic         │
│     SeverityScorer    triage tier LOW/MEDIUM/HIGH/CRITICAL      │
│                                                                 │
│  ④ Recommendation     src/workflow/orchestrator.py             │
│     Blends ③ + LLM synthesis → approve/decline/refer           │
│     Declares workflow_mode + llm_available on every response    │
│                                                                 │
│  ⑤ Human Review       POST /cases/{id}/review                  │
│     approve | reject | escalate | request_info                  │
│     Reviewer action is the decision of record                   │
│                                                                 │
│  ⑥ Audit & Governance src/review/case_manager.py (SQLite)      │
│     Every case: case_id, request_id, recommendation,           │
│     evidence_refs, rule_findings, 4 version fields,             │
│     reviewer_action, timestamps                                  │
│     PII masked before storage  (src/privacy/pii_handler.py)    │
│                                                                 │
│  ⑦ Evaluation         eval/                                    │
│     15-case test set across 5 difficulty categories             │
│     Scored on groundedness, rule consistency, failure modes     │
└─────────────────────────────────────────────────────────────────┘
```

---

## Quick start

```bash
# 1. Clone and create env
git clone <repo-url>
cd Insurance-AI-Decision-Workflow
python -m venv .venv && .venv\Scripts\activate    # Windows
# python -m venv .venv && source .venv/bin/activate  # Linux/Mac

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp env.example .env
# Edit .env — set ANTHROPIC_API_KEY (or set LLM_PROVIDER=none for deterministic-only)

# 4. (Optional) Build/refresh the FAISS index
#    cd ../Insurance-NLP-AWS && python run_pipeline.py --local
#    cp insurance_faiss.index ../Insurance-AI-Decision-Workflow/data/
#    cp insurance_metadata.json ../Insurance-AI-Decision-Workflow/data/

# 5. Start the API
uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## API reference

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/assess` | Run full workflow; creates a case |
| `GET` | `/cases` | List cases (filter by `?status=`) |
| `GET` | `/cases/{id}` | Full case detail with all audit fields |
| `POST` | `/cases/{id}/review` | Submit reviewer decision |
| `GET` | `/cases/{id}/history` | Audit event log for a case |
| `GET` | `/audit/export` | Full audit log as JSONL |
| `GET` | `/health` | Subsystem status (retrieval, severity, LLM) |
| `GET` | `/versions` | Current version manifest |

Interactive docs: `http://localhost:8000/docs`

---

## Example: submit and review a case

```bash
# 1. Assess a policyholder
curl -X POST http://localhost:8000/assess \
  -H "Content-Type: application/json" \
  -d '{
    "policyholder_id": "PH_001",
    "age": 34,
    "annual_income": 62000,
    "credit_score": 710,
    "policy_type": "auto",
    "claims_history": []
  }'
# → returns case_id, recommendation, risk_score, severity_tier, versions, ...

# 2. Reviewer approves
curl -X POST http://localhost:8000/cases/<case_id>/review \
  -H "Content-Type: application/json" \
  -d '{
    "reviewer_id": "underwriter_jane",
    "action": "approve",
    "notes": "Clean profile, standard auto. Approved."
  }'

# 3. Export audit log
curl http://localhost:8000/audit/export
```

---

## Workflow modes

The `workflow_mode` field in every response declares exactly what ran:

| Mode | When | Meaning |
|------|------|---------|
| `full` | LLM available | Deterministic tools + retrieval + LLM synthesis |
| `deterministic_only` | LLM unavailable | Deterministic tools + retrieval; no LLM |
| `hard_decline` | Hard rule fired | Eligibility rule violated; LLM not consulted |

The system never claims `full` when it ran `deterministic_only`.
The system never silently degrades — mode and `llm_available` are
always present in the response.

---

## Related repositories

| Repo | Role |
|------|------|
| [insurance-nlp-aws](../Insurance-NLP-AWS) | Ingestion and extraction layer — PDF ETL, FAISS index build, NER training, AWS deployment |
| [claims-severity-prediction](../Claims-Severity-Prediction) | Severity model — fine-tuned LoRA/QLoRA model powering `src/severity/severity_scorer.py` |

See [docs/ingestion_boundary.md](docs/ingestion_boundary.md) for the
explicit interface between this repo and `insurance-nlp-aws`.

---

## Governance

See [docs/governance.md](docs/governance.md) for:
- Human role boundary (the model recommends; the reviewer decides)
- PII policy (what is stored, what is hashed, what is never persisted)
- Versioning policy (when to increment each version field)
- Failure mode transparency

---

## Evaluation

See [eval/report.md](eval/report.md) for:
- Test results across 5 case categories
- Known failure modes
- What this system is and is not safe for
