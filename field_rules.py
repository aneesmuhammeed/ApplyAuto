import re
from typing import Dict, Any, Optional

def get_field_match_by_rules(label: str, profile: Dict[str, Any]) -> Optional[str]:
    """
    Deterministic rule-based mapper for common form fields.
    Avoids unnecessary local AI calls and guarantees zero fabrication.
    All rules return None when the profile lacks the value.
    Specific compound labels run before generic labels to prevent misrouting.
    """
    if not label or not label.strip() or not profile:
        return None

    # Strip asterisks, colons, and extra trailing whitespace from label
    lbl = re.sub(r'[\*:]', '', label).lower().strip()

    # =========================================================================
    # 1. SPECIFIC PHONE COUNTRY / DIAL CODE (Runs BEFORE Generic Phone)
    # =========================================================================
    if re.search(r'\b(phone\s*country\s*code|dial\s*code|country\s*calling\s*code|country\s*dial\s*code|countrycode)\b', lbl) or lbl in ["+91", "dial code", "code"]:
        return profile.get("phone_code") or profile.get("dial_code") or profile.get("country_code") or None

    # =========================================================================
    # 2. GENERIC PHONE / MOBILE NUMBER
    # =========================================================================
    if re.search(r'\b(phone|mobile|cell|telephone|contact\s*number)\b', lbl):
        return profile.get("phone") or None

    # =========================================================================
    # 3. SPECIFIC PREFERRED LOCATION (Runs BEFORE Generic Location)
    # =========================================================================
    if re.search(r'\b(preferred\s*location|location\s*preference|workplace\s*preference)\b', lbl):
        return profile.get("preferred_location") or None

    # =========================================================================
    # 4. GENERIC CURRENT LOCATION / CITY / ADDRESS
    # =========================================================================
    if not any(w in lbl for w in ["willing", "relocate", "relocation", "prefer", "office", "hear", "source"]):
        if re.search(r'^(city|current\s*city)$', lbl):
            return profile.get("city") or profile.get("location") or profile.get("current_location") or None
        if re.search(r'^(location|current\s*location|location\s*\(city\)|address|current\s*address)$', lbl):
            return profile.get("location") or profile.get("current_location") or profile.get("city") or None

    # =========================================================================
    # 5. SPECIFIC TOTAL EXPERIENCE (Years / Months)
    # =========================================================================
    if re.search(r'(experience\s*\(?\s*(in\s*)?years?\)?|total\s*experience\s*(in\s*)?years?|work\s*experience\s*years?)', lbl):
        val = profile.get("total_experience_years")
        if val is None:
            val = profile.get("experience_years")
        return str(val) if val is not None and str(val).strip() != "" else None

    if re.search(r'(experience\s*\(?\s*(in\s*)?months?\)?|total\s*experience\s*(in\s*)?months?|work\s*experience\s*months?)', lbl):
        val = profile.get("total_experience_months")
        if val is None:
            val = profile.get("experience_months")
        return str(val) if val is not None and str(val).strip() != "" else None

    # =========================================================================
    # 6. SPECIFIC NOTICE PERIOD (Days / Availability)
    # =========================================================================
    if re.search(r'(notice\s*period\s*\(?\s*(in\s*)?days\)?|available\s*to\s*join\s*\(?\s*(in\s*)?days\)?|notice\s*days)', lbl):
        val = profile.get("notice_period_days")
        return str(val) if val is not None and str(val).strip() != "" else None

    if re.search(r'\b(notice\s*period|availability|available\s*to\s*join)\b', lbl):
        return profile.get("notice_period") or profile.get("availability") or None

    # =========================================================================
    # 7. CANDIDATE NAMES (First, Last, Full Name)
    # =========================================================================
    full_name = (profile.get("full_name") or "").strip()
    name_parts = full_name.split() if full_name else []
    first_name = profile.get("first_name") or (name_parts[0] if name_parts else "")
    last_name = profile.get("last_name") or (" ".join(name_parts[1:]) if len(name_parts) > 1 else "")

    if re.search(r'\b(first(\s*|_)?name|given(\s*|_)?name)\b', lbl):
        return first_name or None
    if re.search(r'\b(last(\s*|_)?name|surname|family(\s*|_)?name)\b', lbl):
        return last_name or None
    if re.search(r'\b(full(\s*|_)?name|your\s*name|candidate\s*name)\b', lbl):
        return full_name or None
    if re.search(r'^(name|applicant\s*name)$', lbl):
        return full_name or None

    # =========================================================================
    # 8. EMAIL ADDRESS
    # =========================================================================
    if re.search(r'\b(e-?mail|email\s*address)\b', lbl):
        return profile.get("email") or None

    # =========================================================================
    # 9. CURRENCY & COMPENSATION
    # =========================================================================
    if re.search(r'^(currency|salary\s*currency|inr|preferred\s*currency)$', lbl):
        return profile.get("currency") or None

    # =========================================================================
    # 10. COUNTRY (Strictly field name, never authorization questions)
    # =========================================================================
    if not any(w in lbl for w in ["authorized", "authorization", "visa", "sponsorship", "legally", "eligible", "require"]):
        if re.search(r'^(country|your\s*country|country\s*[\/\-]\s*region|country\s*of\s*residence)$', lbl):
            if profile.get("country"):
                return profile["country"]
            loc = profile.get("location", "")
            if loc:
                parts = [p.strip() for p in loc.split(",") if p.strip()]
                if parts:
                    return parts[-1]
            return loc or None

    # =========================================================================
    # 11. LINKS (LinkedIn, GitHub, Portfolio)
    # =========================================================================
    if re.search(r'linkedin.*github|github.*linkedin|portfolio.*linkedin|linkedin.*portfolio', lbl):
        return profile.get("linkedin_url") or profile.get("github_url") or profile.get("portfolio_url") or None

    if re.search(r'\b(linkedin|linked\s*in)\b', lbl):
        return profile.get("linkedin_url") or None

    if re.search(r'\b(github|git)\b', lbl):
        return profile.get("github_url") or None

    if re.search(r'\b(portfolio|website|personal\s*site|url)\b', lbl):
        return profile.get("portfolio_url") or None

    # =========================================================================
    # 12. EDUCATION: Degree, School, Major, GPA, Graduation/Start Year, Board
    # =========================================================================
    edus = profile.get("education", [])
    first_edu = edus[0] if isinstance(edus, list) and len(edus) > 0 and isinstance(edus[0], dict) else {}

    # Specific Graduation / End / Start Year runs before Degree / Education
    if re.search(r'\b(grad(uation)?\s*year|end\s*year|completion\s*year)\b', lbl):
        return str(first_edu["end_year"]) if first_edu.get("end_year") else None

    if re.search(r'\b(start\s*year)\b', lbl):
        return str(first_edu["start_year"]) if first_edu.get("start_year") else None

    if re.search(r'\b(gpa|cgpa|grade\s*point|percentage|academic\s*score)\b', lbl):
        return str(first_edu["gpa"]) if first_edu.get("gpa") else None

    # Board or University Board MUST run before generic School / University
    if re.search(r'\b(board|university\s*board)\b', lbl):
        return first_edu.get("board_or_university") or None

    if re.search(r'\b(degree|highest\s*education|level\s*of\s*education)\b', lbl):
        return first_edu.get("degree") or None

    if re.search(r'\b(discipline|major|field\s*of\s*study|course)\b', lbl):
        return first_edu.get("field_of_study") or None

    if re.search(r'\b(school|university|college|institution)\b', lbl):
        return first_edu.get("institution") or None

    # =========================================================================
    # 13. DATES (Joining, Relieving, Birth Date)
    # =========================================================================
    if re.search(r'\b(date\s*of\s*joining|joining\s*date|start\s*date)\b', lbl):
        return profile.get("start_date") or None
    if re.search(r'\b(date\s*of\s*relieving|relieving\s*date|end\s*date)\b', lbl):
        return profile.get("relieving_date") or None
    if re.search(r'\b(dob|birth\s*date|date\s*of\s*birth)\b', lbl):
        return profile.get("birth_date") or None

    # =========================================================================
    # 14. REFERRAL SOURCE
    # =========================================================================
    if re.search(r'\b(how did you (hear|find)|referral\s*source|source)\b', lbl):
        return profile.get("referral_source") or profile.get("source") or None

    # =========================================================================
    # 15. OPTIONAL NOTES / SUMMARY
    # =========================================================================
    if re.search(r'\b(anything\s*else|additional\s*(notes?|comments?|info)|cover\s*note|if you would like)\b', lbl):
        return profile.get("summary") or None

    return None
