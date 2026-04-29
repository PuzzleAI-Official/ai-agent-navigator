"""Sandbox + venv + credential management for Agent 5.

This module is the canonical owner of:
  * Per-candidate sandbox directory naming (``candidate_slug``)
  * Virtual-environment creation + race-safe locking
    (``acquire_venv_lock``, ``create_venv``, ``venv_python_path``)
  * Pre-creation of venvs in parallel (``precreate_venvs_for_candidates``)
  * Pre-install of common HTTP/audio dependencies (``preinstall_venv_deps``)
  * Credential resolution from registry + env vars (``resolve_credentials``,
    ``resolve_candidate_credentials``)
  * Env-var name extraction from harness source (``extract_env_vars_from_code``)
  * Env-var similarity scoring (``env_var_similarity``)
  * Sandbox env var assembly (``build_sandbox_env`` — re-exported from
    ``agent5.tools``)

The ``puzzleeval.agents.implement_test_env`` legacy module re-exports every
public name here so source-grep tests + external callers keep working.

Critical invariants (R2 from the plan's risk register):
  * ``_VENV_CREATE_LOCKS`` and ``_VENV_CREATE_LOCKS_GUARD`` are MODULE-LEVEL
    state. Every caller MUST resolve to the SAME dict object — otherwise
    two threads can race for the same sandbox dir and the lock fails to
    serialize. The legacy ``implement_test_env._VENV_CREATE_LOCKS`` name
    is bound to ``sandbox._VENV_CREATE_LOCKS`` via a re-export shim, NOT
    re-declared. Do not redeclare these in any other module.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any

from puzzleeval.provider_registry import _normalize

if TYPE_CHECKING:
    from puzzleeval.schemas import Agent5Input, ScreenedCandidate, TestHarness


# ---------------------------------------------------------------------------
# VENV_PREINSTALL_MANIFEST — packages every harness needs
# ---------------------------------------------------------------------------
# Pre-installed into the venv right after creation. Saves 2-3 Agent 5 turns
# the builder would otherwise spend on `pip install requests` etc.
VENV_PREINSTALL_MANIFEST: list[str] = [
    "requests>=2.31.0",          # HTTP client — every REST harness
    "websocket-client>=1.6.0",   # sync WS — OpenAI Realtime, ElevenLabs
    "pydub>=0.25.1",             # audio format conversion (MP3/WAV/PCM16)
    "soundfile>=0.12.1",         # PCM16 I/O without ffmpeg dependency
    "numpy>=1.26.0",             # array math (pydub dep + audio pipelines)
    "python-dotenv>=1.0.0",      # env loading (common for candidate keys)
]


# ---------------------------------------------------------------------------
# Per-sandbox lock registry — race-safe venv creation
# ---------------------------------------------------------------------------
# Both ``precreate_venvs_for_candidates`` (background, post-selection) and
# ``create_venv`` (synchronous, called from Agent 5 build) can race for the
# same sandbox dir if Agent 4 finishes fast and Agent 5 starts before
# pre-creation completes. The per-sandbox-dir lock ensures whichever call
# gets there first does the work; the other short-circuits when it sees
# the venv python already exists.
#
# CRITICAL: this dict is module-level state. Every caller (sandbox.py,
# implement_test_env.py, pipeline_runner.py) MUST resolve to the SAME
# object. Test ``test_venv_lock_dict_singleton_across_modules_after_move``
# enforces this invariant.
_VENV_CREATE_LOCKS: dict[str, threading.Lock] = {}
_VENV_CREATE_LOCKS_GUARD = threading.Lock()


def candidate_slug(name: str) -> str:
    """Convert a candidate name to a filesystem-safe directory name.

    "Google Document AI" → "google_document_ai"
    "AWS Textract (OCR)" → "aws_textract_ocr"
    """
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    return slug[:40]


def acquire_venv_lock(sandbox_dir: Path) -> threading.Lock:
    """Get-or-create the per-sandbox venv-creation lock.

    Two callers passing the SAME sandbox_dir get the SAME lock object.
    Different sandbox dirs get different locks (no cross-candidate
    contention).
    """
    key = str(sandbox_dir.resolve())
    with _VENV_CREATE_LOCKS_GUARD:
        lock = _VENV_CREATE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _VENV_CREATE_LOCKS[key] = lock
        return lock


def venv_python_path(venv_dir: Path) -> Path:
    """Return the platform-correct path to the venv's python interpreter."""
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def create_venv(
    sandbox_dir: Path,
    logger,
    trace_id: str,
    candidate_name: str,
) -> bool:
    """Create a Python venv in the sandbox directory. Returns True on success.

    The venv is at sandbox_dir/.venv. Commands run via ``run_code()``
    will automatically use it (via ``build_sandbox_env()`` PATH injection).

    After the venv is created, we pre-install ``VENV_PREINSTALL_MANIFEST``
    (small, widely-used HTTP/audio deps). This is best-effort — pre-install
    failures do NOT fail venv creation. Controlled by
    ``PUZZLEEVAL_VENV_PREINSTALL=0`` to disable (debug / minimal-venv runs).

    **Idempotent + race-safe**: when the venv already exists (pre-created
    by ``precreate_venvs_for_candidates`` during Agent 4), short-circuits
    after acquiring the per-sandbox lock. When two callers race for the
    same sandbox, the second one waits, sees the venv exists, and
    short-circuits — no duplicate work, no concurrent pip install.
    """
    venv_dir = sandbox_dir / ".venv"
    vpython = venv_python_path(venv_dir)
    lock = acquire_venv_lock(sandbox_dir)
    with lock:
        if vpython.exists():
            logger.info(
                f"Venv already exists for {candidate_name} — short-circuit",
                extra={
                    "operation": "venv_create_short_circuit",
                    "trace_id": trace_id,
                    "candidate_name": candidate_name,
                    "venv_dir": str(venv_dir),
                },
            )
            return True
        try:
            # `python -m venv .venv` includes pip by default. On
            # Windows+anaconda this can silently fail — the venv's
            # python.exe exists but `python -m pip` returns "No module
            # named pip" (real-run trace 8ded6706). Defense: after
            # creation, verify pip actually landed and run ensurepip
            # as a fallback if not.
            result = subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                # Verify pip actually landed in the venv (Windows+anaconda
                # edge case fix).
                pip_check = subprocess.run(
                    [str(vpython), "-m", "pip", "--version"],
                    capture_output=True, text=True, timeout=15,
                )
                if pip_check.returncode != 0:
                    logger.warning(
                        f"Venv created for {candidate_name} but pip missing — "
                        "bootstrapping via ensurepip",
                        extra={
                            "operation": "venv_pip_missing_ensurepip",
                            "trace_id": trace_id,
                            "candidate_name": candidate_name,
                            "pip_check_stderr": (pip_check.stderr or "")[:300],
                        },
                    )
                    # Best-effort ensurepip — don't fail venv creation
                    subprocess.run(
                        [str(vpython), "-m", "ensurepip", "--upgrade", "--default-pip"],
                        capture_output=True, text=True, timeout=30,
                    )
                logger.info(f"Venv created for {candidate_name}", extra={
                    "operation": "venv_create",
                    "trace_id": trace_id,
                    "candidate_name": candidate_name,
                    "venv_dir": str(venv_dir),
                })
                if os.environ.get("PUZZLEEVAL_VENV_PREINSTALL", "1") != "0":
                    preinstall_venv_deps(venv_dir, logger, trace_id, candidate_name)
                return True
            else:
                logger.warning(
                    f"Venv creation failed for {candidate_name}: {result.stderr[:500]}",
                    extra={
                        "operation": "venv_create_failed",
                        "trace_id": trace_id,
                        "candidate_name": candidate_name,
                    },
                )
                return False
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning(f"Venv creation error for {candidate_name}: {e}", extra={
                "operation": "venv_create_error",
                "trace_id": trace_id,
                "candidate_name": candidate_name,
            })
            return False


