"""Evidence-led workflow runner with immutable attempts and bounded revisions."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import ValidationError
from .models import (
    CycleStatus, MemoryChange, MemoryOperation, MemoryProposal, QualityFinding,
    QualityResult, StageAttempt, StageAttemptStatus,
)
from .provider import ProviderError, StrategyProvider
from .repository import VaultRepository
from .semantic import validate_stage_output


STAGES = ("source_collection", "research", "competitive", "audience", "reconciliation", "strategy", "opportunities", "document_assembly", "memory_proposal")


class WorkflowRunner:
    def __init__(self, repository: VaultRepository, provider: StrategyProvider) -> None:
        self.repository = repository
        self.provider = provider

    def run(self, organisation_id: str, brand_id: str, project_id: str, cycle_id: str) -> None:
        cycle = self.repository.get_cycle(organisation_id, brand_id, project_id, cycle_id)
        if cycle.status == CycleStatus.QUEUED:
            cycle = self.repository.update_cycle_status(organisation_id, brand_id, project_id, cycle_id, CycleStatus.RUNNING, cycle.revision)
        project_dir = self.repository.root / "organisations" / organisation_id / "brands" / brand_id / "projects" / project_id
        context: dict[str, Any] = {"cycle_id": cycle_id, "brand_slug": brand_id}
        active_stage = "source_collection"
        try:
            brief = (project_dir / "brief.md").read_text(encoding="utf-8")
            self._source_collection(organisation_id, brand_id, project_id, cycle_id, brief, context)
            for stage in ("research", "competitive", "audience"):
                active_stage = stage
                context[stage] = self._execute_stage(organisation_id, brand_id, project_id, cycle_id, stage, brief, context)
            cycle = self.repository.get_cycle(organisation_id, brand_id, project_id, cycle_id)
            if cycle.status == CycleStatus.RUNNING:
                cycle = self.repository.update_cycle_status(organisation_id, brand_id, project_id, cycle_id, CycleStatus.RECONCILING, cycle.revision)
            active_stage = "reconciliation"
            context["reconciliation"] = self._execute_stage(organisation_id, brand_id, project_id, cycle_id, active_stage, brief, context)
            context["coverage_map"] = context["reconciliation"].get("coverage_map", {})
            for stage in ("strategy", "opportunities", "document_assembly", "memory_proposal"):
                active_stage = stage
                context[stage] = self._execute_stage(organisation_id, brand_id, project_id, cycle_id, stage, brief, context)
                if stage == "document_assembly":
                    context["full_document_word_count"] = context[stage].get("full_document_word_count")
            self._create_memory_proposal(organisation_id, brand_id, project_id, cycle_id, context["memory_proposal"])
            self._write_document(organisation_id, brand_id, project_id, cycle_id, context["document_assembly"])
            cycle = self.repository.get_cycle(organisation_id, brand_id, project_id, cycle_id)
            cycle = self.repository.update_cycle_status(organisation_id, brand_id, project_id, cycle_id, CycleStatus.REVIEW, cycle.revision)
            self.repository.update_cycle_status(organisation_id, brand_id, project_id, cycle_id, CycleStatus.COMPLETED, cycle.revision)
        except Exception as exc:
            cycle = self.repository.get_cycle(organisation_id, brand_id, project_id, cycle_id)
            if cycle.status not in {CycleStatus.COMPLETED, CycleStatus.FAILED}:
                self.repository.update_cycle_status(organisation_id, brand_id, project_id, cycle_id, CycleStatus.FAILED, cycle.revision)
            message = str(exc) if isinstance(exc, (ProviderError, ValidationError)) else "The cycle stopped during %s because an unexpected internal error occurred." % active_stage.replace("_", " ")
            self._write_event(organisation_id, brand_id, project_id, cycle_id, "workflow_failed", {
                "stage": active_stage,
                "message": message[:500],
                "action": "Review this reason, adjust the brief or provider configuration if indicated, then start a new cycle.",
            })
            raise

    def _source_collection(self, organisation_id: str, brand_id: str, project_id: str, cycle_id: str, brief: str, context: dict[str, Any]) -> None:
        output = {"stage": "source_collection", "cycle_id": cycle_id, "brand_slug": brand_id, "sources": self.provider.collect_sources(brief, context)}
        self._write_attempt(organisation_id, brand_id, project_id, cycle_id, "source_collection", 1, output, StageAttemptStatus.PASSED)
        context["source_collection"] = output

    def _execute_stage(self, organisation_id: str, brand_id: str, project_id: str, cycle_id: str, stage: str, brief: str, context: dict[str, Any]) -> dict[str, Any]:
        for revision in (1, 2):
            output = self.provider.generate(stage, brief, context, revision)
            issues = validate_stage_output(output, context)
            errors = [issue for issue in issues if issue.severity == "error"]
            status = StageAttemptStatus.PASSED if not errors else (StageAttemptStatus.REVISING if revision == 1 else StageAttemptStatus.NEEDS_HUMAN_REVIEW)
            attempt = self._write_attempt(organisation_id, brand_id, project_id, cycle_id, stage, revision, output, status)
            for issue in issues:
                self.repository.add_quality_finding(organisation_id, brand_id, project_id, QualityFinding(
                    finding_id="quality-" + uuid4().hex[:20], cycle_id=cycle_id, stage_id=stage, attempt_id=attempt.attempt_id,
                    check=issue.check_id.lower().replace("_", "-"), result=QualityResult.FAIL if issue.severity == "error" else QualityResult.WARN,
                    summary=issue.message, required_action="Revise this stage." if issue.severity == "error" else None,
                ))
            if not errors:
                self._write_event(organisation_id, brand_id, project_id, cycle_id, "stage_passed", {"stage": stage, "attempt": revision})
                return output
        raise ValidationError("%s needs human review after two revisions" % stage)

    def _write_attempt(self, organisation_id: str, brand_id: str, project_id: str, cycle_id: str, stage: str, number: int, output: dict[str, Any], status: StageAttemptStatus) -> StageAttempt:
        payload = json.dumps(output, sort_keys=True, default=str)
        attempt = StageAttempt(attempt_id="attempt-" + uuid4().hex[:18], cycle_id=cycle_id, stage_id=stage, attempt_number=number, status=status, input_hash=hashlib.sha256(payload.encode()).hexdigest(), output=output)
        return self.repository.write_stage_attempt(organisation_id, brand_id, project_id, attempt)

    def _create_memory_proposal(self, organisation_id: str, brand_id: str, project_id: str, cycle_id: str, output: dict[str, Any]) -> None:
        changes = [MemoryChange(change_id=item["change_id"], operation=MemoryOperation(item["operation"]), section=item["section"], content=item["content"], evidence_ids=item["evidence_ids"]) for item in output["changes"]]
        self.repository.create_memory_proposal(MemoryProposal(proposal_id="proposal-" + uuid4().hex[:18], organisation_id=organisation_id, brand_id=brand_id, project_id=project_id, cycle_id=cycle_id, changes=changes))

    def _write_document(self, organisation_id: str, brand_id: str, project_id: str, cycle_id: str, output: dict[str, Any]) -> None:
        directory = self.repository.root / "organisations" / organisation_id / "brands" / brand_id / "projects" / project_id / "cycles" / cycle_id
        (directory / "strategy-document.md").write_text("# Strategy document\n\n" + output["full_document"], encoding="utf-8")
        (directory / "strategy-document.json").write_text(json.dumps(output, indent=2), encoding="utf-8")

    def _write_event(self, organisation_id: str, brand_id: str, project_id: str, cycle_id: str, kind: str, detail: dict[str, Any]) -> None:
        path = self.repository.root / "organisations" / organisation_id / "brands" / brand_id / "projects" / project_id / "cycles" / cycle_id / "learning-ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": kind, "detail": detail}) + "\n")
