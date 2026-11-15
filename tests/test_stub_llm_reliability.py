import os
import sys
import json
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from playwright.async_api import async_playwright

# Support running both from project root and from backend directory
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))
if (ROOT_DIR / "backend").exists():
    sys.path.insert(0, str(ROOT_DIR / "backend"))
import database
from database import init_db
import ai_client
from ai_client import ai_generate_tailored_answer, clear_llm_cache
from form_filler import fill_form_on_page

FIXTURES_DIR = Path(__file__).parent / "fixtures"

def get_fixture_url(filename: str) -> str:
    path = (FIXTURES_DIR / filename).resolve()
    return f"file:///{str(path).replace('\\', '/')}"

class TestStubLLMReliability(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        init_db()
        clear_llm_cache()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context()

    async def asyncTearDown(self):
        clear_llm_cache()
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    # =========================================================================
    # 1. Stub LLM returning Invalid JSON
    # =========================================================================
    async def test_stub_llm_invalid_json_asks_user(self):
        """Assert system asks user instead of guessing when LLM returns invalid JSON."""
        options = ["First Choice", "Second Choice", "Third Choice"]

        # Stub LLM returns non-JSON conversational text
        stub_invalid_json = "I believe the candidate matches the first choice very well."

        with patch("ai_client.call_qwen", new_callable=AsyncMock) as mock_qwen:
            mock_qwen.return_value = stub_invalid_json

            # Option choice must return None, NEVER options[0]!
            decision = await ai_generate_tailored_answer(
                question_text="Select preferred tier",
                candidate_profile={},
                options=options
            )
            self.assertIsNone(decision, "System must not guess or default to options[0] on invalid JSON")

        # Test on live HTML fixture: verify form filler collects into batch without guessing
        page = await self.context.new_page()
        await page.goto(get_fixture_url("native_select_empty_placeholder.html"))

        with patch("ai_client.call_qwen", new_callable=AsyncMock) as mock_qwen:
            mock_qwen.return_value = stub_invalid_json

            res = await fill_form_on_page(
                page=page,
                profile={},
                saved_answers=[]
            )

            # Field must NOT be filled in DOM
            dom_val = await page.locator("#relocation-select").input_value()
            self.assertEqual(dom_val, "")

            # Question must be collected in unanswered batch with empty suggestion
            self.assertTrue(res["needs_input"])
            self.assertEqual(len(res["unanswered_questions"]), 1)
            q = res["unanswered_questions"][0]
            self.assertEqual(q["ai_suggestion"], "")

    # =========================================================================
    # 2. Stub LLM returning Wrong / Out-of-Range Option
    # =========================================================================
    async def test_stub_llm_wrong_or_out_of_range_option_asks_user(self):
        """Assert system asks user when LLM returns out-of-range or negative option index."""
        options = ["Alpha", "Beta", "Gamma"]

        # Scenario A: Negative index (-1 indicates no match)
        stub_neg = json.dumps({"option_index": -1, "confidence": 0.0, "reason": "No candidate facts match"})
        with patch("ai_client.call_qwen", new_callable=AsyncMock) as mock_qwen:
            mock_qwen.return_value = stub_neg
            res_neg = await ai_generate_tailored_answer(
                question_text="Choose project",
                candidate_profile={},
                options=options
            )
            self.assertIsNone(res_neg, "Negative index must return None ('ask user')")

        clear_llm_cache()

        # Scenario B: Out-of-range overflow index (e.g. index 50 when only 3 options exist)
        stub_overflow = json.dumps({"option_index": 50, "confidence": 0.99, "reason": "Hallucinated index"})
        with patch("ai_client.call_qwen", new_callable=AsyncMock) as mock_qwen:
            mock_qwen.return_value = stub_overflow
            res_overflow = await ai_generate_tailored_answer(
                question_text="Choose project",
                candidate_profile={},
                options=options
            )
            self.assertIsNone(res_overflow, "Overflow index must return None ('ask user')")

    # =========================================================================
    # 3. Stub LLM Timeout / Network Failure
    # =========================================================================
    async def test_stub_llm_timeout_asks_user(self):
        """Assert system asks user cleanly when local LLM server times out or fails."""
        options = ["Yes", "No"]

        # Stub LLM returns None (simulating timeout or connection error)
        with patch("ai_client.call_qwen", new_callable=AsyncMock) as mock_qwen:
            mock_qwen.return_value = None

            decision = await ai_generate_tailored_answer(
                question_text="Requires security clearance?",
                candidate_profile={},
                options=options
            )
            self.assertIsNone(decision, "LLM timeout must cleanly return None ('ask user')")

        # Test on live HTML fixture with radio groups
        page = await self.context.new_page()
        await page.goto(get_fixture_url("radio_groups_name_and_noname.html"))

        with patch("ai_client.call_qwen", new_callable=AsyncMock) as mock_qwen:
            mock_qwen.return_value = None

            res = await fill_form_on_page(
                page=page,
                profile={},
                saved_answers=[]
            )

            # Neither radio should be checked by guessing
            self.assertFalse(await page.locator("#work_auth_yes").is_checked())
            self.assertFalse(await page.locator("#work_auth_no").is_checked())
            self.assertTrue(res["needs_input"])

    # =========================================================================
    # 4. Never Auto-Fill Free-Text: Must Ask User
    # =========================================================================
    async def test_stub_llm_never_autofills_free_text(self):
        """Assert free-text generated by LLM is never auto-filled into the DOM."""
        page = await self.context.new_page()
        await page.goto(get_fixture_url("conditional_question.html"))

        # Reveal the dependent text input
        await page.locator("#visa_sponsorship").select_option("Yes")
        self.assertTrue(await page.locator("#current_visa_status").is_visible())

        stub_free_text = "I currently hold an H-1B specialty occupation visa valid through 2027."
        with patch("ai_client.call_qwen", new_callable=AsyncMock) as mock_qwen:
            mock_qwen.return_value = stub_free_text

            res = await fill_form_on_page(
                page=page,
                profile={"full_name": "Test Candidate"},
                saved_answers=[]
            )

            # 1. DOM text field must NOT be filled automatically!
            dom_val = await page.locator("#current_visa_status").input_value()
            self.assertEqual(dom_val, "", "Free-text must NEVER be auto-filled into the DOM without user approval")

            # 2. Must be in unanswered questions with suggestion
            self.assertTrue(res["needs_input"])
            unanswered_labels = [q["label"] for q in res["unanswered_questions"]]
            self.assertIn("Please specify your current visa category and expiry", unanswered_labels)

            q_item = next(q for q in res["unanswered_questions"] if q["label"] == "Please specify your current visa category and expiry")
            self.assertEqual(q_item["ai_suggestion"], stub_free_text)

    # =========================================================================
    # 5. Think Tag Stripping and Option Index Parsing
    # =========================================================================
    async def test_think_tag_stripping_and_index_parsing(self):
        """Verify Qwen <think>...</think> tokens are stripped and option index JSON is properly parsed."""
        options = ["Software Engineering", "Product Design", "Marketing"]

        # Scenario 1: Model outputs verbose <think>...</think> block before valid JSON with index
        stub_with_think = """<think>
The user is asking for the role. The candidate profile lists React and FastAPI.
Option 0 is Software Engineering.
</think>
{"index": 0, "confidence": 0.95}"""

        with patch("ai_client.call_qwen", new_callable=AsyncMock) as mock_qwen:
            mock_qwen.return_value = stub_with_think
            decision = await ai_generate_tailored_answer(
                question_text="Preferred Department",
                candidate_profile={"skills": ["React", "FastAPI"]},
                options=options
            )
            self.assertEqual(decision, "Software Engineering")

        clear_llm_cache()

        # Scenario 2: Legacy option_index format with confidence
        stub_legacy = """{"option_index": 1, "confidence": 0.88}"""
        with patch("ai_client.call_qwen", new_callable=AsyncMock) as mock_qwen:
            mock_qwen.return_value = stub_legacy
            decision = await ai_generate_tailored_answer(
                question_text="Preferred Department",
                candidate_profile={},
                options=options
            )
            self.assertEqual(decision, "Product Design")

if __name__ == "__main__":
    unittest.main()
