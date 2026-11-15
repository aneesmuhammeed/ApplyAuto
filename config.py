import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

# Ensure .env is loaded from the backend directory regardless of where python is executed
backend_dir = Path(__file__).parent.resolve()
root_dir = backend_dir.parent.resolve()
env_path = backend_dir / ".env"
load_dotenv(dotenv_path=env_path)

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
        except Exception:
            pass

    # Create random token on first run
    new_token = secrets.token_hex(32)
    try:
        token_file.write_text(new_token, encoding="utf-8")
    except Exception as e:
        print(f"[Security Warning] Could not persist API token to {token_file}: {e}")
    return new_token

class Settings:
    def __init__(self):
        self.SQLITE_DB_PATH: str = str(db_file)
        self.POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "2"))
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
            return float(os.getenv("LLM_CONFIDENCE_THRESHOLD", "0.8"))
        except (ValueError, TypeError):
            return 0.8

    @property
    def HEADLESS_BROWSER(self) -> bool:
        return os.getenv("HEADLESS_BROWSER", "False").lower() in ("true", "1", "yes")

settings = Settings()
