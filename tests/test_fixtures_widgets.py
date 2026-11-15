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

    # =========================================================================
    # 9. Dial Code Combobox & Country Equivalence Verification
    # =========================================================================
    async def test_dial_code_matching_and_verification(self):
        from widget_executors import match_option_candidate

        # 1. Pure matching logic
        # A. Target "India +91" vs Candidate "+91" (previously scored 0.57 < 0.75)
        m1 = match_option_candidate("India +91", [("opt1", "+91")])
        self.assertIsNotNone(m1)
        self.assertEqual(m1[1], "+91")

        # B. Target "+91" vs Candidate "India +91"
        m2 = match_option_candidate("+91", [("opt2", "India +91")])
        self.assertIsNotNone(m2)
        self.assertEqual(m2[1], "India +91")

        # C. Target "India +91" vs Candidate "India"
        m3 = match_option_candidate("India +91", [("opt3", "India")])
        self.assertIsNotNone(m3)
        self.assertEqual(m3[1], "India")

        # D. Disambiguate shared dial code (+1)
        m4 = match_option_candidate("United States +1", [("ca", "Canada (+1)"), ("us", "United States (+1)")])
        self.assertIsNotNone(m4)
        self.assertEqual(m4[0], "us")

        # 2. Live DOM verification
        page = await self.context.new_page()
        await page.goto(get_fixture_url("dial_code_combobox.html"))

        cb_exec = widget_registry.get_executor(WidgetType.CUSTOM_COMBOBOX)

        # React-Select style dial code
        react_el = await page.wait_for_selector("#country-code")
        v_ok, read_val = await cb_exec.verify(page, react_el, "India +91")
        self.assertTrue(v_ok)
        self.assertIn("India", read_val)

        # intl-tel-input style dial code
        iti_flag = await page.wait_for_selector(".iti__selected-flag")
        v_ok_iti, read_val_iti = await cb_exec.verify(page, iti_flag, "India +91")
        self.assertTrue(v_ok_iti)
        self.assertIn("+91", read_val_iti)

        # Hidden input backed picker
        picker_btn = await page.wait_for_selector("#hidden-backed-wrapper button")
        v_ok_p, read_val_p = await cb_exec.verify(page, picker_btn, "India +91")
        self.assertTrue(v_ok_p)
        self.assertEqual(read_val_p, "India")

    # =========================================================================
    # 10. Edge Cases: Display Matching, Hidden Input Combobox, and Strict Type Detection
    # =========================================================================
    async def test_edge_cases_matching_and_type_detection(self):
        from widget_executors import display_matches, detect_widget_type

        # 1. Test display_matches unit logic
        self.assertTrue(display_matches("India +91", "+91"))
        self.assertTrue(display_matches("India +91", "India"))
        self.assertTrue(display_matches("India +91", "India +91"))
        self.assertTrue(display_matches("+91", "India +91"))
        self.assertFalse(display_matches("India +91", "+1"))
        self.assertFalse(display_matches("India +91", "United States"))

        page = await self.context.new_page()
        await page.goto(get_fixture_url("edge_cases_widgets.html"))

        cb_exec = widget_registry.get_executor(WidgetType.CUSTOM_COMBOBOX)

        # 2. React-select dial code showing only "+91"
        dc_el = await page.wait_for_selector("#dial-code-input")
        ok_dc, read_dc = await cb_exec.verify(page, dc_el, "India +91")
        self.assertTrue(ok_dc, f"Should verify successfully against displayed '+91': read '{read_dc}'")
        self.assertEqual(read_dc, "+91")

        # 3. Combobox whose value lives only in a hidden input
        hidden_box = await page.wait_for_selector("#select2-country-box")
        # Set hidden input value to 'India'
        await page.evaluate("() => document.getElementById('hidden-country-val').value = 'India'")
        ok_h, read_h = await cb_exec.verify(page, hidden_box, "India")
        self.assertTrue(ok_h, f"Should read from hidden input: read '{read_h}'")
        self.assertEqual(read_h, "India")

        # 4. Native select inside div with class "select-wrapper" (Must NOT detect as CUSTOM_COMBOBOX)
        native_sel = await page.wait_for_selector("#native-work-auth")
        detected_sel_type = await detect_widget_type(page, native_sel)
        self.assertEqual(detected_sel_type, WidgetType.NATIVE_SELECT, "Native select in .select-wrapper must detect as NATIVE_SELECT")

        # 5. Text input inside div with class "selectable" (Must NOT detect as CUSTOM_COMBOBOX)
        text_el = await page.wait_for_selector("#candidate-github")
        detected_text_type = await detect_widget_type(page, text_el)
        self.assertEqual(detected_text_type, WidgetType.TEXT, "Input in .selectable must detect as TEXT")

        # 6. Two comboboxes on one page: listbox scoping test
        p1_input = await page.wait_for_selector("#skill-primary-input")
        res_p1 = await cb_exec.execute(page, p1_input, "Python")
        self.assertTrue(res_p1.success, f"Filling primary combobox failed: {res_p1.error}")

        p2_input = await page.wait_for_selector("#skill-secondary-input")
        res_p2 = await cb_exec.execute(page, p2_input, "TypeScript")
        self.assertTrue(res_p2.success, f"Filling secondary combobox failed: {res_p2.error}")

        # 7. Date field with and without placeholder
        date_exec = widget_registry.get_executor(WidgetType.DATE)
        date_no_ph = await page.wait_for_selector("#date-no-placeholder")
        res_date = await date_exec.execute(page, date_no_ph, "2025-06-15")
        self.assertTrue(res_date.success, f"Filling date without placeholder failed: {res_date.error}")
        self.assertEqual(await date_no_ph.input_value(), "2025-06-15")

        # 8. match_option_candidate boolean leading token test
        from widget_executors import match_option_candidate
        opts = [(1, "Yes, absolutely"), (2, "No, none of the above"), (3, "Not applicable")]
        m_no = match_option_candidate("no", opts)
        self.assertIsNotNone(m_no)
        self.assertEqual(m_no[0], 2, "Option 'No, none of the above' should match 'no'")
        self.assertEqual(m_no[1], "No, none of the above")

        # 9. form_filler fast_restore exact label and map_intent tests
        from form_filler import map_intent_to_profile, fast_restore_form_data
        oneword = {"full_name": "Cher", "first_name": "Cher"}
        ln = await map_intent_to_profile("last_name", oneword)
        self.assertEqual(ln, "", "One-word names must yield empty last name string")

        # 10. Verify fast_restore_form_data returns RestoreResult dataclass
        dummy_form_data = {
            "fields": {
                "field_1": {
                    "label": "Availability Date",
                    "value": "2025-06-15",
                    "type": "date",
                    "selector": "#date-no-placeholder"
                }
            }
        }
        restore_res = await fast_restore_form_data(page, dummy_form_data)
        self.assertEqual(restore_res.restored_count, 1)
        self.assertTrue(restore_res.all_passed)
        self.assertEqual(len(restore_res.verified_fields), 1)

if __name__ == "__main__":
    unittest.main()
