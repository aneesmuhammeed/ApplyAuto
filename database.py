import sqlite3
import json
import os
import re
import time
import uuid
import numpy as np
from typing import Dict, Any, List, Optional
from config import settings

DB_PATH = settings.SQLITE_DB_PATH

def get_db_connection():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn

CURRENT_SCHEMA_VERSION = 2

def run_migrations(conn: sqlite3.Connection):
    """
    Versioned schema migration using SQLite PRAGMA user_version.
    Ensures existing databases upgrade safely without data loss.
    """
    cursor = conn.cursor()
    version_row = cursor.execute("PRAGMA user_version").fetchone()
    current_version = version_row[0] if version_row else 0

    tables = [t[0] for t in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

    if current_version < 1:
        # Migration 1: Ensure initial columns, triggers, and indices
        if "applications" in tables:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_apps_status_created ON applications(status, created_at)")
        if "saved_answers" in tables:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_saved_answers_qkey ON saved_answers(question_key)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_saved_answers_norm ON saved_answers(normalized_text)")
        cursor.execute("PRAGMA user_version = 1")
        conn.commit()
        current_version = 1

    if current_version < 2:
        # Migration 2: Verify all extended profile and application columns exist
        if "applications" in tables:
            app_cols = [c[1] for c in cursor.execute("PRAGMA table_info(applications)").fetchall()]
            for col, col_def in [
                ("verification_failed", "INTEGER DEFAULT 0"),
                ("verification_confirmed", "INTEGER DEFAULT 0"),
                ("requires_manual_confirmation", "INTEGER DEFAULT 0"),
                ("verification_failures", "TEXT DEFAULT '[]'"),
                ("pre_submit_screenshot", "TEXT DEFAULT ''"),
                ("post_submit_screenshot", "TEXT DEFAULT ''"),
                ("answer_scope", "TEXT DEFAULT 'global'"),
                ("unanswered_questions", "TEXT DEFAULT '[]'"),
                ("batch_answers", "TEXT DEFAULT '[]'")
            ]:
                if col not in app_cols:
                    cursor.execute(f"ALTER TABLE applications ADD COLUMN {col} {col_def}")

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_apps_status_created ON applications(status, created_at)")
        cursor.execute("PRAGMA user_version = 2")
        conn.commit()

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Profiles Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS profiles (
        id TEXT PRIMARY KEY,
        full_name TEXT DEFAULT '',
        email TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        phone_code TEXT DEFAULT '+91',
        location TEXT DEFAULT '',
        country TEXT DEFAULT 'India',
        preferred_location TEXT DEFAULT '',
        total_experience_years TEXT DEFAULT '',
        total_experience_months TEXT DEFAULT '',
        notice_period TEXT DEFAULT '',
        notice_period_days TEXT DEFAULT '',
        current_salary TEXT DEFAULT '',
        expected_salary TEXT DEFAULT '',
        currency TEXT DEFAULT 'INR',
        gender TEXT DEFAULT '',
        pronouns TEXT DEFAULT '',
        birth_date TEXT DEFAULT '',
        referral_source TEXT DEFAULT 'LinkedIn',
        linkedin_url TEXT DEFAULT '',
        github_url TEXT DEFAULT '',
        portfolio_url TEXT DEFAULT '',
        summary TEXT DEFAULT '',
        education TEXT DEFAULT '[]',
        work_experience TEXT DEFAULT '[]',
        skills TEXT DEFAULT '[]',
        projects TEXT DEFAULT '[]',
        certifications TEXT DEFAULT '[]',
        resume_file_path TEXT DEFAULT '',
        raw_resume_text TEXT DEFAULT '',
        updated_at TEXT,
        created_at TEXT
    )
    """)

    # 2. Applications Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS applications (
        id TEXT PRIMARY KEY,
        job_url TEXT NOT NULL,
        company_name TEXT DEFAULT '',
        job_title TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'queued',
        stage_message TEXT DEFAULT 'Task queued for worker',
        form_data TEXT DEFAULT '{}',
        unanswered_question TEXT DEFAULT NULL,
        unanswered_questions TEXT DEFAULT '[]',
        batch_answers TEXT DEFAULT '[]',
        ai_suggestion TEXT DEFAULT '',
        user_answer TEXT DEFAULT NULL,
        save_answer_for_future INTEGER DEFAULT 0,
        answer_category TEXT DEFAULT 'general',
        review_screenshot_url TEXT DEFAULT '',
        logs TEXT DEFAULT '[]',
        error_message TEXT DEFAULT NULL,
        created_at TEXT,
        updated_at TEXT
    )
    """)

    # 3. Saved Answers Memory
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS saved_answers (
        id TEXT PRIMARY KEY,
        question_key TEXT DEFAULT '',
        question_pattern TEXT NOT NULL,
        normalized_text TEXT DEFAULT '',
        field_type TEXT DEFAULT 'text',
        answer TEXT NOT NULL,
        options_seen TEXT DEFAULT '[]',
        source TEXT DEFAULT 'user',
        scope TEXT DEFAULT 'global',
        company_name TEXT DEFAULT '',
        last_used TEXT DEFAULT '',
        category TEXT DEFAULT 'general',
        is_sensitive INTEGER DEFAULT 0,
        usage_count INTEGER DEFAULT 0,
        created_at TEXT,
        updated_at TEXT
    )
    """)

    # 4. Worker Status Heartbeat
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS worker_status (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'offline',
        last_heartbeat TEXT,
        active_application_id TEXT,
        meta TEXT DEFAULT '{}'
    )
    """)

    # 5. Persistent Embedding Cache
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS embedding_cache (
        normalized_pattern TEXT PRIMARY KEY,
        embedding BLOB NOT NULL,
        dim INTEGER NOT NULL,
        updated_at TEXT
    )
    """)

    # 6. Persistent LLM Cache
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS llm_cache (
        cache_key TEXT PRIMARY KEY,
        cache_type TEXT NOT NULL,
        prompt_hash TEXT DEFAULT '',
        response_json TEXT NOT NULL,
        created_at TEXT
    )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_llm_cache_type ON llm_cache(cache_type)")

    # Run versioned migrations
    run_migrations(conn)

    # Default profile record
    cursor.execute("SELECT id FROM profiles WHERE id = 'default'")
    if not cursor.fetchone():
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("""
        INSERT INTO profiles (id, full_name, email, phone, location, created_at, updated_at)
        VALUES ('default', 'Muhammed Anees K', 'muhammedanees.mec@gmail.com', '+91 9876543210', 'Kochi, Kerala, India', ?, ?)
        """, (now, now))

    # Default worker status
    cursor.execute("SELECT id FROM worker_status WHERE id = 'laptop-worker'")
    if not cursor.fetchone():
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("""
        INSERT INTO worker_status (id, status, last_heartbeat)
        VALUES ('laptop-worker', 'online', ?)
        """, (now,))

    conn.commit()
    conn.close()

# Database helper functions
def extract_clean_city_from_location(loc_str: str) -> str:
    """
    Extract a clean city name from a candidate's location string.
    Filters out institutional and academic names (e.g. 'Govt. Model Engineering College, Kochi' -> 'Kochi').
    """
    if not loc_str or not loc_str.strip():
        return ""
    
    parts = [p.strip() for p in loc_str.split(",") if p.strip()]
    if not parts:
        return ""

    institutional_terms = [
        "college", "university", "institute", "school", "campus", "hostel",
        "hall", "academy", "polytechnic", "hospital", "govt", "government",
        "engineering", "technology", "dept", "department"
    ]
    
    country_state_terms = [
        "india", "usa", "united states", "kerala", "karnataka", "tamil nadu",
        "maharashtra", "telangana", "delhi", "california", "texas"
    ]

    # Look for candidate part that is not an institution and not a broad country
    for p in parts:
        p_low = p.lower()
        if any(term in p_low for term in institutional_terms):
            continue
        if p_low in country_state_terms:
            continue
        return p

    # Fallback to last part or first part if all else fails
    for p in reversed(parts):
        p_low = p.lower()
        if not any(term in p_low for term in institutional_terms):
            return p
    return parts[0]

def get_profile() -> Dict[str, Any]:
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM profiles LIMIT 1").fetchone()
    conn.close()
    if not row:
        return {}
    d = dict(row)
    for field in ["education", "work_experience", "skills", "projects", "certifications"]:
        if isinstance(d.get(field), str):
            try:
                d[field] = json.loads(d[field])
            except Exception:
                d[field] = []

    # Derive first_name and last_name from full_name if not explicitly set
    full_name = (d.get("full_name") or "").strip()
    name_parts = full_name.split() if full_name else []
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    if not d.get("first_name"):
        d["first_name"] = first_name
    if not d.get("last_name"):
        d["last_name"] = last_name or first_name

    # Derive clean city from location
    loc = d.get("location") or ""
    clean_city = extract_clean_city_from_location(loc)
    if not d.get("city"):
        d["city"] = clean_city

    return d

def update_profile(data: Dict[str, Any]):
    conn = get_db_connection()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    profile_id = data.get("id") or "default"
    
    # Fetch existing profile row to support safe partial merges
    cur = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,))
    existing_row = cur.fetchone()
    existing = dict(existing_row) if existing_row else {}

    def pick_val(key, default=""):
        if key in data and data[key] is not None:
            return data[key]
        return existing.get(key, default)

    def pick_json(key):
        if key in data and data[key] is not None:
            val = data[key]
            return json.dumps(val) if isinstance(val, (list, dict)) else str(val)
        return existing.get(key, "[]")

    education = pick_json("education")
    work_exp = pick_json("work_experience")
    skills = pick_json("skills")
    projects = pick_json("projects")
    certs = pick_json("certifications")

    resume_path = data.get("resume_file_path") or existing.get("resume_file_path", "")
    raw_resume = data.get("raw_resume_text") or existing.get("raw_resume_text", "")

    conn.execute("""
    INSERT INTO profiles (
        id, full_name, email, phone, phone_code, location, country, preferred_location,
        linkedin_url, github_url, portfolio_url, summary,
        total_experience_years, total_experience_months,
        notice_period, notice_period_days,
        current_salary, expected_salary, currency,
        gender, pronouns, birth_date, referral_source,
        education, work_experience, skills, projects, certifications,
        resume_file_path, raw_resume_text, updated_at, created_at
    ) VALUES (
        ?, ?, ?, ?, ?, ?, ?, ?,
        ?, ?, ?, ?,
        ?, ?,
        ?, ?,
        ?, ?, ?,
        ?, ?, ?, ?,
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?
    )
    ON CONFLICT(id) DO UPDATE SET
        full_name=excluded.full_name,
        email=excluded.email,
        phone=excluded.phone,
        phone_code=excluded.phone_code,
        location=excluded.location,
        country=excluded.country,
        preferred_location=excluded.preferred_location,
        linkedin_url=excluded.linkedin_url,
        github_url=excluded.github_url,
        portfolio_url=excluded.portfolio_url,
        summary=excluded.summary,
        total_experience_years=excluded.total_experience_years,
        total_experience_months=excluded.total_experience_months,
        notice_period=excluded.notice_period,
        notice_period_days=excluded.notice_period_days,
        current_salary=excluded.current_salary,
        expected_salary=excluded.expected_salary,
        currency=excluded.currency,
        gender=excluded.gender,
        pronouns=excluded.pronouns,
        birth_date=excluded.birth_date,
        referral_source=excluded.referral_source,
        education=excluded.education,
        work_experience=excluded.work_experience,
        skills=excluded.skills,
        projects=excluded.projects,
        certifications=excluded.certifications,
        resume_file_path=CASE WHEN excluded.resume_file_path != '' THEN excluded.resume_file_path ELSE profiles.resume_file_path END,
        raw_resume_text=CASE WHEN excluded.raw_resume_text != '' THEN excluded.raw_resume_text ELSE profiles.raw_resume_text END,
        updated_at=excluded.updated_at
    """, (
        profile_id,
        pick_val("full_name"),
        pick_val("email"),
        pick_val("phone"),
        pick_val("phone_code", "+91"),
        pick_val("location"),
        pick_val("country", "India"),
        pick_val("preferred_location"),
        pick_val("linkedin_url"),
        pick_val("github_url"),
        pick_val("portfolio_url"),
        pick_val("summary"),
        str(pick_val("total_experience_years")),
        str(pick_val("total_experience_months")),
        pick_val("notice_period"),
        str(pick_val("notice_period_days")),
        pick_val("current_salary"),
        pick_val("expected_salary"),
        pick_val("currency", "INR"),
        pick_val("gender"),
        pick_val("pronouns"),
        pick_val("birth_date"),
        pick_val("referral_source", "LinkedIn"),
        education,
        work_exp,
        skills,
        projects,
        certs,
        resume_path,
        raw_resume,
        now,
        existing.get("created_at", now)
    ))
    conn.commit()
    conn.close()

REQUIRED_PROFILE_CHECKS = [
    {
        "key": "full_name",
        "label": "Full Legal Name",
        "category": "Basic Contact",
        "type": "text",
        "description": "Your full legal name as used in formal applications.",
        "placeholder": "e.g. Muhammed Anees K"
    },
    {
        "key": "email",
        "label": "Email Address",
        "category": "Basic Contact",
        "type": "email",
        "description": "Primary email for application confirmations.",
        "placeholder": "e.g. your.email@example.com"
    },
    {
        "key": "phone",
        "label": "Phone Number",
        "category": "Basic Contact",
        "type": "tel",
        "description": "Primary phone/mobile contact number.",
        "placeholder": "e.g. 9876543210"
    },
    {
        "key": "phone_code",
        "label": "Phone Country Dial Code",
        "category": "Basic Contact",
        "type": "text",
        "description": "Calling code prefix for international phone inputs.",
        "placeholder": "e.g. +91"
    },
    {
        "key": "location",
        "label": "Current City & State",
        "category": "Basic Contact",
        "type": "text",
        "description": "Your current residential location.",
        "placeholder": "e.g. Kochi, Kerala, India"
    },
    {
        "key": "country",
        "label": "Country of Residence",
        "category": "Basic Contact",
        "type": "text",
        "description": "Your primary country of residence.",
        "placeholder": "e.g. India"
    },
    {
        "key": "preferred_location",
        "label": "Preferred Work Location",
        "category": "Preferences & Availability",
        "type": "text",
        "description": "Where you prefer to work (e.g. Bangalore, Kochi, Remote).",
        "placeholder": "e.g. Bangalore / Remote"
    },
    {
        "key": "total_experience_years",
        "label": "Total Experience (Years)",
        "category": "Preferences & Availability",
        "type": "number",
        "description": "Total professional experience in full years (0 for freshers/students).",
        "placeholder": "e.g. 0"
    },
    {
        "key": "notice_period",
        "label": "Notice Period / Availability",
        "category": "Preferences & Availability",
        "type": "select",
        "options": ["Immediate", "15 Days", "30 Days", "60 Days", "90 Days"],
        "description": "Time needed to join a new company.",
        "placeholder": "Select notice period"
    },
    {
        "key": "notice_period_days",
        "label": "Notice Period (in Days)",
        "category": "Preferences & Availability",
        "type": "number",
        "description": "Notice period expressed numerically in days (0 for immediate).",
        "placeholder": "e.g. 0"
    },
    {
        "key": "gender",
        "label": "Gender",
        "category": "Demographics & Diversity",
        "type": "select",
        "options": ["Male", "Female", "Non-binary", "Prefer not to say"],
        "description": "Optional demographic question required by standard EEO/diversity forms.",
        "placeholder": "Select gender"
    },
    {
        "key": "pronouns",
        "label": "Pronouns",
        "category": "Demographics & Diversity",
        "type": "select",
        "options": ["he/him", "she/her", "they/them", "Prefer not to say"],
        "description": "Preferred pronouns for communications.",
        "placeholder": "Select pronouns"
    },
    {
        "key": "current_salary",
        "label": "Current Annual Salary / CTC",
        "category": "Compensation",
        "type": "text",
        "description": "Current CTC in local currency (or 0 for freshers/students).",
        "placeholder": "e.g. 0 or 1200000"
    },
    {
        "key": "expected_salary",
        "label": "Expected Annual Salary / CTC",
        "category": "Compensation",
        "type": "text",
        "description": "Expected compensation target or range.",
        "placeholder": "e.g. 1500000 or As per industry standards"
    },
    {
        "key": "currency",
        "label": "Preferred Currency",
        "category": "Compensation",
        "type": "select",
        "options": ["INR", "USD", "EUR", "GBP"],
        "description": "Currency for compensation numbers.",
        "placeholder": "e.g. INR"
    },
    {
        "key": "linkedin_url",
        "label": "LinkedIn Profile URL",
        "category": "Online Profiles",
        "type": "url",
        "description": "Public LinkedIn profile URL.",
        "placeholder": "https://www.linkedin.com/in/..."
    },
    {
        "key": "github_url",
        "label": "GitHub Profile URL",
        "category": "Online Profiles",
        "type": "url",
        "description": "Public GitHub profile URL.",
        "placeholder": "https://github.com/..."
    },
    {
        "key": "referral_source",
        "label": "Default Application Source",
        "category": "Preferences & Availability",
        "type": "select",
        "options": ["LinkedIn", "Company Career Website", "Referral", "Job Board", "Campus Placement"],
        "description": "Default answer for 'How did you hear about this position?'.",
        "placeholder": "Select source"
    },
    {
        "key": "education",
        "label": "Education Records",
        "category": "Academic & Career History",
        "type": "special",
        "description": "At least one education record with degree, institution, and end year.",
        "placeholder": ""
    },
    {
        "key": "resume_file_path",
        "label": "Resume Document File",
        "category": "Documents",
        "type": "special",
        "description": "Uploaded PDF or DOCX resume document.",
        "placeholder": ""
    }
]

def calculate_profile_completeness(profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Computes profile completeness percentage and returns a structured list of missing items
    so the user can be asked once to fill in any missing details needed for job applications.
    """
    if profile is None:
        profile = get_profile()

    evaluated = []
    for check in REQUIRED_PROFILE_CHECKS:
        k = check["key"]
        val = profile.get(k)
        is_filled = False

        if k == "education":
            if isinstance(val, list) and len(val) > 0:
                first = val[0]
                if isinstance(first, dict) and (first.get("degree") or first.get("institution")):
                    is_filled = True
        elif k == "resume_file_path":
            if val and str(val).strip():
                is_filled = True
        elif val is not None:
            s = str(val).strip()
            # Allow "0" for numeric fields (e.g. 0 years experience, 0 days notice period)
            if s != "":
                is_filled = True

        current_val = val if is_filled else ""
        if isinstance(current_val, list):
            current_val = f"{len(current_val)} items"

        evaluated.append({
            **check,
            "is_filled": is_filled,
            "current_value": str(current_val)
        })

    filled_count = sum(1 for item in evaluated if item["is_filled"])
    total_count = len(evaluated)
    score = int(round((filled_count / total_count) * 100)) if total_count > 0 else 100
    missing_fields = [item for item in evaluated if not item["is_filled"]]

    return {
        "score": score,
        "filled_count": filled_count,
        "total_count": total_count,
        "is_complete": score == 100,
        "missing_fields": missing_fields,
        "all_fields": evaluated
    }

