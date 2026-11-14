import os
import re
import asyncio
from typing import Dict, Any, List, Optional
from playwright.async_api import Page, Frame, ElementHandle
from field_rules import get_field_match_by_rules
from ai_client import ai_interpret_field, ai_match_saved_answers, ai_generate_tailored_answer

async def detect_security_checkpoint(page: Page) -> bool:
    """
    Check if an active, blocking CAPTCHA challenge or verification modal is actually blocking the page.
    Filters out background reCAPTCHA badge iframes and invisible tokens.
    """
    try:
        captcha_frames = await page.query_selector_all(
            'iframe[src*="recaptcha/enterprise/bframe"], iframe[src*="recaptcha/api2/bframe"], iframe[src*="hcaptcha.com/box"]'
        )
        for frame in captcha_frames:
            if await frame.is_visible():
                box = await frame.bounding_box()
                if box and box["width"] > 200 and box["height"] > 200:
                    return True

        visible_blocks = await page.query_selector_all(
            'h1, h2, h3, .challenge-container, #cf-challenge-running, [id*="captcha"]'
        )
        for el in visible_blocks:
            if await el.is_visible():
                txt = (await el.inner_text() or "").lower()
                if any(w in txt for w in ["verify you are human", "enter the verification code", "one-time password", "security check"]):
                    return True
    except Exception:
        pass

    return False

async def get_form_context(page: Page):
    """
    Detect whether the application form is hosted on the top page
    or inside an embedded ATS iframe (Greenhouse, Lever, Workday, etc.).
    Prioritizes embedded ATS frames with actual application fields over
    host-page marketing forms (e.g. newsletter signups).
    """
    best_context = page.main_frame
    max_app_score = -1

    for f in page.frames:
        url = f.url.lower()
        # Skip security, verification, and analytics iframes
        if any(skip in url for skip in ["recaptcha", "google.com", "hcaptcha", "challenges.cloudflare.com", "doubleclick", "tagmanager", "compliance"]):
            continue

        try:
            inputs = await f.query_selector_all('input:not([type="hidden"]), select, [role="combobox"], textarea')
            count = len(inputs)
            if count == 0:
                continue

            # Calculate application relevance score
            is_ats = any(w in url for w in ["greenhouse", "lever.co", "workday", "smartrecruiters", "ashby", "job_app", "keka"])
            score = count * (5 if is_ats else 1)

            # Bonus for core job application field signatures
            for inp in inputs[:10]:
                id_name = (await inp.get_attribute("id") or "") + " " + (await inp.get_attribute("name") or "")
                id_name = id_name.lower()
                if any(k in id_name for k in ["resume", "cv", "first_name", "last_name", "candidate", "cover_letter"]):
                    score += 20

            if score > max_app_score:
                max_app_score = score
                best_context = f
        except Exception:
            continue

    return best_context if best_context != page.main_frame else page

async def is_date_picker_element(ctx, el: ElementHandle, label: str) -> bool:
    """Detect if an element is a native date input or custom calendar picker."""
    try:
        inp_type = (await el.get_attribute("type") or "").lower()
        if inp_type in ["date", "month", "datetime-local"]:
            return True
        el_class = (await el.get_attribute("class") or "").lower()
        if any(dp in el_class for dp in ["datepicker", "hasdatepicker", "date-picker", "flatpickr", "p-datepicker", "bs-datepicker"]):
            return True
        parent_class = await el.evaluate("e => e.parentElement ? e.parentElement.className.toLowerCase() : ''")
        if any(dp in parent_class for dp in ["calendar", "datepicker", "date"]):
            return True
        el_id = (await el.get_attribute("id") or "").lower()
        el_name = (await el.get_attribute("name") or "").lower()
        lbl = label.lower()
        if any(w in el_id or w in el_name or w in lbl for w in ["dateofjoining", "dateofrelieving", "joiningdate", "startdate", "enddate", "birthdate", "graduationdate"]):
            return True
    except Exception:
        pass
    return False

