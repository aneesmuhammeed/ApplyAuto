# ApplyMate – 100% Local Autonomous AI Job Application Agent

ApplyMate is a private, lightweight personal AI agent that automates the tedious job application process on your machine. It utilizes **SQLite** for zero-cloud local memory, **Playwright** for browser automation with multi-tab isolation, **FastEmbed** for vector similarity retrieval, and a local **llama.cpp** server running **Qwen3-0.6B** for intelligent form comprehension and option evaluation.

---

## 🏗️ Architecture & Core Components

```
┌────────────────────────────────────────────────────────┐
│     User Interfaces (Choose any or use concurrently)   │
│   • Next.js Web Dashboard: http://localhost:3000       │
│   • Interactive Terminal CLI: python backend/cli.py    │
│   • Telegram Bot: Verified Chat ID & inline approvals  │
└───────────────────────────┬────────────────────────────┘
                            │ (Local HTTP REST + Token Auth)
┌───────────────────────────▼────────────────────────────┐
│      FastAPI Backend & SQLite Database: :8000          │
│   • Database: storage/applymate.db (Profiles & Memory) │
│   • Token Auth: X-API-Token header / Bearer auth       │
│   • CORS Restricted to DASHBOARD_ORIGIN                │
└───────────────────────────┬────────────────────────────┘
                            │ (Oldest-First FIFO Queue)
┌───────────────────────────▼────────────────────────────┐
│       Playwright Background Worker (worker.py)         │
│   • Tab Isolation: Distinct Page tab per application   │
│   • Dedicated Widget Executors & Verification Re-Read  │
│   • Local LLM: JSON Option Choice & Sanitized Labels   │
│   • Audit Trail: Pre-submit & Post-submit Screenshots  │
└────────────────────────────────────────────────────────┘
```

---

## 🛠️ Prerequisites & Installation

### 1. System Requirements
- **OS**: Windows, macOS, or Linux
- **Python**: 3.11 or 3.12
- **Node.js**: 18+ (for Web Dashboard)
- **Local LLM Server**: `llama-server` (llama.cpp) or Ollama running Qwen3-0.6B

### 2. Python Environment Setup
```powershell
# Clone the repository
git clone <repo-url>
cd ApplyMate

# Create and activate virtual environment
python -m venv .venv
.venv\Scripts\activate   # On Linux/macOS: source .venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt

# Install Playwright browser binaries
playwright install chromium
```

### 3. Frontend Setup
```powershell
cd frontend
npm install
cd ..
```

---

## 🔑 Environment Configuration (`.env`)

Copy the template file to `.env`:
```powershell
cp .env.example .env
```

| Key | Description | Default / Example |
| :--- | :--- | :--- |
| `LOCAL_LLM_ENDPOINT` | URL of local LLM completions endpoint | `http://127.0.0.1:1234/v1/chat/completions` |
| `LOCAL_LLM_MODEL` | Identifier of model loaded in server | `tensorblock/Qwen_Qwen3-0.6B-GGUF:Q3_K_M` |
| `LLM_CONFIDENCE_THRESHOLD` | Minimum confidence score to accept LLM choice | `0.80` |
| `HEADLESS_BROWSER` | Whether to run Playwright in background (`True`) or visibly (`False`) | `False` |
| `POLL_INTERVAL` | Seconds between queue poll checks | `2` |
| `DASHBOARD_ORIGIN` | Allowed origin for CORS | `http://localhost:3000` |
| `API_TOKEN` | Cryptographically random auth token (auto-generated if unset) | `api_token.txt` |
| `TELEGRAM_BOT_TOKEN` | Optional bot token for notifications & mobile review | `your_bot_token_here` |
| `TELEGRAM_CHAT_ID` | Your personal Telegram chat ID (rejects unauthorized users) | `your_chat_id_here` |

---

## 🚀 How to Run ApplyMate

ApplyMate is modular; start the services in separate terminals:

### 1. Start Local LLM (`llama-server`)
Ensure your local `llama-server` is active on port `1234` with at least `-c 8192` context size:
```powershell
llama-server -m path\to\qwen3-0.6b-q3_k_m.gguf --port 1234 -c 8192
```
> **Note**: Running with `-c 8192` ensures sufficient context window capacity for long job descriptions, custom question evaluation, and system instructions without truncation.

