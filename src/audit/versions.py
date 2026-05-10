"""
Workflow version manifest.

Every audit record, API response, and case row stores these four version
strings.  Increment the relevant constant whenever the corresponding
component changes so that recommendations remain traceable to the exact
system state that produced them.

Versioning policy (see docs/governance.md):
  model_version   — bump when the LLM model ID or parameters change
  prompt_version  — bump when the system/user prompt templates change
  rules_version   — bump when RuleChecker or RiskCalculator thresholds change
  kb_version      — set at index-build time by the ingestion pipeline;
                    the DocumentLoader reads this from kb_version.txt
"""

from dataclasses import dataclass

PROMPT_VERSION = "v1.1"               # bumped: LLM schema now includes risk_score
RULES_VERSION = "v1.0"
KB_VERSION_FALLBACK = "unknown"        # overridden at runtime by DocumentLoader
_MODEL_VERSION_FALLBACK = "unknown"    # overridden at runtime by the active LLM client


@dataclass(frozen=True)
class VersionManifest:
    model_version: str
    prompt_version: str
    rules_version: str
    kb_version: str

    def to_dict(self) -> dict:
        return {
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "rules_version": self.rules_version,
            "kb_version": self.kb_version,
        }


def build_manifest(
    kb_version: str = KB_VERSION_FALLBACK,
    model_version: str | None = None,
) -> VersionManifest:
    """
    Build the manifest for the current run using live component versions.

    model_version should be passed from the active LLM client so the audit
    record reflects the actual model in use, not a hardcoded constant.
    """
    return VersionManifest(
        model_version=model_version or _MODEL_VERSION_FALLBACK,
        prompt_version=PROMPT_VERSION,
        rules_version=RULES_VERSION,
        kb_version=kb_version,
    )