def get_applications() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM applications ORDER BY created_at DESC").fetchall()
    conn.close()
    res = []
    for r in rows:
        d = dict(r)
        for json_col in ["form_data", "unanswered_question", "unanswered_questions", "batch_answers", "logs", "verification_failures"]:
            if d.get(json_col):
                try:
                    d[json_col] = json.loads(d[json_col])
                except Exception:
                    pass
        res.append(d)
    return res

def get_application(app_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    for json_col in ["form_data", "unanswered_question", "unanswered_questions", "batch_answers", "logs", "verification_failures"]:
        if d.get(json_col):
            try:
                d[json_col] = json.loads(d[json_col])
            except Exception:
                pass
    return d

def create_application(job_url: str, company_name: str = "") -> str:
    conn = get_db_connection()
    app_id = str(uuid.uuid4())
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    conn.execute("""
    INSERT INTO applications (id, job_url, company_name, status, stage_message, created_at, updated_at)
    VALUES (?, ?, ?, 'queued', 'Queued for local worker', ?, ?)
    """, (app_id, job_url, company_name or "Direct Application", now, now))
    conn.commit()
    conn.close()
    return app_id

def update_application(app_id: str, updates: Dict[str, Any]):
    conn = get_db_connection()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    updates["updated_at"] = now
    
    # JSON serialize any dict/list fields, with size guard on form_data (max 512KB)
    for field in ["form_data", "unanswered_question", "unanswered_questions", "batch_answers", "logs", "verification_failures"]:
        if field in updates:
            val = updates[field]
            if isinstance(val, (dict, list)):
                s = json.dumps(val)
                # Form data snapshot size limit guard
                if field == "form_data" and len(s) > 524288:  # 512KB
                    # Truncate large histories inside form_data if oversized
                    if isinstance(val, dict) and "fields" in val:
                        trimmed = {"fields": val.get("fields", {}), "question_attempts": val.get("question_attempts", {})}
                        s = json.dumps(trimmed)
                updates[field] = s
            elif isinstance(val, str) and field == "form_data" and len(val) > 524288:
                try:
                    parsed = json.loads(val)
                    if isinstance(parsed, dict) and "fields" in parsed:
                        updates[field] = json.dumps({"fields": parsed.get("fields", {}), "question_attempts": parsed.get("question_attempts", {})})
                except Exception:
                    pass
            
    cols = ", ".join([f"{k} = ?" for k in updates.keys()])
    values = list(updates.values()) + [app_id]
    conn.execute(f"UPDATE applications SET {cols} WHERE id = ?", values)
    conn.commit()
    conn.close()

def acquire_queued_application(worker_id: str = "laptop-worker") -> Optional[Dict[str, Any]]:
    """
    Atomically acquire the oldest 'queued' application to ensure no two workers or polling cycles
    ever claim the same task concurrently. Uses an immediate UPDATE ... WHERE status='queued' transaction.
    """
    conn = get_db_connection()
    try:
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        with conn:
            # Find the oldest queued application
            row = conn.execute("SELECT id FROM applications WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1").fetchone()
            if not row:
                return None
            app_id = row["id"]
            # Atomically update to processing
            cursor = conn.execute(
                "UPDATE applications SET status = 'processing', stage_message = 'Worker processing application...', updated_at = ? WHERE id = ? AND status = 'queued'",
                (now, app_id)
            )
            if cursor.rowcount == 0:
                return None
        return get_application(app_id)
    finally:
        conn.close()

def recover_stuck_processing_applications(timeout_seconds: int = 300) -> int:
    """
    Reset applications stuck in 'processing' state back to 'queued' or paused after an unexpected crash.
    """
    conn = get_db_connection()
    try:
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        with conn:
            cursor = conn.execute("""
            UPDATE applications
            SET status = 'queued', stage_message = 'Recovered stuck task after restart', updated_at = ?
            WHERE status = 'processing'
            """, (now,))
            return cursor.rowcount
    finally:
        conn.close()

def delete_application(app_id: str):
    conn = get_db_connection()
    conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
    conn.commit()
    conn.close()

def normalize_question_key(text: str) -> str:
    """Normalize question text for consistent dictionary/hash lookup."""
    t = (text or "").lower().strip()
    t = re.sub(r'[^\w\s]', '', t, flags=re.UNICODE)
    t = re.sub(r'\s+', ' ', t)
    return t

CANONICAL_QUESTION_KEYS = {
    # Work authorization & visa
    "authorized to work": "work_authorization",
    "legally authorized": "work_authorization",
    "require sponsorship": "visa_sponsorship",
    "visa sponsorship": "visa_sponsorship",
    "require visa": "visa_sponsorship",
    # Availability & notice
    "notice period": "notice_period",
    "how soon can you start": "start_date_availability",
    "available to start": "start_date_availability",
    "available to intern": "internship_availability",
    "work from office": "work_from_office",
    "relocate": "relocation_willingness",
    "remote": "remote_work_preference",
    # Compensation
    "expected salary": "expected_salary",
    "expected ctc": "expected_ctc",
    "current salary": "current_salary",
    "current ctc": "current_ctc",
    # Experience & education
    "years of experience": "years_of_experience",
    "gpa": "academic_gpa",
    "degree": "education_degree",
    "university": "education_institution",
    "college": "education_institution",
    "graduation year": "graduation_year",
    # Contact & Links
    "phone": "contact_phone",
    "mobile": "contact_phone",
    "linkedin": "linkedin_url",
    "github": "github_url",
    "portfolio": "portfolio_url",
    "website": "portfolio_url",
    # EEO / Diversity
    "gender": "eeo_gender",
    "race": "eeo_race",
    "ethnicity": "eeo_race",
    "veteran": "eeo_veteran",
    "disability": "eeo_disability"
}

def generate_question_key(text: str, field_type: str = "text", company_name: str = "", scope: str = "global") -> str:
    """Generate a clean slugified machine-readable key for exact indexing and deduplication."""
    norm = normalize_question_key(text)
    
    # Check for known canonical patterns
    key_base = ""
    for pattern, canon_key in CANONICAL_QUESTION_KEYS.items():
        if pattern in norm:
            key_base = canon_key
            break
            
    if not key_base:
        # Fallback to slugified normalized text (first 6 words or up to 48 chars)
        words = norm.split()[:6]
        key_base = "_".join(words)[:48] if words else "question"

    # Differentiate field type if not standard text
    if field_type and field_type.lower() not in ("text", ""):
        key_base = f"{key_base}_{field_type.lower()}"

    if scope in ["company", "this company"] and company_name:
        comp_slug = re.sub(r'[^\w\s]', '', company_name.lower(), flags=re.UNICODE).strip()
        comp_slug = re.sub(r'\s+', '_', comp_slug)[:20]
        return f"{comp_slug}:{key_base}"
    return key_base

def get_saved_answers() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM saved_answers ORDER BY created_at DESC").fetchall()
    conn.close()
    results = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("options_seen"), str):
            try:
                d["options_seen"] = json.loads(d["options_seen"])
            except Exception:
                d["options_seen"] = []
        # Ensure question_key is not empty
        if not d.get("question_key"):
            d["question_key"] = generate_question_key(d.get("question_pattern") or d.get("normalized_text", ""), d.get("field_type", "text"), d.get("company_name", ""), d.get("scope", "global"))
        results.append(d)
    return results

