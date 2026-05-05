"""
PII masking layer.

Identifies sensitive fields in policyholder data and applies the appropriate
treatment before the data is persisted in the audit database or exported.

What is stored (hashed):  policyholder_id, annual_income, credit_score
What is stored (plain):   age, policy_type, claims_count, risk signals
What is never stored:     raw name, address, SSN, DOB (not in current schema)

See docs/governance.md — Data Retention Policy section.
"""

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

_HASH_FIELDS = frozenset({"policyholder_id", "annual_income", "credit_score"})
_DROP_FIELDS = frozenset({"name", "address", "ssn", "date_of_birth", "email", "phone"})
_SALT = "insurance-workflow-v1"


def _hash(value: Any) -> str:
    raw = f"{_SALT}:{value}"
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def mask_for_storage(data: dict[str, Any]) -> dict[str, Any]:
    """
    Return a copy of *data* safe for audit-DB storage.

    - Fields in _DROP_FIELDS are removed entirely.
    - Fields in _HASH_FIELDS are replaced with a deterministic hash prefix
      so records remain linkable without exposing the raw value.
    """
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k in _DROP_FIELDS:
            continue
        if k in _HASH_FIELDS:
            out[k] = _hash(v)
        else:
            out[k] = v
    return out


def redact_for_export(data: dict[str, Any]) -> dict[str, Any]:
    """
    Redact all sensitive fields for external/export use.
    Hash fields are replaced with '[REDACTED]'.
    """
    out = dict(data)
    for k in _HASH_FIELDS | _DROP_FIELDS:
        if k in out:
            out[k] = "[REDACTED]"
    return out


def safe_claims_count(claims: list | None) -> int:
    """Return only the count of claims — never the raw claims records."""
    return len(claims) if claims else 0


def strip_pii_from_request(data: dict[str, Any]) -> dict[str, Any]:
    """
    Produce a PII-clean dict suitable for passing to the LLM prompt.
    The LLM must never see raw PII (income, credit score as absolute numbers,
    policyholder_id).  Replace with risk-tier labels instead.
    """
    income = float(data.get("annual_income", 0))
    credit = int(data.get("credit_score", 0))

    income_tier = (
        "high" if income >= 100_000 else
        "medium" if income >= 50_000 else
        "low"
    )
    credit_tier = (
        "excellent" if credit >= 750 else
        "good"      if credit >= 700 else
        "fair"      if credit >= 600 else
        "poor"
    )

    return {
        "age": data.get("age"),
        "policy_type": data.get("policy_type"),
        "income_tier": income_tier,
        "credit_tier": credit_tier,
        "claims_count": safe_claims_count(data.get("claims_history")),
    }
