"""
Deterministic rule checker.

Runs a fixed set of business rules against policyholder data and returns
binary flags, required actions, and rule violations.  All logic here is
explicit and fully auditable — no LLM involvement.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

RULES_VERSION = "v1.0"


@dataclass
class RuleCheckResult:
    flags: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    compliance_score: int = 100           # 100 − (violations × 10), floor 0
    hard_decline: bool = False            # True → workflow must not approve
    rules_version: str = RULES_VERSION


class RuleChecker:
    """
    Business-rule evaluator.

    Rules are versioned via RULES_VERSION.  Increment that constant whenever
    thresholds or logic change so audit records remain traceable.
    """

    _RULES = {
        "age":     {"min": 18, "max": 80, "senior": 65, "young": 25},
        "credit":  {"excellent": 750, "good": 700, "fair": 600, "poor": 500},
        "claims":  {"max_per_year": 2, "high_severity_threshold": 10_000, "recent_days": 365},
        "income":  {"min_absolute": 20_000, "min_ratio": 0.10},
    }

    def check_all(self, data: dict[str, Any]) -> RuleCheckResult:
        result = RuleCheckResult()
        try:
            self._check_age(data.get("age", 0), result)
            self._check_credit(data.get("credit_score", 0), result)
            self._check_claims(data.get("claims_history", []),
                               data.get("policy_start_date"), result)
            self._check_income(data.get("annual_income", 0),
                               data.get("premium_amount", 0), result)

            result.compliance_score = max(0, 100 - len(result.violations) * 10)
            result.hard_decline = (
                "age_below_minimum" in result.violations or
                "age_above_maximum" in result.violations
            )

            logger.info(
                "RuleChecker: id=%s violations=%d compliance=%d hard_decline=%s",
                data.get("policyholder_id", "?"),
                len(result.violations),
                result.compliance_score,
                result.hard_decline,
            )
        except Exception:
            logger.exception("RuleChecker error")
            result.flags.append("rule_check_error")
            result.actions.append("manual_review_required")
            result.violations.append("system_error")
            result.compliance_score = 50

        return result

    # ------------------------------------------------------------------ #
    #  Individual rule groups                                              #
    # ------------------------------------------------------------------ #

    def _check_age(self, age: int, r: RuleCheckResult) -> None:
        cfg = self._RULES["age"]
        if age < cfg["min"]:
            r.flags.append("underage_applicant")
            r.actions.append("reject_application")
            r.violations.append("age_below_minimum")
        elif age > cfg["max"]:
            r.flags.append("overage_applicant")
            r.actions.append("senior_underwriting_review")
            r.violations.append("age_above_maximum")
        else:
            if age >= cfg["senior"]:
                r.flags.append("senior_applicant")
                r.actions.append("senior_underwriting_review")
            if age <= cfg["young"]:
                r.flags.append("young_applicant")
                r.actions.append("young_driver_review")

    def _check_credit(self, score: int, r: RuleCheckResult) -> None:
        cfg = self._RULES["credit"]
        if score < cfg["poor"]:
            r.flags.append("poor_credit")
            r.actions.append("high_premium_required")
            r.violations.append("credit_below_threshold")
        elif score < cfg["fair"]:
            r.flags.append("fair_credit")
            r.actions.append("standard_underwriting")
        elif score < cfg["good"]:
            r.flags.append("good_credit")
            r.actions.append("preferred_underwriting")
        else:
            r.flags.append("excellent_credit")
            r.actions.append("preferred_underwriting")

    def _check_claims(
        self, claims: list, policy_start_date: str | None, r: RuleCheckResult
    ) -> None:
        cfg = self._RULES["claims"]
        if not claims:
            r.flags.append("no_claims_history")
            r.actions.append("standard_underwriting")
            return

        recent = 0
        high_severity = 0
        for claim in claims:
            if policy_start_date:
                try:
                    claim_dt = datetime.strptime(claim.get("date", ""), "%Y-%m-%d")
                    policy_dt = datetime.strptime(policy_start_date, "%Y-%m-%d")
                    if (policy_dt - claim_dt).days <= cfg["recent_days"]:
                        recent += 1
                except (ValueError, TypeError):
                    pass
            if float(claim.get("amount", 0)) > cfg["high_severity_threshold"]:
                high_severity += 1

        if recent > cfg["max_per_year"]:
            r.flags.append("excessive_recent_claims")
            r.actions.append("high_risk_underwriting")
            r.violations.append("too_many_recent_claims")
        if high_severity >= 1:
            r.flags.append("high_severity_claims")
            r.actions.append("specialist_review_required")
            r.violations.append("high_severity_claims_detected")
        if recent > 0:
            r.flags.append("recent_claims_present")
            r.actions.append("claims_history_review")

    def _check_income(self, income: float, premium: float, r: RuleCheckResult) -> None:
        cfg = self._RULES["income"]
        if income < cfg["min_absolute"]:
            r.flags.append("income_below_minimum")
            r.actions.append("income_verification_required")
            r.violations.append("income_below_minimum")

        if premium > 0 and income / premium < cfg["min_ratio"]:
            r.flags.append("high_premium_ratio")
            r.actions.append("premium_adjustment_required")
            r.violations.append("premium_too_high_for_income")

        if income >= 100_000:
            r.flags.append("high_income")
            r.actions.append("preferred_underwriting")
        elif income >= 50_000:
            r.flags.append("medium_income")
            r.actions.append("standard_underwriting")
        else:
            r.flags.append("low_income")
            r.actions.append("basic_underwriting")
