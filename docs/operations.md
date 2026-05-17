# Operations Guide

Deployment, configuration, health monitoring, routine maintenance, and incident procedures.

---

## Environment Variables

Environment variables take precedence over all values in `config/config.yaml`. The config file provides defaults; environment variables override them at runtime.

### Security — required in production

| Variable | Default | Description |
|----------|---------|-------------|
| `API_KEY` | (unset) | Bearer token checked against the `X-API-Key` request header. If unset, all endpoints are open-access. **Never run with this unset in production.** |
| `PII_HASH_SALT` | `insurance-workflow-v1` | Salt for SHA-256 hashing of `policyholder_id`, `annual_income`, and `credit_score`. **Must be changed from the default before any real policyholder data is processed.** Store in a secrets manager; never commit to source control. |

### LLM provider

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `anthropic` | `anthropic` \| `ollama` \| `none`. Set `none` to run in `deterministic_only` mode with no LLM dependency. |
| `ANTHROPIC_API_KEY` | (unset) | Required when `LLM_PROVIDER=anthropic`. |
| `ANTHROPIC_MODEL` | `claude-haiku-4-5-20251001` | Anthropic model identifier. Increment `MODEL_VERSION` in `src/audit/versions.py` whenever this changes. |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server base URL. Used when `LLM_PROVIDER=ollama`. |
| `OLLAMA_MODEL` | `llama2:7b` | Model tag passed to Ollama `/api/generate`. |

### Retrieval

| Variable | Default | Description |
|----------|---------|-------------|
| `FAISS_INDEX_PATH` | `data/insurance_faiss.index` | Path to the FAISS flat-L2 index produced by the `insurance-nlp-aws` pipeline. |
| `FAISS_METADATA_PATH` | `data/insurance_metadata.json` | Path to the matching metadata file (doc_id, policy_id, text_snippet per vector). |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformer model for query embedding. Must match the model used at index-build time in `insurance-nlp-aws`. |

### Severity model

| Variable | Default | Description |
|----------|---------|-------------|
| `SEVERITY_MODEL_PATH` | (unset) | Path to the fine-tuned LoRA adapter from `claims-severity-prediction`. If unset, `SeverityScorer` runs in `rule_based` mode (confidence 0.55 vs. 0.78 for model mode). |

### Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `data/workflow.db` | SQLite database path. The file is created on first run. Requires a persistent mount in containerised deployments. |

---

## Local Development

```bash
git clone <repo-url>
cd insurance-ai-decision-workflow

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

pip install -r requirements.txt

cp .env.example .env
# Edit .env — at minimum set ANTHROPIC_API_KEY,
# or set LLM_PROVIDER=none to run without LLM.

uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
```

Interactive API docs: `http://localhost:8000/docs`

To run in fully deterministic mode (no LLM, no severity model):

```bash
LLM_PROVIDER=none uvicorn src.api.main:app --port 8000 --reload
```

---

## Docker Deployment

```bash
docker compose up --build
```

The compose service:
- Exposes port `8000`
- Mounts a persistent `workflow-data` volume for the SQLite database and FAISS artifacts
- Runs a health check every 30 seconds against `GET /health`
- Runs the application as a non-root user

To pass secrets without writing them to `.env`:

```bash
API_KEY=<secret> \
PII_HASH_SALT=<secret> \
ANTHROPIC_API_KEY=<key> \
docker compose up
```

---

## Smoke Test and Evaluation

Run after every deployment before serving live traffic.

```bash
# Smoke test — exercises escalation, review round-trip, hard-decline guard, orchestrator
python tests/smoke_day3.py

# Evaluation harness — 15 test cases across 5 difficulty categories
python eval/harness.py
```

Both scripts run in-process (no server required). Expected results are documented in `eval/report.md`.

---

## Health Monitoring

`GET /health` is the primary observability endpoint. Poll it from your load balancer, uptime monitor, or alerting system.

```bash
curl http://localhost:8000/health
```

### Fields to alert on

| Field | Alert condition | Consequence |
|-------|----------------|-------------|
| `status` | `degraded` | One or more subsystems are impaired |
| `subsystems.retrieval.available` | `false` | FAISS index missing or unloadable; `evidence_refs` will be empty on all new assessments |
| `subsystems.severity_mode` | `rule_based` when `model` expected | Severity model not loaded; confidence is 0.55 instead of 0.78 |
| `case_counts.escalated` | Sustained high value | May indicate systematic CRITICAL severity or a misconfigured threshold |

The service does not emit Prometheus metrics out of the box. Structure your monitoring around the `/health` JSON response and structured log output.

