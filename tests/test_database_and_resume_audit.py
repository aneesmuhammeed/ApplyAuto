import os
import sys
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import threading
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))
if (ROOT_DIR / "backend").exists():
    sys.path.insert(0, str(ROOT_DIR / "backend"))

from database import (
    init_db, get_db_connection, create_application, update_application,
    get_application, acquire_queued_application, generate_question_key,
    run_migrations, CURRENT_SCHEMA_VERSION
)
from resume_parser import parse_resume

class TestDatabaseAndResumeAudit(unittest.TestCase):

    def test_schema_migration_from_v0_to_current(self):
        """Test migrating an older empty/v0 SQLite database up to CURRENT_SCHEMA_VERSION."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            temp_db_path = f.name

        conn = None
        try:
            conn = sqlite3.connect(temp_db_path)
            # Simulate initial legacy table without new columns
            conn.execute("""
            CREATE TABLE applications (
                id TEXT PRIMARY KEY,
                job_url TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at TEXT
            )
            """)
            conn.execute("PRAGMA user_version = 0")
            conn.commit()

            # Run migration mechanism
            run_migrations(conn)

            # Check new user_version
            v = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(v, CURRENT_SCHEMA_VERSION)

            # Check migrated columns
            cols = [c[1] for c in conn.execute("PRAGMA table_info(applications)").fetchall()]
            self.assertIn("verification_failed", cols)
            self.assertIn("batch_answers", cols)
            self.assertIn("unanswered_questions", cols)
        finally:
            if conn:
                conn.close()
            if os.path.exists(temp_db_path):
                try:
                    os.remove(temp_db_path)
                except Exception:
                    pass

    def test_concurrent_connections_no_database_locked(self):
        """Simulate concurrent threads writing and updating applications."""
        app_id = create_application("https://jobs.lever.co/test/role", "ConcurrentCorp")
        errors = []

        def worker_writer(thread_id: int):
            try:
                for i in range(10):
                    update_application(app_id, {
                        "stage_message": f"Thread {thread_id} step {i}",
                        "form_data": {"fields": {f"field_{thread_id}_{i}": "val"}}
                    })
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker_writer, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Encountered concurrency lock errors: {errors}")
        app_data = get_application(app_id)
        self.assertIsNotNone(app_data)
        self.assertIn("fields", app_data["form_data"])

    def test_corrupted_form_data_snapshot_handling(self):
        """Corrupted JSON in form_data must not crash application retrieval or parsing."""
        bad_id = f"bad_json_app_{os.getpid()}"
        conn = get_db_connection()
        conn.execute("DELETE FROM applications WHERE id = ?", (bad_id,))
        conn.execute("""
        INSERT INTO applications (id, job_url, status, form_data, created_at)
        VALUES (?, 'https://lever.co/job', 'queued', '{corrupted_json: True, invalid', '2026-11-15 00:00:00')
        """, (bad_id,))
        conn.commit()
        conn.close()

        try:
            # Should safely fall back without throwing an exception
            app = get_application(bad_id)
            self.assertIsNotNone(app)
            # form_data raw corrupted string remains safely wrapped without crashing
            self.assertTrue(isinstance(app.get("form_data"), (dict, str)))
        finally:
            conn = get_db_connection()
            conn.execute("DELETE FROM applications WHERE id = ?", (bad_id,))
            conn.commit()
            conn.close()

    def test_deterministic_question_key_generation(self):
        """Verify unicode normalization and company-scoped key generation."""
        k1 = generate_question_key("Are you legally authorized to work in India?", "select")
        self.assertEqual(k1, "work_authorization_select")

        # Company scope
        k2 = generate_question_key("Willing to relocate?", "text", company_name="Acme Corp", scope="this company")
        self.assertTrue(k2.startswith("acme_corp:"))
        self.assertIn("relocation_willingness", k2)

        # Unicode / special punctuation characters
        k3 = generate_question_key("Notice Period (in Days) * ? :", "number")
        self.assertEqual(k3, "notice_period_number")

    def test_resume_parser_contact_and_location_extraction(self):
        """Test resume parsing on structured text."""
        sample_docx_text = """
        Johnathan Doe
        john.doe@example.com | +91 9876543210
        San Francisco, California, United States
        https://linkedin.com/in/john-doe
        https://github.com/johndoe
        
        SKILLS
        Python, FastAPI, TypeScript, React.js
        
        EDUCATION
        University of California, Berkeley
        B.S. in Computer Science | 2024
        """
        with patch("resume_parser.extract_text_and_links_from_pdf", return_value=(sample_docx_text, [])):
            res = parse_resume("dummy.pdf")
            self.assertEqual(res["full_name"], "Johnathan Doe")
            self.assertEqual(res["email"], "john.doe@example.com")
            self.assertIn("9876543210", res["phone"])
            self.assertEqual(res["location"], "San Francisco, California, United States")
            self.assertEqual(res["linkedin_url"], "https://linkedin.com/in/john-doe")
            self.assertEqual(res["github_url"], "https://github.com/johndoe")

if __name__ == "__main__":
    unittest.main()
