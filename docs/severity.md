# Severity Scoring: Role in the Workflow

## What the severity score is

The `SeverityScorer` estimates the `UltimateIncurredClaimCost` for a case
and maps it to a business tier.  It is powered by the fine-tuned LoRA model
from the `claims-severity-prediction` repository, with an honest rule-based
fallback when the model is unavailable.

This is **not** the same as the risk score.  The risk score (`0–100`)
measures the probability of an adverse outcome.  The severity tier measures
the *magnitude* of the financial exposure if a claim materialises.

A young driver with a clean record may have a low risk score but a HIGH
severity tier on a commercial policy — the risk of a claim is low, but the
cost if it happens is high.  Both signals matter.

---

## Tier definitions

| Tier | Estimated Cost | Colour | Business meaning |
|------|---------------|--------|-----------------|
| LOW | < $5,000 | Green | Standard policy; routine reserve |
| MEDIUM | $5k – $25k | Amber | Moderate exposure; verify coverage limits |
| HIGH | $25k – $100k | Orange | Specialist reviewer required before decision |
| CRITICAL | > $100k | Red | Auto-escalated to senior underwriter |

---

## How severity drives case routing

```
Assessment completed
        │
        ▼
severity_tier == CRITICAL?
    YES → case.status = "escalated"  (immediate, on creation)
          audit_event: "auto_escalated", reason: "severity_tier=CRITICAL"
          → Senior underwriter queue
    NO  → case.status = "pending_review"
          → Standard or priority reviewer queue based on tier
```

The routing is enforced in `src/review/case_manager.create_case()`.
CRITICAL cases cannot enter `pending_review` — they are escalated
automatically and the audit log records the reason.

Reviewers can always override by submitting a `reject` or `approve` action,
but the escalation is the default path and the audit trail shows it happened.

---

## What the reviewer sees

Every case response includes:

```json
{
  "severity_tier":            "HIGH",
  "severity_estimated_cost":  48000.0,
  "severity_note":            "Specialist reviewer required. Estimated cost $25k–$100k."
}
```

The `severity_note` is a plain-English routing instruction — not a model
metric.  Reviewers do not need to interpret a cost distribution; they see
a direct instruction.

---

## Severity mode: model vs rule-based

The `SeverityScorer` operates in one of two modes:

| `severity_mode` | When active | Confidence |
|-----------------|------------|------------|
| `model` | `claims_severity_model/` found + GPU/peft/transformers available | ~0.78 |
| `rule_based` | Model absent or deps missing | ~0.55 |

Both modes produce a `SeverityResult` with the same fields.
The mode is always declared — reviewers can weight their judgment accordingly.

To enable model mode:
```bash
# Point to the fine-tuned LoRA adapter
export SEVERITY_MODEL_PATH=../Claims-Severity-Prediction/claims_severity_model
# Install deps (GPU recommended)
pip install peft transformers torch accelerate
```

`GET /health` → `subsystems.severity_mode` shows which mode is active.

---

## Relationship to claims-severity-prediction

The fine-tuned model in `claims-severity-prediction/claims_severity_model/`
was trained to predict `UltimateIncurredClaimCost` via LoRA/QLoRA on a
causal language model.  See that repo's `claims_severity_technical_brief.md`
for training details (LoRA rank, 4-bit quantisation, VRAM management).

The `SeverityScorer` in this repo wraps that model and:
1. Translates the numeric cost prediction into a business tier
2. Provides a rule-based fallback so the workflow operates without GPU
3. Adds the plain-English `business_note` that the reviewer sees
4. Drives the automatic CRITICAL escalation routing

The claims-severity-prediction repo remains the authoritative source for
model training and evaluation.  The `SeverityScorer` is the consumption
interface inside this workflow.
