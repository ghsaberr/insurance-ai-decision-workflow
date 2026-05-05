"""
FAISS-based document retriever.

Consumes the artifacts produced by the insurance-nlp-aws ingestion pipeline:
  - insurance_faiss.index      (FAISS flat-L2 index)
  - insurance_metadata.json    (doc_id / policy_id / s3_key per vector)

The boundary is explicit: insurance-nlp-aws owns extraction and indexing;
this module owns retrieval inside the workflow.  See docs/ingestion_boundary.md.
"""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

KB_VERSION_FILE = "kb_version.txt"


@dataclass
class RetrievedDoc:
    doc_id: str
    policy_id: str
    content_snippet: str
    distance: float
    metadata: dict


class DocumentLoader:
    """
    Loads the FAISS index built by insurance-nlp-aws and exposes a single
    `retrieve(query, top_k)` method consumed by the workflow orchestrator.

    If the index files are absent the loader initialises in degraded mode
    and `retrieve()` returns an empty list.  This is surfaced explicitly in
    the workflow response — it is never hidden.
    """

    def __init__(
        self,
        index_path: str | None = None,
        metadata_path: str | None = None,
        embedding_model: str = "all-MiniLM-L6-v2",
    ):
        self.index_path = index_path or os.getenv(
            "FAISS_INDEX_PATH",
            str(Path(__file__).parents[2] / "data" / "insurance_faiss.index"),
        )
        self.metadata_path = metadata_path or os.getenv(
            "FAISS_METADATA_PATH",
            str(Path(__file__).parents[2] / "data" / "insurance_metadata.json"),
        )
        self.embedding_model_name = embedding_model
        self._index = None
        self._metadata: list[dict] = []
        self._model = None
        self._available = False
        self.kb_version = self._read_kb_version()

        self._initialise()

    # ------------------------------------------------------------------ #
    #  Public interface                                                    #
    # ------------------------------------------------------------------ #

    def retrieve(self, query: str, top_k: int = 3) -> list[RetrievedDoc]:
        """Return top_k documents most relevant to *query*."""
        if not self._available:
            logger.warning("DocumentLoader not available — returning empty results")
            return []

        try:
            import numpy as np

            vec = self._model.encode([query]).astype("float32")
            distances, indices = self._index.search(vec, top_k)

            results: list[RetrievedDoc] = []
            for dist, idx in zip(distances[0], indices[0]):
                if idx < 0 or idx >= len(self._metadata):
                    continue
                meta = self._metadata[idx]
                results.append(
                    RetrievedDoc(
                        doc_id=meta.get("doc_id", f"DOC_{idx}"),
                        policy_id=meta.get("policy_id", "unknown"),
                        content_snippet=meta.get("text_snippet", ""),
                        distance=float(dist),
                        metadata=meta,
                    )
                )

            logger.info("DocumentLoader: query='%s' hits=%d", query[:60], len(results))
            return results

        except Exception:
            logger.exception("DocumentLoader retrieval error")
            return []

    @property
    def is_available(self) -> bool:
        return self._available

    def health(self) -> dict[str, Any]:
        return {
            "available": self._available,
            "index_path": self.index_path,
            "doc_count": self._index.ntotal if self._index else 0,
            "kb_version": self.kb_version,
        }

    # ------------------------------------------------------------------ #
    #  Internals                                                           #
    # ------------------------------------------------------------------ #

    def _initialise(self) -> None:
        if not Path(self.index_path).exists():
            logger.warning(
                "FAISS index not found at %s. "
                "Run the insurance-nlp-aws pipeline to build it: "
                "`python run_pipeline.py --local`",
                self.index_path,
            )
            return

        if not Path(self.metadata_path).exists():
            logger.warning("FAISS metadata not found at %s", self.metadata_path)
            return

        try:
            import faiss
            from sentence_transformers import SentenceTransformer

            self._index = faiss.read_index(self.index_path)
            with open(self.metadata_path, encoding="utf-8") as f:
                self._metadata = json.load(f)

            self._model = SentenceTransformer(self.embedding_model_name)
            self._available = True
            logger.info(
                "DocumentLoader ready: %d vectors, kb_version=%s",
                self._index.ntotal,
                self.kb_version,
            )
        except ImportError as exc:
            logger.warning("DocumentLoader deps missing (%s) — retrieval disabled", exc)
        except Exception:
            logger.exception("DocumentLoader init error")

    def _read_kb_version(self) -> str:
        version_path = Path(self.index_path or "").parent / KB_VERSION_FILE
        if version_path.exists():
            return version_path.read_text().strip()
        # Fall back to index modification date if version file absent
        p = Path(self.index_path or "")
        if p.exists():
            from datetime import datetime
            mtime = datetime.fromtimestamp(p.stat().st_mtime)
            return f"faiss-{mtime.strftime('%Y-%m-%d')}"
        return "unknown"
