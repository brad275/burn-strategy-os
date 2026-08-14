"""Deterministic validation for semantic contract v1.x stage outputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .errors import ValidationError


CONFIDENCE = {"high", "medium", "low"}
EVIDENCE_CONFIDENCE = {"verified", "likely", "unverified"}
FRESHNESS = {"current", "recent", "historical"}
COVERAGE = {"strong", "adequate", "thin", "missing"}
STAGES = {
    "research", "competitive", "audience", "reconciliation",
    "strategy", "opportunities", "document_assembly", "memory_proposal",
}
AI_PATTERN = re.compile(r"\b(ai|agents?|machine learning|automation|llms?)\b", re.IGNORECASE)
GENERIC_PATTERN = re.compile(r"\b(gen z values|millennials want|consumers are increasingly)\b", re.IGNORECASE)
DEMO_PATTERN = re.compile(r"^\s*(gen z|millennials?|ages?\s+\d|\d{2}\s*[-–]\s*\d{2})\b", re.IGNORECASE)


@dataclass(frozen=True)
class ValidationIssue:
    check_id: str
    severity: str
    message: str
    can_auto_revise: bool


def validate_stage_output(payload: Mapping[str, Any], context: Optional[Mapping[str, Any]] = None) -> List[ValidationIssue]:
    """Return objective contract issues; judgment rubrics remain human/model evaluated."""
    if not isinstance(payload, Mapping):
        raise ValidationError("stage output must be an object")
    stage = payload.get("stage")
    if stage not in STAGES:
        raise ValidationError("unknown semantic stage: %r" % stage)
    common = _common(payload, require_revision=stage not in {"reconciliation", "memory_proposal"})
    validator = {
        "research": _research,
        "competitive": _competitive,
        "audience": _audience,
        "reconciliation": _reconciliation,
        "strategy": _strategy,
        "opportunities": _opportunities,
        "document_assembly": _document,
        "memory_proposal": _memory,
    }[stage]
    return common + validator(payload, context or {})


def _common(payload: Mapping[str, Any], require_revision: bool) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    for field in ("cycle_id", "brand_slug"):
        if not _text(payload.get(field)):
            issues.append(_issue("CONTRACT_REQUIRED", "error", "%s is required" % field, False))
    if require_revision:
        revision = payload.get("revision", payload.get("version"))
        if not isinstance(revision, int) or revision < 1 or revision > 3:
            issues.append(_issue("CONTRACT_REVISION", "error", "revision/version must be 1, 2, or 3", False))
    return issues


def _research(payload: Mapping[str, Any], context: Mapping[str, Any]) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    market = payload.get("market_position", {})
    if not _text(market.get("summary")) or len(market.get("data_points", [])) < 1:
        issues.append(_issue("R_CITE", "error", "Market position requires a summary and evidence", True))
    tensions = payload.get("cultural_tensions", [])
    if len(tensions) < 2:
        issues.append(_issue("R_TENSION_COUNT", "error", "Fewer than 2 cultural tensions identified", True))
    for tension in tensions:
        if not tension.get("evidence"):
            issues.append(_issue("R_TENSION_EVIDENCE", "error", "Cultural tension lacks evidence", True))
    if len(payload.get("signals", [])) < 3:
        issues.append(_issue("CONTRACT_SIGNALS", "error", "Fewer than 3 signals supplied", True))
    body = payload.get("body", "")
    if GENERIC_PATTERN.search(body):
        issues.append(_issue("R_NO_GENERIC", "warning", "Generic trend language detected", True))
    issues.extend(_validate_sources_and_evidence(payload, "R_CITE"))
    return issues


def _competitive(payload: Mapping[str, Any], context: Mapping[str, Any]) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    competitors = payload.get("competitors", [])
    if len(competitors) < 3:
        issues.append(_issue("C_COMP_COUNT", "error", "Fewer than 3 competitors mapped", True))
    for competitor in competitors:
        if not _text(competitor.get("structural_limitation")):
            issues.append(_issue("C_LIMITATION", "error", "Competitor missing structural limitation", True))
    stories = payload.get("unowned_stories", [])
    if len(stories) < 2:
        issues.append(_issue("C_UNOWNED_COUNT", "error", "Fewer than 2 unowned narrative territories", True))
    known_tensions = set(context.get("cultural_tension_names", []))
    if known_tensions:
        for story in stories:
            if story.get("cultural_tension_ref") not in known_tensions:
                issues.append(_issue("C_TENSION_LINK", "error", "Unowned story does not link to a research tension", True))
    issues.extend(_validate_sources_and_evidence(payload, "C_SOURCE"))
    return issues


def _audience(payload: Mapping[str, Any], context: Mapping[str, Any]) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    portrait = payload.get("audience_portrait", {})
    if DEMO_PATTERN.search(portrait.get("summary", "")):
        issues.append(_issue("A_NO_DEMO", "error", "Audience portrait leads with demographics", True))
    if len(portrait.get("behaviors", [])) < 3:
        issues.append(_issue("A_BEHAVIOR_COUNT", "error", "Fewer than 3 observable behaviors", True))
    if len(portrait.get("communities", [])) < 2:
        issues.append(_issue("A_COMMUNITY", "error", "Fewer than 2 named communities", True))
    if len(payload.get("audience_tensions", [])) < 2:
        issues.append(_issue("A_TENSION_COUNT", "error", "Fewer than 2 audience tensions", True))
    participation = payload.get("cultural_participation", {})
    if len(participation.get("platforms", [])) < 2:
        issues.append(_issue("A_PLATFORM_COUNT", "error", "Fewer than 2 platform insights", True))
    issues.extend(_validate_sources_and_evidence(payload, "A_SOURCE"))
    return issues


def _reconciliation(payload: Mapping[str, Any], context: Mapping[str, Any]) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    coverage = payload.get("coverage_map", {})
    required = {"market_position", "competitive_landscape", "cultural_tensions", "audience_behavior", "audience_tensions"}
    if set(coverage) != required or any(value not in COVERAGE for value in coverage.values()):
        issues.append(_issue("RC_COVERAGE", "error", "Coverage map incomplete or invalid", False))
    for gap in payload.get("gap_alerts", []):
        if gap.get("impact") == "critical" and not _text(gap.get("recommended_assumption")):
            issues.append(_issue("RC_CRITICAL_GAP", "error", "Critical gap has no recommended assumption", False))
    for contradiction in payload.get("contradictions", []):
        if not contradiction.get("agents"):
            issues.append(_issue("RC_CONTRADICTION_SOURCE", "error", "Contradiction missing agent attribution", False))
    return issues


def _strategy(payload: Mapping[str, Any], context: Mapping[str, Any]) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    idea = payload.get("big_idea", {})
    if not _text(idea.get("name")) or len(idea.get("name", "").split()) > 12:
        issues.append(_issue("S_SINGLE_IDEA", "error", "Big idea must be one concept of 12 words or fewer", True))
    argument = payload.get("strategic_argument", "")
    if not re.search(r"\b(because|the reason|this works because)\b", argument, re.IGNORECASE):
        issues.append(_issue("S_CAUSAL", "error", "Strategic argument lacks causal reasoning", True))
    if len(payload.get("evidence_refs", [])) < 3:
        issues.append(_issue("S_EVIDENCE_REFS", "error", "Strategy not grounded in enough evidence", True))
    if AI_PATTERN.search(payload.get("body", "") + " " + payload.get("burn_advantage", "")):
        issues.append(_issue("S_NO_AI", "error", "AI or technology language detected", True))
    coverage = context.get("coverage_map", {})
    if any(value in {"thin", "missing"} for value in coverage.values()) and not payload.get("assumptions"):
        issues.append(_issue("S_GAP_ACKNOWLEDGED", "error", "Strategy does not acknowledge evidence gaps", True))
    count = _word_count(argument)
    if count < 400 or count > 600:
        issues.append(_issue("S_ARGUMENT_LENGTH", "warning", "Strategic argument is %s words" % count, True))
    return issues


def _opportunities(payload: Mapping[str, Any], context: Mapping[str, Any]) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    moments = payload.get("cultural_moments", [])
    if len(moments) < 2:
        issues.append(_issue("O_MOMENT_COUNT", "error", "Fewer than 2 cultural moments", True))
    for moment in moments:
        if not _date(moment.get("date_start")):
            issues.append(_issue("O_MOMENT_DATE", "error", "Cultural moment lacks a valid date", True))
        if not _text(moment.get("big_idea_connection")):
            issues.append(_issue("O_BIG_IDEA_LINK", "error", "Moment not connected to strategic position", True))
    partners = payload.get("partnership_candidates", [])
    if len(partners) < 3:
        issues.append(_issue("O_PARTNER_COUNT", "error", "Fewer than 3 partnership candidates", True))
    windows = payload.get("platform_windows", [])
    if len(windows) < 1:
        issues.append(_issue("CONTRACT_WINDOWS", "error", "No platform window supplied", True))
    for window in windows:
        if not _text(window.get("expiry_estimate")):
            issues.append(_issue("O_WINDOW_EXPIRY", "error", "Platform window missing expiry estimate", True))
        if not _text(window.get("big_idea_connection")):
            issues.append(_issue("O_BIG_IDEA_LINK", "error", "Window not connected to strategic position", True))
    issues.extend(_validate_sources_and_evidence(payload, "O_SOURCE"))
    return issues


def _document(payload: Mapping[str, Any], context: Mapping[str, Any]) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    executive = payload.get("executive_narrative", "")
    full = payload.get("full_document", "")
    executive_count = _word_count(executive)
    full_count = _word_count(full)
    if payload.get("executive_narrative_word_count") != executive_count or not 600 <= executive_count <= 800:
        issues.append(_issue("D_EXEC_LENGTH", "error", "Executive narrative word count is invalid", True))
    if payload.get("full_document_word_count") != full_count or not 3000 <= full_count <= 5000:
        issues.append(_issue("D_FULL_LENGTH", "warning", "Full document word count is invalid", True))
    if re.search(r"\b(see section|as noted above)\b", executive, re.IGNORECASE):
        issues.append(_issue("D_STANDALONE", "error", "Executive narrative is not standalone", True))
    if AI_PATTERN.search(executive + " " + full):
        issues.append(_issue("D_NO_AI", "error", "AI or technology language detected in final document", True))
    source_ids = {source.get("id") for source in payload.get("source_appendix", [])}
    for trace in payload.get("claim_traceability", []):
        if trace.get("source_ref") not in source_ids:
            issues.append(_issue("D_SOURCE_TRACE", "error", "Claim references a missing source", False))
    return issues


def _memory(payload: Mapping[str, Any], context: Mapping[str, Any]) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    if payload.get("proposal_status") != "proposed":
        issues.append(_issue("M_PROPOSAL_LABEL", "error", "Memory proposal must start proposed", False))
    if len(payload.get("explore_next", [])) < 2:
        issues.append(_issue("M_EXPLORE", "error", "Fewer than 2 explore_next directions", True))
    flags = payload.get("confidence_flags", {})
    required = {"market_position", "competitive_landscape", "audience_understanding", "strategic_pov"}
    if set(flags) != required or any(value not in CONFIDENCE for value in flags.values()):
        issues.append(_issue("CONTRACT_CONFIDENCE", "error", "Confidence flags are incomplete or invalid", True))
    elif all(value == "high" for value in flags.values()):
        issues.append(_issue("M_UNCERTAINTY", "error", "All confidence flags are high", True))
    full_count = context.get("full_document_word_count")
    if isinstance(full_count, int) and payload.get("total_word_count", 0) >= full_count:
        issues.append(_issue("M_SHORTER", "error", "Memory proposal is not shorter than the document", True))
    if context.get("has_approved_memory") and not payload.get("diffs"):
        issues.append(_issue("M_DIFF_PRESENT", "warning", "No diffs from current memory", True))
    return issues


def _validate_sources_and_evidence(payload: Mapping[str, Any], check_id: str) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    sources = payload.get("sources", [])
    source_ids = {source.get("id") for source in sources if _valid_source(source)}
    for evidence in _walk_evidence(payload):
        if evidence.get("source_ref") not in source_ids:
            issues.append(_issue(check_id, "error", "Evidence references a missing or invalid source", True))
        if evidence.get("confidence") not in EVIDENCE_CONFIDENCE or evidence.get("freshness") not in FRESHNESS:
            issues.append(_issue(check_id, "error", "Evidence confidence or freshness is invalid", True))
    return issues


def _walk_evidence(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        if {"id", "claim", "source_ref", "date_of_evidence", "confidence", "freshness"}.issubset(value):
            yield value
        for nested in value.values():
            yield from _walk_evidence(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_evidence(nested)


def _valid_source(source: Any) -> bool:
    return (
        isinstance(source, Mapping)
        and _text(source.get("id"))
        and _text(source.get("label"))
        and _date(source.get("publication_date"))
        and _date(source.get("accessed_date"))
        and source.get("type") in {"report", "article", "social", "earnings", "campaign", "interview", "data", "other"}
    )


def _issue(check_id: str, severity: str, message: str, can_auto_revise: bool) -> ValidationIssue:
    return ValidationIssue(check_id, severity, message, can_auto_revise)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _word_count(value: str) -> int:
    return len(re.findall(r"\b[\w’'-]+\b", value or ""))