def precreate_venvs_for_candidates(
    candidate_names: list[str],
    trace_id: str,
    logger,
    runs_root: "Path | str | None" = None,
    max_workers: int | None = None,
) -> dict[str, bool]:
    """Pre-create venvs for SELECTED candidates in parallel.

    Hook for pipeline_runner: called immediately after the user submits
    Phase 6 selections (or after Phase 7 auto-pick), running concurrently
    with Agent 4 verification. Each candidate's venv lives at the same
    path Agent 5 expects — so when Agent 5 calls ``create_venv`` later,
    it short-circuits via the lock-guarded existence check.

    Saves 60-120s of Agent 5's build wall-clock per candidate by moving
    venv create + ``pip install`` of ``VENV_PREINSTALL_MANIFEST`` into
    the Agent 4 phase's natural slack.

    **Only runs for SELECTED candidates** — pass the post-selection
    filtered name list. Non-selected Agent 2 candidates never get venvs.

    Returns a dict mapping ``candidate_name → success bool``. Failures
    are logged but never raise — Agent 5's ``create_venv`` falls back to
    creating the venv synchronously (legacy path) when a candidate's
    pre-creation didn't complete.
    """
    from puzzleeval.web_doc_cache import candidate_sandbox_dir

    if not candidate_names:
        return {}
    workers = max_workers if max_workers is not None else min(len(candidate_names), 5)
    workers = max(1, workers)

    logger.info(
        f"Pre-creating venvs for {len(candidate_names)} selected candidate(s)",
        extra={
            "operation": "venv_precreate_start",
            "trace_id": trace_id,
            "candidate_count": len(candidate_names),
            "workers": workers,
        },
    )

    results: dict[str, bool] = {}

    def _create_one(name: str) -> tuple[str, bool]:
        try:
            sandbox_dir = candidate_sandbox_dir(trace_id, name, runs_root=runs_root)
            sandbox_dir.mkdir(parents=True, exist_ok=True)
            ok = create_venv(sandbox_dir, logger, trace_id, name)
            return name, ok
        except Exception as exc:  # noqa: BLE001 — must never crash background task
            logger.warning(
                f"venv pre-create exception for {name}: "
                f"{type(exc).__name__}: {exc}",
                extra={
                    "operation": "venv_precreate_exception",
                    "trace_id": trace_id,
                    "candidate_name": name,
                    "error_type": type(exc).__name__,
                    "error_msg": str(exc)[:300],
                },
            )
            return name, False

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_create_one, name) for name in candidate_names]
        for future in as_completed(futures):
            name, ok = future.result()
            results[name] = ok

    succeeded = sum(1 for v in results.values() if v)
    logger.info(
        f"Pre-create complete: {succeeded}/{len(results)} venvs ready",
        extra={
            "operation": "venv_precreate_complete",
            "trace_id": trace_id,
            "succeeded": succeeded,
            "total": len(results),
        },
    )
    return results


