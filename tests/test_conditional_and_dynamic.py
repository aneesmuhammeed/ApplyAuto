import os
import sys
import unittest
from pathlib import Path
from playwright.async_api import async_playwright

# Support running both from project root and from backend directory
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))
if (ROOT_DIR / "backend").exists():
    sys.path.insert(0, str(ROOT_DIR / "backend"))
import database
from database import init_db
from widget_executors import widget_registry, WidgetType
from form_filler import fill_form_on_page

FIXTURES_DIR = Path(__file__).parent / "fixtures"

def get_fixture_url(filename: str) -> str:
    path = (FIXTURES_DIR / filename).resolve()
    return f"file:///{str(path).replace('\\', '/')}"

class TestConditionalAndDynamic(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        init_db()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context()

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    async def test_conditional_question_reveal_and_rescan(self):
        """
        Verify conditional questions:
        1. Initial scan finds visa sponsorship question; dependent field is hidden.
        2. Answering 'Yes' reveals the dependent required field in the live DOM.
        3. The filler re-scans the page and discovers/asks for the newly revealed question.
        4. When answered, the re-scan pass fills and verifies it in the DOM.
        """
        page = await self.context.new_page()
        await page.goto(get_fixture_url("conditional_question.html"))

        # Verify initial state: conditional field is hidden
        cond_field = page.locator("#current_visa_status")
        self.assertFalse(await cond_field.is_visible())

        # Step 1: Initial form fill pass
        res_pass1 = await fill_form_on_page(
            page=page,
            profile={"full_name": "Test Candidate"},
            saved_answers=[]
        )

        # Assert initial scan found the visa sponsorship question and stopped
        self.assertTrue(res_pass1["needs_input"])
        labels_pass1 = [q["label"] for q in res_pass1["unanswered_questions"]]
        self.assertIn("Will you now or in the future require visa sponsorship?", labels_pass1)
        self.assertNotIn("Please specify your current visa category and expiry", labels_pass1)

        # Step 2: User answers 'Yes' to visa sponsorship via NativeSelectWidgetExecutor
        select_el = await page.wait_for_selector("#visa_sponsorship")
        select_exec = widget_registry.get_executor(WidgetType.NATIVE_SELECT)
        res_select = await select_exec.execute(page, select_el, "Yes")
        self.assertTrue(res_select.success)

        # Dependent field is now visible in the DOM!
        self.assertTrue(await cond_field.is_visible())

        # Step 3: Re-scan pass without answer for newly revealed field -> filler collects it into batch!
        res_pass2 = await fill_form_on_page(
            page=page,
            profile={"full_name": "Test Candidate"},
            saved_answers=[],
            existing_form_data=res_pass1["form_data"]
        )

        self.assertTrue(res_pass2["needs_input"])
        labels_pass2 = [q["label"] for q in res_pass2["unanswered_questions"]]
        self.assertIn("Please specify your current visa category and expiry", labels_pass2)

        # Step 4: When candidate provides answer for the revealed field, re-scan fills and verifies it
        saved_visa_answer = [{
            "id": "visa_1",
            "question_pattern": "Please specify your current visa category and expiry",
            "normalized_text": "please specify your current visa category and expiry",
            "field_type": "text",
            "answer": "H-1B, valid until December 2027",
            "scope": "global",
            "company_name": ""
        }]

        res_pass3 = await fill_form_on_page(
            page=page,
            profile={"full_name": "Test Candidate"},
            saved_answers=saved_visa_answer,
            existing_form_data=res_pass2["form_data"]
        )

        # Verify newly revealed field was filled in live DOM
        dom_val = await cond_field.input_value()
        self.assertEqual(dom_val, "H-1B, valid until December 2027")

        filled_labels = [f["field"] for f in res_pass3["fields_filled"]]
        self.assertIn("Please specify your current visa category and expiry", filled_labels)

if __name__ == "__main__":
    unittest.main()
