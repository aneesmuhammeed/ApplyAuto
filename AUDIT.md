# AUDIT.md: ApplyMate Comprehensive Codebase & Architecture Audit

## 1. Executive Summary & Repository Structure Status

ApplyMate is a local autonomous job application assistant written in Python with Playwright, FastAPI, SQLite, and an optional Next.js frontend, Telegram bot, and CLI.

### Structure & Layout Reconciliation
- **GitHub Origin**: `https://github.com/aneesmuhammeed/ApplyAuto.git` (Remote branch `main`).
- **Workspace Layout**: The active local directory `ApllyAAutomation\` contains the root workspace with `backend/`, `frontend/`, `storage/`, and `tests/`. The git version control is initialized inside `ApllyAAutomation\backend\`.
- **Layout Consistency**:
  - The Python server can be launched cleanly via `python -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000` (or `uvicorn main:app` inside `backend/`).
  - Worker runs via `python backend/worker.py` (or `python worker.py` inside `backend/`).
  - Shared storage is anchored in `storage/` at the repository root via `backend/config.py` (`root_dir / "storage"`), ensuring that neither SQLite (`applymate.db`), API tokens (`api_token.txt`), nor uploaded PDF resumes are ever duplicated or orphaned.
  - The Next.js dashboard is located in `frontend/`, configured to talk to `http://localhost:8000` via `NEXT_PUBLIC_BACKEND_URL`.
  - All test suites in `tests/` and `backend/tests/` pass with **27/27** passing tests in both directory contexts.

---

## 2. Baseline Test Results (`pytest tests -v`)

- **Total Test Cases**: 27
- **Passed**: 27 (100%)
- **Failed**: 0
- **Errors**: 0
- **Skipped**: 0
- **Duration**: ~4m 19s (full browser suites)

### Test Coverage Breakdown
1. `tests/test_conditional_and_dynamic.py` (1 test): Dynamic conditional question reveal, re-scan, and post-fill DOM verification.
2. `tests/test_fixtures_widgets.py` (10 tests): Date parsing (4 formats), dependent dropdowns, dial code matching, edge case fixtures (hidden inputs, React-select, `.select-wrapper`, `.selectable`), female/male order disambiguation, iframe ATS form encapsulation, MUI Autocomplete, native select placeholder skipping, radio button grouping without name attributes, and portaled async React-Select.
3. `tests/test_stub_llm_reliability.py` (5 tests): LLM timeout fallback, JSON corruption fallback, free-text prompt non-invention, option out-of-range bounds checking, and `<think>` tag stripping with index parsing.
4. `tests/test_telegram_bot.py` (8 tests): Quote and whitespace sanitization, placeholder credential detection, valid dispatching, single-attempt `getMe` verification, and format checking.
5. `tests/test_worker_circuit_breaker.py` (3 tests): 
   - Circuit breaker attempt tracking (1, 2, 3), exponential backoff, `manual_required` marking, exclusion from retry batch, and `"paused"` status transition.
   - Persistence of attempt counters in `form_data` across simulated worker restart (counter does not reset to 0).
   - Deduplication of question batch hashes (order-independent SHA-256 hash avoids re-notification).

---

## 3. Detailed Audit Findings Table

