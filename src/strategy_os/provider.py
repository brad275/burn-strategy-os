"""Model adapter boundary. Routes and storage never call Anthropic directly."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REQUEST_TIMEOUT_SECONDS = 240
SOURCE_COLLECTION_MAX_TOKENS = 6000
DEFAULT_STAGE_MAX_TOKENS = 7000
STAGE_MAX_TOKENS = {
    "research": 8000,
    "competitive": 8000,
    "audience": 8000,
    "reconciliation": 4000,
    "strategy": 7000,
    "opportunities": 7000,
    "document_assembly": 7000,
    "memory_proposal": 4000,
}
RESEARCH_SOURCE_LIMIT = 12
RESEARCH_EXCERPT_CHARS = 240
COMPACT_TEXT_CHARS = 220
DEFAULT_XAI_MODEL = "grok-4.6"
XAI_API_URL = "https://api.x.ai/v1/chat/completions"


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
        return self._request(json.dumps(build_generate_prompt(stage_id, brief, context, revision)), STAGE_MAX_TOKENS.get(stage_id, DEFAULT_STAGE_MAX_TOKENS), use_web_search=False)

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
        result = self._request(json.dumps(prompt), SOURCE_COLLECTION_MAX_TOKENS, use_web_search=True)
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
        self._validate_content_blocks(payload)
        text = self._extract_text(payload)
        if not text.strip():
            raise ProviderError("The model completed without returning structured text; try the cycle again.")
        try:
            result = self._parse_json_text(text)
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
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
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
        return "\n".join(parts)

    @staticmethod
    def _validate_content_blocks(payload: dict[str, Any]) -> None:
        for block in payload["content"]:
            if not isinstance(block, dict) or block.get("type") != "web_search_tool_result":
                continue
            content = block.get("content")
            if isinstance(content, dict) and content.get("type") == "web_search_tool_result_error":
                code = content.get("error_code") if isinstance(content.get("error_code"), str) else "unknown"
                explanations = {
                    "too_many_requests": "Web search was rate limited; wait briefly and try again.",
                    "max_uses_exceeded": "Web search reached its per-cycle search limit before source collection completed.",
                    "unavailable": "Web search is temporarily unavailable; try again shortly.",
                }
                raise ProviderError(explanations.get(code, "Web search failed (%s); review the brief and try again." % code))

    @staticmethod
    def _parse_json_text(text: str) -> Any:
        stripped = text.strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as exc:
            original_error = exc
        fenced_blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", stripped, flags=re.IGNORECASE)
        for candidate in reversed(fenced_blocks):
            try:
                return json.loads(candidate.strip())
            except json.JSONDecodeError:
                continue
        decoder = json.JSONDecoder()
        best_result: Any = None
        best_length = -1
        for position in range(len(stripped)):
            if stripped[position] != "{":
                continue
            try:
                result, end = decoder.raw_decode(stripped[position:])
            except json.JSONDecodeError:
                continue
            if isinstance(result, dict) and end > best_length:
                best_result = result
                best_length = end
        if best_result is not None:
            return best_result
        raise original_error

    @staticmethod
    def _rules_for_stage(stage_id: str) -> list[str]:
        rules = [
            "Return only valid JSON, with no markdown fences.",
            "Use only factual claims that cite evidence and a source in the supplied context.",
            "Label deductions as inference and gaps as assumptions.",
            "Never mention AI, agents, systems, or automation in client-facing strategy copy.",
        ]
        if stage_id == "research":
            rules.extend([
                "Keep every summary, claim, excerpt, and body concise; do not write long essays.",
                "Cite collected_sources by id. Do not repeat full source records, URLs, publishers, or long excerpts.",
                "Include 2–3 cultural tensions, 3–5 signals, and 1–3 market-position data points.",
                "market_position.summary: at most 60 words. body: at most 180 words. each evidence.claim: at most 35 words.",
                "sources must be compact records only: id, label, type, publication_date, accessed_date — only sources you cite.",
                "Do not copy collected_sources wholesale into the output.",
            ])
        if stage_id == "competitive":
            rules.extend([
                "Keep every limitation, claim, and story name concise; do not write long essays or a body field.",
                "Cite collected_sources by id. Do not repeat full source records or copy the research object wholesale.",
                "Include 3–4 competitors and 2–3 unowned stories. Link each story to a research cultural tension name.",
                "structural_limitation: at most 40 words. each evidence.claim: at most 35 words.",
                "sources must be compact records only: id, label, type, publication_date, accessed_date — only sources you cite.",
            ])
        if stage_id == "audience":
            rules.extend([
                "Keep the portrait, tensions, and platform notes concise; do not write long essays.",
                "Cite collected_sources by id. Do not copy research or competitive records wholesale.",
                "Include 3–5 behaviors, 2–3 communities, 2–3 audience tensions, and 2–3 platforms.",
                "audience_portrait.summary: at most 60 words. each evidence.claim: at most 35 words.",
                "sources must be compact records only: id, label, type, publication_date, accessed_date — only sources you cite.",
            ])
        return rules

    @staticmethod
    def _evidence_schema() -> dict[str, Any]:
        return {
            "id": "evidence-...",
            "claim": "<=35 words",
            "source_ref": "collected source id",
            "date_of_evidence": "YYYY-MM-DD",
            "confidence": "verified|likely|unverified",
            "freshness": "current|recent|historical",
        }

    @staticmethod
    def _source_schema() -> dict[str, Any]:
        return {
            "id": "source-...",
            "label": "short label",
            "type": "report|article|social|earnings|campaign|interview|data|other",
            "publication_date": "YYYY-MM-DD",
            "accessed_date": "YYYY-MM-DD",
        }

    @staticmethod
    def _research_output_schema() -> dict[str, Any]:
        evidence, source = AnthropicProvider._evidence_schema(), AnthropicProvider._source_schema()
        return {
            "stage": "research",
            "cycle_id": "from context",
            "brand_slug": "from context",
            "revision": 1,
            "market_position": {"summary": "<=60 words", "data_points": [evidence]},
            "cultural_tensions": [{"name": "short name", "evidence": evidence}],
            "signals": [evidence],
            "body": "<=180 words",
            "sources": [source],
        }

    @staticmethod
    def _competitive_output_schema() -> dict[str, Any]:
        evidence, source = AnthropicProvider._evidence_schema(), AnthropicProvider._source_schema()
        return {
            "stage": "competitive",
            "cycle_id": "from context",
            "brand_slug": "from context",
            "revision": 1,
            "competitors": [{"name": "short name", "structural_limitation": "<=40 words", "evidence": evidence}],
            "unowned_stories": [{"name": "short name", "cultural_tension_ref": "research tension name"}],
            "sources": [source],
        }

    @staticmethod
    def _audience_output_schema() -> dict[str, Any]:
        evidence, source = AnthropicProvider._evidence_schema(), AnthropicProvider._source_schema()
        return {
            "stage": "audience",
            "cycle_id": "from context",
            "brand_slug": "from context",
            "revision": 1,
            "audience_portrait": {"summary": "<=60 words", "behaviors": ["observable behaviour"], "communities": ["named community"]},
            "audience_tensions": [{"name": "short name", "evidence": evidence}],
            "cultural_participation": {"platforms": ["platform"]},
            "sources": [source],
        }

    @staticmethod
    def _compact_sources(context: dict[str, Any]) -> list[dict[str, Any]]:
        collected = context.get("source_collection") if isinstance(context.get("source_collection"), dict) else {}
        compact: list[dict[str, Any]] = []
        raw_sources = collected.get("sources") if isinstance(collected.get("sources"), list) else []
        for source in raw_sources:
            if not isinstance(source, dict):
                continue
            excerpt = source.get("excerpt")
            if isinstance(excerpt, str) and len(excerpt) > RESEARCH_EXCERPT_CHARS:
                excerpt = excerpt[:RESEARCH_EXCERPT_CHARS].rstrip() + "…"
            compact.append({
                "id": source.get("id"),
                "label": source.get("label"),
                "title": source.get("title"),
                "publisher": source.get("publisher"),
                "type": source.get("type"),
                "publication_date": source.get("publication_date"),
                "accessed_date": source.get("accessed_date"),
                "excerpt": excerpt,
            })
            if len(compact) >= RESEARCH_SOURCE_LIMIT:
                break
        return compact

    @staticmethod
    def _compact_research(research: Any) -> dict[str, Any]:
        if not isinstance(research, dict):
            return {}
        market = research.get("market_position") if isinstance(research.get("market_position"), dict) else {}
        tensions = []
        for item in research.get("cultural_tensions") or []:
            if not isinstance(item, dict):
                continue
            evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
            tensions.append({"name": item.get("name"), "claim": _clip_text(evidence.get("claim"), COMPACT_TEXT_CHARS)})
            if len(tensions) >= 4:
                break
        return {
            "market_position_summary": _clip_text(market.get("summary"), 400),
            "cultural_tensions": tensions,
        }

    @staticmethod
    def _compact_competitive(competitive: Any) -> dict[str, Any]:
        if not isinstance(competitive, dict):
            return {}
        competitors = []
        for item in competitive.get("competitors") or []:
            if not isinstance(item, dict):
                continue
            competitors.append({
                "name": item.get("name"),
                "structural_limitation": _clip_text(item.get("structural_limitation"), COMPACT_TEXT_CHARS),
            })
            if len(competitors) >= 4:
                break
        stories = []
        for item in competitive.get("unowned_stories") or []:
            if not isinstance(item, dict):
                continue
            stories.append({"name": item.get("name"), "cultural_tension_ref": item.get("cultural_tension_ref")})
            if len(stories) >= 3:
                break
        return {"competitors": competitors, "unowned_stories": stories}

    @staticmethod
    def _context_for_stage(stage_id: str, context: dict[str, Any]) -> dict[str, Any]:
        if stage_id not in {"research", "competitive", "audience"}:
            return context
        compact = {
            "cycle_id": context.get("cycle_id"),
            "brand_slug": context.get("brand_slug"),
            "collected_sources": AnthropicProvider._compact_sources(context),
        }
        if stage_id in {"competitive", "audience"}:
            compact["research"] = AnthropicProvider._compact_research(context.get("research"))
        if stage_id == "audience":
            compact["competitive"] = AnthropicProvider._compact_competitive(context.get("competitive"))
        return compact


def _clip_text(value: Any, limit: int) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


def build_generate_prompt(stage_id: str, brief: str, context: dict[str, Any], revision: int) -> dict[str, Any]:
    prompt = {
        "role": "BURN Strategy OS evidence-led strategist",
        "stage": stage_id,
        "revision": revision,
        "brief": brief,
        "context": AnthropicProvider._context_for_stage(stage_id, context),
        "rules": AnthropicProvider._rules_for_stage(stage_id),
    }
    schemas = {
        "research": AnthropicProvider._research_output_schema,
        "competitive": AnthropicProvider._competitive_output_schema,
        "audience": AnthropicProvider._audience_output_schema,
    }
    builder = schemas.get(stage_id)
    if builder:
        prompt["output_schema"] = builder()
    return prompt


class XaiProvider:
    """Grok chat-completions adapter for strategy stages. Does not run web search."""

    def __init__(self, api_key: str, model: str = DEFAULT_XAI_MODEL) -> None:
        self.api_key = api_key
        self.model = model or DEFAULT_XAI_MODEL

    def generate(self, stage_id: str, brief: str, context: dict[str, Any], revision: int) -> dict[str, Any]:
        return self._request(json.dumps(build_generate_prompt(stage_id, brief, context, revision)), STAGE_MAX_TOKENS.get(stage_id, DEFAULT_STAGE_MAX_TOKENS))

    def collect_sources(self, brief: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        raise ProviderError("Grok is not used for source collection; Anthropic web search is required.")

    def _request(self, prompt: str, max_tokens: int) -> dict[str, Any]:
        payload = self._post({
            "model": self.model,
            "temperature": 0.2,
            "max_completion_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "Return only valid JSON. No markdown fences. No preamble."},
                {"role": "user", "content": prompt},
            ],
        })
        self._validate_response(payload)
        text = self._extract_text(payload)
        if not text.strip():
            raise ProviderError("The model completed without returning structured text; try the cycle again.")
        try:
            result = AnthropicProvider._parse_json_text(text)
        except json.JSONDecodeError as exc:
            raise ProviderError("The model returned text that was not valid structured JSON; try the cycle again.") from exc
        if not isinstance(result, dict):
            raise ProviderError("The model returned an unexpected structured result instead of a JSON object.")
        return result

    def _post(self, request_payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            XAI_API_URL,
            data=json.dumps(request_payload).encode(),
            headers={
                "content-type": "application/json",
                "authorization": "Bearer %s" % self.api_key,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read().decode()
        except HTTPError as exc:
            raise ProviderError(self._http_failure(exc.code)) from exc
        except (URLError, TimeoutError) as exc:
            raise ProviderError("The Grok provider could not complete this strategy stage because the connection failed; try again.") from exc
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderError("The Grok provider returned an unreadable response; try again.") from exc
        if not isinstance(payload, dict):
            raise ProviderError("The Grok provider returned an unexpected response shape; try again.")
        return payload

    @staticmethod
    def _http_failure(status: int) -> str:
        if status in {401, 403}:
            return "The Grok provider rejected authentication; check the configured Railway XAI_API_KEY."
        if status == 429:
            return "The Grok provider rate limit was reached; wait briefly and try again."
        if status >= 500:
            return "The Grok provider is temporarily unavailable; try again shortly."
        return "The Grok provider rejected the request (HTTP %d); review the stage configuration." % status

    @staticmethod
    def _validate_response(payload: dict[str, Any]) -> None:
        if isinstance(payload.get("error"), dict):
            error = payload["error"]
            error_type = error.get("type") if isinstance(error.get("type"), str) else "provider_error"
            raise ProviderError("The Grok provider returned an API error (%s); try again or review the provider configuration." % error_type)
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError("The Grok provider returned an unexpected response shape; try again.")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        if isinstance(message.get("refusal"), str) and message["refusal"].strip():
            raise ProviderError("The model declined this request; review the brief wording before trying again.")
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise ProviderError("The model response was cut off before the structured result was complete; reduce the brief or raise the output limit.")
        if finish_reason == "content_filter":
            raise ProviderError("The model declined this request; review the brief wording before trying again.")
        if finish_reason not in {"stop", "end_turn", None}:
            label = finish_reason if isinstance(finish_reason, str) else "missing"
            raise ProviderError("The model returned an unexpected completion state (%s); try again." % label)

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str:
        choice = payload["choices"][0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ProviderError("The Grok provider returned an unexpected response shape; try again.")
        value = message.get("content")
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ProviderError("The Grok provider returned an invalid text block; try again.")
        return value


class LiveStrategyProvider:
    """Sonnet collects sources. Grok 4.6 writes every later strategy stage."""

    def __init__(self, anthropic: AnthropicProvider, xai: XaiProvider) -> None:
        self.anthropic = anthropic
        self.xai = xai

    def generate(self, stage_id: str, brief: str, context: dict[str, Any], revision: int) -> dict[str, Any]:
        return self.xai.generate(stage_id, brief, context, revision)

    def collect_sources(self, brief: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        return self.anthropic.collect_sources(brief, context)


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
