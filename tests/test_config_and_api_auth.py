import os
import sys
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))
if (ROOT_DIR / "backend").exists():
    sys.path.insert(0, str(ROOT_DIR / "backend"))

from config import Settings, safe_load_dotenv, settings
from main import app, is_safe_job_url

class TestConfigAndAPIAuth(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app)
        self.token = settings.API_TOKEN

    def test_config_parsing_types_and_defaults(self):
        """Test type coercion and fallback for settings."""
        # Non-headless test
        with unittest.mock.patch.dict(os.environ, {
            "HEADLESS_BROWSER": "False",
            "POLL_INTERVAL": "5",
            "LLM_CONFIDENCE_THRESHOLD": "0.75"
        }):
            cfg = Settings()
            self.assertFalse(cfg.HEADLESS_BROWSER)
            self.assertEqual(cfg.POLL_INTERVAL, 5)
            self.assertEqual(cfg.LLM_CONFIDENCE_THRESHOLD, 0.75)

        # Truthy headless variations
        with unittest.mock.patch.dict(os.environ, {
            "HEADLESS_BROWSER": "True",
            "POLL_INTERVAL": "not_an_int",
            "LLM_CONFIDENCE_THRESHOLD": "not_a_float"
        }):
            cfg = Settings()
            self.assertTrue(cfg.HEADLESS_BROWSER)
            self.assertEqual(cfg.POLL_INTERVAL, 2)
            self.assertEqual(cfg.LLM_CONFIDENCE_THRESHOLD, 0.8)

    def test_safe_job_url_validation(self):
        """Disallow file://, local addresses, or invalid schemes."""
        self.assertTrue(is_safe_job_url("https://jobs.lever.co/company/job123"))
        self.assertTrue(is_safe_job_url("http://boards.greenhouse.io/corp/apply"))
        
        # Block SSRF & internal schemes
        self.assertFalse(is_safe_job_url("file:///etc/passwd"))
        self.assertFalse(is_safe_job_url("http://localhost:8000/api"))
        self.assertFalse(is_safe_job_url("http://127.0.0.1:1234/status"))
        self.assertFalse(is_safe_job_url("http://169.254.169.254/latest/meta-data"))
        self.assertFalse(is_safe_job_url("http://192.168.1.1/admin"))
        self.assertFalse(is_safe_job_url("http://10.0.0.1/internal"))
        self.assertFalse(is_safe_job_url("javascript:alert(1)"))

    def test_api_auth_required_on_routes(self):
        """Ensure all protected routes reject requests without valid token."""
        protected_endpoints = [
            ("GET", "/api/applications"),
            ("GET", "/api/profile"),
            ("GET", "/api/saved-answers"),
            ("GET", "/api/worker/status"),
            ("POST", "/api/resume/upload"),
        ]

        for method, endpoint in protected_endpoints:
            if method == "GET":
                res = self.client.get(endpoint)
            else:
                res = self.client.post(endpoint)
            self.assertEqual(
                res.status_code, 401,
                f"Endpoint {endpoint} should require authentication, got {res.status_code}"
            )

    def test_api_auth_success_with_x_api_token(self):
        """Verify endpoints succeed with X-API-Token header."""
        res = self.client.get("/api/applications", headers={"X-API-Token": self.token})
        self.assertEqual(res.status_code, 200)

    def test_api_auth_success_with_bearer_token(self):
        """Verify endpoints succeed with Authorization: Bearer token."""
        res = self.client.get("/api/applications", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(res.status_code, 200)

    def test_token_bootstrap_origin_protection(self):
        """Token bootstrap must only succeed from authorized dashboard origins."""
        # Unauthorized origin
        res = self.client.get("/api/auth/token", headers={"Origin": "http://evil-attacker.com"})
        self.assertEqual(res.status_code, 403)

        # Authorized origin
        res = self.client.get("/api/auth/token", headers={"Origin": "http://localhost:3000"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json().get("token"), self.token)

    def test_resume_upload_validations(self):
        """Resume upload must reject legacy .doc, spoofed headers, and invalid types."""
        # 1. Reject .doc
        res = self.client.post(
            "/api/resume/upload",
            headers={"X-API-Token": self.token},
            files={"file": ("resume.doc", b"Old Word Doc", "application/msword")}
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Legacy .doc format is rejected", res.json().get("detail", ""))

        # 2. Reject fake PDF (header missing %PDF-)
        res = self.client.post(
            "/api/resume/upload",
            headers={"X-API-Token": self.token},
            files={"file": ("resume.pdf", b"Not a real PDF", "application/pdf")}
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("File header does not match valid PDF signature", res.json().get("detail", ""))

if __name__ == "__main__":
    unittest.main()
