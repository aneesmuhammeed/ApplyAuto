import os
import re
import time
import asyncio
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from playwright.async_api import Page, Frame, ElementHandle
from field_rules import get_field_match_by_rules
from field_lookup import lookup_field_value, LookupStage
from ai_client import ai_interpret_field, ai_match_saved_answers, ai_generate_tailored_answer
from widget_executors import (
    WidgetType, widget_registry, verify_filled_widgets,
    KNOWN_DATE_PICKER_CLASSES, parse_date_value, format_date_for_element
)
from database import generate_question_key

async def detect_security_checkpoint(page: Page) -> Optional[str]:
    """
    Check if an active, blocking CAPTCHA challenge or bot verification is blocking the page:
    1. Interactive Cloudflare Turnstile / challenge-running
    2. Active reCAPTCHA / hCaptcha challenge frames
    3. Arkose Labs / Datadome / PerimeterX modals
    4. Text headings ("Verify you are human", "Security Check")
    Returns a description of the barrier if found, or None.
    """
    try:
        # 1. Cloudflare Turnstile & Challenge platform
        cf_blocks = await page.query_selector_all(
            '#cf-challenge-running, #challenge-stage, .cf-turnstile-wrapper, iframe[src*="challenges.cloudflare.com"]'
        )
        for cf in cf_blocks:
            if await cf.is_visible():
                return "Cloudflare Bot Verification detected. Please solve the challenge on your laptop window, then click Resume."

        # 2. Active reCAPTCHA / hCaptcha challenge frames
        captcha_frames = await page.query_selector_all(
            'iframe[src*="recaptcha/enterprise/bframe"], iframe[src*="recaptcha/api2/bframe"], iframe[src*="hcaptcha.com/box"]'
        )
        for frame in captcha_frames:
            if await frame.is_visible():
                box = await frame.bounding_box()
                if box and box["width"] > 200 and box["height"] > 200:
                    return "Security CAPTCHA challenge detected. Please complete on your laptop window, then click Resume."

        # 3. Arkose Labs / Datadome / PerimeterX
        bot_frames = await page.query_selector_all(
            'iframe[src*="arkoselabs"], iframe[src*="datadome"], iframe[src*="perimeterx"], #px-captcha, [id*="dd-captcha"]'
        )
        for frame in bot_frames:
            if await frame.is_visible():
                return "Anti-Bot Verification challenge detected. Please solve on your laptop window, then click Resume."

        # 4. Visible heading text blocks
        visible_blocks = await page.query_selector_all(
            'h1, h2, h3, .challenge-container, [id*="captcha"]'
        )
        for el in visible_blocks:
            if await el.is_visible():
                txt = (await el.inner_text() or "").lower()
                if any(w in txt for w in ["verify you are human", "enter the verification code", "one-time password", "security check", "suspicious activity"]):
                    return "Security verification detected. Please complete on your laptop window, then click Resume."
    except Exception:
        pass

    return None

async def detect_login_wall(page: Page) -> Optional[str]:
    """
    Detect if the job portal is gated behind a mandatory authentication wall
    (Workday, Taleo, LinkedIn, custom candidate logins).
    """
    try:
        url = page.url.lower()
        if any(w in url for w in ["/login", "/signin", "/auth/login", "/candidates/login", "/myworkday"]):
            pw = await page.query_selector('input[type="password"]')
            if pw and await pw.is_visible():
                return "Candidate Login Required. Please sign in on the open browser window, then click Resume."

        # Check for visible login headings
        login_headings = await page.query_selector_all('h1, h2, h3, .login-title, [class*="login"]')
        for el in login_headings:
            if await el.is_visible():
                txt = (await el.inner_text() or "").lower()
                if any(w in txt for w in ["sign in to apply", "log in to your account", "candidate sign in", "create an account to apply", "please log in to continue", "please log in", "log in", "sign in"]):
                    pw = await page.query_selector('input[type="password"]')
                    if pw and await pw.is_visible():
                        return "Candidate Login Required. Please sign in on the open browser window, then click Resume."

        # Check for password input alongside submit login/sign-in button
        pw = await page.query_selector('input[type="password"]')
        if pw and await pw.is_visible():
            submit_btn = await page.query_selector('input[type="submit"][value*="Log In" i], input[type="submit"][value*="Sign In" i], button:has-text("Log In"), button:has-text("Sign In")')
            if submit_btn and await submit_btn.is_visible():
                return "Candidate Login Required. Please sign in on the open browser window, then click Resume."
    except Exception:
        pass

    return None

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
    """Detect if an element is a native date input or custom calendar picker using word boundaries on known picker classes."""
    try:
        inp_type = (await el.get_attribute("type") or "").lower()
        if inp_type in ["date", "month"]:
            return True
        el_class = await el.get_attribute("class") or ""
        if KNOWN_DATE_PICKER_CLASSES.search(el_class):
            return True
        parent_class = await el.evaluate("e => e.parentElement ? (e.parentElement.className || '') : ''")
        if KNOWN_DATE_PICKER_CLASSES.search(parent_class):
            return True
    except Exception:
        pass
    return False

async def set_date_on_element(el: ElementHandle, date_str: str) -> bool:
    """Universal date setter supporting jQuery UI datepicker, Flatpickr, native date, and formatted text."""
    try:
        parsed_d = parse_date_value(date_str)
        if not parsed_d:
            print(f"[set_date_on_element] Invalid date string '{date_str}', returning failure without fallback.")
            return False

        iso_date = parsed_d.isoformat()
        day = f"{parsed_d.day:02d}"
        mo = f"{parsed_d.month:02d}"
        yr = f"{parsed_d.year:04d}"
        mName = parsed_d.strftime("%B")

        res = await el.evaluate("""(e, info) => {
            let d = new Date(info.isoDate + 'T00:00:00');
            if (isNaN(d.getTime())) {
                return false; // Return failure, NEVER fallback to today's date!
            }

            // 1. jQuery UI Datepicker
            if (window.jQuery && window.jQuery(e).data('datepicker')) {
                try {
                    window.jQuery(e).datepicker('setDate', d);
                    e.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                } catch(err) {}
            }

            // 2. Flatpickr
            if (e._flatpickr) {
                try {
                    e._flatpickr.setDate(d, true);
                    return true;
                } catch(err) {}
            }

            // 3. Bootstrap Datepicker
            if (window.jQuery && window.jQuery(e).data('bs.datepicker')) {
                try {
                    window.jQuery(e).datepicker('update', d);
                    e.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                } catch(err) {}
            }

            // 4. Format based on element hints (dd/mm/yyyy, mm/dd/yyyy, yyyy-mm-dd, MMM yyyy)
            e.removeAttribute('readonly');
            let ph = (e.placeholder || '').toLowerCase().trim();
            let inpType = (e.type || '').toLowerCase().trim();
            let formattedVal = '';

            if (inpType === 'date') {
                formattedVal = `${info.yr}-${info.mo}-${info.day}`;
            } else if (inpType === 'month') {
                formattedVal = `${info.yr}-${info.mo}`;
            } else if (/\\bmm[\\/-]dd[\\/-]yyyy\\b/i.test(ph) || /\\bm[\\/-]d[\\/-]y\\b/i.test(ph)) {
                formattedVal = `${info.mo}/${info.day}/${info.yr}`;
            } else if (/\\bdd[\\/-]mm[\\/-]yyyy\\b/i.test(ph) || /\\bd[\\/-]m[\\/-]y\\b/i.test(ph)) {
                formattedVal = `${info.day}/${info.mo}/${info.yr}`;
            } else if (/\\byyyy[\\/-]mm[\\/-]dd\\b/i.test(ph)) {
                formattedVal = `${info.yr}-${info.mo}-${info.day}`;
            } else if (!/(\\bdd\\b|\\bd\\b|day)/i.test(ph) && (/\\b(mmm\\s*yyyy|month\\s*yyyy|month,\\s*year)\\b/i.test(ph) || ph === 'mmm yyyy' || ph === 'month yyyy')) {
                formattedVal = `${info.mName} ${info.yr}`;
            } else if (!/(\\bdd\\b|\\bd\\b|day)/i.test(ph) && /\\bmm[\\/-]yyyy\\b/i.test(ph)) {
                formattedVal = `${info.mo}/${info.yr}`;
            } else {
                formattedVal = `${info.day}/${info.mo}/${info.yr}`;
            }

            try {
                let nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                nativeSetter.call(e, formattedVal);
            } catch(err) {
                e.value = formattedVal;
            }

            e.dispatchEvent(new Event('input', { bubbles: true }));
            e.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        }""", {
            "isoDate": iso_date,
            "day": day,
            "mo": mo,
            "yr": yr,
            "mName": mName
        })
        return bool(res)
    except Exception as e:
        print(f"Error setting date on element: {e}")
        return False

