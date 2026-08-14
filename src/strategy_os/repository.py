"""Atomic, tenant-safe filesystem repository."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Any, Dict, Iterable, List, Optional

from .errors import ConflictError, ImmutableRecordError, NotFoundError, ValidationError
from .lifecycle import require_cycle_transition, require_project_transition
from .models import (
    CycleManifest,
    CycleStatus,
    FeedbackEvent,
    MemoryChange,
    MemoryProposal,
    MemoryProposalStatus,
    Project,
    ProjectStatus,
    QualityFinding,
    StageAttempt,
    utc_now,
    validate_id,
)


class VaultRepository:
    """Canonical Strategy OS store rooted at a caller-supplied directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def sha256_text(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def create_project(self, project: Project, brief_markdown: str) -> Project:
        project.validate()
        if project.status != ProjectStatus.DRAFT or project.revision != 1:
            raise ValidationError("new projects must be draft at revision 1")
        if not brief_markdown.strip():
            raise ValidationError("brief_markdown is required")
        project_dir = self._project_dir(project.organisation_id, project.brand_id, project.project_id)
        with self._lock:
            self._write_once(project_dir / "brief.md", brief_markdown.encode("utf-8"))
            self._write_once(project_dir / "project.json", self._json_bytes(project.to_dict()))
        return project

    def get_project(self, organisation_id: str, brand_id: str, project_id: str) -> Project:
        path = self._project_dir(organisation_id, brand_id, project_id) / "project.json"
        data = self._read_json(path)
        return Project(
            **{**data, "status": ProjectStatus(data["status"])}
        )

    def update_project_status(
        self,
        organisation_id: str,
        brand_id: str,
        project_id: str,
        target: ProjectStatus,
        expected_revision: int,
        current_cycle_id: Optional[str] = None,
        approved_document_version: Optional[str] = None,
    ) -> Project:
        with self._lock:
            current = self.get_project(organisation_id, brand_id, project_id)
            if current.revision != expected_revision:
                raise ConflictError("project revision changed")
            require_project_transition(current.status, target)
            updated = replace(
                current,
                status=target,
                revision=current.revision + 1,
                current_cycle_id=current_cycle_id if current_cycle_id is not None else current.current_cycle_id,
                approved_document_version=(
                    approved_document_version
                    if approved_document_version is not None
                    else current.approved_document_version
                ),
            )
            updated.validate()
            path = self._project_dir(organisation_id, brand_id, project_id) / "project.json"
            self._atomic_write(path, self._json_bytes(updated.to_dict()))
            return updated

    def delete_project(self, organisation_id: str, brand_id: str, project_id: str) -> None:
        with self._lock:
            project = self.get_project(organisation_id, brand_id, project_id)
            if project.current_cycle_id and project.status == ProjectStatus.RUNNING:
                active = self.get_cycle(organisation_id, brand_id, project_id, project.current_cycle_id)
                if active.status in {CycleStatus.QUEUED, CycleStatus.RUNNING, CycleStatus.RECONCILING, CycleStatus.REVIEW}:
                    raise ConflictError("cannot delete a project while a cycle is running")
            shutil.rmtree(self._project_dir(organisation_id, brand_id, project_id))

    def create_cycle(self, cycle: CycleManifest) -> CycleManifest:
        cycle.validate()
        project = self.get_project(cycle.organisation_id, cycle.brand_id, cycle.project_id)
        if project.current_cycle_id is not None and project.status == ProjectStatus.RUNNING:
            active = self.get_cycle(cycle.organisation_id, cycle.brand_id, cycle.project_id, project.current_cycle_id)
            if active.status in {CycleStatus.QUEUED, CycleStatus.RUNNING, CycleStatus.RECONCILING, CycleStatus.REVIEW}:
                raise ConflictError("an active cycle already exists for this project")
        path = self._cycle_dir(cycle.organisation_id, cycle.brand_id, cycle.project_id, cycle.cycle_id) / "manifest.json"
        with self._lock:
            self._write_once(path, self._json_bytes(cycle.to_dict()))
            self.update_project_status(
                cycle.organisation_id,
                cycle.brand_id,
                cycle.project_id,
                ProjectStatus.RUNNING,
                expected_revision=project.revision,
                current_cycle_id=cycle.cycle_id,
            )
        return cycle

    def get_cycle(self, organisation_id: str, brand_id: str, project_id: str, cycle_id: str) -> CycleManifest:
        path = self._cycle_dir(organisation_id, brand_id, project_id, cycle_id) / "manifest.json"
        data = self._read_json(path)
        return CycleManifest(**{**data, "status": CycleStatus(data["status"])})

    def update_cycle_status(
        self,
        organisation_id: str,
        brand_id: str,
        project_id: str,
        cycle_id: str,
        target: CycleStatus,
        expected_revision: int,
    ) -> CycleManifest:
        with self._lock:
            current = self.get_cycle(organisation_id, brand_id, project_id, cycle_id)
            if current.revision != expected_revision:
                raise ConflictError("cycle revision changed")
            require_cycle_transition(current.status, target)
            completed_at = utc_now() if target == CycleStatus.COMPLETED else current.completed_at
            updated = replace(current, status=target, completed_at=completed_at, revision=current.revision + 1)
            updated.validate()
            path = self._cycle_dir(organisation_id, brand_id, project_id, cycle_id) / "manifest.json"
            self._atomic_write(path, self._json_bytes(updated.to_dict()))
            if target in {CycleStatus.COMPLETED, CycleStatus.FAILED}:
                project = self.get_project(organisation_id, brand_id, project_id)
                if project.status == ProjectStatus.RUNNING:
                    self.update_project_status(
                        organisation_id,
                        brand_id,
                        project_id,
                        ProjectStatus.REVIEW,
                        expected_revision=project.revision,
                    )
            return updated

    def write_stage_attempt(
        self,
        organisation_id: str,
        brand_id: str,
        project_id: str,
        attempt: StageAttempt,
    ) -> StageAttempt:
        attempt.validate()
        self.get_cycle(organisation_id, brand_id, project_id, attempt.cycle_id)
        path = (
            self._cycle_dir(organisation_id, brand_id, project_id, attempt.cycle_id)
            / "attempts" / attempt.stage_id / (attempt.attempt_id + ".json")
        )
        self._write_once(path, self._json_bytes(attempt.to_dict()))
        return attempt

    def add_quality_finding(
        self,
        organisation_id: str,
        brand_id: str,
        project_id: str,
        finding: QualityFinding,
    ) -> QualityFinding:
        finding.validate()
        cycle_dir = self._cycle_dir(organisation_id, brand_id, project_id, finding.cycle_id)
        self._read_json(cycle_dir / "manifest.json")
        finding_path = cycle_dir / "quality" / (finding.finding_id + ".json")
        ledger_path = cycle_dir / "quality-ledger.jsonl"
        with self._lock:
            self._write_once(finding_path, self._json_bytes(finding.to_dict()))
            existing = ledger_path.read_bytes() if ledger_path.exists() else b""
            line = json.dumps(finding.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
            self._atomic_write(ledger_path, existing + line)
        return finding

    def add_feedback(self, feedback: FeedbackEvent) -> FeedbackEvent:
        feedback.validate()
        self.get_cycle(feedback.organisation_id, feedback.brand_id, feedback.project_id, feedback.cycle_id)
        path = (
            self._project_dir(feedback.organisation_id, feedback.brand_id, feedback.project_id)
            / "feedback" / (feedback.feedback_id + ".json")
        )
        self._write_once(path, self._json_bytes(feedback.to_dict()))
        return feedback

    def create_memory_proposal(self, proposal: MemoryProposal) -> MemoryProposal:
        proposal.validate()
        self.get_cycle(proposal.organisation_id, proposal.brand_id, proposal.project_id, proposal.cycle_id)
        path = self._memory_dir(proposal.organisation_id, proposal.brand_id) / "proposals" / (proposal.proposal_id + ".json")
        self._write_once(path, self._json_bytes(proposal.to_dict()))
        return proposal

    def decide_memory_proposal(
        self,
        organisation_id: str,
        brand_id: str,
        proposal_id: str,
        actor_id: str,
        approved_change_ids: Iterable[str],
        decision_id: str,
        memory_version: Optional[str],
    ) -> Dict[str, Any]:
        for value, name in (
            (organisation_id, "organisation_id"), (brand_id, "brand_id"),
            (proposal_id, "proposal_id"), (actor_id, "actor_id"), (decision_id, "decision_id")
        ):
            validate_id(value, name)
        proposal_path = self._memory_dir(organisation_id, brand_id) / "proposals" / (proposal_id + ".json")
        proposal_data = self._read_json(proposal_path)
        if proposal_data["status"] != MemoryProposalStatus.PROPOSED.value:
            raise ConflictError("memory proposal has already been decided")
        changes = proposal_data["changes"]
        available = {item["change_id"] for item in changes}
        approved = set(approved_change_ids)
        unknown = approved - available
        if unknown:
            raise ValidationError("unknown approved change_ids: %s" % sorted(unknown))
        if approved and memory_version is None:
            raise ValidationError("memory_version is required when changes are approved")
        if memory_version is not None:
            validate_id(memory_version, "memory_version")
        memory_dir = self._memory_dir(organisation_id, brand_id)
        current_json_path = memory_dir / "CURRENT.json"
        current_data = self._read_json(current_json_path) if current_json_path.exists() else {
            "schema_version": "1.0", "memory_version": None, "sections": {}
        }
        if proposal_data.get("base_memory_version") != current_data.get("memory_version"):
            raise ConflictError("memory proposal base version is stale")
        status = (
            MemoryProposalStatus.REJECTED if not approved else
            MemoryProposalStatus.APPROVED if approved == available else
            MemoryProposalStatus.PARTIALLY_APPROVED
        )
        decision = {
            "schema_version": "1.0",
            "decision_id": decision_id,
            "proposal_id": proposal_id,
            "actor_id": actor_id,
            "approved_change_ids": sorted(approved),
            "rejected_change_ids": sorted(available - approved),
            "status": status.value,
            "memory_version": memory_version,
            "created_at": utc_now(),
        }
        decision_path = self._memory_dir(organisation_id, brand_id) / "decisions" / (decision_id + ".json")
        with self._lock:
            self._write_once(decision_path, self._json_bytes(decision))
            if approved:
                selected = [item for item in changes if item["change_id"] in approved]
                sections = dict(current_data.get("sections", {}))
                for item in selected:
                    sections[item["section"]] = {
                        "content": item["content"],
                        "evidence_ids": item["evidence_ids"],
                        "operation": item["operation"],
                        "proposal_id": proposal_id,
                    }
                memory_data = {
                    "schema_version": "1.0",
                    "memory_version": memory_version,
                    "previous_memory_version": current_data.get("memory_version"),
                    "promoted_from_proposal": proposal_id,
                    "sections": sections,
                }
                markdown = self._memory_markdown(memory_version or "", proposal_id, sections)
                version_path = self._memory_dir(organisation_id, brand_id) / "versions" / ((memory_version or "") + ".md")
                self._write_once(version_path, markdown.encode("utf-8"))
                self._write_once(
                    self._memory_dir(organisation_id, brand_id) / "versions" / ((memory_version or "") + ".json"),
                    self._json_bytes(memory_data),
                )
                self._atomic_write(self._memory_dir(organisation_id, brand_id) / "CURRENT.md", markdown.encode("utf-8"))
                self._atomic_write(current_json_path, self._json_bytes(memory_data))
            decided_proposal = {**proposal_data, "status": status.value, "decision_id": decision_id}
            self._atomic_write(proposal_path, self._json_bytes(decided_proposal))
        return decision

    def rebuild_index(self) -> Dict[str, List[str]]:
        """Return canonical IDs discoverable without a database."""
        projects: List[str] = []
        cycles: List[str] = []
        for path in self.root.glob("organisations/*/brands/*/projects/*/project.json"):
            projects.append(self._read_json(path)["project_id"])
        for path in self.root.glob("organisations/*/brands/*/projects/*/cycles/*/manifest.json"):
            cycles.append(self._read_json(path)["cycle_id"])
        return {"projects": sorted(projects), "cycles": sorted(cycles)}

    def _project_dir(self, organisation_id: str, brand_id: str, project_id: str) -> Path:
        return self._scoped_path(
            "organisations", organisation_id, "brands", brand_id, "projects", project_id
        )

    def _cycle_dir(self, organisation_id: str, brand_id: str, project_id: str, cycle_id: str) -> Path:
        return self._project_dir(organisation_id, brand_id, project_id) / "cycles" / self._segment(cycle_id, "cycle_id")

    def _memory_dir(self, organisation_id: str, brand_id: str) -> Path:
        return self._scoped_path("organisations", organisation_id, "brands", brand_id, "memory")

    def _scoped_path(self, *parts: str) -> Path:
        cleaned = [self._segment(part, "path segment") if index % 2 == 1 else part for index, part in enumerate(parts)]
        candidate = self.root.joinpath(*cleaned).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValidationError("path escapes repository root")
        return candidate

    @staticmethod
    def _segment(value: str, field_name: str) -> str:
        validate_id(value, field_name)
        return value

    @staticmethod
    def _json_bytes(value: Dict[str, Any]) -> bytes:
        return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")

    @staticmethod
    def _memory_markdown(memory_version: str, proposal_id: str, sections: Dict[str, Dict[str, Any]]) -> str:
        lines = [
            "---", 'title: "Approved Brand Memory"', "schema_version: \"1.0\"",
            'memory_version: "%s"' % memory_version,
            'promoted_from_proposal: "%s"' % proposal_id, "---", "",
        ]
        for section_name in sorted(sections):
            section = sections[section_name]
            lines.extend(["## %s" % section_name.replace("_", " ").title(), "", section["content"], ""])
        return "\n".join(lines)

    def _write_once(self, path: Path, content: bytes) -> None:
        if path.exists():
            raise ImmutableRecordError("record already exists: %s" % path.name)
        self._atomic_write(path, content)

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary_path), str(path))
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise NotFoundError("record not found") from exc
