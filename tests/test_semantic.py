import unittest

from strategy_os import ValidationError, validate_stage_output


def evidence(identifier, source="source_one"):
    return {
        "id": identifier,
        "claim": "A specific sourced observation",
        "source_ref": source,
        "date_of_evidence": "2026-08-01",
        "confidence": "verified",
        "freshness": "current",
    }


SOURCE = {
    "id": "source_one",
    "label": "Primary report",
    "url": "https://example.test/report",
    "publication_date": "2026-08-01",
    "accessed_date": "2026-08-13",
    "type": "report",
}


class SemanticValidationTests(unittest.TestCase):
    def test_unknown_stage_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_stage_output({"stage": "mystery"})

    def test_valid_research_contract_has_no_objective_issues(self):
        payload = {
            "stage": "research",
            "cycle_id": "cycle_one",
            "brand_slug": "test-brand",
            "revision": 1,
            "market_position": {"summary": "Specific position", "data_points": [evidence("evidence_one")]},
            "cultural_tensions": [
                {"name": "Tension one", "description": "Detail", "evidence": [evidence("evidence_two")], "strategic_angle": "Angle"},
                {"name": "Tension two", "description": "Detail", "evidence": [evidence("evidence_three")], "strategic_angle": "Angle"},
            ],
            "signals": [evidence("evidence_four"), evidence("evidence_five"), evidence("evidence_six")],
            "body": "Specific narrative without generic phrases.",
            "sources": [SOURCE],
            "agent_confidence": "medium",
        }
        self.assertEqual(validate_stage_output(payload), [])

    def test_research_detects_missing_tensions_and_sources(self):
        payload = {
            "stage": "research",
            "cycle_id": "cycle_one",
            "brand_slug": "test-brand",
            "revision": 1,
            "market_position": {"summary": "Position", "data_points": [evidence("evidence_one", "missing_source")]},
            "cultural_tensions": [],
            "signals": [],
            "body": "Gen Z values authenticity.",
            "sources": [SOURCE],
        }
        check_ids = {issue.check_id for issue in validate_stage_output(payload)}
        self.assertTrue({"R_CITE", "R_TENSION_COUNT", "R_NO_GENERIC", "CONTRACT_SIGNALS"}.issubset(check_ids))

    def test_strategy_acknowledges_thin_coverage(self):
        payload = {
            "stage": "strategy",
            "cycle_id": "cycle_one",
            "brand_slug": "test-brand",
            "revision": 1,
            "tension": "A real tension.",
            "big_idea": {"name": "Test Unhurried", "description": "A world."},
            "strategic_argument": "because " + "word " * 399,
            "burn_advantage": "Senior creative leadership.",
            "assumptions": [],
            "evidence_refs": ["evidence_one", "evidence_two", "evidence_three"],
            "body": "A strategic position.",
            "agent_confidence": "medium",
        }
        issues = validate_stage_output(payload, {"coverage_map": {"audience_behavior": "thin"}})
        self.assertIn("S_GAP_ACKNOWLEDGED", {issue.check_id for issue in issues})

    def test_markdown_strategy_skips_json_word_quotas(self):
        payload = {
            "stage": "strategy",
            "cycle_id": "cycle_one",
            "brand_slug": "test-brand",
            "revision": 1,
            "markdown": "## Big idea\nMake Each Moment Matter\n## Strategic argument\nA short argument.",
        }
        self.assertEqual(validate_stage_output(payload), [])

    def test_memory_never_arrives_preapproved(self):
        payload = {
            "stage": "memory_proposal",
            "cycle_id": "cycle_one",
            "brand_slug": "test-brand",
            "proposal_status": "approved",
            "market_position": "Position",
            "competitive_landscape": "Landscape",
            "audience_understanding": "Audience",
            "strategic_pov": "POV",
            "explore_next": ["Question one", "Question two"],
            "confidence_flags": {
                "market_position": "high",
                "competitive_landscape": "medium",
                "audience_understanding": "low",
                "strategic_pov": "medium",
            },
            "total_word_count": 10,
        }
        issues = validate_stage_output(payload)
        self.assertIn("M_PROPOSAL_LABEL", {issue.check_id for issue in issues})


if __name__ == "__main__":
    unittest.main()
