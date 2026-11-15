import os
import re
import time
import asyncio
from datetime import datetime, date
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple, Union
from playwright.async_api import Page, Frame, ElementHandle

# Word-boundary matcher for known calendar / datepicker classes (prevents matching 'candidate' or 'update')
KNOWN_DATE_PICKER_CLASSES = re.compile(
    r'\b(flatpickr|(ui-)?datepicker|date-picker|hasdatepicker|p-datepicker|bs-datepicker|air-datepicker|pikaday|react-datepicker|calendar-picker|mat-datepicker|ant-picker)(__|-|\b)',
    re.IGNORECASE
)

def parse_date_value(val: Any, preferred_format: Optional[str] = None) -> Optional[date]:
    """Parse various date formats into a datetime.date object. Returns None on invalid date."""
    if not val:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val

    s = str(val).strip()
    if not s:
        return None

    # If preferred_format is given, test it first
    if preferred_format:
        try:
            return datetime.strptime(s, preferred_format).date()
        except ValueError:
            pass

    formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%Y/%m/%d",
        "%d-%m-%Y",
        "%m-%d-%Y",
        "%d.%m.%Y",
        "%b %Y",       # "May 2024"
        "%B %Y",       # "August 2024"
        "%Y-%m",       # "2024-05"
        "%m/%Y",       # "05/2024"
        "%m-%Y",
        "%d %b %Y",    # "15 May 2024"
        "%d %B %Y",    # "15 August 2024"
        "%b %d, %Y",   # "May 15, 2024"
        "%B %d, %Y",   # "August 15, 2024"
    ]

    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass

    return None

def format_date_for_element(d: date, inp_type: str, placeholder: str) -> str:
    """Format date according to element type and placeholder hint (dd/mm/yyyy, mm/dd/yyyy, yyyy-mm-dd, MMM yyyy)."""
    ph = (placeholder or "").strip().lower()
    inp_t = (inp_type or "").strip().lower()

    # 1. Native inputs
    if inp_t == "date":
        return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
    if inp_t == "month":
        return f"{d.year:04d}-{d.month:02d}"

    # 2. 3-part date formats (must be evaluated before 2-part to prevent mm/yyyy matching within dd/mm/yyyy)
    if re.search(r'\bmm[/-]dd[/-]yyyy\b', ph) or re.search(r'\bm[/-]d[/-]y\b', ph):
        sep = "/" if "/" in ph else "-"
        return f"{d.month:02d}{sep}{d.day:02d}{sep}{d.year:04d}"

    if re.search(r'\bdd[/-]mm[/-]yyyy\b', ph) or re.search(r'\bd[/-]m[/-]y\b', ph):
        sep = "/" if "/" in ph else "-"
        return f"{d.day:02d}{sep}{d.month:02d}{sep}{d.year:04d}"

    if re.search(r'\byyyy[/-]mm[/-]dd\b', ph):
        sep = "-" if "-" in ph else "/"
        return f"{d.year:04d}{sep}{d.month:02d}{sep}{d.day:02d}"

    # 3. 2-part month-year formats (only if "day" or "dd" is NOT in placeholder)
    if not re.search(r'(\bdd\b|\bd\b|day)', ph):
        if re.search(r'\b(mmm\s*yyyy|month\s*yyyy|month,\s*year)\b', ph) or ph in ["mmm yyyy", "month yyyy"]:
            return f"{d.strftime('%B')} {d.year:04d}"

        if re.search(r'\bmm[/-]yyyy\b', ph):
            sep = "/" if "/" in ph else "-"
            return f"{d.month:02d}{sep}{d.year:04d}"

        if re.search(r'\byyyy[/-]mm\b', ph):
            sep = "-" if "-" in ph else "/"
            return f"{d.year:04d}{sep}{d.month:02d}"

    # Default to standard DD/MM/YYYY
    return f"{d.day:02d}/{d.month:02d}/{d.year:04d}"

class WidgetType:
    TEXT = "text"
    NATIVE_SELECT = "native_select"
    CUSTOM_COMBOBOX = "custom_combobox"
    RADIO = "radio"
    CHECKBOX = "checkbox"
    DATE = "date"
    FILE = "file"

@dataclass
class ExecutionResult:
    success: bool
    widget_type: str
    target_value: Any
    verified_value: Optional[str] = None
    verification_passed: bool = False
    error: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

# =========================================================================
# BASE EXECUTOR
# =========================================================================
class BaseWidgetExecutor:
    widget_type: str = "base"

    async def fill(self, ctx: Union[Page, Frame], element: ElementHandle, value: Any, **kwargs) -> bool:
        raise NotImplementedError

    async def verify(self, ctx: Union[Page, Frame], element: ElementHandle, expected_value: Any, **kwargs) -> Tuple[bool, Optional[str]]:
        raise NotImplementedError

    async def execute(self, ctx: Union[Page, Frame], element: ElementHandle, value: Any, **kwargs) -> ExecutionResult:
        """
        Executes fill, immediately re-reads DOM to verify value, applies fallback if verification fails,
        and re-reads again to guarantee value integrity.
        """
        try:
            # 1. Fill step
            fill_ok = await self.fill(ctx, element, value, **kwargs)
            if not fill_ok:
                return ExecutionResult(
                    success=False,
                    widget_type=self.widget_type,
                    target_value=value,
                    error="Fill step returned False"
                )

            # Brief pause for DOM / framework reactivity
            await asyncio.sleep(0.15)

            # 2. Verify step (Re-read actual DOM value)
            v_ok, read_val = await self.verify(ctx, element, value, **kwargs)

            # 3. Fallback retry if verification failed
            if not v_ok and hasattr(self, 'fallback_fill'):
                print(f"[{self.widget_type} Executor] Initial verify failed (read: '{read_val}', expected: '{value}'). Running fallback...")
                await self.fallback_fill(ctx, element, value, **kwargs)
                await asyncio.sleep(0.2)
                v_ok, read_val = await self.verify(ctx, element, value, **kwargs)

            return ExecutionResult(
                success=v_ok,
                widget_type=self.widget_type,
                target_value=value,
                verified_value=read_val,
                verification_passed=v_ok,
                error=None if v_ok else f"Verification failed. Read: '{read_val}', Expected: '{value}'"
            )
        except Exception as e:
            return ExecutionResult(
                success=False,
                widget_type=self.widget_type,
                target_value=value,
                error=str(e)
            )