| ID | File : Line | Severity | Description | Status |
| :--- | :--- | :--- | :--- | :--- |
| **AUD-001** | `backend/form_filler.py:290` | **Medium** | Unescaped CSS ID selector in `aria-labelledby` traversal: `lbl_target = await ctx.query_selector(f'#{lid}')`. Resolved: Changed to `[id="{lid}"]` attribute selectors throughout `form_filler.py`. | **Resolved** |
| **AUD-002** | `backend/debug_dropdowns.py:21` | **Low** | Unescaped CSS ID selector: `page.query_selector(f"#{aria_controls}")`. Resolved: Changed to attribute selector `[id="{aria_controls}"]`. | **Resolved** |
| **AUD-003** | `backend/field_lookup.py:12` | **Low** | Dual import pattern `from backend.widget_executors import ...` inside `except ImportError`. Causes ambiguity depending on `sys.path`. Standardized on direct module imports. | **Resolved** |
| **AUD-004** | `backend/main.py:53-58` | **High** | Token bootstrap endpoint `/api/auth/token` allows any request originating from `127.0.0.1` or `localhost` without credentials. Resolved: Restricted strictly to configured `DASHBOARD_ORIGIN` check; unauthenticated loopback access blocked. | **Resolved** |
| **AUD-005** | `backend/worker.py:28` | **Medium** | In-memory `self.pages: Dict[str, Page]` cleanup on exit paths. Fixed: Pages now cleanly closed in `finally:` blocks and on failure. | **Resolved** |
| **AUD-006** | `backend/worker.py:900` | **Low** | Worker main polling loop sleeps for `POLL_INTERVAL=2` seconds even when active jobs are immediately pending, adding artificial latency between queue items. Resolved: Immediate consecutive queue processing with minimal 0.1s yielding. | **Resolved** |
| **AUD-007** | `backend/ai_client.py:224` | **Low** | Swallowed exception in `query_local_llm`: `except Exception: pass` when logging debug payload or closing connection hides transport diagnostics. | **Open** |
| **AUD-008** | `backend/config.py:29` | **Low** | Swallowed exception in `get_or_create_api_token`: `except Exception: pass` silently suppresses OS filesystem read errors when reading `api_token.txt`. Resolved: Added explicit logging and file permissions (0o600). | **Resolved** |
| **AUD-009** | `backend/database.py:666, 682, 971` | **Medium** | Swallowed exceptions in database operations. Resolved: Added versioned schema migrations (`PRAGMA user_version`), atomic queue claiming, WAL mode, index guards, 512KB form_data snapshot bounding, and safe corrupted JSON handling. | **Resolved** |
| **AUD-010** | `backend/form_filler.py:62, 95, 153, 311, 358, 441, 536, 1476, 1523` | **Medium** | Swallowed exceptions across DOM inspections: `except Exception: pass` in element bounding box, focus, and blur operations silently ignores DOM detachment or page navigation errors. | **Open** |
| **AUD-011** | `backend/widget_executors.py:687, 734, 742, 749, 783, 895, 909, 1325, 1338, 1367, 1387, 1490` | **Medium** | Excessive `except Exception: pass` blocks in widget scrolling, listbox option discovery, and verification re-reads. | **Open** |
| **AUD-012** | `backend/worker.py:107, 117, 123, 129, 345, 608, 674, 679, 795` | **Medium** | Swallowed exceptions in worker lifecycle: cookie injection, page screenshot capture, and status updates swallow exceptions silently with `pass`. | **Open** |
| **AUD-013** | `backend/resume_parser.py:23` | **Low** | Swallowed exception during pdfplumber text extraction fallback. Resolved: Upgraded regexes for +91 phone numbers and standard "City, State, Country" patterns with clear warning logging on parsing failure. | **Resolved** |
| **AUD-014** | `backend/telegram_bot.py:91` | **Low** | Swallowed exception in `get_telegram_config()`: `except Exception: pass` hides malformed environment parsing errors. | **Open** |
| **AUD-015** | `backend/main.py:334` | **Medium** | Race condition in file upload: `settings.RESUME_STORAGE_DIR` file write was synchronous inside async FastAPI handler. Resolved: Offloaded to worker thread via `asyncio.to_thread`. | **Resolved** |
| **AUD-016** | `backend/database.py:15` | **High** | SQLite concurrency race condition: Resolved by enabling `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` with 30s connection timeout. | **Resolved** |
| **AUD-017** | `backend/worker.py:185` | **Medium** | Missing timeout on application duration: Resolved by adding 15-minute global execution timeout per application with screenshot capture and failed status transition. | **Resolved** |
| **AUD-018** | `backend/ai_client.py:90` | **Medium** | Missing connection/read timeout configuration in `urllib.request` for local llama.cpp completions endpoint. Resolved: Replaced with `httpx.AsyncClient` with configurable 15s timeout and exponential backoff retry. | **Resolved** |
| **AUD-019** | `backend/telegram_bot.py:165` | **Medium** | Missing timeout in `urllib.request.urlopen` for Telegram Bot API calls. Resolved: Configured explicit 6s/8s timeouts and connection verification via `getMe`. | **Resolved** |
| **AUD-020** | `backend/field_lookup.py:37` | **Medium** | Claim vs Implementation: README claims FastEmbed vector similarity retrieval is active, but FastEmbed is optional in `requirements.txt` and fails gracefully to exact-only matching if model weights cannot be downloaded locally. | **Open** |
| **AUD-021** | `README.md:12-14` | **Medium** | Documentation mismatch: README documents running terminal CLI with `python backend/cli.py` and `python backend/field_lookup.py --review-memory`. Resolved: Documented `backend/cli.py list/answer` and `backend/manage_memory.py list/edit/delete`. | **Resolved** |
| **AUD-022** | `backend/.env.example:2-3` | **Low** | Deprecated / dead configuration: `SUPABASE_URL` and `SUPABASE_KEY` are documented in `.env.example`, but ApplyMate was refactored to 100% local SQLite storage and Supabase remote syncing is dormant. | **Open** |
| **AUD-023** | `backend/config.py:42-46` | **Low** | Configuration redundancy: `LLAMA_CPP_URL` / `LOCAL_LLM_ENDPOINT` and `LLAMA_MODEL_NAME` / `LOCAL_LLM_MODEL` exist as redundant aliases in `config.py` and `.env.example`. | **Open** |
| **AUD-024** | `backend/worker.py:950` | **High** | Playwright subprocess pipe closure on Ctrl+C (Windows asyncio): Resolved via `main_worker_entry` with explicit `try/finally` awaiting `close_browser()` before loop teardown. | **Resolved** |
| **AUD-025** | `backend/database.py:720` | **High** | Non-atomic queue polling race: Resolved via `acquire_queued_application()` using atomic `UPDATE ... WHERE status='queued'` transaction. | **Resolved** |
