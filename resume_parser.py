import os
import re
import json
from typing import Dict, Any, List
import pypdf
import docx

def extract_text_from_pdf(file_path: str) -> str:
    """Extract plain text from PDF using pypdf."""
    text = ""
    try:
        reader = pypdf.PdfReader(file_path)
        for page in reader.pages:
            extracted = page.extract_text()
            if extracted:
                text += extracted + "\n"
    except Exception as e:
        print(f"Error reading PDF {file_path}: {e}")
    return text.strip()

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
    Extract structured data from resume file (PDF or DOCX).
    Extracts name, email, phone, links, education, work experience, projects, certifications,
    and skills strictly from the document.
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        raw_text = extract_text_from_pdf(file_path)
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
        "linkedin_url": "",
        "github_url": "",
        "portfolio_url": "",
        "summary": "",
        "education": [],
        "work_experience": [],
        "skills": [],
        "projects": [],
        "certifications": []
    }

    if not raw_text:
        return parsed

    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]

    # 1. Full Name detection (typically first non-empty header line)
    for line in lines[:5]:
        if len(line.split()) in [2, 3, 4] and not any(char in line for char in ["@", "http", "/", "\\", ":", "+"]):
            parsed["full_name"] = line
            break

    # 2. Email extraction regex
    email_match = re.search(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', raw_text)
    if email_match:
        parsed["email"] = email_match.group(0)

    # 3. Phone extraction regex
    phone_match = re.search(r'(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', raw_text)
    if phone_match:
        parsed["phone"] = phone_match.group(0)

    # 4. LinkedIn regex
    linkedin_match = re.search(r'https?://(?:www\.)?linkedin\.com/in/[\w-]+', raw_text)
    if linkedin_match:
        parsed["linkedin_url"] = linkedin_match.group(0)

    # 5. GitHub regex
    github_match = re.search(r'https?://(?:www\.)?github\.com/[\w-]+', raw_text)
    if github_match:
        parsed["github_url"] = github_match.group(0)

    # 6. Portfolio / Personal site
    site_match = re.search(r'https?://(?!www\.linkedin|github\.com)[a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,}(?:/[\w\-]*)*', raw_text)
    if site_match:
        parsed["portfolio_url"] = site_match.group(0)

    # 7. Common Technical Skills Extraction
    tech_keywords = [
        "Python", "JavaScript", "TypeScript", "React", "Next.js", "Node.js", "FastAPI",
        "Django", "Flask", "SQL", "PostgreSQL", "MySQL", "SQLite", "MongoDB", "Redis",
        "Docker", "Kubernetes", "Git", "GitHub", "Playwright", "Selenium", "HTML", "CSS",
        "Tailwind CSS", "Java", "C++", "C#", "Go", "Rust", "AWS", "Azure", "GCP", "Linux",
        "REST APIs", "GraphQL", "Machine Learning", "Deep Learning", "PyTorch", "TensorFlow",
        "llama.cpp", "Transformers", "Data Structures", "Algorithms", "CI/CD"
    ]
    found_skills = []
    for skill in tech_keywords:
        pattern = r'\b' + re.escape(skill) + r'\b'
        if re.search(pattern, raw_text, re.IGNORECASE):
            found_skills.append(skill)
    parsed["skills"] = list(dict.fromkeys(found_skills))

    # 8. Education & Degrees
    edu_keywords = ["B.Tech", "B.E", "B.S", "BSc", "Bachelor", "M.Tech", "M.S", "MSc", "Master", "Ph.D", "Computer Science", "Information Technology", "Engineering"]
    found_edu = []
    for line in lines:
        if any(re.search(r'\b' + re.escape(k) + r'\b', line, re.IGNORECASE) for k in edu_keywords):
            if len(line) < 120 and line not in found_edu:
                found_edu.append(line)
    parsed["education"] = found_edu[:3]

    # 9. Projects extraction
    projects = []
    in_proj = False
    for line in lines:
        if re.search(r'\b(projects|academic projects|key projects)\b', line, re.IGNORECASE):
            in_proj = True
            continue
        if in_proj:
            if re.search(r'\b(skills|education|experience|certifications|awards)\b', line, re.IGNORECASE) and len(line) < 30:
                in_proj = False
                break
            if len(line) > 10 and len(line) < 150:
                projects.append(line)
    parsed["projects"] = projects[:4]

    return parsed
