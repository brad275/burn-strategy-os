"""Versioned Strategy OS record contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import re
from typing import Any, Dict, List, Optional

from .errors import ValidationError


SCHEMA_VERSION = "1.0"
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,127}$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValidationError(
            "%s must start with a lowercase letter and contain only "
            "lowercase letters, numbers, underscores, or hyphens" % field_name
        )


def validate_timestamp(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValidationError("%s is required" % field_name)
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValidationError("%s must be ISO 8601" % field_name) from exc
    if parsed.tzinfo is None:
        raise ValidationError("%s must include a timezone" % field_name)


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s is required" % field_name)


class StringEnum(str, Enum):
    pass


class ProjectStatus(StringEnum):
    DRAFT = "draft"
    RUNNING = "running"
    REVIEW = "review"
    APPROVED = "approved"
    ARCHIVED = "archived"


class CycleStatus(StringEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RECONCILING = "reconciling"
    REVIEW = "review"
    COMPLETED = "completed"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class StageAttemptStatus(StringEnum):
    PRODUCED = "produced"
    VERIFYING = "verifying"
    REVISING = "revising"
    PASSED = "passed"
    FAILED = "failed"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


class QualityResult(StringEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class FeedbackVerdict(StringEnum):
    APPROVE = "approve"
    CHALLENGE = "challenge"
    CORRECT = "correct"
    REJECT = "reject"


class MemoryProposalStatus(StringEnum):
    PROPOSED = "proposed"
    PARTIALLY_APPROVED = "partially_approved"
    APPROVED = "approved"
    REJECTED = "rejected"


class MemoryOperation(StringEnum):
    ADD = "add"
    AMEND = "amend"
    SUPERSEDE = "supersede"


@dataclass
class Record:
    schema_version: str = SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValidationError("unsupported schema_version: %s" % self.schema_version)

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return _enum_values(asdict(self))


def _enum_values(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _enum_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_enum_values(item) for item in value]
    return value


@dataclass
class Project(Record):
    project_id: str = ""
    organisation_id: str = ""
    brand_id: str = ""
    name: str = ""
    status: ProjectStatus = ProjectStatus.DRAFT
    created_by: str = ""
    created_at: str = field(default_factory=utc_now)
    current_cycle_id: Optional[str] = None
    approved_document_version: Optional[str] = None
    revision: int = 1

    def validate(self) -> None:
        super().validate()
        for name in ("project_id", "organisation_id", "brand_id", "created_by"):
            validate_id(getattr(self, name), name)
        _require_text(self.name, "name")
        validate_timestamp(self.created_at, "created_at")
        if self.current_cycle_id is not None:
            validate_id(self.current_cycle_id, "current_cycle_id")
        if self.approved_document_version is not None:
            validate_id(self.approved_document_version, "approved_document_version")
        if self.revision < 1:
            raise ValidationError("revision must be positive")


@dataclass
class CycleManifest(Record):
    cycle_id: str = ""
    organisation_id: str = ""
    brand_id: str = ""
    project_id: str = ""
    status: CycleStatus = CycleStatus.QUEUED
    brief_sha256: str = ""
    memory_version_in: Optional[str] = None
    feedback_ids: List[str] = field(default_factory=list)
    prompt_versions: Dict[str, str] = field(default_factory=dict)
    code_version: str = "uncommitted"
    started_at: str = field(default_factory=utc_now)
    completed_at: Optional[str] = None
    revision: int = 1

    def validate(self) -> None:
        super().validate()
        for name in ("cycle_id", "organisation_id", "brand_id", "project_id"):
            validate_id(getattr(self, name), name)
        if not SHA256_PATTERN.fullmatch(self.brief_sha256):
            raise ValidationError("brief_sha256 must be a lowercase SHA-256 hex digest")
        if self.memory_version_in is not None:
            validate_id(self.memory_version_in, "memory_version_in")
        for feedback_id in self.feedback_ids:
            validate_id(feedback_id, "feedback_id")
        validate_timestamp(self.started_at, "started_at")
        if self.completed_at is not None:
            validate_timestamp(self.completed_at, "completed_at")
        if self.status == CycleStatus.COMPLETED and self.completed_at is None:
            raise ValidationError("completed cycles require completed_at")
        if self.revision < 1:
            raise ValidationError("revision must be positive")


@dataclass
class StageAttempt(Record):
    attempt_id: str = ""
    cycle_id: str = ""
    stage_id: str = ""
    attempt_number: int = 1
    status: StageAttemptStatus = StageAttemptStatus.PRODUCED
    input_hash: str = ""
    output: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def validate(self) -> None:
        super().validate()
        for name in ("attempt_id", "cycle_id", "stage_id"):
            validate_id(getattr(self, name), name)
        if self.attempt_number not in (1, 2, 3):
            raise ValidationError("attempt_number must be 1, 2, or 3")
        if not SHA256_PATTERN.fullmatch(self.input_hash):
            raise ValidationError("input_hash must be a lowercase SHA-256 hex digest")
        if not isinstance(self.output, dict):
            raise ValidationError("output must be an object")
        validate_timestamp(self.created_at, "created_at")


@dataclass
class QualityFinding(Record):
    finding_id: str = ""
    cycle_id: str = ""
    stage_id: str = ""
    attempt_id: str = ""
    check: str = ""
    result: QualityResult = QualityResult.PASS
    summary: str = ""
    evidence_refs: List[str] = field(default_factory=list)
    required_action: Optional[str] = None
    resolved_by_attempt: Optional[str] = None
    created_at: str = field(default_factory=utc_now)

    def validate(self) -> None:
        super().validate()
        for name in ("finding_id", "cycle_id", "stage_id", "attempt_id", "check"):
            validate_id(getattr(self, name), name)
        _require_text(self.summary, "summary")
        if self.result == QualityResult.FAIL and not self.required_action:
            raise ValidationError("failed quality findings require required_action")
        for evidence_ref in self.evidence_refs:
            validate_id(evidence_ref, "evidence_ref")
        if self.resolved_by_attempt is not None:
            validate_id(self.resolved_by_attempt, "resolved_by_attempt")
        validate_timestamp(self.created_at, "created_at")


@dataclass
class FeedbackEvent(Record):
    feedback_id: str = ""
    organisation_id: str = ""
    brand_id: str = ""
    project_id: str = ""
    cycle_id: str = ""
    target_type: str = ""
    target_id: str = ""
    verdict: FeedbackVerdict = FeedbackVerdict.APPROVE
    reason: Optional[str] = None
    replacement: Optional[str] = None
    created_by: str = ""
    created_at: str = field(default_factory=utc_now)

    def validate(self) -> None:
        super().validate()
        for name in (
            "feedback_id", "organisation_id", "brand_id", "project_id",
            "cycle_id", "target_id", "created_by"
        ):
            validate_id(getattr(self, name), name)
        if self.target_type not in ("record", "stage_output", "document"):
            raise ValidationError("target_type is invalid")
        if self.verdict != FeedbackVerdict.APPROVE and not self.reason:
            raise ValidationError("challenge, correct, and reject feedback require a reason")
        validate_timestamp(self.created_at, "created_at")


@dataclass
class MemoryChange:
    change_id: str
    operation: MemoryOperation
    section: str
    content: str
    evidence_ids: List[str]

    def validate(self) -> None:
        validate_id(self.change_id, "change_id")
        validate_id(self.section, "section")
        _require_text(self.content, "content")
        if not self.evidence_ids:
            raise ValidationError("memory changes require evidence_ids")
        for evidence_id in self.evidence_ids:
            validate_id(evidence_id, "evidence_id")


@dataclass
class MemoryProposal(Record):
    proposal_id: str = ""
    organisation_id: str = ""
    brand_id: str = ""
    project_id: str = ""
    cycle_id: str = ""
    base_memory_version: Optional[str] = None
    changes: List[MemoryChange] = field(default_factory=list)
    status: MemoryProposalStatus = MemoryProposalStatus.PROPOSED
    created_at: str = field(default_factory=utc_now)

    def validate(self) -> None:
        super().validate()
        for name in ("proposal_id", "organisation_id", "brand_id", "project_id", "cycle_id"):
            validate_id(getattr(self, name), name)
        if self.base_memory_version is not None:
            validate_id(self.base_memory_version, "base_memory_version")
        if not self.changes:
            raise ValidationError("memory proposals require at least one change")
        for change in self.changes:
            change.validate()
        validate_timestamp(self.created_at, "created_at")
