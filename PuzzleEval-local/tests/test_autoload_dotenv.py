"""Regression test for the .env autoloader's empty-string-shadowing bug.

CI environments (and many shells) commonly declare placeholder env vars
as empty strings (``ANTHROPIC_API_KEY=`` with no value) before later
injecting the real value. Our autoloader uses ``override=False`` so real
shell vars beat the .env file — but stock ``load_dotenv(override=False)``
treats an empty string as "set" and refuses to overwrite, silently
shadowing the .env file's correct value.

The fix in ``puzzleeval/__init__.py:_autoload_dotenv`` evicts empty /
whitespace-only entries from ``os.environ`` BEFORE calling
``load_dotenv`` so the file value reaches the agents. Real (non-empty)
shell values still win.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest


def _write_env(tmp_path: Path, content: str) -> Path:
    p = tmp_path / ".env"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


def test_empty_env_var_does_not_shadow_dotenv(tmp_path, monkeypatch):
    """The original bug: ``ANTHROPIC_API_KEY=`` (empty) in os.environ
    silently swallowed the real value from .env."""
    env_file = _write_env(tmp_path, """
        REAL_KEY=loaded-from-file
    """)
    monkeypatch.setenv("REAL_KEY", "")  # the shadowing empty value
    monkeypatch.chdir(tmp_path)

    # Re-import the autoloader and run it (we can't re-import puzzleeval
    # itself because it's already cached; call the function directly).
    from puzzleeval import _autoload_dotenv
    _autoload_dotenv()

    assert os.environ.get("REAL_KEY") == "loaded-from-file"


def test_real_shell_value_still_wins_over_dotenv(tmp_path, monkeypatch):
    """Non-empty shell values must still beat the .env file (override=False
    semantics) — only empties get evicted."""
    env_file = _write_env(tmp_path, """
        REAL_KEY=from-file
    """)
    monkeypatch.setenv("REAL_KEY", "from-shell")
    monkeypatch.chdir(tmp_path)

    from puzzleeval import _autoload_dotenv
    _autoload_dotenv()

    assert os.environ.get("REAL_KEY") == "from-shell"


def test_whitespace_only_env_var_treated_as_empty(tmp_path, monkeypatch):
    """Whitespace-only is the same footgun pattern."""
    _write_env(tmp_path, """
        SOME_KEY=actual-value
    """)
    monkeypatch.setenv("SOME_KEY", "   ")  # tabs/spaces only
    monkeypatch.chdir(tmp_path)

    from puzzleeval import _autoload_dotenv
    _autoload_dotenv()

    assert os.environ.get("SOME_KEY") == "actual-value"


def test_unset_env_var_loaded_normally(tmp_path, monkeypatch):
    """Sanity: when the var isn't in env at all, the .env value loads."""
    _write_env(tmp_path, """
        BRAND_NEW_KEY=hello
    """)
    monkeypatch.delenv("BRAND_NEW_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    from puzzleeval import _autoload_dotenv
    _autoload_dotenv()

    assert os.environ.get("BRAND_NEW_KEY") == "hello"


def test_keys_not_in_env_file_are_untouched(tmp_path, monkeypatch):
    """Eviction logic must scope to keys defined in the .env file —
    other empty env vars must NOT be wiped."""
    _write_env(tmp_path, """
        DEFINED_IN_FILE=loaded
    """)
    monkeypatch.setenv("DEFINED_IN_FILE", "")
    monkeypatch.setenv("UNRELATED_EMPTY", "")
    monkeypatch.chdir(tmp_path)

    from puzzleeval import _autoload_dotenv
    _autoload_dotenv()

    assert os.environ.get("DEFINED_IN_FILE") == "loaded"
    # UNRELATED_EMPTY should still be present (and still empty)
    assert "UNRELATED_EMPTY" in os.environ
    assert os.environ["UNRELATED_EMPTY"] == ""
