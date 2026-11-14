import os
import time
import base64
import asyncio
from typing import Optional, Dict, Any, List
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from config import settings
from database import (
    get_profile, get_saved_answers, get_applications, get_application,
    update_application, update_worker_heartbeat, create_saved_answer
)
from form_filler import fill_form_on_page, detect_security_checkpoint, get_form_context, set_date_on_element

class ApplyMateWorker:
    def __init__(self):
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.is_running = True
        self.worker_id = "laptop-worker"

    async def init_browser(self):
        if not self.playwright:
            self.playwright = await async_playwright().start()
        if not self.browser:
            self.browser = await self.playwright.chromium.launch(
                headless=settings.HEADLESS_BROWSER,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--no-first-run"
                ]
            )
            user_data_dir = os.path.join(settings.RESUME_STORAGE_DIR, "browser_profile")
            os.makedirs(user_data_dir, exist_ok=True)
            self.context = await self.browser.new_context(viewport={"width": 1280, "height": 800})
            self.page = await self.context.new_page()

    async def close_browser(self):
        if self.context:
            await self.context.close()
            self.context = None
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    async def update_heartbeat(self, status: str = "online", active_app_id: Optional[str] = None):
        try:
            update_worker_heartbeat(status=status, active_app_id=active_app_id)
        except Exception as e:
            print(f"[Heartbeat] Error: {e}")

    async def log_app(self, app_id: str, message: str):
        print(f"[{app_id[:8]}] {message}")
        try:
            app = get_application(app_id)
            logs = app.get("logs") if app and isinstance(app.get("logs"), list) else []
            logs.append({"timestamp": time.time(), "message": message})
            update_application(app_id, {
                "logs": logs,
                "stage_message": message
            })
        except Exception as e:
            print(f"Error logging to SQLite: {e}")

    async def capture_screenshot(self) -> str:
        """Capture page screenshot and return base64 data URI for dashboard review."""
        if not self.page:
            return ""
        try:
            img_bytes = await self.page.screenshot(full_page=False)
            b64 = base64.b64encode(img_bytes).decode("utf-8")
            return f"data:image/png;base64,{b64}"
        except Exception as e:
            print(f"Screenshot error: {e}")
            return ""

    async def process_task(self, app: Dict[str, Any]):
        app_id = app["id"]
        job_url = app["job_url"]
        status = app["status"]
        company_name = app.get("company_name", "")

        await self.update_heartbeat(status="busy", active_app_id=app_id)
        profile = get_profile()
        saved_answers = get_saved_answers()
        resume_path = profile.get("resume_file_path")

        await self.init_browser()

        # Step 1: Newly Queued task
        if status == "queued":
            await self.log_app(app_id, f"Opening application URL: {job_url}")
            update_application(app_id, {"status": "processing"})

            try:
                await self.page.goto(job_url, timeout=45000, wait_until="domcontentloaded")
                await asyncio.sleep(2)
                # Attempt to extract job title/company name from page title if empty
                page_title = await self.page.title()
                if page_title and not app.get("job_title"):
                    update_application(app_id, {"job_title": page_title[:80]})
            except Exception as e:
                await self.log_app(app_id, f"Failed to load job page: {e}")
                update_application(app_id, {
                    "status": "failed",
                    "error_message": f"Navigation failed: {e}"
                })
                return

        # Check if resuming from paused (e.g. user manually solved CAPTCHA or clicked resume)
        if status == "paused":
            await self.log_app(app_id, "Resuming application after manual verification...")
            update_application(app_id, {"status": "processing"})

        # Check if resuming from user input
        if status == "needs_input":
            user_answer = app.get("user_answer")
            unanswered = app.get("unanswered_question")
            if user_answer and unanswered:
                field_id = unanswered.get("field_id")
                await self.log_app(app_id, f"Applying provided answer: '{user_answer}'")
                
                try:
                    ctx = await get_form_context(self.page)
                    # Look for input or React Select container
                    el = None
                    if field_id and " " not in field_id and not field_id.startswith((".", "#", ":")):
                        try:
                            el = await ctx.query_selector(f'#{field_id}')
                        except Exception:
                            el = None
                    if not el:
                        try:
                            el = await ctx.query_selector(f'[name="{field_id}"]')
                        except Exception:
                            el = None
                    if not el and unanswered.get("label"):
                        lbl_text = unanswered.get("label", "").replace('"', '\\"')
                        try:
                            lbl_el = await ctx.query_selector(f'label:has-text("{lbl_text}")')
                            if lbl_el:
                                for_attr = await lbl_el.get_attribute("for")
                                if for_attr:
                                    el = await ctx.query_selector(f'#{for_attr}')
                        except Exception:
                            pass

                    if el:
                        tag = await el.evaluate("e => e.tagName.toLowerCase()")
                        role = await el.get_attribute("role") or ""

                        if tag == "select":
                            # Native select element (including Select2)
                            await el.evaluate("""(sel, target) => {
                                let match = null;
                                let tLow = target.toLowerCase().trim();
                                for (let o of sel.options) {
                                    let oText = o.text.toLowerCase().trim();
                                    let oVal = o.value.toLowerCase().trim();
                                    if (oText === tLow || oVal === tLow || oText.includes(tLow) || tLow.includes(oText)) {
                                        match = o;
                                        break;
                                    }
                                }
                                if (match) {
                                    sel.value = match.value;
                                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                                    if (window.jQuery && window.jQuery(sel).data('select2')) {
                                        window.jQuery(sel).val(match.value).trigger('change');
                                    }
                                }
                            }""", user_answer)
                        elif unanswered.get("type") == "select":
                            # Dropdown select question
                            parent_container = await el.query_selector("xpath=ancestor::div[contains(@class, 'select')]")
                            control = await parent_container.query_selector(".select__control") if parent_container else None
                            trigger = control or el
                            if trigger:
                                await trigger.click()
                                await asyncio.sleep(0.4)

                                # 1. aria-controls
                                aria_controls = await el.get_attribute("aria-controls")
                                listbox = await ctx.query_selector(f"#{aria_controls}") if aria_controls else None

                                # 2. aria-owns
                                if not listbox:
                                    aria_owns = await el.get_attribute("aria-owns")
                                    listbox = await ctx.query_selector(f"#{aria_owns}") if aria_owns else None

                                # 3. portaled menu attached to body (check both frame and page)
                                if not listbox:
                                    listbox = await ctx.query_selector('.select__menu:not([class*="control"]), [role="listbox"]:not(.iti__country-list):not([class*="iti"]), [class*="MuiAutocomplete-popper"], [class*="ant-select-dropdown"]')
                                if not listbox:
                                    listbox = await self.page.query_selector('.select__menu:not([class*="control"]), [role="listbox"]:not(.iti__country-list):not([class*="iti"]), [class*="MuiAutocomplete-popper"], [class*="ant-select-dropdown"]')

                                opt_el = None
                                if listbox:
                                    opt_items = await listbox.query_selector_all('[role="option"], .select__option')
                                    # 1. Exact match
                                    for o in opt_items:
                                        if (await o.inner_text() or "").strip().lower() == str(user_answer).strip().lower():
                                            opt_el = o
                                            break
                                    # 2. Boolean normalization
                                    if not opt_el:
                                        u_lower = str(user_answer).strip().lower()
                                        if u_lower.startswith("no"):
                                            for o in opt_items:
                                                if (await o.inner_text() or "").strip().lower() == "no":
                                                    opt_el = o
                                                    break
                                        elif u_lower.startswith("yes"):
                                            for o in opt_items:
                                                if (await o.inner_text() or "").strip().lower() == "yes":
                                                    opt_el = o
                                                    break
                                    # 3. Substring match
                                    if not opt_el:
                                        for o in opt_items:
                                            o_txt = (await o.inner_text() or "").strip().lower()
                                            if o_txt in str(user_answer).lower() or str(user_answer).lower() in o_txt:
                                                opt_el = o
                                                break
                                if not opt_el:
                                    opt_el = await ctx.query_selector(f'.select__option:has-text("{user_answer}")') or await self.page.query_selector(f'.select__option:has-text("{user_answer}")')
                                if opt_el:
                                    await opt_el.click()
                                    await asyncio.sleep(0.3)
                                else:
                                    await el.fill(user_answer)
                                    await asyncio.sleep(0.5)
                                    await self.page.keyboard.press("ArrowDown")
                                    await self.page.keyboard.press("Enter")
                            else:
                                await el.fill(user_answer)
                                await asyncio.sleep(0.5)
                                await self.page.keyboard.press("ArrowDown")
                                await self.page.keyboard.press("Enter")
                        elif role == "combobox":
                            # Typeahead combobox with text answer
                            await el.focus()
                            await el.fill(user_answer)
                            await asyncio.sleep(1.0)
                            opt_el = await ctx.query_selector(f'.select__option:has-text("{user_answer}")')
                            if opt_el:
                                await opt_el.click()
                            else:
                                await self.page.keyboard.press("ArrowDown")
                                await self.page.keyboard.press("Enter")
                        elif unanswered.get("type") == "date":
                            # Datepicker or calendar input
                            await set_date_on_element(el, str(user_answer))
                        else:
                            # Standard text / textarea input (with date fallback)
                            inp_class = (await el.get_attribute("class") or "").lower()
                            if "datepicker" in inp_class or "calendar" in inp_class:
                                await set_date_on_element(el, str(user_answer))
                            else:
                                await el.fill(user_answer)
                except Exception as e:
                    print(f"Error filling user answer: {e}")

                # Save answer to reusable memory if requested (excluding standard core identity fields)
                if app.get("save_answer_for_future"):
                    lbl_text = unanswered.get("label", "")
                    lbl_lower = lbl_text.lower().strip()
                    is_core_identity = lbl_lower in ["first name", "last name", "full name", "email", "phone", "mobile phone", "resume upload"]
                    if not is_core_identity:
                        try:
                            create_saved_answer(
                                pattern=lbl_text,
                                answer=user_answer,
                                field_type=unanswered.get("type", "text"),
                                category=app.get("answer_category", "general"),
                                scope=app.get("answer_scope", "global"),
                                company_name=company_name,
                                options_seen=unanswered.get("options", []),
                                source="user"
                            )
                            await self.log_app(app_id, f"Saved question to reusable memory: '{lbl_text}'")
                        except Exception as e:
                            print(f"Failed to save reusable answer: {e}")

                # Reset question state
                update_application(app_id, {
                    "status": "processing",
                    "user_answer": None,
                    "unanswered_question": None,
                    "ai_suggestion": ""
                })

        # Execute autofill pass: scans form, uses local Qwen AI for understanding and answer generation
        await self.log_app(app_id, "Scanning form fields and utilizing local Qwen AI for intelligent autofill...")
        fill_res = await fill_form_on_page(self.page, profile, saved_answers, resume_path, job_context=company_name)

        if fill_res["fields_filled"]:
            filled_summary = ", ".join([f"{f['field']}: {f['value']}" for f in fill_res["fields_filled"]])
            await self.log_app(app_id, f"Filled fields: {filled_summary}")

        # Check if a visible CAPTCHA or security checkpoint is present
        if await detect_security_checkpoint(self.page):
            screenshot = await self.capture_screenshot()
            await self.log_app(app_id, "Form fields filled! Visible security CAPTCHA detected. Please solve on laptop.")
            update_application(app_id, {
                "status": "paused",
                "stage_message": "Fields filled! Please complete the verification on your laptop window, then click Resume.",
                "review_screenshot_url": screenshot
            })
            return

        # If question requires user input / confirmation
        if fill_res["needs_input"]:
            screenshot = await self.capture_screenshot()
            q_info = fill_res["unanswered_question"]
            ai_sug = fill_res.get("ai_suggestion", "")
            await self.log_app(app_id, f"Question requires your confirmation: '{q_info['label']}'. AI suggested: '{ai_sug}'")
            update_application(app_id, {
                "status": "needs_input",
                "unanswered_question": q_info,
                "ai_suggestion": ai_sug,
                "review_screenshot_url": screenshot,
                "stage_message": f"Question needs answer: {q_info['label']}"
            })
            return

        # Handle multi-page form continuation if "Next" button exists
        if fill_res["has_next_page"]:
            await self.log_app(app_id, "Found multi-page form. Navigating to next page...")
            try:
                ctx = await get_form_context(self.page)
                next_btn = await ctx.query_selector('button:has-text("Next"), input[value="Next"], button:has-text("Next Step"), button:has-text("Save and Continue")')
                if not next_btn:
                    next_btn = await self.page.query_selector('button:has-text("Next"), input[value="Next"], button:has-text("Next Step"), button:has-text("Save and Continue")')
                if next_btn and await next_btn.is_visible() and not await next_btn.is_disabled():
                    await next_btn.click(timeout=5000)
                    await asyncio.sleep(2)
                    return
            except Exception as e:
                print(f"Next button click failed: {e}")

        # If all fields filled and ready for review
        screenshot = await self.capture_screenshot()
        await self.log_app(app_id, "Form successfully filled! Pausing for your final review and explicit approval.")
        update_application(app_id, {
            "status": "waiting_review",
            "review_screenshot_url": screenshot,
            "stage_message": "All fields filled. Please review and click 'Approve & Submit' to finalize."
        })

    async def submit_application(self, app: Dict[str, Any]):
        app_id = app["id"]
        await self.log_app(app_id, "Explicit user approval received. Submitting application now...")
        try:
            ctx = await get_form_context(self.page)
            submit_btn = await ctx.query_selector('button[type="submit"], input[type="submit"], button:has-text("Submit"), button:has-text("Apply")')
            if not submit_btn:
                submit_btn = await self.page.query_selector('button[type="submit"], input[type="submit"], button:has-text("Submit"), button:has-text("Apply")')
            if submit_btn:
                await submit_btn.click()
                await asyncio.sleep(3)
                screenshot = await self.capture_screenshot()
                await self.log_app(app_id, "Application submitted successfully!")
                update_application(app_id, {
                    "status": "completed",
                    "stage_message": "Submitted successfully!",
                    "review_screenshot_url": screenshot
                })
            else:
                await self.log_app(app_id, "Submit button not found automatically. Check laptop screen.")
                update_application(app_id, {
                    "status": "paused",
                    "stage_message": "Submit button not found automatically. Check laptop screen."
                })
        except Exception as e:
            await self.log_app(app_id, f"Error clicking submit button: {e}")
            update_application(app_id, {
                "status": "failed",
                "error_message": f"Submit error: {e}"
            })

    async def run_loop(self):
        print("="*60)
        print("ApplyMate Local Background Worker Started (SQLite + Qwen3-0.6B)")
        print(f"SQLite DB: {settings.SQLITE_DB_PATH}")
        print(f"Local AI Endpoint: {settings.LLAMA_CPP_URL}")
        print("="*60)

        while self.is_running:
            try:
                await self.update_heartbeat(status="online")

                # Fetch all applications from local SQLite
                apps = get_applications()

                # 1. Check for 'submitting' tasks (User clicked Approve & Submit)
                submitting_tasks = [a for a in apps if a["status"] == "submitting"]
                if submitting_tasks:
                    await self.submit_application(submitting_tasks[0])
                    await asyncio.sleep(settings.POLL_INTERVAL)
                    continue

                # 2. Check for 'needs_input' tasks where user provided an answer
                answered_tasks = [a for a in apps if a["status"] == "needs_input" and a.get("user_answer")]
                if answered_tasks:
                    await self.process_task(answered_tasks[0])
                    await asyncio.sleep(settings.POLL_INTERVAL)
                    continue

                # 3. Check for 'queued' or active 'processing' tasks
                queued_tasks = [a for a in apps if a["status"] in ("queued", "processing")]
                if queued_tasks:
                    await self.process_task(queued_tasks[0])
                    await asyncio.sleep(settings.POLL_INTERVAL)
                    continue

            except Exception as e:
                print(f"[Worker Exception] {e}")
                await asyncio.sleep(3)

            await asyncio.sleep(settings.POLL_INTERVAL)

if __name__ == "__main__":
    worker = ApplyMateWorker()
    try:
        asyncio.run(worker.run_loop())
    except KeyboardInterrupt:
        print("\nWorker stopped by user.")
