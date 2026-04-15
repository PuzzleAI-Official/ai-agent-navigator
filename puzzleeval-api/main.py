import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env BEFORE any other imports that might read env vars
load_dotenv(Path(__file__).parent / ".env", override=True)

# Add PuzzleEval to Python path
PUZZLEEVAL_DIR = Path(__file__).resolve().parent.parent / "PuzzleEval-local"
if str(PUZZLEEVAL_DIR) not in sys.path:
    sys.path.insert(0, str(PUZZLEEVAL_DIR))

# Set provider registry path if not already set
if not os.environ.get("PUZZLEEVAL_PROVIDER_REGISTRY"):
    registry_path = PUZZLEEVAL_DIR / "provider_registry.json"
    if registry_path.exists():
        os.environ["PUZZLEEVAL_PROVIDER_REGISTRY"] = str(registry_path)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routes.runs import router as runs_router
from routes.chat import router as chat_router
from routes.files import router as files_router
from routes.events import router as events_router
from routes.monitoring import router as monitoring_router  # Phase 2: enterprise-gated stubs

app = FastAPI(title="PuzzleEval API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8080", "http://localhost:8081"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(runs_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(files_router, prefix="/api")
app.include_router(events_router, prefix="/api")
app.include_router(monitoring_router, prefix="/api")  # Phase 2: enterprise-only routes


@app.get("/api/health")
async def health():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    return {
        "status": "ok",
        "api_key_set": bool(key and key != "YOUR_KEY_HERE"),
        "provider_registry": os.environ.get("PUZZLEEVAL_PROVIDER_REGISTRY", "not set"),
    }
