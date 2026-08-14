"""Model adapter boundary. Routes and storage never call Anthropic directly."""

from __future__ import annotations

import json
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ProviderError(RuntimeError):
    pass


class StrategyProvider(Protocol):
    def generate(self, stage_id: str, brief: str, context: dict[str, Any], revision: int) -> dict[str, Any]: ...
    def collect_sources(self, brief: str, context: dict[str, Any]) -> list[dict[str, Any]]: ...


class AnthropicProvider:
    """Minimal Messages API adapter using structured JSON text returned by the model."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-5") -> None:
        self.api_key = api_key
        self.model = model

    def generate(self, stage_id: str, brief: str, context: dict[str, Any], revision: int) -> dict[str, Any]:
        prompt = {
            "role": "BURN Strategy OS evidence-led strategist",
            "stage": stage_id,
            "revision": revision,
            "brief": brief,
            "context": context,
            "rules": [
                "Return only valid JSON, with no markdown fences.",
                "Use only factual claims that cite evidence and a source in the supplied context.",
                "Label deductions as inference and gaps as assumptions.",
                "Never mention AI, agents, systems, or automation in client-facing strategy copy.",
            ],
        }
        return self._request(json.dumps(prompt), 7000, use_web_search=False)

    def collect_sources(self, brief: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        prompt = {
            "role": "BURN Strategy OS source researcher",
            "brief": brief,
            "instructions": [
                "Use web search to locate 6–12 credible, relevant sources published recently where possible.",
                "Return only valid JSON in this exact shape: {\"sources\":[...]}",
                "Every source must contain id, label, title, publisher, url, publication_date (YYYY-MM-DD), accessed_date (YYYY-MM-DD), type, and a short attributable excerpt.",
                "Never invent a URL, date, publisher, quote, or source. Omit anything you cannot verify.",
            ],
        }
        result = self._request(json.dumps(prompt), 6000, use_web_search=True)
        sources = result.get("sources")
        if not isinstance(sources, list) or len(sources) < 3:
            raise ProviderError("Source research returned too little verified material; revise the brief or try again.")
        return sources

    def _request(self, prompt: str, max_tokens: int, use_web_search: bool) -> dict[str, Any]:
        request_payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "messages": [{"role": "user", "content": prompt}],
        }
        if use_web_search:
            request_payload["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}]
        body = json.dumps(request_payload).encode()
        request = Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=90) as response:
                payload = json.loads(response.read().decode())
        except (HTTPError, URLError, TimeoutError) as exc:
            raise ProviderError("The model provider could not complete this stage.") from exc
        if payload.get("stop_reason") == "pause_turn" and use_web_search:
            request_payload["messages"].append({"role": "assistant", "content": payload.get("content", [])})
            body = json.dumps(request_payload).encode()
            try:
                with urlopen(Request("https://api.anthropic.com/v1/messages", data=body, headers={"content-type": "application/json", "x-api-key": self.api_key, "anthropic-version": "2023-06-01"}, method="POST"), timeout=90) as response:
                    payload = json.loads(response.read().decode())
            except (HTTPError, URLError, TimeoutError) as exc:
                raise ProviderError("The model provider could not complete source research.") from exc
        text = "".join(block.get("text", "") for block in payload.get("content", []) if block.get("type") == "text")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError("The model returned an invalid structured result.") from exc


class FixtureProvider:
    """Deterministic source-backed output used only for automated verification."""

    def generate(self, stage_id: str, brief: str, context: dict[str, Any], revision: int) -> dict[str, Any]:
        return fixture_stage(stage_id, context["cycle_id"], context["brand_slug"], revision)

    def collect_sources(self, brief: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        return [{**_source(), "title": "Fixture market report", "publisher": "BURN", "url": "https://burnstudio.co", "captured_at": "2026-08-01T00:00:00Z", "excerpt": brief[:300]}]


def _source() -> dict[str, Any]:
    return {"id": "source-fixture", "label": "Fixture market report", "type": "report", "publication_date": "2026-08-01", "accessed_date": "2026-08-01"}


def _evidence(identifier: str = "evidence-fixture") -> dict[str, Any]:
    return {"id": identifier, "claim": "A verified fixture fact supports this observation.", "source_ref": "source-fixture", "date_of_evidence": "2026-08-01", "confidence": "verified", "freshness": "current"}


def _words(prefix: str, count: int) -> str:
    return " ".join([prefix] * count)


def fixture_stage(stage_id: str, cycle_id: str, brand_slug: str, revision: int) -> dict[str, Any]:
    source, evidence = _source(), _evidence()
    common = {"stage": stage_id, "cycle_id": cycle_id, "brand_slug": brand_slug, "revision": revision, "sources": [source]}
    if stage_id == "research":
        return {**common, "market_position": {"summary": "A distinctive market position.", "data_points": [evidence]}, "cultural_tensions": [{"name": "Tension one", "evidence": evidence}, {"name": "Tension two", "evidence": evidence}], "signals": [evidence, {**evidence, "id": "evidence-two"}, {**evidence, "id": "evidence-three"}], "body": "Specific evidence-led research."}
    if stage_id == "competitive":
        return {**common, "competitors": [{"name": name, "structural_limitation": "A distinct limitation", "evidence": evidence} for name in ("One", "Two", "Three")], "unowned_stories": [{"name": "Story one", "cultural_tension_ref": "Tension one"}, {"name": "Story two", "cultural_tension_ref": "Tension two"}]}
    if stage_id == "audience":
        return {**common, "audience_portrait": {"summary": "People seek meaningful, shareable participation.", "behaviors": ["behaviour one", "behaviour two", "behaviour three"], "communities": ["community one", "community two"]}, "audience_tensions": [{"name": "tension one", "evidence": evidence}, {"name": "tension two", "evidence": evidence}], "cultural_participation": {"platforms": ["platform one", "platform two"]}}
    if stage_id == "reconciliation":
        return {"stage": stage_id, "cycle_id": cycle_id, "brand_slug": brand_slug, "coverage_map": {"market_position": "adequate", "competitive_landscape": "adequate", "cultural_tensions": "adequate", "audience_behavior": "adequate", "audience_tensions": "adequate"}, "gap_alerts": [], "contradictions": []}
    if stage_id == "strategy":
        return {**common, "big_idea": {"name": "Make Each Moment Matter"}, "strategic_argument": _words("Because evidence connects culture, audience and distinction.", 75), "evidence_refs": ["evidence-fixture", "evidence-two", "evidence-three"], "assumptions": [], "burn_advantage": "Exceptional creative work joined to practical strategic clarity.", "body": "A client-facing strategy grounded in evidence."}
    if stage_id == "opportunities":
        return {**common, "cultural_moments": [{"name": "Moment one", "date_start": "2026-09-01", "big_idea_connection": "It makes the idea tangible", "evidence": evidence}, {"name": "Moment two", "date_start": "2026-10-01", "big_idea_connection": "It extends the idea", "evidence": evidence}], "partnership_candidates": ["Partner one", "Partner two", "Partner three"], "platform_windows": [{"name": "Window", "expiry_estimate": "2026-Q4", "big_idea_connection": "It carries the idea", "evidence": evidence}]}
    if stage_id == "document_assembly":
        executive = _words("A focused executive narrative makes the evidence useful for decisive action.", 60)
        full = _words("This complete strategy document translates the evidence into a clear creative and business direction.", 350)
        return {**common, "executive_narrative": executive, "executive_narrative_word_count": len(executive.split()), "full_document": full, "full_document_word_count": len(full.split()), "source_appendix": [source], "claim_traceability": [{"claim": "Fixture claim", "source_ref": "source-fixture"}]}
    if stage_id == "memory_proposal":
        return {"stage": stage_id, "cycle_id": cycle_id, "brand_slug": brand_slug, "proposal_status": "proposed", "changes": [{"change_id": "change-position", "operation": "add", "section": "strategic_pov", "content": "A concise, source-backed strategic point of view.", "evidence_ids": ["evidence-fixture"]}], "explore_next": ["Explore one", "Explore two"], "confidence_flags": {"market_position": "high", "competitive_landscape": "medium", "audience_understanding": "medium", "strategic_pov": "low"}, "total_word_count": 30, "diffs": []}
    raise ProviderError("Unknown workflow stage.")
