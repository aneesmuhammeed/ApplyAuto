import sqlite3
import json
import os
import re
import time
import uuid
from typing import Dict, Any, List, Optional
from config import settings

DB_PATH = settings.SQLITE_DB_PATH

def get_db_connection():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

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
        location TEXT DEFAULT '',
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
        question_pattern TEXT NOT NULL,
        normalized_text TEXT DEFAULT '',
        answer TEXT NOT NULL,
        category TEXT DEFAULT 'general',
        scope TEXT DEFAULT 'global',
        company_name TEXT DEFAULT '',
        options_seen TEXT DEFAULT '[]',
        is_sensitive INTEGER DEFAULT 0,
        usage_count INTEGER DEFAULT 0,
        created_at TEXT,
        updated_at TEXT
    )
    """)

    # Run column migrations for existing databases
    existing_cols = [c[1] for c in cursor.execute("PRAGMA table_info(saved_answers)").fetchall()]
    if "normalized_text" not in existing_cols:
        cursor.execute("ALTER TABLE saved_answers ADD COLUMN normalized_text TEXT DEFAULT ''")
    if "options_seen" not in existing_cols:
        cursor.execute("ALTER TABLE saved_answers ADD COLUMN options_seen TEXT DEFAULT '[]'")
    if "scope" not in existing_cols:
        cursor.execute("ALTER TABLE saved_answers ADD COLUMN scope TEXT DEFAULT 'global'")
    if "company_name" not in existing_cols:
        cursor.execute("ALTER TABLE saved_answers ADD COLUMN company_name TEXT DEFAULT ''")
    if "field_type" not in existing_cols:
        cursor.execute("ALTER TABLE saved_answers ADD COLUMN field_type TEXT DEFAULT 'text'")
    if "source" not in existing_cols:
        cursor.execute("ALTER TABLE saved_answers ADD COLUMN source TEXT DEFAULT 'user'")
    if "last_used" not in existing_cols:
        cursor.execute("ALTER TABLE saved_answers ADD COLUMN last_used TEXT DEFAULT ''")

    # Migrations for applications table
    app_cols = [c[1] for c in cursor.execute("PRAGMA table_info(applications)").fetchall()]
    if "answer_scope" not in app_cols:
        cursor.execute("ALTER TABLE applications ADD COLUMN answer_scope TEXT DEFAULT 'global'")

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
    return d

def update_profile(data: Dict[str, Any]):
    conn = get_db_connection()
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    profile_id = data.get("id") or "default"
    
    # serialize json lists
    education = json.dumps(data.get("education", []))
    work_exp = json.dumps(data.get("work_experience", []))
    skills = json.dumps(data.get("skills", []))
    projects = json.dumps(data.get("projects", []))
    certs = json.dumps(data.get("certifications", []))
    
    conn.execute("""
    INSERT INTO profiles (
        id, full_name, email, phone, location, linkedin_url, github_url, portfolio_url,
        summary, education, work_experience, skills, projects, certifications,
        resume_file_path, raw_resume_text, updated_at, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        full_name=excluded.full_name,
        email=excluded.email,
        phone=excluded.phone,
        location=excluded.location,
        linkedin_url=excluded.linkedin_url,
        github_url=excluded.github_url,
        portfolio_url=excluded.portfolio_url,
        summary=excluded.summary,
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
        data.get("full_name", ""),
        data.get("email", ""),
        data.get("phone", ""),
        data.get("location", ""),
        data.get("linkedin_url", ""),
        data.get("github_url", ""),
        data.get("portfolio_url", ""),
        data.get("summary", ""),
        education,
        work_exp,
        skills,
        projects,
        certs,
        data.get("resume_file_path", ""),
        data.get("raw_resume_text", ""),
        now,
        now
    ))
    conn.commit()
    conn.close()

def get_applications() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM applications ORDER BY created_at DESC").fetchall()
    conn.close()
    res = []
    for r in rows:
        d = dict(r)
        for json_col in ["form_data", "unanswered_question", "logs"]:
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
    for json_col in ["form_data", "unanswered_question", "logs"]:
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
    
    # JSON serialize any dict/list fields
    for field in ["form_data", "unanswered_question", "logs"]:
        if field in updates and isinstance(updates[field], (dict, list)):
            updates[field] = json.dumps(updates[field])
            
    cols = ", ".join([f"{k} = ?" for k in updates.keys()])
    values = list(updates.values()) + [app_id]
    conn.execute(f"UPDATE applications SET {cols} WHERE id = ?", values)
    conn.commit()
    conn.close()

def delete_application(app_id: str):
    conn = get_db_connection()
    conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
    conn.commit()
    conn.close()

def get_saved_answers() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM saved_answers ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def normalize_question_key(text: str) -> str:
    """Normalize question text for consistent dictionary/hash lookup."""
    t = text.lower().strip()
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', ' ', t)
    return t

def create_saved_answer(
    pattern: str,
    answer: str,
    field_type: str = "text",
    category: str = "general",
    scope: str = "global",
    company_name: str = "",
    options_seen: Optional[List[str]] = None,
    source: str = "user"
) -> str:
    conn = get_db_connection()
    ans_id = str(uuid.uuid4())
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    norm = normalize_question_key(pattern)
    opts_json = json.dumps(options_seen or [])
    conn.execute("""
    INSERT INTO saved_answers (
        id, question_pattern, normalized_text, field_type, answer, category,
        scope, company_name, options_seen, source, last_used, created_at, updated_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (ans_id, pattern, norm, field_type, answer, category, scope, company_name, opts_json, source, now, now, now))
    conn.commit()
    conn.close()
    return ans_id

def touch_saved_answer(ans_id: str):
    """Update last_used timestamp and increment usage_count when an answer is matched."""
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

# Initialize DB on load
init_db()