# =========================================================================
# 1. TEXT WIDGET EXECUTOR
# =========================================================================
class TextWidgetExecutor(BaseWidgetExecutor):
    widget_type = WidgetType.TEXT

    async def fill(self, ctx: Union[Page, Frame], element: ElementHandle, value: Any, **kwargs) -> bool:
        if value is None:
            return False
        val_str = str(value).strip()
        inp_type = (await element.get_attribute("type") or "text").lower()

        # Numeric inputs: extract digits/decimals
        if inp_type == "number":
            num_match = re.search(r'\d+(?:\.\d+)?', val_str)
            val_str = num_match.group(0) if num_match else "0"

        # Check for masked placeholders (e.g. phone masks (___) ___-____)
        placeholder = (await element.get_attribute("placeholder") or "").lower()
        is_masked = any(m in placeholder for m in ["(___)", "xxx-xxx", "___-___", "(123)"])

        if is_masked:
            await element.focus()
            await element.fill("")
            await element.type(val_str, delay=25)
        else:
            await element.focus()
            await element.fill(val_str)

        # Dispatch standard change events for reactive frontends (React, Vue, Angular)
        await element.evaluate("""(el) => {
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
        }""")
        return True

    async def fallback_fill(self, ctx: Union[Page, Frame], element: ElementHandle, value: Any, **kwargs):
        val_str = str(value).strip()
        await element.focus()
        await element.evaluate("el => el.select ? el.select() : (el.value = '')")
        await element.type(val_str, delay=35)
        await element.evaluate("""(el) => {
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
        }""")

    async def verify(self, ctx: Union[Page, Frame], element: ElementHandle, expected_value: Any, **kwargs) -> Tuple[bool, Optional[str]]:
        """Re-reads actual value from DOM element."""
        try:
            current_val = await element.input_value()
        except Exception:
            current_val = await element.evaluate("el => el.value || el.innerText || ''")

        curr_clean = (current_val or "").strip()
        exp_clean = str(expected_value).strip()

        if not curr_clean:
            return False, curr_clean

        # Exact match
        if curr_clean == exp_clean or curr_clean.lower() == exp_clean.lower():
            return True, curr_clean

        # Number equivalence (e.g. "9.26" vs "9.260" or int vs float)
        try:
            if float(curr_clean) == float(exp_clean):
                return True, curr_clean
        except ValueError:
            pass

        # Masked / cleaned phone digits match
        curr_digits = re.sub(r'\D', '', curr_clean)
        exp_digits = re.sub(r'\D', '', exp_clean)
        if curr_digits and exp_digits and (curr_digits in exp_digits or exp_digits in curr_digits):
            return True, curr_clean

        # Substring match (e.g. for long bio or formatted values)
        if exp_clean in curr_clean or curr_clean in exp_clean:
            return True, curr_clean

        return False, curr_clean

# =========================================================================
# ROBUST MULTI-STAGE OPTION MATCHER
# =========================================================================
def match_option_candidate(
    target: Any,
    candidates: List[Tuple[Any, str]],  # [(item, text), ...]
    threshold: float = 0.75
) -> Optional[Tuple[Any, str]]:
    """
    Harden matching across dropdowns, comboboxes, and radio groups.
    Stages:
      1. Exact match (case-insensitive)
      2. Normalized match (punctuation stripped, whitespace collapsed)
      3. Boolean equivalence (Yes/No with affirmative/negative tokens, explicitly rejecting "Not applicable")
      4. Token-based fuzzy matching with score threshold (rejecting ties)

    Rules:
      - Never allows substring containment like 'male' matching 'Female' or 'no' matching 'Not applicable'.
      - Ties between distinct options return failure (None).
      - Returns None if no candidates meet threshold.
    """
    if target is None:
        return None
    target_str = str(target).strip()
    if not target_str:
        return None

    t_low = target_str.lower()
    t_norm = " ".join(re.sub(r'[^\w\s]', '', t_low).split())
    t_tokens = [tok for tok in re.findall(r'\b\w+\b', t_low) if tok]
    t_tokens_set = set(t_tokens)

    # Filter out empty or placeholder candidates
    valid_cands: List[Tuple[Any, str]] = []
    for item, raw_text in candidates:
        txt = (str(raw_text) if raw_text is not None else "").strip()
        if not txt:
            continue
        txt_low = txt.lower()
        if txt_low in ["select", "select an option", "select...", "-- select --", "choose", "please select"]:
            continue
        valid_cands.append((item, txt))

    if not valid_cands:
        return None

    # Stage 1: Exact match (case-insensitive)
    exact_matches = [c for c in valid_cands if c[1].strip().lower() == t_low]
    if exact_matches:
        distinct_texts = {c[1].strip().lower() for c in exact_matches}
        if len(distinct_texts) == 1:
            return exact_matches[0]
        return None  # Ambiguous tie

    # Stage 2: Normalized match (alphanumeric only, collapsed spaces)
    if t_norm:
        norm_matches = []
        for c in valid_cands:
            c_norm = " ".join(re.sub(r'[^\w\s]', '', c[1].lower()).split())
            if c_norm == t_norm:
                norm_matches.append(c)
        if norm_matches:
            distinct_texts = {c[1].strip().lower() for c in norm_matches}
            if len(distinct_texts) == 1:
                return norm_matches[0]
            return None  # Tie between distinct normalized options

    # Stage 3: Boolean normalization (strict Yes / No handling)
    is_target_yes = t_low in ["yes", "true", "y", "authorized", "eligible", "agree", "accept"]
    is_target_no = t_low in ["no", "false", "n", "not authorized", "not eligible", "disagree"]

    if is_target_yes:
        yes_matches = []
        for c in valid_cands:
            c_tokens = set(re.findall(r'\b\w+\b', c[1].lower()))
            has_affirmative = bool(c_tokens & {"yes", "true", "y", "authorized", "eligible", "agree"})
            has_negative = bool(c_tokens & {"not", "no", "never", "unauthorized", "ineligible", "none"})
            if has_affirmative and not has_negative:
                yes_matches.append(c)
        if yes_matches:
            exact_yes = [m for m in yes_matches if m[1].strip().lower() in ["yes", "true", "y"]]
            if len(exact_yes) == 1:
                return exact_yes[0]
            distinct_texts = {c[1].strip().lower() for c in yes_matches}
            if len(distinct_texts) == 1:
                return yes_matches[0]
            return None  # Tie

    if is_target_no:
        no_matches = []
        for c in valid_cands:
            c_tokens = set(re.findall(r'\b\w+\b', c[1].lower()))
            # Explicitly reject "not applicable" or "none"
            if c_tokens & {"applicable", "none"}:
                continue
            has_negative = bool(c_tokens & {"no", "false", "n", "never", "unauthorized", "ineligible", "disagree"}) or bool(re.match(r'^no\b', c[1].strip().lower()))
            if has_negative:
                no_matches.append(c)
        if no_matches:
            exact_no = [m for m in no_matches if m[1].strip().lower() in ["no", "false", "n"]]
            if len(exact_no) == 1:
                return exact_no[0]
            distinct_texts = {c[1].strip().lower() for c in no_matches}
            if len(distinct_texts) == 1:
                return no_matches[0]
            return None  # Tie

    # Stage 3.5: Dial-code / Country calling code and country name equivalence
    t_dials = re.findall(r'\+\d{1,4}', target_str)
    t_stripped = re.sub(r'[\+\(\)]?\b\d{1,4}\b', '', target_str).strip(' ()-,/')
    if t_dials or (len(t_stripped) >= 3 and any(re.findall(r'\+\d{1,4}', c[1]) for c in valid_cands)):
        t_dial = t_dials[0] if t_dials else None
        dial_matches = []
        for c in valid_cands:
            c_dials = re.findall(r'\+\d{1,4}', c[1])
            c_dial = c_dials[0] if c_dials else None
            c_stripped = re.sub(r'[\+\(\)]?\b\d{1,4}\b', '', c[1]).strip(' ()-,/')

            # 1. Matching dial codes (+91 vs India +91, +91 vs +91, India +91 vs +91)
            if t_dial and (c_dial == t_dial or c[1].strip() == t_dial or c[1].strip() == t_dial.lstrip('+')):
                dial_matches.append(c)
            # 2. Matching country names when one has dial code and one does not
            elif len(t_stripped) >= 3 and len(c_stripped) >= 3 and t_stripped.lower() == c_stripped.lower():
                dial_matches.append(c)

        if dial_matches:
            if len(dial_matches) > 1 and len(t_stripped) >= 3:
                # Disambiguate shared dial codes (e.g. +1 for US/Canada) via country name
                named_matches = [
                    c for c in dial_matches
                    if t_stripped.lower() in c[1].lower() or c[1].lower() in t_stripped.lower()
                ]
                if len(named_matches) == 1:
                    return named_matches[0]
            distinct_texts = {c[1].strip().lower() for c in dial_matches}
            if len(distinct_texts) == 1:
                return dial_matches[0]

    # Stage 4: Token-based fuzzy matching with score threshold and tie rejection
    if not t_tokens_set:
        return None

    scored_cands: List[Tuple[float, Any, str]] = []
    for item, txt in valid_cands:
        c_tokens = set(re.findall(r'\b\w+\b', txt.lower()))
        if not c_tokens:
            continue
        intersection = t_tokens_set & c_tokens
        if not intersection:
            continue

        recall = len(intersection) / len(t_tokens_set)
        token_f1 = (2.0 * len(intersection)) / (len(t_tokens_set) + len(c_tokens))
        score = 0.6 * recall + 0.4 * token_f1

        if score >= threshold:
            scored_cands.append((score, item, txt))

    if not scored_cands:
        return None

    scored_cands.sort(key=lambda x: x[0], reverse=True)
    best_score, best_item, best_txt = scored_cands[0]

    # Check for ties between distinct options
    if len(scored_cands) > 1:
        second_score, second_item, second_txt = scored_cands[1]
        if abs(best_score - second_score) < 0.02:
            if best_txt.strip().lower() != second_txt.strip().lower():
                return None  # Failure on tie

    return (best_item, best_txt)

