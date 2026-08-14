import json
from pathlib import Path
import tempfile
import unittest

from strategy_os import (
    ConflictError,
    CycleManifest,
    CycleStatus,
    FeedbackEvent,
    FeedbackVerdict,
    ImmutableRecordError,
    MemoryChange,
    MemoryProposal,
    Project,
    QualityFinding,
    QualityResult,
    StageAttempt,
    ValidationError,
    VaultRepository,
)
from strategy_os.models import MemoryOperation


DIGEST = "b" * 64


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = VaultRepository(self.root)
        self.project = Project(
            project_id="project_one",
            organisation_id="org_burn",
            brand_id="brand_test",
            name="Test Pitch",
            created_by="actor_brad",
        )
        self.repository.create_project(self.project, "# Test brief\n")
        self.cycle = CycleManifest(
            cycle_id="cycle_one",
            organisation_id="org_burn",
            brand_id="brand_test",
            project_id="project_one",
            brief_sha256=DIGEST,
            prompt_versions={"research": "2.0"},
        )
        self.repository.create_cycle(self.cycle)

    def tearDown(self):
        self.temporary.cleanup()

    def test_project_and_cycle_rebuild_without_database(self):
        rebuilt = self.repository.rebuild_index()
        self.assertEqual(rebuilt, {"projects": ["project_one"], "cycles": ["cycle_one"]})

    def test_cross_tenant_lookup_does_not_find_record(self):
        with self.assertRaises(Exception) as caught:
            self.repository.get_project("org_other", "brand_test", "project_one")
        self.assertNotIn("org_burn", str(caught.exception))

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.repository.get_project("../private", "brand_test", "project_one")

    def test_stage_attempt_is_immutable(self):
        attempt = StageAttempt(
            attempt_id="attempt_one",
            cycle_id="cycle_one",
            stage_id="research",
            attempt_number=1,
            input_hash=DIGEST,
            output={"body": "Evidence"},
        )
        self.repository.write_stage_attempt("org_burn", "brand_test", "project_one", attempt)
        with self.assertRaises(ImmutableRecordError):
            self.repository.write_stage_attempt("org_burn", "brand_test", "project_one", attempt)

    def test_cycle_transition_uses_optimistic_revision(self):
        running = self.repository.update_cycle_status(
            "org_burn", "brand_test", "project_one", "cycle_one", CycleStatus.RUNNING, 1
        )
        self.assertEqual(running.revision, 2)
        with self.assertRaises(ConflictError):
            self.repository.update_cycle_status(
                "org_burn", "brand_test", "project_one", "cycle_one", CycleStatus.RECONCILING, 1
            )

    def test_only_one_active_cycle_per_project(self):
        second = CycleManifest(
            cycle_id="cycle_two",
            organisation_id="org_burn",
            brand_id="brand_test",
            project_id="project_one",
            brief_sha256=DIGEST,
        )
        with self.assertRaises(ConflictError):
            self.repository.create_cycle(second)

    def test_completed_cycle_releases_project_for_next_cycle(self):
        cycle = self.repository.update_cycle_status(
            "org_burn", "brand_test", "project_one", "cycle_one", CycleStatus.RUNNING, 1
        )
        cycle = self.repository.update_cycle_status(
            "org_burn", "brand_test", "project_one", "cycle_one", CycleStatus.RECONCILING, cycle.revision
        )
        cycle = self.repository.update_cycle_status(
            "org_burn", "brand_test", "project_one", "cycle_one", CycleStatus.REVIEW, cycle.revision
        )
        self.repository.update_cycle_status(
            "org_burn", "brand_test", "project_one", "cycle_one", CycleStatus.COMPLETED, cycle.revision
        )
        second = CycleManifest(
            cycle_id="cycle_two",
            organisation_id="org_burn",
            brand_id="brand_test",
            project_id="project_one",
            brief_sha256=DIGEST,
        )
        self.repository.create_cycle(second)
        self.assertEqual(self.repository.get_project("org_burn", "brand_test", "project_one").current_cycle_id, "cycle_two")

    def test_quality_ledger_retains_attributable_finding(self):
        finding = QualityFinding(
            finding_id="finding_one",
            cycle_id="cycle_one",
            stage_id="research",
            attempt_id="attempt_one",
            check="evidence",
            result=QualityResult.FAIL,
            summary="Unsupported claim",
            required_action="Add a source or remove the claim",
        )
        self.repository.add_quality_finding("org_burn", "brand_test", "project_one", finding)
        ledger = next(self.root.glob("**/quality-ledger.jsonl"))
        line = json.loads(ledger.read_text().strip())
        self.assertEqual(line["finding_id"], "finding_one")
        self.assertEqual(line["required_action"], "Add a source or remove the claim")

    def test_feedback_is_immutable(self):
        feedback = FeedbackEvent(
            feedback_id="feedback_one",
            organisation_id="org_burn",
            brand_id="brand_test",
            project_id="project_one",
            cycle_id="cycle_one",
            target_type="record",
            target_id="intel_one",
            verdict=FeedbackVerdict.CORRECT,
            reason="The market figure is wrong.",
            replacement="Use the audited result.",
            created_by="actor_brad",
        )
        self.repository.add_feedback(feedback)
        with self.assertRaises(ImmutableRecordError):
            self.repository.add_feedback(feedback)

    def test_memory_proposal_requires_human_decision_to_change_current(self):
        proposal = MemoryProposal(
            proposal_id="proposal_one",
            organisation_id="org_burn",
            brand_id="brand_test",
            project_id="project_one",
            cycle_id="cycle_one",
            changes=[
                MemoryChange(
                    "change_one",
                    MemoryOperation.ADD,
                    "market_position",
                    "The brand is repositioning.",
                    ["source_one"],
                )
            ],
        )
        self.repository.create_memory_proposal(proposal)
        self.assertEqual(list(self.root.glob("**/CURRENT.md")), [])
        decision = self.repository.decide_memory_proposal(
            "org_burn",
            "brand_test",
            "proposal_one",
            "actor_brad",
            ["change_one"],
            "decision_one",
            "memory_one",
        )
        self.assertEqual(decision["status"], "approved")
        current = next(self.root.glob("**/CURRENT.md")).read_text()
        self.assertIn("The brand is repositioning.", current)

    def test_rejected_memory_proposal_does_not_create_current(self):
        proposal = MemoryProposal(
            proposal_id="proposal_two",
            organisation_id="org_burn",
            brand_id="brand_test",
            project_id="project_one",
            cycle_id="cycle_one",
            changes=[MemoryChange("change_two", MemoryOperation.ADD, "strategy", "Reject this", ["source_two"])],
        )
        self.repository.create_memory_proposal(proposal)
        decision = self.repository.decide_memory_proposal(
            "org_burn", "brand_test", "proposal_two", "actor_brad", [], "decision_two", None
        )
        self.assertEqual(decision["status"], "rejected")
        self.assertEqual(list(self.root.glob("**/CURRENT.md")), [])

    def test_later_memory_version_preserves_approved_sections(self):
        first = MemoryProposal(
            proposal_id="proposal_three",
            organisation_id="org_burn",
            brand_id="brand_test",
            project_id="project_one",
            cycle_id="cycle_one",
            changes=[MemoryChange("change_three", MemoryOperation.ADD, "market_position", "First position", ["source_one"])],
        )
        self.repository.create_memory_proposal(first)
        self.repository.decide_memory_proposal(
            "org_burn", "brand_test", "proposal_three", "actor_brad", ["change_three"], "decision_three", "memory_one"
        )
        second = MemoryProposal(
            proposal_id="proposal_four",
            organisation_id="org_burn",
            brand_id="brand_test",
            project_id="project_one",
            cycle_id="cycle_one",
            base_memory_version="memory_one",
            changes=[MemoryChange("change_four", MemoryOperation.ADD, "strategy", "Second strategy", ["source_two"])],
        )
        self.repository.create_memory_proposal(second)
        self.repository.decide_memory_proposal(
            "org_burn", "brand_test", "proposal_four", "actor_brad", ["change_four"], "decision_four", "memory_two"
        )
        current = next(self.root.glob("**/CURRENT.md")).read_text()
        self.assertIn("First position", current)
        self.assertIn("Second strategy", current)

    def test_stale_memory_proposal_cannot_overwrite_current(self):
        first = MemoryProposal(
            proposal_id="proposal_five",
            organisation_id="org_burn",
            brand_id="brand_test",
            project_id="project_one",
            cycle_id="cycle_one",
            changes=[MemoryChange("change_five", MemoryOperation.ADD, "strategy", "Current strategy", ["source_one"])],
        )
        self.repository.create_memory_proposal(first)
        self.repository.decide_memory_proposal(
            "org_burn", "brand_test", "proposal_five", "actor_brad", ["change_five"], "decision_five", "memory_one"
        )
        stale = MemoryProposal(
            proposal_id="proposal_six",
            organisation_id="org_burn",
            brand_id="brand_test",
            project_id="project_one",
            cycle_id="cycle_one",
            base_memory_version=None,
            changes=[MemoryChange("change_six", MemoryOperation.AMEND, "strategy", "Stale strategy", ["source_two"])],
        )
        self.repository.create_memory_proposal(stale)
        with self.assertRaises(ConflictError):
            self.repository.decide_memory_proposal(
                "org_burn", "brand_test", "proposal_six", "actor_brad", ["change_six"], "decision_six", "memory_two"
            )


if __name__ == "__main__":
    unittest.main()
