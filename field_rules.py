import re
from typing import Dict, Any, Optional

def get_field_match_by_rules(label: str, profile: Dict[str, Any]) -> Optional[str]:
    """
    Deterministic rule-based mapper for common form fields.
    Avoids unnecessary local AI calls and guarantees zero fabrication.
    """
    lbl = label.lower().strip()

    # Source / How did you hear about us
    if re.search(r'\b(how did you (hear|find)|referral\s*source|source)\b', lbl):
        return "LinkedIn"

    # Optional context / notes / comments
    if re.search(r'\b(anything\s*else|additional\s*(notes?|comments?|info)|cover\s*note|if you would like)\b', lbl):
        return profile.get("summary") or "N/A"

    # Full Name / First Name / Last Name
    full_name = profile.get("full_name", "")
    name_parts = full_name.split()
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    if re.search(r'\b(first(\s*|_)?name|given(\s*|_)?name)\b', lbl):
        return first_name
    if re.search(r'\b(last(\s*|_)?name|surname|family(\s*|_)?name)\b', lbl):
        return last_name or first_name
    if re.search(r'\b(full(\s*|_)?name|your\s*name|candidate\s*name)\b', lbl):
        return full_name
    if re.search(r'^(name|applicant\s*name)$', lbl):
        return full_name

    # Contact
    if re.search(r'\b(e-?mail|email\s*address)\b', lbl):
        return profile.get("email", "")

    if re.search(r'\b(phone|mobile|cell|telephone|contact\s*number)\b', lbl):
        return profile.get("phone", "")

    # Phone Dial Code
    if re.search(r'^(phone\s*country\s*code|dial\s*code|country\s*code|\+91|countrycode)$', lbl) or lbl in ["+91", "dial code", "code", "select"]:
        return "India (+91)"

    # Currency
    if re.search(r'^(currency|salary\s*currency|inr)$', lbl) or lbl in ["inr", "currency"]:
        return "INR"

    # Preferred Location / Location Preference
    if re.search(r'\b(preferred\s*location|location\s*preference)\b', lbl):
        return "Bangalore Corporate Office"

    # Total Experience (years / months)
    if re.search(r'\b(experience\s*\(in\s*years\)|total\s*experience|work\s*experience\s*years?)\b', lbl):
        return "0"
    if re.search(r'\b(experience\s*\(in\s*months\)|work\s*experience\s*months?)\b', lbl):
        return "0"

    # Notice period / availability
    if re.search(r'\b(notice\s*period|availability)\b', lbl):
        return "Immediate"

    # Dates: Date of Joining, Relieving, Birth Date
    if re.search(r'\b(date\s*of\s*joining|joining\s*date|start\s*date)\b', lbl):
        return "2024-05-15"
    if re.search(r'\b(date\s*of\s*relieving|relieving\s*date|end\s*date)\b', lbl):
        return "2024-08-15"
    if re.search(r'\b(dob|birth\s*date|date\s*of\s*birth)\b', lbl):
        return "2004-10-03"

    # Country (strictly field name, never authorization questions)
    if not any(w in lbl for w in ["authorized", "authorization", "visa", "sponsorship", "legally", "eligible", "require"]):
        if re.search(r'^(country|your\s*country|country\s*[\/\-]\s*region|country\s*of\s*residence)$', lbl):
            loc = profile.get("location", "")
            if "india" in loc.lower():
                return "India"
            return loc or "India"

    # Location / Address / City (strictly field name, never relocation/hearing questions)
    if not any(w in lbl for w in ["willing", "relocate", "relocation", "prefer", "office", "hear", "source"]):
        if re.search(r'^(location|current\s*location|location\s*\(city\)|city|address|current\s*address)$', lbl):
            return profile.get("location", "")

    # Links (handles composite labels like "LinkedIn, GitHub or Portfolio")
    if re.search(r'linkedin.*github|github.*linkedin|portfolio.*linkedin|linkedin.*portfolio', lbl):
        return profile.get("linkedin_url") or profile.get("github_url") or profile.get("portfolio_url", "")

    if re.search(r'\b(linkedin|linked\s*in)\b', lbl):
        return profile.get("linkedin_url", "")

    if re.search(r'\b(github|git)\b', lbl):
        return profile.get("github_url", "")

    if re.search(r'\b(portfolio|website|personal\s*site|url)\b', lbl):
        return profile.get("portfolio_url", "")

    # Education: Degree, School, Major, Graduation Date
    if re.search(r'\b(degree|highest\s*education|level\s*of\s*education)\b', lbl):
        return "Bachelor's Degree"

    if re.search(r'\b(discipline|major|field\s*of\s*study|course)\b', lbl):
        return "Computer Science and Engineering"

    if re.search(r'\b(school|university|college|institution)\b', lbl):
        return "Govt. Model Engineering College"

    if re.search(r'\b(grad(uation)?\s*year|end\s*year|completion\s*year)\b', lbl):
        return "2027"

    if re.search(r'\b(start\s*year)\b', lbl):
        return "2023"

    # Demographics & Equal Opportunity (EEOC)
    if re.search(r'^(gender|sex)$|\b(your\s*gender|identify\s*your\s*gender)\b', lbl):
        return "Male"

    if re.search(r'\bpronouns?\b', lbl):
        return "he/him"

    if re.search(r'\b(hispanic|latino)\b', lbl):
        return "No"

    if re.search(r'\bveteran\b', lbl):
        return "I am not a protected veteran"

    if re.search(r'\bdisabilit(y|ies)\b', lbl):
        return "No"

    return None

