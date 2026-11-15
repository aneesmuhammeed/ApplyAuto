import httpx
import json
import re
from typing import Optional, Dict, Any, List
from config import settings

# L1 in-memory cache for ultra-low latency lookups
_LLM_MEMORY_CACHE: Dict[str, Any] = {}

def sanitize_form_label(label: str, max_length: int = 150) -> str:
    """
    Sanitize untrusted form labels before sending to local LLM:
    1. Truncate to maximum length (default 150 chars).
    2. Strip prompt injection vectors, markdown/XML tags, and instruction-like text.
    3. Remove non-printable control characters.
    """
    if not label or not isinstance(label, str):
        return ""

    # 1. Truncate raw input first to avoid regex DoS on massive payloads
    s = label[:max_length * 2].strip()

    # 2. Strip control characters (ASCII < 32 except space) and direction overrides
    s = re.sub(r'[\x00-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e]', ' ', s)

    # 3. Strip instruction-like phrases and prompt injection patterns (case-insensitive)
    injection_patterns = [
        r'(?i)\bignore\s+(all\s+|any\s+|previous\s+|above\s+|prior\s+)*(instructions|prompts|rules|commands|constraints)\b',
        r'(?i)\bdisregard\s+(all\s+|any\s+|previous\s+|above\s+|prior\s+)*(instructions|prompts|rules|commands|constraints)\b',
        r'(?i)\bforget\s+(all\s+|any\s+|previous\s+|above\s+|prior\s+)*(instructions|prompts|rules|commands)\b',
        r'(?i)\b(system\s*prompt|system\s*instruction|developer\s*mode|override\s*rules?)\b',
        r'(?i)\byou\s+are\s+(now\s+)?(a|an|in|the)\b',
        r'(?i)\b(system|assistant|user)\s*:',
        r'(?i)<\s*/?\s*(system|assistant|user|prompt|instruction|script|think)\s*>',
        r'\[\s*/?\s*(INST|SYS)\s*\]',
        r'<\|im_start\|>|<\|im_end\|>',
        r'(?i)\b(do\s*not\s*follow|bypass|hack|jailbreak)\b',
        r'(?i)\binstead\s*,?\s*(output|say|print|return|reply)\b'
    ]
    for pat in injection_patterns:
        s = re.sub(pat, ' ', s)

    # 4. Collapse consecutive whitespace
    s = re.sub(r'\s+', ' ', s).strip()

    # 5. Final length truncation
    if len(s) > max_length:
        s = s[:max_length].rstrip()

    return s

def normalize_label_for_cache(label: str) -> str:
    """Normalize label for consistent cache key generation."""
    if not label or not isinstance(label, str):
        return ""
    cleaned = re.sub(r'[^a-z0-9\s]', ' ', label.lower())
    return " ".join(cleaned.split())

def make_cache_key(
    label: str,
    cache_type: str = "choice",
    options: Optional[List[str]] = None,
    tag_type: str = ""
) -> str:
    """Generate consistent cache key for L1/L2 cache."""
    norm_lbl = normalize_label_for_cache(sanitize_form_label(label))
    if cache_type == "choice":
        norm_opts = ",".join(sorted([normalize_label_for_cache(o) for o in (options or [])]))
        return f"choice:{norm_lbl}:{norm_opts}"
    elif cache_type == "interpret":
        return f"interpret:{norm_lbl}:{tag_type}"
    elif cache_type == "freetext":
        return f"freetext:{norm_lbl}"
    return f"{cache_type}:{norm_lbl}"

def get_cached_llm_result(cache_key: str) -> Optional[Any]:
    """Retrieve result from L1 memory cache or L2 database cache."""
    if not cache_key:
        return None
    # 1. Check L1 in-memory cache
    if cache_key in _LLM_MEMORY_CACHE:
        return _LLM_MEMORY_CACHE[cache_key]
    # 2. Check L2 persistent SQLite cache
    try:
        from database import get_llm_cache
        res = get_llm_cache(cache_key)
        if res is not None:
            _LLM_MEMORY_CACHE[cache_key] = res
            return res
    except Exception as e:
        print(f"[LLM Cache] Error reading DB cache: {e}")
    return None

