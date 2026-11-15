import os
import secrets
from pathlib import Path

# Ensure .env is loaded safely handling BOM, CRLF, and quotes
backend_dir = Path(__file__).parent.resolve()
root_dir = backend_dir.parent.resolve()
env_path = backend_dir / ".env"

def safe_load_dotenv(path: Path) -> None:
    """Load .env file with support for UTF-8-SIG (BOM), CRLF, and quoted values."""
    if not path.is_file():
        return
    try:
        content = path.read_text(encoding="utf-8-sig")
    except Exception:
        try:
            content = path.read_text(encoding="latin-1")
        except Exception:
            return

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        # Strip matching quotes if wrapped
        if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
            val = val[1:-1]
        # Do not overwrite variables already explicitly passed in environment
        if key not in os.environ:
            os.environ[key] = val

safe_load_dotenv(env_path)

# Absolute path to storage in project root so it never splits between directories
storage_dir = root_dir / "storage"
storage_dir.mkdir(exist_ok=True)
db_file = storage_dir / "applymate.db"

def get_or_create_api_token(storage_path: Path) -> str:
    """Retrieve or generate cryptographically secure random token on first run."""
    env_token = os.getenv("API_TOKEN") or os.getenv("APPLYMATE_API_TOKEN")
    if env_token and env_token.strip():
        return env_token.strip()

    token_file = storage_path / "api_token.txt"
    if token_file.exists():
        try:
            token = token_file.read_text(encoding="utf-8").strip()
            if token:
                return token
        except Exception as e:
            print(f"[Security Warning] Could not read existing API token from {token_file}: {e}")

    # Create random token on first run
    new_token = secrets.token_hex(32)
    try:
        # Write token file
        token_file.write_text(new_token, encoding="utf-8")
        # Restrict permissions to read/write only by current owner (0o600)
        try:
            token_file.chmod(0o600)
        except Exception:
            pass
    except Exception as e:
        print(f"[Security Warning] Could not persist API token to {token_file}: {e}")
    return new_token

class Settings:
    def __init__(self):
        self.SQLITE_DB_PATH: str = str(db_file)
        
        # Robust integer parsing for POLL_INTERVAL
        try:
            self.POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "2"))
        except (ValueError, TypeError):
            self.POLL_INTERVAL: int = 2
            
        self.RESUME_STORAGE_DIR: str = str(storage_dir)
        self.DASHBOARD_ORIGIN: str = os.getenv("DASHBOARD_ORIGIN", "http://localhost:3000")
        self.API_TOKEN: str = get_or_create_api_token(storage_dir)

    @property
    def LLM_ENDPOINT(self) -> str:
        return os.getenv("LOCAL_LLM_ENDPOINT") or os.getenv("LLAMA_CPP_URL") or "http://127.0.0.1:1234/v1/chat/completions"

    @property
    def LLAMA_CPP_URL(self) -> str:
        return self.LLM_ENDPOINT

    @property
    def LLM_MODEL(self) -> str:
        return os.getenv("LOCAL_LLM_MODEL") or os.getenv("LLAMA_MODEL_NAME") or "tensorblock/Qwen_Qwen3-0.6B-GGUF:Q3_K_M"

    @property
    def LLAMA_MODEL_NAME(self) -> str:
        return self.LLM_MODEL

    @property
    def LLM_CONFIDENCE_THRESHOLD(self) -> float:
        try:
            val = float(os.getenv("LLM_CONFIDENCE_THRESHOLD", "0.8"))
            return max(0.0, min(1.0, val))
        except (ValueError, TypeError):
            return 0.8

    @property
    def HEADLESS_BROWSER(self) -> bool:
        val = str(os.getenv("HEADLESS_BROWSER", "False")).strip().lower()
        return val in ("true", "1", "yes", "on")

settings = Settings()