def get_saved_answer_by_id(ans_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM saved_answers WHERE id = ?", (ans_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get("options_seen"), str):
        try:
            d["options_seen"] = json.loads(d["options_seen"])
        except Exception:
            d["options_seen"] = []
    return d

def create_saved_answer(
    pattern: str,
    answer: str,
    question_key: Optional[str] = None,
    field_type: str = "text",
    category: str = "general",
    scope: str = "global",
    company_name: str = "",
    options_seen: Optional[List[str]] = None,
    source: str = "user",
    last_used: Optional[str] = None
) -> str:
    conn = get_db_connection()
    ans_id = str(uuid.uuid4())
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    norm = normalize_question_key(pattern)
    norm_scope = "this company" if scope in ["company", "this company"] else "global"
    
    q_key = (question_key or "").strip()
    if not q_key:
        q_key = generate_question_key(pattern, field_type, company_name, norm_scope)
        
    opts_json = json.dumps(options_seen or []) if isinstance(options_seen, list) else (options_seen or "[]")
    last_used_val = last_used or now

    conn.execute("""
    INSERT INTO saved_answers (
        id, question_key, question_pattern, normalized_text, field_type, answer, category,
        scope, company_name, options_seen, source, last_used, created_at, updated_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (ans_id, q_key, pattern, norm, field_type, answer, category, norm_scope, company_name, opts_json, source, last_used_val, now, now))
    conn.commit()
    conn.close()
    return ans_id

def update_saved_answer(ans_id: str, updates: Dict[str, Any]):
    """Update fields on an existing saved answer."""
    conn = get_db_connection()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    updates["updated_at"] = now
    if "options_seen" in updates and isinstance(updates["options_seen"], list):
        updates["options_seen"] = json.dumps(updates["options_seen"])
    if "question_pattern" in updates and "normalized_text" not in updates:
        updates["normalized_text"] = normalize_question_key(updates["question_pattern"])
    if "scope" in updates:
        updates["scope"] = "this company" if updates["scope"] in ["company", "this company"] else "global"
    cols = ", ".join([f"{k} = ?" for k in updates.keys()])
    values = list(updates.values()) + [ans_id]
    conn.execute(f"UPDATE saved_answers SET {cols} WHERE id = ?", values)
    conn.commit()
    conn.close()

def touch_saved_answer(ans_id: str):
    """Update last_used timestamp and increment usage_count when an answer is matched and used."""
    try:
        conn = get_db_connection()
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        conn.execute("UPDATE saved_answers SET usage_count = usage_count + 1, last_used = ?, updated_at = ? WHERE id = ?", (now, now, ans_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error touching saved answer: {e}")

def delete_saved_answer(ans_id: str):
    conn = get_db_connection()
    conn.execute("DELETE FROM saved_answers WHERE id = ?", (ans_id,))
    conn.commit()
    conn.close()

def seed_resume_facts_to_memory(profile: Dict[str, Any]):
    """Seed structured candidate facts into answer memory with source='resume'."""
    facts = []
    if profile.get("phone"):
        facts.append(("contact_phone", "Phone Number", profile["phone"], "text", "general"))
    if profile.get("linkedin_url"):
        facts.append(("linkedin_url", "LinkedIn Profile URL", profile["linkedin_url"], "text", "general"))
    if profile.get("github_url"):
        facts.append(("github_url", "GitHub Profile URL", profile["github_url"], "text", "general"))
    if profile.get("portfolio_url"):
        facts.append(("portfolio_url", "Portfolio Website URL", profile["portfolio_url"], "text", "general"))
    if profile.get("location"):
        facts.append(("location_city", "Current City / Location", profile["location"], "text", "general"))
        
    # Education facts
    edus = profile.get("education", [])
    if edus and isinstance(edus, list) and len(edus) > 0:
        first_edu = edus[0]
        if first_edu.get("institution"):
            facts.append(("education_institution", "College / University Name", first_edu["institution"], "text", "experience"))
        if first_edu.get("degree"):
            degree_str = first_edu["degree"]
            if first_edu.get("field_of_study"):
                degree_str += f" in {first_edu['field_of_study']}"
            facts.append(("education_degree", "Degree & Major", degree_str, "text", "experience"))
        if first_edu.get("gpa"):
            facts.append(("academic_gpa", "Academic GPA / Grade", str(first_edu["gpa"]), "text", "experience"))
        if first_edu.get("end_year"):
            facts.append(("graduation_year", "Graduation Year", str(first_edu["end_year"]), "text", "experience"))

    existing_rows = get_saved_answers()
    existing_keys = {r.get("question_key") for r in existing_rows if r.get("question_key")}
    existing_norms = {r.get("normalized_text") for r in existing_rows if r.get("normalized_text")}

    for q_key, pattern, answer, field_type, category in facts:
        norm = normalize_question_key(pattern)
        if q_key not in existing_keys and norm not in existing_norms:
            create_saved_answer(
                pattern=pattern,
                answer=answer,
                question_key=q_key,
                field_type=field_type,
                category=category,
                scope="global",
                source="resume"
            )

def update_worker_heartbeat(status: str = "online", active_app_id: Optional[str] = None):
    conn = get_db_connection()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    conn.execute("""
    INSERT INTO worker_status (id, status, last_heartbeat, active_application_id)
    VALUES ('laptop-worker', ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        status = excluded.status,
        last_heartbeat = excluded.last_heartbeat,
        active_application_id = excluded.active_application_id
    """, (status, now, active_app_id))
    conn.commit()
    conn.close()

def get_worker_status() -> Dict[str, Any]:
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM worker_status WHERE id = 'laptop-worker'").fetchone()
    conn.close()
    return dict(row) if row else {"id": "laptop-worker", "status": "offline"}

def get_persisted_embeddings() -> Dict[str, np.ndarray]:
    """Retrieve all persisted question pattern embeddings from SQLite."""
    conn = get_db_connection()
    rows = conn.execute("SELECT normalized_pattern, embedding, dim FROM embedding_cache").fetchall()
    conn.close()
    cache = {}
    for r in rows:
        try:
            arr = np.frombuffer(r["embedding"], dtype=np.float32)
            if arr.shape[0] == r["dim"]:
                cache[r["normalized_pattern"]] = arr
        except Exception:
            pass
    return cache

def save_persisted_embedding(normalized_pattern: str, embedding: np.ndarray):
    """Save an embedding vector to SQLite keyed by normalized_pattern."""
    if not normalized_pattern or embedding is None:
        return
    conn = get_db_connection()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    blob = embedding.astype(np.float32).tobytes()
    dim = int(embedding.shape[0])
    conn.execute("""
    INSERT INTO embedding_cache (normalized_pattern, embedding, dim, updated_at)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(normalized_pattern) DO UPDATE SET
        embedding=excluded.embedding,
        dim=excluded.dim,
        updated_at=excluded.updated_at
    """, (normalized_pattern, blob, dim, now))
    conn.commit()
    conn.close()

def get_llm_cache(cache_key: str) -> Optional[Any]:
    """Retrieve cached LLM result from persistent SQLite cache."""
    if not cache_key:
        return None
    try:
        conn = get_db_connection()
        row = conn.execute("SELECT response_json FROM llm_cache WHERE cache_key = ?", (cache_key,)).fetchone()
        conn.close()
        if row and row["response_json"]:
            return json.loads(row["response_json"])
    except Exception as e:
        print(f"[LLM Cache] Error reading cache key '{cache_key}': {e}")
    return None

def set_llm_cache(cache_key: str, cache_type: str, response: Any, prompt_hash: str = ""):
    """Store LLM result in persistent SQLite cache."""
    if not cache_key or response is None:
        return
    try:
        conn = get_db_connection()
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        resp_json = json.dumps(response, ensure_ascii=False)
        conn.execute("""
        INSERT INTO llm_cache (cache_key, cache_type, prompt_hash, response_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
            cache_type=excluded.cache_type,
            prompt_hash=excluded.prompt_hash,
            response_json=excluded.response_json,
            created_at=excluded.created_at
        """, (cache_key, cache_type, prompt_hash, resp_json, now))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[LLM Cache] Error writing cache key '{cache_key}': {e}")

def clear_llm_cache(cache_type: Optional[str] = None):
    """Clear LLM cache entries."""
    try:
        conn = get_db_connection()
        if cache_type:
            conn.execute("DELETE FROM llm_cache WHERE cache_type = ?", (cache_type,))
        else:
            conn.execute("DELETE FROM llm_cache")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[LLM Cache] Error clearing cache: {e}")

# Initialize DB on load
init_db()

