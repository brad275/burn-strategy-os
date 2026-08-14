"""Private Strategy OS pilot HTTP application."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Optional, Set
from uuid import uuid4

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import COOKIE_NAME, issue_cookie, read_cookie
from .config import Settings
from .errors import ConflictError, NotFoundError, ValidationError
from .models import CycleManifest, FeedbackEvent, FeedbackVerdict, Project, utc_now, validate_id
from .provider import AnthropicProvider, FixtureProvider, StrategyProvider
from .repository import VaultRepository
from .workflow import WorkflowRunner


def slug(value: str) -> str:
    candidate = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if len(candidate) < 3:
        candidate = "brand-" + candidate
    return candidate[:100]


class ProjectCreate(BaseModel):
    name: str = Field(min_length=3, max_length=160)
    brand_name: str = Field(min_length=2, max_length=160)
    organisation_name: str = Field(default="BURN", min_length=2, max_length=160)
    brief_markdown: str = Field(min_length=30, max_length=30000)
    expected_revision: int = Field(default=0, ge=0)
    mode: str = Field(default="live", pattern="^(live|fixture)$")


class CycleCreate(BaseModel):
    expected_revision: int = Field(ge=1)
    mode: str = Field(default="live", pattern="^(live|fixture)$")


class FeedbackCreate(BaseModel):
    target_type: str = Field(pattern="^(record|stage_output|document)$")
    target_id: str = Field(min_length=3, max_length=128)
    verdict: FeedbackVerdict
    reason: Optional[str] = Field(default=None, max_length=3000)
    replacement: Optional[str] = Field(default=None, max_length=3000)
    expected_revision: int = Field(ge=1)


class MemoryDecision(BaseModel):
    proposal_id: str
    approved_change_ids: list[str]
    memory_version: Optional[str] = None
    expected_revision: int = Field(ge=1)


def create_app(settings: Optional[Settings] = None, provider: Optional[StrategyProvider] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    repository = VaultRepository(settings.data_root)
    app = FastAPI(title="BURN Strategy OS", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.repository = repository
    app.state.provider = provider
    app.state.running_cycles: Set[str] = set()
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.exception_handler(ValidationError)
    async def validation_error(_: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(ConflictError)
    async def conflict_error(_: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "code": "revision_conflict"}, status_code=409)

    @app.exception_handler(NotFoundError)
    async def not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    def actor(request: Request) -> str:
        current = read_cookie(settings.app_access_token, request.cookies.get(COOKIE_NAME))
        if not current:
            raise HTTPException(status_code=401, detail="Private pilot access is required.")
        return current

    @app.get("/health")
    def health() -> dict[str, Any]:
        try:
            marker = settings.data_root / ".healthcheck"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch(exist_ok=True)
            marker.unlink(missing_ok=True)
        except OSError:
            return {"status": "degraded", "storage": "unavailable"}
        return {"status": "ok", "storage": "writable"}

    @app.get("/login", response_class=HTMLResponse)
    def login() -> str:
        return _login_html()

    @app.post("/login")
    def login_submit(access_token: str = Form(...)) -> Response:
        if not settings.app_access_token or not hashlib.sha256(access_token.encode()).digest() == hashlib.sha256(settings.app_access_token.encode()).digest():
            return HTMLResponse(_login_html("That access token is not recognised."), status_code=401)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE_NAME, issue_cookie(settings.app_access_token), httponly=True, secure=settings.secure_cookie, samesite="strict", max_age=60 * 60 * 12)
        return response

    @app.post("/logout")
    def logout(_: str = Depends(actor)) -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        return response

    @app.get("/", response_class=HTMLResponse)
    def home(_: str = Depends(actor)) -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/api/projects")
    def projects(_: str = Depends(actor)) -> dict[str, Any]:
        return {"items": [_project_summary(repository, path) for path in repository.root.glob("organisations/*/brands/*/projects/*/project.json")]}

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectCreate, _: str = Depends(actor)) -> dict[str, Any]:
        organisation_id, brand_id = slug(body.organisation_name), slug(body.brand_name)
        project_id = "project-" + uuid4().hex[:18]
        project = Project(project_id=project_id, organisation_id=organisation_id, brand_id=brand_id, name=body.name, created_by="brad")
        repository.create_project(project, body.brief_markdown)
        _write_project_metadata(repository, project, body.organisation_name, body.brand_name, body.mode)
        return {"project": project.to_dict(), "brand_name": body.brand_name, "organisation_name": body.organisation_name}

    @app.get("/api/projects/{organisation_id}/{brand_id}/{project_id}")
    def project_detail(organisation_id: str, brand_id: str, project_id: str, _: str = Depends(actor)) -> dict[str, Any]:
        project = repository.get_project(organisation_id, brand_id, project_id)
        return _project_detail(repository, project)

    @app.post("/api/projects/{organisation_id}/{brand_id}/{project_id}/cycles", status_code=202)
    async def create_cycle(organisation_id: str, brand_id: str, project_id: str, body: CycleCreate, _: str = Depends(actor)) -> dict[str, Any]:
        project = repository.get_project(organisation_id, brand_id, project_id)
        if project.revision != body.expected_revision:
            raise ConflictError("project revision changed; refresh and try again")
        metadata = _project_metadata(repository, project)
        mode = body.mode or metadata.get("mode", "live")
        if mode == "live" and not settings.anthropic_api_key:
            raise HTTPException(status_code=503, detail="The live model key has not been configured in Railway yet. Use fixture verification locally only.")
        cycle = CycleManifest(cycle_id="cycle-" + uuid4().hex[:18], organisation_id=organisation_id, brand_id=brand_id, project_id=project_id, brief_sha256=repository.sha256_text(_brief(repository, project)), prompt_versions={"workflow": "pilot-v1"}, code_version=settings.code_version)
        repository.create_cycle(cycle)
        selected = app.state.provider or (FixtureProvider() if mode == "fixture" else AnthropicProvider(settings.anthropic_api_key or ""))
        task = asyncio.create_task(_run_cycle(app, selected, organisation_id, brand_id, project_id, cycle.cycle_id))
        task.add_done_callback(lambda __: app.state.running_cycles.discard(cycle.cycle_id))
        app.state.running_cycles.add(cycle.cycle_id)
        return {"cycle": cycle.to_dict(), "poll": f"/api/cycles/{organisation_id}/{brand_id}/{project_id}/{cycle.cycle_id}"}

    @app.get("/api/cycles/{organisation_id}/{brand_id}/{project_id}/{cycle_id}")
    def cycle_detail(organisation_id: str, brand_id: str, project_id: str, cycle_id: str, _: str = Depends(actor)) -> dict[str, Any]:
        cycle = repository.get_cycle(organisation_id, brand_id, project_id, cycle_id)
        directory = _cycle_dir(repository, organisation_id, brand_id, project_id, cycle_id)
        return {"cycle": cycle.to_dict(), "stages": _stage_summaries(directory), "ledger": _ledger(directory), "document_ready": (directory / "strategy-document.md").exists(), "memory_proposals": _memory_proposals(repository, organisation_id, brand_id)}

    @app.get("/api/cycles/{organisation_id}/{brand_id}/{project_id}/{cycle_id}/stages/{stage_id}")
    def stage_output(organisation_id: str, brand_id: str, project_id: str, cycle_id: str, stage_id: str, _: str = Depends(actor)) -> dict[str, Any]:
        directory = _cycle_dir(repository, organisation_id, brand_id, project_id, cycle_id) / "attempts" / stage_id
        items = [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))] if directory.exists() else []
        return {"items": items}

    @app.get("/api/cycles/{organisation_id}/{brand_id}/{project_id}/{cycle_id}/document")
    def document(organisation_id: str, brand_id: str, project_id: str, cycle_id: str, _: str = Depends(actor)) -> FileResponse:
        path = _cycle_dir(repository, organisation_id, brand_id, project_id, cycle_id) / "strategy-document.md"
        if not path.exists():
            raise NotFoundError("strategy document is not ready")
        return FileResponse(path, media_type="text/markdown", filename="burn-strategy-document.md")

    @app.post("/api/cycles/{organisation_id}/{brand_id}/{project_id}/{cycle_id}/feedback", status_code=201)
    def feedback(organisation_id: str, brand_id: str, project_id: str, cycle_id: str, body: FeedbackCreate, current_actor: str = Depends(actor)) -> dict[str, Any]:
        cycle = repository.get_cycle(organisation_id, brand_id, project_id, cycle_id)
        if cycle.revision != body.expected_revision:
            raise ConflictError("cycle revision changed; refresh and try again")
        event = FeedbackEvent(feedback_id="feedback-" + uuid4().hex[:18], organisation_id=organisation_id, brand_id=brand_id, project_id=project_id, cycle_id=cycle_id, target_type=body.target_type, target_id=slug(body.target_id), verdict=body.verdict, reason=body.reason, replacement=body.replacement, created_by=current_actor)
        repository.add_feedback(event)
        return {"feedback": event.to_dict()}

    @app.post("/api/projects/{organisation_id}/{brand_id}/{project_id}/memory/decisions")
    def decide_memory(organisation_id: str, brand_id: str, project_id: str, body: MemoryDecision, current_actor: str = Depends(actor)) -> dict[str, Any]:
        project = repository.get_project(organisation_id, brand_id, project_id)
        if project.revision != body.expected_revision:
            raise ConflictError("project revision changed; refresh and try again")
        decision = repository.decide_memory_proposal(organisation_id, brand_id, body.proposal_id, current_actor, body.approved_change_ids, "decision-" + uuid4().hex[:18], body.memory_version)
        return {"decision": decision}

    return app


async def _run_cycle(app: FastAPI, provider: StrategyProvider, organisation_id: str, brand_id: str, project_id: str, cycle_id: str) -> None:
    await asyncio.to_thread(WorkflowRunner(app.state.repository, provider).run, organisation_id, brand_id, project_id, cycle_id)


def _cycle_dir(repository: VaultRepository, organisation_id: str, brand_id: str, project_id: str, cycle_id: str) -> Path:
    return repository.root / "organisations" / organisation_id / "brands" / brand_id / "projects" / project_id / "cycles" / cycle_id


def _brief(repository: VaultRepository, project: Project) -> str:
    return (repository.root / "organisations" / project.organisation_id / "brands" / project.brand_id / "projects" / project.project_id / "brief.md").read_text()


def _write_project_metadata(repository: VaultRepository, project: Project, organisation_name: str, brand_name: str, mode: str) -> None:
    path = repository.root / "organisations" / project.organisation_id / "brands" / project.brand_id / "projects" / project.project_id / "display.json"
    path.write_text(json.dumps({"organisation_name": organisation_name, "brand_name": brand_name, "mode": mode}), encoding="utf-8")


def _project_metadata(repository: VaultRepository, project: Project) -> dict[str, Any]:
    path = repository.root / "organisations" / project.organisation_id / "brands" / project.brand_id / "projects" / project.project_id / "display.json"
    return json.loads(path.read_text()) if path.exists() else {"mode": "live"}


def _project_summary(repository: VaultRepository, path: Path) -> dict[str, Any]:
    project = Project(**{**json.loads(path.read_text()), "status": json.loads(path.read_text())["status"]})
    return {**project.to_dict(), **_project_metadata(repository, project)}


def _project_detail(repository: VaultRepository, project: Project) -> dict[str, Any]:
    directory = repository.root / "organisations" / project.organisation_id / "brands" / project.brand_id / "projects" / project.project_id
    cycles = [json.loads(path.read_text()) for path in sorted(directory.glob("cycles/*/manifest.json"), reverse=True)]
    return {"project": project.to_dict(), "brief_markdown": _brief(repository, project), "cycles": cycles, **_project_metadata(repository, project)}


def _stage_summaries(directory: Path) -> list[dict[str, Any]]:
    result = []
    for stage_dir in sorted((directory / "attempts").glob("*")) if (directory / "attempts").exists() else []:
        attempts = [json.loads(path.read_text()) for path in sorted(stage_dir.glob("*.json"))]
        result.append({"stage": stage_dir.name, "attempts": len(attempts), "status": attempts[-1]["status"] if attempts else "pending"})
    return result


def _ledger(directory: Path) -> list[dict[str, Any]]:
    path = directory / "learning-ledger.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _memory_proposals(repository: VaultRepository, organisation_id: str, brand_id: str) -> list[dict[str, Any]]:
    directory = repository.root / "organisations" / organisation_id / "brands" / brand_id / "memory" / "proposals"
    return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"), reverse=True)] if directory.exists() else []


def _login_html(error: str = "") -> str:
    message = f'<p class="error">{error}</p>' if error else ""
    return f'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>BURN Strategy OS</title><link rel="stylesheet" href="/static/style.css"></head><body class="login"><main><p class="eyebrow">PRIVATE PILOT</p><h1>BURN Strategy OS.</h1><p>Enter the private access token to continue.</p>{message}<form method="post"><label>ACCESS TOKEN<input name="access_token" type="password" autocomplete="current-password" required></label><button>Enter Strategy OS</button></form></main></body></html>'''


app = create_app()
