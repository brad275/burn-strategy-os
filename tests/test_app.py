"""Private HTTP pilot contracts and persistence checks."""

from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from fastapi.testclient import TestClient

from strategy_os.app import create_app
from strategy_os.config import Settings
from strategy_os.provider import FixtureProvider, ProviderError


class FailingProvider(FixtureProvider):
    def collect_sources(self, brief, context):
        raise ProviderError("Source research paused without resumable search results.")


class PilotAppTests(unittest.TestCase):
    def make_app(self, root: Path):
        return create_app(Settings(root, "test-access-token", None, "test", "test-sha"), FixtureProvider())

    def login(self, client: TestClient) -> None:
        response = client.post("/login", data={"access_token": "test-access-token"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)

    def test_private_gate_and_health_do_not_expose_secrets(self) -> None:
        with TemporaryDirectory() as raw:
            with TestClient(self.make_app(Path(raw))) as client:
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertNotIn("test-access-token", client.get("/health").text)
                self.assertEqual(client.get("/api/projects").status_code, 401)
                self.assertEqual(client.get("/", follow_redirects=False).status_code, 303)
                self.assertEqual(client.post("/login", data={"access_token": "wrong"}).status_code, 401)

    def test_fixture_cycle_persists_document_and_memory_proposal(self) -> None:
        with TemporaryDirectory() as raw:
            settings_root = Path(raw)
            with TestClient(self.make_app(settings_root)) as client:
                self.login(client)
                created = client.post("/api/projects", json={
                    "name": "Pragmatic Play strategy pilot",
                    "brand_name": "Pragmatic Play",
                    "brief_markdown": "A detailed working brief for a Pragmatic Play strategy pilot. It names the business decision, audience, category context, creative ambition and a useful deadline.",
                    "mode": "fixture", "expected_revision": 0,
                })
                self.assertEqual(created.status_code, 201)
                project = created.json()["project"]
                started = client.post(
                    "/api/projects/{}/{}/{}/cycles".format(project["organisation_id"], project["brand_id"], project["project_id"]),
                    json={"expected_revision": 1, "mode": "fixture"},
                )
                self.assertEqual(started.status_code, 202)
                cycle_id = started.json()["cycle"]["cycle_id"]
                path = "/api/cycles/{}/{}/{}/{}".format(project["organisation_id"], project["brand_id"], project["project_id"], cycle_id)
                for _ in range(30):
                    status = client.get(path).json()
                    if status["cycle"]["status"] in {"completed", "failed"}:
                        break
                    time.sleep(0.05)
                self.assertEqual(status["cycle"]["status"], "completed")
                self.assertTrue(status["document_ready"])
                self.assertEqual(len(status["memory_proposals"]), 1)
                self.assertGreaterEqual(len(status["stages"]), 9)
            with TestClient(self.make_app(settings_root)) as after_redeploy:
                self.login(after_redeploy)
                self.assertEqual(len(after_redeploy.get("/api/projects").json()["items"]), 1)

    def test_live_cycle_requires_configured_key(self) -> None:
        with TemporaryDirectory() as raw:
            with TestClient(self.make_app(Path(raw))) as client:
                self.login(client)
                project = client.post("/api/projects", json={
                    "name": "Pragmatic Play strategy pilot", "brand_name": "Pragmatic Play",
                    "brief_markdown": "A detailed working brief that is sufficiently long for a secure live-provider configuration failure test.",
                    "mode": "live", "expected_revision": 0,
                }).json()["project"]
                response = client.post(
                    "/api/projects/{}/{}/{}/cycles".format(project["organisation_id"], project["brand_id"], project["project_id"]),
                    json={"expected_revision": 1, "mode": "live"},
                )
                self.assertEqual(response.status_code, 503)
                self.assertIn("not been configured", response.json()["detail"])

    def test_live_cycle_requires_xai_key(self) -> None:
        with TemporaryDirectory() as raw:
            app = create_app(Settings(Path(raw), "test-access-token", "configured", "test", "test-sha"))
            with TestClient(app) as client:
                self.login(client)
                project = client.post("/api/projects", json={
                    "name": "Pragmatic Play strategy pilot", "brand_name": "Pragmatic Play",
                    "brief_markdown": "A detailed working brief that is sufficiently long for a secure live-provider configuration failure test.",
                    "mode": "live", "expected_revision": 0,
                }).json()["project"]
                response = client.post(
                    "/api/projects/{}/{}/{}/cycles".format(project["organisation_id"], project["brand_id"], project["project_id"]),
                    json={"expected_revision": 1, "mode": "live"},
                )
                self.assertEqual(response.status_code, 503)
                self.assertIn("not been configured", response.json()["detail"])

    def test_failed_cycle_exposes_safe_ledger_reason(self) -> None:
        with TemporaryDirectory() as raw:
            app = create_app(Settings(Path(raw), "test-access-token", "configured", "test", "test-sha", xai_api_key="configured"), FailingProvider())
            with TestClient(app) as client:
                self.login(client)
                project = client.post("/api/projects", json={
                    "name": "Pragmatic Play strategy pilot", "brand_name": "Pragmatic Play",
                    "brief_markdown": "A detailed working brief for testing a safely reported live provider failure in the cycle ledger.",
                    "mode": "live", "expected_revision": 0,
                }).json()["project"]
                started = client.post(
                    "/api/projects/{}/{}/{}/cycles".format(project["organisation_id"], project["brand_id"], project["project_id"]),
                    json={"expected_revision": 1, "mode": "live"},
                ).json()["cycle"]
                path = "/api/cycles/{}/{}/{}/{}".format(project["organisation_id"], project["brand_id"], project["project_id"], started["cycle_id"])
                for _ in range(30):
                    status = client.get(path).json()
                    if status["cycle"]["status"] == "failed":
                        break
                    time.sleep(0.05)
                failure = status["ledger"][-1]
                self.assertEqual(failure["event"], "workflow_failed")
                self.assertEqual(failure["detail"]["stage"], "source_collection")
                self.assertIn("resumable search results", failure["detail"]["message"])
                self.assertIn("Retry this cycle from the stopped stage", failure["detail"]["action"])

    def test_retry_resumes_from_failed_stage_without_recollecting_sources(self) -> None:
        class StrategyCutoffProvider(FixtureProvider):
            def __init__(self):
                self.source_calls = 0
                self.generated = []
                self.strategy_failures = 1

            def collect_sources(self, brief, context):
                self.source_calls += 1
                return super().collect_sources(brief, context)

            def generate(self, stage_id, brief, context, revision):
                self.generated.append(stage_id)
                if stage_id == "strategy" and self.strategy_failures:
                    self.strategy_failures -= 1
                    raise ProviderError("The model response was cut off before the structured result was complete; reduce the brief or raise the output limit.")
                return super().generate(stage_id, brief, context, revision)

        provider = StrategyCutoffProvider()
        with TemporaryDirectory() as raw:
            app = create_app(Settings(Path(raw), "test-access-token", "configured", "test", "test-sha", xai_api_key="configured"), provider)
            with TestClient(app) as client:
                self.login(client)
                project = client.post("/api/projects", json={
                    "name": "Pragmatic Play strategy pilot", "brand_name": "Pragmatic Play",
                    "brief_markdown": "A detailed working brief for testing retry from a failed synthesis stage without repeating source collection.",
                    "mode": "live", "expected_revision": 0,
                }).json()["project"]
                started = client.post(
                    "/api/projects/{}/{}/{}/cycles".format(project["organisation_id"], project["brand_id"], project["project_id"]),
                    json={"expected_revision": 1, "mode": "live"},
                ).json()["cycle"]
                path = "/api/cycles/{}/{}/{}/{}".format(project["organisation_id"], project["brand_id"], project["project_id"], started["cycle_id"])
                for _ in range(40):
                    status = client.get(path).json()
                    if status["cycle"]["status"] == "failed":
                        break
                    time.sleep(0.05)
                self.assertEqual(status["cycle"]["status"], "failed")
                self.assertEqual(status["ledger"][-1]["detail"]["stage"], "strategy")
                self.assertEqual(provider.source_calls, 1)
                self.assertNotIn("reconciliation", provider.generated)
                retried = client.post(path + "/retry", json={"expected_revision": status["cycle"]["revision"]})
                self.assertEqual(retried.status_code, 202)
                for _ in range(80):
                    status = client.get(path).json()
                    if status["cycle"]["status"] in {"completed", "failed"}:
                        break
                    time.sleep(0.05)
                self.assertEqual(status["cycle"]["status"], "completed", status)
                self.assertEqual(provider.source_calls, 1)
                self.assertEqual(provider.generated.count("strategy"), 2)
                self.assertTrue(status["document_ready"])

    def test_retry_rejects_a_completed_cycle(self) -> None:
        with TemporaryDirectory() as raw:
            with TestClient(self.make_app(Path(raw))) as client:
                self.login(client)
                created = client.post("/api/projects", json={
                    "name": "Pragmatic Play strategy pilot",
                    "brand_name": "Pragmatic Play",
                    "brief_markdown": "A detailed working brief used to prove completed cycles cannot be retried from a failed-stage endpoint.",
                    "mode": "fixture", "expected_revision": 0,
                })
                project = created.json()["project"]
                started = client.post(
                    "/api/projects/{}/{}/{}/cycles".format(project["organisation_id"], project["brand_id"], project["project_id"]),
                    json={"expected_revision": 1, "mode": "fixture"},
                )
                cycle_id = started.json()["cycle"]["cycle_id"]
                path = "/api/cycles/{}/{}/{}/{}".format(project["organisation_id"], project["brand_id"], project["project_id"], cycle_id)
                for _ in range(30):
                    status = client.get(path).json()
                    if status["cycle"]["status"] in {"completed", "failed"}:
                        break
                    time.sleep(0.05)
                self.assertEqual(status["cycle"]["status"], "completed")
                response = client.post(path + "/retry", json={"expected_revision": status["cycle"]["revision"]})
                self.assertEqual(response.status_code, 409)

    def test_delete_project_removes_it_from_the_homepage(self) -> None:
        with TemporaryDirectory() as raw:
            with TestClient(self.make_app(Path(raw))) as client:
                self.login(client)
                project = client.post("/api/projects", json={
                    "name": "Pragmatic Play strategy pilot", "brand_name": "Pragmatic Play",
                    "brief_markdown": "A detailed working brief used only to verify that a stale project can be removed from the homepage.",
                    "mode": "fixture", "expected_revision": 0,
                }).json()["project"]
                path = "/api/projects/{}/{}/{}".format(project["organisation_id"], project["brand_id"], project["project_id"])
                self.assertEqual(len(client.get("/api/projects").json()["items"]), 1)
                self.assertEqual(client.delete(path).status_code, 200)
                self.assertEqual(client.get("/api/projects").json()["items"], [])
                self.assertEqual(client.get(path).status_code, 404)