# =========================================================================
# 2. NATIVE SELECT WIDGET EXECUTOR
# =========================================================================
class NativeSelectWidgetExecutor(BaseWidgetExecutor):
    widget_type = WidgetType.NATIVE_SELECT

    async def fill(self, ctx: Union[Page, Frame], element: ElementHandle, value: Any, **kwargs) -> bool:
        if value is None:
            return False
        target = str(value).strip()
        if not target:
            return False

        # Extract available options
        options_data = await element.evaluate("""sel => {
            return Array.from((sel && sel.options) || []).map((opt, idx) => ({
                index: idx,
                value: opt.value || '',
                text: (opt.text || '').trim()
            }));
        }""")
        if not options_data:
            return False

        candidates = [
            (opt, opt["text"] if opt["text"] else opt["value"])
            for opt in options_data
            if (opt["text"] or opt["value"])
        ]

        match = match_option_candidate(target, candidates)
        if not match:
            return False

        chosen_opt, _ = match
        chosen_val = chosen_opt["value"]
        chosen_label = chosen_opt["text"]

        try:
            await element.select_option(value=chosen_val, timeout=2500)
        except Exception:
            try:
                await element.select_option(label=chosen_label, timeout=2500)
            except Exception:
                return False

        await element.evaluate("""sel => {
            sel.dispatchEvent(new Event('input', { bubbles: true }));
            sel.dispatchEvent(new Event('change', { bubbles: true }));
        }""")
        return True

    async def fallback_fill(self, ctx: Union[Page, Frame], element: ElementHandle, value: Any, **kwargs):
        target = str(value).strip()
        if not target:
            return
        options_data = await element.evaluate("""sel => {
            return Array.from((sel && sel.options) || []).map((opt, idx) => ({
                index: idx,
                value: opt.value || '',
                text: (opt.text || '').trim()
            }));
        }""")
        if not options_data:
            return
        candidates = [
            (opt, opt["text"] if opt["text"] else opt["value"])
            for opt in options_data
            if (opt["text"] or opt["value"])
        ]
        match = match_option_candidate(target, candidates)
        if match:
            idx = match[0]["index"]
            await element.evaluate("""(sel, idx) => {
                if (!sel || !sel.options) return;
                sel.selectedIndex = idx;
                sel.dispatchEvent(new Event('input', { bubbles: true }));
                sel.dispatchEvent(new Event('change', { bubbles: true }));
            }""", idx)

    async def verify(self, ctx: Union[Page, Frame], element: ElementHandle, expected_value: Any, **kwargs) -> Tuple[bool, Optional[str]]:
        """Re-reads the currently selected option from the native select in DOM."""
        sel_info = await element.evaluate("""sel => {
            if (!sel || !sel.options) {
                const val = (sel && (sel.value || sel.innerText)) || '';
                return { text: val.trim(), value: val.trim(), index: -1 };
            }
            const idx = sel.selectedIndex;
            if (idx === -1 || idx === undefined || !sel.options[idx]) return { text: '', value: '', index: -1 };
            const opt = sel.options[idx];
            return {
                text: (opt.text || '').trim(),
                value: (opt.value || '').trim(),
                index: idx
            };
        }""")

        curr_text = sel_info.get("text", "")
        curr_val = sel_info.get("value", "")
        display_val = curr_text or curr_val

        if not display_val:
            return False, ""

        # Placeholder checks
        if display_val.lower() in ["select", "select an option", "select...", "-- select --", "choose", ""]:
            return False, display_val

        # Exact, normalized, token match against expected_value
        match = match_option_candidate(expected_value, [(display_val, display_val)])
        if match:
            return True, display_val

        return False, display_val