async def set_date_on_element(el: ElementHandle, date_str: str) -> bool:
    """Universal date setter supporting jQuery UI datepicker, Flatpickr, native date, and formatted text."""
    try:
        await el.evaluate("""(e, rawDate) => {
            let d = new Date(rawDate);
            if (isNaN(d.getTime())) {
                d = new Date();
            }

            // 1. jQuery UI Datepicker
            if (window.jQuery && window.jQuery(e).data('datepicker')) {
                try {
                    window.jQuery(e).datepicker('setDate', d);
                    e.dispatchEvent(new Event('change', { bubbles: true }));
                    return;
                } catch(err) {}
            }

            // 2. Flatpickr
            if (e._flatpickr) {
                try {
                    e._flatpickr.setDate(d, true);
                    return;
                } catch(err) {}
            }

            // 3. Fallback: remove readonly, format value based on element hints, and dispatch events
            e.removeAttribute('readonly');
            let months = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
            let mName = months[d.getMonth()];
            let yr = d.getFullYear();
            let day = ('0' + d.getDate()).slice(-2);

            let ph = (e.placeholder || '').toLowerCase();
            if (ph.includes('june') || ph.includes('month') || ph.includes('yy')) {
                e.value = `${mName} ${yr}`;
            } else if (e.type === 'date') {
                e.value = `${yr}-${('0' + (d.getMonth() + 1)).slice(-2)}-${day}`;
            } else {
                e.value = `${day}/${('0' + (d.getMonth() + 1)).slice(-2)}/${yr}`;
            }

            e.dispatchEvent(new Event('input', { bubbles: true }));
            e.dispatchEvent(new Event('change', { bubbles: true }));
        }""", date_str)
        return True
    except Exception as e:
        print(f"Error setting date on element: {e}")
        return False

async def get_element_label(ctx, el: ElementHandle) -> str:
    """
    Comprehensive label resolver supporting:
    1. aria-labelledby (resolves multiple IDs)
    2. aria-label
    3. <label for="id">
    4. Wrapping ancestor <label>
    5. fieldset > legend (crucial for radio & checkbox groups)
    6. Form-group / question container headers (.field-label, .question-title, etc.)
    7. Placeholder
    8. Preceding sibling or name attribute
    """
    try:
        # 1. Check aria-labelledby
        aria_labelledby = await el.get_attribute("aria-labelledby")
        if aria_labelledby:
            parts = []
            for lid in aria_labelledby.split():
                lbl_target = await ctx.query_selector(f'#{lid}')
                if lbl_target:
                    txt = (await lbl_target.inner_text() or "").strip()
                    if txt and not any(skip in txt.lower() for skip in ["error", "required", "select..."]):
                        parts.append(txt)
            if parts:
                return " ".join(parts).rstrip("*").strip()

        # 2. Check aria-label
        aria_label = await el.get_attribute("aria-label")
        if aria_label and aria_label.strip():
            return aria_label.strip().rstrip("*").strip()

        # 3. Check id match in <label for="...">
        el_id = await el.get_attribute("id")
        if el_id:
            label_el = await ctx.query_selector(f'label[for="{el_id}"]')
            if label_el:
                txt = (await label_el.inner_text() or "").strip()
                if txt:
                    return txt.rstrip("*").strip()

        # 4. Check wrapping ancestor label
        parent_lbl = await el.query_selector("xpath=ancestor::label")
        if parent_lbl:
            txt = (await parent_lbl.inner_text() or "").strip()
            if txt:
                return txt.rstrip("*").strip()

        # 5. Check fieldset > legend (for radio and checkbox groups)
        fieldset = await el.query_selector("xpath=ancestor::fieldset")
        if fieldset:
            legend = await fieldset.query_selector("legend")
            if legend:
                txt = (await legend.inner_text() or "").strip()
                if txt:
                    return txt.rstrip("*").strip()

        # 6. Check question container header or label
        container = await el.query_selector("xpath=ancestor::div[contains(@class, 'field') or contains(@class, 'form-group') or contains(@class, 'question') or contains(@class, 'select__container')]")
        if container:
            lbl = await container.query_selector("label, legend, .label, [class*='label'], [class*='title'], [class*='header']")
            if lbl:
                txt = (await lbl.inner_text() or "").strip()
                if txt and not any(skip in txt.lower() for skip in ["select...", "error"]):
                    return txt.rstrip("*").strip()

        # 7. Check placeholder
        placeholder = await el.get_attribute("placeholder")
        if placeholder and placeholder.strip() and not placeholder.strip().lower().startswith("select"):
            return placeholder.strip().rstrip("*").strip()

        # 8. Check name attribute
        name = await el.get_attribute("name")
        if name and name.strip():
            clean_name = re.sub(r'[_\-\[\]]', ' ', name).strip().title()
            return clean_name
    except Exception:
        pass

    return ""

