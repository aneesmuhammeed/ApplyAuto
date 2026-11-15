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
from form_filler import fill_form_on_page, get_form_context

FIXTURES_DIR = Path(__file__).parent / "fixtures"

def get_fixture_url(filename: str) -> str:
    path = (FIXTURES_DIR / filename).resolve()
    return f"file:///{str(path).replace('\\', '/')}"

class TestFixturesWidgets(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        init_db()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context()

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    # =========================================================================
    # 1. Native Select with Empty-Text Placeholder
    # =========================================================================
    async def test_native_select_empty_placeholder(self):
        page = await self.context.new_page()
        await page.goto(get_fixture_url("native_select_empty_placeholder.html"))

        select_el = await page.wait_for_selector("#relocation-select")
        executor = widget_registry.get_executor(WidgetType.NATIVE_SELECT)

        # Select target option
        res = await executor.execute(page, select_el, "Yes, open to relocation")
        self.assertTrue(res.success)
        self.assertTrue(res.verification_passed)
        self.assertEqual(res.verified_value, "Yes, open to relocation")

        # Verify DOM state
        val = await select_el.input_value()
        self.assertEqual(val, "yes")

    # =========================================================================
    # 2. React-Select with Portaled Async Options
    # =========================================================================
    async def test_react_select_portaled_async(self):
        page = await self.context.new_page()
        await page.goto(get_fixture_url("react_select_portaled.html"))

        input_el = await page.wait_for_selector("#university-input")
        executor = widget_registry.get_executor(WidgetType.CUSTOM_COMBOBOX)

        # Execute fill for target university
        target = "Govt. Model Engineering College"
        res = await executor.execute(page, input_el, target)
        self.assertTrue(res.success)
        self.assertTrue(res.verification_passed)

        # Verify selected value in DOM
        val = await input_el.input_value()
        self.assertEqual(val, target)

    # =========================================================================
    # 3. MUI Autocomplete Component
    # =========================================================================
    async def test_mui_autocomplete(self):
        page = await self.context.new_page()
        await page.goto(get_fixture_url("mui_autocomplete.html"))

        input_el = await page.wait_for_selector("#mui-skill-input")
        executor = widget_registry.get_executor(WidgetType.CUSTOM_COMBOBOX)

        target = "Python"
        res = await executor.execute(page, input_el, target)
        self.assertTrue(res.success)
        self.assertTrue(res.verification_passed)

        val = await input_el.input_value()
        self.assertEqual(val, target)

    # =========================================================================
    # 4. Female/Male List Order (Exact-First Matching)
    # =========================================================================
    async def test_female_male_order_exact_matching(self):
        page = await self.context.new_page()
        await page.goto(get_fixture_url("female_male_order.html"))

        select_el = await page.wait_for_selector("#gender")
        executor = widget_registry.get_executor(WidgetType.NATIVE_SELECT)

        # Attempting to select "Male" must NOT match "Female" (which is option 0)
        res = await executor.execute(page, select_el, "Male")
        self.assertTrue(res.success)
        self.assertTrue(res.verification_passed)
        self.assertEqual(res.verified_value, "Male")

        # Verify DOM is strictly "Male", not "Female"
        val = await select_el.input_value()
        self.assertEqual(val, "Male")

    # =========================================================================
    # 5. Radio Groups With and Without Name Attribute
    # =========================================================================
    async def test_radio_groups_with_and_without_name(self):
        page = await self.context.new_page()
        await page.goto(get_fixture_url("radio_groups_name_and_noname.html"))

        executor = widget_registry.get_executor(WidgetType.RADIO)

        # Group 1: with name="work_authorization"
        r1_el = await page.wait_for_selector("#work_auth_yes")
        res1 = await executor.execute(page, r1_el, "Yes")
        self.assertTrue(res1.success)
        self.assertTrue(res1.verification_passed)
        self.assertTrue(await r1_el.is_checked())
        self.assertFalse(await page.locator("#work_auth_no").is_checked())

        # Group 2: without name attribute (grouped by container/legend)
        r2_el = await page.wait_for_selector("#relocate_no")
        res2 = await executor.execute(page, r2_el, "No, remote only")
        self.assertTrue(res2.success)
        self.assertTrue(res2.verification_passed)
        self.assertTrue(await r2_el.is_checked())
        self.assertFalse(await page.locator("#relocate_yes").is_checked())

    # =========================================================================
    # 6. Dependent Country to State Dropdowns
    # =========================================================================
    async def test_dependent_country_to_state_dropdowns(self):
        page = await self.context.new_page()
        await page.goto(get_fixture_url("dependent_country_state.html"))

        country_el = await page.wait_for_selector("#country_select")
        state_el = await page.wait_for_selector("#state_select")
        select_exec = widget_registry.get_executor(WidgetType.NATIVE_SELECT)

        # State is initially disabled
        self.assertTrue(await state_el.is_disabled())

        # Step 1: Fill Country ("India")
        res_country = await select_exec.execute(page, country_el, "India")
        self.assertTrue(res_country.success)
        self.assertEqual(await country_el.input_value(), "India")

        # State should now be enabled and populated
        self.assertFalse(await state_el.is_disabled())

        # Step 2: Fill State ("Kerala")
        res_state = await select_exec.execute(page, state_el, "Kerala")
        self.assertTrue(res_state.success)
        self.assertEqual(await state_el.input_value(), "Kerala")

    # =========================================================================
    # 7. Date Inputs in Four Formats
    # =========================================================================
    async def test_date_inputs_four_formats(self):
        page = await self.context.new_page()
        await page.goto(get_fixture_url("date_inputs_four_formats.html"))

        date_exec = widget_registry.get_executor(WidgetType.DATE)

        # Format 1: Native Date (YYYY-MM-DD)
        el1 = await page.wait_for_selector("#native_date")
        res1 = await date_exec.execute(page, el1, "2024-06-01")
        self.assertTrue(res1.success)
        self.assertEqual(await el1.input_value(), "2024-06-01")

        # Format 2: Masked DD/MM/YYYY
        el2 = await page.wait_for_selector("#date_dmy")
        res2 = await date_exec.execute(page, el2, "2024-06-01")
        self.assertTrue(res2.success)
        self.assertEqual(await el2.input_value(), "01/06/2024")

        # Format 3: Masked MM/DD/YYYY
        el3 = await page.wait_for_selector("#date_mdy")
        res3 = await date_exec.execute(page, el3, "2024-06-01")
        self.assertTrue(res3.success)
        self.assertEqual(await el3.input_value(), "06/01/2024")

        # Format 4: Native Month (YYYY-MM)
        el4 = await page.wait_for_selector("#native_month")
        res4 = await date_exec.execute(page, el4, "2024-06-01")
        self.assertTrue(res4.success)
        self.assertEqual(await el4.input_value(), "2024-06")

    # =========================================================================
    # 8. Form Inside an iFrame Context
    # =========================================================================
    async def test_form_inside_iframe(self):
        page = await self.context.new_page()
        await page.goto(get_fixture_url("form_inside_iframe.html"))

        # Detect the ATS application frame
        ctx = await get_form_context(page)
        self.assertIsNotNone(ctx)

        # Autofill form within detected frame context
        res = await fill_form_on_page(
            page=page,
            profile={
                "full_name": "Muhammed Anees K",
                "email": "muhammedanees.mec@gmail.com",
                "phone": "+91 9876543210"
            },
            saved_answers=[]
        )

        # Verify fields were successfully filled inside the iframe
        frame = page.frame(name="job_application_iframe")
        self.assertIsNotNone(frame)
        val_fname = await frame.locator("#first_name").input_value()
        val_lname = await frame.locator("#last_name").input_value()
        val_email = await frame.locator("#email").input_value()

        self.assertEqual(val_fname, "Muhammed")
        self.assertEqual(val_lname, "Anees K")
        self.assertEqual(val_email, "muhammedanees.mec@gmail.com")

if __name__ == "__main__":
    unittest.main()