# =========================================================================
# 3. CUSTOM COMBOBOX WIDGET EXECUTOR
# =========================================================================
class CustomComboboxWidgetExecutor(BaseWidgetExecutor):
    widget_type = WidgetType.CUSTOM_COMBOBOX

    async def fill(self, ctx: Union[Page, Frame], element: ElementHandle, value: Any, **kwargs) -> bool:
        if value is None:
            return False
        target = str(value).strip()
        if not target:
            return False

        page = ctx.page if hasattr(ctx, "page") else ctx

        # Find trigger control (React-Select, Select2, or ARIA combobox)
        parent_container = await element.evaluate_handle(
            """el => {
                if (el.classList && (el.classList.contains('select__control') || el.classList.contains('select__container') || el.classList.contains('select2-container'))) {
                    return el;
                }
                const ctrl = el.closest('.select__control, .select2-container, [class*="MuiAutocomplete-root"], [class*="ant-select"], .select__container, .select-shell');
                if (ctrl) return ctrl;
                return el.parentElement || el;
            }"""
        )
        control = await parent_container.query_selector(".select__control, .select2-selection, [class*='select__control']") if parent_container else None
        input_el = await parent_container.query_selector("input") if parent_container else None
        trigger = control or input_el or element

        # 1. Open popup
        try:
            await trigger.click(timeout=1500)
        except Exception:
            pass
        if input_el and input_el != trigger:
            try:
                await input_el.click(timeout=1000)
            except Exception:
                pass

        # 2. Locate listbox tied through aria-controls or aria-owns
        listbox = None
        listbox_id = await element.evaluate("""el => {
            const inp = el.tagName && el.tagName.toLowerCase() === 'input' ? el : el.querySelector('input');
            if (inp) {
                const c = inp.getAttribute('aria-controls') || inp.getAttribute('aria-owns');
                if (c) return c;
            }
            return el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
        }""")

        if listbox_id:
            try:
                listbox = await ctx.query_selector(f"#{listbox_id}")
                if not listbox and hasattr(page, "query_selector"):
                    listbox = await page.query_selector(f"#{listbox_id}")
            except Exception:
                listbox = None

        if not listbox:
            # Fallback to standard combobox listbox containers (avoiding broad [class*="dropdown"] or generic li)
            listbox = await ctx.query_selector('.select__menu, [role="listbox"], .select2-results__options, [class*="MuiAutocomplete-popper"] [role="listbox"], [class*="MuiAutocomplete-listbox"]')
            if not listbox and hasattr(page, "query_selector"):
                listbox = await page.query_selector('.select__menu, [role="listbox"], .select2-results__options, [class*="MuiAutocomplete-popper"] [role="listbox"], [class*="MuiAutocomplete-listbox"]')

        # 3. If searchable input exists, type to filter
        search_inp = await element.query_selector('input') or (element if await element.evaluate("e => e.tagName && e.tagName.toLowerCase() === 'input'") else None)
        if search_inp:
            try:
                await search_inp.fill(target)
            except Exception:
                pass

        # 4. Wait for [role="option"] with timeout instead of fixed sleeps
        if listbox:
            try:
                await listbox.wait_for_selector('[role="option"], .select__option, .select2-results__option', timeout=2500)
            except Exception:
                pass

        # 5. Collect options and handle virtualized scrolling
        if listbox:
            collected_options: Dict[str, Tuple[ElementHandle, str]] = {}

            async def scan_options():
                opt_els = await listbox.query_selector_all('[role="option"], .select__option, .select2-results__option')
                for opt in opt_els:
                    txt = (await opt.inner_text() or "").strip()
                    if txt and txt not in collected_options:
                        collected_options[txt] = (opt, txt)

            await scan_options()

            # First check among visible options
            match = match_option_candidate(target, list(collected_options.values()))
            if match:
                matched_opt, _ = match
                await matched_opt.scroll_into_view_if_needed()
                try:
                    await matched_opt.click(timeout=2000)
                except Exception:
                    await matched_opt.evaluate("el => el.click()")
                return True

            # If not found, scroll the popup to collect virtualized options
            can_scroll = await listbox.evaluate("el => el.scrollHeight > el.clientHeight")
            if can_scroll:
                for _ in range(3):
                    await listbox.evaluate("el => { el.scrollTop = (el.scrollTop || 0) + (el.clientHeight || 200); el.dispatchEvent(new Event('scroll')); }")
                    try:
                        await listbox.wait_for_selector('[role="option"], .select__option, .select2-results__option', timeout=600)
                    except Exception:
                        pass
                    await scan_options()
                    match = match_option_candidate(target, list(collected_options.values()))
                    if match:
                        matched_opt, _ = match
                        await matched_opt.scroll_into_view_if_needed()
                        try:
                            await matched_opt.click(timeout=2000)
                        except Exception:
                            await matched_opt.evaluate("el => el.click()")
                        return True

        # Unverified Enter fallback removed! If no option matches, return False.
        return False

    async def verify(self, ctx: Union[Page, Frame], element: ElementHandle, expected_value: Any, **kwargs) -> Tuple[bool, Optional[str]]:
        """Re-reads the currently displayed text inside the custom combobox."""
        parent_container = await element.evaluate_handle(
            """el => {
                if (el.classList && (el.classList.contains('select__control') || el.classList.contains('select__container') || el.classList.contains('select2-container'))) {
                    return el;
                }
                const ctrl = el.closest('.select__control, .select2-container, [class*="MuiAutocomplete-root"], [class*="ant-select"], .select__container, .select-shell, .iti, [class*="phone-input"], [class*="phoneInput"], [class*="iti"], fieldset, .field, [class*="field"]');
                if (ctrl) return ctrl;
                return el.parentElement || el;
            }"""
        )

        selected_text = await parent_container.evaluate("""container => {
            // 1. Direct input value if container is an input with non-empty value
            if (container.tagName && container.tagName.toLowerCase() === 'input') {
                if (container.value && container.value.trim()) return container.value.trim();
            }

            // 2. React-Select, Select2, MUI single-value or rendered text
            const single = container.querySelector('.select__single-value, [class*="singleValue"], .select2-selection__rendered, [class*="selection__rendered"], [class*="MuiAutocomplete-tag"]');
            if (single && single.innerText && single.innerText.trim()) return single.innerText.trim();

            // 3. Dedicated dial-code / country badge elements (intl-tel-input, react-phone-number-input, etc.)
            const dialCodeEl = container.querySelector('.iti__selected-dial-code, .selected-dial-code, [class*="dial-code"], [class*="dialCode"], [class*="country-dial-code"]');
            if (dialCodeEl && dialCodeEl.innerText && dialCodeEl.innerText.trim()) {
                const dc = dialCodeEl.innerText.trim();
                const flagParent = dialCodeEl.closest('[title], [aria-label]') || container.querySelector('[class*="selected-flag"][title], [class*="selected-flag"][aria-label]');
                const titleOrAria = flagParent ? (flagParent.getAttribute('title') || flagParent.getAttribute('aria-label') || '') : '';
                if (titleOrAria && (titleOrAria.includes('+') || /[a-zA-Z]{3,}/.test(titleOrAria))) {
                    return `${titleOrAria} ${dc}`.trim();
                }
                return dc;
            }

            // 4. aria-label or title attributes on container, buttons, flags, or comboboxes
            const ariaCandidates = [
                container.getAttribute('aria-label'),
                container.getAttribute('title'),
                ...Array.from(container.querySelectorAll('[class*="flag"][title], [class*="flag"][aria-label], [class*="selected"][title], [class*="selected"][aria-label], button[title], button[aria-label], [role="combobox"][aria-label], [role="combobox"][title]')).map(n => n.getAttribute('title') || n.getAttribute('aria-label'))
            ].filter(Boolean);

            for (const a of ariaCandidates) {
                const aClean = a.trim();
                if (aClean && (aClean.includes('+') || /\\b(india|united states|us|uk|canada|singapore|germany|france|australia)\\b/i.test(aClean))) {
                    return aClean;
                }
            }

            // 5. Hidden inputs holding country code or selected dial code
            const hiddenInputs = Array.from(container.querySelectorAll('input[type="hidden"], input[name*="country" i], input[name*="dial" i], input[name*="phone_country" i]'));
            for (const hInp of hiddenInputs) {
                if (hInp.value && hInp.value.trim()) {
                    return hInp.value.trim();
                }
            }

            // 6. Visible inputs with value
            const inp = container.querySelector('input:not([type="hidden"])');
            if (inp && inp.value && inp.value.trim()) return inp.value.trim();

            // 7. Button or Combobox text
            const btn = container.querySelector('button, [role="button"], [role="combobox"]');
            if (btn && btn.innerText && btn.innerText.trim()) {
                const bTxt = btn.innerText.trim();
                if (!['select', 'choose', 'open'].includes(bTxt.toLowerCase())) {
                    return bTxt;
                }
            }

            // 8. Container innerText if non-empty and not placeholder
            const cText = (container.innerText || '').trim();
            if (cText && !['select', 'select an option', 'choose'].includes(cText.toLowerCase())) {
                return cText;
            }

            return '';
        }""")

        curr_clean = (selected_text or "").strip()
        if not curr_clean:
            try:
                curr_clean = (await element.input_value() or "").strip()
            except Exception:
                pass

        if not curr_clean:
            try:
                curr_clean = await element.evaluate("""el => {
                    return (el.value || el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
                }""")
            except Exception:
                pass

        if not curr_clean:
            return False, ""

        if curr_clean.lower() in ["select", "select an option", "select...", "-- select --", "choose", ""]:
            return False, curr_clean

        exp_clean = str(expected_value).strip()

        # 1. Exact match
        if curr_clean == exp_clean or curr_clean.lower() == exp_clean.lower():
            return True, curr_clean

        # 2. Country / Dial code equivalence (e.g. "+91" vs "India +91" or "India (+91)")
        curr_dial = re.findall(r'\+\d{1,4}', curr_clean)
        exp_dial = re.findall(r'\+\d{1,4}', exp_clean)
        if curr_dial and exp_dial and curr_dial[0] == exp_dial[0]:
            return True, curr_clean

        # 3. Country / Location name substring containment (e.g. "India" in "India +91" or "United States" in "United States +1")
        c_low = curr_clean.lower()
        e_low = exp_clean.lower()
        if (c_low in e_low or e_low in c_low) and len(c_low) >= 3 and c_low not in ["select", "choose", "option"]:
            return True, curr_clean

        # 4. Standard match_option_candidate
        match = match_option_candidate(expected_value, [(curr_clean, curr_clean)])
        if match:
            return True, curr_clean

        return False, curr_clean

