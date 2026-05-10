"""
Deterministic risk calculator.

Produces a numeric risk proxy (0–100) from structured policyholder fields.
This is a hard, rule-based component — output is fully reproducible and
auditable without an LLM.
"""

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

RULES_VERSION = "v1.0"


@dataclass
class RiskCalcResult:
    score: float           # 0–100
    level: str             # Low | Medium | High
    breakdown: dict        # per-factor contributions
    rules_version: str


class RiskCalculator:
    """
    Weighted, deterministic risk scorer.

    Weights:
      age            0.20
      credit_score   0.30
      income         0.20
      claims_history 0.30
    """

    _WEIGHTS = {"age": 0.20, "credit_score": 0.30, "income": 0.20, "claims_history": 0.30}

    def calculate(self, data: dict[str, Any]) -> RiskCalcResult:
        try:
            age_raw = self._age_risk(data.get("age", 0))
            credit_raw = self._credit_risk(data.get("credit_score", 0))
            income_raw = self._income_risk(data.get("annual_income", 0))
            claims_raw = self._claims_risk(data.get("claims_history", []))

            score = (
                age_raw    * self._WEIGHTS["age"] +
                credit_raw * self._WEIGHTS["credit_score"] +
                income_raw * self._WEIGHTS["income"] +
                claims_raw * self._WEIGHTS["claims_history"]
            )
            score = round(max(0.0, min(100.0, score)), 2)
            level = "High" if score >= 70 else "Medium" if score >= 40 else "Low"

            breakdown = {
                "age_raw": age_raw,
                "credit_raw": credit_raw,
                "income_raw": income_raw,
                "claims_raw": claims_raw,
            }

            logger.info(
                "RiskCalculator: id=%s score=%.1f level=%s",
                data.get("policyholder_id", "?"),
                score,
                level,
            )
            return RiskCalcResult(score=score, level=level, breakdown=breakdown,
                                  rules_version=RULES_VERSION)

        except Exception:
            logger.exception("RiskCalculator error — returning default 50.0")
            return RiskCalcResult(score=50.0, level="Medium", breakdown={},
                                  rules_version=RULES_VERSION)

    # ------------------------------------------------------------------ #
    #  Sub-scorers (each returns 0–100 before weighting)                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _age_risk(age: int) -> float:
        if age < 25 or age > 65:
            return 100.0
        if age < 30:
            return 75.0
        if age < 40:
            return 25.0
        return 0.0

    @staticmethod
    def _credit_risk(score: int) -> float:
        if score < 500:
            return 100.0
        if score < 600:
            return 83.0
        if score < 700:
            return 50.0
        if score < 750:
            return 17.0
        return 0.0

    @staticmethod
    def _income_risk(income: float) -> float:
        if income < 30_000:
            return 100.0
        if income < 50_000:
            return 75.0
        if income < 75_000:
            return 40.0
        if income < 100_000:
            return 15.0
        return 0.0

    @staticmethod
    def _claims_risk(claims: list) -> float:
        n = len(claims)
        if n >= 5:
            return 100.0
        if n >= 3:
            return 83.0
        if n == 2:
            return 50.0
        if n == 1:
            return 27.0
        return 0.0
