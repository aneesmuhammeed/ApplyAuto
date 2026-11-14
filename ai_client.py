import httpx
import json
import re
from typing import Optional, Dict, Any, List
from config import settings

async def call_qwen(prompt: str, system_prompt: str = "You are an intelligent, factual job application assistant. You strictly use candidate facts without inventing qualifications. Keep answers concise.", max_tokens: int = 150) -> Optional[str]:
    """
    Call local llama.cpp server running Qwen3-0.6B Q3_K_M.
    Uses /no_think for fast deterministic answers and formatted system instruction.
    """
    clean_prompt = prompt.strip()
    # Add /no_think tag if not present for concise and low-latency inference
    if not clean_prompt.startswith("/no_think"):
        clean_prompt = f"/no_think\n{clean_prompt}"

    payload = {
        "model": settings.LLAMA_MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": clean_prompt}
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "stream": False
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(settings.LLAMA_CPP_URL, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                # Clean any think tag artifacts if model outputs them
                content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
                return content
            else:
                print(f"[Local AI] Non-200 response: {resp.status_code} - {resp.text}")
                return None
    except Exception as e:
        print(f"[Local AI] Request failed to {settings.LLAMA_CPP_URL}: {e}")
        return None

async def ai_interpret_field(label: str, tag_type: str = "text") -> Dict[str, Any]:
    """
    Use Qwen to understand what information an unfamiliar form field requires.
    Classifies intent: full_name, first_name, last_name, email, phone, location,
    linkedin, github, portfolio, resume, salary, notice_period, work_auth, custom_question.
    """
    prompt = f"""Analyze this job application form field and return a JSON object with:
"intent": one of ["full_name", "first_name", "last_name", "email", "phone", "location", "linkedin", "github", "portfolio", "resume", "salary", "notice_period", "work_auth", "custom_question", "optional_notes"],
"is_sensitive": boolean (true for work authorization, visa sponsorship, legal background, or salary),
"needs_user_input": boolean

Field Label: "{label}"
Field Tag Type: "{tag_type}"

Return ONLY valid JSON like: {{"intent": "...", "is_sensitive": false, "needs_user_input": false}}"""

    system_prompt = "You are a form field understanding model. Output strictly valid JSON."
    resp = await call_qwen(prompt, system_prompt=system_prompt, max_tokens=80)
    if resp:
        try:
            m = re.search(r'\{.*\}', resp, re.DOTALL)
            if m:
                return json.loads(m.group(0))
        except Exception:
            pass
    return {"intent": "custom_question", "is_sensitive": False, "needs_user_input": True}

async def ai_match_saved_answers(
    question_text: str,
    saved_answers: List[Dict[str, Any]],
    company_name: str = ""
) -> Optional[str]:
    """
    Lookup matching saved answer using exact normalized match, followed by embeddings similarity (threshold >= 0.85).
    Guarantees zero hallucination.
    """
    if not saved_answers or not question_text:
        return None

    try:
        from field_lookup import lookup_field_value, LookupStage
        val, stage, meta = await lookup_field_value(
            label=question_text,
            field_type="text",
            saved_answers=saved_answers,
            profile={}, # memory stages only
            company_name=company_name,
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
    Use local Qwen3-0.6B to synthesize an answer tailored to the question
    strictly from candidate facts without inventing qualifications.
    """
    # Selectively extract relevant slices of profile rather than dumping everything
    full_name = candidate_profile.get("full_name", "")
    skills = ", ".join(candidate_profile.get("skills", []))
    location = candidate_profile.get("location", "")
    summary = candidate_profile.get("summary", "")
    projects = "\n".join(candidate_profile.get("projects", []))
    experience = "\n".join([str(e) for e in candidate_profile.get("work_experience", [])])

    candidate_facts = f"""Candidate Name: {full_name}
Pronouns: he/him
Gender: Male
Location: {location}
Summary: {summary}
Skills: {skills}
Projects: {projects}
Experience: {experience}"""

    if options:
        # Prevent context window overflow on 100+ option dropdowns (e.g. 240 countries or 160 currencies)
        # Keep options under 25 items by prioritizing candidate profile context
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

        opt_str = ", ".join([f'"{o}"' for o in trimmed_options])
        prompt = f"""{candidate_facts}

Job Position: {job_context}
Question: "{question_text}"
Available Choices: [{opt_str}]

Task: Select the single best matching choice from Available Choices for this candidate.
Respond with ONLY one of the exact choices from Available Choices. Do not add any other words."""
    else:
        prompt = f"""{candidate_facts}

Job Position: {job_context}
Question: "{question_text}"

Task: Write a concise, factual answer (1-2 sentences) strictly using the candidate facts above.
Never invent qualifications or experience. If the candidate's profile doesn't mention it, reply "UNKNOWN"."""

    q_low = question_text.lower()
    if "current salary" in q_low or "present salary" in q_low or "current ctc" in q_low:
        return "0"
    if "expected salary" in q_low or "expected ctc" in q_low:
        return "As per company standards"

    print(f"[Local AI Prompt] Asking Qwen: '{question_text[:70]}'")
    if options:
        print(f"[Local AI Options] Given choices: {options}")
    ans = await call_qwen(prompt, max_tokens=150)
    if ans and "UNKNOWN" not in ans.upper() and len(ans.strip()) > 0:
        clean_ans = ans.strip().strip('"').strip("'").rstrip(".")
        print(f"[Local AI Raw Response] -> '{clean_ans}'")

        # If options provided, strictly ensure answer is one of the valid options
        if options:
            # 1. Exact match (case-insensitive)
            for opt in options:
                if opt.lower() == clean_ans.lower():
                    print(f"[Local AI Decision] Selected option: '{opt}'")
                    return opt

            # 2. Substring match
            for opt in options:
                if opt.lower() in clean_ans.lower() or clean_ans.lower() in opt.lower():
                    print(f"[Local AI Decision] Selected option: '{opt}'")
                    return opt

            # 3. Domain-specific fallbacks
            q_lower = question_text.lower()
            if "gender" in q_lower or "sex" in q_lower:
                for opt in options:
                    if opt.lower() == "male":
                        return opt
            if "hispanic" in q_lower or "latino" in q_lower:
                for opt in options:
                    if opt.lower() == "no":
                        return opt
            if "veteran" in q_lower:
                for opt in options:
                    if "not a protected" in opt.lower() or opt.lower() == "no":
                        return opt

            # 4. Strict safety fallback to prevent invalid options
            fallback_choice = options[0] if options else None
            print(f"[Local AI Decision] Fallback choice: '{fallback_choice}'")
            return fallback_choice

        if any(w in clean_ans.lower() for w in ["the candidate", "the summary does not", "not provided", "does not mention", "unknown"]):
            return ""
        return clean_ans

    return None