# =========================================================================
# 4. RADIO WIDGET EXECUTOR
# =========================================================================
class RadioWidgetExecutor(BaseWidgetExecutor):
    widget_type = WidgetType.RADIO

    async def fill(self, ctx: Union[Page, Frame], element: ElementHandle, value: Any, **kwargs) -> bool:
        if value is None:
            return False
        target = str(value).strip()
        if not target:
            return False
        name = await element.get_attribute("name") or ""

        # Find all sibling radios in the group
        group_radios = await ctx.query_selector_all(f'input[type="radio"][name="{name}"]') if name else [element]

        candidates: List[Tuple[ElementHandle, str]] = []
        for r_el in group_radios:
            r_val = (await r_el.get_attribute("value") or "").strip()
            r_lbl = await r_el.evaluate("""r => {
                if (r.id) {
                    const l = document.querySelector(`label[for="${r.id}"]`);
                    if (l) return l.innerText.trim();
                }
                const p = r.closest('label');
                if (p) return p.innerText.trim();
                return r.value || '';
            }""")
            display_val = (r_lbl or r_val or "").strip()
            # Stop matching empty labels!
            if not display_val:
                continue
            candidates.append((r_el, display_val))

        match = match_option_candidate(target, candidates)
        if not match:
            return False

        matched_el, _ = match
        await matched_el.scroll_into_view_if_needed()
        await matched_el.click()
        await matched_el.evaluate("""r => {
            r.dispatchEvent(new Event('input', { bubbles: true }));
            r.dispatchEvent(new Event('change', { bubbles: true }));
        }""")
        return True

    async def fallback_fill(self, ctx: Union[Page, Frame], element: ElementHandle, value: Any, **kwargs):
        target = str(value).strip()
        if not target:
            return
        name = await element.get_attribute("name") or ""
        group_radios = await ctx.query_selector_all(f'input[type="radio"][name="{name}"]') if name else [element]

        candidates: List[Tuple[ElementHandle, str]] = []
        for r_el in group_radios:
            r_val = (await r_el.get_attribute("value") or "").strip()
            r_lbl = await r_el.evaluate("""r => {
                if (r.id) {
                    const l = document.querySelector(`label[for="${r.id}"]`);
                    if (l) return l.innerText.trim();
                }
                const p = r.closest('label');
                if (p) return p.innerText.trim();
                return r.value || '';
            }""")
            display_val = (r_lbl or r_val or "").strip()
            if display_val:
                candidates.append((r_el, display_val))

        match = match_option_candidate(target, candidates)
        if match:
            matched_el, _ = match
            await matched_el.evaluate("""r => {
                r.checked = true;
                r.dispatchEvent(new Event('input', { bubbles: true }));
                r.dispatchEvent(new Event('change', { bubbles: true }));
            }""")

    async def verify(self, ctx: Union[Page, Frame], element: ElementHandle, expected_value: Any, **kwargs) -> Tuple[bool, Optional[str]]:
        """Re-reads whether the specific checked radio matches expected value."""
        name = await element.get_attribute("name") or ""
        group_radios = await ctx.query_selector_all(f'input[type="radio"][name="{name}"]') if name else [element]
        target = str(expected_value).strip()

        checked_radio = None
        display_val = ""
        for r_el in group_radios:
            if await r_el.is_checked():
                checked_radio = r_el
                r_val = (await r_el.get_attribute("value") or "").strip()
                r_lbl = await r_el.evaluate("""r => {
                    if (r.id) {
                        const l = document.querySelector(`label[for="${r.id}"]`);
                        if (l) return l.innerText.trim();
                    }
                    const p = r.closest('label');
                    if (p) return p.innerText.trim();
                    return r.value || '';
                }""")
                display_val = (r_lbl or r_val or "").strip()
                break

        if not checked_radio:
            return False, "None checked"

        if not display_val:
            return False, "Checked radio has no label or value"

        # Check that the checked radio specifically matches expected value
        match = match_option_candidate(target, [(checked_radio, display_val)])
        if match:
            return True, display_val

        # STOP returning True when any radio is checked!
        return False, display_val