def is_intent_plausible(intent: str, label: str) -> bool:
    """Validate that AI intent is linguistically plausible for the label."""
    lbl = label.lower()
    if intent in ["first_name", "last_name", "full_name"]:
        return bool(re.search(r'\b(name|applicant|candidate|given|surname|family)\b', lbl))
    if intent == "email":
        return "mail" in lbl
    if intent == "phone":
        return bool(re.search(r'\b(phone|mobile|cell|tel|number)\b', lbl))
    if intent == "location":
        return bool(re.search(r'\b(location|city|country|state|address|zip|postal)\b', lbl))
    if intent in ["linkedin", "github", "portfolio"]:
        return any(w in lbl for w in ["link", "url", "web", "site", "git", "portfolio", "social"])
    return False

async def map_intent_to_profile(intent: str, profile: Dict[str, Any]) -> Optional[str]:
    """Map validated field intent to candidate profile facts."""
    full_name = profile.get("full_name", "")
    parts = full_name.split()
    first_name = parts[0] if parts else ""
    last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

    mapping = {
        "full_name": full_name,
        "first_name": first_name,
        "last_name": last_name or first_name,
        "email": profile.get("email", ""),
        "phone": profile.get("phone", ""),
        "location": profile.get("location", ""),
        "linkedin": profile.get("linkedin_url", ""),
        "github": profile.get("github_url", ""),
        "portfolio": profile.get("portfolio_url", "") or profile.get("linkedin_url", ""),
        "optional_notes": profile.get("summary", "") or "N/A"
    }
    return mapping.get(intent)

def is_sensitive_question(label: str) -> bool:
    """Detect whether a question involves sensitive compliance, legal, or compensation terms."""
    lbl = label.lower()
    sensitive_keywords = [
        "salary", "compensation", "pay", "rate",
        "visa", "sponsorship", "authorized to work", "work authorization", "citizenship",
        "disability", "veteran", "gender", "race", "ethnicity", "sexual orientation",
        "criminal", "felony", "misdemeanor", "background check", "drug test",
        "consent", "agreement", "acknowledge"
    ]
    return any(k in lbl for k in sensitive_keywords)

async def check_validation_errors(ctx) -> List[str]:
    """Check for active validation error messages on the page after filling."""
    errors = []
    try:
        error_els = await ctx.query_selector_all(
            '[aria-invalid="true"], .error-message, .field-error, .help-block-error, [class*="errorMessage"], [class*="error-text"]'
        )
        for el in error_els:
            if await el.is_visible():
                txt = (await el.inner_text() or "").strip()
                if txt and txt not in errors:
                    errors.append(txt)
    except Exception:
        pass
    return errors

