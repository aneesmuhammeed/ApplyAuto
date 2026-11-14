import os
from pathlib import Path
from pydantic import BaseModel
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

class Settings:
    SQLITE_DB_PATH: str = str(db_file)
    LLAMA_CPP_URL: str = os.getenv("LLAMA_CPP_URL", "http://127.0.0.1:1234/v1/chat/completions")
    LLAMA_MODEL_NAME: str = os.getenv("LLAMA_MODEL_NAME", "tensorblock/Qwen_Qwen3-0.6B-GGUF:Q3_K_M")
    POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "2"))
    HEADLESS_BROWSER: bool = False
    RESUME_STORAGE_DIR: str = str(storage_dir)

settings = Settings()
