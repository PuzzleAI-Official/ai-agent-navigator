# ============================================================================
# PuzzleEval Package
# ============================================================================
# This file makes the `puzzleeval/` directory a Python "package" — meaning
# other code can do `import puzzleeval` or `from puzzleeval import ...`.
#
# We store the version string here so it's accessible from code:
#   import puzzleeval
#   print(puzzleeval.__version__)  # "0.1.0"
# ============================================================================

__version__ = "0.1.0"


# ----------------------------------------------------------------------
# Auto-load .env on FIRST import of the puzzleeval package.
#
# This makes credential pickup work for every entry point, not just
# the CLI:
#
#   - `python -m puzzleeval.cli ...`        → works (CLI's own loader)
#   - `python -c "from puzzleeval ..."`     → works (this loader)
#   - `from puzzleeval.plugin_status import snapshot_all`  → works
#   - `import puzzleeval` from a notebook    → works
#   - pytest                                  → works (tests set their own env)
#
# Walks up from CWD to find the first .env in either the current dir or
# a sibling `puzzleeval-api/` dir (the standard repo layout). Existing
# os.environ values WIN over .env (override=False) so shell vars still
# beat the file.
#
# Silent skip when python-dotenv isn't installed — keeps the package
# importable even before `pip install -e .` completes its dependency
# resolution. Once python-dotenv is installed, the loader runs.
# ----------------------------------------------------------------------
def _autoload_dotenv() -> None:
    try:
        from dotenv import dotenv_values, load_dotenv  # type: ignore
    except ImportError:
        return
    import os
    from pathlib import Path
    cwd = Path.cwd().resolve()
    candidates = []
    for parent in [cwd] + list(cwd.parents):
        candidates.append(parent / ".env")
        candidates.append(parent / "puzzleeval-api" / ".env")
    # Also check next to the package itself (works when imported from
    # arbitrary CWDs — site-packages installs, container layouts, etc.)
    pkg_dir = Path(__file__).resolve().parent
    for parent in pkg_dir.parents:
        candidates.append(parent / ".env")
        candidates.append(parent / "puzzleeval-api" / ".env")
    seen = set()
    for path in candidates:
        path_str = str(path)
        if path_str in seen:
            continue
        seen.add(path_str)
        if path.exists() and path.is_file():
            # Footgun fix: ``load_dotenv(override=False)`` treats ANY existing
            # os.environ entry — including an EMPTY STRING — as "set" and
            # refuses to overwrite. Many CI / shell environments declare
            # placeholders as ``ANTHROPIC_API_KEY=`` (empty) before later
            # injection, which silently shadows a perfectly good .env value.
            # We pre-clear keys whose current value is empty/whitespace-only
            # so the .env file's real value reaches the agents. Real values
            # in os.environ still WIN — only empties get evicted.
            try:
                file_keys = set(dotenv_values(path).keys())
            except Exception:  # noqa: BLE001
                file_keys = set()
            for k in file_keys:
                v = os.environ.get(k, None)
                if v is not None and v.strip() == "":
                    del os.environ[k]
            load_dotenv(path, override=False)
            return  # First match wins


_autoload_dotenv()


# ----------------------------------------------------------------------
# Propagate provider_registry.json credentials into os.environ.
#
# The registry was historically a CANDIDATE-SCOPED credential source —
# Agent 5 read it to inject keys into harness subprocesses. System-level
# plugins (tts, transcription, voice_realtime, webhook_receiver,
# outbound_delivery) read ``os.environ`` directly and were blind to the
# registry. Users had to duplicate OpenAI/ElevenLabs keys across .env
# AND the registry.
#
# This call promotes the registry to a first-class credential source on
# par with .env. After it runs, plugins' ``is_available()`` reads the
# same values the harnesses will, from ONE source of truth.
#
# Runs AFTER ``_autoload_dotenv`` so explicit .env / shell values still
# win (override=False). Empty-string placeholders get evicted first so
# the registry's real value reaches the plugins.
#
# Silent skip when the registry file doesn't exist — the function short-
# circuits and returns an empty registry, nothing propagates. Same for
# malformed JSON. The registry is optional; the pipeline works without it.
# ----------------------------------------------------------------------
def _autoload_provider_registry() -> None:
    try:
        from puzzleeval.provider_registry import load_registry, sync_to_environ
    except ImportError:
        return
    try:
        registry = load_registry()
        sync_to_environ(registry, override=False)
    except Exception:  # noqa: BLE001
        # Never let registry propagation crash import. Any failure here
        # is logged by the caller when they subsequently call load_registry
        # themselves (Agent 5 / CLI path).
        return


_autoload_provider_registry()
