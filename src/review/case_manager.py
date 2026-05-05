"""
SQLite-backed case manager.

Every assessment request creates a persistent Case record.  A reviewer then
acts on that case (approve / reject / escalate / request_info).  The action
is stored and the full history is queryable.

This replaces the in-memory `audit_trail = []` that existed in the prototype.
Data survives process restarts, can be exported, and is the authoritative
record of every workflow decision.

Schema
------
cases
  case_id          TEXT  PK
  request_id       TEXT
  created_at       TEXT  ISO-8601
  status           TEXT  pending_review | approved | rejected | escalated | info_requested
  policyholder_id  TEXT  hashed by privacy layer
  recommendation   TEXT  JSON
  evidence_refs    TEXT  JSON array of doc_ids
  rule_findings    TEXT  JSON
  severity_tier    TEXT  LOW | MEDIUM | HIGH | CRITICAL
  model_version    TEXT
  prompt_version   TEXT
  kb_version       TEXT
  rules_version    TEXT
  llm_available    INTEGER  0 | 1
  workflow_mode    TEXT  full | deterministic_only
  reviewer_id      TEXT
  reviewer_action  TEXT
  reviewer_notes   TEXT
  reviewed_at      TEXT  ISO-8601

audit_events
  id               INTEGER PK AUTOINCREMENT
  case_id          TEXT
  event_at         TEXT  ISO-8601
  event_type       TEXT  case_created | review_submitted | status_changed
  actor            TEXT
  detail           TEXT  JSON
"""

import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DB_PATH_DEFAULT = str(Path(__file__).parents[2] / "data" / "workflow.db")

_CREATE_CASES = """
CREATE TABLE IF NOT EXISTS cases (
    case_id          TEXT PRIMARY KEY,
    request_id       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending_review',
    policyholder_id  TEXT,
    recommendation   TEXT,
    evidence_refs    TEXT,
    rule_findings    TEXT,
    severity_tier    TEXT,
    model_version    TEXT,
    prompt_version   TEXT,
    kb_version       TEXT,
    rules_version    TEXT,
    llm_available    INTEGER DEFAULT 1,
    workflow_mode    TEXT DEFAULT 'full',
    reviewer_id      TEXT,
    reviewer_action  TEXT,
    reviewer_notes   TEXT,
    reviewed_at      TEXT
);
"""

_CREATE_AUDIT = """
CREATE TABLE IF NOT EXISTS audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL,
    event_at    TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    actor       TEXT,
    detail      TEXT
);
"""

_VALID_ACTIONS = frozenset({"approve", "reject", "escalate", "request_info"})

_TERMINAL_STATUSES = frozenset({"approved", "rejected"})


@dataclass
class Case:
    case_id: str
    request_id: str
    created_at: str
    status: str
    policyholder_id: str
    recommendation: dict
    evidence_refs: list[str]
    rule_findings: dict
    severity_tier: str
    model_version: str
    prompt_version: str
    kb_version: str
    rules_version: str
    llm_available: bool
    workflow_mode: str
    reviewer_id: str | None = None
    reviewer_action: str | None = None
    reviewer_notes: str | None = None
    reviewed_at: str | None = None


@dataclass
class ReviewRequest:
    reviewer_id: str
    action: str                           # approve | reject | escalate | request_info
    notes: str = ""


