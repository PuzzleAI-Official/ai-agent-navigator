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

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routes.runs import router as runs_router
from routes.chat import router as chat_router
from routes.files import router as files_router
from routes.events import router as events_router
from routes.monitoring import router as monitoring_router  # Phase 2: enterprise-gated stubs

logger = logging.getLogger("puzzleeval-api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan handler — clean startup + plugin teardown.

    The new plugins (webhook_receiver, outbound_delivery, voice_realtime)
    each spin up background HTTP/SMTP servers bound to 127.0.0.1 ports on
    first synthesize_input() call. Without an explicit shutdown hook, those
    threads + sockets leak across uvicorn restarts (common during
    development with --reload, and during production deploys). Restarts
    fail to rebind the requested port, fall to ephemeral, and any harness
    pointing at the original port silently breaks.

    This handler:
      1. (startup) Touches the plugin registry so every plugin imports
         and registers, but doesn't bind ports yet (plugins are lazy).
      2. (shutdown) Calls shutdown() on every registered plugin that
         exposes one, so background servers close cleanly. Idempotent —
         each plugin's shutdown() handles being called multiple times.
    """
    # Startup: ensure plugins register (auto-imported via tool_plugins/__init__)
    try:
        from puzzleeval.tool_plugins import list_plugins
        plugin_count = len(list_plugins())
        logger.info("startup: %d tool plugins registered", plugin_count)
    except Exception as exc:  # noqa: BLE001
        logger.warning("startup: plugin registry import failed: %s", exc)

    yield

    # Shutdown: close every plugin's background server
    try:
        from puzzleeval.tool_plugins import list_plugins
        for plugin in list_plugins():
            shutdown_fn = getattr(plugin, "shutdown", None)
            if callable(shutdown_fn):
                try:
                    shutdown_fn()
                    logger.info("shutdown: closed plugin %s", plugin.name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "shutdown: plugin %s.shutdown() raised: %s",
                        plugin.name, exc,
                    )
    except Exception as exc:  # noqa: BLE001
        logger.warning("shutdown: plugin teardown loop failed: %s", exc)


app = FastAPI(title="PuzzleEval API", version="0.1.0", lifespan=lifespan)

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