async def get_group_question(ctx, el: ElementHandle) -> str:
    """
    Exclusively resolves the overall question for radio groups and multi-checkbox groups:
    1. fieldset > legend (W3C standard)
    2. Question container headers (.field__label, .question-title, etc.)
    3. Label elements that do NOT wrap the radio/checkbox options
    4. Nearby preceding text / sibling headings
    5. aria-labelledby on the fieldset/container
    """
    try:
        # 1. Check fieldset > legend (W3C standard)
        fieldset = await el.query_selector("xpath=ancestor::fieldset")
        if fieldset:
            legend = await fieldset.query_selector("legend")
            if legend:
                txt = (await legend.inner_text() or "").strip()
                if txt and not any(skip in txt.lower() for skip in ["select...", "error"]):
                    return txt.rstrip("*").strip()

        # 2. Check question container (field / question / form-group)
        container = await el.query_selector("xpath=ancestor::div[contains(@class, 'field') or contains(@class, 'question') or contains(@class, 'form-group') or contains(@class, 'application-question') or contains(@class, 'form-item') or contains(@class, 'radio-group') or contains(@class, 'checkbox-group')]")
        if container:
            # Query header/label inside container (excluding option labels)
            lbl = await container.query_selector(".field__label, .field-label, .question-header, .question-title, .label, [class*='label']:not([class*='option']):not([class*='choice']), legend, h3, h4, p[class*='title'], p[class*='label'], strong")
            if lbl:
                txt = (await lbl.inner_text() or "").strip()
                if txt and not any(skip in txt.lower() for skip in ["select...", "error"]):
                    return txt.rstrip("*").strip()

            # Query label that does NOT wrap an input
            lbl2 = await container.query_selector("label:not(:has(input))")
            if lbl2:
                txt = (await lbl2.inner_text() or "").strip()
                if txt and not any(skip in txt.lower() for skip in ["select...", "error"]):
                    return txt.rstrip("*").strip()

        # 3. Check aria-labelledby on el
        aria_labelledby = await el.get_attribute("aria-labelledby")
        if aria_labelledby:
            parts = []
            for lid in aria_labelledby.split():
                lbl_target = await ctx.query_selector(f'#{lid}')
                if lbl_target:
                    txt = (await lbl_target.inner_text() or "").strip()
                    if txt and not any(skip in txt.lower() for skip in ["error", "required"]):
                        parts.append(txt)
            if parts:
                return " ".join(parts).rstrip("*").strip()

        # 4. Check preceding sibling element (nearby text)
        prec = await el.query_selector("xpath=preceding-sibling::*[self::label or self::span or self::p or self::div or self::h4 or self::h3][1]")
        if prec:
            txt = (await prec.inner_text() or "").strip()
            if txt and len(txt) < 150 and not any(skip in txt.lower() for skip in ["error", "select..."]):
                return txt.rstrip("*").strip()

        # 5. Clean name attribute
        name = await el.get_attribute("name")
        if name and name.strip():
            clean_name = re.sub(r'\[.*?\]|[_\-\.]', ' ', name).strip().title()
            if clean_name and len(clean_name) > 3 and not clean_name.isdigit():
                return clean_name
    except Exception:
        pass
    return ""

async def get_option_label(ctx, el: ElementHandle) -> str:
    """
    Resolves the specific choice label for an individual radio or checkbox button:
    1. Wrapping ancestor <label>
    2. Matching <label for="...">
    3. Next sibling <span> or text
    4. HTML value attribute
    """
    try:
        # 1. Wrapping ancestor label
        parent_txt = await el.evaluate("e => e.closest('label') ? e.closest('label').innerText.trim() : ''")
        if parent_txt:
            return parent_txt.rstrip("*").strip()

        # 2. Matching <label for="...">
        el_id = await el.get_attribute("id")
        if el_id:
            lbl = await ctx.query_selector(f'label[for="{el_id}"]')
            if lbl:
                txt = (await lbl.inner_text() or "").strip()
                if txt:
                    return txt.rstrip("*").strip()

        # 3. Next sibling span/label text
        txt = await el.evaluate("""(e) => {
            if (e.nextElementSibling && (e.nextElementSibling.tagName === 'SPAN' || e.nextElementSibling.tagName === 'LABEL')) {
                return e.nextElementSibling.innerText.trim();
            }
            if (e.parentElement) {
                let clone = e.parentElement.cloneNode(true);
                let inputs = clone.querySelectorAll('input');
                inputs.forEach(i => i.remove());
                return clone.innerText.trim();
            }
            return e.value || '';
        }""")
        if txt and txt.strip():
            return txt.strip().rstrip("*").strip()

        # 4. Fallback to value attribute
        val = await el.get_attribute("value")
        if val and val.strip() and val.strip() not in ["on", "1", "true"]:
            return val.strip()
    except Exception:
        pass
    return ""

