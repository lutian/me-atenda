"""Public Compose configuration checks, without real environment files or Docker startup."""
import json
import os
from pathlib import Path
import subprocess
import unittest


class ComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[2]
        # Do not inherit credentials, Compose overrides, or a user's environment file.
        environment = {
            "PATH": os.environ.get("PATH", os.defpath),
            "COMPOSE_DISABLE_ENV_FILE": "1",
            "POSTGRES_PASSWORD": "placeholder",
            "DATABASE_URL": "postgresql+psycopg://outbound:placeholder@postgres:5432/outbound",
            "ADMIN_API_KEY": "placeholder",
            "TWILIO_ACCOUNT_SID": "placeholder",
            "TWILIO_AUTH_TOKEN": "placeholder",
            "TWILIO_PHONE_NUMBER": "+15555550100",
            "PUBLIC_BASE_URL": "https://example.invalid",
            "OPENAI_API_KEY": "placeholder",
        }
        result = subprocess.run(
            ["docker", "compose", "--env-file", os.devnull, "--file", str(root / "docker-compose.yml"),
             "config", "--format", "json"],
            cwd=root, env=environment, capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            raise AssertionError(f"Compose config exited {result.returncode}: {result.stderr}")
        cls.services = json.loads(result.stdout)["services"]

    def test_backend_publishes_host_8001_to_internal_8000_only_on_localhost(self):
        ports = self.services["backend"]["ports"]
        self.assertEqual(len(ports), 1)
        self.assertEqual((ports[0]["host_ip"], str(ports[0]["published"]), ports[0]["target"], ports[0]["protocol"]),
                         ("127.0.0.1", "8001", 8000, "tcp"))

    def test_frontend_binding_and_internal_backend_url_are_preserved(self):
        frontend = self.services["frontend"]
        self.assertEqual(frontend["environment"]["BACKEND_URL"], "http://backend:8000")
        self.assertEqual(len(frontend["ports"]), 1)
        port = frontend["ports"][0]
        self.assertEqual((port["host_ip"], str(port["published"]), port["target"]),
                         ("127.0.0.1", "8501", 8501))

    def test_readiness_and_container_health_ports_are_preserved(self):
        backend, frontend = self.services["backend"], self.services["frontend"]
        self.assertEqual(backend["depends_on"]["postgres"]["condition"], "service_healthy")
        self.assertEqual(frontend["depends_on"]["backend"]["condition"], "service_healthy")
        self.assertIn("http://localhost:8000/health", " ".join(backend["healthcheck"]["test"]))
        self.assertIn("http://localhost:8501/_stcore/health", " ".join(frontend["healthcheck"]["test"]))
        self.assertEqual(self.services["postgres"]["healthcheck"]["test"],
                         ["CMD-SHELL", "pg_isready -U outbound -d outbound"])

    def test_database_has_no_public_port(self):
        self.assertFalse(self.services["postgres"].get("ports"))

    def test_optional_pricing_is_backend_only_and_defaults_empty(self):
        self.assertEqual(self.services["backend"]["environment"]["OBSERVABILITY_PRICING_JSON"], "")
        self.assertNotIn("OBSERVABILITY_PRICING_JSON", self.services["frontend"]["environment"])
        self.assertNotIn("OBSERVABILITY_PRICING_JSON", self.services["postgres"]["environment"])
        source = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
        self.assertIn("OBSERVABILITY_PRICING_JSON: ${OBSERVABILITY_PRICING_JSON:-}", source)


if __name__ == "__main__":
    unittest.main()
