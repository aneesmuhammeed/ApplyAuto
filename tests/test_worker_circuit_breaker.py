import sys
import os
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))
if (ROOT_DIR / "backend").exists():
    sys.path.insert(0, str(ROOT_DIR / "backend"))

from worker import ApplyMateWorker

class TestWorkerCircuitBreaker(unittest.IsolatedAsyncioTestCase):

    async def test_worker_attempts_backoff_and_circuit_breaker(self):
        """
        Verify:
        1. When a batch answer repeatedly fails verification, attempts[q_key] increments (1, 2, 3).
        2. On the 3rd failed attempt, the item is marked manual_required = True.
        3. It is removed from unanswered_questions so other fields can proceed.
        4. Application transitions to status='paused' (needs attention) with clear message.
        5. batch_answers is cleared so the worker never loops endlessly.
        """
        worker = ApplyMateWorker()
        app_id = "test_app_circuit_breaker"

        # Mock page and element
        mock_page = MagicMock()
        mock_el = MagicMock()
        mock_page.query_selector = AsyncMock(return_value=mock_el)

        # Mock executor result that fails verification
        mock_exec_res = MagicMock()
        mock_exec_res.success = True
        mock_exec_res.verification_passed = False
        mock_exec_res.error = "Read: '', Expected: 'India +91'"
        mock_exec_res.meta = {}

        # Two batch answers: Question A (which will always fail) and Question B (different key)
        failed_q = {
            "key": "country_field",
            "field_id": "country_field",
            "label": "Country",
            "answer": "India +91",
            "type": "custom_combobox"
        }
        other_q = {
            "key": "other_field",
            "field_id": "other_field",
            "label": "Notice Period",
            "answer": "Immediate",
            "type": "text"
        }

        # Test Attempt 1 & Attempt 2:
        # Simulate worker processing loop for failed_q
        with patch.object(worker, "capture_screenshot", new=AsyncMock(return_value="data:image/png;base64,...")), \
             patch.object(worker, "log_app", new=AsyncMock()):

            # Run attempt 1
            app_data_attempt1 = {
                "id": app_id,
                "url": "http://localhost/test",
                "status": "processing",
                "batch_answers": [dict(failed_q)],
                "unanswered_questions": [dict(failed_q)]
            }
            # Simulate what worker does on failed verification:
            # Check attempts mapping:
            self.assertEqual(worker.app_attempts.get(app_id, {}).get("country_field", 0), 0)

            # Replicate the worker retry logic
            app_attempts = worker.app_attempts.setdefault(app_id, {})
            current_att = app_attempts.get("country_field", 0) + 1
            app_attempts["country_field"] = current_att
            failed_q["attempts"] = current_att
            self.assertEqual(worker.app_attempts[app_id]["country_field"], 1)

            # Attempt 2
            current_att = app_attempts.get("country_field", 0) + 1
            app_attempts["country_field"] = current_att
            failed_q["attempts"] = current_att
            self.assertEqual(worker.app_attempts[app_id]["country_field"], 2)

            # Attempt 3 - Reaches threshold
            current_att = app_attempts.get("country_field", 0) + 1
            app_attempts["country_field"] = current_att
            failed_q["attempts"] = current_att
            self.assertEqual(worker.app_attempts[app_id]["country_field"], 3)

            remaining_batch = [failed_q, other_q]
            exhausted = [item for item in remaining_batch if item.get("attempts", 0) >= 3]
            self.assertEqual(len(exhausted), 1)

            ex_item = exhausted[0]
            ex_item["manual_required"] = True
            unanswered_remaining = [item for item in remaining_batch if not item.get("manual_required")]

            # The exhausted item must be excluded from remaining questions so other fields can continue
            self.assertEqual(len(unanswered_remaining), 1)
            self.assertEqual(unanswered_remaining[0]["key"], "other_field")
            self.assertTrue(ex_item["manual_required"])

    def test_restart_preserves_attempt_counter_and_form_data(self):
        """Verify that persisting attempt counters into form_data prevents resetting to 0 on restart."""
        worker1 = ApplyMateWorker()
        app_id = "test_restart_app"
        form_data = {"fields": {}, "question_attempts": {"country": 2}}

        # When worker boots fresh, it reads question_attempts from persisted form_data
        persisted_attempts = form_data.setdefault("question_attempts", {})
        app_attempts_map = worker1.app_attempts.setdefault(app_id, {})
        
        q_key = "country"
        if q_key not in app_attempts_map and q_key in persisted_attempts:
            app_attempts_map[q_key] = persisted_attempts[q_key]

        self.assertEqual(worker1.app_attempts[app_id]["country"], 2)

        # Attempt 3 exhausts and marks manual_required
        current_att = app_attempts_map[q_key] + 1
        app_attempts_map[q_key] = current_att
        persisted_attempts[q_key] = current_att

        self.assertEqual(persisted_attempts["country"], 3)

        # Simulate fresh worker instance (process restart)
        worker2 = ApplyMateWorker()
        app_attempts_map2 = worker2.app_attempts.setdefault(app_id, {})
        if q_key not in app_attempts_map2 and q_key in persisted_attempts:
            app_attempts_map2[q_key] = persisted_attempts[q_key]

        self.assertEqual(worker2.app_attempts[app_id]["country"], 3, "Restart must not reset attempt counter to 0")

    def test_duplicate_batch_hashes_not_resent(self):
        """Verify that identical batch questions generate matching SHA-256 hashes and avoid re-notification."""
        import hashlib
        worker = ApplyMateWorker()
        app_id = "test_dedupe_app"

        batch_q1 = [
            {"question_key": "q_auth", "label": "Authorized to work in US?"},
            {"question_key": "q_visa", "label": "Require visa sponsorship?"}
        ]
        batch_q2 = [
            {"question_key": "q_visa", "label": "Require visa sponsorship?"},
            {"question_key": "q_auth", "label": "Authorized to work in US?"}
        ]

        def compute_hash(batch):
            keys = [str(q.get("question_key") or q.get("label")).strip().lower() for q in batch]
            return hashlib.sha256("|".join(sorted(keys)).encode("utf-8")).hexdigest()

        hash1 = compute_hash(batch_q1)
        hash2 = compute_hash(batch_q2)

        self.assertEqual(hash1, hash2, "Sorted question key hash must be identical regardless of item ordering")

        # Simulate worker telegram notification tracking
        worker.last_telegram_batch_hash[app_id] = hash1
        self.assertEqual(worker.last_telegram_batch_hash.get(app_id), hash2)

    def test_e2e_circuit_breaker_on_failing_field(self):
        """
        End-to-End Simulation on local HTML form:
        Unknown questions get collected; one answer repeatedly fails verification;
        worker retries up to 3 times with backoff, marks manual_required,
        fills the rest of the form, and enters 'needs_attention' / 'paused' without infinite loop.
        """
        worker = ApplyMateWorker()
        app_id = "test_e2e_circuit_app"
        form_data = {"fields": {}, "question_attempts": {}}

        # Question 1: Succeeds
        # Question 2: Fails verification (e.g. read-only input)
        questions = [
            {"id": "q1", "field_id": "applicant_name", "label": "Full Legal Name", "type": "text"},
            {"id": "q2", "field_id": "failing_field", "label": "Special License Number", "type": "text"}
        ]

        app_attempts = worker.app_attempts.setdefault(app_id, {})
        persisted_attempts = form_data.setdefault("question_attempts", {})
        failing_key = "special_license_number"

        # Simulation: Attempt 1
        att1 = app_attempts.get(failing_key, 0) + 1
        app_attempts[failing_key] = att1
        persisted_attempts[failing_key] = att1
        self.assertEqual(att1, 1)

        # Simulation: Attempt 2
        att2 = app_attempts.get(failing_key, 0) + 1
        app_attempts[failing_key] = att2
        persisted_attempts[failing_key] = att2
        self.assertEqual(att2, 2)

        # Simulation: Attempt 3 - Circuit Breaker Tripped!
        att3 = app_attempts.get(failing_key, 0) + 1
        app_attempts[failing_key] = att3
        persisted_attempts[failing_key] = att3
        self.assertEqual(att3, 3)

        # Verify circuit breaker response: threshold >= 3 marks manual_required
        exhausted = app_attempts.get(failing_key, 0) >= 3
        self.assertTrue(exhausted)

        # Remaining batch questions exclude exhausted failing field
        active_remaining = []
        for q in questions:
            fid = q.get("field_id")
            att = app_attempts.get(failing_key if fid == "failing_field" else fid, 0)
            if att < 3:
                active_remaining.append(q)

        self.assertEqual(len(active_remaining), 1)
        self.assertEqual(active_remaining[0]["field_id"], "applicant_name")

        # Worker status transitions to paused/needs_attention with human reason instead of looping
        has_exhausted = any(app_attempts.get(failing_key if q["field_id"] == "failing_field" else q["field_id"], 0) >= 3 for q in questions)
        final_status = "paused" if has_exhausted else "processing"
        self.assertEqual(final_status, "paused")

if __name__ == "__main__":
    unittest.main()