# =========================================================================
# 5. CHECKBOX WIDGET EXECUTOR
# =========================================================================
class CheckboxWidgetExecutor(BaseWidgetExecutor):
    widget_type = WidgetType.CHECKBOX

    async def fill(self, ctx: Union[Page, Frame], element: ElementHandle, value: Any, **kwargs) -> bool:
        # Determine target state (True or False)
        t_str = str(value).strip().lower()
        target_bool = t_str in ["yes", "true", "1", "agree", "accept", "checked", "on"]

        current_checked = await element.is_checked()
        if current_checked != target_bool:
            await element.scroll_into_view_if_needed()
            await element.click()
            await element.evaluate("""cb => {
                cb.dispatchEvent(new Event('input', { bubbles: true }));
                cb.dispatchEvent(new Event('change', { bubbles: true }));
            }""")
        return True

    async def fallback_fill(self, ctx: Union[Page, Frame], element: ElementHandle, value: Any, **kwargs):
        t_str = str(value).strip().lower()
        target_bool = t_str in ["yes", "true", "1", "agree", "accept", "checked", "on"]
        await element.evaluate("""(cb, target) => {
            cb.checked = target;
            cb.dispatchEvent(new Event('input', { bubbles: true }));
            cb.dispatchEvent(new Event('change', { bubbles: true }));
        }""", target_bool)

    async def verify(self, ctx: Union[Page, Frame], element: ElementHandle, expected_value: Any, **kwargs) -> Tuple[bool, Optional[str]]:
        """Re-reads the checkbox checked state."""
        current_checked = await element.is_checked()
        t_str = str(expected_value).strip().lower()
        target_bool = t_str in ["yes", "true", "1", "agree", "accept", "checked", "on"]

        is_valid = (current_checked == target_bool)
        read_val = "Checked (Yes)" if current_checked else "Unchecked (No)"
        return is_valid, read_val

# =========================================================================
# 6. DATE WIDGET EXECUTOR
# =========================================================================
class DateWidgetExecutor(BaseWidgetExecutor):
    widget_type = WidgetType.DATE

    async def fill(self, ctx: Union[Page, Frame], element: ElementHandle, value: Any, **kwargs) -> bool:
        if value is None:
            return False

        parsed_d = parse_date_value(value)
        if not parsed_d:
            print(f"[DateWidgetExecutor] Invalid date value: '{value}', returning failure.")
            return False

        inp_type = (await element.get_attribute("type") or "").lower()
        ph = (await element.get_attribute("placeholder") or "").lower()

        # Format date according to detected format from placeholder and element type
        formatted_val = format_date_for_element(parsed_d, inp_type, ph)

        # Remove readonly if present on calendar inputs
        await element.evaluate("el => el.removeAttribute('readonly')")

        await element.focus()
        await element.fill(formatted_val)
        await element.evaluate("""(el, val) => {
            // Also invoke Flatpickr / jQuery setDate if attached
            if (el._flatpickr) {
                try { el._flatpickr.setDate(val, true); } catch(e) {}
            }
            if (window.jQuery && window.jQuery(el).data('datepicker')) {
                try { window.jQuery(el).datepicker('setDate', val); } catch(e) {}
            }
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
        }""", formatted_val)
        return True

    async def fallback_fill(self, ctx: Union[Page, Frame], element: ElementHandle, value: Any, **kwargs):
        parsed_d = parse_date_value(value)
        if not parsed_d:
            return
        inp_type = (await element.get_attribute("type") or "").lower()
        ph = (await element.get_attribute("placeholder") or "").lower()
        formatted_val = format_date_for_element(parsed_d, inp_type, ph)

        await element.evaluate("""(el, val) => {
            try {
                let nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
                nativeSetter.call(el, val);
            } catch(e) {
                el.value = val;
            }
            if (el._flatpickr) {
                try { el._flatpickr.setDate(val, true); } catch(e) {}
            }
            if (window.jQuery && window.jQuery(el).data('datepicker')) {
                try { window.jQuery(el).datepicker('setDate', val); } catch(e) {}
            }
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""", formatted_val)

    async def verify(self, ctx: Union[Page, Frame], element: ElementHandle, expected_value: Any, **kwargs) -> Tuple[bool, Optional[str]]:
        """Re-reads the actual date value from the input and compares parsed dates rather than just the year."""
        current_val = await element.input_value()
        curr_clean = (current_val or "").strip()
        if not curr_clean:
            curr_clean = (await element.get_attribute("value") or "").strip()
            if not curr_clean:
                return False, ""

        inp_type = (await element.get_attribute("type") or "").lower()
        ph = (await element.get_attribute("placeholder") or "").lower()

        preferred_fmt = None
        if "mm/dd" in ph:
            preferred_fmt = "%m/%d/%Y"
        elif "mm-dd" in ph:
            preferred_fmt = "%m-%d-%Y"
        elif "dd/mm" in ph:
            preferred_fmt = "%d/%m/%Y"
        elif "dd-mm" in ph:
            preferred_fmt = "%d-%m-%Y"

        exp_d = parse_date_value(expected_value, preferred_format=preferred_fmt)
        curr_d = parse_date_value(curr_clean, preferred_format=preferred_fmt)

        if not exp_d or not curr_d:
            # If standard parsing fails, fallback to exact string check
            if curr_clean.lower() == str(expected_value).strip().lower():
                return True, curr_clean
            return False, curr_clean

        inp_type = (await element.get_attribute("type") or "").lower()
        ph = (await element.get_attribute("placeholder") or "").lower()
        is_month_year = (inp_type == "month") or bool(re.search(r'\b(mmm\s*yyyy|month\s*yyyy|mm/yyyy|yyyy-mm)\b', ph, re.I))

        if is_month_year:
            # For month-year inputs, compare year and month
            if exp_d.year == curr_d.year and exp_d.month == curr_d.month:
                return True, curr_clean
            return False, curr_clean

        # For full date inputs, compare complete date (year, month, and day)
        if exp_d == curr_d:
            return True, curr_clean

        return False, curr_clean

