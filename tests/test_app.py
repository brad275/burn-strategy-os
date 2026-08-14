"""Private HTTP pilot contracts and persistence checks."""

from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from fastapi.testclient import TestClient

from strategy_os.app import create_app
from strategy_os.config import Settings
from strategy_os.provider import FixtureProvider


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

