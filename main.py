import os
import re
import hmac
import uuid
import shutil
import asyncio
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import settings
from database import (
    get_profile, update_profile, calculate_profile_completeness,
    get_applications, get_application,
    create_application, update_application, delete_application,
    get_saved_answers, create_saved_answer, update_saved_answer, delete_saved_answer,
    get_worker_status
)
from resume_parser import parse_resume

def _write_file_sync(dest: str, data: bytes) -> None:
    with open(dest, "wb") as buffer:
        buffer.write(data)

app = FastAPI(title="ApplyMate Local API", version="2.0.0")

# Security: Restrict CORS strictly to dashboard origins and local loopback
allowed_origins = [
    settings.DASHBOARD_ORIGIN,
    "http://localhost:3000",
    "http://127.0.0.1:3000"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

os.makedirs(settings.RESUME_STORAGE_DIR, exist_ok=True)

# Security: Authentication Middleware enforcing random token header created on first run
@app.middleware("http")
async def verify_token_header(request: Request, call_next):
    # 1. Allow CORS preflight requests
    if request.method == "OPTIONS":
        return await call_next(request)

    # 2. Allow open health checks and OpenAPI docs
    if request.url.path in ["/health", "/docs", "/openapi.json"]:
        return await call_next(request)

    # 3. Allow authorized dashboard origin / local loopback to bootstrap the token
    if request.url.path == "/api/auth/token":
        origin = request.headers.get("origin")
        client_host = request.client.host if request.client else ""
        if origin in allowed_origins or client_host in ["127.0.0.1", "::1", "localhost"]:
            return JSONResponse({"status": "success", "token": settings.API_TOKEN})
        return JSONResponse({"status": "error", "detail": "Unauthorized origin"}, status_code=403)

    # 4. Require X-API-Token or Bearer Authorization header
    token = request.headers.get("X-API-Token")
    if not token and "authorization" in request.headers:
        auth_val = request.headers["authorization"]
        if auth_val.lower().startswith("bearer "):
            token = auth_val[7:].strip()

    if not token or not hmac.compare_digest(str(token), str(settings.API_TOKEN)):
        return JSONResponse(
            status_code=401,
            content={"status": "error", "detail": "Unauthorized: Invalid or missing X-API-Token header"}
        )

    return await call_next(request)

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
    verification_confirmed: Optional[bool] = None
    confirm_verification_failures: Optional[bool] = None

class BatchAnswerItem(BaseModel):
    id: Optional[str] = None
    field_id: Optional[str] = None
    question_key: Optional[str] = None
    label: str
    type: str = "text"
    options: Optional[List[str]] = []
    answer: str
    scope: str = "global"
    save_for_future: bool = True
    category: str = "general"

class BatchAnswerRequest(BaseModel):
    batch_answers: List[BatchAnswerItem]

class SavedAnswerRequest(BaseModel):
    question_pattern: str
    answer: str
    category: str = "general"
    scope: str = "global"
    company_name: str = ""
    field_type: str = "text"

class UpdateSavedAnswerRequest(BaseModel):
    question_pattern: Optional[str] = None
    answer: Optional[str] = None
    category: Optional[str] = None
    scope: Optional[str] = None
    company_name: Optional[str] = None
    field_type: Optional[str] = None

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
    app_data = get_application(app_id)
    if not app_data:
        raise HTTPException(status_code=404, detail="Application not found")

    # If submitting/approving, block approval if verification failed until explicitly confirmed!
    if req.status == "submitting":
        is_confirmed = bool(req.verification_confirmed or req.confirm_verification_failures)
        if app_data.get("verification_failed") and not is_confirmed and not app_data.get("verification_confirmed"):
            raise HTTPException(
                status_code=400,
                detail="Form fields failed verification re-read. Explicit user confirmation is required before approving submission."
            )

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
    if req.verification_confirmed is not None:
        updates["verification_confirmed"] = 1 if req.verification_confirmed else 0
    if req.confirm_verification_failures is not None:
        updates["verification_confirmed"] = 1 if req.confirm_verification_failures else 0
    
    update_application(app_id, updates)
    return {"status": "success"}

@app.post("/api/applications/{app_id}/answer_batch")
def answer_application_batch(app_id: str, req: BatchAnswerRequest):
    app_data = get_application(app_id)
    if not app_data:
        raise HTTPException(status_code=404, detail="Application not found")

    answers_list = [item.dict() for item in req.batch_answers]

    # Queue answers for worker to apply through widget executors, verify in live DOM,
    # and only then persist the verified option text to Answer Memory.
    update_application(app_id, {
        "status": "processing",
        "batch_answers": answers_list,
        "unanswered_questions": [],
        "unanswered_question": None,
        "stage_message": f"Received {len(answers_list)} answers. Applying and verifying via widget executors..."
    })

    return {
        "status": "success",
        "received_count": len(answers_list),
        "message": "Answers queued for worker verification and memory persistence."
    }

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

@app.patch("/api/saved-answers/{ans_id}")
def edit_saved_answer(ans_id: str, req: UpdateSavedAnswerRequest):
    updates = {k: v for k, v in req.dict().items() if v is not None}
    if not updates:
        return {"status": "noop", "message": "No updates provided"}
    update_saved_answer(ans_id, updates)
    return {"status": "success", "id": ans_id}

# 4. Profile & Resume Endpoints
@app.get("/api/profile")
def fetch_profile():
    return get_profile()

@app.post("/api/profile")
def save_profile(data: Dict[str, Any]):
    update_profile(data)
    return {"status": "success", "profile": get_profile()}

@app.get("/api/profile/completeness")
def get_profile_completeness_status():
    prof = get_profile()
    return calculate_profile_completeness(prof)

@app.post("/api/profile/completeness")
def save_profile_completeness_items(data: Dict[str, Any]):
    update_profile(data)
    prof = get_profile()
    comp = calculate_profile_completeness(prof)
    return {
        "status": "success",
        "completeness": comp,
        "profile": prof
    }

# Security: Strict resume upload policies
MAX_RESUME_SIZE = 10 * 1024 * 1024  # 10 MB limit
ALLOWED_EXTENSIONS = {".pdf", ".docx"}
ALLOWED_MIME_TYPES = {
    "application/pdf",
    "application/x-pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/zip",
    "application/x-zip-compressed",
    "application/octet-stream"
}

@app.post("/api/resume/upload")
async def upload_resume(file: UploadFile = File(...)):
    """
    Secure Resume Upload:
    1. Rejects .doc explicitly and restricts extensions strictly to .pdf and .docx.
    2. Enforces maximum file size limit (10MB).
    3. Validates MIME type and magic file signatures (%PDF- and PK\x03\x04).
    4. Saves file under a cryptographically random UUID filename to prevent traversal and overwrites.
    """
    original_name = os.path.basename(file.filename or "")
    ext = os.path.splitext(original_name)[1].lower()

    # Reject .doc explicitly and enforce allowed extensions
    if ext == ".doc":
        raise HTTPException(
            status_code=400,
            detail="Legacy .doc format is rejected for security reasons. Please upload .pdf or .docx."
        )
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Only PDF and modern DOCX files are supported."
        )

    # Validate MIME type
    content_type = (file.content_type or "").lower().split(";")[0].strip()
    if content_type and content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid MIME type: '{content_type}'. Only PDF and DOCX files are allowed."
        )

    # Read contents and validate size
    contents = await file.read()
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(contents) > MAX_RESUME_SIZE:
        raise HTTPException(status_code=400, detail="File size exceeds maximum allowed limit (10MB).")

    # Validate file magic bytes
    if ext == ".pdf" and not contents.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="File header does not match valid PDF signature.")
    if ext == ".docx" and not contents.startswith(b"PK\x03\x04"):
        raise HTTPException(status_code=400, detail="File header does not match valid DOCX package signature.")

    # Save under a random UUID-based name to avoid collisions and path manipulation
    safe_filename = f"{uuid.uuid4().hex}{ext}"
    file_dest = os.path.join(settings.RESUME_STORAGE_DIR, safe_filename)
    
    # Offload blocking disk write to threadpool
    await asyncio.to_thread(_write_file_sync, file_dest, contents)

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
        "file_name": original_name,
        "stored_file_name": safe_filename,
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

if __name__ == "__main__":
    import uvicorn
    # Bind explicitly to 127.0.0.1 loopback
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