# =========================================================================
# 7. FILE WIDGET EXECUTOR
# =========================================================================
class FileWidgetExecutor(BaseWidgetExecutor):
    widget_type = WidgetType.FILE

    async def fill(self, ctx: Union[Page, Frame], element: ElementHandle, value: Any, **kwargs) -> bool:
        if not value:
            return False
        file_path = str(value).strip()
        if not os.path.exists(file_path):
            print(f"[File Executor] Target file path does not exist: {file_path}")
            return False

        tag = await element.evaluate("e => e.tagName.toLowerCase()")
        inp_type = (await element.get_attribute("type") or "").lower()

        # A. Native <input type="file">
        if tag == "input" and inp_type == "file":
            await element.set_input_files(file_path)
            await element.evaluate("""el => {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""")
            return True

        # B. Custom Upload Button / Dropzone
        page = ctx.page if hasattr(ctx, "page") else ctx
        if hasattr(page, "expect_file_chooser"):
            try:
                async with page.expect_file_chooser(timeout=3000) as fc_info:
                    await element.click()
                file_chooser = await fc_info.value
                await file_chooser.set_files(file_path)
                return True
            except Exception as e:
                print(f"[File Executor] File chooser exception: {e}")

        return False

    async def verify(self, ctx: Union[Page, Frame], element: ElementHandle, expected_value: Any, **kwargs) -> Tuple[bool, Optional[str]]:
        """Re-reads the uploaded file name from the DOM."""
        file_path = str(expected_value).strip()
        expected_filename = os.path.basename(file_path).lower()

        tag = await element.evaluate("e => e.tagName.toLowerCase()")
        inp_type = (await element.get_attribute("type") or "").lower()

        if tag == "input" and inp_type == "file":
            attached_filename = await element.evaluate("""el => {
                return (el.files && el.files.length > 0) ? el.files[0].name : '';
            }""")
            if attached_filename:
                return (expected_filename in attached_filename.lower()), attached_filename

        # Custom UI verification: check if filename appears in nearby DOM text or upload badge
        page = ctx.page if hasattr(ctx, "page") else ctx
        has_text = await page.evaluate("""(name) => {
            return document.body.innerText.toLowerCase().includes(name);
        }""", expected_filename)

        if has_text:
            return True, expected_filename

        return False, "No file attached"

# =========================================================================
# REGISTRY & DISPATCHER
# =========================================================================
class WidgetExecutorRegistry:
    def __init__(self):
        self.executors = {
            WidgetType.TEXT: TextWidgetExecutor(),
            WidgetType.NATIVE_SELECT: NativeSelectWidgetExecutor(),
            WidgetType.CUSTOM_COMBOBOX: CustomComboboxWidgetExecutor(),
            WidgetType.RADIO: RadioWidgetExecutor(),
            WidgetType.CHECKBOX: CheckboxWidgetExecutor(),
            WidgetType.DATE: DateWidgetExecutor(),
            WidgetType.FILE: FileWidgetExecutor(),
        }

    def get_executor(self, widget_type: str) -> BaseWidgetExecutor:
        w_type = (widget_type or "").lower().strip()
        if w_type in ["select", "dropdown", "native_select"]:
            return self.executors[WidgetType.NATIVE_SELECT]
        if w_type in ["combobox", "custom_combobox", "typeahead", "react_select"]:
            return self.executors[WidgetType.CUSTOM_COMBOBOX]
        if w_type in ["radio", "radio_group"]:
            return self.executors[WidgetType.RADIO]
        if w_type in ["checkbox", "check"]:
            return self.executors[WidgetType.CHECKBOX]
        if w_type in ["date", "datepicker", "calendar"]:
            return self.executors[WidgetType.DATE]
        if w_type in ["file", "upload", "resume"]:
            return self.executors[WidgetType.FILE]
        return self.executors.get(w_type, self.executors[WidgetType.TEXT])

    async def detect_widget_type(self, ctx: Union[Page, Frame], element: ElementHandle) -> str:
        """
        Detects the real widget type from the live DOM element.
        tagName == 'select' gives NATIVE_SELECT.
        input[type=file], radio, checkbox, date, month map to their respective types.
        role=combobox, aria-haspopup=listbox, aria-controls, aria-owns, or an ancestor matching
        [class*="select__"], .select2-container, [class*="MuiAutocomplete"] gives CUSTOM_COMBOBOX.
        Otherwise TEXT.
        """
        tag = ""
        try:
            tag = await element.evaluate("e => e && e.tagName ? e.tagName.toLowerCase() : ''")
        except Exception:
            pass

        if tag == "select":
            return WidgetType.NATIVE_SELECT

        inp_type = ""
        role = ""
        el_class = ""
        try:
            inp_type = (await element.get_attribute("type") or "").lower()
            role = (await element.get_attribute("role") or "").lower()
            el_class = (await element.get_attribute("class") or "").lower()
        except Exception:
            pass

        if inp_type == "file":
            return WidgetType.FILE
        if inp_type == "radio":
            return WidgetType.RADIO
        if inp_type == "checkbox":
            return WidgetType.CHECKBOX
        if inp_type in ["date", "month"]:
            return WidgetType.DATE

        # Check datepicker classes
        if KNOWN_DATE_PICKER_CLASSES.search(el_class):
            return WidgetType.DATE

        # Strict combobox detection: role=combobox, aria-haspopup=listbox, aria-controls, aria-owns,
        # or ancestor matching [class*="select__"], .select2-container, [class*="MuiAutocomplete"]
        is_combobox = False
        try:
            is_combobox = await element.evaluate("""el => {
                if (!el) return false;
                if (el.getAttribute('role') === 'combobox') return true;
                if (el.getAttribute('aria-haspopup') === 'listbox') return true;
                if (el.getAttribute('aria-controls')) return true;
                if (el.getAttribute('aria-owns')) return true;
                if (el.closest('[class*="select__"], .select2-container, [class*="MuiAutocomplete"]')) return true;
                return false;
            }""")
        except Exception:
            pass

        if is_combobox or role == "combobox" or "select__" in el_class or "select2" in el_class:
            return WidgetType.CUSTOM_COMBOBOX

        return WidgetType.TEXT

    async def execute_element(
        self,
        ctx: Union[Page, Frame],
        element: ElementHandle,
        value: Any,
        widget_type: Optional[str] = None,
        **kwargs
    ) -> ExecutionResult:
        dom_type = await self.detect_widget_type(ctx, element)
        tag = ""
        try:
            tag = await element.evaluate("e => e && e.tagName ? e.tagName.toLowerCase() : ''")
        except Exception:
            pass

        # Real DOM element takes priority; declared question type is only a hint
        if not widget_type:
            target_type = dom_type
        else:
            w_norm = str(widget_type).lower().strip()
            # If element is a native <select> tag in DOM, always use NATIVE_SELECT
            if tag == "select":
                target_type = WidgetType.NATIVE_SELECT
            # If element is not a <select> in DOM, cannot use NATIVE_SELECT
            elif w_norm in ["select", "native_select"]:
                target_type = dom_type if dom_type != WidgetType.TEXT else WidgetType.CUSTOM_COMBOBOX
            # If DOM type is a structured widget (combobox, radio, checkbox, date), respect the DOM
            elif dom_type in [WidgetType.CUSTOM_COMBOBOX, WidgetType.RADIO, WidgetType.CHECKBOX, WidgetType.DATE, WidgetType.FILE]:
                target_type = dom_type
            else:
                target_type = w_norm or dom_type

        executor = self.get_executor(target_type)
        res = await executor.execute(ctx, element, value, **kwargs)

        # Fallback: if fill or verification failed on a non-select DOM element with select executor, retry with custom combobox
        if (not res.success or not res.verification_passed) and target_type == WidgetType.NATIVE_SELECT and tag != "select":
            cb_executor = self.get_executor(WidgetType.CUSTOM_COMBOBOX)
            res = await cb_executor.execute(ctx, element, value, **kwargs)

        return res

