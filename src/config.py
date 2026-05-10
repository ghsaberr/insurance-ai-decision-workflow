"""
YAML config loader.

Reads config/config.yaml and populates os.environ defaults for any
variable not already set.  Environment variables always take precedence.

Call load() once at application startup, before instantiating any
component that reads os.environ (orchestrator, case_manager, etc.).
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parents[1] / "config" / "config.yaml"

_YAML_TO_ENV: dict[tuple[str, str], str] = {
    ("llm",       "provider"):         "LLM_PROVIDER",
    ("llm",       "anthropic_model"):  "ANTHROPIC_MODEL",
    ("llm",       "ollama_model"):     "OLLAMA_MODEL",
    ("llm",       "ollama_url"):       "OLLAMA_URL",
    ("retrieval", "index_path"):       "FAISS_INDEX_PATH",
    ("retrieval", "metadata_path"):    "FAISS_METADATA_PATH",
    ("retrieval", "embedding_model"):  "EMBEDDING_MODEL",
    ("severity",  "model_path"):       "SEVERITY_MODEL_PATH",
    ("storage",   "db_path"):          "DB_PATH",
}


def load() -> None:
    """
    Load config/config.yaml and apply values as os.environ defaults.

    Safe to call multiple times (subsequent calls are no-ops once env
    vars are set).  Silently continues if the file is absent or
    unparseable so the application can still start from env vars alone.
    """
    if not _CONFIG_PATH.exists():
        logger.debug("config.yaml not found at %s — env vars only", _CONFIG_PATH)
        return

    try:
        import yaml
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    except ImportError:
        logger.warning("pyyaml not installed — config.yaml not loaded; using env vars only")
        return
    except Exception:
        logger.exception("Failed to parse config.yaml — using env vars only")
        return

    applied: list[str] = []
    for (section, key), env_var in _YAML_TO_ENV.items():
        value = cfg.get(section, {}).get(key)
        if value is not None and str(value).strip() and env_var not in os.environ:
            os.environ[env_var] = str(value)
            applied.append(env_var)

    if applied:
        logger.info("config.yaml applied defaults for: %s", ", ".join(applied))
    else:
        logger.debug("config.yaml loaded — all settings already in environment")
