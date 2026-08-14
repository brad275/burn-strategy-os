import unittest

from strategy_os import (
    CycleManifest,
    FeedbackEvent,
    FeedbackVerdict,
    MemoryChange,
    MemoryProposal,
    QualityFinding,
    QualityResult,
    StageAttempt,
    ValidationError,
)
from strategy_os.models import MemoryOperation


DIGEST = "a" * 64


class ModelValidationTests(unittest.TestCase):
    def test_cycle_requires_real_digest(self):
        cycle = CycleManifest(
            cycle_id="cycle_one",
            organisation_id="org_burn",
            brand_id="brand_test",
            project_id="project_one",
            brief_sha256="not-a-digest",
        )
        with self.assertRaises(ValidationError):
            cycle.validate()

    def test_non_approval_feedback_requires_reason(self):
        feedback = FeedbackEvent(
            feedback_id="feedback_one",
            organisation_id="org_burn",
            brand_id="brand_test",
            project_id="project_one",
            cycle_id="cycle_one",
            target_type="record",
            target_id="intel_one",
            verdict=FeedbackVerdict.REJECT,
            created_by="actor_brad",
        )
        with self.assertRaises(ValidationError):
            feedback.validate()

    def test_failed_quality_finding_requires_action(self):
        finding = QualityFinding(
            finding_id="finding_one",
            cycle_id="cycle_one",
            stage_id="research",
            attempt_id="attempt_one",
            check="evidence",
            result=QualityResult.FAIL,
            summary="The claim has no source.",
        )
        with self.assertRaises(ValidationError):
            finding.validate()

    def test_attempt_limit_is_initial_plus_two_revisions(self):
        valid = StageAttempt(
            attempt_id="attempt_three",
            cycle_id="cycle_one",
            stage_id="research",
            attempt_number=3,
            input_hash=DIGEST,
        )
        valid.validate()
        invalid = StageAttempt(
            attempt_id="attempt_four",
            cycle_id="cycle_one",
            stage_id="research",
            attempt_number=4,
            input_hash=DIGEST,
        )
        with self.assertRaises(ValidationError):
            invalid.validate()

    def test_memory_change_requires_evidence(self):
        proposal = MemoryProposal(
            proposal_id="proposal_one",
            organisation_id="org_burn",
            brand_id="brand_test",
            project_id="project_one",
            cycle_id="cycle_one",
            changes=[MemoryChange("change_one", MemoryOperation.ADD, "market_position", "New position", [])],
        )
        with self.assertRaises(ValidationError):
            proposal.validate()


if __name__ == "__main__":
    unittest.main()
