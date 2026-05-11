# Changelog

All notable changes are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Version identifiers correspond to the `RULES_VERSION` and `PROMPT_VERSION` constants in `src/audit/versions.py`. Because this is a decision-support system, rule and prompt changes are treated as versioned releases — not silent updates — to preserve audit traceability.

---

## [Unreleased]

---

## [1.1.0] — 2026-05-10

**`rules_version: v1.0` · `prompt_version: v1.1`**

### Fixed

- Hard-decline guard at `POST /cases/{id}/review` now correctly blocks `approve` actions for all eligibility violations, not only the first one evaluated.
- Audit event timestamps are now consistently ISO-8601 UTC across all event types (`case_created`, `review_submitted`, `auto_escalated`, `status_changed`).
- `workflow_mode` and all four version fields are now correctly populated in responses when the `hard_decline` path short-circuits the orchestrator.
- Service now logs a prominent `WARNING` at startup if `PII_HASH_SALT` is still set to the default value (`insurance-workflow-v1`). Requests are still served, but the warning is visible in structured logs.
- Compliance score calculation no longer underflows below 0 when violation count exceeds 10.

---

## [1.0.1] — 2026-05-05

**`rules_version: v1.0` · `prompt_version: v1.1`**

### Added

- `CRITICAL` severity tier now triggers automatic case escalation at creation time. Cases with `severity_tier == CRITICAL` receive `status: escalated` instead of `status: pending_review` and are routed directly to the senior underwriter queue.
- `auto_escalated` audit event recorded with `actor: system` and `detail: {reason: "severity_tier=CRITICAL"}` whenever automatic escalation fires.
- `severity_note` plain-English routing instruction (`"Specialist reviewer required. Estimated cost $25k–$100k."`) included in every `POST /assess` response and every case record.
- `business_note` field added to `SeverityResult` dataclass for downstream routing consumers.
- `prompt_version` incremented to `v1.1` — LLM output schema now includes `risk_score` as an explicit field.

### Changed

- `CaseManager.create_case()` inspects `severity_tier` before writing `status`; CRITICAL cases bypass `pending_review` entirely.
- `GET /health` now reports `subsystems.severity_mode` (`model` or `rule_based`) to make the active scoring path observable.

---

## [1.0.0] — 2026-05-05

**`rules_version: v1.0` · `prompt_version: v1.0`**

Initial production-ready release. All mock behaviours removed; environment config boundary established.

### Added

**Seven-layer underwriting workflow**

- Layer ①: Ingestion boundary with `insurance-nlp-aws` — explicit interface documented in `docs/ingestion_boundary.md`.
- Layer ②: `DocumentLoader` — FAISS flat-L2 semantic retrieval using `all-MiniLM-L6-v2`; graceful degraded mode when index is absent.
- Layer ③: Deterministic tools
  - `RiskCalculator` — four-factor weighted score (age 20%, credit 30%, income 20%, claims 30%); output range 0–100; `rules_version: v1.0`.
  - `RuleChecker` — binary flag and violation system; hard-decline on `age_below_minimum` / `age_above_maximum`; `rules_version: v1.0`.
  - `SeverityScorer` — `LOW / MEDIUM / HIGH / CRITICAL` tiers; model mode (LoRA adapter from `claims-severity-prediction`) and rule-based fallback.
- Layer ④: `WorkflowOrchestrator` — score blending (`0.6 × deterministic + 0.4 × LLM`); pluggable LLM providers (Anthropic Claude, Ollama, `none`); PII stripped from prompt before LLM call.
- Layer ⑤: Human review — `POST /cases/{id}/review` accepts `approve / reject / escalate / request_info`; reviewer action is the decision of record.
- Layer ⑥: `CaseManager` — SQLite WAL-mode persistence; full audit event log; JSONL export via `GET /audit/export`; PII hashed before write.
- Layer ⑦: Evaluation — 15-case harness across 5 difficulty categories (`eval/harness.py`); results in `eval/report.md`.

**API — 8 endpoints**

`POST /assess` · `GET /cases` · `GET /cases/{id}` · `POST /cases/{id}/review` · `GET /cases/{id}/history` · `GET /audit/export` · `GET /health` · `GET /versions`

**Governance**

- `workflow_mode` (`full` / `deterministic_only` / `hard_decline`) declared on every response — no silent degradation.
- `llm_available` boolean on every response — machine-readable for downstream routing.
- Four version fields on every case: `model_version`, `prompt_version`, `rules_version`, `kb_version`.
- PII privacy layer: SHA-256 hash before storage; never-persist list enforced at write time; tiers-only representation in LLM prompt.
- Hard-decline cases: `confidence: 1.0`, `recommendation: decline`, cannot be approved by reviewers.

**Infrastructure**

- Dockerfile — Python 3.11 slim, non-root user.
- `docker-compose.yml` — persistent data volume; 30-second health check.
- Environment-variable-first configuration with `config/config.yaml` defaults.
- `python-json-logger` structured logging throughout.

---

_`prompt_version` history: `v1.0` → initial release; `v1.1` → added `risk_score` field to LLM output schema (2026-05-05)_  
_`rules_version` history: `v1.0` → initial release_