async def get_element_label(ctx, el: ElementHandle) -> str:
    """
    Comprehensive label resolver supporting:
    1. Radio group question delegation (fieldset/legend or container header)
    2. aria-labelledby (resolves multiple IDs)
    3. aria-label
    4. <label for="id">
    5. Wrapping ancestor <label>
    6. Form-group / question container headers (.field-label, .question-title, etc.)
    7. Nearby preceding sibling element (label, span, div, p)
    8. Placeholder
    9. Preceding sibling or name attribute
    """
    try:
        inp_type = (await el.get_attribute("type") or "").lower()
        if inp_type == "radio":
            group_q = await get_group_question(ctx, el)
            if group_q:
                return group_q

        # 1. Check aria-labelledby
        aria_labelledby = await el.get_attribute("aria-labelledby")
        if aria_labelledby:
            parts = []
            for lid in aria_labelledby.split():
                lbl_target = await ctx.query_selector(f'[id="{lid}"]')
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

        # 4. Check wrapping ancestor label (if not radio)
        if inp_type != "radio":
            parent_txt = await el.evaluate("e => e.closest('label') ? e.closest('label').innerText.trim() : ''")
            if parent_txt:
                return parent_txt.rstrip("*").strip()

        # 5. Check question container header or label
        container = await el.query_selector("xpath=ancestor::div[contains(@class, 'field') or contains(@class, 'form-group') or contains(@class, 'question') or contains(@class, 'select__container') or contains(@class, 'form-item')]")
        if container:
            lbl = await container.query_selector("label, legend, .label, [class*='label'], [class*='title'], [class*='header']")
            if lbl:
                txt = (await lbl.inner_text() or "").strip()
                if txt and not any(skip in txt.lower() for skip in ["select...", "error"]):
                    return txt.rstrip("*").strip()

        # 6. Check nearby preceding sibling element
        prec = await el.query_selector("xpath=preceding-sibling::*[self::label or self::span or self::p or self::div][1]")
        if prec:
            txt = (await prec.inner_text() or "").strip()
            if txt and len(txt) < 150 and not any(skip in txt.lower() for skip in ["select...", "error"]):
                return txt.rstrip("*").strip()

        # 7. Check placeholder
        placeholder = await el.get_attribute("placeholder")
        if placeholder and placeholder.strip() and not placeholder.strip().lower().startswith("select"):
            return placeholder.strip().rstrip("*").strip()

        # 8. Check name attribute
        name = await el.get_attribute("name")
        if name and name.strip():
            clean_name = re.sub(r'[_\-\[\]\.]', ' ', name).strip().title()
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
    full_name = (profile.get("full_name") or "").strip()
    parts = full_name.split() if full_name else []
    first_name = profile.get("first_name") or (parts[0] if parts else "")
    last_name = profile.get("last_name") or (" ".join(parts[1:]) if len(parts) > 1 else "")
    city = profile.get("city") or profile.get("location", "")

    mapping = {
        "full_name": full_name,
        "first_name": first_name,
        "last_name": last_name or "",
        "email": profile.get("email", ""),
        "phone": profile.get("phone", ""),
        "location": profile.get("location", "") or city,
        "city": city,
        "linkedin": profile.get("linkedin_url", ""),
        "github": profile.get("github_url", ""),
        "portfolio": profile.get("portfolio_url", "") or profile.get("linkedin_url", "")
    }
    return mapping.get(intent)

SENSITIVE_PATTERNS = [
    r'\b(salary|compensation|pay|rate|ctc|remuneration)\b',
    r'\b(expected\s*(salary|ctc|compensation|pay))\b',
    r'\b(years?\s*(of\s*)?experience|how\s*many\s*years)\b',
    r'\b(notice\s*period|how\s*soon|available\s*to\s*join)\b',
    r'\b(gpa|cgpa|grade\s*point|percentage)\b',
    r'\b(visa|sponsorship|authorized\s*to\s*work|work\s*authorization|citizenship|alien)\b',
    r'\b(disability|veteran|gender|race|ethnicity|sexual\s*orientation)\b',
    r'\b(criminal|felony|misdemeanor|background\s*check|drug\s*test)\b',
    r'\b(security\s*clearance|secret\s*clearance)\b',
    r'\b(consent|agreement|acknowledge)\b',
    r'\b(dob|birth\s*date|date\s*of\s*birth)\b',
    r'\b(ssn|social\s*security|passport|national\s*id)\b'
]
SENSITIVE_REGEX = re.compile('|'.join(SENSITIVE_PATTERNS), re.IGNORECASE)

def is_sensitive_question(label: str) -> bool:
    """
    Detect whether a question involves sensitive compliance, legal, compensation,
    or quantitative qualification terms using strict word-boundary matching.
    Prevents false-positives like 'rate' matching 'corporate' or 'accelerate'.
    These fields must NEVER be invented by AI — they must come strictly from the
    resume profile or previously approved saved answers, or pause for user input.
    """
    if not label:
        return False
    return bool(SENSITIVE_REGEX.search(label.strip()))

async def verify_form_completion_and_errors(ctx) -> Dict[str, Any]:
    """
    Exhaustively re-reads every interactive element in the form:
    1. Checks if any required field is empty or unselected.
    2. Checks for aria-invalid or visible error text nodes.
    3. Checks for form-level error banners and alerts.
    """
    empty_required = []
    validation_errors = []

    # 1. Scan for visible error messages
    error_selectors = [
        '[aria-invalid="true"]',
        '.error-message', '.field-error', '.help-block-error', '.invalid-feedback',
        '[class*="errorMessage"]', '[class*="error-text"]', '[class*="field-error"]',
        '[role="alert"]', '.alert-danger', '.form-error'
    ]
    try:
        error_els = await ctx.query_selector_all(", ".join(error_selectors))
        for el in error_els:
            if await el.is_visible():
                txt = (await el.inner_text() or "").strip()
                if txt and len(txt) < 200 and txt not in validation_errors:
                    if not any(skip in txt.lower() for skip in ["cookie", "privacy policy", "all rights reserved"]):
                        validation_errors.append(txt)
    except Exception:
        pass

    # 2. Re-read all form fields to verify required fields are actually populated
    try:
        elements = await ctx.query_selector_all('input, select, textarea, [role="combobox"]')
        checked_radio_groups = set()

        for el in elements:
            inp_type = (await el.get_attribute("type") or "").lower()
            tag = await el.evaluate("e => e.tagName.toLowerCase()")
            if inp_type in ["submit", "button", "reset", "hidden"]:
                continue
            if not await el.is_visible() and inp_type != "file" and tag != "select":
                continue

            # Determine if element is required
            is_req = False
            if await el.get_attribute("required") is not None or await el.get_attribute("aria-required") == "true":
                is_req = True
            
            label = await get_element_label(ctx, el)
            if "*" in label or "required" in (await el.get_attribute("class") or "").lower():
                is_req = True

            parent = await el.query_selector("xpath=ancestor::div[contains(@class, 'field') or contains(@class, 'form-group') or contains(@class, 'question')][1]")
            if parent:
                parent_html = (await parent.inner_html() or "").lower()
                if 'class="asterisk"' in parent_html or 'class="required"' in parent_html or "aria-required" in parent_html:
                    is_req = True

            if not is_req:
                continue

            field_name = label or await el.get_attribute("name") or "Required Field"
            if inp_type == "radio":
                name = await el.get_attribute("name") or ""
                if name in checked_radio_groups:
                    continue
                checked_radio_groups.add(name)
                group_radios = await ctx.query_selector_all(f'input[type="radio"][name="{name}"]') if name else [el]
                if not any([await r.is_checked() for r in group_radios]):
                    empty_required.append(f"{field_name} (Radio group unselected)")

            elif inp_type == "checkbox":
                if not await el.is_checked():
                    empty_required.append(f"{field_name} (Checkbox unchecked)")

            elif tag == "select":
                val = await el.evaluate("s => (s && s.options && s.selectedIndex >= 0 && s.options[s.selectedIndex]) ? s.options[s.selectedIndex].text.trim() : ''")
                if not val or val.lower() in ["select", "select an option", "select...", "-- select --", "choose"]:
                    empty_required.append(f"{field_name} (Dropdown unselected)")

            elif tag in ["input", "textarea"]:
                val = await el.input_value()
                if not val or not val.strip():
                    empty_required.append(f"{field_name} (Empty)")

    except Exception as e:
        print(f"Error re-reading required fields: {e}")

    return {
        "validation_errors": validation_errors,
        "empty_required_fields": empty_required,
        "is_valid": len(validation_errors) == 0 and len(empty_required) == 0
    }