def set_cached_llm_result(cache_key: str, cache_type: str, result: Any, prompt_hash: str = ""):
    """Save result into both L1 memory cache and L2 persistent SQLite cache."""
    if not cache_key or result is None:
        return
    _LLM_MEMORY_CACHE[cache_key] = result
    try:
        from database import set_llm_cache
        set_llm_cache(cache_key=cache_key, cache_type=cache_type, response=result, prompt_hash=prompt_hash)
    except Exception as e:
        print(f"[LLM Cache] Error saving to DB cache: {e}")

def clear_llm_cache(cache_type: Optional[str] = None):
    """Clear L1 and L2 LLM caches."""
    global _LLM_MEMORY_CACHE
    if cache_type:
        _LLM_MEMORY_CACHE = {k: v for k, v in _LLM_MEMORY_CACHE.items() if not k.startswith(f"{cache_type}:")}
    else:
        _LLM_MEMORY_CACHE.clear()
    try:
        from database import clear_llm_cache as db_clear
        db_clear(cache_type)
    except Exception as e:
        print(f"[LLM Cache] Error clearing DB cache: {e}")

async def call_qwen(
    prompt: str,
    system_prompt: str = "You are an intelligent, factual job application assistant. You strictly use candidate facts without inventing qualifications. Keep answers concise.",
    max_tokens: int = 150,
    timeout: float = 20.0
) -> Optional[str]:
    """
    Call local LLM server running Qwen with configurable endpoint and model name.
    Uses /no_think for fast deterministic answers and formatted system instruction.
    """
    clean_prompt = prompt.strip()
    if not clean_prompt.startswith("/no_think"):
        clean_prompt = f"/no_think\n{clean_prompt}"

    endpoint = settings.LLM_ENDPOINT
    model_name = settings.LLM_MODEL

    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": clean_prompt}
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "stream": False
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(endpoint, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                # Clean think tag artifacts
                content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
                return content
            else:
                print(f"[Local AI] Non-200 response from {endpoint}: {resp.status_code} - {resp.text}")
                return None
    except Exception as e:
        print(f"[Local AI] Request failed to {endpoint} ({model_name}): {e}")
        return None

async def ai_interpret_field(label: str, tag_type: str = "text") -> Dict[str, Any]:
    """
    Use Qwen to understand what information an unfamiliar form field requires.
    Treats form label as untrusted input, truncating and sanitizing it.
    Caches results per normalized label to eliminate redundant LLM calls.
    """
    clean_label = sanitize_form_label(label, max_length=150)
    cache_key = make_cache_key(clean_label, cache_type="interpret", tag_type=tag_type)

    cached = get_cached_llm_result(cache_key)
    if cached is not None and isinstance(cached, dict):
        return cached

    prompt = f"""Analyze this job application form field and return a JSON object with:
"intent": one of ["full_name", "first_name", "last_name", "email", "phone", "location", "linkedin", "github", "portfolio", "resume", "salary", "notice_period", "work_auth", "custom_question"],
"is_sensitive": boolean (true for work authorization, visa sponsorship, legal background, or salary),
"needs_user_input": boolean

Field Label: "{clean_label}"
Field Tag Type: "{tag_type}"

Return ONLY valid JSON like: {{"intent": "...", "is_sensitive": false, "needs_user_input": false}}"""

    system_prompt = "You are a form field understanding model. Output strictly valid JSON."
    resp = await call_qwen(prompt, system_prompt=system_prompt, max_tokens=80, timeout=12.0)
    if resp:
        try:
            m = re.search(r'\{[\s\S]*?\}', resp)
            if m:
                parsed = json.loads(m.group(0))
                if isinstance(parsed, dict) and "intent" in parsed:
                    set_cached_llm_result(cache_key, "interpret", parsed)
                    return parsed
        except Exception:
            pass

    fallback = {"intent": "custom_question", "is_sensitive": False, "needs_user_input": True}
    set_cached_llm_result(cache_key, "interpret", fallback)
    return fallback

async def ai_match_saved_answers(
    question_text: str,
    saved_answers: List[Dict[str, Any]],
    field_type: str = "text",
    options: Optional[List[str]] = None,
    company_name: str = ""
) -> Optional[str]:
    """
    Lookup matching saved answer using exact normalized match, followed by embeddings similarity (threshold >= 0.85).
    Rejects answers that do not match one of the available options for dropdowns/radios.
    Guarantees zero hallucination.
    """
    if not saved_answers or not question_text:
        return None

    try:
        from field_lookup import lookup_field_value, LookupStage
        val, stage, meta = await lookup_field_value(
            label=question_text,
            field_type=field_type,
            saved_answers=saved_answers,
            profile={}, # memory stages only
            company_name=company_name,
            options=options,
            similarity_threshold=0.85
        )
        if val and stage in [LookupStage.EXACT_MEMORY, LookupStage.SIMILARITY_EMBEDDINGS]:
            return val
    except Exception as e:
        print(f"[AIMatchSavedAnswers] Error: {e}")

    return None

async def ai_generate_tailored_answer(
    question_text: str,
    candidate_profile: Dict[str, Any],
    job_context: str = "",
    options: Optional[List[str]] = None
) -> Optional[str]:
    """
    Evaluate form questions using local LLM:
    1. For option choices (dropdown/radio): prompts for JSON containing option_index and confidence.
       Treats invalid JSON, low confidence (< threshold), out-of-range index, or timeout as 'ask the user' (returns None).
       Never returns options[0] fallback!
    2. For free-text: synthesizes 1-2 sentence suggestions strictly from candidate facts.
       NOTE: Caller must NEVER auto-fill free text into the page; show as suggestion in question batch.
    3. Untrusted input defense: sanitizes and truncates question_text and job_context.
    4. Caches results per normalized label and options.
    """
    clean_q = sanitize_form_label(question_text, max_length=150)
    clean_job = sanitize_form_label(job_context, max_length=80)
    norm_q = normalize_label_for_cache(clean_q)

    # Check cache
    if options:
        cache_key = make_cache_key(clean_q, cache_type="choice", options=options)
        cached = get_cached_llm_result(cache_key)
        if cached is not None:
            return None if cached == "__ASK_USER__" else str(cached)
    else:
        cache_key = make_cache_key(clean_q, cache_type="freetext")
        cached = get_cached_llm_result(cache_key)
        if cached is not None:
            return str(cached)

    # Selectively extract relevant slices of profile rather than dumping everything
    full_name = candidate_profile.get("full_name", "")
    raw_skills = candidate_profile.get("skills", [])
    skills = ", ".join([str(s) for s in raw_skills]) if isinstance(raw_skills, list) else str(raw_skills)
    location = candidate_profile.get("location", "")
    summary = candidate_profile.get("summary", "")

    raw_projects = candidate_profile.get("projects", [])
    project_strs = []
    for p in raw_projects:
        if isinstance(p, dict):
            p_title = p.get("title") or p.get("name") or ""
            p_desc = p.get("description") or ""
            project_strs.append(f"{p_title}: {p_desc}".strip(": "))
        elif isinstance(p, str):
            project_strs.append(p)
    projects = "\n".join(project_strs)

    raw_exp = candidate_profile.get("work_experience", [])
    exp_strs = []
    for e in raw_exp:
        if isinstance(e, dict):
            role = e.get("role") or e.get("title") or ""
            comp = e.get("company") or ""
            dur = e.get("duration") or ""
            exp_strs.append(f"{role} at {comp} ({dur})".strip())
        elif isinstance(e, str):
            exp_strs.append(e)
    experience = "\n".join(exp_strs)

    pronouns = candidate_profile.get("pronouns") or "Not specified"
    gender = candidate_profile.get("gender") or "Not specified"

    candidate_facts = f"""Candidate Name: {full_name}
Pronouns: {pronouns}
Gender: {gender}
Location: {location}
Summary: {summary}
Skills: {skills}
Projects: {projects}
Experience: {experience}"""

    # Check explicit salary queries first
    q_low = clean_q.lower()
    if "current salary" in q_low or "present salary" in q_low or "current ctc" in q_low:
        sal = candidate_profile.get("current_salary")
        return str(sal) if sal is not None and str(sal).strip() else None
    if "expected salary" in q_low or "expected ctc" in q_low:
        exp_sal = candidate_profile.get("expected_salary")
        return str(exp_sal) if exp_sal is not None and str(exp_sal).strip() else None

    # =========================================================================
    # OPTION CHOICE PATH (JSON with option_index & confidence)
    # =========================================================================
    if options:
        trimmed_options = options
        if len(options) > 25:
            favored = []
            cand_keywords = ["india", "+91", "91", "inr", "rupee", "kerala", "kochi", "bangalore"]
            for o in options:
                if any(kw in o.lower() for kw in cand_keywords):
                    favored.append(o)
            for o in options:
                if o not in favored:
                    favored.append(o)
                if len(favored) >= 20:
                    break
            trimmed_options = favored

        formatted_opts = "\n".join([f'[{idx}] "{sanitize_form_label(opt, 80)}"' for idx, opt in enumerate(trimmed_options)])
        prompt = f"""{candidate_facts}

Job Position: {clean_job}
Question: "{clean_q}"
Available Options:
{formatted_opts}

Task: Select the single best matching option from Available Options for this candidate based strictly on candidate facts.
Respond ONLY with a valid JSON object containing:
- "option_index": integer (0 to {len(trimmed_options) - 1} corresponding to the best option, or -1 if no option matches or candidate facts lack info)
- "confidence": float between 0.0 and 1.0 (confidence that this option accurately reflects candidate facts)
- "reason": concise explanation

Example response:
{{"option_index": 0, "confidence": 0.95, "reason": "Candidate facts state willingness to relocate."}}
"""
        system_prompt = "You are a factual form option evaluator. You strictly output valid JSON containing option_index and confidence."
        print(f"[Local AI Prompt] Asking Qwen option choice for: '{clean_q[:70]}'")

        resp = await call_qwen(prompt, system_prompt=system_prompt, max_tokens=100, timeout=15.0)
        if not resp:
            print(f"[Local AI Decision] Timeout or failure calling LLM for '{clean_q}'. Asking user.")
            set_cached_llm_result(cache_key, "choice", "__ASK_USER__")
            return None

        # Parse JSON
        m = re.search(r'\{[\s\S]*?\}', resp)
        if not m:
            print(f"[Local AI Decision] Invalid JSON in LLM response: '{resp[:100]}'. Asking user.")
            set_cached_llm_result(cache_key, "choice", "__ASK_USER__")
            return None

        try:
            data = json.loads(m.group(0))
        except Exception as e:
            print(f"[Local AI Decision] Failed to parse JSON ({e}): '{resp[:100]}'. Asking user.")
            set_cached_llm_result(cache_key, "choice", "__ASK_USER__")
            return None

        # Read option_index
        opt_idx = data.get("option_index")
        if opt_idx is None:
            opt_idx = data.get("index")

        try:
            opt_idx = int(opt_idx)
        except (ValueError, TypeError):
            print(f"[Local AI Decision] Non-integer option_index '{opt_idx}'. Asking user.")
            set_cached_llm_result(cache_key, "choice", "__ASK_USER__")
            return None

        if opt_idx < 0 or opt_idx >= len(trimmed_options):
            print(f"[Local AI Decision] option_index {opt_idx} out of range [0, {len(trimmed_options)-1}]. Asking user.")
            set_cached_llm_result(cache_key, "choice", "__ASK_USER__")
            return None

        # Read confidence
        try:
            conf = float(data.get("confidence", 0.0))
        except (ValueError, TypeError):
            conf = 0.0

        threshold = settings.LLM_CONFIDENCE_THRESHOLD
        if conf < threshold:
            print(f"[Local AI Decision] Low confidence ({conf:.2f} < {threshold:.2f}) for option [{opt_idx}] '{trimmed_options[opt_idx]}'. Asking user.")
            set_cached_llm_result(cache_key, "choice", "__ASK_USER__")
            return None

        chosen_option = trimmed_options[opt_idx]
        print(f"[Local AI Decision] Selected option [{opt_idx}] '{chosen_option}' with confidence {conf:.2f}")
        set_cached_llm_result(cache_key, "choice", chosen_option)
        return chosen_option

    # =========================================================================
    # FREE-TEXT SUGGESTION PATH (Strictly for question batch suggestions)
    # =========================================================================
    prompt = f"""{candidate_facts}

Job Position: {clean_job}
Question: "{clean_q}"

Task: Write a concise, factual answer (1-2 sentences) strictly using the candidate facts above.
Never invent qualifications or experience. If the candidate's profile doesn't mention it, reply "UNKNOWN"."""

    print(f"[Local AI Prompt] Asking Qwen free-text suggestion for: '{clean_q[:70]}'")
    ans = await call_qwen(prompt, max_tokens=150, timeout=15.0)

    if ans and "UNKNOWN" not in ans.upper() and len(ans.strip()) > 0:
        clean_ans = ans.strip().strip('"').strip("'")
        if any(w in clean_ans.lower() for w in ["the candidate", "the summary does not", "not provided", "does not mention", "unknown"]):
            set_cached_llm_result(cache_key, "freetext", "")
            return ""
        set_cached_llm_result(cache_key, "freetext", clean_ans)
        return clean_ans

    set_cached_llm_result(cache_key, "freetext", "")
    return ""