### 2. Start FastAPI Backend Server
```powershell
python -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000
```
- API Documentation available at: **`http://127.0.0.1:8000/docs`**

### 3. Start Background Worker
```powershell
python backend/worker.py
```
- Listens to SQLite `applications` table oldest-first (`ORDER BY created_at ASC`).
- Maintains isolated Playwright browser tabs per application without collisions.

### 4. Start Web Dashboard
```powershell
cd frontend
npm run dev
```
- Access the interface at: **`http://localhost:3000`**

### 5. (Optional) Run Interactive Terminal CLI
To review, answer unknown questions, or manage Answer Memory without opening the browser:
```powershell
# Review pending batches and answer questions directly in terminal
python backend/cli.py

# Inspect, edit, or delete saved answers from Answer Memory
python backend/field_lookup.py --review-memory
```

### 6. (Optional) Run Telegram Bot
To receive notifications and approve job submissions from your phone:
```powershell
python backend/telegram_bot.py
```

---

## 🧩 Supported Widget Types & Executors

ApplyMate avoids brittle string clicks by utilizing dedicated widget executors with live DOM verification:

1. **Text, Email, Phone & Textarea Inputs**:
   - Matches candidate profile facts via deterministic rules and intent mapping.
   - Model-synthesized free text is **never auto-filled**; it is displayed as a suggestion in the batch for your approval.
2. **Native `<select>` Dropdowns**:
   - Skips empty placeholder values (e.g. `-- select --`, `Choose...`).
   - Uses exact-first matching rules (e.g. "Male" will never accidentally match "Female").
3. **Custom Comboboxes & Autocompletes**:
   - Full support for **React-Select**, **Material-UI (MUI)**, **Select2**, and portaled ARIA listboxes.
   - Discovers popups via `aria-controls` / `aria-owns` and handles virtualized listbox scrolling.
4. **Radio Button Groups**:
   - Works seamlessly with standard `name="..."` groups and unnamed container-grouped `<fieldset>` / `role="radiogroup"` elements.
   - Prevents empty label collisions and strictly validates checked state during re-read.
5. **Checkboxes (Consent & Policies)**:
   - Identifies terms, policy, and compliance agreements.
   - Always paused for explicit candidate confirmation unless pre-saved in verified Answer Memory.
6. **Date & Calendar Inputs**:
   - Auto-detects 4 formats from placeholders and DOM types:
     - Native date (`YYYY-MM-DD`)
     - Masked DD/MM/YYYY text (`15/08/2024`)
     - Masked MM/DD/YYYY text (`08/15/2024`)
     - Native Month / Month-Year (`2024-08` / `August 2024`)
   - Verification compares parsed calendar dates rather than superficial strings.
7. **File & Resume Uploads**:
   - Detects hidden inputs, dropzones, and custom file buttons.
   - Validates MIME type and magic bytes (strictly rejects `.doc` or spoofed files) and stores under secure UUID filenames.
8. **Embedded iFrames**:
   - Automatically detects and scopes into ATS iframes (Greenhouse, Lever, Workday) embedded within career marketing portals.

---

## 🛡️ Reliability & Security Safeguards

- **No Hallucinated Fallbacks**: If the LLM returns invalid JSON or low confidence (< 0.80), the system asks you instead of falling back to `options[0]`.
- **Prompt Injection Defense**: Untrusted form labels from third-party sites are truncated to 150 characters and stripped of instruction-like tokens (`ignore instructions`, `[INST]`, `<think>`, control characters).
- **DOM Re-Read Verification**: After filling any field, ApplyMate re-reads the live DOM value. Any discrepancy flags the application with `verification_failed = 1` and strictly blocks submission until confirmed.
- **Submit Context Scoping**: Submit buttons are located strictly inside the detected form context (`ctx`), ignoring generic "Apply with LinkedIn" or "Start Application" buttons.
- **Audit Screenshots**: Automatically saves `{app_id}_pre_submit.png` and `{app_id}_post_submit.png` in `storage/` as photographic proof.

---

## 🧪 Running the Automated Test Suite

```powershell
# Run the complete test suite
pytest tests -v

# Run individual test suites
pytest tests/test_fixtures_widgets.py -v
pytest tests/test_conditional_and_dynamic.py -v
pytest tests/test_stub_llm_reliability.py -v
```

GitHub Actions automatically runs this test suite on every push and pull request across Linux with headless Chromium.
