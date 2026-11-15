import os
import re
import time
import base64
import asyncio
import hashlib
from typing import Optional, Dict, Any, List
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from config import settings
from database import (
    get_profile, get_saved_answers, get_applications, get_application,
    update_application, update_worker_heartbeat, create_saved_answer,
    acquire_queued_application, recover_stuck_processing_applications
)
from form_filler import (
    fill_form_on_page, detect_security_checkpoint, detect_login_wall,
    get_form_context, set_date_on_element, fast_restore_form_data,
    get_element_label
)
from widget_executors import WidgetType, widget_registry, verify_filled_widgets
from telegram_bot import send_batch_questions_to_telegram

class ApplyMateWorker:
    def __init__(self):
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.pages: Dict[str, Page] = {}  # Per-application page mapping: app_id -> Page
        self.is_running = True
        self.worker_id = "laptop-worker"
        self.app_attempts: Dict[str, Dict[str, int]] = {}  # Per-app question retry attempts: app_id -> {q_key: count}
        self.last_telegram_batch_hash: Dict[str, str] = {}  # Per-app last notified batch hash: app_id -> hash

    @property
    def page(self) -> Optional[Page]:
        """Backwards compatibility property: returns most recently active page if any."""
        for p in reversed(list(self.pages.values())):
            if p and not p.is_closed():
                return p
        if self.context and self.context.pages:
            for p in self.context.pages:
                if not p.is_closed():
                    return p
        return None

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
            except Exception as e:
                print(f"[Browser Init] Persistent context notice: {e}. Falling back to standard context.")
                self.browser = await self.playwright.chromium.launch(
                    headless=settings.HEADLESS_BROWSER,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                )
                self.context = await self.browser.new_context(viewport={"width": 1280, "height": 800})

    async def get_or_create_page(self, app_id: str) -> Page:
        """
        Retrieves the dedicated Page for this application, or opens a new tab in the persistent context.
        Guarantees each application maintains its own tab without navigating away from other applications.
        """
        await self.init_browser()
        if app_id in self.pages:
            p = self.pages[app_id]
            if not p.is_closed():
                return p
            else:
                del self.pages[app_id]

        # If context has an unused blank tab on startup, claim it for this app
        if self.context and self.context.pages:
            claimed_pages = set(self.pages.values())
            for candidate in self.context.pages:
                if candidate not in claimed_pages and not candidate.is_closed():
                    cand_url = candidate.url or ""
                    if cand_url in ["about:blank", ""]:
                        self.pages[app_id] = candidate
                        return candidate

        # Open a new tab in the persistent context dedicated to this application
        new_page = await self.context.new_page()
        self.pages[app_id] = new_page
        return new_page

    async def close_page(self, app_id: str):
        """Closes and unregisters the page dedicated to this application."""
        if app_id in self.pages:
            p = self.pages.pop(app_id, None)
            if p and not p.is_closed():
                try:
                    await p.close()
                except Exception:
                    pass

    async def close_browser(self):
        for app_id in list(self.pages.keys()):
            await self.close_page(app_id)
        self.pages.clear()
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
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

    async def capture_screenshot(self, page: Optional[Page] = None) -> str:
        """Capture page screenshot and return base64 data URI for dashboard review."""
        target_page = page or self.page
        if not target_page or target_page.is_closed():
            return ""
        try:
            img_bytes = await target_page.screenshot(full_page=False)
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

        # Verify profile completeness before processing application
        completeness = calculate_profile_completeness(profile)
        if not completeness.get("is_complete") and len(completeness.get("missing_fields", [])) > 2:
            missing_names = [f["label"] for f in completeness.get("missing_fields", [])[:3]]
            warn_msg = f"Candidate Profile has critical missing fields: {missing_names}. Please update your profile in the dashboard."
            await self.log_app(app_id, f"[Profile Warning] {warn_msg}")
            print(f"[Worker] {warn_msg}")

        # Check resume file exists on disk
        if resume_path and not os.path.exists(resume_path):
            missing_resume_msg = f"Resume document path '{resume_path}' does not exist on disk. Please upload your resume in the dashboard."
            await self.log_app(app_id, f"[File Error] {missing_resume_msg}")
            print(f"[Worker] {missing_resume_msg}")

        # Each application gets its own dedicated page in the persistent context
        page = await self.get_or_create_page(app_id)

        # Check if total application execution time exceeded global timeout (15 minutes)
        created_at_str = app.get("created_at") or ""
        if created_at_str:
            try:
                created_ts = time.mktime(time.strptime(created_at_str, '%Y-%m-%d %H:%M:%S'))
                if (time.time() - created_ts) > 900:  # 15 minutes timeout
                    timeout_msg = f"Application exceeded maximum duration timeout of 15 minutes."
                    await self.log_app(app_id, f"[Timeout] {timeout_msg}")
                    screenshot = await self.capture_screenshot(page)
                    update_application(app_id, {
                        "status": "failed",
                        "error_message": timeout_msg,
                        "review_screenshot_url": screenshot
                    })
                    await self.close_page(app_id)
                    return
            except Exception:
                pass
        current_url = page.url if page else ""
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
                await page.goto(job_url, timeout=45000, wait_until="domcontentloaded")
                await asyncio.sleep(2)
                page_title = await page.title()
                if page_title and not app.get("job_title"):
                    update_application(app_id, {"job_title": page_title[:80]})

                # Check for initial Login Wall or Security Challenge
                barrier = await detect_security_checkpoint(page) or await detect_login_wall(page)
                if barrier:
                    screenshot = await self.capture_screenshot(page)
                    await self.log_app(app_id, f"Barrier detected: {barrier}")
                    update_application(app_id, {
                        "status": "paused",
                        "stage_message": barrier,
                        "review_screenshot_url": screenshot
                    })
                    return

                # If recovering from crash, fast-restore previously filled fields using executors and re-verify!
                if is_recovery:
                    ctx = await get_form_context(page)
                    restore_res = await fast_restore_form_data(ctx, persisted_form_data, resume_path=resume_path)
                    if restore_res.restored_count > 0:
                        await self.log_app(app_id, f"[Crash Recovery] Restored {restore_res.restored_count} previously filled field(s) from snapshot. Resuming exactly where left off.")
                    if restore_res.failed_fields:
                        failed_names = [f.get("field") for f in restore_res.failed_fields]
                        await self.log_app(app_id, f"[Crash Recovery Warning] Re-verification failed for restored field(s): {failed_names}")
            except Exception as e:
                await self.log_app(app_id, f"Failed to load job page: {e}")
                screenshot = await self.capture_screenshot(page)
                update_application(app_id, {
                    "status": "failed",
                    "error_message": f"Navigation failed: {e}",
                    "review_screenshot_url": screenshot
                })
                await self.close_page(app_id)
                return

        # Check if resuming from paused (e.g. user manually solved CAPTCHA or clicked resume)
        if status == "paused":
            await self.log_app(app_id, "Resuming application after manual verification in browser...")
            # Re-read DOM values for fields marked manual_required
            ctx = await get_form_context(page)
            unanswered_q_list = app.get("unanswered_questions") or []
            if app.get("unanswered_question") and not unanswered_q_list:
                unanswered_q_list = [app.get("unanswered_question")]
            
            still_pending = []
            for q_item in unanswered_q_list:
                q_field_id = (q_item.get("field_id") or "").strip()
                q_label = (q_item.get("label") or "").strip()
                target_el = None
                if q_field_id:
                    try:
                        target_el = await ctx.query_selector(f'[id="{q_field_id}"]') or await ctx.query_selector(f'[name="{q_field_id}"]')
                    except Exception:
                        target_el = None
                if not target_el and q_label:
                    try:
                        clean_lbl = q_label.strip().lower()
                        for cand in await ctx.query_selector_all('input, select, textarea, [role="combobox"]'):
                            cand_lbl = (await get_element_label(ctx, cand)).strip().lower()
                            if cand_lbl and (cand_lbl == clean_lbl or clean_lbl in cand_lbl):
                                target_el = cand
                                break
                    except Exception:
                        pass
                
                # If target element now has a non-empty value entered by the user in the browser, consider it satisfied
                if target_el:
                    dom_type = await widget_registry.detect_widget_type(ctx, target_el)
                    ok, read_val = await widget_registry.get_executor(dom_type).verify(ctx, target_el, q_item.get("answer") or "")
                    if not ok and read_val:
                        # Value was entered manually by the user
                        ok = True
                    if ok and read_val:
                        await self.log_app(app_id, f"[Resume] Verified manual entry in browser for '{q_label}': '{read_val}'")
                        ans_key = f"id:{q_field_id}" if q_field_id else f"label:{q_label.lower().strip()}"
                        persisted_form_data.setdefault("fields", {})[ans_key] = {
                            "key": ans_key,
                            "id": q_field_id,
                            "name": q_field_id,
                            "label": q_label,
                            "tag": "input",
                            "type": dom_type,
                            "value": str(read_val),
                            "verified": read_val,
                            "timestamp": time.time()
                        }
                        continue
                still_pending.append(q_item)
            
            update_application(app_id, {
                "status": "processing",
                "unanswered_questions": still_pending,
                "unanswered_question": still_pending[0] if still_pending else None,
                "form_data": persisted_form_data
            })

        # Check if batch answers or single user answer arrived (CLI, Telegram, Dashboard)
        batch_answers = app.get("batch_answers") or []
        if not batch_answers and app.get("user_answer") and app.get("unanswered_question"):
            u_q = app.get("unanswered_question")
            batch_answers = [{
                "id": u_q.get("id"),
                "field_id": u_q.get("field_id"),
                "label": u_q.get("label", ""),
                "type": u_q.get("type", "text"),
                "options": u_q.get("options", []),
                "answer": app.get("user_answer"),
                "scope": app.get("answer_scope", "global"),
                "save_for_future": bool(app.get("save_answer_for_future", False)),
                "category": app.get("answer_category", "general")
            }]

        if batch_answers:
            await self.log_app(app_id, f"Processing batch of {len(batch_answers)} answer(s) via dedicated widget executors...")
            ctx = await get_form_context(page)
            remaining_batch = []
            applied_count = 0

            # Gather interactive form controls for robust lookup
            interactive_elements = await ctx.query_selector_all('input, select, textarea, [role="combobox"]')

            for ans_item in batch_answers:
                ans_val = ans_item.get("answer")
                ans_lbl = (ans_item.get("label") or "").strip()
                ans_field_id = (ans_item.get("field_id") or "").strip()
                ans_type = (ans_item.get("type") or "text").lower().strip()
                ans_scope = ans_item.get("scope", "global")
                ans_cat = ans_item.get("category", "general")
                save_mem = bool(ans_item.get("save_for_future", True))

                if ans_val is None or str(ans_val).strip() == "":
                    continue

                # Unique key for tracking attempts per question per application
                q_key = (ans_field_id or ans_lbl).strip().lower()
                app_attempts_map = self.app_attempts.setdefault(app_id, {})
                
                # Check persisted attempts in form_data if memory map doesn't have it (survives worker restarts)
                persisted_attempts = persisted_form_data.setdefault("question_attempts", {})
                if q_key not in app_attempts_map and q_key in persisted_attempts:
                    app_attempts_map[q_key] = persisted_attempts[q_key]
                current_attempt = app_attempts_map.get(q_key, 0)

                # Exponential backoff delay if this question was previously attempted
                if current_attempt > 0:
                    backoff_sec = 2 ** min(current_attempt, 4)  # 2, 4, 8, 16s
                    await self.log_app(app_id, f"Backing off for {backoff_sec}s before retry attempt {current_attempt + 1} on '{ans_lbl}'...")
                    await asyncio.sleep(backoff_sec)

                el = None
                # A. Try exact CSS ID using [id="..."] attribute selector
                if ans_field_id:
                    try:
                        el = await ctx.query_selector(f'[id="{ans_field_id}"]')
                    except Exception:
                        el = None
                # B. Try exact name attribute
                if not el and ans_field_id:
                    try:
                        el = await ctx.query_selector(f'[name="{ans_field_id}"]')
                    except Exception:
                        el = None
                # C. Try label matching without :has-text
                if not el and ans_lbl:
                    try:
                        ans_lbl_clean = ans_lbl.strip().lower()
                        all_labels = await ctx.query_selector_all('label')
                        for l_el in all_labels:
                            l_txt = (await l_el.inner_text() or "").strip().lower()
                            if l_txt == ans_lbl_clean or ans_lbl_clean in l_txt:
                                for_attr = await l_el.get_attribute("for")
                                if for_attr:
                                    el = await ctx.query_selector(f'[id="{for_attr}"]')
                                    if el:
                                        break
                    except Exception:
                        el = None

                # D. Iterate interactive elements comparing resolved label or ID/name
                if not el:
                    ans_lbl_low = ans_lbl.lower()
                    for cand_el in interactive_elements:
                        try:
                            cand_lbl = await get_element_label(ctx, cand_el)
                            if cand_lbl and (cand_lbl.lower() == ans_lbl_low or ans_lbl_low in cand_lbl.lower() or cand_lbl.lower() in ans_lbl_low):
                                el = cand_el
                                break
                            cand_id = (await cand_el.get_attribute("id") or "").lower()
                            cand_name = (await cand_el.get_attribute("name") or "").lower()
                            if ans_field_id and (cand_id == ans_field_id.lower() or cand_name == ans_field_id.lower()):
                                el = cand_el
                                break
                        except Exception:
                            continue

                if not el:
                    await self.log_app(app_id, f"[Warning] Could not locate element in DOM for question '{ans_lbl}'. Retaining in batch.")
                    remaining_batch.append(ans_item)
                    continue

                try:
                    await el.scroll_into_view_if_needed()
                except Exception:
                    pass

                # Apply via widget executor with automatic DOM re-read verification
                try:
                    exec_res = await widget_registry.execute_element(ctx, el, ans_val, widget_type=ans_type)
                except Exception as exec_err:
                    await self.log_app(app_id, f"[Executor Error] Exception during widget execution for '{ans_lbl}': {exec_err}. Retaining in batch.")
                    remaining_batch.append(ans_item)
                    continue

                if exec_res.success and exec_res.verification_passed:
                    applied_count += 1
                    # Reset attempt counter on success in memory and persisted snapshot
                    app_attempts_map[q_key] = 0
                    persisted_attempts[q_key] = 0
                    # Store the verified option text rather than unverified user input
                    verified_text = str(exec_res.verified_value if exec_res.verified_value is not None else ans_val)
                    await self.log_app(app_id, f"Verified answer for '{ans_lbl}': '{verified_text}'")

                    # Record newly answered field in persisted_form_data
                    ans_key = f"id:{ans_field_id}" if ans_field_id else f"label:{ans_lbl.lower().strip()}"
                    persisted_form_data.setdefault("fields", {})[ans_key] = {
                        "key": ans_key,
                        "id": ans_field_id,
                        "name": ans_field_id,
                        "label": ans_lbl,
                        "tag": "input",
                        "type": ans_type,
                        "value": verified_text,
                        "verified": exec_res.verified_value,
                        "timestamp": time.time()
                    }

                    # ONLY save to answer memory after executor successfully verifies the fill
                    if save_mem:
                        lbl_lower = ans_lbl.lower().strip()
                        is_core_id = lbl_lower in ["first name", "last name", "full name", "email", "phone", "mobile phone", "resume upload"]
                        if not is_core_id:
                            try:
                                create_saved_answer(
                                    pattern=ans_lbl,
                                    answer=verified_text,  # Storing verified option text
                                    field_type=ans_type,
                                    category=ans_cat,
                                    scope=ans_scope,
                                    company_name=company_name if ans_scope in ["this company", "company"] else "",
                                    options_seen=ans_item.get("options", []),
                                    source="user"
                                )
                                await self.log_app(app_id, f"Saved verified answer to Answer Memory ({ans_scope} scope): '{ans_lbl}' -> '{verified_text}'")
                            except Exception as save_err:
                                print(f"[Worker] Failed to save answer to memory: {save_err}")
                else:
                    err_msg = exec_res.error or "Verification check failed after fill"
                    current_attempt += 1
                    app_attempts_map[q_key] = current_attempt
                    persisted_attempts[q_key] = current_attempt
                    ans_item["attempts"] = current_attempt
                    ans_item["last_error"] = err_msg
                    ans_item["verification_failed"] = True

                    # Check if option click succeeded but value is unverified/unreadable in DOM
                    if exec_res.meta.get("unverified"):
                        ans_item["unverified"] = True
                        ans_item["needs_manual_confirmation"] = True
                        await self.log_app(app_id, f"[Combobox Unverified] Option clicked for '{ans_lbl}', but displayed value is unreadable in DOM. Flagged for manual confirmation.")
                    else:
                        await self.log_app(app_id, f"[Verification Failed] (Attempt {current_attempt}/3) Value check failed for '{ans_lbl}': {err_msg}.")
                    remaining_batch.append(ans_item)

            saved_answers = get_saved_answers()
            await self.log_app(app_id, f"Applied & verified {applied_count}/{len(batch_answers)} batch answers.")

            # If any answer failed verification or could not be applied:
            if remaining_batch:
                # Check for max retry exhaustion (backoff / circuit breaker)
                exhausted = [item for item in remaining_batch if item.get("attempts", 0) >= 3]
                unverified_items = [item for item in remaining_batch if item.get("unverified")]
                screenshot = await self.capture_screenshot(page)
                if exhausted:
                    ex_item = exhausted[0]
                    ex_lbl = ex_item.get("label") or "Question"
                    ex_err = ex_item.get("last_error") or "Verification failed"
                    ex_item["manual_required"] = True
                    pause_msg = f"Needs attention: 3 failed attempts to verify '{ex_lbl}' ({ex_err}). Field marked manual_required. Please review in the visible browser and click Resume."
                    await self.log_app(app_id, f"[Needs Attention] {pause_msg}")

                    # Filter out the exhausted item so other fields can continue if resumed, but mark application paused for attention
                    unanswered_remaining = [item for item in remaining_batch if not item.get("manual_required")]
                    update_application(app_id, {
                        "status": "paused",  # Needs attention state where user can fix in browser and click Resume
                        "batch_answers": [],
                        "user_answer": None,
                        "unanswered_questions": unanswered_remaining,
                        "unanswered_question": ex_item,
                        "stage_message": pause_msg,
                        "review_screenshot_url": screenshot,
                        "form_data": persisted_form_data
                    })
                    return

                # If option click succeeded but value is unverified in DOM, flag for manual confirmation instead of looping
                if unverified_items:
                    u_lbl = unverified_items[0].get("label") or "Field"
                    u_msg = f"Needs attention: Field '{u_lbl}' option was clicked, but displayed value could not be confirmed in DOM. Please confirm in the browser and click Resume."
                    await self.log_app(app_id, f"[Manual Confirmation Required] {u_msg}")
                    update_application(app_id, {
                        "status": "needs_input",
                        "batch_answers": [],
                        "user_answer": None,
                        "unanswered_questions": remaining_batch,
                        "unanswered_question": unverified_items[0],
                        "stage_message": u_msg,
                        "review_screenshot_url": screenshot,
                        "form_data": persisted_form_data
                    })
                    return

                # Under retry threshold: retain failed items in unanswered_questions,
                # but CRITICALLY CLEAR batch_answers so the worker loop does NOT endless-loop!
                first_rem = remaining_batch[0]
                stage_msg = f"Verification failed for '{first_rem.get('label')}'. Please review or provide answer."
                update_application(app_id, {
                    "status": "needs_input",
                    "batch_answers": [],  # Clear batch_answers to break the loop until new user input
                    "user_answer": None,
                    "unanswered_questions": remaining_batch,
                    "unanswered_question": first_rem,
                    "stage_message": stage_msg,
                    "review_screenshot_url": screenshot,
                    "form_data": persisted_form_data
                })
                # DO NOT proceed to fill_form_on_page while pending batch answers are unresolved!
                return

            # All batch answers were successfully applied and verified in the DOM!
            update_application(app_id, {
                "status": "processing",
                "batch_answers": [],
                "user_answer": None,
                "unanswered_question": None,
                "unanswered_questions": [],
                "form_data": persisted_form_data
            })

        # Execute autofill pass: scans form, uses local Qwen AI for understanding and answer generation
        await self.log_app(app_id, "Scanning form fields and utilizing local Qwen AI for intelligent autofill...")
        fill_res = await fill_form_on_page(
            page, profile, saved_answers, resume_path,
            job_context=company_name,
            existing_form_data=persisted_form_data,
            application_id=app_id
        )

        if "form_data" in fill_res:
            persisted_form_data = fill_res["form_data"]
            update_application(app_id, {"form_data": persisted_form_data})

        if fill_res["fields_filled"]:
            filled_summary = ", ".join([f"{f['field']}: {f['value']}" for f in fill_res["fields_filled"]])
            await self.log_app(app_id, f"Filled fields: {filled_summary}")

        # Check if a visible CAPTCHA or security checkpoint is present
        checkpoint = await detect_security_checkpoint(page)
        if checkpoint:
            screenshot = await self.capture_screenshot(page)
            await self.log_app(app_id, f"Security challenge detected: {checkpoint}")
            update_application(app_id, {
                "status": "paused",
                "stage_message": checkpoint,
                "review_screenshot_url": screenshot
            })
            return

        # If questions require user input / confirmation
        if fill_res["needs_input"]:
            screenshot = await self.capture_screenshot(page)
            q_batch = fill_res.get("unanswered_questions") or []
            if not q_batch and fill_res.get("unanswered_question"):
                q_batch = [fill_res["unanswered_question"]]

            first_q = q_batch[0] if q_batch else {"label": "Unknown Question"}
            ai_sug = first_q.get("ai_suggestion", "")

            await self.log_app(app_id, f"Collected batch of {len(q_batch)} question(s) requiring your confirmation. First: '{first_q.get('label')}'.")

            # Deliver notification to Telegram if configured (deduped by hash of question keys)
            try:
                batch_keys = []
                for q in q_batch:
                    k = q.get("question_key") or q.get("key") or q.get("field_id") or q.get("label", "")
                    batch_keys.append(str(k).strip().lower())
                batch_hash = hashlib.sha256("|".join(sorted(batch_keys)).encode("utf-8")).hexdigest()

                last_hash = self.last_telegram_batch_hash.get(app_id)
                if last_hash != batch_hash:
                    self.last_telegram_batch_hash[app_id] = batch_hash
                    send_batch_questions_to_telegram(company_name, "Job Application", q_batch, app_id)
                else:
                    print(f"[Worker] Telegram notification skipped: identical question batch already sent for {app_id}.")
            except Exception as tg_err:
                print(f"[Worker] Telegram notification error: {tg_err}")

            update_application(app_id, {
                "status": "needs_input",
                "unanswered_questions": q_batch,
                "unanswered_question": first_q,
                "ai_suggestion": ai_sug,
                "review_screenshot_url": screenshot,
                "stage_message": f"Batch of {len(q_batch)} question(s) waiting for answers" if len(q_batch) > 1 else f"Question needs answer: {first_q.get('label')}",
                "form_data": fill_res.get("form_data", persisted_form_data)
            })
            return

        # 1. READ VERIFICATION REPORT: Put any widget that failed its re-read into review message & block approval
        v_report = fill_res.get("verification_report") or {}
        failed_widgets = v_report.get("failed_fields") or []
        if not v_report.get("all_passed", True) or failed_widgets:
            failed_items_desc = []
            for fw in failed_widgets:
                f_name = fw.get("field") or "Unknown field"
                f_target = fw.get("target_value") or ""
                f_verified = fw.get("verified_value") or "empty/unmatched"
                failed_items_desc.append(f"'{f_name}' (expected: '{f_target}', read: '{f_verified}')")
            
            failed_summary = "; ".join(failed_items_desc)
            review_msg = f"Verification re-read failed for {len(failed_widgets)} field(s): {failed_summary}. Manual review and confirmation required before approval."
            await self.log_app(app_id, f"[Verification Warning] {review_msg}")
            
            screenshot = await self.capture_screenshot(page)
            update_application(app_id, {
                "status": "waiting_review",
                "review_screenshot_url": screenshot,
                "stage_message": review_msg,
                "verification_failed": 1,
                "verification_failures": failed_widgets,
                "requires_manual_confirmation": 1
            })
            return

        # 2. Check if validation errors or unfilled required fields were detected on current step
        if fill_res.get("validation_errors") or fill_res.get("empty_required_fields"):
            all_errs = fill_res.get("validation_errors", []) + fill_res.get("empty_required_fields", [])
            err_msg = "; ".join(all_errs[:3])
            screenshot = await self.capture_screenshot(page)
            await self.log_app(app_id, f"Form validation notice: {err_msg}")
            update_application(app_id, {
                "status": "waiting_review",
                "review_screenshot_url": screenshot,
                "stage_message": f"Validation notice: {err_msg}. Please review on laptop before submitting."
            })
            return

        # 3. Handle multi-step wizard continuation if "Next" button exists and current step is fully valid
        if fill_res["has_next_page"]:
            await self.log_app(app_id, "Step complete! Advancing to next wizard step...")
            try:
                ctx = await get_form_context(page)
                next_btn = await ctx.query_selector('button:has-text("Next"), input[value="Next"], button:has-text("Next Step"), button:has-text("Save and Continue")')
                if not next_btn and ctx != page:
                    next_btn = await page.query_selector('button:has-text("Next"), input[value="Next"], button:has-text("Next Step"), button:has-text("Save and Continue")')
                if next_btn and await next_btn.is_visible() and not await next_btn.is_disabled():
                    await next_btn.click(timeout=5000)
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        pass
                    await asyncio.sleep(2)
                    await self.log_app(app_id, "New step loaded. Scanning next page fields...")
                    return
            except Exception as e:
                print(f"Next button click failed: {e}")

        # 4. Final step reached: all wizard pages filled and verified
        screenshot = await self.capture_screenshot(page)
        await self.log_app(app_id, "All steps completed! Pausing for your final review and explicit approval.")
        update_application(app_id, {
            "status": "waiting_review",
            "review_screenshot_url": screenshot,
            "stage_message": "All steps filled and verified. Please review and click 'Approve & Submit' to finalize.",
            "verification_failed": 0
        })

    async def restore_standby_page(self, app: Dict[str, Any]):
        """
        Crash Recovery Standby:
        If worker restarted while a task was waiting for user input,
        proactively re-opens the browser on the app's dedicated page at the application URL,
        restores all previously filled fields from snapshot using executors, re-verifies them,
        and scrolls to the pending question.
        """
        app_id = app["id"]
        job_url = app["job_url"]
        persisted_form_data = app.get("form_data") or {"fields": {}}
        if not isinstance(persisted_form_data, dict):
            persisted_form_data = {"fields": {}}
        profile = get_profile()
        resume_path = profile.get("resume_file_path")
        unanswered = (app.get("unanswered_questions") or [app.get("unanswered_question") or {}])[0]

        try:
            page = await self.get_or_create_page(app_id)
            current_url = page.url if page else ""
            if not current_url or current_url == "about:blank" or (job_url.split("?")[0] not in current_url and current_url.split("?")[0] not in job_url):
                await self.log_app(app_id, f"[Crash Recovery] Proactively restoring browser session at waiting question...")
                await page.goto(job_url, timeout=45000, wait_until="domcontentloaded")
                await asyncio.sleep(2)
                ctx = await get_form_context(page)
                restore_res = await fast_restore_form_data(ctx, persisted_form_data, resume_path=resume_path)
                if restore_res.restored_count > 0:
                    await self.log_app(app_id, f"[Crash Recovery] Standby session ready with {restore_res.restored_count} verified field(s) restored.")
                if restore_res.failed_fields:
                    failed_names = [f.get("field") for f in restore_res.failed_fields]
                    await self.log_app(app_id, f"[Crash Recovery Warning] Re-verification failed for restored field(s): {failed_names}")

                field_id = unanswered.get("field_id")
                ans_lbl = unanswered.get("label", "")
                target_el = None
                if field_id:
                    try:
                        target_el = await ctx.query_selector(f'[id="{field_id}"]') or await ctx.query_selector(f'[name="{field_id}"]')
                    except Exception:
                        target_el = None
                if not target_el and ans_lbl:
                    try:
                        clean_lbl = ans_lbl.strip().lower()
                        for cand in await ctx.query_selector_all('input, select, textarea, [role="combobox"]'):
                            cand_lbl = (await get_element_label(ctx, cand)).strip().lower()
                            if cand_lbl and (cand_lbl == clean_lbl or clean_lbl in cand_lbl):
                                target_el = cand
                                break
                    except Exception:
                        pass
                if target_el:
                    try:
                        await target_el.scroll_into_view_if_needed()
                    except Exception:
                        pass
        except Exception as e:
            print(f"[Crash Recovery Standby] Error restoring standby page: {e}")

    async def submit_application(self, app: Dict[str, Any]):
        app_id = app["id"]

        # Check approval gate: block approval if verification failed until explicitly confirmed!
        if app.get("verification_failed") and not app.get("verification_confirmed"):
            await self.log_app(app_id, "[Submit Blocked] Approval blocked: Form fields failed verification re-read. Please confirm and approve explicitly.")
            update_application(app_id, {
                "status": "waiting_review",
                "stage_message": "Submit blocked: Please confirm fields that failed verification re-read before approving."
            })
            return

        await self.log_app(app_id, "Explicit user approval received. Submitting application now...")
        try:
            page = await self.get_or_create_page(app_id)
            ctx = await get_form_context(page)

            # Discover all interactive buttons and submit inputs inside the detected form context
            candidate_buttons = await ctx.query_selector_all('button, input[type="submit"], [role="button"]')
            if not candidate_buttons and ctx != page:
                candidate_buttons = await page.query_selector_all('button, input[type="submit"], [role="button"]')

            submit_btn = None
            best_score = -1

            OPEN_FORM_REGEX = re.compile(
                r'\b(apply\s+with|apply\s+for\s+this\s+job|start\s+application|open\s+application|sign\s+in\s+to\s+apply|apply\s+manually|apply\s+on\s+company\s+website|apply\s+online|autofill)\b',
                re.IGNORECASE
            )

            SUBMIT_EXACT_REGEX = re.compile(
                r'^(submit|submit\s+application|submit\s+job\s+application|send\s+application|complete\s+application|finish\s+application)$',
                re.IGNORECASE
            )

            SUBMIT_PARTIAL_REGEX = re.compile(
                r'\b(submit|submit\s+application|send\s+application|complete\s+application|finish\s+application)\b',
                re.IGNORECASE
            )

            for btn in candidate_buttons:
                try:
                    if not await btn.is_visible() or await btn.is_disabled():
                        continue

                    # Read text via inner_text, value attribute, aria-label
                    b_text = (await btn.inner_text() or "").strip()
                    if not b_text:
                        b_text = (await btn.get_attribute("value") or "").strip()
                    if not b_text:
                        b_text = (await btn.get_attribute("aria-label") or "").strip()

                    b_type = (await btn.get_attribute("type") or "").lower().strip()
                    b_role = (await btn.get_attribute("role") or "").lower().strip()

                    # Skip buttons that only open the form
                    if OPEN_FORM_REGEX.search(b_text):
                        continue

                    if b_text.lower() in ["apply", "apply now", "easy apply"]:
                        continue

                    score = 0
                    if SUBMIT_EXACT_REGEX.search(b_text):
                        score += 50
                    elif SUBMIT_PARTIAL_REGEX.search(b_text):
                        score += 30

                    if b_type == "submit":
                        score += 20
                    if b_role == "button":
                        score += 10

                    if score > best_score and score >= 30:
                        best_score = score
                        submit_btn = btn

                except Exception:
                    continue

            # Fallback if no text matched but an input[type="submit"] exists inside form
            if not submit_btn:
                form_submit = await ctx.query_selector('form input[type="submit"], form button[type="submit"]')
                if form_submit and await form_submit.is_visible() and not await form_submit.is_disabled():
                    submit_btn = form_submit

            if not submit_btn:
                await self.log_app(app_id, "[Submit Error] Submit button not found automatically inside form. Check laptop screen.")
                update_application(app_id, {
                    "status": "paused",
                    "stage_message": "Submit button not found automatically. Check laptop screen."
                })
                return

            # Capture screenshot BEFORE clicking
            screenshot_dir = os.path.abspath(settings.RESUME_STORAGE_DIR)
            os.makedirs(screenshot_dir, exist_ok=True)
            pre_screenshot_path = os.path.join(screenshot_dir, f"{app_id}_pre_submit.png")
            try:
                await page.screenshot(path=pre_screenshot_path, full_page=False)
                await self.log_app(app_id, f"Captured pre-submit screenshot: {pre_screenshot_path}")
            except Exception as ss_err:
                print(f"[Worker] Pre-submit screenshot error: {ss_err}")

            # Click submit button
            await submit_btn.scroll_into_view_if_needed()
            await submit_btn.click()
            await asyncio.sleep(3)

            try:
                await page.wait_for_load_state("domcontentloaded", timeout=6000)
            except Exception:
                pass

            # Capture screenshot AFTER clicking
            post_screenshot_path = os.path.join(screenshot_dir, f"{app_id}_post_submit.png")
            post_b64 = ""
            try:
                await page.screenshot(path=post_screenshot_path, full_page=False)
                post_b64 = await self.capture_screenshot(page)
                await self.log_app(app_id, f"Captured post-submit screenshot: {post_screenshot_path}")
            except Exception as ss_err:
                print(f"[Worker] Post-submit screenshot error: {ss_err}")

            await self.log_app(app_id, "Application submitted successfully!")
            update_application(app_id, {
                "status": "completed",
                "stage_message": "Submitted successfully!",
                "review_screenshot_url": post_b64,
                "pre_submit_screenshot": pre_screenshot_path,
                "post_submit_screenshot": post_screenshot_path
            })
        except Exception as e:
            await self.log_app(app_id, f"Error clicking submit button: {e}")
            screenshot = await self.capture_screenshot(page)
            update_application(app_id, {
                "status": "failed",
                "error_message": f"Submit error: {e}",
                "review_screenshot_url": screenshot
            })
        finally:
            # Ensure the application's page is closed on exit
            await self.close_page(app_id)

    async def run_loop(self):
        print("="*60)
        print("ApplyMate Local Background Worker Started (SQLite + Qwen3-0.6B)")
        print(f"SQLite DB: {settings.SQLITE_DB_PATH}")
        print(f"Local AI Endpoint: {settings.LLAMA_CPP_URL}")
        print("="*60)

        # Automatically recover any tasks left stuck in 'processing' from an earlier crash
        recovered = recover_stuck_processing_applications()
        if recovered > 0:
            print(f"[Worker Startup] Recovered {recovered} application(s) stuck in 'processing' from previous crash.")

        while self.is_running:
            try:
                await self.update_heartbeat(status="online")

                # Fetch all applications from local SQLite
                apps = get_applications()

                # 1. Check for 'submitting' tasks (User clicked Approve & Submit) - Oldest First
                submitting_tasks = [a for a in apps if a.get("status") == "submitting"]
                if submitting_tasks:
                    submitting_tasks.sort(key=lambda x: x.get("created_at") or "")
                    await self.submit_application(submitting_tasks[0])
                    # Minimal pause after immediate work to prevent unnecessary wait
                    await asyncio.sleep(0.1)
                    continue

                # 2. Check for answered batch questions on 'needs_input' tasks - Oldest First
                # STRICT GUARD: ONLY touch if the user has provided batch answers!
                answered_tasks = [
                    a for a in apps 
                    if a.get("status") == "needs_input" and (bool(a.get("batch_answers")) or bool(a.get("user_answer")))
                ]
                if answered_tasks:
                    answered_tasks.sort(key=lambda x: x.get("created_at") or "")
                    await self.process_task(answered_tasks[0])
                    # Minimal pause after immediate work to prevent unnecessary wait
                    await asyncio.sleep(0.1)
                    continue

                # 3. Check for 'queued' tasks - Atomically acquire OLDEST FIRST!
                # STRICT GUARD: Never touch applications in 'needs_input', 'paused', or 'waiting_review'
                task_to_run = acquire_queued_application(self.worker_id)
                if task_to_run:
                    await self.process_task(task_to_run)
                    # Minimal pause after immediate work to prevent unnecessary wait
                    await asyncio.sleep(0.1)
                    continue

                # Applications in needs_input (waiting for user answer), paused, or waiting_review
                # are NEVER touched by the worker loop!

            except Exception as e:
                print(f"[Worker Exception] {e}")
                await asyncio.sleep(3)

            await asyncio.sleep(settings.POLL_INTERVAL)

async def main_worker_entry():
    worker = ApplyMateWorker()
    try:
        await worker.run_loop()
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("\n[Worker] Shutdown signal received. Performing graceful resource cleanup...")
    finally:
        worker.is_running = False
        await worker.close_browser()
        print("[Worker] Browser, context, and Playwright subprocess cleanly terminated.")

if __name__ == "__main__":
    try:
        asyncio.run(main_worker_entry())
    except KeyboardInterrupt:
        pass