class CaseManager:
    """
    Thread-safe SQLite case manager.

    A single CaseManager instance is shared across FastAPI requests via
    application state.  Each operation opens its own connection from the
    pool — SQLite's WAL mode allows concurrent readers and one writer.
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or _DB_PATH_DEFAULT
        self._lock = threading.Lock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def create_case(
        self,
        request_id: str,
        policyholder_id_hashed: str,
        recommendation: dict,
        evidence_refs: list[str],
        rule_findings: dict,
        severity_tier: str,
        versions: dict,
        llm_available: bool,
        workflow_mode: str,
    ) -> Case:
        """Persist a new case and return the Case object."""
        case_id = str(uuid.uuid4())
        now = _now()

        case = Case(
            case_id=case_id,
            request_id=request_id,
            created_at=now,
            status="pending_review",
            policyholder_id=policyholder_id_hashed,
            recommendation=recommendation,
            evidence_refs=evidence_refs,
            rule_findings=rule_findings,
            severity_tier=severity_tier,
            model_version=versions.get("model_version", ""),
            prompt_version=versions.get("prompt_version", ""),
            kb_version=versions.get("kb_version", ""),
            rules_version=versions.get("rules_version", ""),
            llm_available=llm_available,
            workflow_mode=workflow_mode,
        )

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO cases (
                    case_id, request_id, created_at, status,
                    policyholder_id, recommendation, evidence_refs, rule_findings,
                    severity_tier, model_version, prompt_version, kb_version,
                    rules_version, llm_available, workflow_mode
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    case_id, request_id, now, "pending_review",
                    policyholder_id_hashed,
                    json.dumps(recommendation),
                    json.dumps(evidence_refs),
                    json.dumps(rule_findings),
                    severity_tier,
                    versions.get("model_version", ""),
                    versions.get("prompt_version", ""),
                    versions.get("kb_version", ""),
                    versions.get("rules_version", ""),
                    int(llm_available),
                    workflow_mode,
                ),
            )
            self._log_event(conn, case_id, "case_created", actor="system",
                            detail={"workflow_mode": workflow_mode,
                                    "severity_tier": severity_tier})

        logger.info("Case created: case_id=%s status=pending_review", case_id)
        return case

    def submit_review(self, case_id: str, review: ReviewRequest) -> Case:
        """Record a reviewer action and update the case status."""
        if review.action not in _VALID_ACTIONS:
            raise ValueError(
                f"Invalid action '{review.action}'. "
                f"Must be one of: {sorted(_VALID_ACTIONS)}"
            )

        action_to_status = {
            "approve":      "approved",
            "reject":       "rejected",
            "escalate":     "escalated",
            "request_info": "info_requested",
        }
        new_status = action_to_status[review.action]
        now = _now()

        with self._conn() as conn:
            row = conn.execute(
                "SELECT status FROM cases WHERE case_id = ?", (case_id,)
            ).fetchone()

            if row is None:
                raise KeyError(f"Case not found: {case_id}")

            if row["status"] in _TERMINAL_STATUSES:
                raise ValueError(
                    f"Case {case_id} is already in terminal status '{row['status']}' "
                    "and cannot be updated."
                )

            conn.execute(
                """
                UPDATE cases
                SET status=?, reviewer_id=?, reviewer_action=?,
                    reviewer_notes=?, reviewed_at=?
                WHERE case_id=?
                """,
                (new_status, review.reviewer_id, review.action,
                 review.notes, now, case_id),
            )
            self._log_event(
                conn, case_id, "review_submitted",
                actor=review.reviewer_id,
                detail={"action": review.action, "new_status": new_status,
                        "notes": review.notes[:200] if review.notes else ""},
            )

        logger.info(
            "Review submitted: case_id=%s action=%s new_status=%s reviewer=%s",
            case_id, review.action, new_status, review.reviewer_id,
        )
        return self.get_case(case_id)

    def get_case(self, case_id: str) -> Case:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM cases WHERE case_id = ?", (case_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Case not found: {case_id}")
        return _row_to_case(row)

    def list_cases(
        self,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Case]:
        query = "SELECT * FROM cases"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_case(r) for r in rows]

    def get_case_events(self, case_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE case_id = ? ORDER BY event_at",
                (case_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def export_audit_jsonl(self) -> str:
        """Return all cases as a JSONL string suitable for audit export."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM cases ORDER BY created_at"
            ).fetchall()
        lines = [json.dumps(dict(r)) for r in rows]
        return "\n".join(lines)

    def count_by_status(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM cases GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ------------------------------------------------------------------ #
    #  Internals                                                           #
    # ------------------------------------------------------------------ #

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(_CREATE_CASES)
            conn.execute(_CREATE_AUDIT)

    @staticmethod
    def _log_event(
        conn: sqlite3.Connection,
        case_id: str,
        event_type: str,
        actor: str,
        detail: dict,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_events (case_id, event_at, event_type, actor, detail) "
            "VALUES (?,?,?,?,?)",
            (case_id, _now(), event_type, actor, json.dumps(detail)),
        )


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_case(row: sqlite3.Row) -> Case:
    d = dict(row)
    return Case(
        case_id=d["case_id"],
        request_id=d["request_id"],
        created_at=d["created_at"],
        status=d["status"],
        policyholder_id=d["policyholder_id"],
        recommendation=json.loads(d["recommendation"] or "{}"),
        evidence_refs=json.loads(d["evidence_refs"] or "[]"),
        rule_findings=json.loads(d["rule_findings"] or "{}"),
        severity_tier=d["severity_tier"] or "UNKNOWN",
        model_version=d["model_version"] or "",
        prompt_version=d["prompt_version"] or "",
        kb_version=d["kb_version"] or "",
        rules_version=d["rules_version"] or "",
        llm_available=bool(d.get("llm_available", 1)),
        workflow_mode=d.get("workflow_mode") or "full",
        reviewer_id=d["reviewer_id"],
        reviewer_action=d["reviewer_action"],
        reviewer_notes=d["reviewer_notes"],
        reviewed_at=d["reviewed_at"],
    )
