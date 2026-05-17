"""
Claims severity scorer.

Wraps the fine-tuned LoRA model from claims-severity-prediction and exposes
a single `score()` method used by the workflow orchestrator.

Role in the workflow
--------------------
The severity tier is a triage signal, not a risk decision:

  LOW       (<$5 000)     → standard review queue
  MEDIUM    ($5k–$25k)    → priority review
  HIGH      ($25k–$100k)  → specialist reviewer required
  CRITICAL  (>$100k)      → auto-escalate to senior underwriter

This directly controls case routing (see case_manager.py) and gives the
reviewer context that risk score alone cannot provide.

Implementation modes
--------------------
1. Model mode   — loads adapter_model.safetensors (GPU recommended)
2. Rule mode    — deterministic tiers from claim amount / count / type
                  used when model path is absent or GPU unavailable

Both modes use the same SeverityResult interface.  The mode is surfaced
in every response so reviewers know which path ran.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MODEL_PATH_DEFAULT = str(
    Path(__file__).parents[3] / "claims-severity-prediction" / "claims_severity_model"
)

_TIER_THRESHOLDS = {
    "LOW":      5_000,
    "MEDIUM":  25_000,
    "HIGH":   100_000,
}


@dataclass
class SeverityResult:
    tier: str                    # LOW | MEDIUM | HIGH | CRITICAL
    estimated_cost_usd: float    # point estimate from model or rule
    confidence: float            # 0–1
    mode: str                    # "model" | "rule_based"
    business_note: str           # human-readable routing guidance


_BUSINESS_NOTES = {
    "LOW":      "Standard review queue. Estimated cost below $5,000.",
    "MEDIUM":   "Priority review. Estimated cost $5k–$25k; may require additional documentation.",
    "HIGH":     "Specialist reviewer required. Estimated cost $25k–$100k.",
    "CRITICAL": "Auto-escalated to senior underwriter. Estimated cost exceeds $100k.",
}


class SeverityScorer:
    """
    Severity estimation component.

    Initialises in model mode if the adapter path exists and torch/transformers
    are importable.  Falls back to rule-based mode transparently — the mode
    is always declared in SeverityResult.mode so the reviewer knows which path ran.
    """

    def __init__(self, model_path: str | None = None):
        self.model_path = model_path or os.getenv(
            "SEVERITY_MODEL_PATH", _MODEL_PATH_DEFAULT
        )
        self._model = None
        self._tokenizer = None
        self._mode = "rule_based"
        self._try_load_model()

    def score(self, data: dict[str, Any]) -> SeverityResult:
        if self._mode == "model":
            return self._score_model(data)
        return self._score_rules(data)

    @property
    def mode(self) -> str:
        return self._mode

    # ------------------------------------------------------------------ #
    #  Model-based scoring                                                 #
    # ------------------------------------------------------------------ #

    def _score_model(self, data: dict[str, Any]) -> SeverityResult:
        try:
            import torch

            prompt = self._build_prompt(data)
            inputs = self._tokenizer(
                prompt, return_tensors="pt", truncation=True, max_length=512
            )
            with torch.no_grad():
                outputs = self._model.generate(
                    **inputs,
                    max_new_tokens=20,
                    do_sample=False,
                    pad_token_id=self._tokenizer.eos_token_id,
                )
            generated = self._tokenizer.decode(
                outputs[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True
            ).strip()

            cost = self._parse_cost(generated)
            tier = self._cost_to_tier(cost)
            return SeverityResult(
                tier=tier,
                estimated_cost_usd=cost,
                confidence=0.78,
                mode="model",
                business_note=_BUSINESS_NOTES[tier],
            )
        except Exception:
            logger.exception("Model inference failed — falling back to rule-based")
            return self._score_rules(data)

    # ------------------------------------------------------------------ #
    #  Rule-based fallback (always available, fully deterministic)         #
    # ------------------------------------------------------------------ #

    def _score_rules(self, data: dict[str, Any]) -> SeverityResult:
        """
        Deterministic tier from structured claim fields.
        Used when the fine-tuned model is unavailable.
        """
        claims = data.get("claims_history") or []
        claim_count = len(claims)

        # Use most recent high-value claim if present
        max_claim_amount = max(
            (float(c.get("amount", 0)) for c in claims), default=0.0
        )

        income = float(data.get("annual_income", 0))
        credit = int(data.get("credit_score", 0))
        age = int(data.get("age", 0))

        # Base estimate from max claim amount
        if max_claim_amount > 0:
            base = max_claim_amount
        else:
            # Proxy from risk factors when no prior claims
            base = 3_000
            if income < 30_000:
                base += 5_000
            if credit < 500:
                base += 8_000
            if age < 25 or age > 70:
                base += 4_000
            if claim_count > 0:
                base += claim_count * 3_000

        tier = self._cost_to_tier(base)
        return SeverityResult(
            tier=tier,
            estimated_cost_usd=round(base, 2),
            confidence=0.55,
            mode="rule_based",
            business_note=_BUSINESS_NOTES[tier],
        )

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _try_load_model(self) -> None:
        adapter_file = Path(self.model_path) / "adapter_model.safetensors"
        if not adapter_file.exists():
            logger.info(
                "Severity model not found at %s — using rule-based mode. "
                "Point SEVERITY_MODEL_PATH to claims_severity_model/ to enable model mode.",
                self.model_path,
            )
            return

        try:
            from peft import AutoPeftModelForCausalLM
            from transformers import AutoTokenizer
            import torch

            self._tokenizer = AutoTokenizer.from_pretrained(self.model_path)
            self._model = AutoPeftModelForCausalLM.from_pretrained(
                self.model_path,
                torch_dtype=torch.float32,
                device_map="auto",
            )
            self._model.eval()
            self._mode = "model"
            logger.info("SeverityScorer: model mode loaded from %s", self.model_path)
        except ImportError:
            logger.info("peft/transformers not installed — rule-based mode")
        except Exception:
            logger.exception("SeverityScorer model load error — rule-based mode")

    @staticmethod
    def _build_prompt(data: dict[str, Any]) -> str:
        claims = data.get("claims_history") or []
        return (
            f"Policy type: {data.get('policy_type', 'unknown')}. "
            f"Claims count: {len(claims)}. "
            f"Predict the UltimateIncurredClaimCost in USD as a single number:"
        )

    @staticmethod
    def _parse_cost(text: str) -> float:
        import re
        match = re.search(r"[\d,]+\.?\d*", text.replace(",", ""))
        if match:
            return float(match.group().replace(",", ""))
        return 5_000.0

    @staticmethod
    def _cost_to_tier(cost: float) -> str:
        if cost < _TIER_THRESHOLDS["LOW"]:
            return "LOW"
        if cost < _TIER_THRESHOLDS["MEDIUM"]:
            return "MEDIUM"
        if cost < _TIER_THRESHOLDS["HIGH"]:
            return "HIGH"
        return "CRITICAL"
