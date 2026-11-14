import os
import re
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
from form_filler import (
    fill_form_on_page, detect_security_checkpoint, detect_login_wall,
    get_form_context, set_date_on_element, fast_restore_form_data
)

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
        if not self.context:
            user_data_dir = os.path.abspath(os.path.join(settings.RESUME_STORAGE_DIR, "browser_profile"))
            os.makedirs(user_data_dir, exist_ok=True)
            try:
                self.context = await self.playwright.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    headless=settings.HEADLESS_BROWSER,
                    viewport={"width": 1280, "height": 800},
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-extensions",
                        "--no-first-run"
                    ]
                )
                self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            except Exception as e:
                print(f"[Browser Init] Persistent context notice: {e}. Falling back to standard context.")
                self.browser = await self.playwright.chromium.launch(
                    headless=settings.HEADLESS_BROWSER,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                )
                self.context = await self.browser.new_context(viewport={"width": 1280, "height": 800})
                self.page = await self.context.new_page()

    async def close_browser(self):
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
            self.page = None
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
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
        persisted_form_data = app.get("form_data") or {"fields": {}}
        if not isinstance(persisted_form_data, dict):
            persisted_form_data = {"fields": {}}
        if "fields" not in persisted_form_data:
            persisted_form_data["fields"] = {}

        await self.update_heartbeat(status="busy", active_app_id=app_id)
        profile = get_profile()
        saved_answers = get_saved_answers()
        resume_path = profile.get("resume_file_path")

        await self.init_browser()

        # Check if browser is currently positioned on the target job application page
        current_url = self.page.url if self.page else ""
        is_page_open = (
            current_url 
            and current_url != "about:blank" 
            and (job_url.split("?")[0] in current_url or current_url.split("?")[0] in job_url)
        )

        if not is_page_open:
            is_recovery = (status in ("needs_input", "processing", "paused") and bool(persisted_form_data.get("fields")))
            if is_recovery:
                await self.log_app(app_id, f"[Crash Recovery] Worker resumed. Reconnecting browser to application: {job_url}")
            else:
                await self.log_app(app_id, f"Opening application URL: {job_url}")

            if status == "queued":
                update_application(app_id, {"status": "processing"})

            try:
                await self.page.goto(job_url, timeout=45000, wait_until="domcontentloaded")
                await asyncio.sleep(2)
                page_title = await self.page.title()
                if page_title and not app.get("job_title"):
                    update_application(app_id, {"job_title": page_title[:80]})

                # Check for initial Login Wall or Security Challenge
                barrier = await detect_security_checkpoint(self.page) or await detect_login_wall(self.page)
                if barrier:
                    screenshot = await self.capture_screenshot()
                    await self.log_app(app_id, f"Barrier detected: {barrier}")
                    update_application(app_id, {
                        "status": "paused",
                        "stage_message": barrier,
                        "review_screenshot_url": screenshot
                    })
                    return

                # If recovering from crash, fast-restore previously filled fields without refilling!
                if is_recovery:
                    ctx = await get_form_context(self.page)
                    restored_count = await fast_restore_form_data(ctx, persisted_form_data, resume_path=resume_path)
                    if restored_count > 0:
                        await self.log_app(app_id, f"[Crash Recovery] Restored {restored_count} previously filled fields from snapshot. Resuming exactly where left off.")
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
                        inp_type = (await el.get_attribute("type") or "").lower()

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
                        elif inp_type == "radio":
                            name = await el.get_attribute("name") or ""
                            from form_filler import get_option_label
                            group_radios = await ctx.query_selector_all(f'input[type="radio"][name="{name}"]') if name else [el]
                            for r_el in group_radios:
                                r_lbl = await get_option_label(ctx, r_el)
                                if r_lbl.lower() == str(user_answer).lower() or (str(user_answer).lower() == "yes" and r_lbl.lower().startswith("yes")) or (str(user_answer).lower() == "no" and r_lbl.lower().startswith("no")):
                                    await r_el.click()
                                    break
                        elif inp_type == "checkbox":
                            if str(user_answer).lower() in ["yes", "true", "check", "1"]:
                                await el.check()
                        elif unanswered.get("type") == "date":
                            # Datepicker or calendar input
                            await set_date_on_element(el, str(user_answer))
                        elif inp_type == "number":
                            num_match = re.search(r'\d+(?:\.\d+)?', str(user_answer))
                            val_num = num_match.group(0) if num_match else "0"
                            await el.fill(val_num)
                            await el.evaluate("""(e) => {
                                e.dispatchEvent(new Event('input', { bubbles: true }));
                                e.dispatchEvent(new Event('change', { bubbles: true }));
                            }""")
                        else:
                            # Standard text / textarea input (with date fallback)
                            inp_class = (await el.get_attribute("class") or "").lower()
                            if "datepicker" in inp_class or "calendar" in inp_class:
                                await set_date_on_element(el, str(user_answer))
                            else:
                                await el.fill(str(user_answer))
                                await el.evaluate("""(e) => {
                                    e.dispatchEvent(new Event('input', { bubbles: true }));
                                    e.dispatchEvent(new Event('change', { bubbles: true }));
                                }""")

                        # Record newly answered field in persisted_form_data
                        ans_key = f"id:{field_id}" if field_id else f"label:{unanswered.get('label', '').lower().strip()}"
                        persisted_form_data["fields"][ans_key] = {
                            "key": ans_key,
                            "id": field_id,
                            "name": field_id,
                            "label": unanswered.get("label", ""),
                            "tag": tag if 'tag' in locals() else "input",
                            "type": inp_type if 'inp_type' in locals() and inp_type else unanswered.get("type", "text"),
                            "value": str(user_answer),
                            "timestamp": time.time()
                        }
                        update_application(app_id, {"form_data": persisted_form_data})
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
                            saved_answers = get_saved_answers()
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
        fill_res = await fill_form_on_page(
            self.page, profile, saved_answers, resume_path,
            job_context=company_name,
            existing_form_data=persisted_form_data
        )

        if "form_data" in fill_res:
            persisted_form_data = fill_res["form_data"]
            update_application(app_id, {"form_data": persisted_form_data})

        if fill_res["fields_filled"]:
            filled_summary = ", ".join([f"{f['field']}: {f['value']}" for f in fill_res["fields_filled"]])
            await self.log_app(app_id, f"Filled fields: {filled_summary}")

        # Check if a visible CAPTCHA or security checkpoint is present
        checkpoint = await detect_security_checkpoint(self.page)
        if checkpoint:
            screenshot = await self.capture_screenshot()
            await self.log_app(app_id, f"Security challenge detected: {checkpoint}")
            update_application(app_id, {
                "status": "paused",
                "stage_message": checkpoint,
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
                "stage_message": f"Question needs answer: {q_info['label']}",
                "form_data": fill_res.get("form_data", persisted_form_data)
            })
            return

        # 1. Check if validation errors or unfilled required fields were detected on current step
        if fill_res.get("validation_errors") or fill_res.get("empty_required_fields"):
            all_errs = fill_res.get("validation_errors", []) + fill_res.get("empty_required_fields", [])
            err_msg = "; ".join(all_errs[:3])
            screenshot = await self.capture_screenshot()
            await self.log_app(app_id, f"Form validation notice: {err_msg}")
            update_application(app_id, {
                "status": "waiting_review",
                "review_screenshot_url": screenshot,
                "stage_message": f"Validation notice: {err_msg}. Please review on laptop before submitting."
            })
            return

        # 2. Handle multi-step wizard continuation if "Next" button exists and current step is fully valid
        if fill_res["has_next_page"]:
            await self.log_app(app_id, "Step complete! Advancing to next wizard step...")
            try:
                ctx = await get_form_context(self.page)
                next_btn = await ctx.query_selector('button:has-text("Next"), input[value="Next"], button:has-text("Next Step"), button:has-text("Save and Continue")')
                if not next_btn:
                    next_btn = await self.page.query_selector('button:has-text("Next"), input[value="Next"], button:has-text("Next Step"), button:has-text("Save and Continue")')
                if next_btn and await next_btn.is_visible() and not await next_btn.is_disabled():
                    await next_btn.click(timeout=5000)
                    try:
                        await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        pass
                    await asyncio.sleep(2)
                    await self.log_app(app_id, "New step loaded. Scanning next page fields...")
                    return
            except Exception as e:
                print(f"Next button click failed: {e}")

        # 3. Final step reached: all wizard pages filled and verified
        screenshot = await self.capture_screenshot()
        await self.log_app(app_id, "All steps completed! Pausing for your final review and explicit approval.")
        update_application(app_id, {
            "status": "waiting_review",
            "review_screenshot_url": screenshot,
            "stage_message": "All steps filled. Please review and click 'Approve & Submit' to finalize."
        })

    async def restore_standby_page(self, app: Dict[str, Any]):
        """
        Crash Recovery Standby:
        If worker restarted while a task was waiting for user input,
        proactively re-opens the browser at the application URL, restores
        all previously filled fields from snapshot, and scrolls to the pending question.
        """
        app_id = app["id"]
        job_url = app["job_url"]
        persisted_form_data = app.get("form_data") or {"fields": {}}
        if not isinstance(persisted_form_data, dict):
            persisted_form_data = {"fields": {}}
        profile = get_profile()
        resume_path = profile.get("resume_file_path")
        unanswered = app.get("unanswered_question") or {}

        try:
            await self.init_browser()
            current_url = self.page.url if self.page else ""
            if not current_url or current_url == "about:blank" or (job_url.split("?")[0] not in current_url and current_url.split("?")[0] not in job_url):
                await self.log_app(app_id, f"[Crash Recovery] Proactively restoring browser session at waiting question...")
                await self.page.goto(job_url, timeout=45000, wait_until="domcontentloaded")
                await asyncio.sleep(2)
                ctx = await get_form_context(self.page)
                restored = await fast_restore_form_data(ctx, persisted_form_data, resume_path=resume_path)
                if restored > 0:
                    await self.log_app(app_id, f"[Crash Recovery] Standby session ready with {restored} fields restored. Waiting for your reply...")
                field_id = unanswered.get("field_id")
                if field_id:
                    try:
                        el = await ctx.query_selector(f"#{field_id}") or await ctx.query_selector(f'[name="{field_id}"]')
                        if el:
                            await el.scroll_into_view_if_needed()
                    except Exception:
                        pass
        except Exception as e:
            print(f"[Crash Recovery Standby] Error restoring standby page: {e}")

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

                # 4. Proactive crash-recovery standby for pending needs_input tasks
                waiting_input_tasks = [a for a in apps if a["status"] == "needs_input" and not a.get("user_answer")]
                if waiting_input_tasks and (not self.page or self.page.url == "about:blank"):
                    wait_app = waiting_input_tasks[0]
                    await self.restore_standby_page(wait_app)

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
