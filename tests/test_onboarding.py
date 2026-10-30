"""First-data setup, editable settings, and delivery/source separation."""

import copy
import json
import re
from dataclasses import replace
from unittest.mock import Mock, patch

from src.core.operations import OperationError
from src.integrations.registry import INTEGRATIONS
from tests.test_artifacts import export
from tests.test_web import BEARER, WebFixture


class OnboardingTests(WebFixture):
    def setUp(self):
        super().setUp()
        self.login()

    def add_form(self, method, connection_id="new_health", extra=None):
        return self.client.post(
            "/connections/new",
            data={
                "integration": "apple_health",
                "id": connection_id,
                "method": method,
                "setting.cutoff": "2026-06-04",
                **(extra or {}),
            },
            follow_redirects=False,
        )

    def test_delivery_choice_filters_fields_and_validates_before_saving(self):
        choice = self.client.get("/connections/new?integration=apple_health")
        self.assertIn("How do you want to send data?", choice.text)
        self.assertIn("method=webhook", choice.text)
        self.assertIn("method=upload", choice.text)
        self.assertIn("Advanced delivery methods", choice.text)
        for method in ("upload", "webhook"):
            form = self.client.get(
                f"/connections/new?integration=apple_health&method={method}"
            )
            self.assertIn('name="setting.cutoff" required', form.text)
            self.assertNotIn('name="setting.data_dir"', form.text)
            self.assertNotIn('name="credential.', form.text)
        folder = self.client.get(
            "/connections/new?integration=apple_health&method=folder"
        )
        self.assertIn('name="folder.path" value="" required', folder.text)
        original = self.config_path.read_text()
        for method, extra in (
            ("folder", {}),
            ("webhook", {"folder.path": "/unwanted"}),
            ("wrong", {}),
        ):
            response = self.add_form(method, extra=extra)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.config_path.read_text(), original)
        self.assertEqual(
            self.client.get(
                "/connections/new?integration=listenbrainz&method=webhook"
            ).status_code,
            400,
        )

    def test_webhook_onboarding_receives_first_data_and_keeps_token_one_time(self):
        response = self.add_form("webhook")
        self.assertEqual(
            response.headers["location"], "/connections/new_health/setup?method=webhook"
        )
        setup = self.client.get(response.headers["location"])
        self.assertIn("Enable automatic delivery", setup.text)
        self.assertFalse(self.service.connection("new_health")["delivery"]["enabled"])
        enabled = self.client.post(
            "/connections/new_health/setup", data={"method": "webhook"}
        )
        self.assertEqual(enabled.status_code, 200)
        self.assertEqual(enabled.headers["cache-control"], "no-store")
        self.assertNotIn('http-equiv="refresh"', enabled.text)
        token = re.search(r'value="Bearer ([^"]+)"', enabled.text)[1]
        waiting = self.client.get("/connections/new_health/setup?method=webhook")
        self.assertNotIn(token, waiting.text)
        self.assertIn("Waiting for your first automatic export", waiting.text)
        self.assertIn('http-equiv="refresh"', waiting.text)
        # Retrying setup and viewing settings cannot silently replace a working token.
        self.client.post("/connections/new_health/setup", data={"method": "webhook"})
        self.assertTrue(self.service.verify_delivery_token("new_health", token))
        settings = self.client.get("/connections/new_health/settings")
        self.assertIn("Delivery token active", settings.text)
        self.assertNotIn(token, settings.text)
        response = self.client.post(
            "/api/connections/new_health/receive",
            headers={"Authorization": f"Bearer {token}"},
            content=json.dumps(export(("2026-06-05", 1))),
        )
        self.assertEqual(response.status_code, 201)
        complete = self.client.get("/connections/new_health/setup?method=webhook")
        self.assertIn(
            "An automatic export was received and imported successfully", complete.text
        )
        self.assertNotIn('http-equiv="refresh"', complete.text)
        activity = self.client.get("/connections/new_health")
        self.assertIn("via webhook", activity.text)
        self.assertNotIn("Replace delivery token", activity.text)
        self.assertNotIn("Remove connection", activity.text)
        self.assertIn("Upload an export", activity.text)

    def test_manual_upload_does_not_claim_the_sender_has_connected(self):
        created = self.add_form("upload")
        setup = self.client.get(created.headers["location"])
        self.assertIn("Upload your first export", setup.text)
        self.assertNotIn("Enable automatic delivery", setup.text)
        response = self.client.post(
            "/connections/new_health/upload",
            files={"file": ("export.json", json.dumps(export(("2026-06-05", 1))))},
            data={"option.role": "ongoing", "import_now": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.client.post("/connections/new_health/setup", data={"method": "webhook"})
        waiting = self.client.get("/connections/new_health/setup?method=webhook")
        self.assertIn("Waiting for your first automatic export", waiting.text)
        self.assertNotIn("imported successfully", waiting.text)

    def test_setup_failure_is_retryable_and_scopes_execution_to_one_connection(self):
        self.add_form("webhook")
        backend = Mock()
        backend.plan.side_effect = OperationError(
            "Execution is unavailable; try again."
        )
        self.service.orchestrator = backend
        response = self.client.post(
            "/connections/new_health/setup", data={"method": "webhook"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Your connection is saved", response.text)
        self.assertFalse(self.service.connection("new_health")["delivery"]["enabled"])
        self.assertEqual(len(self.service.miroir().connections), 3)
        own = {
            "action": "create",
            "connection_id": "new_health",
            "deployment": "personal-new_health",
        }
        backend.plan.side_effect = None
        backend.plan.return_value = [
            own,
            {
                "action": "update",
                "connection_id": "music",
                "deployment": "personal-music",
            },
            {"action": "delete", "deployment": "personal-removed"},
        ]
        response = self.client.post(
            "/connections/new_health/setup", data={"method": "webhook"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(backend.apply.call_args.args[1], [own])
        self.assertTrue(self.service.connection("new_health")["delivery"]["enabled"])

    def test_settings_edits_detect_conflicts_and_preserve_other_configuration(self):
        raw = json.loads(self.config_path.read_text())
        raw["connections"][1]["schedule"] = {"cron": "0 0 * * *", "timezone": "UTC"}
        raw["connections"][1]["binding"] = {
            "pipeline_name": "health_pipeline",
            "dataset_name": "health_dataset",
        }
        self.config_path.write_text(json.dumps(raw))
        original, _ = self.service._raw_config()
        view = self.service.connection_settings("health")
        response = self.client.post(
            "/connections/health/settings",
            data={
                "file_revision": view["file_revision"],
                "setting.cutoff": "2026-06-05",
            },
        )
        self.assertEqual(response.status_code, 200)
        raw = json.loads(self.config_path.read_text())
        expected = copy.deepcopy(original)
        expected["connections"][1]["settings"]["cutoff"] = "2026-06-05"
        self.assertEqual(raw, expected)
        stale = self.client.post(
            "/connections/health/settings",
            data={
                "file_revision": view["file_revision"],
                "setting.cutoff": "2026-06-06",
            },
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(json.loads(self.config_path.read_text()), expected)
        revision = self.service.connection_settings("health")["file_revision"]
        invalid = self.client.put(
            "/api/connections/health/settings",
            headers={**BEARER, "If-Match": revision},
            json={"cutoff": "bad-date"},
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(json.loads(self.config_path.read_text()), expected)
        # Folder settings can be added and removed without affecting other delivery.
        self.service.update_folder("health", str(self.root), revision)
        self.assertTrue(self.service.connection("health")["can_sync"])
        revision = self.service.connection_settings("health")["file_revision"]
        self.service.update_folder("health", None, revision)
        self.assertFalse(self.service.connection("health")["can_sync"])
        self.assertTrue(self.service.connection("health")["accepts_files"])

    def test_another_file_integration_uses_the_same_setup_and_settings_views(self):
        health = INTEGRATIONS["apple_health"]
        definition = replace(
            health,
            name="other_files",
            title="Other files",
            formats=(
                replace(
                    health.formats[0],
                    id="example_json",
                    label="Example JSON",
                    sender="Automatic delivery",
                    sender_instructions="Send JSON from your preferred client.",
                ),
            ),
        )
        registry = {**INTEGRATIONS, "other_files": definition}
        with (
            patch("src.integrations.registry.INTEGRATIONS", registry),
            patch("src.core.config.INTEGRATIONS", registry),
        ):
            self.service.create_connection(
                "other_files",
                "other",
                {"cutoff": "2026-06-04"},
                {},
                delivery_method="webhook",
            )
            enabled = self.client.post(
                "/connections/other/setup", data={"method": "webhook"}
            )
            self.assertEqual(enabled.status_code, 200)
            self.assertIn("Send JSON from your preferred client.", enabled.text)
            self.assertNotIn("Health Auto Export", enabled.text)
            activity = self.client.get("/connections/other")
            self.assertIn("Choose a Example JSON file", activity.text)
            self.assertNotIn("Health Auto Export", activity.text)
            self.assertEqual(
                self.client.get("/connections/other/settings").status_code, 200
            )


class FolderSettingsTests(WebFixture):
    def setUp(self):
        super().setUp()
        self.login()

    def save_folder(self, path):
        revision = self.service.connection_settings("health")["file_revision"]
        return self.client.post(
            "/connections/health/folder",
            data={"file_revision": revision, "folder.path": path},
            follow_redirects=False,
        )

    def test_folder_is_delivery_configuration_not_a_source_setting(self):
        page = self.client.get("/connections/health")
        # Format options render as choices on the upload form.
        self.assertIn('name="option.role"', page.text)
        self.assertEqual(self.save_folder(str(self.root)).status_code, 303)
        raw = json.loads(self.config_path.read_text())
        health = next(c for c in raw["connections"] if c["id"] == "health")
        self.assertEqual(health["folder"], {"path": str(self.root)})
        self.assertNotIn("data_dir", health["settings"])
        view = self.service.connection_settings("health")
        self.assertEqual(view["folder_values"]["pattern"], "*.json")
        self.assertTrue(self.service.connection("health")["can_sync"])
        self.assertEqual(self.save_folder("relative").status_code, 400)
        self.assertEqual(self.save_folder("").status_code, 303)
        raw = json.loads(self.config_path.read_text())
        health = next(c for c in raw["connections"] if c["id"] == "health")
        self.assertNotIn("folder", health)