@dataclass
class RestoreResult:
    restored_count: int
    total_fields: int
    verified_fields: List[Dict[str, Any]] = field(default_factory=list)
    failed_fields: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return len(self.failed_fields) == 0

async def fast_restore_form_data(
    ctx,
    form_data: Dict[str, Any],
    resume_path: Optional[str] = None
) -> RestoreResult:
    """
    Crash Recovery Engine:
    Fast-restores previously filled fields directly into the active form DOM
    using dedicated widget executors and re-verifies each restored value.
    Eliminates :has-text and blind keyboard navigation.
    Returns RestoreResult with count and verification details.
    """
    if not form_data or not isinstance(form_data, dict):
        return RestoreResult(restored_count=0, total_fields=0)
    
    fields = form_data.get("fields", {})
    if not fields or not isinstance(fields, dict):
        return RestoreResult(restored_count=0, total_fields=0)

    restored_count = 0
    verified_fields: List[Dict[str, Any]] = []
    failed_fields: List[Dict[str, Any]] = []

    # Pre-fetch interactive elements for fallback label matching
    interactive_elements = None

    for key, f in fields.items():
        try:
            val = f.get("value")
            if val is None or str(val).strip() == "":
                continue

            f_id = f.get("id")
            f_name = f.get("name")
            f_label = f.get("label", "")
            f_type = (f.get("type") or "text").lower().strip()
            f_tag = (f.get("tag") or "input").lower().strip()
            f_selector = f.get("selector", "")

            el = None
            if f_selector:
                try:
                    el = await ctx.query_selector(f_selector)
                except Exception:
                    el = None

            if not el and f_id:
                try:
                    el = await ctx.query_selector(f'[id="{f_id}"]')
                except Exception:
                    el = None

            if not el and f_name:
                try:
                    el = await ctx.query_selector(f'[name="{f_name}"]')
                except Exception:
                    el = None

            # Find by label without :has-text
            if not el and f_label:
                try:
                    f_lbl_clean = f_label.strip().lower()
                    all_labels = await ctx.query_selector_all('label')
                    for l_el in all_labels:
                        l_txt = (await l_el.inner_text() or "").strip().lower()
                        if l_txt == f_lbl_clean or f_lbl_clean in l_txt:
                            for_attr = await l_el.get_attribute("for")
                            if for_attr:
                                el = await ctx.query_selector(f'[id="{for_attr}"]')
                                if el:
                                    break
                except Exception:
                    el = None

            # Fallback to interactive controls
            if not el and f_label:
                try:
                    if interactive_elements is None:
                        interactive_elements = await ctx.query_selector_all('input, select, textarea, [role="combobox"]')
                    f_lbl_clean = f_label.strip().lower()
                    for cand in interactive_elements:
                        cand_lbl = (await get_element_label(ctx, cand)).strip().lower()
                        if cand_lbl and (cand_lbl == f_lbl_clean or f_lbl_clean in cand_lbl or cand_lbl in f_lbl_clean):
                            el = cand
                            break
                except Exception:
                    el = None

            if not el:
                continue

            # Detect real widget type from DOM element (declared question type is only a hint)
            detected_type = await widget_registry.detect_widget_type(ctx, el)
            w_type = detected_type or WidgetType.TEXT

            target_val = val
            if w_type == WidgetType.FILE:
                if not (val and os.path.exists(str(val))):
                    target_val = resume_path

            if target_val is None:
                continue

            # Restore and re-verify via dedicated widget executor with live DOM reconciliation
            exec_res = await widget_registry.execute_element(ctx, el, target_val, widget_type=f_type)

            if exec_res.success and exec_res.verification_passed:
                restored_count += 1
                verified_fields.append({
                    "field": f_label or f_id or key,
                    "target_value": str(target_val),
                    "verified_value": exec_res.verified_value
                })
            else:
                failed_fields.append({
                    "field": f_label or f_id or key,
                    "target_value": str(target_val),
                    "verified_value": exec_res.verified_value,
                    "error": exec_res.error or "Verification check failed after restore"
                })
                print(f"[Crash Recovery Warning] Verification failed restoring '{f_label or f_id or key}': expected '{target_val}', got '{exec_res.verified_value}' ({exec_res.error})")

        except Exception as e:
            print(f"[Crash Recovery] Error restoring field '{key}': {e}")
            failed_fields.append({
                "field": key,
                "target_value": str(f.get("value")),
                "verified_value": None,
                "error": str(e)
            })

    return RestoreResult(
        restored_count=restored_count,
        total_fields=len(fields),
        verified_fields=verified_fields,
        failed_fields=failed_fields
    )