---

## Logging

The service uses `python-json-logger`. Every log line is a JSON object containing:

- `timestamp` — ISO-8601 UTC
- `level` — `DEBUG` / `INFO` / `WARNING` / `ERROR`
- `message`
- `workflow_mode` — present on assessment-path log lines
- `llm_available` — present on assessment-path log lines
- `case_id` — present when a case is created or reviewed
- Full exception traceback on `ERROR` events

Forward structured log output to any aggregation system (CloudWatch Logs, Datadog, Splunk, etc.) without pre-processing.

---

## Routine Operations

### Refreshing the knowledge base

When new policy documents are available, rebuild the FAISS index in `insurance-nlp-aws` and copy the output artifacts here:

```bash
# In insurance-nlp-aws/
python run_pipeline.py --local      # ETL + embedding + FAISS index, no AWS required

# Copy artifacts
cp insurance_faiss.index   ../insurance-ai-decision-workflow/data/
cp insurance_metadata.json ../insurance-ai-decision-workflow/data/
echo "faiss-$(date +%Y-%m-%d)" > ../insurance-ai-decision-workflow/data/kb_version.txt
```

Restart the API after copying. `DocumentLoader` loads the index at startup, not dynamically. The `kb_version` on all subsequent cases will reflect the new build date.

Verify with `GET /health` → `subsystems.retrieval.doc_count` and `subsystems.retrieval.kb_version`.

---

### Bumping `rules_version`

Perform this whenever any threshold in `RuleChecker` or any weight in `RiskCalculator` changes.

1. Edit the relevant constants in `src/tools/rule_checker.py` and / or `src/tools/risk_calculator.py`.
2. Increment `RULES_VERSION` in `src/audit/versions.py` (and in `risk_calculator.py` and `rule_checker.py` where it is also declared).
3. Re-run the evaluation harness: `python eval/harness.py`. Confirm all expected cases still pass.
4. Record the change in `CHANGELOG.md`.

Without this step, the new thresholds apply to new cases while old cases appear to have been scored under the same rules — an auditability gap in any regulated context.

---

### Bumping `prompt_version`

Perform this whenever the system prompt or user message template in `src/workflow/orchestrator.py` changes.

1. Edit the prompt.
2. Increment `PROMPT_VERSION` in `src/audit/versions.py`.
3. Re-run the evaluation harness.
4. Record the change in `CHANGELOG.md`.

---

### Switching LLM providers

To switch from Anthropic Claude to Ollama:

```bash
LLM_PROVIDER=ollama
OLLAMA_URL=http://your-ollama-host:11434
OLLAMA_MODEL=llama2:7b
```

Restart the service. Confirm with `GET /health` → `subsystems.llm_provider`.

If switching to a model with meaningfully different behaviour, increment `model_version` and record the change.

---

### Rotating the API key

1. Generate a new secret (minimum 32 random bytes, base64 or hex encoded).
2. Set `API_KEY` to the new value in your secrets manager.
3. Restart the service or trigger a rolling restart in your container orchestrator.
4. Verify with a test request using the new key.

---

### Rotating `PII_HASH_SALT`

Rotating the salt changes the hash output for all future records. Records written before the rotation retain the previous hash and **cannot be correlated by hash value** with records written after.

Rotate only when operationally mandated. When rotation is required:
1. Document the rotation date and reason in your internal compliance log.
2. Note that cross-period record linkage by `policyholder_id` hash will require the raw identifier (not the hash) as the join key.

---

## Production Checklist

Complete this checklist before serving live traffic.

- [ ] `API_KEY` set to a random secret (not the placeholder value from `.env.example`)
- [ ] `PII_HASH_SALT` set to a random secret (not `insurance-workflow-v1`)
- [ ] `ANTHROPIC_API_KEY` configured, or `LLM_PROVIDER=none` set intentionally
- [ ] FAISS index artifacts present at configured paths; `GET /health` → `retrieval.available: true`
- [ ] `data/` directory on persistent storage (survives container restarts)
- [ ] Smoke test passes: `python tests/smoke_day3.py`
- [ ] Evaluation harness passes: `python eval/harness.py`
- [ ] Health check endpoint wired to load balancer or container orchestrator health check
- [ ] Structured log output forwarded to aggregation system
- [ ] Reviewer team briefed on `workflow_mode`, `hard_decline`, and `severity_tier` fields
- [ ] Alert rules configured for `retrieval.available: false` and `status: degraded`
- [ ] `PII_HASH_SALT` stored in secrets manager, not in `.env` file committed to source control
