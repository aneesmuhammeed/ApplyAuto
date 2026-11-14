import os
import re
import json
from typing import Dict, Any, List, Optional
import pypdf
import docx

def extract_text_and_links_from_pdf(file_path: str):
    """Extract plain text and hyperlink annotations from PDF."""
    text = ""
    links = []
    try:
        reader = pypdf.PdfReader(file_path)
        for page in reader.pages:
            if '/Annots' in page:
                for annot in page['/Annots']:
                    try:
                        obj = annot.get_object()
                        if '/A' in obj and '/URI' in obj['/A']:
                            uri = obj['/A']['/URI']
                            if uri and uri not in links:
                                links.append(uri)
                    except Exception:
                        pass
            extracted = page.extract_text()
            if extracted:
                text += extracted + "\n"
    except Exception as e:
        print(f"Error reading PDF {file_path}: {e}")
    return text.strip(), links

def extract_text_from_docx(file_path: str) -> str:
    """Extract plain text from DOCX using python-docx."""
    text = ""
    try:
        doc = docx.Document(file_path)
        for para in doc.paragraphs:
            if para.text:
                text += para.text + "\n"
    except Exception as e:
        print(f"Error reading DOCX {file_path}: {e}")
    return text.strip()

def parse_resume(file_path: str) -> Dict[str, Any]:
    """
    High-Fidelity Resume Parser:
    Extracts structured JSON across education, skills, projects, links,
    work experience, and certifications.
    """
    ext = os.path.splitext(file_path)[1].lower()
    links = []
    if ext == ".pdf":
        raw_text, links = extract_text_and_links_from_pdf(file_path)
    elif ext in [".docx", ".doc"]:
        raw_text = extract_text_from_docx(file_path)
    else:
        raise ValueError(f"Unsupported file type: {ext}")

    parsed: Dict[str, Any] = {
        "raw_text": raw_text,
        "full_name": "",
        "email": "",
        "phone": "",
        "location": "",
        "dob": "",
        "linkedin_url": "",
        "github_url": "",
        "portfolio_url": "",
        "summary": "",
        "skills": [],
        "skills_detail": {
            "technical": [],
            "soft": [],
            "interests": []
        },
        "education": [],
        "work_experience": [],
        "projects": [],
        "certifications": []
    }

    if not raw_text:
        return parsed

    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]

    # 1. Links Extraction from Annotations and Text
    for u in links:
        u_clean = u.strip()
        if "linkedin.com" in u_clean:
            parsed["linkedin_url"] = u_clean
        elif "github.com" in u_clean:
            parsed["github_url"] = u_clean
        elif not parsed["portfolio_url"] and any(ext in u_clean for ext in [".vercel.app", ".me", ".io", ".dev", ".com", ".netlify.app"]) and "drive.google" not in u_clean and "mailto:" not in u_clean:
            parsed["portfolio_url"] = u_clean

    if not parsed["linkedin_url"]:
        li = re.search(r'https?://(?:www\.)?linkedin\.com/in/[\w-]+', raw_text)
        if li: parsed["linkedin_url"] = li.group(0)

    if not parsed["github_url"]:
        gh = re.search(r'https?://(?:www\.)?github\.com/[\w-]+', raw_text)
        if gh: parsed["github_url"] = gh.group(0)

    if not parsed["portfolio_url"]:
        site = re.search(r'https?://(?!www\.linkedin|github\.com)[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}(?:/[\w\-]*)*', raw_text)
        if site: parsed["portfolio_url"] = site.group(0)

    # 2. Contact & Identity
    for line in lines[:5]:
        if len(line.split()) in [2, 3, 4] and not any(char in line for char in ["@", "http", "/", "\\", ":", "+", "|"]):
            parsed["full_name"] = line
            break

    em = re.search(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', raw_text)
    if em: parsed["email"] = em.group(0)

    ph = re.search(r'(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', raw_text)
    if ph: parsed["phone"] = ph.group(0)

    dob = re.search(r'DOB\s*[\-–:]\s*(\d{2}[\/\-]\d{2}[\/\-]\d{4})', raw_text, re.I)
    if dob: parsed["dob"] = dob.group(1)

    loc = re.search(r'(Kochi,\s*Kerala,\s*India|Govt\.\s*Model Engineering College,\s*Kochi|[A-Za-z\s]+College,\s*[A-Za-z\s]+)', raw_text)
    if loc:
        parsed["location"] = loc.group(0).split("\n")[0].strip()

    # 3. Section Boundary Parsing
    sections: Dict[str, List[str]] = {}
    current_sec = "HEADER"
    sections[current_sec] = []

    known_headers = [
        "SKILLS & INTERESTS", "SKILLS", "TECHNICAL SKILLS", "EDUCATION",
        "WORK EXPERIENCE", "EXPERIENCE", "EMPLOYMENT", "PROJECTS", "KEY PROJECTS",
        "COURSES AND CERTIFICATIONS", "CERTIFICATIONS", "ACHIEVEMENTS AND ACTIVITIES",
        "ACHIEVEMENTS", "PUBLICATIONS", "REFERENCES"
    ]

    for line in lines:
        upper = line.upper()
        matched_hdr = None
        for kh in known_headers:
            if upper == kh or upper.startswith(kh + " ") or upper.startswith("• " + kh):
                matched_hdr = kh
                break
        if matched_hdr:
            current_sec = matched_hdr
            sections[current_sec] = []
        else:
            sections.setdefault(current_sec, []).append(line)

    # 4. Parse Skills (Categorized + Flat)
    skills_lines = sections.get("SKILLS & INTERESTS", []) or sections.get("SKILLS", []) or sections.get("TECHNICAL SKILLS", [])
    active_category = "technical"
    for sl in skills_lines:
        if "Technical Skills:" in sl or "Technical:" in sl:
            active_category = "technical"
            part = sl.split("Skills:")[-1]
            items = [s.strip().rstrip(".") for s in re.split(r'[,|•]', part) if s.strip()]
            parsed["skills_detail"]["technical"].extend(items)
        elif "Soft Skills:" in sl or "Soft:" in sl:
            active_category = "soft"
            part = sl.split("Skills:")[-1]
            items = [s.strip().rstrip(".") for s in re.split(r'[,|•]', part) if s.strip()]
            parsed["skills_detail"]["soft"].extend(items)
        elif "Interests:" in sl or "Areas of Interest:" in sl:
            active_category = "interests"
            part = sl.split(":")[-1]
            items = [s.strip().rstrip(".") for s in re.split(r'[,|•]', part) if s.strip()]
            parsed["skills_detail"]["interests"].extend(items)
        elif not sl.startswith("•"):
            items = [s.strip().rstrip(".") for s in re.split(r'[,|•]', sl) if s.strip()]
            parsed["skills_detail"][active_category].extend(items)

    # Clean and deduplicate skills
    flat_skills = []
    for cat in ["technical", "soft", "interests"]:
        cleaned_cat = []
        for s in parsed["skills_detail"][cat]:
            clean_s = re.sub(r'^[•\-\s]+', '', s).strip().rstrip(".")
            if clean_s and len(clean_s) > 1 and clean_s not in cleaned_cat:
                cleaned_cat.append(clean_s)
                if clean_s not in flat_skills:
                    flat_skills.append(clean_s)
        parsed["skills_detail"][cat] = cleaned_cat
    parsed["skills"] = flat_skills

    # 5. Parse Education
    edu_lines = sections.get("EDUCATION", [])
    edu_blocks = []
    cur_block = []
    for l in edu_lines:
        if l.startswith("•"):
            if cur_block: edu_blocks.append(cur_block)
            cur_block = [l]
        else:
            cur_block.append(l)
    if cur_block: edu_blocks.append(cur_block)

    for b in edu_blocks:
        b_text = " ".join(b)
        gpa_match = re.search(r'(\d+\.\d+)\s*(?:GPA)?(?:\s*\||\s*\/|\s*$)', b_text)
        pct_match = re.search(r'(\d+(?:\.\d+)?%?)', b_text)
        year_match = re.search(r'\b(20\d\d)\b', b_text)

        inst = re.sub(r'^[•\s]+', '', b[0])
        inst = re.sub(r'\d+\.\d+.*|\d+%.*|\b20\d\d.*', '', inst).strip(' |-\t')

        deg = ""
        major = ""
        board = ""
        if len(b) > 1:
            rest = b[1]
            if "," in rest:
                board = rest.split(",")[0].strip()
                deg_part = rest.split(",")[-1].strip()
            else:
                deg_part = rest
            if " in " in deg_part:
                deg = deg_part.split(" in ")[0].strip()
                major = deg_part.split(" in ")[-1].strip()
            else:
                deg = deg_part

        gpa_val = gpa_match.group(1) if gpa_match else (pct_match.group(1) if pct_match else "")
        parsed["education"].append({
            "institution": inst,
            "degree": deg or ("B.Tech" if "b.tech" in b_text.lower() else ("12th" if "12th" in b_text else ("10th" if "10th" in b_text else "Degree"))),
            "field_of_study": major or ("Computer Science" if "computer science" in b_text.lower() else ""),
            "gpa": gpa_val,
            "end_year": year_match.group(1) if year_match else "",
            "board_or_university": board
        })

    # 6. Parse Work Experience
    exp_lines = sections.get("WORK EXPERIENCE", []) or sections.get("EXPERIENCE", [])
    exp_blocks = []
    cur_block = []
    for l in exp_lines:
        if l.startswith("•"):
            if cur_block: exp_blocks.append(cur_block)
            cur_block = [l]
        else:
            cur_block.append(l)
    if cur_block: exp_blocks.append(cur_block)

    for b in exp_blocks:
        header_line = re.sub(r'^[•\s]+', '', b[0])
        dur_match = re.search(r'(\d+\s*(?:Months?|Years?))', header_line, re.I)
        dur = dur_match.group(1) if dur_match else ""
        comp = re.sub(r'\d+\s*(?:Months?|Years?).*', '', header_line, flags=re.I).strip()

        role = b[1] if len(b) > 1 and not b[1].startswith("Technologies") else "Intern"
        techs = []
        desc_lines = []
        for l in b[1:]:
            if "Technologies Used:" in l:
                tech_str = l.split("Technologies Used:")[-1]
                techs = [t.strip().rstrip(".") for t in tech_str.split(",") if t.strip()]
            elif l != role:
                desc_lines.append(l)

        parsed["work_experience"].append({
            "company": comp,
            "role": role,
            "duration": dur,
            "technologies": techs,
            "description": " ".join(desc_lines).strip()
        })

    # 7. Parse Projects
    proj_lines = sections.get("PROJECTS", []) or sections.get("KEY PROJECTS", [])
    proj_blocks = []
    cur_block = []
    for l in proj_lines:
        if l.startswith("•"):
            if cur_block: proj_blocks.append(cur_block)
            cur_block = [l]
        else:
            cur_block.append(l)
    if cur_block: proj_blocks.append(cur_block)

    for b in proj_blocks:
        header_line = re.sub(r'^[•\s]+', '', b[0])
        p_name = header_line.split("Team Size:")[0].split("TeamSize:")[0].strip()
        ts_match = re.search(r'Team\s*Size:\s*(\d+)', header_line, re.I)
        ts = ts_match.group(1) if ts_match else ""

        role = ""
        dur = ""
        techs = []
        desc_lines = []
        for l in b[1:]:
            if "Team Member" in l or "Lead" in l or "Developer" in l:
                role_match = re.search(r'(\d+\s*(?:Months?|Years?))', l, re.I)
                if role_match: dur = role_match.group(1)
                role = re.sub(r'\d+\s*(?:Months?|Years?).*', '', l, flags=re.I).strip()
            elif "Technologies Used:" in l:
                tech_str = l.split("Technologies Used:")[-1]
                techs = [t.strip().rstrip(".") for t in tech_str.split(",") if t.strip()]
            else:
                desc_lines.append(l)

        parsed["projects"].append({
            "name": p_name,
            "role": role or "Developer",
            "team_size": ts,
            "duration": dur,
            "technologies": techs,
            "description": " ".join(desc_lines).strip(),
            "url": ""
        })

    # 8. Parse Certifications
    cert_lines = sections.get("COURSES AND CERTIFICATIONS", []) or sections.get("CERTIFICATIONS", [])
    for cl in cert_lines:
        clean_cl = re.sub(r'^[•\s]+', '', cl).strip()
        if clean_cl and len(clean_cl) > 10:
            parsed["certifications"].append(clean_cl)

    # 9. Summary synthesis (if not explicitly in resume)
    if not parsed["summary"]:
        degrees = [e.get("degree") for e in parsed["education"] if e.get("degree")]
        latest_deg = degrees[0] if degrees else "Student"
        top_skills = ", ".join(parsed["skills"][:5])
        parsed["summary"] = f"{parsed['full_name']} - {latest_deg} candidate with core skills in {top_skills}."

    return parsed