def preinstall_venv_deps(
    venv_dir: Path,
    logger,
    trace_id: str,
    candidate_name: str,
) -> None:
    """Best-effort pre-install of common harness deps into a fresh venv.

    Runs ``python -m pip install --quiet <manifest>`` using the venv's
    interpreter. Timeouts at 180s (accommodates slow networks). Logs
    success/failure but never raises — the builder is still responsible
    for writing requirements.txt with any candidate-specific extras, and
    missing packages would surface in the builder's own env_check turn.
    """
    vpython = venv_python_path(venv_dir)
    if not vpython.exists():
        logger.warning(
            f"venv python not found for pre-install: {vpython}",
            extra={"operation": "venv_preinstall_missing_python",
                   "trace_id": trace_id,
                   "candidate_name": candidate_name},
        )
        return
    try:
        result = subprocess.run(
            [str(vpython), "-m", "pip", "install", "--quiet",
             "--disable-pip-version-check", *VENV_PREINSTALL_MANIFEST],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode == 0:
            logger.info(
                f"Pre-installed {len(VENV_PREINSTALL_MANIFEST)} deps "
                f"for {candidate_name}",
                extra={"operation": "venv_preinstall_ok",
                       "trace_id": trace_id,
                       "candidate_name": candidate_name,
                       "packages": VENV_PREINSTALL_MANIFEST},
            )
        else:
            logger.info(
                f"Pre-install partial for {candidate_name}: "
                f"{result.stderr[:400]}",
                extra={"operation": "venv_preinstall_partial",
                       "trace_id": trace_id,
                       "candidate_name": candidate_name},
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.info(
            f"Pre-install failed for {candidate_name} (non-fatal): {exc}",
            extra={"operation": "venv_preinstall_error",
                   "trace_id": trace_id,
                   "candidate_name": candidate_name},
        )


def build_sandbox_env(sandbox_dir: Path, extra_env: dict[str, str] | None = None) -> dict:
    """Build environment variables for subprocess execution in the sandbox.

    Re-exports from ``agent5.tools.build_sandbox_env`` so callers have one
    canonical home for sandbox-related helpers.
    """
    from puzzleeval.agents.agent5 import tools as _tools
    return _tools.build_sandbox_env(sandbox_dir, extra_env)


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------

def resolve_credentials(
    input_data: "Agent5Input",
    candidate: "ScreenedCandidate",
    provider_slug: str,
) -> dict[str, str] | None:
    """Resolve credentials for a candidate from registry or env vars.

    Used during Agent 5 build setup. Tries provider_credentials registry
    first (matched by normalized candidate or provider name), falls back
    to scanning env vars for matching auth-shaped names.
    """
    credentials = None

    if input_data.provider_credentials:
        norm_provider = _normalize(candidate.provider)
        norm_candidate = _normalize(candidate.name)
        credentials = (
            input_data.provider_credentials.get(norm_candidate)
            or input_data.provider_credentials.get(norm_provider)
        )

    if not credentials:
        possible_vars = [f"{provider_slug}_API_KEY"]
        # Also try to extract from any existing harness code
        sandbox_path = (
            Path("runs") / input_data.trace_id
            / "harnesses" / candidate_slug(candidate.name)
        )
        harness_code = _read_harness_code(sandbox_path)
        if harness_code:
            possible_vars.extend(extract_env_vars_from_code(harness_code))

        env_creds: dict[str, str] = {}
        for var in possible_vars:
            val = os.environ.get(var)
            if val:
                env_creds[var] = val
        if env_creds:
            credentials = env_creds

    return credentials


def resolve_candidate_credentials(
    harness: "TestHarness",
    provider_credentials: dict[str, dict[str, str]] | None,
) -> dict[str, str] | None:
    """Resolve credentials for a candidate at test-execution time.

    A candidate can use multiple providers (e.g., an "ElevenLabs Voice
    Stack" harness calls ElevenLabs for TTS + OpenAI Whisper for STT +
    Anthropic Claude for reasoning). Historically this resolver
    short-circuited on the first registry-key match and returned only
    one provider's env vars — harnesses that needed cross-provider keys
    got KeyError on the others.

    General fix: union EVERY registered provider whose normalized key
    appears anywhere in the candidate's searchable surface — name,
    provider, validation_notes. "elevenlabs voice stack" pulls
    ElevenLabs AND OpenAI AND Anthropic when all three are mentioned in
    the candidate's description.

    Env-var fallback (harness.auth_env_vars) still fills in any key
    that wasn't provided via the registry — unchanged.
    """
    credentials: dict[str, str] = {}

    if provider_credentials:
        searchable_parts = [
            getattr(harness, "candidate_name", "") or "",
            getattr(harness, "provider", "") or "",
            getattr(harness, "validation_notes", "") or "",
        ]
        searchable = " ".join(searchable_parts).lower()
        candidate_key = harness.candidate_name.lower().strip()
        provider_key = harness.provider.lower().strip()

        for registry_key, env_dict in provider_credentials.items():
            norm_key = registry_key.lower().strip()
            if not norm_key:
                continue
            if (
                norm_key == candidate_key
                or norm_key == provider_key
                or norm_key in candidate_key
                or candidate_key in norm_key
                or norm_key in provider_key
                or provider_key in norm_key
                or norm_key in searchable  # cross-provider mention match
            ):
                credentials.update(env_dict)
                # Don't break — union ALL matching providers

    if harness.auth_env_vars:
        for var_name in harness.auth_env_vars:
            if var_name not in credentials:
                env_val = os.environ.get(var_name)
                if env_val:
                    credentials[var_name] = env_val

    return credentials if credentials else None


def _read_harness_code(sandbox_dir: Path) -> str | None:
    """Read harness.py from sandbox if it exists."""
    harness_file = sandbox_dir / "harness.py"
    if harness_file.exists():
        try:
            return harness_file.read_text(encoding="utf-8")
        except OSError:
            return None
    return None


def extract_env_vars_from_code(code: str) -> list[str]:
    """Parse harness.py source code to find environment variable names
    used via os.environ.get() or os.environ[].

    Returns a deduplicated, ORDER-PRESERVED list of env var names that
    look like auth-related credentials (containing KEY, TOKEN, SECRET,
    ID, PASSWORD, AUTH, CREDENTIAL, REALM). Ignores generic vars like
    PYTHONDONTWRITEBYTECODE.
    """
    if not code:
        return []

    pattern = r'os\.environ(?:\.get)?\s*[\(\[]\s*["\']([A-Z][A-Z0-9_]*)["\']'
    matches = re.findall(pattern, code)

    auth_keywords = {"KEY", "TOKEN", "SECRET", "ID", "PASSWORD", "AUTH", "CREDENTIAL", "REALM"}
    auth_vars = [v for v in matches if any(kw in v for kw in auth_keywords)]

    seen: set[str] = set()
    result: list[str] = []
    for var in auth_vars:
        if var not in seen:
            seen.add(var)
            result.append(var)
    return result


def env_var_similarity(a: str, b: str) -> float:
    """Compute similarity between two normalized env var names.

    Uses longest common substring ratio. Returns 0.0-1.0.

    "NANONETSAPIKEY" vs "NANONETSINAPIKEY" → high similarity
    "NANONETSAPIKEY" vs "OPENAIKEY" → low similarity
    """
    if not a or not b:
        return 0.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    best = 0
    for i in range(len(shorter)):
        for j in range(i + 3, len(shorter) + 1):  # min substring length 3
            if shorter[i:j] in longer:
                best = max(best, j - i)
    return best / max(len(a), len(b))


__all__ = [
    # Constants
    "VENV_PREINSTALL_MANIFEST",
    # Sandbox naming
    "candidate_slug",
    # Venv management
    "acquire_venv_lock",
    "venv_python_path",
    "create_venv",
    "precreate_venvs_for_candidates",
    "preinstall_venv_deps",
    # Sandbox env
    "build_sandbox_env",
    # Credential resolution
    "resolve_credentials",
    "resolve_candidate_credentials",
    "extract_env_vars_from_code",
    "env_var_similarity",
]