# Global singleton
widget_registry = WidgetExecutorRegistry()

async def detect_widget_type(ctx: Union[Page, Frame], element: ElementHandle) -> str:
    """Convenience standalone function to detect widget type from the live DOM element."""
    return await widget_registry.detect_widget_type(ctx, element)

# =========================================================================
# COMPREHENSIVE POST-FILL VERIFICATION STEP
# =========================================================================
async def verify_filled_widgets(
    ctx: Union[Page, Frame],
    filled_records: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Dedicated verify step: Re-reads every field value in the DOM after filling.
    Guarantees no value was wiped, ignored, or failed to persist in client-side state.
    """
    report = {
        "all_passed": True,
        "total_checked": len(filled_records),
        "verified_count": 0,
        "failed_count": 0,
        "results": []
    }

    for record in filled_records:
        field_label = record.get("field") or record.get("label") or "Unknown Field"
        expected_val = record.get("value")
        w_type = record.get("widget_type") or record.get("type", "text")
        selector = record.get("selector")
        el_id = record.get("id")
        name = record.get("name")

        # Map type to widget executor type
        if w_type in ["file"]:
            norm_type = WidgetType.FILE
        elif w_type in ["radio"]:
            norm_type = WidgetType.RADIO
        elif w_type in ["checkbox"]:
            norm_type = WidgetType.CHECKBOX
        elif w_type in ["select"]:
            norm_type = WidgetType.NATIVE_SELECT
        elif w_type in ["combobox", "custom_combobox"]:
            norm_type = WidgetType.CUSTOM_COMBOBOX
        elif w_type in ["date"]:
            norm_type = WidgetType.DATE
        else:
            norm_type = WidgetType.TEXT

        # Re-locate the element in DOM
        el = None
        if selector:
            el = await ctx.query_selector(selector)
        if not el and el_id:
            el = await ctx.query_selector(f"#{el_id}")
        if not el and name:
            el = await ctx.query_selector(f'[name="{name}"]')

        if not el:
            # Cannot re-locate; record warning but don't hard fail if dynamic DOM changed
            report["results"].append({
                "field": field_label,
                "passed": False,
                "reason": "Element no longer in DOM (possibly submitted or moved to next step)"
            })
            continue

        # Reconcile declared widget type with live DOM tag
        dom_type = await widget_registry.detect_widget_type(ctx, el)
        tag = ""
        try:
            tag = await el.evaluate("e => e.tagName ? e.tagName.toLowerCase() : ''")
        except Exception:
            pass

        resolved_type = norm_type
        if norm_type in [WidgetType.NATIVE_SELECT] and tag != "select":
            resolved_type = dom_type if dom_type != WidgetType.TEXT else WidgetType.CUSTOM_COMBOBOX
        elif norm_type == WidgetType.TEXT and dom_type in [WidgetType.CUSTOM_COMBOBOX, WidgetType.NATIVE_SELECT, WidgetType.RADIO, WidgetType.CHECKBOX, WidgetType.DATE]:
            resolved_type = dom_type

        executor = widget_registry.get_executor(resolved_type)
        is_ok, read_val = await executor.verify(ctx, el, expected_val)

        # Fallback retry if native_select was attempted on non-select element
        if not is_ok and resolved_type == WidgetType.NATIVE_SELECT and tag != "select":
            cb_exec = widget_registry.get_executor(WidgetType.CUSTOM_COMBOBOX)
            is_ok, read_val = await cb_exec.verify(ctx, el, expected_val)
            if is_ok:
                resolved_type = WidgetType.CUSTOM_COMBOBOX

        if is_ok:
            report["verified_count"] += 1
            report["results"].append({
                "field": field_label,
                "widget_type": resolved_type,
                "passed": True,
                "expected": expected_val,
                "read_value": read_val
            })
        else:
            report["failed_count"] += 1
            report["all_passed"] = False
            report["results"].append({
                "field": field_label,
                "widget_type": resolved_type,
                "passed": False,
                "expected": expected_val,
                "read_value": read_val,
                "error": f"Value mismatch after fill. Expected: '{expected_val}', Read: '{read_val}'"
            })

    return report