async def fill_form_on_page(
    page: Page,
    profile: Dict[str, Any],
    saved_answers: List[Dict[str, Any]],
    resume_path: Optional[str] = None,
    job_context: str = ""
) -> Dict[str, Any]:
    """
    Intelligent Form Automation Engine (Sequential Top-to-Bottom Scraper):
    Scrapes and fills every form element one by one in natural document order:
      1. File inputs (resume & portfolio)
      2. Custom comboboxes & Typeahead dropdowns
      3. Native <select> elements (Select2 & standard)
      4. Radio button groups & checkboxes
      5. Datepickers & Calendar inputs
      6. Text, email, phone, and textarea fields
    Maintains exact top-to-bottom visual order, preventing bottom fields from jumping ahead.
    """
    ctx = await get_form_context(page)

    result = {
        "completed": True,
        "needs_input": False,
        "unanswered_question": None,
        "ai_suggestion": "",
        "fields_filled": [],
        "has_next_page": False,
        "submit_button_found": False,
        "validation_errors": []
    }

    # Query all interactive form controls in natural top-to-bottom DOM order
    elements = await ctx.query_selector_all('input, select, textarea, [role="combobox"]')
    processed_radio_groups = set()

    for el_idx, el in enumerate(elements):
        try:
            tag = await el.evaluate("e => e.tagName.toLowerCase()")
            inp_type = (await el.get_attribute("type") or "").lower()
            role = (await el.get_attribute("role") or "").lower()
            el_id = (await el.get_attribute("id") or "").lower()
            name = await el.get_attribute("name") or ""
            el_class = (await el.get_attribute("class") or "").lower()

            # Filter non-input / non-interactive buttons
            if inp_type in ["submit", "button", "reset", "hidden"] and inp_type != "file":
                continue
            if "search-input" in el_id or "iti-" in el_id or "select2-selection" in el_class or "recaptcha" in el_id:
                continue

            # Elements must be visible unless file input or select (Select2 hides native selects)
            if not await el.is_visible() and inp_type != "file" and tag != "select":
                continue

            # Radio group deduplication: handle the group on the first radio encountered
            if inp_type == "radio":
                if name and name in processed_radio_groups:
                    continue
                if name:
                    processed_radio_groups.add(name)

            # Resolve human-readable label
            label = await get_element_label(ctx, el)
            if not label or label.lower() in ["select", "select an option", "+91"]:
                clean_name = name.split(".")[-1] if name else el_id
                if "countrycode" in clean_name.lower():
                    label = "Country Code"
                elif "currency" in clean_name.lower():
                    label = "Currency"
                elif "gender" in clean_name.lower():
                    label = "Gender"
                elif "locationpreference" in clean_name.lower() or "preferredlocation" in clean_name.lower():
                    label = "Preferred Location"
                elif "experience" in clean_name.lower() or "workexperience" in clean_name.lower():
                    label = "Experience (in years)"
                elif clean_name:
                    label = clean_name.replace("_", " ").strip().title()

            if not label:
                continue

            # =========================================================================
            # 1. FILE UPLOAD (Resume / Additional Documents)
            # =========================================================================
            if inp_type == "file":
                if resume_path and os.path.exists(resume_path):
                    try:
                        await el.set_input_files(resume_path)
                        result["fields_filled"].append({"field": label or "Resume Upload", "value": os.path.basename(resume_path)})
                    except Exception as e:
                        print(f"File upload error on '{label}': {e}")
                continue

            # =========================================================================
            # 2. CUSTOM COMBOBOXES & REACT/GREENHOUSE TYPEAHEADS
            # =========================================================================
            if role == "combobox" or (inp_type == "text" and await el.query_selector("xpath=ancestor::div[contains(@class, 'select')]")):
                cb_txt = (await el.inner_text() or "").strip()
                if cb_txt in ["+91", "INR", "USD"] or (cb_txt == "Select" and any(w in label.lower() for w in ["phone", "code", "dial"])):
                    continue

                parent_container = await el.query_selector("xpath=ancestor::div[contains(@class, 'select')]")
                if parent_container:
                    val_container = await parent_container.query_selector("[class*='singleValue'], [class*='single-value'], .select2-selection__rendered")
                    curr_txt = (await val_container.inner_text() or "").strip() if val_container else ""
                    if curr_txt and curr_txt not in ["Select...", "", "Select", "Select an option"]:
                        continue

                # A. Country combobox
                is_country_field = bool(re.search(r'^(country|your\s*country|country\s*[\/\-]\s*region|country\s*of\s*residence)$', label.lower().strip()))
                if is_country_field:
                    country_val = "India"
                    if profile.get("location") and "india" in profile.get("location", "").lower():
                        country_val = "India"
                    control = await parent_container.query_selector(".select__control") if parent_container else None
                    if control:
                        await control.click()
                        await asyncio.sleep(0.3)
                        opt_match = await ctx.query_selector(f'.select__option:has-text("{country_val}")') or await page.query_selector(f'.select__option:has-text("{country_val}")')
                        if opt_match:
                            await opt_match.click()
                            result["fields_filled"].append({"field": label, "value": country_val})
                            await asyncio.sleep(0.8)
                            continue

                # B. Location / City typeahead
                is_location_field = bool(re.search(r'^(location|current\s*location|location\s*\(city\)|city|address)$', label.lower().strip())) or (
                    any(w in label.lower() for w in ["location", "city"]) and not any(w in label.lower() for w in ["willing", "relocate", "prefer", "able to work", "office", "authorized", "require"])
                )
                if is_location_field:
                    loc_val = profile.get("location") or "Kochi, Kerala, India"
                    city_search = loc_val.split(",")[0].strip() if "," in loc_val else loc_val
                    try:
                        control = await parent_container.query_selector(".select__control") if parent_container else None
                        if control:
                            await control.click()
                            await asyncio.sleep(0.3)
                        await el.focus()
                        await el.type(city_search, delay=80)
                        await asyncio.sleep(1.2)
                        opt_match = await ctx.query_selector(f'.select__option:has-text("{city_search}")')
                        if not opt_match:
                            opt_match = await page.query_selector(f'.select__option:has-text("{city_search}")')
                        if opt_match:
                            matched_text = (await opt_match.inner_text() or "").strip()
                            await opt_match.click()
                            result["fields_filled"].append({"field": label, "value": matched_text or loc_val})
                        else:
                            await page.keyboard.press("ArrowDown")
                            await page.keyboard.press("Enter")
                            result["fields_filled"].append({"field": label, "value": loc_val})
                        await asyncio.sleep(0.4)
                        continue
                    except Exception as e:
                        print(f"Error autofilling location combobox: {e}")

                # C. Check deterministic rules & saved answers
                matched_val = get_field_match_by_rules(label, profile)
                if not matched_val:
                    matched_val = await ai_match_saved_answers(label, saved_answers, company_name=job_context)

                # D. Open dropdown popup & extract options
                options = []
                control = await parent_container.query_selector(".select__control") if parent_container else None
                trigger = control or el

                if trigger:
                    try:
                        await trigger.click()
                        await asyncio.sleep(0.4)

                        aria_controls = await el.get_attribute("aria-controls")
                        listbox = await ctx.query_selector(f"#{aria_controls}") if aria_controls else None
                        if not listbox and aria_controls:
                            listbox = await page.query_selector(f"#{aria_controls}")
                        if not listbox:
                            aria_owns = await el.get_attribute("aria-owns")
                            listbox = await ctx.query_selector(f"#{aria_owns}") if aria_owns else None
                            if not listbox and aria_owns:
                                listbox = await page.query_selector(f"#{aria_owns}")
                        if not listbox:
                            listbox = await ctx.query_selector('.select__menu:not([class*="control"]), [role="listbox"]:not(.iti__country-list):not([class*="iti"]), [class*="MuiAutocomplete-popper"], [class*="ant-select-dropdown"]') or await page.query_selector('.select__menu:not([class*="control"]), [role="listbox"]:not(.iti__country-list):not([class*="iti"]), [class*="MuiAutocomplete-popper"], [class*="ant-select-dropdown"]')

                        if listbox:
                            opt_els = await listbox.query_selector_all('[role="option"], .select__option')
                            for o in opt_els:
                                t = (await o.inner_text() or "").strip()
                                if t and t not in options and not "select" in t.lower():
                                    options.append(t)

                        if not options and matched_val:
                            await el.fill(str(matched_val)[:3])
                            await asyncio.sleep(0.5)
                            if listbox:
                                opt_els = await listbox.query_selector_all('[role="option"], .select__option')
                                for o in opt_els:
                                    t = (await o.inner_text() or "").strip()
                                    if t and t not in options:
                                        options.append(t)
                    except Exception as e:
                        print(f"Error opening combobox: {e}")

                # E. If matched_val is known, select it
                if matched_val and options:
                    for o_text in options:
                        if o_text.lower() == str(matched_val).strip().lower():
                            matched_val = o_text
                            break
                    m_lower = str(matched_val).strip().lower()
                    if m_lower.startswith("no"):
                        for o_text in options:
                            if o_text.lower() == "no":
                                matched_val = o_text
                                break
                    elif m_lower.startswith("yes"):
                        for o_text in options:
                            if o_text.lower() == "yes":
                                matched_val = o_text
                                break

                    opt_match = None
                    if listbox:
                        opt_match = await listbox.query_selector(f'[role="option"]:has-text("{matched_val}"), .select__option:has-text("{matched_val}")')
                    if not opt_match:
                        opt_match = await ctx.query_selector(f'[role="option"]:has-text("{matched_val}"), .select__option:has-text("{matched_val}")') or await page.query_selector(f'[role="option"]:has-text("{matched_val}"), .select__option:has-text("{matched_val}")')

                    if opt_match:
                        await opt_match.click()
                        await asyncio.sleep(0.3)
                        result["fields_filled"].append({"field": label, "value": str(matched_val)})
                        continue
                    else:
                        # Virtualized list handling: type into combobox to filter DOM, or scroll popup
                        try:
                            await el.fill(str(matched_val))
                            await asyncio.sleep(0.3)
                            # Re-check if filtering revealed the option
                            if listbox:
                                opt_match = await listbox.query_selector(f'[role="option"]:has-text("{matched_val}"), .select__option:has-text("{matched_val}")')
                            if not opt_match:
                                opt_match = await ctx.query_selector(f'[role="option"]:has-text("{matched_val}"), .select__option:has-text("{matched_val}")') or await page.query_selector(f'[role="option"]:has-text("{matched_val}"), .select__option:has-text("{matched_val}")')

                            if opt_match:
                                await opt_match.click()
                                await asyncio.sleep(0.3)
                                result["fields_filled"].append({"field": label, "value": str(matched_val)})
                                continue

                            # If still not found, scroll virtualized list container
                            if listbox:
                                for scroll_ratio in [0.25, 0.5, 0.75, 1.0]:
                                    await listbox.evaluate(f"(box) => box.scrollTop = box.scrollHeight * {scroll_ratio}")
                                    await asyncio.sleep(0.15)
                                    opt_match = await listbox.query_selector(f'[role="option"]:has-text("{matched_val}"), .select__option:has-text("{matched_val}")')
                                    if opt_match:
                                        await opt_match.click()
                                        await asyncio.sleep(0.3)
                                        result["fields_filled"].append({"field": label, "value": str(matched_val)})
                                        break
                                if opt_match:
                                    continue

                            # Fallback: keyboard navigation
                            await page.keyboard.press("ArrowDown")
                            await page.keyboard.press("Enter")
                            await asyncio.sleep(0.3)
                            result["fields_filled"].append({"field": label, "value": str(matched_val)})
                            continue
                        except Exception as e:
                            print(f"Error selecting virtualized option for {label}: {e}")

                await page.keyboard.press("Escape")
                await asyncio.sleep(0.2)

                # F. Unknown Combobox Question -> Pause for User Input
                field_id = await el.get_attribute("id") or label
                is_boolean_question = bool(re.search(r'^(are|do|will|have|is|can|should|would|did|was|were)\b|\b(yes\/no)\b', label.strip(), re.I))

                if options:
                    print(f"[AI Reasoning] Querying Qwen with options for '{label[:60]}...'")
                    ai_decision = await ai_generate_tailored_answer(label, profile, job_context=job_context, options=options)
                    print(f"[AI Decision] Qwen chose: '{ai_decision}' from {options}")

                    result["needs_input"] = True
                    result["unanswered_question"] = {
                        "field_id": field_id,
                        "label": label,
                        "type": "select",
                        "options": options
                    }
                    result["ai_suggestion"] = ai_decision or (options[0] if options else "")
                    result["completed"] = False
                    return result
                elif is_boolean_question:
                    fallback_options = ["Yes", "No"]
                    ai_decision = await ai_generate_tailored_answer(label, profile, job_context=job_context, options=fallback_options)
                    result["needs_input"] = True
                    result["unanswered_question"] = {
                        "field_id": field_id,
                        "label": label,
                        "type": "select",
                        "options": fallback_options
                    }
                    result["ai_suggestion"] = ai_decision or "Yes"
                    result["completed"] = False
                    return result
                else:
                    ai_decision = await ai_generate_tailored_answer(label, profile, job_context=job_context) or ""
                    result["needs_input"] = True
                    result["unanswered_question"] = {
                        "field_id": field_id,
                        "label": label,
                        "type": "text",
                        "options": []
                    }
                    result["ai_suggestion"] = ai_decision
                    result["completed"] = False
                    return result

            # =========================================================================
            # 3. NATIVE SELECT DROPDOWNS (<select>)
            # =========================================================================
            if tag == "select":
                selected_text = await el.evaluate("sel => sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text.trim() : ''")
                if selected_text and selected_text.lower() not in ["select", "select an option", "select...", "-- select --", "choose", ""]:
                    continue

                options = await el.query_selector_all('option')
                opt_texts = []
                for opt in options:
                    txt = await opt.inner_text()
                    if txt and txt.strip() and not any(skip in txt.lower() for skip in ["select an option", "select...", "-- select --"]):
                        opt_texts.append(txt.strip())

                # A. Phone Country Code
                is_phone_dial_code = "countrycode" in el_id or "countrycode" in name.lower() or any(w in label.lower() for w in ["country code", "dial code", "+91"]) or (opt_texts and any(ot in ["+91", "+93"] for ot in opt_texts[:10]))
                if is_phone_dial_code:
                    await el.evaluate("""(sel) => {
                        let match = null;
                        for (let o of sel.options) {
                            if (o.value === '+91' || o.text.includes('+91') || o.text.toLowerCase().includes('india')) {
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
                    }""")
                    result["fields_filled"].append({"field": label or "Country Code", "value": "+91"})
                    continue

                # B. Currency
                is_currency = "currency" in el_id or "currency" in name.lower() or (opt_texts and any(ot in ["INR", "USD", "EUR"] for ot in opt_texts[:10]))
                if is_currency:
                    await el.evaluate("""(sel) => {
                        let match = null;
                        for (let o of sel.options) {
                            if (o.value === 'INR' || o.text.includes('INR') || o.text.toLowerCase().includes('rupee')) {
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
                    }""")
                    result["fields_filled"].append({"field": label or "Currency", "value": "INR"})
                    continue

                # C. Rules & Reusable Memory match
                matched_val = get_field_match_by_rules(label, profile)
                if not matched_val and opt_texts:
                    matched_val = await ai_match_saved_answers(label, saved_answers, company_name=job_context)

                if matched_val:
                    m_lower = str(matched_val).strip().lower()
                    for ot in opt_texts:
                        if ot.lower() == m_lower or (m_lower == "yes" and ot.lower() == "yes") or (m_lower == "no" and ot.lower() == "no"):
                            matched_val = ot
                            break

                    applied = await el.evaluate("""(sel, target) => {
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
                            return true;
                        }
                        return false;
                    }""", str(matched_val))

                    if applied:
                        result["fields_filled"].append({"field": label, "value": str(matched_val)})
                        continue
                    else:
                        try:
                            await el.select_option(label=str(matched_val), timeout=2000)
                            result["fields_filled"].append({"field": label, "value": str(matched_val)})
                            continue
                        except Exception:
                            pass

                # D. Pause for unconfirmed select dropdown
                ai_decision = ""
                if opt_texts:
                    opt_texts_to_pass = opt_texts[:20]
                    ai_decision = await ai_generate_tailored_answer(label, profile, job_context=job_context, options=opt_texts_to_pass)

                field_id = await el.get_attribute("id") or name or label
                result["needs_input"] = True
                result["unanswered_question"] = {
                    "field_id": field_id,
                    "label": label,
                    "type": "select",
                    "options": opt_texts[:25]
                }
                result["ai_suggestion"] = ai_decision or (opt_texts[0] if opt_texts else "")
                result["completed"] = False
                return result

            # =========================================================================
            # 4. RADIO BUTTON GROUPS
            # =========================================================================
            if inp_type == "radio":
                group_radios = await ctx.query_selector_all(f'input[type="radio"][name="{name}"]') if name else [el]
                radio_options = []
                for r_el in group_radios:
                    r_lbl = await get_element_label(ctx, r_el)
                    if r_lbl and r_lbl != label:
                        radio_options.append(r_lbl)

                # Check if already answered
                any_checked = False
                for r_el in group_radios:
                    if await r_el.is_checked():
                        any_checked = True
                        break
                if any_checked:
                    continue

                matched_val = get_field_match_by_rules(label, profile)
                if not matched_val:
                    matched_val = await ai_match_saved_answers(label, saved_answers, company_name=job_context)

                if matched_val:
                    m_lower = str(matched_val).strip().lower()
                    for r_el in group_radios:
                        r_lbl = await get_element_label(ctx, r_el)
                        if r_lbl.lower() == m_lower or (m_lower == "yes" and r_lbl.lower().startswith("yes")) or (m_lower == "no" and r_lbl.lower().startswith("no")):
                            await r_el.click()
                            result["fields_filled"].append({"field": label, "value": r_lbl})
                            break
                    continue

                # Pause on unknown radio group
                ai_decision = await ai_generate_tailored_answer(label, profile, job_context=job_context, options=radio_options or ["Yes", "No"])
                field_id = await el.get_attribute("id") or name or label
                result["needs_input"] = True
                result["unanswered_question"] = {
                    "field_id": field_id,
                    "label": label,
                    "type": "select",
                    "options": radio_options or ["Yes", "No"]
                }
                result["ai_suggestion"] = ai_decision or "Yes"
                result["completed"] = False
                return result

            # =========================================================================
            # 5. CHECKBOXES (Consent, Terms, Acknowledgment)
            # =========================================================================
            if inp_type == "checkbox":
                if await el.is_checked():
                    continue
                lbl_lower = label.lower()
                is_consent = any(w in lbl_lower for w in ["agree", "consent", "acknowledge", "certify", "confirm", "terms", "policy", "truthful"])
                if is_consent:
                    await el.check()
                    result["fields_filled"].append({"field": label, "value": "Checked (Consent)"})
                    continue

                matched_val = get_field_match_by_rules(label, profile)
                if not matched_val:
                    matched_val = await ai_match_saved_answers(label, saved_answers, company_name=job_context)

                if str(matched_val).lower() in ["yes", "true", "check", "1"]:
                    await el.check()
                    result["fields_filled"].append({"field": label, "value": "Checked"})
                    continue

                # If required and uncertain, prompt user
                is_required = await el.get_attribute("required") is not None or await el.get_attribute("aria-required") == "true"
                if is_required:
                    field_id = await el.get_attribute("id") or name or label
                    result["needs_input"] = True
                    result["unanswered_question"] = {
                        "field_id": field_id,
                        "label": label,
                        "type": "select",
                        "options": ["Yes", "No"]
                    }
                    result["ai_suggestion"] = "Yes"
                    result["completed"] = False
                    return result
                continue

            # =========================================================================
            # 6. DATEPICKERS & CALENDAR INPUTS
            # =========================================================================
            is_date = await is_date_picker_element(ctx, el, label)
            if is_date:
                curr_val = await el.input_value()
                if curr_val and curr_val.strip():
                    continue

                matched_val = get_field_match_by_rules(label, profile)
                if not matched_val:
                    matched_val = await ai_match_saved_answers(label, saved_answers, company_name=job_context)

                if matched_val:
                    await set_date_on_element(el, str(matched_val))
                    result["fields_filled"].append({"field": label, "value": str(matched_val)})
                    continue

                # Prompt user with HTML5 interactive datepicker
                field_id = await el.get_attribute("id") or name or label
                suggested_date = "2024-05-15"
                if "reliev" in label.lower() or "end" in label.lower():
                    suggested_date = "2024-08-15"
                elif "birth" in label.lower() or "dob" in label.lower():
                    suggested_date = "2004-10-03"

                result["needs_input"] = True
                result["unanswered_question"] = {
                    "field_id": field_id,
                    "label": label,
                    "type": "date",
                    "options": []
                }
                result["ai_suggestion"] = suggested_date
                result["completed"] = False
                return result

            # =========================================================================
            # 7. TEXT, EMAIL, PHONE, AND TEXTAREA INPUTS
            # =========================================================================
            if tag in ["input", "textarea"]:
                # Skip elements inside custom select controls
                if await el.query_selector('xpath=ancestor::div[contains(@class, "select")]'):
                    continue

                curr_val = await el.input_value()
                if curr_val and curr_val.strip():
                    continue

                matched_value = get_field_match_by_rules(label, profile)

                # Split phone number handling: avoid duplicate country codes
                if matched_value and any(w in label.lower() for w in ["phone", "mobile", "cell"]):
                    has_iti = bool(await ctx.query_selector('.iti__selected-dial-code, [class*="iti__selected"]'))
                    if has_iti:
                        matched_value = re.sub(r'^\+\d{1,3}\s*', '', str(matched_value)).strip()

                if not matched_value:
                    tag_name = await el.evaluate("e => e.tagName.toLowerCase()")
                    field_intel = await ai_interpret_field(label, tag_type=tag_name)
                    intent = field_intel.get("intent", "custom_question")

                    if is_intent_plausible(intent, label):
                        matched_value = await map_intent_to_profile(intent, profile)

                    if not matched_value:
                        matched_value = await ai_match_saved_answers(label, saved_answers, company_name=job_context)

                    if not matched_value and not is_sensitive_question(label) and not field_intel.get("is_sensitive", False):
                        matched_value = await ai_generate_tailored_answer(label, profile, job_context=job_context)

                if matched_value:
                    await el.fill(str(matched_value))
                    result["fields_filled"].append({"field": label, "value": str(matched_value)})
                    continue

                # Skip optional notes
                is_optional = any(word in label.lower() for word in ["optional", "not required", "if you would like"])
                if is_optional:
                    continue

                # Pause on unknown text / custom questions
                field_id = await el.get_attribute("id") or name or label
                suggested_ans = await ai_generate_tailored_answer(label, profile, job_context=job_context) or ""

                result["needs_input"] = True
                result["unanswered_question"] = {
                    "field_id": field_id,
                    "label": label,
                    "type": "text",
                    "options": []
                }
                result["ai_suggestion"] = suggested_ans
                result["completed"] = False
                return result

        except Exception as e:
            print(f"Error handling form element [{el_idx}]: {e}")
            continue

    # POST-FILL VALIDATION & REQUIRED-FIELD VERIFICATION
    errors = await check_validation_errors(ctx)
    if errors:
        result["validation_errors"] = errors
        print(f"[Form Validation] Field errors detected on page: {errors}")

    # MULTI-STEP WIZARD & ACCIDENTAL SUBMIT GUARD
    buttons = await ctx.query_selector_all('button, input[type="submit"]')
    for btn in buttons:
        try:
            if not await btn.is_visible() or await btn.is_disabled():
                continue
            txt = (await btn.inner_text() or await btn.get_attribute("value") or "").lower().strip()
            # Only trigger multi-page if it's explicitly an enabled next/step button
            if any(w == txt or f" {w}" in txt or f"{w} " in txt for w in ["next", "step 2", "save and continue", "next page", "next step", "continue to next"]):
                result["has_next_page"] = True
                break
            if any(w in txt for w in ["submit", "apply now", "finish", "complete application", "submit application"]):
                result["submit_button_found"] = True
        except Exception:
            pass

    return result