async def fill_form_on_page(
    page: Page,
    profile: Dict[str, Any],
    saved_answers: List[Dict[str, Any]],
    resume_path: Optional[str] = None,
    job_context: str = "",
    existing_form_data: Optional[Dict[str, Any]] = None,
    application_id: str = ""
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

    form_data = existing_form_data.copy() if (existing_form_data and isinstance(existing_form_data, dict)) else {"fields": {}, "current_step": 1}
    if "fields" not in form_data or not isinstance(form_data["fields"], dict):
        form_data["fields"] = {}

    def record_filled(k_id, k_name, k_label, k_tag, k_type, k_val, selector=""):
        key = f"id:{k_id}" if k_id else (f"name:{k_name}" if k_name else f"label:{k_label.lower().strip()}")
        form_data["fields"][key] = {
            "key": key,
            "id": k_id,
            "name": k_name,
            "label": k_label,
            "tag": k_tag,
            "type": k_type,
            "value": str(k_val),
            "selector": selector,
            "timestamp": time.time()
        }

    result = {
        "completed": True,
        "needs_input": False,
        "unanswered_question": None,
        "unanswered_questions": [],
        "ai_suggestion": "",
        "fields_filled": [],
        "has_next_page": False,
        "submit_button_found": False,
        "validation_errors": [],
        "warnings": [],
        "form_data": form_data
    }

    # Batch question collector: collects all unknown questions on page without crashing
    unanswered_questions: List[Dict[str, Any]] = []
    seen_question_labels = set()

    def collect_unanswered_question(
        field_id: str,
        label: str,
        field_type: str,
        options: Optional[List[str]] = None,
        ai_suggestion: str = "",
        category: str = "general"
    ):
        clean_lbl = (label or "").strip()
        lbl_key = clean_lbl.lower()
        if not clean_lbl or lbl_key in seen_question_labels:
            return

        # Prevent duplicate / ghost questions for core identity fields that are already filled
        if any(f.get("field", "").strip().lower() == lbl_key for f in result["fields_filled"]):
            return

        # If a core identity field already has a non-empty value in profile, don't ask as unknown
        if is_intent_plausible("first_name", clean_lbl) and profile.get("first_name"):
            return
        if is_intent_plausible("last_name", clean_lbl) and profile.get("last_name"):
            return
        if is_intent_plausible("email", clean_lbl) and profile.get("email"):
            return
        if is_intent_plausible("phone", clean_lbl) and profile.get("phone"):
            return
        if is_intent_plausible("location", clean_lbl) and (profile.get("location") or profile.get("city")):
            return

        seen_question_labels.add(lbl_key)

        # Generate stable, canonical slugified question key
        stable_key = generate_question_key(clean_lbl, field_type=field_type, company_name=job_context)

        # Scope classification: location, relocation, notice period, nda are company-specific
        company_specific_keywords = [
            "relocat", "office", "bangalore", "bengaluru", "location", "commute",
            "notice period", "nda", "non-disclosure", "policy", "agreement"
        ]
        is_company_scope = any(k in lbl_key for k in company_specific_keywords)
        scope = "this company" if is_company_scope else "global"

        q_dict = {
            "id": f"q_{len(unanswered_questions) + 1}",
            "question_key": stable_key,
            "field_id": field_id or stable_key,
            "label": clean_lbl,
            "type": field_type,
            "options": options or [],
            "ai_suggestion": ai_suggestion,
            "category": category,
            "scope": scope,
            "save_for_future": True
        }
        unanswered_questions.append(q_dict)
        print(f"[FormFiller] Collected unknown question into batch: '{clean_lbl}' ({field_type}, key={stable_key})")

    # Query all interactive form controls in natural top-to-bottom DOM order
    elements = await ctx.query_selector_all('input, select, textarea, [role="combobox"]')
    processed_radio_groups = set()

    for el_idx, el in enumerate(elements):
        label = ""
        tag = ""
        inp_type = ""
        el_id = ""
        name = ""
        try:
            tag = await el.evaluate("e => e.tagName.toLowerCase()")
            inp_type = (await el.get_attribute("type") or "").lower()
            role = (await el.get_attribute("role") or "").lower()
            el_id = await el.get_attribute("id") or ""
            name = await el.get_attribute("name") or ""
            el_class = (await el.get_attribute("class") or "").lower()

            # Filter non-input / non-interactive buttons
            if inp_type in ["submit", "button", "reset", "hidden"] and inp_type != "file":
                continue
            if "search-input" in el_id.lower() or "iti-" in el_id.lower() or "select2-selection" in el_class or "recaptcha" in el_id.lower():
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
                        lbl_low = (label or "").lower()
                        el_id_low = el_id.lower()
                        name_low = name.lower()
                        is_cover_letter = any(w in lbl_low or w in el_id_low or w in name_low for w in ["cover", "letter"])

                        # If field is specifically for an optional cover letter, don't duplicate resume
                        if is_cover_letter:
                            is_required = await el.get_attribute("required") is not None or await el.get_attribute("aria-required") == "true"
                            if not is_required:
                                continue

                        # Use FileWidgetExecutor (fills & verifies attachment in DOM)
                        file_exec = widget_registry.get_executor(WidgetType.FILE)
                        exec_res = await file_exec.execute(ctx, el, resume_path)
                        if exec_res.success:
                            fname = os.path.basename(resume_path)
                            result["fields_filled"].append({
                                "field": label or "Resume Upload",
                                "value": fname,
                                "type": "file",
                                "id": el_id,
                                "name": name,
                                "selector": f'[id="{el_id}"]' if el_id else "",
                                "verified": exec_res.verified_value
                            })
                            record_filled(el_id, name, label or "Resume Upload", tag, "file", resume_path, selector=f'[id="{el_id}"]' if el_id else "")
                    except Exception as e:
                        print(f"File upload error on '{label}': {e}")
                continue

            # =========================================================================
            # 2. CUSTOM COMBOBOXES & REACT/GREENHOUSE TYPEAHEADS
            # =========================================================================
            is_combobox = (
                role == "combobox"
                or (await el.get_attribute("aria-haspopup") == "listbox")
                or bool(await el.get_attribute("aria-controls"))
                or bool(await el.get_attribute("aria-owns"))
                or bool(await el.evaluate("""e => !!e.closest('[role="combobox"], [class*="select__"], .select2-container, [class*="MuiAutocomplete"]')"""))
            )
            if is_combobox:
                cb_txt = (await el.inner_text() or "").strip()
                if cb_txt in ["+91", "INR", "USD"] or (cb_txt == "Select" and any(w in label.lower() for w in ["phone", "code", "dial"])):
                    continue

                parent_handle = await el.evaluate_handle("""e => e.closest('[role="combobox"], [class*="select__"], .select2-container, [class*="MuiAutocomplete"]')""")
                parent_container = parent_handle.as_element() if parent_handle else None
                if parent_container:
                    val_container = await parent_container.query_selector("[class*='singleValue'], [class*='single-value'], .select2-selection__rendered")
                    curr_txt = (await val_container.inner_text() or "").strip() if val_container else ""
                    if curr_txt and curr_txt not in ["Select...", "", "Select", "Select an option"]:
                        continue

                # A. Country combobox
                is_country_field = bool(re.search(r'^(country|your\s*country|country\s*[\/\-]\s*region|country\s*of\s*residence)$', label.lower().strip()))
                if is_country_field:
                    loc_val = profile.get("location") or ""
                    loc_parts = [p.strip() for p in loc_val.split(",") if p.strip()]
                    country_val = profile.get("country") or (loc_parts[-1] if loc_parts else "India")
                    cb_exec = widget_registry.get_executor(WidgetType.CUSTOM_COMBOBOX)
                    exec_res = await cb_exec.execute(ctx, el, country_val)
                    if exec_res.success:
                        val_str = str(exec_res.verified_value or country_val)
                        result["fields_filled"].append({
                            "field": label,
                            "value": val_str,
                            "type": "combobox",
                            "id": el_id,
                            "name": name,
                            "selector": f'[id="{el_id}"]' if el_id else "",
                            "verified": exec_res.verified_value
                        })
                        record_filled(el_id, name, label, tag, "combobox", val_str, selector=f'[id="{el_id}"]' if el_id else "")
                        continue

                # B. Location / City typeahead
                is_location_field = bool(re.search(r'^(location|current\s*location|location\s*\(city\)|city|address)$', label.lower().strip())) or (
                    any(w in label.lower() for w in ["location", "city"]) and not any(w in label.lower() for w in ["willing", "relocate", "prefer", "able to work", "office", "authorized", "require"])
                )
                if is_location_field:
                    clean_city = profile.get("city")
                    loc_val = profile.get("location") or "Kochi, Kerala, India"
                    city_search = clean_city or (loc_val.split(",")[0].strip() if "," in loc_val else loc_val)
                    cb_exec = widget_registry.get_executor(WidgetType.CUSTOM_COMBOBOX)
                    exec_res = await cb_exec.execute(ctx, el, city_search)
                    if not exec_res.success and clean_city and loc_val != clean_city:
                        # Fallback attempt with full loc_val or alternate part if city search yielded no match
                        alt_search = loc_val.split(",")[-1].strip()
                        exec_res = await cb_exec.execute(ctx, el, alt_search)
                    if exec_res.success:
                        val_str = str(exec_res.verified_value or city_search)
                        result["fields_filled"].append({
                            "field": label,
                            "value": val_str,
                            "type": "combobox",
                            "id": el_id,
                            "name": name,
                            "selector": f'[id="{el_id}"]' if el_id else "",
                            "verified": exec_res.verified_value
                        })
                        record_filled(el_id, name, label, tag, "combobox", val_str, selector=f'[id="{el_id}"]' if el_id else "")
                        continue

                # C. Extract options & check saved answers (memory) and non-sensitive deterministic rules
                options = []
                control = await parent_container.query_selector(".select__control") if parent_container else None
                trigger = control or el

                if trigger:
                    try:
                        await trigger.click()
                        await asyncio.sleep(0.4)

                        aria_controls = await el.get_attribute("aria-controls")
                        listbox = await ctx.query_selector(f'[id="{aria_controls}"]') if aria_controls else None
                        if not listbox and aria_controls:
                            listbox = await page.query_selector(f'[id="{aria_controls}"]')
                        if not listbox:
                            aria_owns = await el.get_attribute("aria-owns")
                            listbox = await ctx.query_selector(f'[id="{aria_owns}"]') if aria_owns else None
                            if not listbox and aria_owns:
                                listbox = await page.query_selector(f'[id="{aria_owns}"]')
                        if not listbox:
                            listbox = await ctx.query_selector('.select__menu:not([class*="control"]), [role="listbox"]:not(.iti__country-list):not([class*="iti"]), [class*="MuiAutocomplete-popper"], [class*="ant-select-dropdown"]') or await page.query_selector('.select__menu:not([class*="control"]), [role="listbox"]:not(.iti__country-list):not([class*="iti"]), [class*="MuiAutocomplete-popper"], [class*="ant-select-dropdown"]')

                        if listbox:
                            opt_els = await listbox.query_selector_all('[role="option"], .select__option')
                            for o in opt_els:
                                t = (await o.inner_text() or "").strip()
                                if t and t not in options and not "select" in t.lower():
                                    options.append(t)
                    except Exception as e:
                        print(f"Combobox options extraction error for '{label}': {e}")

                matched_val = await ai_match_saved_answers(label, saved_answers, field_type="combobox", options=options, company_name=job_context)
                if not matched_val and not is_sensitive_question(label):
                    matched_val = get_field_match_by_rules(label, profile)

                # E. If matched_val is known, select via CustomComboboxWidgetExecutor
                if matched_val:
                    exec_res = await widget_registry.get_executor(WidgetType.CUSTOM_COMBOBOX).execute(ctx, el, matched_val)
                    if exec_res.success:
                        result["fields_filled"].append({
                            "field": label,
                            "value": str(matched_val),
                            "type": "combobox",
                            "id": el_id,
                            "name": name,
                            "selector": f'[id="{el_id}"]' if el_id else "",
                            "verified": exec_res.verified_value
                        })
                        record_filled(el_id, name, label, tag, "combobox", str(matched_val), selector=f'[id="{el_id}"]' if el_id else "")
                        continue

                await page.keyboard.press("Escape")
                await asyncio.sleep(0.2)

                # F. Unknown Combobox Question -> Pause for User Input
                field_id = await el.get_attribute("id") or label
                is_boolean_question = bool(re.search(r'^(are|do|will|have|is|can|should|would|did|was|were)\b|\b(yes\/no)\b', label.strip(), re.I))

                if options:
                    ai_decision = ""
                    if not is_sensitive_question(label):
                        print(f"[AI Reasoning] Querying Qwen with options for '{label[:60]}...'")
                        ai_decision = await ai_generate_tailored_answer(label, profile, job_context=job_context, options=options, application_id=application_id)
                        print(f"[AI Decision] Qwen chose: '{ai_decision}' from {options[:5]}...")

                    collect_unanswered_question(
                        field_id=field_id,
                        label=label,
                        field_type="combobox",
                        options=options,
                        ai_suggestion=ai_decision or "",
                        category="general"
                    )
                    continue
                elif is_boolean_question:
                    fallback_options = ["Yes", "No"]
                    ai_decision = ""
                    if not is_sensitive_question(label):
                        ai_decision = await ai_generate_tailored_answer(label, profile, job_context=job_context, options=fallback_options, application_id=application_id)
                    collect_unanswered_question(
                        field_id=field_id,
                        label=label,
                        field_type="combobox",
                        options=fallback_options,
                        ai_suggestion=ai_decision or "",
                        category="general"
                    )
                    continue
                else:
                    ai_decision = ""
                    if not is_sensitive_question(label):
                        ai_decision = await ai_generate_tailored_answer(label, profile, job_context=job_context, application_id=application_id) or ""
                    collect_unanswered_question(
                        field_id=field_id,
                        label=label,
                        field_type="text",
                        options=[],
                        ai_suggestion=ai_decision or "",
                        category="general"
                    )
                    continue

            # =========================================================================
            # 3. NATIVE SELECT DROPDOWNS (<select>)
            # =========================================================================
            if tag == "select":
                selected_val = await el.evaluate("sel => sel && sel.value ? sel.value.trim() : ''")
                selected_text = await el.evaluate("sel => (sel && sel.options && sel.selectedIndex >= 0 && sel.options[sel.selectedIndex]) ? sel.options[sel.selectedIndex].text.trim() : ''")
                is_placeholder = (
                    not selected_val or
                    selected_text.lower() in ["", "select", "choose"] or
                    any(p in selected_text.lower() for p in ["select", "choose", "--", "please select"])
                )
                if not is_placeholder:
                    continue

                options = await el.query_selector_all('option')
                opt_texts = []
                for opt in options:
                    opt_val = await opt.get_attribute("value") or ""
                    txt = await opt.inner_text()
                    txt_clean = (txt or "").strip()
                    if txt_clean and opt_val.strip() and not any(skip in txt_clean.lower() for skip in ["select an option", "select...", "-- select --", "please select", "--"]):
                        opt_texts.append(txt_clean)

                # A. Phone Country Code
                is_phone_dial_code = "countrycode" in el_id or "countrycode" in name.lower() or any(w in label.lower() for w in ["country code", "dial code", "+91"]) or (opt_texts and any(ot in ["+91", "+93"] for ot in opt_texts[:10]))
                if is_phone_dial_code:
                    await el.evaluate("""(sel) => {
                        if (!sel || !sel.options) return;
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
                    record_filled(el_id, name, label or "Country Code", tag, "select", "+91")
                    continue

                # B. Currency
                is_currency = "currency" in el_id or "currency" in name.lower() or (opt_texts and any(ot in ["INR", "USD", "EUR"] for ot in opt_texts[:10]))
                if is_currency:
                    await el.evaluate("""(sel) => {
                        if (!sel || !sel.options) return;
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
                    record_filled(el_id, name, label or "Currency", tag, "select", "INR")
                    continue

                # C. Extract options & check saved answers (memory) and non-sensitive deterministic rules
                select_options = []
                try:
                    select_options = await el.evaluate("""sel => {
                        return Array.from((sel && sel.options) || []).map(o => o.text.trim()).filter(Boolean);
                    }""")
                except Exception:
                    select_options = []

                matched_val = await ai_match_saved_answers(label, saved_answers, field_type="select", options=select_options, company_name=job_context)
                if not matched_val and not is_sensitive_question(label):
                    matched_val = get_field_match_by_rules(label, profile)

                if matched_val:
                    exec_res = await widget_registry.get_executor(WidgetType.NATIVE_SELECT).execute(ctx, el, matched_val)
                    if exec_res.success:
                        result["fields_filled"].append({
                            "field": label,
                            "value": str(matched_val),
                            "type": "select",
                            "id": el_id,
                            "name": name,
                            "selector": f'[id="{el_id}"]' if el_id else "",
                            "verified": exec_res.verified_value
                        })
                        record_filled(el_id, name, label, tag, "select", str(matched_val), selector=f'[id="{el_id}"]' if el_id else "")
                        continue

                # D. Pause for unconfirmed select dropdown -> Collect into batch
                ai_decision = ""
                if opt_texts and not is_sensitive_question(label):
                    opt_texts_to_pass = opt_texts[:20]
                    ai_decision = await ai_generate_tailored_answer(label, profile, job_context=job_context, options=opt_texts_to_pass, application_id=application_id)

                field_id = await el.get_attribute("id") or name or label
                collect_unanswered_question(
                    field_id=field_id,
                    label=label,
                    field_type="select",
                    options=opt_texts[:25],
                    ai_suggestion=ai_decision or "",
                    category="general"
                )
                continue

            # =========================================================================
            # 4. RADIO BUTTON GROUPS
            # =========================================================================
            if inp_type == "radio":
                group_label = await get_group_question(ctx, el) or label
                group_radios = await ctx.query_selector_all(f'input[type="radio"][name="{name}"]') if name else [el]
                radio_options = []
                for r_el in group_radios:
                    r_lbl = await get_option_label(ctx, r_el)
                    if r_lbl and r_lbl not in radio_options:
                        radio_options.append(r_lbl)

                # Check if already answered
                any_checked = False
                for r_el in group_radios:
                    if await r_el.is_checked():
                        any_checked = True
                        break
                if any_checked:
                    continue

                # Check saved answers (memory) and non-sensitive deterministic rules
                matched_val = await ai_match_saved_answers(group_label, saved_answers, field_type="radio", options=radio_options, company_name=job_context)
                if not matched_val and not is_sensitive_question(group_label):
                    matched_val = get_field_match_by_rules(group_label, profile)

                if matched_val:
                    exec_res = await widget_registry.get_executor(WidgetType.RADIO).execute(ctx, el, matched_val)
                    if exec_res.success:
                        result["fields_filled"].append({
                            "field": group_label,
                            "value": str(matched_val),
                            "type": "radio",
                            "id": el_id,
                            "name": name,
                            "selector": f'[name="{name}"]' if name else "",
                            "verified": exec_res.verified_value
                        })
                        record_filled(el_id, name, group_label, "input", "radio", str(matched_val))
                        continue

                # Pause on unknown radio group -> Collect into batch
                ai_decision = ""
                if not is_sensitive_question(group_label):
                    ai_decision = await ai_generate_tailored_answer(group_label, profile, job_context=job_context, options=radio_options or ["Yes", "No"], application_id=application_id)
                field_id = await el.get_attribute("id") or name or group_label
                collect_unanswered_question(
                    field_id=field_id,
                    label=group_label,
                    field_type="radio",
                    options=radio_options or ["Yes", "No"],
                    ai_suggestion=ai_decision or "",
                    category="general"
                )
                continue

            # =========================================================================
            # 5. CHECKBOXES (Consent, Terms, Acknowledgment)
            # =========================================================================
            if inp_type == "checkbox":
                if await el.is_checked():
                    continue

                opt_lbl = await get_option_label(ctx, el)
                group_q = await get_group_question(ctx, el)
                checkbox_label = group_q if (group_q and group_q != opt_lbl) else (opt_lbl or label)

                # Policy: Consent, agreement, policy, and sensitive checkboxes must ALWAYS be confirmed by user
                # unless explicitly approved and stored in reusable memory.
                matched_val = await ai_match_saved_answers(checkbox_label, saved_answers, field_type="checkbox", options=["Yes", "No", opt_lbl] if opt_lbl else ["Yes", "No"], company_name=job_context)
                if matched_val and str(matched_val).lower() in ["yes", "true", "check", "1", opt_lbl.lower()]:
                    exec_res = await widget_registry.get_executor(WidgetType.CHECKBOX).execute(ctx, el, "Yes")
                    if exec_res.success:
                        result["fields_filled"].append({
                            "field": checkbox_label,
                            "value": opt_lbl or "Checked (User Approved)",
                            "type": "checkbox",
                            "id": el_id,
                            "name": name,
                            "selector": f'[id="{el_id}"]' if el_id else "",
                            "verified": exec_res.verified_value
                        })
                        record_filled(el_id, name, checkbox_label, "input", "checkbox", "Yes", selector=f'[id="{el_id}"]' if el_id else "")
                        continue

                # Pause on consent / agreement / required checkboxes -> Collect into batch
                field_id = await el.get_attribute("id") or name or checkbox_label
                collect_unanswered_question(
                    field_id=field_id,
                    label=checkbox_label,
                    field_type="checkbox",
                    options=["Yes", "No"],
                    ai_suggestion="Yes",
                    category="legal"
                )
                continue

            # =========================================================================
            # 6. DATEPICKERS & CALENDAR INPUTS
            # =========================================================================
            is_date = await is_date_picker_element(ctx, el, label)
            if is_date:
                curr_val = await el.input_value()
                if curr_val and curr_val.strip():
                    continue

                matched_val = None
                # Sensitive date check: Date of Birth, DOB, etc. are sensitive and must NEVER be auto-filled
                if not is_sensitive_question(label):
                    matched_val = get_field_match_by_rules(label, profile)
                    if not matched_val:
                        matched_val = await ai_match_saved_answers(label, saved_answers, field_type="date", options=[], company_name=job_context)

                if matched_val:
                    exec_res = await widget_registry.get_executor(WidgetType.DATE).execute(ctx, el, str(matched_val))
                    if exec_res.success:
                        result["fields_filled"].append({
                            "field": label,
                            "value": str(matched_val),
                            "type": "date",
                            "id": el_id,
                            "name": name,
                            "selector": f'[id="{el_id}"]' if el_id else "",
                            "verified": exec_res.verified_value
                        })
                        record_filled(el_id, name, label, tag, "date", str(matched_val), selector=f'[id="{el_id}"]' if el_id else "")
                        continue

                # Prompt user with HTML5 interactive datepicker -> Collect into batch
                field_id = await el.get_attribute("id") or name or label
                collect_unanswered_question(
                    field_id=field_id,
                    label=label,
                    field_type="date",
                    options=[],
                    ai_suggestion=None,
                    category="timeline"
                )
                continue

            # =========================================================================
            # 7. TEXT, EMAIL, PHONE, AND TEXTAREA INPUTS
            # =========================================================================
            if tag in ["input", "textarea"]:
                # Skip elements inside custom combobox/select controls
                if await el.query_selector('xpath=ancestor::*[contains(@class, "select__") or contains(@class, "select2") or contains(@class, "MuiAutocomplete")]'):
                    continue

                curr_val = await el.input_value()
                if curr_val and curr_val.strip():
                    continue

                # Check saved answers (memory) and non-sensitive deterministic rules
                matched_value = await ai_match_saved_answers(label, saved_answers, field_type="text", options=[], company_name=job_context)
                if not matched_value and not is_sensitive_question(label):
                    matched_value = get_field_match_by_rules(label, profile)

                # Split phone number handling: avoid duplicate country codes
                if matched_value and any(w in label.lower() for w in ["phone", "mobile", "cell"]):
                    has_dial_dropdown = bool(await ctx.query_selector('.iti__selected-dial-code, [class*="iti__selected"], select[name*="countryCode"], select[id*="countryCode"], [aria-label*="country code"], [class*="dial-code"]'))
                    raw_str = str(matched_value).strip()
                    clean_digits = re.sub(r'^\+\d{1,3}\s*', '', raw_str).strip()
                    if has_dial_dropdown or len(clean_digits) == 10:
                        matched_value = clean_digits

                if not matched_value and not is_sensitive_question(label):
                    tag_name = await el.evaluate("e => e.tagName.toLowerCase()")
                    field_intel = await ai_interpret_field(label, tag_type=tag_name)
                    intent = field_intel.get("intent", "custom_question")

                    if is_intent_plausible(intent, label):
                        matched_value = await map_intent_to_profile(intent, profile)

                    if not matched_value:
                        matched_value = await ai_match_saved_answers(label, saved_answers, field_type="text", options=[], company_name=job_context)

                if matched_value:
                    exec_res = await widget_registry.get_executor(WidgetType.TEXT).execute(ctx, el, matched_value)
                    if exec_res.success:
                        result["fields_filled"].append({
                            "field": label,
                            "value": str(matched_value),
                            "type": "text",
                            "id": el_id,
                            "name": name,
                            "selector": f'[id="{el_id}"]' if el_id else "",
                            "verified": exec_res.verified_value
                        })
                        record_filled(el_id, name, label, tag, inp_type or "text", str(matched_value), selector=f'[id="{el_id}"]' if el_id else "")
                        continue

                # Skip optional notes
                is_optional = any(word in label.lower() for word in ["optional", "not required", "if you would like"])
                if is_optional:
                    continue

                # Pause on unknown text / custom questions -> Collect into batch
                field_id = await el.get_attribute("id") or name or label
                suggested_ans = await ai_generate_tailored_answer(label, profile, job_context=job_context, application_id=application_id) or ""

                collect_unanswered_question(
                    field_id=field_id,
                    label=label,
                    field_type="text",
                    options=[],
                    ai_suggestion=suggested_ans,
                    category="general"
                )
                continue

        except Exception as e:
            err_msg = f"Error handling form element [{el_idx}] (label='{label or 'unknown'}', tag='{tag or 'unknown'}', type='{inp_type or 'unknown'}', id='{el_id or 'unknown'}'): {e}"
            print(f"[FormFiller Error] {err_msg}")
            result.setdefault("warnings", []).append(err_msg)
            continue

    # DYNAMIC FILE CHOOSER FALLBACK (For dropzones/buttons where input[type="file"] is instantiated on click)
    has_uploaded_resume = any("resume" in f["field"].lower() or "attach" in f["field"].lower() for f in result["fields_filled"])
    if not has_uploaded_resume and resume_path and os.path.exists(resume_path):
        try:
            upload_btn = await ctx.query_selector('button:has-text("Attach Resume"), button:has-text("Upload Resume"), [data-automation-id="file-upload-drop-zone"], .btn-upload-resume')
            if upload_btn and await upload_btn.is_visible():
                async with page.expect_file_chooser(timeout=3000) as fc_info:
                    await upload_btn.click()
                file_chooser = await fc_info.value
                await file_chooser.set_files(resume_path)
                result["fields_filled"].append({"field": "Resume Upload", "value": os.path.basename(resume_path)})
                record_filled("resume", "resume", "Resume Upload", "input", "file", resume_path)
        except Exception:
            pass

    # BATCH UNANSWERED QUESTIONS CHECK:
    # If any unknown questions were collected on this page, return the complete batch!
    if unanswered_questions:
        result["needs_input"] = True
        result["unanswered_questions"] = unanswered_questions
        result["unanswered_question"] = unanswered_questions[0]
        result["ai_suggestion"] = unanswered_questions[0].get("ai_suggestion", "")
        result["completed"] = False
        print(f"[FormFiller] Collected batch of {len(unanswered_questions)} unknown questions on page.")
        return result

    # POST-FILL VERIFY STEP: Dedicated verification re-reads every filled widget from live DOM!
    verify_report = await verify_filled_widgets(ctx, result["fields_filled"])
    result["verification_report"] = verify_report
    print(f"[Widget Verification] Re-read {verify_report['verified_count']}/{verify_report['total_checked']} filled widgets in DOM.")
    if not verify_report["all_passed"]:
        print(f"[Widget Verification Warning] {verify_report['failed_count']} filled widgets failed re-read check!")

    # POST-FILL VALIDATION & REQUIRED-FIELD VERIFICATION (Re-reads all fields)
    verification = await verify_form_completion_and_errors(ctx)
    if not verification["is_valid"]:
        result["validation_errors"] = verification["validation_errors"]
        result["empty_required_fields"] = verification["empty_required_fields"]
        print(f"[Form Verification] Validation errors: {verification['validation_errors']}")
        print(f"[Form Verification] Empty required fields: {verification['empty_required_fields']}")

    # MULTI-STEP WIZARD & ACCIDENTAL SUBMIT GUARD
    buttons = await ctx.query_selector_all('button, input[type="submit"]')
    for btn in buttons:
        try:
            if not await btn.is_visible() or await btn.is_disabled():
                continue
            txt = (await btn.inner_text() or await btn.get_attribute("value") or "").lower().strip()
            
            # Submit button guard: submission buttons must NEVER trigger multi-page advance
            is_submit = any(w in txt for w in ["submit", "apply now", "finish", "complete application", "submit application"])
            if is_submit:
                result["submit_button_found"] = True
                continue

            # Only trigger multi-page if it's explicitly an enabled next/step button
            if any(w == txt or f" {w}" in txt or f"{w} " in txt for w in ["next", "step 2", "save and continue", "next page", "next step", "continue to next"]):
                result["has_next_page"] = True
                break
        except Exception:
            pass

    return result
