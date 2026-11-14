# reload-trigger-1
import os
import shutil
from typing import Dict, Any, List
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import settings
from database import (
    get_profile, update_profile, get_applications, get_application,
    create_application, update_application, delete_application,
    get_saved_answers, create_saved_answer, delete_saved_answer,
    get_worker_status
)
from resume_parser import parse_resume

app = FastAPI(title="ApplyMate Local API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs(settings.RESUME_STORAGE_DIR, exist_ok=True)

# Pydantic schemas
class NewApplicationRequest(BaseModel):
    job_url: str
    company_name: str = ""

class UpdateApplicationRequest(BaseModel):
    status: str = None
    stage_message: str = None
    user_answer: str = None
    save_answer_for_future: bool = False
    answer_category: str = "general"
    answer_scope: str = "global"

class SavedAnswerRequest(BaseModel):
    question_pattern: str
    answer: str
    category: str = "general"
    scope: str = "global"
    company_name: str = ""
    field_type: str = "text"

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "ApplyMate Local API (SQLite)"}

# 1. Applications Endpoints
@app.get("/api/applications")
def list_applications():
    return get_applications()

@app.post("/api/applications")
def create_app_task(req: NewApplicationRequest):
    if not req.job_url:
        raise HTTPException(status_code=400, detail="Job URL is required")
    app_id = create_application(req.job_url, req.company_name)
    return {"status": "success", "id": app_id}

@app.get("/api/applications/{app_id}")
def get_app_detail(app_id: str):
    app_data = get_application(app_id)
    if not app_data:
        raise HTTPException(status_code=404, detail="Application not found")
    return app_data

@app.patch("/api/applications/{app_id}")
def update_app_task(app_id: str, req: UpdateApplicationRequest):
    updates = {}
    if req.status is not None:
        updates["status"] = req.status
    if req.stage_message is not None:
        updates["stage_message"] = req.stage_message
    if req.user_answer is not None:
        updates["user_answer"] = req.user_answer
    if req.save_answer_for_future is not None:
        updates["save_answer_for_future"] = 1 if req.save_answer_for_future else 0
    if req.answer_category is not None:
        updates["answer_category"] = req.answer_category
    if req.answer_scope is not None:
        updates["answer_scope"] = req.answer_scope
    
    update_application(app_id, updates)
    return {"status": "success"}

@app.delete("/api/applications/{app_id}")
def remove_application(app_id: str):
    delete_application(app_id)
    return {"status": "success"}

# 2. Worker Status Endpoint
@app.get("/api/worker/status")
def worker_status():
    return get_worker_status()

# 3. Saved Answers Memory Endpoints
@app.get("/api/saved-answers")
def list_saved_answers():
    return get_saved_answers()

@app.post("/api/saved-answers")
def add_saved_answer(req: SavedAnswerRequest):
    ans_id = create_saved_answer(
        pattern=req.question_pattern,
        answer=req.answer,
        field_type=req.field_type,
        category=req.category,
        scope=req.scope,
        company_name=req.company_name,
        source="user"
    )
    return {"status": "success", "id": ans_id}

@app.delete("/api/saved-answers/{ans_id}")
def remove_saved_answer(ans_id: str):
    delete_saved_answer(ans_id)
    return {"status": "success"}

# 4. Profile & Resume Endpoints
@app.get("/api/profile")
def fetch_profile():
    return get_profile()

@app.post("/api/profile")
def save_profile(data: Dict[str, Any]):
    update_profile(data)
    return {"status": "success"}

@app.post("/api/resume/upload")
async def upload_resume(file: UploadFile = File(...)):
    """Upload resume, extract structured info strictly from text, store in local SQLite."""
    allowed_exts = [".pdf", ".docx", ".doc"]
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed_exts:
        raise HTTPException(status_code=400, detail="Only PDF and DOCX files are supported.")

    file_dest = os.path.join(settings.RESUME_STORAGE_DIR, file.filename)
    with open(file_dest, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    parsed = parse_resume(file_dest)

    current_prof = get_profile()
    profile_update = {
        "id": current_prof.get("id") or "default",
        "resume_file_path": os.path.abspath(file_dest),
        "raw_resume_text": parsed.get("raw_text", ""),
        "full_name": parsed.get("full_name") or current_prof.get("full_name", ""),
        "email": parsed.get("email") or current_prof.get("email", ""),
        "phone": parsed.get("phone") or current_prof.get("phone", ""),
        "location": parsed.get("location") or current_prof.get("location", ""),
        "linkedin_url": parsed.get("linkedin_url") or current_prof.get("linkedin_url", ""),
        "github_url": parsed.get("github_url") or current_prof.get("github_url", ""),
        "portfolio_url": parsed.get("portfolio_url") or current_prof.get("portfolio_url", ""),
        "skills": parsed.get("skills") or current_prof.get("skills", []),
        "education": parsed.get("education") or current_prof.get("education", []),
        "projects": parsed.get("projects") or current_prof.get("projects", []),
        "work_experience": parsed.get("work_experience") or current_prof.get("work_experience", []),
        "certifications": parsed.get("certifications") or current_prof.get("certifications", []),
        "summary": parsed.get("summary") or current_prof.get("summary", "")
    }

    update_profile(profile_update)

    return {
        "status": "success",
        "file_name": file.filename,
        "parsed_summary": {
            "full_name": profile_update["full_name"],
            "email": profile_update["email"],
            "phone": profile_update["phone"],
            "location": profile_update["location"],
            "education": profile_update["education"],
            "work_experience": profile_update["work_experience"],
            "projects": profile_update["projects"],
            "skills_count": len(profile_update["skills"])
        }
    }

@app.post("/api/resume/reparse")
def reparse_resume():
    """Re-parse the currently stored resume file and update candidate profile."""
    current_prof = get_profile()
    resume_path = current_prof.get("resume_file_path")
    if not resume_path or not os.path.exists(resume_path):
        raise HTTPException(status_code=404, detail="No resume file found on disk to re-parse. Please upload one first.")
    
    parsed = parse_resume(resume_path)
    profile_update = {
        "id": current_prof.get("id") or "default",
        "resume_file_path": resume_path,
        "raw_resume_text": parsed.get("raw_text", ""),
        "full_name": parsed.get("full_name") or current_prof.get("full_name", ""),
        "email": parsed.get("email") or current_prof.get("email", ""),
        "phone": parsed.get("phone") or current_prof.get("phone", ""),
        "location": parsed.get("location") or current_prof.get("location", ""),
        "linkedin_url": parsed.get("linkedin_url") or current_prof.get("linkedin_url", ""),
        "github_url": parsed.get("github_url") or current_prof.get("github_url", ""),
        "portfolio_url": parsed.get("portfolio_url") or current_prof.get("portfolio_url", ""),
        "skills": parsed.get("skills") or current_prof.get("skills", []),
        "education": parsed.get("education") or current_prof.get("education", []),
        "projects": parsed.get("projects") or current_prof.get("projects", []),
        "work_experience": parsed.get("work_experience") or current_prof.get("work_experience", []),
        "certifications": parsed.get("certifications") or current_prof.get("certifications", []),
        "summary": parsed.get("summary") or current_prof.get("summary", "")
    }
    update_profile(profile_update)
    return {
        "status": "success",
        "message": "Resume successfully re-parsed into structured profile!",
        "profile": get_profile()
    }
