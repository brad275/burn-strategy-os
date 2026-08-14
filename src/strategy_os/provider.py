"""Model adapter boundary. Routes and storage never call Anthropic directly."""

from __future__ import annotations

import json
import re
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
        original_messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        request_payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "messages": original_messages,
        }
        if use_web_search:
            request_payload["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}]
        payload = self._post(request_payload, source_research=use_web_search)
        for _ in range(3):
            if payload.get("stop_reason") != "pause_turn":
                break
            if not use_web_search:
                raise ProviderError("The model paused unexpectedly before completing this stage.")
            content = payload.get("content")
            if not isinstance(content, list) or not content:
                raise ProviderError("Source research paused without resumable search results.")
            # Anthropic server tools require the paused assistant response verbatim,
            # together with the complete original user turn and unchanged tools.
            request_payload["messages"] = original_messages + [{"role": "assistant", "content": content}]
            payload = self._post(request_payload, source_research=True)
        if payload.get("stop_reason") == "pause_turn":
            raise ProviderError("Source research did not finish after several search continuations; try again.")
        self._validate_response(payload)
        text = self._extract_text(payload)
        if not text.strip():
            raise ProviderError("The model completed without returning structured text; try the cycle again.")
        try:
            result = json.loads(self._unwrap_json_fence(text))
        except json.JSONDecodeError as exc:
            raise ProviderError("The model returned text that was not valid structured JSON; try the cycle again.") from exc
        if not isinstance(result, dict):
            raise ProviderError("The model returned an unexpected structured result instead of a JSON object.")
        return result

    def _post(self, request_payload: dict[str, Any], source_research: bool) -> dict[str, Any]:
        request = Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(request_payload).encode(),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=90) as response:
                raw = response.read().decode()
        except HTTPError as exc:
            raise ProviderError(self._http_failure(exc.code)) from exc
        except (URLError, TimeoutError) as exc:
            activity = "source research" if source_research else "this strategy stage"
            raise ProviderError("The model provider could not complete %s because the connection failed; try again." % activity) from exc
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderError("The model provider returned an unreadable response; try again.") from exc
        if not isinstance(payload, dict):
            raise ProviderError("The model provider returned an unexpected response shape; try again.")
        return payload

    @staticmethod
    def _http_failure(status: int) -> str:
        if status in {401, 403}:
            return "The model provider rejected authentication; check the configured Railway API key."
        if status == 429:
            return "The model provider rate limit was reached; wait briefly and try again."
        if status >= 500:
            return "The model provider is temporarily unavailable; try again shortly."
        return "The model provider rejected the request (HTTP %d); review the stage configuration." % status

    @staticmethod
    def _validate_response(payload: dict[str, Any]) -> None:
        if payload.get("type") == "error" or isinstance(payload.get("error"), dict):
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            error_type = error.get("type") if isinstance(error.get("type"), str) else "provider_error"
            raise ProviderError("The model provider returned an API error (%s); try again or review the provider configuration." % error_type)
        stop_reason = payload.get("stop_reason")
        if stop_reason == "refusal":
            raise ProviderError("The model declined this request; review the brief wording before trying again.")
        if stop_reason in {"max_tokens", "model_context_window_exceeded"}:
            raise ProviderError("The model response was cut off before the structured result was complete; reduce the brief or raise the output limit.")
        if stop_reason not in {"end_turn", "stop_sequence"}:
            label = stop_reason if isinstance(stop_reason, str) else "missing"
            raise ProviderError("The model returned an unexpected completion state (%s); try again." % label)
        if not isinstance(payload.get("content"), list):
            raise ProviderError("The model response did not contain a valid content list; try again.")

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str:
        parts: list[str] = []
        for block in payload["content"]:
            if not isinstance(block, dict):
                raise ProviderError("The model response contained an unexpected content block; try again.")
            if block.get("type") == "text":
                value = block.get("text")
                if not isinstance(value, str):
                    raise ProviderError("The model response contained an invalid text block; try again.")
                parts.append(value)
        return "".join(parts)

    @staticmethod
    def _unwrap_json_fence(text: str) -> str:
        stripped = text.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", stripped, flags=re.IGNORECASE)
        return fenced.group(1).strip() if fenced else stripped


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
