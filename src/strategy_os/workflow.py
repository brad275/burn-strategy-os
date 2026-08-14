"""Evidence-led workflow runner with immutable attempts and bounded revisions."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from .errors import ValidationError
from .models import (
    CycleStatus, MemoryChange, MemoryOperation, MemoryProposal, ProjectStatus, QualityFinding,
    QualityResult, StageAttempt, StageAttemptStatus,
)
from .provider import ProviderError, StrategyProvider, _claim_ids
from .repository import VaultRepository
from .semantic import validate_stage_output


STAGES = ("source_collection", "research", "competitive", "audience", "reconciliation", "strategy", "opportunities", "document_assembly", "memory_proposal")
EVIDENCE_STAGES = ("research", "competitive", "audience")
SYNTHESIS_STAGES = ("strategy", "opportunities", "document_assembly", "memory_proposal")
RESUME_FROM = {"research", "strategy"}
STRATEGY_PREREQUISITES = ("source_collection", "research", "competitive", "audience", "reconciliation")


class WorkflowRunner:
    def __init__(self, repository: VaultRepository, provider: StrategyProvider) -> None:
        self.repository = repository
        self.provider = provider

    def run(self, organisation_id: str, brand_id: str, project_id: str, cycle_id: str, from_stage: str | None = None) -> None:
        active_stage = "source_collection"
        replace_outputs = from_stage is not None
        try:
            if from_stage is not None:
                if from_stage not in RESUME_FROM:
                    raise ValidationError("resume is only allowed from research or strategy")
                if not self.repository.latest_passed_stage_output(organisation_id, brand_id, project_id, cycle_id, "source_collection"):
                    raise ValidationError("cannot resume without passed source collection")
                if from_stage == "strategy":
                    for stage in STRATEGY_PREREQUISITES:
                        if not self.repository.latest_passed_stage_output(organisation_id, brand_id, project_id, cycle_id, stage):
                            raise ValidationError("cannot resume from strategy without passed %s" % stage)
            cycle = self.repository.get_cycle(organisation_id, brand_id, project_id, cycle_id)
            if cycle.status in {CycleStatus.QUEUED, CycleStatus.FAILED, CycleStatus.COMPLETED}:
                cycle = self.repository.update_cycle_status(organisation_id, brand_id, project_id, cycle_id, CycleStatus.RUNNING, cycle.revision)
            project = self.repository.get_project(organisation_id, brand_id, project_id)
            if project.status == ProjectStatus.REVIEW:
                self.repository.update_project_status(organisation_id, brand_id, project_id, ProjectStatus.RUNNING, project.revision, current_cycle_id=cycle_id)
            project_dir = self.repository.root / "organisations" / organisation_id / "brands" / brand_id / "projects" / project_id
            context: dict[str, Any] = {"cycle_id": cycle_id, "brand_slug": brand_id}
            for stage in STAGES:
                passed = self.repository.latest_passed_stage_output(organisation_id, brand_id, project_id, cycle_id, stage)
                if passed:
                    context[stage] = passed
            if from_stage:
                for stage in STAGES[STAGES.index(from_stage):]:
                    context.pop(stage, None)
            if context.get("memory_proposal") and not self._memory_proposal_exists(organisation_id, brand_id, cycle_id):
                del context["memory_proposal"]
            brief = (project_dir / "brief.md").read_text(encoding="utf-8")
            if "source_collection" not in context:
                self._source_collection(organisation_id, brand_id, project_id, cycle_id, brief, context)
            for stage in EVIDENCE_STAGES:
                active_stage = stage
                if stage not in context:
                    context[stage] = self._execute_stage(organisation_id, brand_id, project_id, cycle_id, stage, brief, context)
            cycle = self.repository.get_cycle(organisation_id, brand_id, project_id, cycle_id)
            if cycle.status == CycleStatus.RUNNING:
                cycle = self.repository.update_cycle_status(organisation_id, brand_id, project_id, cycle_id, CycleStatus.RECONCILING, cycle.revision)
            active_stage = "reconciliation"
            if "reconciliation" not in context:
                context["reconciliation"] = self._local_reconciliation(organisation_id, brand_id, project_id, cycle_id, context)
            context["coverage_map"] = context["reconciliation"].get("coverage_map", {})
            context["claim_ids"] = _claim_ids(context)
            memory_rerun = "memory_proposal" not in context
            for stage in SYNTHESIS_STAGES:
                active_stage = stage
                if stage not in context:
                    context[stage] = self._execute_stage(organisation_id, brand_id, project_id, cycle_id, stage, brief, context)
                if stage == "document_assembly":
                    assembled = context[stage]
                    context["full_document_word_count"] = assembled.get("full_document_word_count") or len(str(assembled.get("full_document") or assembled.get("markdown") or "").split())
            self._create_memory_proposal(organisation_id, brand_id, project_id, cycle_id, context["memory_proposal"], force_new=memory_rerun)
            self._write_document(organisation_id, brand_id, project_id, cycle_id, context["document_assembly"], replace=replace_outputs)
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
                "action": "Retry this cycle from the stopped stage. Passed stages will not be re-run.",
            })
            raise

    def _source_collection(self, organisation_id: str, brand_id: str, project_id: str, cycle_id: str, brief: str, context: dict[str, Any]) -> None:
        output = {"stage": "source_collection", "cycle_id": cycle_id, "brand_slug": brand_id, "sources": self.provider.collect_sources(brief, context)}
        number = self.repository.next_stage_attempt_number(organisation_id, brand_id, project_id, cycle_id, "source_collection")
        self._write_attempt(organisation_id, brand_id, project_id, cycle_id, "source_collection", number, output, StageAttemptStatus.PASSED)
        context["source_collection"] = output

    def _local_reconciliation(self, organisation_id: str, brand_id: str, project_id: str, cycle_id: str, context: dict[str, Any]) -> dict[str, Any]:
        output = build_reconciliation(context)
        number = self.repository.next_stage_attempt_number(organisation_id, brand_id, project_id, cycle_id, "reconciliation")
        issues = validate_stage_output(output, context)
        errors = [issue for issue in issues if issue.severity == "error"]
        if errors:
            raise ValidationError("reconciliation could not be computed from passed evidence")
        self._write_attempt(organisation_id, brand_id, project_id, cycle_id, "reconciliation", number, output, StageAttemptStatus.PASSED)
        self._write_event(organisation_id, brand_id, project_id, cycle_id, "stage_passed", {"stage": "reconciliation", "attempt": number, "computed": True})
        return output

    def _execute_stage(self, organisation_id: str, brand_id: str, project_id: str, cycle_id: str, stage: str, brief: str, context: dict[str, Any]) -> dict[str, Any]:
        start = self.repository.next_stage_attempt_number(organisation_id, brand_id, project_id, cycle_id, stage)
        for index, number in enumerate((start, start + 1)):
            revision = index + 1
            output = self.provider.generate(stage, brief, context, revision)
            issues = validate_stage_output(output, context)
            errors = [issue for issue in issues if issue.severity == "error"]
            status = StageAttemptStatus.PASSED if not errors else (StageAttemptStatus.REVISING if index == 0 else StageAttemptStatus.NEEDS_HUMAN_REVIEW)
            attempt = self._write_attempt(organisation_id, brand_id, project_id, cycle_id, stage, number, output, status)
            for issue in issues:
                self.repository.add_quality_finding(organisation_id, brand_id, project_id, QualityFinding(
                    finding_id="quality-" + uuid4().hex[:20], cycle_id=cycle_id, stage_id=stage, attempt_id=attempt.attempt_id,
                    check=issue.check_id.lower().replace("_", "-"), result=QualityResult.FAIL if issue.severity == "error" else QualityResult.WARN,
                    summary=issue.message, required_action="Revise this stage." if issue.severity == "error" else None,
                ))
            if not errors:
                self._write_event(organisation_id, brand_id, project_id, cycle_id, "stage_passed", {"stage": stage, "attempt": number})
                return output
        raise ValidationError("%s needs human review after two revisions" % stage)

    def _write_attempt(self, organisation_id: str, brand_id: str, project_id: str, cycle_id: str, stage: str, number: int, output: dict[str, Any], status: StageAttemptStatus) -> StageAttempt:
        payload = json.dumps(output, sort_keys=True, default=str)
        attempt = StageAttempt(attempt_id="attempt-" + uuid4().hex[:18], cycle_id=cycle_id, stage_id=stage, attempt_number=number, status=status, input_hash=hashlib.sha256(payload.encode()).hexdigest(), output=output)
        return self.repository.write_stage_attempt(organisation_id, brand_id, project_id, attempt)

    def _memory_proposal_exists(self, organisation_id: str, brand_id: str, cycle_id: str) -> bool:
        directory = self.repository.root / "organisations" / organisation_id / "brands" / brand_id / "memory" / "proposals"
        if not directory.exists():
            return False
        for path in directory.glob("*.json"):
            if json.loads(path.read_text(encoding="utf-8")).get("cycle_id") == cycle_id:
                return True
        return False

    def _create_memory_proposal(self, organisation_id: str, brand_id: str, project_id: str, cycle_id: str, output: dict[str, Any], force_new: bool = False) -> None:
        if not force_new and self._memory_proposal_exists(organisation_id, brand_id, cycle_id):
            return
        raw_changes = output.get("changes") if isinstance(output.get("changes"), list) else []
        if not raw_changes:
            raise ValidationError("memory proposals require at least one change")
        changes = []
        for item in raw_changes:
            if not isinstance(item, dict):
                raise ValidationError("memory changes require evidence_ids")
            refs = item.get("evidence_ids") or item.get("evidence_refs") or []
            changes.append(MemoryChange(
                change_id=item.get("change_id") or ("change-" + uuid4().hex[:12]),
                operation=MemoryOperation(item.get("operation") or "add"),
                section=item["section"],
                content=item["content"],
                evidence_ids=list(refs),
            ))
        self.repository.create_memory_proposal(MemoryProposal(proposal_id="proposal-" + uuid4().hex[:18], organisation_id=organisation_id, brand_id=brand_id, project_id=project_id, cycle_id=cycle_id, changes=changes))

    def _write_document(self, organisation_id: str, brand_id: str, project_id: str, cycle_id: str, output: dict[str, Any], replace: bool = False) -> None:
        directory = self.repository.root / "organisations" / organisation_id / "brands" / brand_id / "projects" / project_id / "cycles" / cycle_id
        path = directory / "strategy-document.md"
        if path.exists() and not replace:
            return
        body = output.get("full_document") or output.get("markdown") or ""
        path.write_text("# Strategy document\n\n" + body, encoding="utf-8")
        (directory / "strategy-document.json").write_text(json.dumps(output, indent=2), encoding="utf-8")

    def _write_event(self, organisation_id: str, brand_id: str, project_id: str, cycle_id: str, kind: str, detail: dict[str, Any]) -> None:
        path = self.repository.root / "organisations" / organisation_id / "brands" / brand_id / "projects" / project_id / "cycles" / cycle_id / "learning-ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": kind, "detail": detail}) + "\n")


def _coverage(count: int, strong_at: int, adequate_at: int) -> str:
    if count >= strong_at:
        return "strong"
    if count >= adequate_at:
        return "adequate"
    if count > 0:
        return "thin"
    return "missing"


def build_reconciliation(context: dict[str, Any]) -> dict[str, Any]:
    research = context.get("research") if isinstance(context.get("research"), dict) else {}
    competitive = context.get("competitive") if isinstance(context.get("competitive"), dict) else {}
    audience = context.get("audience") if isinstance(context.get("audience"), dict) else {}
    market = research.get("market_position") if isinstance(research.get("market_position"), dict) else {}
    portrait = audience.get("audience_portrait") if isinstance(audience.get("audience_portrait"), dict) else {}
    data_points = market.get("data_points") if isinstance(market.get("data_points"), list) else []
    tensions = research.get("cultural_tensions") if isinstance(research.get("cultural_tensions"), list) else []
    competitors = competitive.get("competitors") if isinstance(competitive.get("competitors"), list) else []
    behaviors = portrait.get("behaviors") if isinstance(portrait.get("behaviors"), list) else []
    audience_tensions = audience.get("audience_tensions") if isinstance(audience.get("audience_tensions"), list) else []
    coverage_map = {
        "market_position": _coverage(len(data_points) + (1 if market.get("summary") else 0), 3, 1),
        "competitive_landscape": _coverage(len(competitors), 3, 1),
        "cultural_tensions": _coverage(len(tensions), 2, 1),
        "audience_behavior": _coverage(len(behaviors), 3, 1),
        "audience_tensions": _coverage(len(audience_tensions), 2, 1),
    }
    gaps = []
    for key, value in coverage_map.items():
        if value in {"thin", "missing"}:
            gaps.append({
                "impact": "material" if value == "thin" else "critical",
                "recommended_assumption": "Treat %s as incomplete until more evidence is collected." % key.replace("_", " "),
            })
    return {
        "stage": "reconciliation",
        "cycle_id": context.get("cycle_id"),
        "brand_slug": context.get("brand_slug"),
        "coverage_map": coverage_map,
        "gap_alerts": gaps[:4],
        "contradictions": [],
        "computed": True,
    }
